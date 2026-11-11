"""Area `profiles`: creating, testing, deleting and logging in auth profiles (docs/04 §5).

- `profiles.yaml` is written atomically and keeps its mode. A secret goes only to the keychain
  (`arr.auth.store_secret`), never through an API model (W1-06.1 review).
- `profile-status.json` next to it remembers `secret_present`, `last_test` and the limit
  window, so listing profiles needs neither a model call nor the keychain, whose access can
  raise a system dialog. It holds no secret.
- Every access to the keychain runs in a thread: a system dialog must not stop the service.
- `_lock` keeps every change of `profiles.yaml` whole, also while a keychain dialog is open.
- Deleting removes the keychain item and the login folder, but only below the profile's own
  folder in `app_dir()` after `resolve`; `~/.codex` and `login_dir`s elsewhere stay. The
  keychain item goes last: if something fails, the profile stays and can be deleted again.
- Logins run in `arr.service.login.LoginService`, and only in arr's own login folders.
- The Not-Aus (W3-06): `disable_profile` writes `disabled` first, so nothing new starts, then
  ends open logins and stops the runs of this workfolder, those of `arr run` too. A run in
  another workfolder sees the block itself (`watch_blocked_profiles`). `plan_run_input` and
  the agent's start refuse a blocked profile (`refuse_disabled`). „Login entfernen“ is a second,
  explicit step on a blocked profile and removes only what belongs to arr.

Listing belongs to `catalog`, which uses `profile_info` from here.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from arr.auth import (
    AuthError,
    SecretNotReplaced,
    delete_secret,
    owns_keychain_item,
    read_secret,
    replace_secret,
    store_secret,
)
from arr.backends.codex_config import codex_home_for
from arr.backends.codex_login import own_login_folder
from arr.errors import ArrError
from arr.models import AuthKind, AuthProfile, ProfilesFile, Provider
from arr.permissions.gatekeeper import run_profiles
from arr.resolve import dump_yaml_model
from arr.runs.launch import PROFILES_FILE, load_profiles
from arr.service.api_models import (
    ApiException,
    ApiModel,
    CreateProfileRequest,
    ErrorCode,
    LoginStart,
    LoginStatus,
    ProfileActionResult,
    ProfileConcurrency,
    ProfileInfo,
    ProfileTest,
    UsageWindow,
)
from arr.service.login import LoginService
from arr.service.profile_check import check_profile, to_profile_test
from arr.service.real_api.base import ServiceState, from_arr_error, not_allowed, not_found
from arr.settings import app_dir

log = logging.getLogger("arr.service")

STATUS_FILE = "profile-status.json"
MAC_LOGIN_STAYS = (
    "„{name}“ nutzt den Claude-Login dieses Macs. Er gehört nicht arr, deshalb entfernt arr "
    "nichts. Abmelden kannst du dich in Claude Code selbst."
)
FOREIGN_STAYS = (
    "„{name}“ verweist auf einen Eintrag außerhalb von arr. arr ändert dort nichts, deshalb "
    "wurde nichts entfernt."
)
NO_LOGIN = "„{name}“ hat keinen Login, den arr entfernen könnte."
PROFILES_HEADER = "# Auth-Profile von arr (docs/02 §1): nur Namen und Art, keine Secrets.\n"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProfileStatus(ApiModel):
    """What arr remembers about a profile between lists; no secret."""

    secret_present: bool = False
    last_test: ProfileTest | None = None
    usage_window: UsageWindow | None = None


def load_status() -> dict[str, ProfileStatus]:
    """`profile-status.json`; a missing or damaged file is an empty one."""
    try:
        raw = json.loads((app_dir() / STATUS_FILE).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {name: ProfileStatus.model_validate(item) for name, item in raw.items()}
    except (OSError, ValueError):
        return {}


def unreadable_profiles() -> ApiException:
    """`profiles.yaml` cannot be read (docs/08 §8.4)."""
    return ApiException.of(
        ErrorCode.CONFIG_INVALID,
        "Die Datei profiles.yaml ließ sich nicht lesen. Sie ist beschädigt oder nicht "
        "zugänglich. Prüfe sie im Finder und versuche es erneut.",
    )


def not_writable(name: str) -> ApiException:
    """The folder of arr takes no change of the profile (docs/08 §8.4)."""
    return ApiException.of(
        ErrorCode.UNAVAILABLE,
        f"Das Profil „{name}“ ließ sich nicht speichern. Der Ordner von arr ist nicht "
        "beschreibbar. Prüfe die Rechte im Finder und versuche es erneut.",
    )


def _write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def save_status(status: dict[str, ProfileStatus]) -> None:
    raw = {name: item.model_dump(mode="json") for name, item in status.items()}
    _write_atomic(app_dir() / STATUS_FILE, json.dumps(raw, ensure_ascii=False, indent=2), 0o600)


def provider_of(kind: AuthKind) -> Provider:
    codex = (AuthKind.CODEX_CHATGPT, AuthKind.OPENAI_API_KEY)
    return Provider.CODEX if kind in codex else Provider.CLAUDE


def profile_info(
    state: ServiceState, profile: AuthProfile, status: ProfileStatus | None
) -> ProfileInfo:
    """A profile for the API: no secret, no model call, no access to the keychain."""
    known = status or ProfileStatus()
    gate = state.gates.get(profile.name)
    login_dir = None
    present = known.secret_present
    if profile.kind is AuthKind.CODEX_CHATGPT:
        home = codex_home_for(profile)
        login_dir = str(home)
        present = (home / "auth.json").is_file()
    keychain = None
    if profile.keychain is not None:
        keychain = f"{profile.keychain.service}/{profile.keychain.account}"
    elif present and login_dir is None:
        keychain = f"arr/{profile.name}"
    settings = state.settings
    return ProfileInfo(
        name=profile.name,
        kind=profile.kind,
        provider=provider_of(profile.kind),
        default_for=[p for p, d in settings.defaults.items() if d.auth == profile.name],
        concurrency=ProfileConcurrency(
            limit=settings.concurrency.get(profile.name, 0),
            in_use=gate.active if gate is not None else 0,
        ),
        secret_present=present,
        keychain_label=keychain,
        login_dir=login_dir,
        last_test=known.last_test,
        usage_window=known.usage_window,
        models=known.last_test.models if known.last_test else [],
        disabled=profile.disabled,
    )


def disabled_text(name: str, verb: str) -> str:
    """Why a blocked profile cannot be tested or logged in; the CLI says the same (W4-00 B10)."""
    return f"„{name}“ ist gesperrt. Entsperre es, dann kannst du es wieder {verb}."


def running_runs(count: int) -> str:
    """„1 laufender Run“, „2 laufende Runs“ (W4-00 B12)."""
    return f"{count} laufender Run" if count == 1 else f"{count} laufende Runs"


def _be(count: int, one: str, more: str) -> str:
    """The singular or plural word for `count`."""
    return one if count == 1 else more


class ProfilesArea:
    """Profile methods of `ApiClient` (see the module docstring)."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state
        self.logins = LoginService(
            self._after_login, poll_url=self._poll_url, undone=self._login_undone
        )
        self._lock = asyncio.Lock()
        self.stop_run: Callable[[str], Awaitable[None]] | None = None
        """Stops a run, set by `RealApi` (`RunsArea.stop_for_profile`)."""

    def _poll_url(self, name: str) -> str:
        return self.state.gui_url(f"/api/profiles/{name}/login")

    # -- profiles.yaml ----------------------------------------------------------------------

    def _load(self) -> ProfilesFile:
        """The file as it is now (it may have been edited by hand since the start)."""
        try:
            if (app_dir() / PROFILES_FILE).is_file():
                return load_profiles()
        except ArrError as exc:
            raise from_arr_error(exc, ErrorCode.CONFIG_INVALID) from None
        except (UnicodeDecodeError, OSError):
            raise unreadable_profiles() from None
        return self.state.profiles_or_empty()

    def _save(self, file: ProfilesFile) -> None:
        path = app_dir() / PROFILES_FILE
        mode = path.stat().st_mode & 0o777 if path.is_file() else 0o600
        _write_atomic(path, PROFILES_HEADER + dump_yaml_model(file), mode)
        self.state._profiles = file

    def _find(self, name: str) -> AuthProfile:
        profile = next((p for p in self._load().profiles if p.name == name), None)
        if profile is None:
            raise not_found("Profil", name)
        return profile

    def find(self, name: str) -> AuthProfile:
        """The profile by a name from outside; unknown names are `not_found`."""
        if not _NAME.match(name):
            raise not_found("Profil", name)
        return self._find(name)

    def _update_status(self, name: str, **changes: object) -> ProfileStatus:
        status = load_status()
        item = status.get(name, ProfileStatus()).model_copy(update=changes)
        status[name] = item
        try:
            save_status(status)
        except OSError:
            raise not_writable(name) from None
        return item

    def _info(self, profile: AuthProfile) -> ProfileInfo:
        return profile_info(self.state, profile, load_status().get(profile.name))

    # -- test -------------------------------------------------------------------------------

    async def test_profile(self, name: str) -> ProfileInfo:
        profile = self._find(name)
        self._refuse_disabled(profile, "testen")
        started = time.time()
        result = await check_profile(profile, self.state.secret_reader)
        changes: dict[str, object] = {"last_test": to_profile_test(profile, result, started)}
        if result.window is not None:
            changes["usage_window"] = result.window
        if result.secret_present is not None:
            changes["secret_present"] = result.secret_present
        self._update_status(name, **changes)
        return self._info(profile)

    # -- create, delete ---------------------------------------------------------------------

    async def _replace_key(self, request: CreateProfileRequest) -> ProfileInfo:
        """„Key ersetzen“: only the keychain item changes, see `replace_secret`.

        A status that cannot be written is another failure: the new key lies there already.
        """
        profile = self._find(request.name)
        if profile.kind is not request.kind:
            raise ApiException.of(
                ErrorCode.INVALID,
                f"„{request.name}“ ist kein Profil dieser Art. Lege ein neues Profil an.",
            )
        assert request.api_key is not None  # checked by CreateProfileRequest
        reader = self.state.secret_reader
        try:
            value = request.api_key.get_secret_value()
            await asyncio.to_thread(replace_secret, profile, value, reader)
        except SecretNotReplaced as exc:
            self._note_failed_replacement(profile, exc.present)
            raise from_arr_error(exc, ErrorCode.UNAVAILABLE) from None
        except AuthError as exc:
            raise from_arr_error(exc, ErrorCode.UNAVAILABLE) from None
        try:
            self._update_status(request.name, secret_present=True, last_test=None)
        except ApiException:
            raise ApiException.of(
                ErrorCode.UNAVAILABLE,
                f"Der neue Key von „{request.name}“ liegt im Schlüsselbund. Nur der Status des "
                "Profils ließ sich nicht speichern, die Karte zeigt noch den alten Test. Prüfe "
                "die Rechte im Finder und teste das Profil.",
            ) from None
        return self._info(profile)

    def _note_failed_replacement(self, profile: AuthProfile, present: bool | None) -> None:
        """The card shows what really lies in the keychain; the key's error comes first."""
        if present is None:
            return
        changes: dict[str, object] = {"secret_present": present}
        if not present:
            changes["last_test"] = None
        with contextlib.suppress(ApiException):
            self._update_status(profile.name, **changes)

    async def create_profile(self, request: CreateProfileRequest) -> ProfileInfo:
        async with self._lock:
            if request.replace:
                return await self._replace_key(request)
            return await self._create(request)

    async def _create(self, request: CreateProfileRequest) -> ProfileInfo:
        if any(p.name == request.name for p in self._load().profiles):
            raise ApiException.of(
                ErrorCode.CONFLICT,
                f"Ein Profil „{request.name}“ gibt es schon. Wähle einen anderen Namen.",
                field="name",
            )
        profile = AuthProfile(name=request.name, kind=request.kind)
        if request.api_key is not None:
            try:
                value = request.api_key.get_secret_value()
                await asyncio.to_thread(store_secret, profile, value)
            except AuthError as exc:
                raise from_arr_error(exc, ErrorCode.UNAVAILABLE) from None
        try:
            # read again: a keychain dialog can take minutes, the file may have changed
            file = self._load()
            self._save(file.model_copy(update={"profiles": [*file.profiles, profile]}))
        except (OSError, ApiException) as exc:
            with contextlib.suppress(AuthError):
                await asyncio.to_thread(delete_secret, profile)
            if isinstance(exc, ApiException):
                raise
            raise not_writable(request.name) from None
        self._update_status(
            request.name, secret_present=request.api_key is not None, last_test=None
        )
        return self._info(profile)

    def _run_ids(self, name: str) -> list[str]:
        """Active runs of the workfolder on this profile, also as the profile of their
        supervising agent (W4-04.1, M4); one that cannot say counts."""
        ids = []
        for record in self.state.store.scan():
            if record.status.is_active:
                resolved = record.load_resolved()
                if resolved is None or name in run_profiles(resolved, self.state.settings):
                    ids.append(record.run_id)
        return ids

    def _active_runs(self, name: str) -> list[str]:
        """Runs that use the profile right now: of this service and of `arr run` beside it."""
        gate = self.state.gates.get(name)
        ids = ["dieser Dienst"] if gate is not None and gate.active else []
        return ids + self._run_ids(name)

    async def delete_profile(self, name: str) -> None:
        if not _NAME.match(name):
            raise not_found("Profil", name)
        async with self._lock:
            await self._delete(name)

    async def _delete(self, name: str) -> None:
        profile = self._find(name)
        if self._active_runs(name):
            raise not_allowed(
                f"„{name}“ wird gerade von laufenden Runs genutzt. Warte, bis sie fertig sind."
            )
        await self.logins.cancel(name)
        file = self._load()
        rest = [p for p in file.profiles if p.name != name]
        try:
            self._save(file.model_copy(update={"profiles": rest}))
        except OSError:
            raise not_writable(name) from None
        try:
            await self._remove_secrets(profile)
        except ApiException:
            with contextlib.suppress(OSError):  # the profile stays, deleting can be repeated
                self._save(file)
            raise
        status = load_status()
        if status.pop(name, None) is not None:
            with contextlib.suppress(OSError):  # create_profile starts it anew anyway
                save_status(status)

    async def _remove_secrets(self, profile: AuthProfile) -> None:
        """Remove login folder and keychain item, the item last; says what could not go."""
        folder = own_login_folder(profile)
        if folder is not None:
            await asyncio.to_thread(shutil.rmtree, folder, True)
            if folder.exists():
                what = (
                    "Der Login (auth.json) liegt noch"
                    if (codex_home_for(profile) / "auth.json").exists()
                    else "Reste liegen noch"
                )
                raise ApiException.of(
                    ErrorCode.UNAVAILABLE,
                    f"Das Profil „{profile.name}“ ließ sich nicht löschen. {what} im Login-Ordner, "
                    "er ist nicht beschreibbar. Prüfe die Rechte im Finder und lösche das Profil "
                    "erneut.",
                )
        elif profile.kind is AuthKind.CODEX_CHATGPT:
            log.info("Profil %s: Login-Ordner liegt außerhalb von arr und bleibt.", profile.name)
        try:
            await asyncio.to_thread(delete_secret, profile)
        except AuthError as exc:
            raise from_arr_error(exc, ErrorCode.UNAVAILABLE) from None

    # -- Not-Aus ----------------------------------------------------------------------------

    def _refuse_disabled(self, profile: AuthProfile, verb: str) -> None:
        if profile.disabled:
            raise not_allowed(disabled_text(profile.name, verb))

    def _write_disabled(self, name: str, disabled: bool) -> AuthProfile:
        file = self._load()
        changed = [
            p.model_copy(update={"disabled": disabled}) if p.name == name else p
            for p in file.profiles
        ]
        try:
            self._save(file.model_copy(update={"profiles": changed}))
        except OSError:
            raise not_writable(name) from None
        return next(p for p in changed if p.name == name)

    async def _stop_runs(self, ids: list[str]) -> list[str]:
        """Stop every run and go on after a failure; returns the runs that could not be stopped."""
        failed = []
        for run_id in ids:
            try:
                if self.stop_run is None:
                    raise RuntimeError("no stop_run")
                await self.stop_run(run_id)
            except Exception as exc:  # noqa: BLE001 - the next run must still be stopped
                log.warning("Not-Aus, Run %s: %s", run_id, type(exc).__name__)
                record = self.state.store.get(run_id)
                if record is None or not record.final:
                    failed.append(run_id)
        return failed

    def _find_for_block(self, name: str) -> tuple[AuthProfile | None, ApiException | None]:
        """The profile, or the reason the file cannot be read: the runs stop either way."""
        if not _NAME.match(name):
            raise not_found("Profil", name)
        try:
            return self._find(name), None
        except ApiException as exc:
            if exc.error.code is ErrorCode.NOT_FOUND:
                raise
            return None, exc

    async def disable_profile(self, name: str, dry_run: bool = False) -> ProfileActionResult:
        """Block the profile, then stop the runs of this workfolder on it.

        If the block cannot be written (or `profiles.yaml` not read), the runs here stop all
        the same and the answer says both (review W3-06, B9): runs in other workfolders only
        see a saved block.
        """
        profile, unsaved = self._find_for_block(name)
        if dry_run:
            ids = self._run_ids(name)
            text = f"{running_runs(len(ids))} {_be(len(ids), 'würde', 'würden')} gestoppt."
            text = text if ids else "Es läuft kein Run."
            info = self._info(profile) if profile is not None else None
            return ProfileActionResult(message=text, runs=ids, profile=info)
        if unsaved is None:
            try:
                async with self._lock:
                    profile = self._write_disabled(name, True)
            except ApiException as exc:
                unsaved = exc
        await self.logins.cancel(name)
        ids = self._run_ids(name)
        failed = await self._stop_runs(ids)
        if unsaved is not None:
            return self._unsaved_block(name, unsaved, ids, failed, profile)
        text = f"„{name}“ ist gesperrt. "
        if failed:
            n = len(failed)
            text += (
                f"{n} {_be(n, 'Run ließ', 'Runs ließen')} sich nicht stoppen: "
                f"{', '.join(failed)}. Beende {_be(n, 'den Prozess', 'die Prozesse')} im Terminal."
            )
        elif ids:
            text += (
                f"{running_runs(len(ids))} {_be(len(ids), 'wird', 'werden')} gestoppt, danach "
                "laufen Zusammenfassung und Scan."
            )
        else:
            text += "Es lief kein Run."
        return ProfileActionResult(
            ok=not failed,
            message=text,
            runs=[run_id for run_id in ids if run_id not in failed],
            failed_runs=failed,
            profile=self._info(profile),
        )

    def _unsaved_block(
        self,
        name: str,
        cause: ApiException,
        ids: list[str],
        failed: list[str],
        profile: AuthProfile | None,
    ) -> ProfileActionResult:
        text = f"„{name}“ ist nicht gesperrt: {cause.error.message} "
        stopped = [run_id for run_id in ids if run_id not in failed]
        if stopped:
            n = len(stopped)
            text += f"{running_runs(n)} hier {_be(n, 'wird', 'werden')} trotzdem gestoppt. "
        if failed:
            n = len(failed)
            text += (
                f"Nicht stoppen {_be(n, 'ließ', 'ließen')} sich: {', '.join(failed)}. Beende "
                f"{_be(n, 'ihn', 'sie')} im Terminal. "
            )
        if not ids:
            text += "Hier lief kein Run. "
        text += "Runs in anderen Workfoldern laufen weiter, bis die Sperre gespeichert ist."
        return ProfileActionResult(
            ok=False,
            message=text,
            runs=stopped,
            failed_runs=failed,
            profile=self._info(profile) if profile is not None else None,
        )

    async def enable_profile(self, name: str) -> ProfileActionResult:
        self.find(name)
        async with self._lock:
            profile = self._write_disabled(name, False)
        return ProfileActionResult(
            message=f"„{name}“ ist entsperrt. Neue Runs dürfen es wieder nutzen.",
            profile=self._info(profile),
        )

    async def _has_secret(self, profile: AuthProfile) -> bool:
        try:
            found = await asyncio.to_thread(read_secret, profile, self.state.secret_reader)
        except AuthError as exc:
            raise from_arr_error(exc, ErrorCode.UNAVAILABLE) from None
        return found is not None

    async def _nothing_to_remove(self, profile: AuthProfile) -> str | None:
        """Why „Login entfernen“ leaves everything as it is; None if there is something."""
        name = profile.name
        if profile.kind is AuthKind.CODEX_CHATGPT:
            if own_login_folder(profile) is None:
                return FOREIGN_STAYS.format(name=name)
            return None if codex_home_for(profile).exists() else NO_LOGIN.format(name=name)
        if not owns_keychain_item(profile):
            return FOREIGN_STAYS.format(name=name)
        if await self._has_secret(profile):
            return None
        if profile.kind is AuthKind.CLAUDE_OAUTH:
            return MAC_LOGIN_STAYS.format(name=name)
        return NO_LOGIN.format(name=name)

    async def remove_login(self, name: str, dry_run: bool = False) -> ProfileActionResult:
        profile = self.find(name)
        if not profile.disabled:
            raise not_allowed(f"Sperre „{name}“ zuerst. Erst dann lässt sich der Login entfernen.")
        async with self._lock:
            stays = await self._nothing_to_remove(profile)
            if stays is not None:
                return ProfileActionResult(message=stays, profile=self._info(profile))
            if dry_run:
                text = f"Der Login von „{name}“ würde aus arr entfernt."
                return ProfileActionResult(
                    message=text, removable=True, profile=self._info(profile)
                )
            if self._active_runs(name):
                raise not_allowed(
                    f"„{name}“ stoppt noch. Warte, bis die Runs beendet sind, und entferne den "
                    "Login dann."
                )
            await self.logins.cancel(name)
            await self._remove_secrets(profile)
            self._update_status(name, secret_present=False, last_test=None)
        return ProfileActionResult(
            message=f"Der Login von „{name}“ ist aus arr entfernt.",
            removed=True,
            profile=self._info(profile),
        )

    # -- login ------------------------------------------------------------------------------

    async def start_login(self, name: str) -> LoginStart:
        async with self._lock:  # a delete cannot slip between the check and the login
            profile = self._find(name)
            self._refuse_disabled(profile, "anmelden")
            self._check_login(profile)
            return await self.logins.start(profile)

    def _check_login(self, profile: AuthProfile) -> None:
        name = profile.name
        if profile.kind in (AuthKind.CLAUDE_API_KEY, AuthKind.OPENAI_API_KEY):
            raise ApiException.of(
                ErrorCode.INVALID,
                "Profile mit API-Key melden sich nicht an. Ersetze den Key auf der Karte.",
            )
        if profile.kind is AuthKind.CLAUDE_OAUTH and not owns_keychain_item(profile):
            raise not_allowed(
                f"„{name}“ verweist auf einen fremden Schlüsselbund-Eintrag. Lege für die "
                "Anmeldung ein neues Profil an."
            )
        if profile.kind is AuthKind.CODEX_CHATGPT and own_login_folder(profile) is None:
            raise not_allowed(
                f"„{name}“ nutzt einen Login-Ordner außerhalb von arr. arr ändert dort nichts. "
                "Lege für die Anmeldung ein neues Profil an."
            )

    async def _after_login(self, profile: AuthProfile) -> ProfileInfo:
        if profile.kind is not AuthKind.CODEX_CHATGPT:
            self._update_status(profile.name, secret_present=True, last_test=None)
        return await self.test_profile(profile.name)

    def _login_undone(self, profile: AuthProfile, removed: bool) -> None:
        with contextlib.suppress(ApiException):
            self._update_status(profile.name, secret_present=not removed, last_test=None)

    async def get_login_status(self, name: str) -> LoginStatus:
        return self.logins.status(name)

    async def cancel_login(self, name: str) -> LoginStatus:
        return await self.logins.cancel(name)

    async def close(self) -> None:
        await self.logins.close()

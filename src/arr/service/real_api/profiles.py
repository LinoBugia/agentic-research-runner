"""Area `profiles`: creating, testing, deleting and logging in auth profiles (docs/04 §5).

- `profiles.yaml` is written atomically and keeps its mode. A secret goes only to the keychain
  (`arr.auth.store_secret`), never through an API model (W1-06.1 review).
- `profile-status.json` next to it remembers `secret_present`, `last_test` and the limit
  window, so listing profiles needs neither a model call nor the keychain, whose access can
  raise a system dialog. It holds no secret.
- Every access to the keychain runs in a thread: a system dialog must not stop the service.
- Deleting removes the keychain item and the login folder, but only below the profile's own
  folder in `app_dir()` after `resolve`; `~/.codex` and `login_dir`s elsewhere stay.
- Logins run in `arr.service.login.LoginService`.

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
from pathlib import Path

from arr.auth import AuthError, delete_secret, owns_keychain_item, store_secret
from arr.backends.codex_config import codex_home_for
from arr.errors import ArrError
from arr.models import AuthKind, AuthProfile, ProfilesFile, Provider
from arr.resolve import dump_yaml_model
from arr.runs.launch import PROFILES_FILE, load_profiles
from arr.service.api_models import (
    ApiException,
    ApiModel,
    CreateProfileRequest,
    ErrorCode,
    LoginStart,
    LoginStatus,
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
LOGIN_ROOTS = ("auth", "codex-homes")
"""Folders of `app_dir()` that hold login folders, one subfolder per profile."""
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
        return {name: ProfileStatus.model_validate(item) for name, item in raw.items()}
    except (OSError, ValueError):
        return {}


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
    )


def login_folder_to_delete(profile: AuthProfile) -> Path | None:
    """The folder to remove for a profile, or None if it is not arr's own.

    Only `app_dir()/<root>/<name>` qualifies, compared after `resolve`, so symlinks and a
    changed `login_dir` cannot lead outside. `~/.codex` never qualifies.
    """
    if profile.kind is not AuthKind.CODEX_CHATGPT:
        return None
    home = codex_home_for(profile).resolve()
    codex = (Path.home() / ".codex").resolve()
    if home == codex or home.is_relative_to(codex):
        return None
    base = app_dir().resolve()
    for root in LOGIN_ROOTS:
        own = (app_dir() / root / profile.name).resolve()
        if own.parent == base / root and (home == own or home.is_relative_to(own)):
            return own
    return None


class ProfilesArea:
    """Profile methods of `ApiClient` (see the module docstring)."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state
        self.logins = LoginService(self._after_login, poll_url=self._poll_url)

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

    def _update_status(self, name: str, **changes: object) -> ProfileStatus:
        status = load_status()
        item = status.get(name, ProfileStatus()).model_copy(update=changes)
        status[name] = item
        save_status(status)
        return item

    def _info(self, profile: AuthProfile) -> ProfileInfo:
        return profile_info(self.state, profile, load_status().get(profile.name))

    # -- test -------------------------------------------------------------------------------

    async def test_profile(self, name: str) -> ProfileInfo:
        profile = self._find(name)
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

    async def create_profile(self, request: CreateProfileRequest) -> ProfileInfo:
        file = self._load()
        if any(p.name == request.name for p in file.profiles):
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
            self._save(file.model_copy(update={"profiles": [*file.profiles, profile]}))
        except OSError:
            with contextlib.suppress(AuthError):
                await asyncio.to_thread(delete_secret, profile)
            raise ApiException.of(
                ErrorCode.UNAVAILABLE,
                f"Das Profil „{request.name}“ ließ sich nicht speichern. Der Ordner von arr ist "
                "nicht beschreibbar. Prüfe die Rechte im Finder und versuche es erneut.",
            ) from None
        self._update_status(
            request.name, secret_present=request.api_key is not None, last_test=None
        )
        return self._info(profile)

    def _active_runs(self, name: str) -> list[str]:
        """Runs that use the profile right now: of this service and of `arr run` beside it."""
        gate = self.state.gates.get(name)
        ids = ["dieser Dienst"] if gate is not None and gate.active else []
        for record in self.state.store.scan():
            if record.meta.status.is_active:
                resolved = record.load_resolved()
                if resolved is None or any(a.auth == name for a in resolved.agents):
                    ids.append(record.run_id)
        return ids

    async def delete_profile(self, name: str) -> None:
        if not _NAME.match(name):
            raise not_found("Profil", name)
        profile = self._find(name)
        if self._active_runs(name):
            raise not_allowed(
                f"„{name}“ wird gerade von laufenden Runs genutzt. Warte, bis sie fertig sind."
            )
        await self.logins.cancel(name)
        try:
            await asyncio.to_thread(delete_secret, profile)
        except AuthError as exc:
            raise from_arr_error(exc, ErrorCode.UNAVAILABLE) from None
        folder = login_folder_to_delete(profile)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        elif profile.kind is AuthKind.CODEX_CHATGPT:
            log.info("Profil %s: Login-Ordner liegt außerhalb von arr und bleibt.", name)
        file = self._load()
        rest = [p for p in file.profiles if p.name != name]
        self._save(file.model_copy(update={"profiles": rest}))
        status = load_status()
        if status.pop(name, None) is not None:
            save_status(status)

    # -- login ------------------------------------------------------------------------------

    async def start_login(self, name: str) -> LoginStart:
        profile = self._find(name)
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
        return await self.logins.start(profile)

    async def _after_login(self, profile: AuthProfile) -> ProfileInfo:
        if profile.kind is not AuthKind.CODEX_CHATGPT:
            self._update_status(profile.name, secret_present=True, last_test=None)
        return await self.test_profile(profile.name)

    async def get_login_status(self, name: str) -> LoginStatus:
        return self.logins.status(name)

    async def cancel_login(self, name: str) -> LoginStatus:
        return await self.logins.cancel(name)

    async def close(self) -> None:
        await self.logins.close()

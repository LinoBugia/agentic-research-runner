"""Area `setup`: workfolder, projects, settings, fixed rules, toolchain (W3-04, docs/08 §5.11).

Paths from the GUI are checked before anything is created. A workfolder switch is refused
while this service executes a run; it is prepared with `await`s and then done in one step by
`ServiceState.switch_workfolder`. Settings go to `arr.yaml` with their comments: only values
that really changed are written, so `wall_time: 2h` stays as the user wrote it. After every
change `state.reload_settings()` brings the settings and the `ProfileGate` limits up to date.

App settings live in `~/.arr/settings.yaml` (`arr.service.state`). The port shows what the
service runs on: only `arr serve` can choose it.

`get_setup_state` comes with W5-02, the mandate with W4-04.
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from arr.errors import ArrError
from arr.models import BUILTIN_LIMITS, Decider, RunVenvMode, WorkfolderSettings
from arr.permissions.policy import AlwaysRule, PermissionRules
from arr.runs.launch import discard_orphaned_reservations
from arr.runs.manager import mark_interrupted_runs
from arr.service.api_models import (
    BUILTIN_FIXED_RULES,
    SYSTEM_PYTHON,
    ApiException,
    ApprovalSettingsInfo,
    AppSettings,
    CreateProjectRequest,
    ErrorCode,
    FixedRule,
    LimitsInfo,
    MandateInfo,
    MandateUpdate,
    NotificationEvent,
    ProjectSetupResult,
    ProviderDefaultsInfo,
    RetentionInfo,
    RuleEffect,
    SetupState,
    ToolchainCandidate,
    ToolchainInfo,
    ToolchainNotice,
    ToolchainTestResult,
    WorkfolderInfo,
    WorkfolderRequest,
    WorkfolderSettingsInfo,
)
from arr.service.real_api.base import (
    ServiceState,
    from_arr_error,
    not_allowed,
    not_found,
    unavailable,
    workfolder_gone,
)
from arr.service.real_api.catalog import CatalogArea
from arr.service.real_api.workfolder import WorkfolderArea, active_records
from arr.service.state import (
    read_app_settings,
    read_service_info,
    remember_workfolder,
    write_app_settings,
    write_service_info,
)
from arr.settings import app_dir
from arr.venvs import base_venv_dir, glob_safe_name, venv_base_prefix, venv_root
from arr.workfolder import (
    REMOVE,
    create_project,
    init_workfolder,
    is_workfolder,
    load_workfolder_settings,
    os_reason,
    path_within,
    set_setting,
    setup_project,
    update_settings_file,
)

log = logging.getLogger("arr.service")

STANDARD_MANDATE = "mandates/standard.md"
MIN_TIMEOUT_S = 60
"""Lower bound of `ApprovalSettingsInfo.timeout_s`; shorter values in `arr.yaml` show as this."""
TEST_TIMEOUT_S = 10.0
TEST_PACKAGES = ("sympy", "numpy")
_PROBE = (
    "import importlib.util as u, json, sys; print(json.dumps({'version': sys.version.split()[0], "
    "'packages': {p: u.find_spec(p) is not None for p in sys.argv[1:]}}))"
)
_IGNORED_ENTRIES = frozenset({".DS_Store"})
_DATA_VOLUME = "/System/Volumes/Data"
"""On macOS the same files as `/` under a second name; `realpath` does not see through it."""
_SECONDS = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def _invalid(message: str, field: str = "path") -> ApiException:
    return ApiException.of(ErrorCode.INVALID, message, field=field)


def duration_text(seconds: int) -> str:
    """`7200` → `2h`, `5400` → `1h30m`: the form `arr.yaml` uses (`parse_duration`)."""
    parts = []
    for unit, size in _SECONDS:
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "0s"


def settings_info(settings: WorkfolderSettings) -> WorkfolderSettingsInfo:
    """`arr.yaml` for the GUI; limits fall back to the built-in ones by field (docs/02 §3.3)."""

    def limit(name: str) -> Any:
        given = name in settings.limits.model_fields_set
        return getattr(settings.limits if given else BUILTIN_LIMITS, name)

    wall_time = limit("wall_time")
    approvals = settings.approvals
    gatekeeper = approvals.gatekeeper
    return WorkfolderSettingsInfo(
        defaults={
            provider: ProviderDefaultsInfo(auth=d.auth, model=d.model, effort=d.effort)
            for provider, d in settings.defaults.items()
        },
        limits=LimitsInfo(
            wall_time_s=int(wall_time.total_seconds()) if wall_time is not None else None,
            turns_per_agent=limit("turns_per_agent"),
            budget_usd=limit("budget_usd"),
        ),
        concurrency=dict(settings.concurrency),
        toolchain=ToolchainInfo(
            python=settings.toolchain.python,
            extra_path=list(settings.toolchain.extra_path),
            run_venv=settings.toolchain.run_venv,
        ),
        approvals=ApprovalSettingsInfo(
            decider=approvals.decider,
            timeout_s=max(MIN_TIMEOUT_S, int(approvals.timeout.total_seconds())),
            gatekeeper_model=gatekeeper.model if gatekeeper else None,
            mandate=gatekeeper.mandate if gatekeeper else None,
        ),
        retention=RetentionInfo(**settings.retention.model_dump()),
    )


def _changes(
    old: WorkfolderSettingsInfo, new: WorkfolderSettingsInfo
) -> dict[tuple[str, ...], Any]:
    """What differs between two settings, as `arr.yaml` key paths with their new values."""
    changes: dict[tuple[str, ...], Any] = {}

    def diff(keys: tuple[str, ...], before: Any, after: Any, value: Any) -> None:
        if before != after:
            changes[keys] = value

    for provider in {*old.defaults, *new.defaults}:
        before = old.defaults.get(provider)
        after = new.defaults.get(provider)
        keys = ("defaults", provider.value)
        if after is None:
            changes[keys] = REMOVE
            continue
        for name in ("auth", "model", "effort"):
            value = getattr(after, name)
            diff(
                (*keys, name),
                getattr(before, name) if before else None,
                value,
                REMOVE if value is None else getattr(value, "value", value),
            )
    wall = new.limits.wall_time_s
    diff(
        ("limits", "wall_time"),
        old.limits.wall_time_s,
        wall,
        duration_text(wall) if wall is not None else None,
    )
    for name in ("turns_per_agent", "budget_usd"):
        diff(
            ("limits", name),
            getattr(old.limits, name),
            getattr(new.limits, name),
            getattr(new.limits, name),
        )
    for name in {*old.concurrency, *new.concurrency}:
        limit = new.concurrency.get(name)
        diff(
            ("concurrency", name),
            old.concurrency.get(name),
            limit,
            REMOVE if limit is None else limit,
        )
    python = new.toolchain.python
    diff(
        ("toolchain", "python"), old.toolchain.python, python, REMOVE if python is None else python
    )
    diff(
        ("toolchain", "extra_path"),
        old.toolchain.extra_path,
        new.toolchain.extra_path,
        list(new.toolchain.extra_path),
    )
    diff(
        ("toolchain", "run_venv"),
        old.toolchain.run_venv,
        new.toolchain.run_venv,
        new.toolchain.run_venv.value,
    )
    diff(
        ("approvals", "decider"),
        old.approvals.decider,
        new.approvals.decider,
        new.approvals.decider.value,
    )
    diff(
        ("approvals", "timeout"),
        old.approvals.timeout_s,
        new.approvals.timeout_s,
        duration_text(new.approvals.timeout_s),
    )
    model = new.approvals.gatekeeper_model or old.approvals.gatekeeper_model
    if old.approvals.gatekeeper_model is None:
        # the section is new; `decider: user` needs none, whatever the page shows as a default
        if new.approvals.decider is not Decider.USER:
            if model is None:
                raise _invalid(
                    "Für den Aufsichts-Agenten fehlt das Modell. Wähle eines aus.",
                    "approvals.gatekeeper_model",
                )
            changes[("approvals", "gatekeeper")] = {
                "model": model,
                "mandate": new.approvals.mandate or STANDARD_MANDATE,
            }
    else:
        diff(("approvals", "gatekeeper", "model"), old.approvals.gatekeeper_model, model, model)
    for name in ("slim_after_days", "archive_after_days", "max_runs_size_gb"):
        diff(
            ("retention", name),
            getattr(old.retention, name),
            getattr(new.retention, name),
            getattr(new.retention, name),
        )
    return changes


def base_venvs() -> list[ToolchainCandidate]:
    """Base venvs below `<root>/venvs/` that a run can use, names by `glob_safe_name`."""
    try:
        entries = sorted(p for p in (venv_root() / "venvs").iterdir() if p.is_dir())
    except OSError:
        return []
    found = []
    for entry in entries:
        name = entry.name
        python = entry / "bin" / "python3"
        if name.startswith(".") or not glob_safe_name(name) or not python.exists():
            continue
        found.append(
            ToolchainCandidate(path=str(base_venv_dir(name)), python_version=_venv_version(entry))
        )
    return found


def _venv_version(venv: Path) -> str:
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return "unbekannt"
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() in ("version", "version_info") and value.strip():
            return value.strip()
    return "unbekannt"


def _interpreter(python: str, workfolder: Path) -> Path:
    """The program of `toolchain.python`: a venv's `bin/python3` or an interpreter itself."""
    path = Path(python).expanduser()
    path = path if path.is_absolute() else workfolder / path
    return path if path.is_file() else path / "bin" / "python3"


def toolchain_notice(settings: WorkfolderSettings, workfolder: Path) -> ToolchainNotice | None:
    """Why Codex agents cannot use this Python (docs/08 §8.4), or None if they can.

    The Codex sandbox denies `~` as a whole (W3-00). `prepare_run` checks a set
    `toolchain.python`; without one the run venv is built from arr's own Python, which nothing
    checks. The message comes before the start, so no run has to fail on it.
    """
    home = Path(os.path.realpath(Path.home()))
    toolchain = settings.toolchain
    if toolchain.python:
        python = Path(toolchain.python).expanduser()
        python = python if python.is_absolute() else workfolder / python
        venv = python.parent.parent if python.is_file() else python
        base = venv_base_prefix(venv)
        places = [python] if base is None else [python, base]
        if not any(path_within(Path(os.path.realpath(p)), home) for p in places):
            return None
        title = f"Die Python-Umgebung {toolchain.python} startet nicht für Codex-Agenten."
        why = "Sie nutzt ein Python unter deinem Benutzerordner"
    elif toolchain.run_venv is RunVenvMode.OVERLAY and path_within(
        Path(os.path.realpath(sys.base_prefix)), home
    ):
        title = "Das Python von arr startet nicht für Codex-Agenten."
        why = "Es liegt unter deinem Benutzerordner"
    else:
        return None
    way = (
        "Wähle unter „Einstellungen → Python-Umgebung“ eine Basis-venv außerhalb davon."
        if base_venvs()
        else "Anlegen kann arr sie noch nicht selbst, bis dahin laufen Runs nur mit Claude-Agenten."
    )
    return ToolchainNotice(
        title=title,
        text=f"{why}, und den sperrt die Codex-Sandbox. Claude-Agenten sind nicht betroffen. {way}",
    )


def _comparable(path: str | Path) -> Path:
    """The resolved path, under the name it has below `/` (`_DATA_VOLUME`)."""
    real = os.path.realpath(path)
    if path_within(Path(real), Path(_DATA_VOLUME)):
        real = real[len(_DATA_VOLUME) :] or "/"
    return Path(real)


def _extra_path_problem(entry: str, workfolder: Path) -> str | None:
    """Why `entry` must not go to `toolchain.extra_path`, or None.

    The entries land on the read list of the agents, so they must not open a way around the
    sensitive paths. Resolved paths count: a link to `~/.ssh` is refused like `~/.ssh`.
    """
    if not entry.startswith("/") or "\0" in entry:
        return "Das ist kein vollständiger Pfad. Gib ihn mit / am Anfang an."
    real, home = _comparable(entry), _comparable(Path.home())
    if path_within(home, real):
        return "Dein Benutzerordner liegt darin, die Agenten könnten ihn dann ganz lesen."
    if path_within(real, home):
        return "Er liegt in deinem Benutzerordner, den die Agenten nicht lesen sollen."
    for reserved, what in (
        (app_dir(), "die eigenen Daten von arr"),
        (workfolder, "den Workfolder"),
        (venv_root(), "die Python-Umgebungen"),
    ):
        if path_within(real, _comparable(reserved)):
            return f"Er liegt im Ordner für {what}, und das regelt arr selbst."
    if not os.path.isdir(entry):
        return "Diesen Ordner gibt es nicht."
    return None


def fixed_rule_id(rule: AlwaysRule) -> str:
    """Stable ID of an „Immer“ rule; `arr.yaml` has none, and positions change."""
    text = json.dumps(rule.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return f"rule-{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def rule_text(rule: AlwaysRule) -> str:
    """A rule in words for the table (docs/08 §5.11)."""
    parts = [*rule.commands, *rule.hosts, *rule.paths]
    what = ", ".join(parts) or "alle Aufrufe"
    return f"{', '.join(rule.tools)}: {what}" if rule.tools else what


class SetupArea:
    """Setup methods of `ApiClient` (W3-04); assistant and mandate follow later."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def get_setup_state(self) -> SetupState:
        raise unavailable("Der Einrichtungs-Assistent", "W5-02")

    async def get_mandate(self, name: str = "standard") -> MandateInfo:
        raise unavailable("Die Vollmacht des Aufsichts-Agenten", "W4-04")

    async def update_mandate(self, name: str, update: MandateUpdate) -> MandateInfo:
        raise unavailable("Die Vollmacht des Aufsichts-Agenten", "W4-04")

    # -- workfolder --------------------------------------------------------------------------

    def _refuse_while_running(self) -> None:
        active = len(self.state.registry.active())
        if active:
            word = "ein Run" if active == 1 else f"{active} Runs"
            raise not_allowed(
                f"Der Workfolder lässt sich gerade nicht wechseln: In „"
                f"{self.state.workfolder.name}“ läuft noch {word} dieses Service. Warte, bis er "
                "fertig ist, oder stoppe ihn, und wechsle dann."
            )

    @staticmethod
    def _checked_path(raw: str) -> Path:
        """The resolved folder for `raw`, or an error before anything is created."""
        text = raw.strip()
        if not text.startswith(("/", "~")) or "\0" in text:
            raise _invalid(
                f"„{text}“ ist kein vollständiger Pfad. Gib den Ordner mit / oder ~/ am Anfang "
                "an, z. B. ~/Test-UX3."
            )
        try:
            root = Path(os.path.realpath(Path(text).expanduser()))
        except (RuntimeError, ValueError):
            raise _invalid(
                f"Den Ordner „{text}“ kennt macOS nicht. Prüfe den Pfad, z. B. ~/Test-UX3."
            ) from None
        for reserved, what in (
            (venv_root(), "die Python-Umgebungen"),
            (app_dir(), "die eigenen Daten von arr"),
        ):
            if path_within(root, Path(os.path.realpath(reserved))):
                raise _invalid(
                    f"{root} liegt im Ordner für {what}, dort darf kein Workfolder liegen. Wähle "
                    "einen anderen Ordner, z. B. ~/Test-UX3."
                )
        probe = root
        try:
            while not probe.exists():
                probe = probe.parent
        except OSError as exc:
            raise _invalid(
                f"Den Pfad {text} kann arr nicht prüfen ({os_reason(exc)}). Wähle einen anderen "
                "Pfad, z. B. ~/Test-UX3."
            ) from None
        if not probe.is_dir():
            raise _invalid(f"{probe} ist eine Datei, kein Ordner. Wähle einen anderen Pfad.")
        if not os.access(probe, os.W_OK | os.X_OK):
            raise _invalid(
                f"arr darf in {probe} nicht schreiben. Wähle einen Ordner, in dem du schreiben "
                "darfst, z. B. in deinem Benutzerordner."
            )
        return root

    @staticmethod
    def _same_folder(first: Path, second: Path) -> bool:
        return path_within(first, second) and path_within(second, first)

    def _is_current(self, root: Path) -> bool:
        return self._same_folder(root, Path(os.path.realpath(self.state.workfolder)))

    @staticmethod
    def _check_new_folder(root: Path) -> None:
        """A new workfolder needs a new or empty folder; foreign content stays untouched."""
        try:
            entries = set(os.listdir(root)) - _IGNORED_ENTRIES if root.exists() else set()
        except OSError as exc:
            raise _invalid(
                f"Der Ordner {root} ist nicht lesbar ({os_reason(exc)}). Prüfe die Rechte oder "
                "wähle einen anderen Ordner."
            ) from None
        if entries:
            raise _invalid(
                f"In {root} liegen schon andere Dateien, und eine arr.yaml fehlt. arr legt dort "
                "keinen Workfolder an und lässt den Ordner, wie er ist. Wähle einen neuen oder "
                "leeren Ordner."
            )

    @staticmethod
    def _check_python(python: str) -> None:
        if python in (c.path for c in base_venvs()):
            return
        if not glob_safe_name(Path(python).name):
            raise _invalid(
                f"Der Name der Basis-venv „{Path(python).name}“ enthält Umlaute, Akzente oder "
                "Sonderzeichen. Damit kann die Codex-Sandbox andere venvs nicht sicher sperren. "
                "Nimm eine venv mit einem Namen aus a–z, 0–9, - und _.",
                "toolchain.python",
            )
        raise _invalid(
            f"Unter {venv_root() / 'venvs'} gibt es keine Basis-venv „{python}“. Wähle eine "
            "aus der Liste.",
            "toolchain.python",
        )

    def _create_workfolder(self, root: Path, python: str | None) -> None:
        self._check_new_folder(root)
        if python:
            self._check_python(python)
        try:
            init_workfolder(root)
            if python:
                update_settings_file(
                    root, lambda data: set_setting(data, ("toolchain", "python"), python)
                )
        except ArrError as exc:
            raise _invalid(exc.message) from None

    @staticmethod
    def _recover(root: Path) -> list[str]:
        """Like the start of the service (`RealApi.recover`), for the new workfolder."""
        interrupted = mark_interrupted_runs(root)
        discard_orphaned_reservations(root)
        return interrupted

    @staticmethod
    def _follow_in_app_files(root: Path) -> None:
        try:
            remember_workfolder(root)
            info = read_service_info()
            if info is not None and info.pid == os.getpid():
                write_service_info(info.model_copy(update={"workfolder": str(root)}))
        except OSError:
            log.exception("App-Dateien konnten dem Workfolder-Wechsel nicht folgen")

    async def select_workfolder(self, request: WorkfolderRequest) -> WorkfolderInfo:
        state = self.state
        info = WorkfolderArea(state)
        root = self._checked_path(request.path)
        if self._is_current(root):
            return await info.get_workfolder()
        self._refuse_while_running()
        if not is_workfolder(root):
            if not request.create:
                if root.is_dir():
                    raise _invalid(
                        f"{root} ist kein arr-Workfolder, die arr.yaml fehlt. Wähle einen "
                        "anderen Ordner oder lege dort einen neuen Workfolder an."
                    )
                raise ApiException.of(
                    ErrorCode.NOT_FOUND,
                    f"Workfolder {root} nicht gefunden. Wurde er verschoben? Wähle einen "
                    "anderen Ordner oder lege ihn neu an.",
                    field="path",
                )
            self._create_workfolder(root, request.python)
        try:
            settings = load_workfolder_settings(root)
        except ArrError as exc:
            raise from_arr_error(exc, ErrorCode.CONFIG_INVALID) from None
        interrupted = await asyncio.to_thread(self._recover, root)
        self._refuse_while_running()  # a run may have started during the recovery
        state.switch_workfolder(root, settings, interrupted)
        self._follow_in_app_files(root)
        return await info.get_workfolder()

    # -- projects ----------------------------------------------------------------------------

    async def _result(self, name: str, created: Iterable[str], message: str) -> ProjectSetupResult:
        project = await CatalogArea(self.state).get_project(name)
        return ProjectSetupResult(project=project, created=list(created), message=message)

    async def _set_up(self, folder: Path, title: str | None) -> ProjectSetupResult:
        try:
            done = setup_project(folder, workfolder=self.state.workfolder, title=title)
        except ArrError as exc:
            raise from_arr_error(exc) from None
        except (OSError, ValueError) as exc:
            reason = os_reason(exc) if isinstance(exc, OSError) else "eine Datei ist nicht lesbar"
            raise _invalid(
                f"„{folder.name}“ ließ sich nicht einrichten ({reason}). Prüfe die Rechte des "
                "Ordners und versuche es erneut."
            ) from None
        created = [*done.created, *done.updated]
        if done.skill_links:
            created.append(f".claude/skills/ ({len(done.skill_links)} Links)")
        name = folder.name
        message = f"„{name}“ repariert." if created else f"„{name}“ ist vollständig eingerichtet."
        return await self._result(name, created, " ".join([message, *done.warnings]))

    @staticmethod
    def _refuse_link(folder: Path) -> None:
        """arr writes only into real folders below `projects/`, never to where a link points."""
        if os.path.islink(folder):
            raise _invalid(
                f"{folder} ist ein symbolischer Link, arr schreibt nicht in den Ordner dahinter. "
                "Lege das Projekt als echten Ordner unter projects/ an."
            )

    def _existing_project(self, raw: str) -> Path:
        """The folder of „Vorhandenen Ordner einrichten“: only directly below `projects/`.

        The resolved path counts, so `projects/..` is refused before anything is written.
        """
        projects = self.state.workfolder / "projects"
        text = raw.strip()
        try:
            path = Path(text).expanduser()
            real = Path(os.path.realpath(path))
        except (RuntimeError, ValueError):
            raise _invalid(f"Den Ordner „{text}“ gibt es nicht. Prüfe den Pfad.") from None
        self._refuse_link(path)
        if not self._same_folder(real.parent, Path(os.path.realpath(projects))):
            raise _invalid(
                f"{path} liegt nicht direkt unter {projects}. arr richtet nur Ordner dort ein. "
                "Verschiebe den Ordner dorthin und versuche es erneut."
            )
        folder = projects / real.name
        if not os.path.isdir(folder):
            raise ApiException.of(
                ErrorCode.NOT_FOUND,
                f"Den Ordner {folder} gibt es nicht. Prüfe den Namen.",
                field="path",
            )
        return folder

    async def create_project(self, request: CreateProjectRequest) -> ProjectSetupResult:
        if request.template:
            raise unavailable("Das Beispielprojekt", "W5-02")
        if request.existing_path:
            return await self._set_up(self._existing_project(request.existing_path), request.title)
        name = request.name
        if not is_workfolder(self.state.workfolder):
            raise from_arr_error(workfolder_gone(self.state.workfolder))
        if (self.state.workfolder / "projects" / name).exists():
            raise ApiException.of(
                ErrorCode.CONFLICT,
                f"Ein Projekt „{name}“ gibt es schon. Wähle einen anderen Ordnernamen.",
                field="name",
            )
        try:
            folder = create_project(self.state.workfolder, name, title=request.title)
        except ArrError as exc:
            raise from_arr_error(exc) from None
        except OSError as exc:
            raise ApiException.of(
                ErrorCode.INVALID,
                f"Das Projekt „{name}“ ließ sich nicht anlegen ({os_reason(exc)}). Prüfe die "
                "Rechte des Workfolders.",
            ) from None
        created = sorted(str(p.relative_to(folder)) for p in folder.iterdir())
        return await self._result(folder.name, created, f"Projekt „{name}“ angelegt.")

    async def setup_project(self, name: str) -> ProjectSetupResult:
        folder = self.state.workfolder / "projects" / name
        if "/" in name or name in (".", "..") or not folder.is_dir():
            raise not_found("Projekt", name)
        self._refuse_link(folder)
        return await self._set_up(folder, None)

    # -- app settings ------------------------------------------------------------------------

    def _port(self) -> int:
        return urlsplit(self.state.base_url).port or AppSettings().port

    async def get_app_settings(self) -> AppSettings:
        saved = read_app_settings()
        try:
            settings = AppSettings.model_validate(
                {k: saved[k] for k in ("open_in", "appearance", "notifications") if k in saved}
            )
        except ValidationError:
            log.warning("settings.yaml ist ungültig, es gelten die Standardwerte")
            settings = AppSettings()
        known = (await WorkfolderArea(self.state).get_workfolder()).known
        return settings.model_copy(update={"port": self._port(), "workfolders": known})

    async def update_app_settings(self, settings: AppSettings) -> AppSettings:
        if settings.port != self._port():
            raise _invalid(
                f"Den Port kann arr im Betrieb nicht ändern, der Service läuft auf {self._port()}.",
                "port",
            )
        notifications = {
            event.value: settings.notifications.get(event, True) for event in NotificationEvent
        }
        write_app_settings(
            {
                **read_app_settings(),
                "open_in": settings.open_in.value,
                "appearance": settings.appearance.value,
                "notifications": notifications,
            }
        )
        return await self.get_app_settings()

    # -- workfolder settings -----------------------------------------------------------------

    def _reload(self) -> WorkfolderSettings:
        try:
            return self.state.reload_settings()
        except ArrError as exc:
            raise from_arr_error(exc, ErrorCode.CONFIG_INVALID) from None

    async def get_workfolder_settings(self) -> WorkfolderSettingsInfo:
        state = self.state
        settings = self._reload()
        # the registry knows a run of this service before the store has seen its folder
        running = max(len(active_records(state)), len(state.registry.active()))
        return settings_info(settings).model_copy(
            update={
                "running_runs": running,
                "toolchain_candidates": base_venvs(),
                "toolchain_notice": toolchain_notice(settings, state.workfolder),
            }
        )

    def _check_update(
        self,
        old: WorkfolderSettingsInfo,
        new: WorkfolderSettingsInfo,
        changes: Mapping[Any, Any],
    ) -> None:
        known = {p.name for p in self.state.profiles_or_empty().profiles}
        for keys in changes:
            if keys[0] == "concurrency" and keys[1] in new.concurrency and keys[1] not in known:
                raise _invalid(
                    f"Profil „{keys[1]}“ gibt es nicht. Wähle eines der Auth-Profile.",
                    f"concurrency.{keys[1]}",
                )
        if ("toolchain", "python") in changes and new.toolchain.python is not None:
            self._check_python(new.toolchain.python)
        for entry in new.toolchain.extra_path:
            problem = None
            if entry not in old.toolchain.extra_path:  # hand-kept values of `arr.yaml` stay
                problem = _extra_path_problem(entry, self.state.workfolder)
            if problem:
                raise _invalid(
                    f"„{entry}“ kann nicht in den PATH. {problem} Nimm einen Programmordner "
                    "außerhalb davon, z. B. /opt/homebrew/bin.",
                    "toolchain.extra_path",
                )

    async def update_workfolder_settings(
        self, settings: WorkfolderSettingsInfo
    ) -> WorkfolderSettingsInfo:
        settings = WorkfolderSettingsInfo.model_validate(settings.model_dump())
        old = settings_info(self._reload())
        changes = _changes(old, settings)
        self._check_update(old, settings, changes)
        if changes:

            def edit(data: Any) -> None:
                for keys, value in changes.items():
                    set_setting(data, keys, value)

            try:
                update_settings_file(self.state.workfolder, edit)
            except ArrError as exc:
                raise from_arr_error(exc, ErrorCode.CONFIG_INVALID) from None
        return await self.get_workfolder_settings()

    # -- fixed rules -------------------------------------------------------------------------

    def _user_rules(self) -> list[AlwaysRule]:
        try:
            return PermissionRules.from_settings(self._reload().permissions).always
        except ValidationError:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID,
                "Der Abschnitt „permissions“ in arr.yaml ist ungültig, deshalb lassen sich die "
                "festen Regeln nicht lesen. Korrigiere ihn in der Datei.",
            ) from None

    async def list_fixed_rules(self) -> list[FixedRule]:
        builtin = [
            FixedRule(
                id=f"std-{index}",
                effect=effect,
                rule=text,
                origin="builtin",
                origin_text="Standard",
                removable=False,
            )
            for index, (effect, text) in enumerate(BUILTIN_FIXED_RULES, 1)
        ]
        own = [
            FixedRule(
                id=fixed_rule_id(rule),
                effect=RuleEffect(rule.action),
                rule=rule_text(rule),
                origin="user",
                origin_text=" · ".join(
                    p for p in ("von dir: Reichweite „immer“", rule.reason) if p
                ),
                removable=True,
            )
            for rule in self._user_rules()
        ]
        return builtin + own

    async def delete_fixed_rule(self, rule_id: str) -> list[FixedRule]:
        if rule_id.startswith("std-"):
            raise not_allowed(
                "Standardregeln sind fest. Du kannst nur Regeln entfernen, die du mit „immer“ "
                "angelegt hast."
            )
        rules = self._user_rules()
        doomed = [i for i, rule in enumerate(rules) if fixed_rule_id(rule) == rule_id]
        if not doomed:
            raise not_found("Regel", rule_id)

        def edit(data: Any) -> None:
            always = data["permissions"]["always"]
            for index in reversed(doomed):
                del always[index]

        try:
            update_settings_file(self.state.workfolder, edit)
        except ArrError as exc:
            raise from_arr_error(exc, ErrorCode.CONFIG_INVALID) from None
        for handle in self.state.registry.active():
            inbox = handle.manager.inbox if handle.manager is not None else None
            if inbox is not None:
                kept = [r for r in inbox.engine.rules.always if fixed_rule_id(r) != rule_id]
                inbox.engine.rules.always[:] = kept
        return await self.list_fixed_rules()

    # -- toolchain ---------------------------------------------------------------------------

    async def test_toolchain(self, python: str | None = None) -> ToolchainTestResult:
        settings = self._reload()
        chosen = settings.toolchain.python if python is None else python
        if chosen == SYSTEM_PYTHON or chosen is None:
            chosen, program = None, Path(sys.base_prefix) / "bin" / "python3"
        else:
            allowed = {settings.toolchain.python, *(c.path for c in base_venvs())}
            if chosen not in allowed:
                return ToolchainTestResult(
                    ok=False,
                    python=chosen,
                    message=f"{chosen} steht nicht in der Liste. Wähle eine Basis-venv daraus.",
                )
            program = _interpreter(chosen, self.state.workfolder)
        shown = chosen or SYSTEM_PYTHON
        started = time.monotonic()
        try:
            done = await asyncio.to_thread(
                subprocess.run,
                [str(program), "-I", "-c", _PROBE, *TEST_PACKAGES],
                capture_output=True,
                text=True,
                timeout=TEST_TIMEOUT_S,
            )
            probe = json.loads(done.stdout) if done.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            probe = None
        duration = int((time.monotonic() - started) * 1000)
        if probe is None:
            return ToolchainTestResult(
                ok=False,
                python=shown,
                message=f"{shown} startet nicht. Wähle eine andere Python-Umgebung.",
                duration_ms=duration,
            )
        version, packages = probe["version"], probe["packages"]
        missing = [name for name, there in packages.items() if not there]
        notice = toolchain_notice(
            settings.model_copy(
                update={"toolchain": settings.toolchain.model_copy(update={"python": chosen})}
            ),
            self.state.workfolder,
        )
        detail = None
        if notice:
            message, detail = notice.title, notice.text
        elif missing:
            verb = "fehlt" if len(missing) == 1 else "fehlen"
            message = f"{shown} läuft mit Python {version}, aber {' und '.join(missing)} {verb}."
            detail = (
                "Wähle eine venv, die alles mitbringt, oder lass Agenten fehlende Pakete in die "
                "Run-venv installieren."
            )
        else:
            message = f"{shown} funktioniert: Python {version}, {' und '.join(packages)} vorhanden."
        return ToolchainTestResult(
            ok=not (notice or missing),
            python=shown,
            python_version=version,
            packages=packages,
            message=message,
            detail=detail,
            duration_ms=duration,
        )

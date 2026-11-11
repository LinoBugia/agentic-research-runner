"""Start service of `arr run`, reusable by the service of W3-01 (docs/04 §6, docs/07 §6).

`plan_launch` finds the workfolder (`--workfolder`, else upwards from the current folder to
the `arr.yaml`), loads `run.yaml` with its group skill and the `--param` overrides, resolves
the run and checks the topology, all before anything is created. `run_launch` then marks runs
left active by a crash as `interrupted` (never a run another process holds the lock of),
reserves the run folder and takes its lock (`RunLock` on `run.lock`, held until the end
including the secret scan), builds the `RunManager` with the real backends
(`ClaudeBackend`/`CodexBackend` per provider, auth profile from `profiles.yaml`, finalizer
always set), runs it, scans the run folder for secrets and reports progress through callbacks.

Two ways to stop a run from outside: `StopControl` (Ctrl-C: the first stops the run with the
finalizer, the second aborts hard) and `stop.request`, a file that `arr runs stop` puts into
the run folder and that the running process polls and consumes (`watch_stop_request`; none
stays behind when `run_launch` returns). A third way is the Not-Aus: `watch_blocked_profiles`
stops the run when one of its profiles is blocked, also from another workfolder. The module knows
neither Typer nor a terminal: text goes through `report`, approvals through the `decider`.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from arr import __version__
from arr.auth import SecretReader
from arr.backends.base import AgentBackend
from arr.errors import ArrError, ConfigError
from arr.models import (
    AuthProfile,
    GroupSpec,
    Limits,
    ProfilesFile,
    Provider,
    ResolvedRun,
    RunInput,
    RunStatus,
    Starter,
    WorkfolderSettings,
)
from arr.permissions.inbox import ApprovalDecider
from arr.resolve import load_group_spec, load_run_input, load_yaml_model, resolve_run
from arr.runs.finalize import finalize_run, last_turn_error
from arr.runs.inspect import (
    LOCKS_AVAILABLE,
    RUN_LOCK,
    STATUS_TEXT,
    STOP_REQUEST,
    TURN_TEXT,
    RunLock,
    describe_checkpoint,
    run_lock_held,
    run_reason,
)
from arr.runs.manager import Finalizer, RunManager, mark_interrupted_runs, set_run_status
from arr.runs.orchestrator import ORCHESTRATOR_LOG, OrchestratorLog, profile_shown
from arr.runs.prepare import PreparedAgent, RunMeta, load_run_meta, new_run_id
from arr.runs.secret_scan import (
    SCAN_KIND,
    ScanReport,
    format_report,
    profile_secrets,
    record_scan,
    scan_run_dir,
)
from arr.runs.topologies import check_topology, topology_for
from arr.settings import app_dir
from arr.skills import discover_skills
from arr.workfolder import (
    is_workfolder,
    load_project_settings,
    load_workfolder_settings,
    project_dir,
)

__all__ = [
    "BACKEND_BUILDERS",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_STOPPED",
    "ORPHAN_MIN_AGE_S",
    "PROFILES_FILE",
    "REPEAT_WINDOW_S",
    "STOP_REQUEST",
    "Launch",
    "LiveFactory",
    "Report",
    "RunLock",
    "RunResult",
    "StopControl",
    "build_manager",
    "describe_approval",
    "discard_orphaned_reservations",
    "describe_entry",
    "exit_code",
    "find_workfolder",
    "follow_run",
    "install_signal_handlers",
    "load_profiles",
    "parse_params",
    "plan_launch",
    "plan_run_input",
    "request_stop",
    "reserve_run",
    "run_launch",
    "blocked_profiles",
    "watch_blocked_profiles",
    "watch_stop_request",
]

log = logging.getLogger(__name__)

PROFILES_FILE = "profiles.yaml"
BLOCK_POLL_S = 2.0
"""How often a running run looks at the block of its profiles (W3-06.1)."""
UNREADABLE = (ArrError, OSError, UnicodeDecodeError, YAMLError)
"""What reading `profiles.yaml` can raise; `ConfigError` is an `ArrError`."""
EXIT_OK, EXIT_FAILED, EXIT_STOPPED = 0, 1, 130
"""Exit codes of `arr run`: `done`, anything else, stopped by the user (like SIGINT)."""

REPEAT_WINDOW_S = 1.0
"""A second SIGINT/SIGTERM within this time counts as the same Ctrl-C. A real Ctrl-C goes to
the whole process group; `uv run` survives it, waits for arr and forwards the signal to arr
about 0.2 s after the kernel already delivered it (W2-05.1, point 7)."""

Report = Callable[[str], None]
"""Receives one German line of progress; `arr run` prints it."""

BackendBuilder = Callable[..., AgentBackend]
"""`builder(agent, *, run_id, inbox, auth, limits) -> AgentBackend`."""


# --------------------------------------------------------------------------------------------
# Planning: workfolder, parameters, profiles, run.yaml
# --------------------------------------------------------------------------------------------


def find_workfolder(given: str | Path | None = None, *, start: str | Path | None = None) -> Path:
    """`given` (must be a workfolder) or the first folder with an `arr.yaml` from `start` up."""
    if given is not None:
        root = Path(given).expanduser().absolute()
        if not is_workfolder(root):
            raise ArrError(
                f"„{root}“ ist kein arr-Workfolder, die Datei arr.yaml fehlt. "
                f"Lege ihn mit „arr init {root}“ an."
            )
        return root
    origin = (Path.cwd() if start is None else Path(start)).expanduser().absolute()
    for folder in (origin, *origin.parents):
        if is_workfolder(folder):
            return folder
    raise ArrError(
        f"Kein arr-Workfolder gefunden: von {origin} aufwärts gibt es keine arr.yaml. "
        "Wechsle in den Workfolder, gib ihn mit --workfolder an oder lege ihn mit „arr init“ an."
    )


def _scalar(raw: str) -> Any:
    """A `--param` value as YAML scalar (`6` → 6, `true` → True), else the text itself."""
    text = raw.strip()
    if not text:
        return ""
    try:
        value = YAML(typ="safe").load(text)
    except YAMLError:
        return raw
    if value is None and text not in ("null", "~"):
        return raw
    return value


def parse_params(items: Sequence[str]) -> dict[str, Any]:
    """`["pass_score=6", "on_fail=stop"]` → `{"pass_score": 6, "on_fail": "stop"}`."""
    params: dict[str, Any] = {}
    for item in items:
        name, sep, raw = item.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ArrError(
                f"„{item}“ ist kein Parameter. Schreibe ihn als name=wert, z. B. --param "
                "pass_score=6."
            )
        params[name] = _scalar(raw)
    return params


def blocked_profiles(names: Iterable[str], known: ProfilesFile) -> list[str]:
    """Which of `names` are blocked now, read anew from `profiles.yaml`.

    A file that does not exist says nothing, then `known` counts. A file that cannot be read
    raises one of `UNREADABLE`: the caller decides what that means.
    """
    file = app_dir() / PROFILES_FILE
    try:
        file.stat()
        current = load_yaml_model(file, ProfilesFile)
    except FileNotFoundError:
        current = known
    wanted = set(names)
    blocked = {p.name for p in (*current.profiles, *known.profiles) if p.disabled}
    return sorted(blocked & wanted)


def refuse_disabled(names: Iterable[str], known: ProfilesFile) -> None:
    """The Not-Aus (W3-06): no agent starts with a blocked profile, on any way of starting.

    Reads `profiles.yaml` anew at every start, because a run may be planned long before its
    agent starts. A file that cannot be read stops the start (fail closed). Raises `ArrError`.
    """
    try:
        blocked = blocked_profiles(names, known)
    except UNREADABLE:
        raise ArrError(
            "Der Sperr-Stand der Profile ließ sich nicht lesen (profiles.yaml). Deshalb startet "
            "kein Agent. Prüfe die Datei und versuche es erneut."
        ) from None
    if blocked:
        raise ArrError(
            f"Das Profil „{blocked[0]}“ ist gesperrt, der Agent läuft nicht an. Entsperre es "
            "unter „Auth-Profile“ oder wähle unter „Einstellungen → Standards und Limits“ ein "
            "anderes Profil als Standard."
        )


def load_profiles(path: str | Path | None = None) -> ProfilesFile:
    """`~/.arr/profiles.yaml` (docs/02 §1); without it a hint how to create it."""
    file = Path(path) if path is not None else app_dir() / PROFILES_FILE
    if not file.is_file():
        raise ArrError(
            f"Es gibt noch keine Auth-Profile: die Datei {file} fehlt. Lege sie an, z. B.:\n"
            "version: 1\n"
            "profiles:\n"
            "  - {name: max, kind: claude_oauth}\n"
            "  - {name: chatgpt-privat, kind: codex_chatgpt}\n"
            "Ein Claude-Profil ohne Token im Schlüsselbund nutzt den Claude-Login dieses Macs; "
            "ein ChatGPT-Profil meldest du mit „arr auth codex-login <name>“ an."
        )
    return load_yaml_model(file, ProfilesFile)


@dataclass(frozen=True, slots=True)
class Launch:
    """Everything `run_launch` needs, checked before any folder is created.

    `run_file` is the `run.yaml` of `arr run`; a run given as data (the service, W3-01) has
    none, its task is inline.
    """

    run_file: Path | None
    workfolder: Path
    settings: WorkfolderSettings
    profiles: ProfilesFile
    run_input: RunInput
    group_spec: GroupSpec | None
    resolved: ResolvedRun
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def task_base(self) -> Path:
        """`task_file` of the `run.yaml` is relative to the file's folder (else the workfolder)."""
        return self.run_file.parent if self.run_file is not None else self.workfolder

    def used_profiles(self) -> list[AuthProfile]:
        """The auth profiles of the run's agents."""
        names = {a.auth for a in self.resolved.agents}
        return [p for p in self.profiles.profiles if p.name in names]


def _group_spec(workfolder: Path, run_input: RunInput, source: str) -> GroupSpec | None:
    if run_input.skill is None:
        return None
    catalog = discover_skills(workfolder / "skills")
    info = catalog.get(run_input.skill, path="skill")
    if info.group_file is None:
        raise ConfigError.single(
            "skill",
            f"Der Skill „{run_input.skill}“ ist kein Group-Skill: Ihm fehlt die group.yaml. "
            "Wähle einen Skill der Art „Group“ oder lege die group.yaml im Skill an.",
            source=source,
        )
    return load_group_spec(info.group_file)


def plan_launch(
    run_file: str | Path,
    *,
    params: Mapping[str, Any] | None = None,
    workfolder: str | Path | None = None,
    profiles: ProfilesFile | None = None,
    cwd: str | Path | None = None,
) -> Launch:
    """Load and check everything for `arr run <run.yaml>`; raises `ArrError`/`ConfigError`.

    Order: workfolder → `arr.yaml` → `run.yaml` → group skill (`skill:`) → project → resolve
    with the `--param` overrides → `check_topology` → `profiles.yaml`. Nothing is created.
    """
    file = Path(run_file).expanduser().absolute()
    if not file.is_file():
        raise ArrError(f"Die Datei {file} gibt es nicht. Gib den Pfad zur run.yaml an.")
    try:
        root = find_workfolder(workfolder, start=cwd)
    except ArrError:
        if workfolder is not None:
            raise
        root = find_workfolder(start=file.parent)
    return plan_run_input(
        load_run_input(file),
        workfolder=root,
        params=params,
        profiles=profiles,
        run_file=file,
        source=str(file),
    )


def plan_run_input(
    run_input: RunInput,
    *,
    workfolder: str | Path,
    params: Mapping[str, Any] | None = None,
    profiles: ProfilesFile | None = None,
    run_file: Path | None = None,
    source: str = "run.yaml",
) -> Launch:
    """Check a run given as data (the service's `start_run`, W3-01) like `plan_launch`.

    `arr.yaml` → group skill → project → resolve with `params` → `check_topology` →
    `profiles.yaml`; nothing is created. `source` names the origin in error messages.
    """
    root = Path(workfolder).expanduser().absolute()
    settings = load_workfolder_settings(root)
    group_spec = _group_spec(root, run_input, source)
    project = project_dir(root, run_input.project)
    resolved = resolve_run(
        run_input,
        group_spec,
        settings,
        dict(params or {}),
        project_settings=load_project_settings(project),
    )
    check_topology(resolved)
    known = profiles if profiles is not None else load_profiles()
    refuse_disabled((a.auth for a in resolved.agents), known)
    return Launch(
        run_file=run_file,
        workfolder=root,
        settings=settings,
        profiles=known,
        run_input=run_input,
        group_spec=group_spec,
        resolved=resolved,
        params=dict(params or {}),
    )


# --------------------------------------------------------------------------------------------
# Backends and RunManager
# --------------------------------------------------------------------------------------------


def _build_claude(
    agent: PreparedAgent, *, run_id: str, inbox: Any, auth: AuthProfile, limits: Limits
) -> AgentBackend:
    from arr.backends.claude import ClaudeBackend

    return ClaudeBackend(agent, run_id=run_id, inbox=inbox, auth=auth, limits=limits)


def _build_codex(
    agent: PreparedAgent, *, run_id: str, inbox: Any, auth: AuthProfile, limits: Limits
) -> AgentBackend:
    from arr.backends.codex import CodexBackend

    return CodexBackend(agent, run_id=run_id, inbox=inbox, auth=auth)


BACKEND_BUILDERS: dict[Provider, BackendBuilder] = {
    Provider.CLAUDE: _build_claude,
    Provider.CODEX: _build_codex,
}
"""Backend per provider; tests replace the entries with builders of a `FakeBackend`."""


class LiveFactory:
    """`BackendFactory` of `arr run`: the real backend per provider with the agent's profile.

    The RunManager calls it after its room, PolicyEngine and inbox exist (`manager.inbox`);
    `build_manager` sets `manager`. Claude gets the run's limits (`max_budget_usd`).
    """

    def __init__(
        self,
        profiles: ProfilesFile,
        limits: Limits,
        builders: Mapping[Provider, BackendBuilder] | None = None,
    ) -> None:
        self.file = profiles
        self.profiles = {p.name: p for p in profiles.profiles}
        self.limits = limits
        self.builders = dict(BACKEND_BUILDERS if builders is None else builders)
        self.manager: RunManager | None = None

    def __call__(self, agent: PreparedAgent) -> AgentBackend:
        manager = self.manager
        if manager is None or manager.inbox is None or manager.prepared is None:
            raise RuntimeError("LiveFactory needs the RunManager with its inbox")
        config = agent.file.config
        auth = self.profiles.get(config.auth)
        if auth is None:  # prepare_run checks this; kept for a factory used on its own
            raise ArrError(f"Das Auth-Profil „{config.auth}“ von „{agent.id}“ gibt es nicht.")
        refuse_disabled([config.auth], self.file)
        builder = self.builders.get(config.provider)
        if builder is None:
            raise ArrError(f"Für den Anbieter „{config.provider}“ gibt es noch kein Backend.")
        return builder(
            agent,
            run_id=manager.prepared.run_id,
            inbox=manager.inbox,
            auth=auth,
            limits=self.limits,
        )


def build_manager(
    launch: Launch,
    *,
    decider: ApprovalDecider | None,
    builders: Mapping[Provider, BackendBuilder] | None = None,
    finalizer: Finalizer = finalize_run,
    starter: Starter = Starter.CLI,
    **kwargs: Any,
) -> RunManager:
    """The RunManager of a planned run: topology, backend factory, finalizer, profiles.

    `starter` is who starts the run (`cli` for `arr run`, `gui` for the service, W3-01);
    further keyword arguments go to the `RunManager` (e.g. `run_id`, `gates`, `lock`).
    """
    factory = LiveFactory(launch.profiles, launch.resolved.limits, builders)
    manager = RunManager(
        launch.workfolder,
        launch.run_input,
        launch.resolved,
        topology=topology_for(launch.resolved.topology.kind),
        backend_factory=factory,
        settings=launch.settings,
        decider=decider,
        finalizer=finalizer,
        starter=starter,
        task_base=launch.task_base,
        profiles=launch.profiles,
        **kwargs,
    )
    factory.manager = manager
    return manager


# --------------------------------------------------------------------------------------------
# Run folder: reservation and lock (W2-05.1 M1; `RunLock` lives in `arr.runs.inspect`)
# --------------------------------------------------------------------------------------------

ORPHAN_MIN_AGE_S = 30.0
"""A `queued` folder without lock and without prepared parts is removed only when it is at
least this old: a process that reserved it a moment ago may not have taken the lock yet."""


def reserve_run(
    launch: Launch,
    *,
    now: datetime | None = None,
    starter: Starter = Starter.CLI,
    status: RunStatus = RunStatus.QUEUED,
) -> Path:
    """Create the run folder with a `meta.json` in status `queued` and return it.

    A run start of Claude Code is reserved `awaiting_approval` instead (W4-01, E15); it goes on
    only after the user's approval, with the same folder and run ID.

    `prepare_run(run_id=…)` takes such a reserved folder over (docs/02 §7, W2-03). Reserving
    first lets `run_launch` hold the lock before the run ever is `preparing`, so no other
    process can take it for a crashed run in between. `starter` is kept by `prepare_run`
    (`gui` for the service, W3-01).
    """
    moment = now or datetime.now()
    runs = launch.workfolder / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    resolved = launch.resolved
    for _ in range(50):
        run_id = new_run_id(resolved.project, resolved.skill, now=moment)
        folder = runs / run_id
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        meta = RunMeta(
            run_id=run_id,
            status=status,
            starter=starter,
            project=resolved.project,
            skill=resolved.skill,
            name=resolved.name,
            created=moment.timestamp(),
            arr_version=__version__,
        )
        (folder / "meta.json").write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return folder
    raise ArrError("Es konnte keine freie Run-ID gefunden werden. Versuche es noch einmal.")


def _unprepared(folder: Path) -> bool:
    """True for a folder that holds nothing but the reservation (`meta.json`, `run.lock`)."""
    try:
        return {p.name for p in folder.iterdir()} <= {"meta.json", RUN_LOCK}
    except OSError:
        return False


def _discard_unprepared(folder: Path) -> None:
    """Remove a reserved folder that `prepare_run` never filled (a configuration error): a
    run that could not be prepared leaves no folder behind, as before the reservation."""
    if _unprepared(folder):
        shutil.rmtree(folder, ignore_errors=True)


def discard_orphaned_reservations(
    workfolder: str | Path, *, min_age_s: float = ORPHAN_MIN_AGE_S, now: float | None = None
) -> list[str]:
    """Remove `queued` run folders that no process holds and that were never prepared.

    They stay behind when a process dies between `reserve_run` and the preparation (W2-05.1
    review N2). A folder counts as orphaned when its `meta.json` says `queued`, nobody holds
    its `run.lock`, it holds nothing but `meta.json` and `run.lock`, and it is at least
    `min_age_s` old (so a reservation made a moment ago by another process survives). Called
    at the start of `arr run` and of the service; returns the IDs of the removed folders.
    """
    runs_dir = Path(workfolder).expanduser() / "runs"
    moment = time.time() if now is None else now
    removed = []
    for meta_path in sorted(runs_dir.glob("*/meta.json")):
        folder = meta_path.parent
        try:
            meta = load_run_meta(folder)
        except (OSError, ValueError):
            continue
        if meta.status is not RunStatus.QUEUED or moment - meta.created < min_age_s:
            continue
        if run_lock_held(folder) or not _unprepared(folder):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(meta.run_id)
    return removed


# --------------------------------------------------------------------------------------------
# Stopping: Ctrl-C and stop.request
# --------------------------------------------------------------------------------------------


class StopControl:
    """Ctrl-C of `arr run`: the first stops the run, the second aborts hard (docs/02 §7).

    The first interrupt calls `RunManager.stop()`: approvals are cancelled, turns interrupted
    and the finalizer runs, the run ends `stopped`. In `finalizing` it only says so (the
    finalizer runs to its end), after the end it says the run is over. A second one, e.g.
    while the finalizer runs, ends the process at once with `EXIT_STOPPED`: the run folder
    stays as it is and `meta.json` says `interrupted` (as after a crash). Signals within
    `REPEAT_WINDOW_S` of the previous one are the same Ctrl-C (forwarded by `uv run`) and
    are ignored. `hard_exit` is `os._exit` outside tests, `clock` is `time.monotonic`.
    """

    def __init__(
        self,
        manager: RunManager,
        report: Report,
        *,
        hard_exit: Callable[[int], Any] = os._exit,
        clock: Callable[[], float] = time.monotonic,
        repeat_window_s: float = REPEAT_WINDOW_S,
    ) -> None:
        self.manager = manager
        self.report = report
        self.hard_exit = hard_exit
        self.clock = clock
        self.repeat_window_s = repeat_window_s
        self.interrupts = 0
        self._last: float | None = None

    def on_interrupt(self) -> None:
        """Handle one SIGINT/SIGTERM."""
        now = self.clock()
        if self._last is not None and now - self._last < self.repeat_window_s:
            return  # the same Ctrl-C, delivered twice (process group + uv run)
        self._last = now
        self.interrupts += 1
        if self.interrupts > 1:
            self.hard_abort()
            return
        status = self.manager.status
        if status is not None and status.is_final:
            self.report("Der Run ist schon beendet. Noch einmal Strg-C beendet arr sofort.")
            return
        if status is RunStatus.FINALIZING:
            self.report(
                "Strg-C: Der Run wird schon abgeschlossen, der Finalizer läuft zu Ende. "
                "Noch einmal Strg-C (nach einer Sekunde) bricht hart ab."
            )
            return
        self.report(
            "Strg-C: Der Run wird gestoppt. Laufende Turns werden unterbrochen, danach läuft "
            "der Finalizer. Noch einmal Strg-C (nach einer Sekunde) bricht hart ab."
        )
        self.manager.stop()

    def hard_abort(self) -> None:
        """Second Ctrl-C: say so, mark the run `interrupted` and leave the process."""
        self.report(
            "Zweites Strg-C: harter Abbruch. Der Run-Ordner bleibt unvollständig, der Status "
            "wird „unterbrochen“ (interrupted)."
        )
        prepared = self.manager.prepared
        if prepared is not None:
            with contextlib.suppress(Exception):
                if not load_run_meta(prepared.run_dir).status.is_final:
                    set_run_status(prepared.run_dir, RunStatus.INTERRUPTED)
                    OrchestratorLog(prepared.run_dir).write(
                        "status", status=RunStatus.INTERRUPTED.value, previous="hard_abort"
                    )
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()
        self.hard_exit(EXIT_STOPPED)


def install_signal_handlers(
    loop: asyncio.AbstractEventLoop, control: StopControl
) -> Callable[[], None]:
    """Route SIGINT and SIGTERM to `control.on_interrupt`; returns the function that undoes it."""
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, control.on_interrupt)

    def remove() -> None:
        for sig in signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)

    return remove


def request_stop(run_dir: str | Path, *, source: str = "cli") -> bool:
    """Put `stop.request` into the run folder (`arr runs stop`); False if it was already there.

    Only for a run in `preparing`, `running` or `paused` whose lock a process holds; otherwise
    `ArrError`. The running `arr run` process picks the file up within about a second
    (`watch_stop_request`) and calls `RunManager.stop()`; no signal goes to another process.
    """
    folder = Path(run_dir)
    meta = load_run_meta(folder)
    state = STATUS_TEXT.get(meta.status, meta.status.value)
    if meta.status not in (RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PAUSED):
        raise ArrError(
            f"Der Run {meta.run_id} läuft nicht (Status: {state}, {meta.status.value}). "
            "Stoppen geht nur bei einem aktiven Run."
        )
    if LOCKS_AVAILABLE and not run_lock_held(folder):
        raise ArrError(
            f"Der Run {meta.run_id} läuft in keinem arr-run-Prozess mehr (Status: {state}, "
            f"{meta.status.value}, aber niemand hält run.lock); der Prozess ist wohl "
            "abgestürzt. Beim nächsten „arr run“ im Workfolder gilt der Run als unterbrochen "
            "(interrupted)."
        )
    request = folder / STOP_REQUEST
    if request.exists():
        return False
    request.write_text(json.dumps({"ts": time.time(), "source": source}) + "\n", encoding="utf-8")
    return True


async def watch_stop_request(
    manager: RunManager, report: Report, done: asyncio.Event, *, interval_s: float = 1.0
) -> None:
    """Poll `stop.request` in the run folder until `done`; the first request stops the run.

    Every request is consumed. A later one (a second `arr runs stop` while the turns are still
    being interrupted, W2-05.3) or one that arrives in `finalizing` or later, when
    `RunManager.stop()` would not act any more, is only answered with a line.
    """
    stopped = False
    while not done.is_set():
        prepared = manager.prepared
        if prepared is not None:
            request = prepared.run_dir / STOP_REQUEST
            if request.exists():
                with contextlib.suppress(OSError):
                    request.unlink()
                status = manager.status
                if status is RunStatus.FINALIZING or (status is not None and status.is_final):
                    report(
                        "Stopp-Anforderung erhalten (arr runs stop), aber der Run wird schon "
                        "abgeschlossen; der Finalizer läuft zu Ende."
                    )
                elif stopped:
                    report(
                        "Stopp-Anforderung erhalten (arr runs stop): Der Run wird schon gestoppt."
                    )
                else:
                    stopped = True
                    report("Stopp-Anforderung erhalten (arr runs stop): Der Run wird gestoppt …")
                    manager.stop()
        await asyncio.sleep(interval_s)


async def watch_blocked_profiles(
    manager: RunManager,
    names: Iterable[str],
    known: ProfilesFile,
    report: Report,
    done: asyncio.Event,
    *,
    interval_s: float = BLOCK_POLL_S,
) -> None:
    """Stop the run as soon as one of its profiles is blocked (W3-06.1), in any workfolder.

    `profiles.yaml` is shared by all workfolders, so this also reaches a run the blocking
    service cannot see. A file that cannot be read does not stop a running run: it logs one
    warning per outage and the run goes on, only the start is fail closed.
    """
    profiles = set(names)
    warned = False
    while not done.is_set():
        try:
            blocked = blocked_profiles(profiles, known)
            warned = False
        except UNREADABLE as exc:
            blocked = []
            if not warned:
                warned = True
                log.warning(
                    "Der Sperr-Stand der Profile ist gerade nicht lesbar (%s). Der Run läuft "
                    "weiter.",
                    type(exc).__name__,
                )
        if blocked:
            report(f"Das Profil „{blocked[0]}“ wurde gesperrt: Der Run wird gestoppt …")
            manager.stop()
            return
        await asyncio.sleep(interval_s)


# --------------------------------------------------------------------------------------------
# Progress: orchestrator.jsonl and approvals.jsonl as German lines
# --------------------------------------------------------------------------------------------


class _Tail:
    """Reads the new complete lines of a growing JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.rest = b""

    def read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        *lines, self.rest = (self.rest + chunk).split(b"\n")
        entries = []
        for line in lines:
            if not line.strip():
                continue
            with contextlib.suppress(ValueError):
                entries.append(json.loads(line))
        return [e for e in entries if isinstance(e, dict)]


def _number(value: Any) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def _clock(ts: Any) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "–"


WAKE_TEXT = {
    "question": "Frage an ihn",
    "mention": "Nachricht an ihn",
    "reply": "Antwort auf seine Nachricht",
    "question_all": "Frage an alle",
    "any": "neue Nachrichten",
    "file": "Datei ist da",
    "timeout": "Wartezeit um",
}
DONE_TEXT = {
    "result": "Ergebnis abgegeben",
    "reported": "meldet fertig",
    "limit": "Turns aufgebraucht (turns_per_agent)",
}
CHANNEL_WAIT_TEXT = {
    "agents": "wartende Agenten",
    "approval": "deine Freigabe",
    "capacity": "einen freien Platz",
}
"""What a discussion waits for, parts of `waiting {channel, reason: mixed}` (docs/09 §4.6)."""
END_TURN_TEXT = {
    "nobody": "wartete 5 min ohne neue Nachricht",
    "capacity": "ein anderer Agent wartet auf seinen Platz",
}
END_TEXT = {
    "all_done": "alle fertig",
    "rounds": "Runden gespielt",
    "all_passed": "alle passen",
    "quiet": "Ruhezeit abgelaufen",
    "stopped": "gestoppt",
    "limit": "Limit erreicht",
}


def _describe_waiting(data: Mapping[str, Any]) -> str | None:
    """`waiting` of an agent or a channel (docs/09 §4.6)."""
    until = f" bis {_clock(data['until'])}" if data.get("until") else ""
    file = f" oder auf „{data['file']}“" if data.get("file") else ""
    reason = str(data.get("reason"))
    if data.get("agent"):
        who = data["agent"]
        match reason:
            case "wait_for_messages":
                return f"⏳ {who} wartet im Turn auf eine Nachricht{file}{until}"
            case "wait_until":
                return f"⏳ {who} beendet den Turn und wartet auf eine Nachricht{file}{until}"
            case "approval":
                return f"⏳ {who} wartet auf eine Freigabe{until}"
            case "capacity":
                return f"⏳ {who} wartet auf einen freien Platz im Profil „{data.get('profile')}“"
        return f"⏳ {who} wartet ({reason}){until}"
    channel = f"#{data.get('channel')}"
    match reason:
        case "idle":
            what = "letzte Runde vorbei, Ende" if data.get("next") == "end" else "nächste Runde"
            return f"Diskussion in {channel} ruht: {what} um {_clock(data.get('until'))}"
        case "agents":
            return f"Diskussion in {channel} wartet auf wartende Agenten{until}"
        case "approval":
            return f"Diskussion in {channel} wartet auf deine Freigabe"
        case "capacity":
            return f"Diskussion in {channel} wartet auf einen freien Platz"
        case "mixed":
            parts = [CHANNEL_WAIT_TEXT.get(str(p), str(p)) for p in data.get("parts") or []]
            return f"Diskussion in {channel} wartet auf {' und '.join(parts)}{until}"
    return None


def describe_entry(entry: Mapping[str, Any], run_dir: Path | None = None) -> str | None:
    """One German progress line for an entry of `orchestrator.jsonl`, None to skip it."""
    kind = str(entry.get("kind"))
    data = entry.get("data") or {}
    if not isinstance(data, Mapping):
        data = {}
    match kind:
        case "status":
            status = str(data.get("status"))
            try:
                word = STATUS_TEXT[RunStatus(status)]
            except ValueError:
                word = status
            return f"Status: {word} ({status})"
        case "checkpoint":
            return "● " + describe_checkpoint(data)
        case "turn":
            text = f"▶ {data.get('agent')}: Turn {data.get('turn')}"
            return text + (f" · Stufe „{data['stage']}“" if data.get("stage") else "")
        case "turn_end":
            status = str(data.get("status"))
            text = f"■ {data.get('agent')}: Turn {data.get('turn')} beendet"
            text += f" ({TURN_TEXT.get(status, status)}"
            if data.get("cost_usd"):
                text += f", {_number(data['cost_usd'])} $"
            text += ")"
            if status == "error" and run_dir is not None:
                error = last_turn_error(run_dir, str(data.get("agent")), data.get("turn"))
                if error:
                    text += f" – {error}"
            return text
        case "limit":
            from arr.runs.inspect import LIMIT_TEXT

            limit = str(data.get("limit"))
            return "Limit erreicht: " + LIMIT_TEXT.get(limit, limit)
        case "stop":
            text = "Stopp: laufende Turns werden unterbrochen, danach läuft der Finalizer."
            profile = profile_shown(data.get("profile"))
            if data.get("reason") == "profile_blocked" and profile is not None:
                text = f"Profil „{profile}“ wurde gesperrt (Not-Aus). {text}"
            return text
        case "pause":
            return f"Pause ({data.get('reason')}): neue Turns warten."
        case "resume":
            return f"Weiter ({data.get('reason')})."
        case "rate_limit":
            action = "gedrosselt" if data.get("action") == "throttle" else "pausiert"
            return (
                f"Rate-Limit bei Profil „{data.get('profile')}“ ({data.get('status')}): "
                f"{action} bis {_clock(data.get('until'))}"
            )
        case "hint":
            return f"📨 Hinweis an {data.get('agent')} mitten im Turn"
        case "wake":
            return f"⏰ {data.get('agent')} geweckt (#{data.get('channel')})"
        case "channel_open":
            return f"Kanal #{data.get('channel')} geöffnet (Stufe „{data.get('stage')}“)"
        case "discussion_start":
            return f"Diskussion in #{data.get('channel')} beginnt ({data.get('mode')})"
        case "discussion_end":
            ended = str(data.get("ended_by"))
            text = (
                f"Diskussion in #{data.get('channel')} beendet: {END_TEXT.get(ended, ended)}, "
                f"{data.get('turns')} Turns"
            )
            return text + (f", {data['rounds']} Runden" if data.get("rounds") else "")
        case "escalation":
            return f"Eskalation in Stufe „{data.get('stage')}“: {data.get('summary')}"
        case "escalation_decided":
            return f"Eskalation entschieden: {data.get('option')}"
        case "error":
            where = f" ({data['where']})" if data.get("where") else ""
            return f"Fehler{where}: {data.get('message')}"
        case "warning":
            return f"Warnung: {data.get('message')}"
        case "waiting":
            return _describe_waiting(data)
        case "round":
            agents = ", ".join(str(a) for a in data.get("agents") or []) or "niemand"
            return (
                f"↻ Runde {data.get('number')} von {data.get('of')} in #{data.get('channel')}: "
                f"{agents}"
            )
        case "wake_scheduled":
            why = WAKE_TEXT.get(str(data.get("reason")), str(data.get("reason")))
            return f"⏰ {data.get('agent')} wird geweckt ({why}) um {_clock(data.get('due'))}"
        case "agent_done":
            reason = DONE_TEXT.get(str(data.get("reason")), str(data.get("reason")))
            return f"✓ {data.get('agent')} ist fertig: {reason}"
        case "end_turn":
            why = END_TURN_TEXT.get(str(data.get("reason")), str(data.get("reason")))
            if data.get("hard"):
                return f"⏹ arr bricht den Turn von {data.get('agent')} hart ab: {why}"
            again = " (zweiter Versuch)" if data.get("attempt") == 2 else ""
            return f"⏹ arr beendet den Turn von {data.get('agent')}{again}: {why}"
        case "wait_end":
            return (
                f"⏹ {data.get('agent')} wartet nicht mehr: Diskussion in "
                f"#{data.get('channel')} ist zu Ende"
            )
        case "turn_silent":
            minutes = int(data.get("seconds") or 0) // 60
            return f"⚠ {data.get('agent')}: seit {minutes} min kein Ereignis im Turn"
        case "wake_skipped" | "ping_pong" | "secret_scan":
            return None
    extra = ", ".join(f"{k}={v}" for k, v in data.items())
    return f"{kind}" + (f": {extra}" if extra else "")


_DECIDED_BY_WORDS = {
    "gatekeeper": "vom Aufsichts-Agenten",
    "mandate_rule": "durch die Vollmacht (von arr geprüft)",
}


def describe_approval(entry: Mapping[str, Any]) -> str | None:
    """One line for an entry of `approvals.jsonl` (open items and their outcome)."""
    event = entry.get("event")
    request = entry.get("request") or {}
    decision = entry.get("decision") or {}
    item = entry.get("item_id")
    who = f"{request.get('agent_id')} · {request.get('tool')}"
    what = request.get("command") or ", ".join(request.get("hosts") or request.get("paths") or [])
    match event:
        case "open":
            line = f"❓ Freigabe-Anfrage {item} von {who}" + (f": {what}" if what else "")
            passed = entry.get("escalated_by") or {}
            return line + (f" · {passed['note']}" if passed.get("note") else "")
        case "decided":
            action = "erlaubt" if decision.get("action") == "allow" else "abgelehnt"
            scope = decision.get("scope")
            line = f"✔ Freigabe {item} {action}" + (f" ({scope})" if scope else "")
            by = _DECIDED_BY_WORDS.get(str(decision.get("decided_by")))
            if by:  # W4-04: decided before it reached the user
                line += f" {by}: {decision.get('reason') or ''}".rstrip(": ")
            return line
        case "expired":
            return f"⌛ Freigabe {item} abgelaufen: abgelehnt."
        case "obsolete":
            return f"✖ Freigabe {item} hinfällig: {entry.get('note') or 'abgelehnt'}"
        case "rule":
            return f"Neue feste Regel aus Freigabe {item} (arr.yaml)."
    return None


async def follow_run(
    manager: RunManager, report: Report, done: asyncio.Event, *, interval_s: float = 0.5
) -> None:
    """Report the run folder and every new entry of the run's logs until `done` is set."""
    while manager.prepared is None:
        if done.is_set():
            return
        await asyncio.sleep(interval_s)
    run_dir = manager.run_dir
    report(f"Run {manager.prepared.run_id}")
    report(f"Ordner: {run_dir}")
    log = _Tail(run_dir / ORCHESTRATOR_LOG)
    approvals = _Tail(run_dir / "approvals.jsonl")
    while True:
        for entry in log.read():
            line = describe_entry(entry, run_dir)
            if line:
                report(line)
        for entry in approvals.read():
            line = describe_approval(entry)
            if line:
                report(line)
        if done.is_set():
            return
        await asyncio.sleep(interval_s)


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RunResult:
    """What `run_launch` returns."""

    run_id: str
    run_dir: Path
    status: RunStatus
    reason: str
    scan: ScanReport | None = None

    @property
    def summary_file(self) -> Path:
        return self.run_dir / "output" / "summary.md"


def exit_code(status: RunStatus) -> int:
    """`0` for `done`, `130` for `stopped`, `1` otherwise."""
    if status is RunStatus.DONE:
        return EXIT_OK
    if status is RunStatus.STOPPED:
        return EXIT_STOPPED
    return EXIT_FAILED


def _no_signals() -> None:
    return None


async def run_launch(
    launch: Launch,
    *,
    decider: ApprovalDecider | None,
    report: Report,
    builders: Mapping[Provider, BackendBuilder] | None = None,
    finalizer: Finalizer = finalize_run,
    interval_s: float = 0.5,
    stop_poll_s: float = 1.0,
    block_poll_s: float = BLOCK_POLL_S,
    hard_exit: Callable[[int], Any] = os._exit,
    signals: bool = True,
    scan: bool = True,
    secret_reader: SecretReader | None = None,
    run_dir: Path | None = None,
    starter: Starter = Starter.CLI,
    on_manager: Callable[[RunManager], None] | None = None,
    lock: RunLock | None = None,
    **manager_kwargs: Any,
) -> RunResult:
    """Run a planned run to its end: crash recovery, reservation and lock, RunManager,
    watchers, secret scan.

    Configuration errors of the preparation are raised (`ArrError`, the reserved folder is
    removed again); everything later ends in a final status. With `signals` Ctrl-C goes to a
    `StopControl` (needs the main thread) until the secret scan is over, so a Ctrl-C after the
    end only says „schon beendet“ and the exit code still follows the status.

    The service (W3-01) passes a folder it reserved itself (`run_dir`, from `reserve_run`
    with `starter=gui`), `signals=False`, and gets the `RunManager` through `on_manager` as
    soon as it exists, for pause, resume and stop. For a run start of Claude Code it also
    passes the `lock` it held while the run waited for approval (W4-01).
    """
    for run_id in mark_interrupted_runs(launch.workfolder):
        report(
            f"Run {run_id} war beim letzten Mal noch aktiv und gilt jetzt als unterbrochen "
            "(interrupted)."
        )
    for run_id in discard_orphaned_reservations(launch.workfolder):
        report(f"Verwaister Run-Ordner {run_id} entfernt (reserviert, aber nie vorbereitet).")
    if run_dir is None:
        run_dir = reserve_run(launch, starter=starter)
    if lock is None:
        lock = RunLock(run_dir)
        lock.acquire()  # an approved run start of Claude Code hands its held lock over (W4-01)
    manager = build_manager(
        launch,
        decider=decider,
        builders=builders,
        finalizer=finalizer,
        starter=starter,
        run_id=run_dir.name,
        lock=lock,
        **manager_kwargs,
    )
    if on_manager is not None:
        on_manager(manager)
    control = StopControl(manager, report, hard_exit=hard_exit)
    remove = _no_signals
    if signals:
        try:
            remove = install_signal_handlers(asyncio.get_running_loop(), control)
        except (ValueError, RuntimeError, NotImplementedError):
            report("Hinweis: Strg-C wird in dieser Umgebung nicht abgefangen.")
    done = asyncio.Event()
    follower = asyncio.create_task(follow_run(manager, report, done, interval_s=interval_s))
    stopper = asyncio.create_task(watch_stop_request(manager, report, done, interval_s=stop_poll_s))
    names = {a.auth for a in launch.resolved.agents}
    blocker = asyncio.create_task(
        watch_blocked_profiles(
            manager, names, launch.profiles, report, done, interval_s=block_poll_s
        )
    )
    try:
        try:
            status = await manager.run()
        except BaseException:
            if manager.prepared is None:  # never prepared: no run folder stays behind
                lock.release()
                _discard_unprepared(run_dir)
            raise
        finally:
            done.set()
            with contextlib.suppress(Exception):
                await follower
            for watcher in (stopper, blocker):
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
        run_id = manager.prepared.run_id if manager.prepared else run_dir.name
        reason = run_reason(load_run_meta(run_dir), OrchestratorLog(run_dir).entries())
        result = RunResult(run_id, run_dir, status, reason)
        report("")
        report(f"Run {run_id} beendet: {STATUS_TEXT.get(status, status.value)} ({status.value}).")
        report(f"Grund: {reason}")
        if result.summary_file.is_file():
            report(f"Zusammenfassung: {result.summary_file}")
        else:
            report("Keine Zusammenfassung: output/summary.md fehlt (siehe orchestrator.jsonl).")
        if scan:
            profiles = launch.used_profiles()
            secrets = await asyncio.to_thread(profile_secrets, profiles, secret_reader)
            result.scan = await asyncio.to_thread(scan_run_dir, run_dir, secrets)
            record_scan(run_dir, result.scan)
            report(format_report(result.scan))
        return result
    finally:
        remove()
        with contextlib.suppress(OSError):  # a request after the watcher's last poll (W2-05.3)
            (run_dir / STOP_REQUEST).unlink(missing_ok=True)
        lock.release()


def latest_scan(run_dir: str | Path) -> dict[str, Any] | None:
    """The last `secret_scan` entry of a run folder, if any."""
    entries = [e for e in OrchestratorLog(Path(run_dir)).entries() if e["kind"] == SCAN_KIND]
    return entries[-1]["data"] if entries else None

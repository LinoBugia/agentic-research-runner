"""Start service of `arr run`, reusable by the service of W3-01 (docs/04 §6, docs/07 §6).

`plan_launch` finds the workfolder (`--workfolder`, else upwards from the current folder to
the `arr.yaml`), loads `run.yaml` with its group skill and the `--param` overrides, resolves
the run and checks the topology, all before anything is created. `run_launch` then marks runs
left active by a crash as `interrupted`, builds the `RunManager` with the real backends
(`ClaudeBackend`/`CodexBackend` per provider, auth profile from `profiles.yaml`, finalizer
always set), runs it, scans the run folder for secrets and reports progress through callbacks.

Two ways to stop a run from outside: `StopControl` (Ctrl-C: the first stops the run with the
finalizer, the second aborts hard) and `stop.request`, a file that `arr runs stop` puts into
the run folder and that the running process polls (`watch_stop_request`). The module knows
neither Typer nor a terminal: text goes through `report`, approvals through the `decider`.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

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
    STATUS_TEXT,
    STOP_REQUEST,
    TURN_TEXT,
    describe_checkpoint,
    run_reason,
)
from arr.runs.manager import Finalizer, RunManager, mark_interrupted_runs, set_run_status
from arr.runs.orchestrator import ORCHESTRATOR_LOG, OrchestratorLog
from arr.runs.prepare import PreparedAgent, load_run_meta
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
    "PROFILES_FILE",
    "STOP_REQUEST",
    "Launch",
    "LiveFactory",
    "Report",
    "RunResult",
    "StopControl",
    "build_manager",
    "describe_approval",
    "describe_entry",
    "exit_code",
    "find_workfolder",
    "follow_run",
    "install_signal_handlers",
    "load_profiles",
    "parse_params",
    "plan_launch",
    "request_stop",
    "run_launch",
    "watch_stop_request",
]

PROFILES_FILE = "profiles.yaml"
EXIT_OK, EXIT_FAILED, EXIT_STOPPED = 0, 1, 130
"""Exit codes of `arr run`: `done`, anything else, stopped by the user (like SIGINT)."""

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
    """Everything `run_launch` needs, checked before any folder is created."""

    run_file: Path
    workfolder: Path
    settings: WorkfolderSettings
    profiles: ProfilesFile
    run_input: RunInput
    group_spec: GroupSpec | None
    resolved: ResolvedRun
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def task_base(self) -> Path:
        """`task_file` of the `run.yaml` is relative to the file's folder."""
        return self.run_file.parent

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
            f"Der Skill „{run_input.skill}“ ist kein Group-Skill: im Ordner {info.path} fehlt "
            "die group.yaml.",
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
    settings = load_workfolder_settings(root)
    run_input = load_run_input(file)
    group_spec = _group_spec(root, run_input, str(file))
    project = project_dir(root, run_input.project)
    resolved = resolve_run(
        run_input,
        group_spec,
        settings,
        dict(params or {}),
        project_settings=load_project_settings(project),
    )
    check_topology(resolved)
    return Launch(
        run_file=file,
        workfolder=root,
        settings=settings,
        profiles=profiles if profiles is not None else load_profiles(),
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
    **kwargs: Any,
) -> RunManager:
    """The RunManager of a planned run: topology, backend factory, finalizer, profiles."""
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
        starter=Starter.CLI,
        task_base=launch.task_base,
        profiles=launch.profiles,
        **kwargs,
    )
    factory.manager = manager
    return manager


# --------------------------------------------------------------------------------------------
# Stopping: Ctrl-C and stop.request
# --------------------------------------------------------------------------------------------


class StopControl:
    """Ctrl-C of `arr run`: the first stops the run, the second aborts hard (docs/02 §7).

    The first interrupt calls `RunManager.stop()`: approvals are cancelled, turns interrupted
    and the finalizer runs, the run ends `stopped`. A second one, e.g. while the finalizer
    runs, ends the process at once with `EXIT_STOPPED`: the run folder stays as it is and
    `meta.json` says `interrupted` (as after a crash). `hard_exit` is `os._exit` outside tests.
    """

    def __init__(
        self,
        manager: RunManager,
        report: Report,
        *,
        hard_exit: Callable[[int], Any] = os._exit,
    ) -> None:
        self.manager = manager
        self.report = report
        self.hard_exit = hard_exit
        self.interrupts = 0

    def on_interrupt(self) -> None:
        """Handle one SIGINT/SIGTERM."""
        self.interrupts += 1
        if self.interrupts > 1:
            self.hard_abort()
            return
        status = self.manager.status
        if status is not None and status.is_final:
            self.report("Der Run ist schon beendet. Noch einmal Strg-C beendet arr sofort.")
            return
        self.report(
            "Strg-C: Der Run wird gestoppt. Laufende Turns werden unterbrochen, danach läuft "
            "der Finalizer. Noch einmal Strg-C bricht hart ab."
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

    Only for a run in `preparing`, `running` or `paused`; otherwise `ArrError`. The running
    `arr run` process picks the file up within about a second (`watch_stop_request`) and
    calls `RunManager.stop()`; no signal goes to another process.
    """
    folder = Path(run_dir)
    meta = load_run_meta(folder)
    if meta.status not in (RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PAUSED):
        state = STATUS_TEXT.get(meta.status, meta.status.value)
        raise ArrError(
            f"Der Run {meta.run_id} läuft nicht (Status: {state}, {meta.status.value}). "
            "Stoppen geht nur bei einem aktiven Run."
        )
    request = folder / STOP_REQUEST
    if request.exists():
        return False
    request.write_text(json.dumps({"ts": time.time(), "source": source}) + "\n", encoding="utf-8")
    return True


async def watch_stop_request(
    manager: RunManager, report: Report, done: asyncio.Event, *, interval_s: float = 1.0
) -> None:
    """Poll `stop.request` in the run folder and stop the run when it appears."""
    while not done.is_set():
        prepared = manager.prepared
        if prepared is not None:
            request = prepared.run_dir / STOP_REQUEST
            if request.exists():
                with contextlib.suppress(OSError):
                    request.unlink()
                report("Stopp-Anforderung erhalten (arr runs stop): Der Run wird gestoppt …")
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
            return "Stopp: laufende Turns werden unterbrochen, danach läuft der Finalizer."
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
            return (
                f"Diskussion in #{data.get('channel')} beendet: {data.get('ended_by')}, "
                f"{data.get('turns')} Turns"
            )
        case "escalation":
            return f"Eskalation in Stufe „{data.get('stage')}“: {data.get('summary')}"
        case "escalation_decided":
            return f"Eskalation entschieden: {data.get('option')}"
        case "error":
            where = f" ({data['where']})" if data.get("where") else ""
            return f"Fehler{where}: {data.get('message')}"
        case "warning":
            return f"Warnung: {data.get('message')}"
        case "wake_skipped" | "ping_pong" | "secret_scan":
            return None
    extra = ", ".join(f"{k}={v}" for k, v in data.items())
    return f"{kind}" + (f": {extra}" if extra else "")


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
            return f"❓ Freigabe-Anfrage {item} von {who}" + (f": {what}" if what else "")
        case "decided":
            action = "erlaubt" if decision.get("action") == "allow" else "abgelehnt"
            scope = decision.get("scope")
            return f"✔ Freigabe {item} {action}" + (f" ({scope})" if scope else "")
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
    hard_exit: Callable[[int], Any] = os._exit,
    signals: bool = True,
    scan: bool = True,
    secret_reader: SecretReader | None = None,
    **manager_kwargs: Any,
) -> RunResult:
    """Run a planned run to its end: crash recovery, RunManager, watchers, secret scan.

    Configuration errors of the preparation are raised (`ArrError`); everything later ends in
    a final status. With `signals` Ctrl-C goes to a `StopControl` (needs the main thread).
    """
    for run_id in mark_interrupted_runs(launch.workfolder):
        report(
            f"Run {run_id} war beim letzten Mal noch aktiv und gilt jetzt als unterbrochen "
            "(interrupted)."
        )
    manager = build_manager(launch, decider=decider, builders=builders, finalizer=finalizer)
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
    try:
        status = await manager.run()
    finally:
        done.set()
        remove()
        with contextlib.suppress(Exception):
            await follower
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stopper
    run_dir = manager.run_dir
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
        secrets = await asyncio.to_thread(profile_secrets, launch.used_profiles(), secret_reader)
        result.scan = await asyncio.to_thread(scan_run_dir, run_dir, secrets)
        record_scan(run_dir, result.scan)
        report(format_report(result.scan))
    return result


def latest_scan(run_dir: str | Path) -> dict[str, Any] | None:
    """The last `secret_scan` entry of a run folder, if any."""
    entries = [e for e in OrchestratorLog(Path(run_dir)).entries() if e["kind"] == SCAN_KIND]
    return entries[-1]["data"] if entries else None

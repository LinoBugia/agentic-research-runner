"""RunManager: the lifecycle of one run from `preparing` to `done`, `stopped` or `failed`.

`RunManager.run()` goes through docs/02 §7 and writes every status change with its time to
`meta.json` at once (atomically) and to `orchestrator.jsonl`:

- `preparing`: `prepare_run` (also into a reserved folder), `tmp/`, the run venv (with
  `run_venv: overlay`), `groupchats/room.sqlite` with the channels, the PolicyEngine with one
  profile per agent, the `ApprovalInbox` with the given decider, one EventLogger and one
  backend per agent (from the backend factory, after the profiles are registered), `start()`.
- `running` ⇄ `paused`: the topology runs with the `Orchestrator` as `RunContext`; limits,
  concurrency per auth profile, rate limits, hints and pacing are the orchestrator's job.
  The wall time is watched here.
- `finalizing`: backends closed, then the finalizer (always, also after stop, limit or error).
- `done` (also after a limit), `stopped` (user stop) or `failed` (error).

`mark_interrupted_runs` sets runs that were `running`, `paused` or `finalizing` when arr
went down to `interrupted`. See docs/01-architektur.md §6-§7, docs/02 §4, §7, docs/07 §2.
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from arr.backends.base import AgentBackend
from arr.errors import ArrError
from arr.models import (
    ProfilesFile,
    ResolvedRun,
    RunInput,
    RunStatus,
    RunVenvMode,
    Starter,
    WorkfolderSettings,
)
from arr.permissions.inbox import ApprovalDecider, ApprovalInbox
from arr.permissions.policy import (
    ApprovalLog,
    PermissionRules,
    PolicyEngine,
    RunPaths,
    profile_for,
)
from arr.room.store import RoomStore
from arr.runs.context import RunContext
from arr.runs.eventlog import EventLogger
from arr.runs.orchestrator import PLAN_STAGE, LimitKind, Orchestrator, OrchestratorLog, StopReason
from arr.runs.pacing import PacingSettings
from arr.runs.prepare import (
    RESERVED_STATUSES,
    PreparedAgent,
    PreparedRun,
    RunMeta,
    load_run_meta,
    prepare_run,
)
from arr.workfolder import load_workfolder_settings

__all__ = [
    "BackendFactory",
    "Finalizer",
    "RunManager",
    "Topology",
    "base_interpreter",
    "create_run_venv",
    "mark_interrupted_runs",
    "set_run_status",
]

logger = logging.getLogger(__name__)

Topology = Callable[[RunContext], Awaitable[None]]
"""A topology: `async def run(ctx: RunContext) -> None` (E8)."""
BackendFactory = Callable[[PreparedAgent], AgentBackend]
"""Builds the backend of one agent; W2-05 wires `ClaudeBackend`/`CodexBackend`."""
Finalizer = Callable[[PreparedRun], Awaitable[None]]
"""Runs in `finalizing`, also after stop, limit or error (W2-04)."""

INTERRUPTIBLE_STATUSES = frozenset(
    {RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.FINALIZING}
)
"""Statuses that become `interrupted` after a crash (docs/01 §7, W2-03.1 G3)."""

STOPPABLE_STATUSES = frozenset({RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PAUSED})
"""Statuses in which `RunManager.stop` acts; in `finalizing` and after it is ignored."""

STOP_GRACE_S = 10.0
"""How long a stopped topology may take to return before it is cancelled."""
CLOSE_TIMEOUT_S = 10.0
"""How long `close()` of a backend may take."""
VENV_TIMEOUT_S = 600.0
BASE_VENV_PTH = "_arr_base_venv.pth"


# --------------------------------------------------------------------------------------------
# meta.json and crash recovery
# --------------------------------------------------------------------------------------------


def _write_meta(run_dir: Path, meta: RunMeta) -> None:
    """Write `meta.json` atomically (temp file + rename), so it is never half written."""
    target = Path(run_dir) / "meta.json"
    temp = target.with_name(f".meta.json.{os.getpid()}.tmp")
    temp.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


def set_run_status(run_dir: str | Path, status: RunStatus, *, now: float | None = None) -> RunMeta:
    """Set the status in `meta.json`: `started` on the first `running`, `ended` when final."""
    run_dir = Path(run_dir)
    meta = load_run_meta(run_dir)
    now = time.time() if now is None else now
    update: dict[str, object] = {"status": status}
    if status is RunStatus.RUNNING and meta.started is None:
        update["started"] = now
    if status.is_final:
        update["ended"] = now
    meta = meta.model_copy(update=update)
    _write_meta(run_dir, meta)
    return meta


def mark_interrupted_runs(workfolder: str | Path) -> list[str]:
    """Set runs left `preparing`, `running`, `paused` or `finalizing` to `interrupted`.

    These runs were active when arr went down; a crash during the preparation also counts.

    Called when arr starts, before any run of this process. Logs stay as they are and remain
    readable; returns the IDs of the runs that were changed.
    """
    runs_dir = Path(workfolder).expanduser() / "runs"
    changed = []
    for meta_path in sorted(runs_dir.glob("*/meta.json")):
        run_dir = meta_path.parent
        try:
            meta = load_run_meta(run_dir)
        except (OSError, ValueError):
            logger.warning("meta.json in %s ist nicht lesbar", run_dir)
            continue
        if meta.status not in INTERRUPTIBLE_STATUSES:
            continue
        set_run_status(run_dir, RunStatus.INTERRUPTED)
        OrchestratorLog(run_dir).write(
            "status", status=RunStatus.INTERRUPTED.value, previous=meta.status.value
        )
        changed.append(meta.run_id)
    return changed


# --------------------------------------------------------------------------------------------
# Run venv (docs/07 §2)
# --------------------------------------------------------------------------------------------


def base_interpreter(base_python: str | None) -> Path:
    """Interpreter the run venv is built from.

    `base_python` is `AgentFile.base_python`: the base venv from `toolchain.python`, or the
    installation prefix when an interpreter was given there (`/usr/bin/python3` → `/usr`).
    Its `bin/python3` (or `bin/python`) is used. Without one: the Python installation arr's
    own environment is based on (`sys.base_prefix`), i.e. the system Python, not arr's venv.
    """
    prefixes = [Path(base_python)] if base_python else []
    prefixes.append(Path(sys.base_prefix))
    for prefix in prefixes:
        for name in ("python3", "python"):
            candidate = prefix / "bin" / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return Path(sys.executable)


def create_run_venv(run_dir: str | Path, base_python: str | None, *, with_pip: bool = True) -> Path:
    """Create `<run>/.venv` over the base Python, with `--system-site-packages` (docs/07 §2).

    A venv cannot see the packages of another venv through `--system-site-packages` (it is
    always based on the underlying installation). So when the base is a venv, a
    `_arr_base_venv.pth` in the run venv adds the base venv's `site-packages` after its own:
    packages installed in the run see the base packages, and the base venv never changes.
    Raises `ArrError` if the venv cannot be created.
    """
    target = Path(run_dir) / ".venv"
    python = base_interpreter(base_python)
    command = [str(python), "-m", "venv", "--system-site-packages"]
    if not with_pip:
        command.append("--without-pip")
    command.append(str(target))
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=VENV_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise ArrError(
            f"Die Run-venv konnte nicht angelegt werden (mit {python}): {detail.strip()[-500:]} "
            "Prüfe „toolchain.python“ in arr.yaml oder setze „run_venv: off“."
        ) from None
    if base_python and (Path(base_python) / "pyvenv.cfg").is_file():
        base_sites = sorted(Path(base_python).glob("lib/python*/site-packages"))
        own_sites = sorted(target.glob("lib/python*/site-packages"))
        if base_sites and own_sites:
            text = "".join(f"{site.absolute()}\n" for site in base_sites)
            (own_sites[0] / BASE_VENV_PTH).write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------------------------
# RunManager
# --------------------------------------------------------------------------------------------


class RunManager:
    """Runs one run through its lifecycle (docs/02 §7); see the module docstring.

    `topology`, `backend_factory`, `decider` and `finalizer` come from outside (W2-04,
    W2-05). The factory is called after room, PolicyEngine (with all profiles) and inbox are
    set up; `manager.inbox` and `manager.room` are available to it then. `stages` are the
    planned stages for the checkpoint `plan` (default: the names of `run.yaml stages`).
    The other arguments are passed to `prepare_run`.

    `stop()`, `pause()` and `resume()` may be called from the event loop at any time.
    """

    def __init__(
        self,
        workfolder: str | Path,
        run_input: RunInput,
        resolved: ResolvedRun,
        *,
        topology: Topology,
        backend_factory: BackendFactory,
        settings: WorkfolderSettings | None = None,
        decider: ApprovalDecider | None = None,
        finalizer: Finalizer | None = None,
        stages: Sequence[str] | None = None,
        starter: Starter = Starter.CLI,
        task_base: Path | None = None,
        profiles: ProfilesFile | None = None,
        run_id: str | None = None,
        allow_external_task_file: bool = False,
        now: datetime | None = None,
        pacing: PacingSettings | None = None,
        stop_grace_s: float = STOP_GRACE_S,
        venv_with_pip: bool = True,
    ) -> None:
        self.workfolder = Path(workfolder).expanduser().absolute()
        self.run_input = run_input
        self.resolved = resolved
        self.topology = topology
        self.backend_factory = backend_factory
        self.settings = settings or load_workfolder_settings(self.workfolder)
        self.decider = decider
        self.finalizer = finalizer
        self.stages = list(stages) if stages is not None else [s.name for s in resolved.stages]
        self.pacing = pacing or PacingSettings()
        self.stop_grace_s = stop_grace_s
        self.venv_with_pip = venv_with_pip
        self._prepare_args: Mapping[str, object] = {
            "starter": starter,
            "task_base": task_base,
            "profiles": profiles,
            "run_id": run_id,
            "allow_external_task_file": allow_external_task_file,
            "now": now,
        }
        self.prepared: PreparedRun | None = None
        self.room: RoomStore | None = None
        self.inbox: ApprovalInbox | None = None
        self.orchestrator: Orchestrator | None = None
        self.backends: dict[str, AgentBackend] = {}
        self.loggers: dict[str, EventLogger] = {}
        self.log: OrchestratorLog | None = None
        self.error: BaseException | None = None
        self._status: RunStatus | None = None
        self._started = False
        self._stop_early = False
        self._tasks: list[asyncio.Task[None]] = []
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def status(self) -> RunStatus | None:
        """Current status (as in `meta.json`), None before `run()`."""
        return self._status

    @property
    def run_dir(self) -> Path:
        """The run folder (after preparing)."""
        if self.prepared is None:
            raise RuntimeError("the run is not prepared yet")
        return self.prepared.run_dir

    # -- control ----------------------------------------------------------------------------

    def stop(self) -> None:
        """Stop the run: approvals cancelled, turns interrupted, then the finalizer (A2).

        Acts before the start and in `preparing`, `running` and `paused`; before the agents
        run, the topology is skipped. In `finalizing` and after it, `stop` is ignored: the
        finalizer runs anyway and a regularly ended run stays `done`. Safe to call more than
        once.
        """
        if self._status is not None and self._status not in STOPPABLE_STATUSES:
            return
        orch = self.orchestrator
        if orch is None or self._status is None or self._status is RunStatus.PREPARING:
            self._stop_early = True
            return
        if not orch.stop_requested:
            self._stop_task = asyncio.get_running_loop().create_task(orch.halt(StopReason.USER))

    def pause(self) -> None:
        """Pause between turns (status `paused`); running turns go on."""
        if self.orchestrator is not None:
            self.orchestrator.pause("user")

    def resume(self) -> None:
        """Lift the user's pause; an open escalation keeps the run paused."""
        if self.orchestrator is not None:
            self.orchestrator.resume("user")

    # -- lifecycle --------------------------------------------------------------------------

    async def run(self) -> RunStatus:
        """Run the whole lifecycle and return the final status.

        Configuration errors of `prepare_run` are raised (no folder is left, a reserved one
        gets `failed`). Everything later ends as `failed` with the error in
        `orchestrator.jsonl` and in `self.error`; the finalizer runs in every case.
        """
        if self._started:
            raise RuntimeError("RunManager.run() can only be called once")
        self._started = True
        try:
            self.prepared = await asyncio.to_thread(self._prepare)
        except BaseException:
            self._fail_reserved()
            raise
        self.log = OrchestratorLog(self.prepared.run_dir)
        self._status = RunStatus.PREPARING
        self.log.write("status", status=RunStatus.PREPARING.value)
        cancelled = False
        try:
            await self._setup()
            if not self._stop_early:
                self._set_status(RunStatus.RUNNING)
                await self._run_topology()
        except asyncio.CancelledError:
            cancelled = True
            self._stop_early = True
            if self.orchestrator is not None:
                await self.orchestrator.halt(StopReason.USER, cancelled=True)
        except Exception as exc:
            logger.exception("Run %s fehlgeschlagen", self.prepared.run_id)
            self.error = exc
            self.log.write("error", message=str(exc), type=type(exc).__name__)
        await self._teardown()
        self._set_status(RunStatus.FINALIZING)
        if self.finalizer is not None:
            try:
                await self.finalizer(self.prepared)
            except Exception as exc:
                logger.exception("Finalizer von %s fehlgeschlagen", self.prepared.run_id)
                self.error = self.error or exc
                self.log.write(
                    "error", message=str(exc), type=type(exc).__name__, where="finalizer"
                )
        final = self._final_status()
        self._set_status(final)
        if cancelled:
            raise asyncio.CancelledError
        return final

    def _prepare(self) -> PreparedRun:
        return prepare_run(
            self.workfolder,
            self.run_input,
            self.resolved,
            self.settings,
            **self._prepare_args,  # type: ignore[arg-type]
        )

    def _fail_reserved(self) -> None:
        """A reserved run that could not be prepared ends as `failed` (its folder stays)."""
        run_id = self._prepare_args.get("run_id")
        if not isinstance(run_id, str):
            return
        run_dir = self.workfolder / "runs" / run_id
        with contextlib.suppress(OSError, ValueError):
            if load_run_meta(run_dir).status in RESERVED_STATUSES:
                set_run_status(run_dir, RunStatus.FAILED)

    def _final_status(self) -> RunStatus:
        orch = self.orchestrator
        if self._stop_early or (orch is not None and orch.stop_reason is StopReason.USER):
            return RunStatus.STOPPED
        if self.error is not None:
            return RunStatus.FAILED
        return RunStatus.DONE

    def _set_status(self, status: RunStatus) -> None:
        assert self.prepared is not None and self.log is not None
        set_run_status(self.prepared.run_dir, status)
        self._status = status
        self.log.write("status", status=status.value)

    def _on_pause(self, paused: bool) -> None:
        if self._status in (RunStatus.RUNNING, RunStatus.PAUSED):
            self._set_status(RunStatus.PAUSED if paused else RunStatus.RUNNING)

    async def _setup(self) -> None:
        prepared = self.prepared
        assert prepared is not None
        run_dir = prepared.run_dir
        resolved = prepared.resolved
        paths = RunPaths(self.workfolder, run_dir, self.settings.toolchain)
        paths.tmp_dir.mkdir(exist_ok=True)
        if self.settings.toolchain.run_venv is RunVenvMode.OVERLAY:
            base = next((a.file.base_python for a in prepared.agents.values()), None)
            await asyncio.to_thread(create_run_venv, run_dir, base, with_pip=self.venv_with_pip)

        self.room = room = RoomStore.create(run_dir)
        roles = {a.id: Path(a.role).stem for a in resolved.agents if a.role}
        room.setup_channels(resolved.groupchats, roles)

        rules = PermissionRules.from_settings(self.settings.permissions)
        engine = PolicyEngine([], rules=rules, log=ApprovalLog(run_dir / "approvals.jsonl"))
        for agent in prepared.agents.values():
            engine.add_profile(profile_for(agent.file.config, paths, rules))
        self.inbox = ApprovalInbox(engine, decider=self.decider, timeout=resolved.approvals.timeout)

        for agent_id, agent in prepared.agents.items():
            self.loggers[agent_id] = EventLogger(agent.agent_dir)
            self.backends[agent_id] = self.backend_factory(agent)
        for backend in self.backends.values():
            await backend.start()

        self.orchestrator = Orchestrator(
            prepared,
            room=room,
            backends=self.backends,
            loggers=self.loggers,
            inbox=self.inbox,
            concurrency=self.settings.concurrency,
            stages=self.stages,
            pacing=self.pacing,
            log=self.log,
            on_pause=self._on_pause,
        )

    async def _run_topology(self) -> None:
        orch = self.orchestrator
        assert orch is not None
        await orch.checkpoint(PLAN_STAGE, stages=list(self.stages))
        self._tasks.append(asyncio.create_task(orch.watch()))
        wall_time = self.resolved.limits.wall_time
        if wall_time is not None:
            self._tasks.append(
                asyncio.create_task(self._watch_wall_time(wall_time.total_seconds()))
            )
        topology = asyncio.create_task(self.topology(orch))
        halted = asyncio.create_task(orch.halted.wait())
        try:
            await asyncio.wait({topology, halted}, return_when=asyncio.FIRST_COMPLETED)
            if not topology.done():
                await asyncio.wait({topology}, timeout=self.stop_grace_s)
            if not topology.done():
                assert self.log is not None
                self.log.write(
                    "error",
                    message="Die Topologie hat nach dem Stopp nicht aufgehört und wurde "
                    "abgebrochen.",
                    where="topology",
                )
                topology.cancel()
                await asyncio.wait({topology})
        finally:
            halted.cancel()
            if not topology.done():
                topology.cancel()
                await asyncio.wait({topology})
        if not topology.cancelled() and (error := topology.exception()) is not None:
            raise error

    async def _watch_wall_time(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        assert self.orchestrator is not None
        await self.orchestrator.halt(StopReason.LIMIT, LimitKind.WALL_TIME, seconds=seconds)

    async def _teardown(self) -> None:
        """Stop the helper tasks, close backends (with a timeout) and the loggers."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for agent_id, backend in self.backends.items():
            try:
                await asyncio.wait_for(backend.close(), CLOSE_TIMEOUT_S)
            except Exception:
                logger.exception("close() von %s fehlgeschlagen", agent_id)
        for events_log in self.loggers.values():
            events_log.close()

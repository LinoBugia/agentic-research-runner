"""Area `runs`: runs of the workfolder, their detail views, actions and starting runs (W3-01,
docs/08-ux-konzept.md §5.3, §6.9; docs/02-workfolder-und-runs.md §7).

Data comes from the run folders through the `RunStore` (`rundir`), so runs of the service,
runs of `arr run` and finished runs look the same. Runs started here (`start_run`) run as a
task in the service's event loop through `run_launch` (reservation with `starter: gui`,
`RunLock`, backends, finalizer, secret scan); the `RunManager` of such a run is reachable
through the `RunRegistry` for pause, resume, stop and messages. A run of another process
(`arr run`) can only be stopped (`stop.request`); pause, resume and messages answer
`not_allowed` with the hint that it runs in the terminal. W3-02 changes this module (and the
pages Runs, Run-Detail, Neuer Run) and nothing else of `RealApi`.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Any

from arr.errors import ArrError, ConfigError
from arr.models import RunInput, RunStatus, Starter
from arr.resolve import dump_yaml_model, validate_model
from arr.room.types import MessageVia
from arr.runs.launch import plan_run_input, request_stop, reserve_run, run_launch
from arr.runs.manager import RunManager, set_run_status
from arr.service.api_client import count_active, count_runs_waiting
from arr.service.api_models import (
    ActionResult,
    AgentChanges,
    AgentState,
    ApiException,
    CapacityInfo,
    ChannelInfo,
    ErrorCode,
    FieldIssue,
    LogEntry,
    Message,
    MessagesPage,
    PostMessageRequest,
    ResolveResult,
    RunAction,
    RunCounts,
    RunDetail,
    RunFilter,
    RunResults,
    RunsPage,
    RunStartAgent,
    RunStartChannel,
    RunSummary,
    StartRunResponse,
    TranscriptKind,
    TranscriptPage,
)
from arr.service.real_api.approvals import ApprovalsArea
from arr.service.real_api.base import RunHandle, ServiceState, not_allowed, not_found
from arr.service.real_api.rundir import (
    RunRecord,
    agent_changes,
    agent_states,
    channel_infos,
    log_entries,
    messages_page,
    model_label,
    role_label,
    run_detail,
    run_results,
    run_summary,
    to_message,
    transcript_page,
    vendor_of,
)
from arr.service.real_api.storage import STORAGE_ACTIONS, StorageArea

log = logging.getLogger("arr.service.runs")

TERMINAL_HINT = (
    "Der Run läuft im Terminal („arr run“), nicht in arr selbst. Von hier geht nur Stoppen; "
    "Pausieren, Fortsetzen und Nachrichten gehen bei Runs, die du in arr gestartet hast."
)
FINISHED_HINT = "Der Run ist beendet. {what} geht nur bei einem laufenden Run."


class RunsArea:
    """The run methods of `ApiClient` (see the module docstring)."""

    def __init__(self, state: ServiceState, approvals: ApprovalsArea, storage: StorageArea) -> None:
        self.state = state
        self.approvals = approvals
        self.storage = storage

    # -- helpers ----------------------------------------------------------------------------

    def _record(self, run_id: str) -> RunRecord:
        record = self.state.store.get(run_id)
        if record is None:
            raise not_found("Run", run_id)
        if not record.settled:
            record.refresh()
        return record

    def _summary(self, record: RunRecord, *, with_agents: bool) -> RunSummary:
        agents = agent_states(record) if with_agents and record.agents else None
        return run_summary(
            record, base_url=self.state.base_url, now=self.state.now(), agents=agents
        )

    # -- lists and detail --------------------------------------------------------------------

    async def list_runs(
        self,
        status: RunFilter = RunFilter.ALL,
        project: str | None = None,
        starter: Starter | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunsPage:
        records = self.state.store.scan()
        for record in records:
            if not record.settled:
                record.refresh()
        summaries = [self._summary(r, with_agents=r.status.is_active) for r in records]
        base = [
            s
            for s in summaries
            if (project is None or s.project == project)
            and (starter is None or s.starter == starter)
        ]
        needle = (q or "").strip().lower()
        if needle:
            base = [s for s in base if needle in s.name.lower() or needle in s.id.lower()]
        counts = RunCounts(
            all=len(base),
            active=count_active(base),
            waiting=count_runs_waiting(base),
            done=sum(1 for s in base if s.status is RunStatus.DONE),
            error=sum(1 for s in base if s.status in (RunStatus.FAILED, RunStatus.INTERRUPTED)),
            archived=sum(1 for s in base if s.archived),
        )
        test = {
            RunFilter.ALL: lambda s: True,
            RunFilter.ACTIVE: lambda s: s.active,
            RunFilter.WAITING: lambda s: s.open_approvals > 0,
            RunFilter.DONE: lambda s: s.status is RunStatus.DONE,
            RunFilter.ERROR: lambda s: s.status in (RunStatus.FAILED, RunStatus.INTERRUPTED),
            RunFilter.ARCHIVED: lambda s: s.archived,
        }[status]
        rows = [s for s in base if test(s)]
        rows.sort(key=lambda s: (not s.active, -(s.created_at if s.active else (s.ended_at or 0))))
        try:
            offset = max(0, int(cursor or 0))
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 1000))
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return RunsPage(
            items=page,
            counts=counts,
            total=len(rows),
            next_cursor=next_cursor,
            projects=sorted({s.project for s in summaries}),
        )

    async def get_run(self, run_id: str) -> RunDetail:
        record = self._record(run_id)
        return run_detail(
            record,
            base_url=self.state.base_url,
            now=self.state.now(),
            owned=self.state.registry.owns(run_id),
            agents=agent_states(record) if record.agents else None,
        )

    async def list_channels(self, run_id: str) -> list[ChannelInfo]:
        return channel_infos(self._record(run_id))

    async def get_messages(
        self,
        run_id: str,
        channel: str,
        before_id: int | None = None,
        since_id: int | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        record = self._record(run_id)
        page = messages_page(record, channel, before_id=before_id, since_id=since_id, limit=limit)
        if page is None:
            raise not_found("Kanal", f"#{channel}")
        return page

    async def list_agents(self, run_id: str) -> list[AgentState]:
        return agent_states(self._record(run_id))

    async def get_transcript(
        self,
        run_id: str,
        agent_id: str,
        from_seq: int | None = None,
        limit: int = 200,
        kinds: Iterable[TranscriptKind] | None = None,
    ) -> TranscriptPage:
        record = self._record(run_id)
        page = transcript_page(record, agent_id, from_seq=from_seq, limit=limit, kinds=kinds)
        if page is None:
            raise not_found("Agent", agent_id)
        return page

    async def get_results(self, run_id: str) -> RunResults:
        return run_results(self._record(run_id))

    async def get_log(self, run_id: str) -> list[LogEntry]:
        return log_entries(self._record(run_id))

    async def get_changes(self, run_id: str) -> list[AgentChanges]:
        return agent_changes(self._record(run_id))

    # -- actions ----------------------------------------------------------------------------

    async def run_action(
        self, run_id: str, action: RunAction, dry_run: bool = False
    ) -> ActionResult:
        if action in STORAGE_ACTIONS:
            raise self.storage.action_error(action)
        record = self._record(run_id)
        status = record.status
        words = {RunAction.PAUSE: "Pausieren", RunAction.RESUME: "Fortsetzen"}
        what = words.get(action, "Stoppen")
        if status.is_final:
            raise not_allowed(FINISHED_HINT.format(what=what))
        handle = self.state.registry.get(run_id)
        owned = self.state.registry.owns(run_id)
        if owned and handle is not None:
            message = self._own_action(handle, action, status, dry_run=dry_run)
        else:
            message = self._foreign_action(record, action, dry_run=dry_run)
        record.refresh_meta()
        return ActionResult(ok=True, message=message, run=self._summary(record, with_agents=False))

    def _own_action(
        self, handle: RunHandle, action: RunAction, status: RunStatus, *, dry_run: bool
    ) -> str:
        manager = handle.manager
        if action is RunAction.PAUSE:
            if status is not RunStatus.RUNNING:
                raise not_allowed("Pausieren geht nur, solange der Run läuft.")
            if not dry_run and manager is not None:
                manager.pause()
            return "Pausiert: Laufende Turns enden noch, danach startet kein neuer."
        if action is RunAction.RESUME:
            if status is not RunStatus.PAUSED:
                raise not_allowed("Fortsetzen geht nur bei einem pausierten Run.")
            if not dry_run and manager is not None:
                manager.resume()
            return "Fortgesetzt: Die Agenten arbeiten weiter."
        if dry_run:
            return (
                "Der Run würde gestoppt: Laufende Turns werden unterbrochen, danach läuft der "
                "Finalizer."
            )
        if manager is not None:
            manager.stop()
        elif handle.task is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(OSError, ValueError):
                set_run_status(handle.run_dir, RunStatus.STOPPED)
        return "Stopp angefordert: Laufende Turns werden unterbrochen, danach läuft der Finalizer."

    def _foreign_action(self, record: RunRecord, action: RunAction, *, dry_run: bool) -> str:
        if action is not RunAction.STOP:
            raise not_allowed(TERMINAL_HINT)
        if dry_run:
            return "Der Prozess, der den Run ausführt, würde ihn in wenigen Sekunden stoppen."
        try:
            new = request_stop(record.run_dir, source="gui")
        except ArrError as exc:
            raise not_allowed(exc.message) from None
        if not new:
            return "Für diesen Run liegt schon eine Stopp-Anforderung vor."
        return (
            "Stopp angefordert: Der Prozess, der den Run ausführt (arr run), stoppt ihn "
            "innerhalb weniger Sekunden, danach läuft der Finalizer."
        )

    async def post_message(self, run_id: str, channel: str, request: PostMessageRequest) -> Message:
        record = self._record(run_id)
        if record.status.is_final:
            raise not_allowed("Der Run ist beendet. Nachrichten gehen nur an laufende Runs.")
        handle = self.state.registry.get(run_id)
        if not self.state.registry.owns(run_id) or handle is None or handle.manager is None:
            raise not_allowed(TERMINAL_HINT)
        room = handle.manager.room
        if room is None:
            raise not_allowed("Der Run wird noch vorbereitet; der Gruppenchat ist noch nicht da.")
        try:
            posted = room.post(
                channel,
                request.body,
                sender="user",
                kind=request.kind,
                to=list(request.to),
                via=MessageVia.GUI,
            )
        except ArrError as exc:
            raise ApiException.of(ErrorCode.INVALID, exc.message) from None
        return to_message(posted)

    # -- resolving and starting --------------------------------------------------------------

    def _plan(self, run_input: dict[str, Any]) -> Any:
        """A `Launch` from the GUI's run input; `ConfigError`/`ArrError` for problems."""
        data = dict(run_input)
        params = data.pop("params", None)
        model = validate_model(RunInput, data, source="Neuer Run")
        return plan_run_input(
            model,
            workfolder=self.state.workfolder,
            params=params if isinstance(params, dict) else None,
            profiles=self.state.profiles(),
            source="Neuer Run",
        )

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
        try:
            launch = await asyncio.to_thread(self._plan, run_input)
        except ConfigError as exc:
            return ResolveResult(
                resolved_run_yaml="",
                errors=[FieldIssue(path=i.path, message=i.message) for i in exc.issues],
            )
        except ArrError as exc:
            return ResolveResult(
                resolved_run_yaml="", errors=[FieldIssue(path="", message=exc.message)]
            )
        resolved = launch.resolved
        needed: dict[str, int] = {}
        for agent in resolved.agents:
            needed[agent.auth] = needed.get(agent.auth, 0) + 1
        capacity = []
        for profile, count in needed.items():
            limit = self.state.settings.concurrency.get(profile)
            gate = self.state.gates.get(profile)
            in_use = gate.active if gate is not None else 0
            capacity.append(
                CapacityInfo(
                    profile=profile,
                    free=max(0, (limit - in_use)) if limit is not None else count,
                    needed=count,
                    limit=limit if limit is not None else count,
                )
            )
        return ResolveResult(
            resolved_run_yaml=dump_yaml_model(resolved),
            agents=[
                RunStartAgent(
                    id=a.id,
                    role_label=role_label(a.role, a.base_id),
                    provider=a.provider,
                    vendor=vendor_of(a.provider),
                    model=a.model,
                    model_label=model_label(a.model),
                    profile=a.auth,
                    tools={"shell": a.tools.shell, "web": a.tools.web},
                    read_dirs=[str(d) for d in a.read_dirs],
                    workspace_mode=a.workspace.mode,
                    skills=list(a.skills),
                )
                for a in resolved.agents
            ],
            channels=[
                RunStartChannel(
                    name=c.name,
                    members=list(c.members),
                    mode=c.mode,
                    anonymous=c.anonymous,
                    open_from=c.open_from,
                )
                for c in resolved.groupchats
            ],
            capacity=capacity,
        )

    async def start_run(self, run_input: dict[str, Any]) -> StartRunResponse:
        try:
            launch = await asyncio.to_thread(self._plan, run_input)
        except ConfigError as exc:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID,
                exc.issues[0].message,
                issues=[FieldIssue(path=i.path, message=i.message) for i in exc.issues],
            ) from None
        except ArrError as exc:
            raise ApiException.of(ErrorCode.CONFIG_INVALID, exc.message) from None
        run_dir = reserve_run(launch, starter=Starter.GUI)
        handle = RunHandle(run_id=run_dir.name, run_dir=run_dir, launch=launch)
        handle.task = asyncio.create_task(self._execute(handle), name=f"arr-run-{run_dir.name}")
        self.state.registry.add(handle)
        self.state.store.get(run_dir.name)
        return StartRunResponse(
            run_id=run_dir.name, status=RunStatus.QUEUED, gui_url=self.state.run_url(run_dir.name)
        )

    async def _execute(self, handle: RunHandle) -> None:
        run_id = handle.run_id

        def on_manager(manager: RunManager) -> None:
            handle.manager = manager

        def report(line: str) -> None:
            if line:
                log.info("[%s] %s", run_id, line)

        try:
            await run_launch(
                handle.launch,
                decider=self.approvals.decider,
                report=report,
                builders=self.state.builders,
                signals=False,
                scan=self.state.scan,
                secret_reader=self.state.secret_reader,
                run_dir=handle.run_dir,
                starter=Starter.GUI,
                on_manager=on_manager,
                gates=self.state.gates,
                **self.state.manager_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except ArrError as exc:
            log.warning("[%s] Run nicht gestartet: %s", run_id, exc.message)
        except Exception:
            log.exception("[%s] Run im Service fehlgeschlagen", run_id)

    async def stop_all(self) -> None:
        """Stop every run of the service and wait for the finalizers and scans (Ctrl-C)."""
        handles = self.state.registry.active()
        for handle in handles:
            if handle.manager is not None:
                handle.manager.stop()
            elif handle.task is not None:
                handle.task.cancel()
        tasks = [h.task for h in handles if h.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

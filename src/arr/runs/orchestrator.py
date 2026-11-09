"""Orchestrator: the `RunContext` implementation that drives the agents of a running run.

The RunManager (`arr.runs.manager`) builds one `Orchestrator` per run after preparing it and
hands it to the topology (`async def run(ctx: RunContext) -> None`, E8). The orchestrator

- runs turns: inbox in the prompt, assignment for `get_assignment()`, every event into the
  agent's EventLogger, always leaving the turn with `contextlib.aclosing(...)`;
- enforces the limits (turns per agent, budget; wall time via `halt` from the manager), the
  concurrency per auth profile (`ProfileGate`) and the reaction to `rate_limit` events;
- pauses between turns, stops (`halt`): open approvals are cancelled, running turns
  interrupted;
- watches the room: hints mid-turn, `blocked` while an approval waits, delivery state of
  `user` messages, waits of the agents, turns without events, and the discussion phases of
  `arr.runs.pacing` (`discuss`);
- sets the status after a turn (docs/09 §4.2): `done` when the agent reported it, submitted a
  result without an open question of its own, or was `done` before and was woken by a message
  to it (L13); `waiting` after `wait_until`; `idle` otherwise;
- turns a question of an agent to the user (`@user`, L16) into an open item „Frage“ of the
  approval inbox and closes it when the user answers in the channel or in the inbox, or when
  the run ends;
- writes `orchestrator.jsonl`; every wait is an entry `waiting` (docs/09 §4.6).

See docs/01-architektur.md §6, docs/02-workfolder-und-runs.md §4, §7, docs/06-gruppenchats.md.
"""

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from arr.backends.base import (
    AgentBackend,
    AgentEvent,
    ErrorData,
    EventKind,
    TurnEndData,
    TurnOutcome,
    TurnStatus,
    Usage,
)
from arr.errors import ArrError
from arr.models import AgentStatus, ResolvedRun, Rubric
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.types import ApprovalType, EscalationOption
from arr.room.address import address, is_user_question
from arr.room.store import ORCHESTRATOR, USER, WAIT_UNTIL_MAX_MIN, AgentRoom, RoomStore
from arr.room.types import (
    DeliveryStatus,
    Message,
    MessageKind,
    Rating,
    Result,
    RoomError,
    WaitKind,
    WaitState,
)
from arr.runs.context import AgentHandle, DiscussionOutcome, RunContext, TurnJob
from arr.runs.eventlog import EventLogger
from arr.runs.pacing import (
    HintGate,
    PacingSettings,
    compose_hint,
    is_question_to,
    render_turn_prompt,
    run_discussion,
)
from arr.runs.prepare import PreparedRun

__all__ = [
    "PLAN_STAGE",
    "LimitKind",
    "LongWait",
    "Orchestrator",
    "OrchestratorLog",
    "ProfileGate",
    "StopReason",
]

logger = logging.getLogger(__name__)

ORCHESTRATOR_LOG = "orchestrator.jsonl"
PLAN_STAGE = "plan"
"""Checkpoint with all planned stages, written at start (`stages=[…]`)."""

WORKED_ON = "{agent} hat ohne Antwort weitergearbeitet."
"""Note of a question to the user that closes because its asker submitted a result."""

RATE_LIMIT_BACKOFF_S = 60.0
"""Pause of a profile after a rate limit without `resets_at`."""

INTERRUPT_TIMEOUT_S = 5.0
END_TURN_RETRY_S = 10.0
"""arr ends a turn (L7, L12) with `interrupt()`; still running after this, it tries again."""
END_TURN_HARD_S = 10.0
"""Still running this long after the second `interrupt()`: arr cancels the turn (W3-07.2 NB2)."""


class StopReason(StrEnum):
    """Why a run stops before its topology is finished."""

    USER = "user"
    LIMIT = "limit"


class LimitKind(StrEnum):
    """Run limits (docs/01 §6, `Limits`)."""

    WALL_TIME = "wall_time"
    TURNS_PER_AGENT = "turns_per_agent"
    BUDGET_USD = "budget_usd"


_AGENT_LIMIT_TEXT = "Dieser Agent hat seine Turns aufgebraucht (turns_per_agent)."

_LIMIT_TEXT = {
    LimitKind.WALL_TIME: "Die Gesamtlaufzeit des Runs ist erreicht.",
    LimitKind.TURNS_PER_AGENT: "Die Zahl der Turns pro Agent ist erreicht.",
    LimitKind.BUDGET_USD: "Das Budget des Runs ist erreicht.",
}


class OrchestratorLog:
    """Append-only `orchestrator.jsonl`: one line `{"ts", "kind", "data"}` per entry.

    Kinds written by the orchestrator and the RunManager: `status`, `checkpoint`, `turn`,
    `turn_end`, `limit`, `stop`, `pause`, `resume`, `rate_limit`, `hint`, `wake`,
    `wake_skipped`, `ping_pong`, `channel_open`, `discussion_start`, `discussion_end`,
    `escalation`, `escalation_decided`, `error`; `prepare_run` writes `warning`.
    Since W3-07 (docs/09 §4.6): `waiting` (`agent` or `channel`, `reason`, `until`; reason
    `none` for a channel means it no longer waits), `round`, `wake_scheduled`, `agent_done`
    (an agent became `done` after a turn, with `reason`) and `turn_silent`. Since W3-09:
    `waiting` with reason `result_from` and the awaited agent in `result_from` (L14), and
    `user_question` (`agent`, `channel`, `msg`, `item`, `state` open/answered/closed, L16).
    """

    def __init__(self, run_dir: Path) -> None:
        self.path = Path(run_dir) / ORCHESTRATOR_LOG
        self._lock = threading.Lock()

    def write(self, kind: str, **data: Any) -> dict[str, Any]:
        """Append one entry and flush; returns it."""
        entry = {"ts": time.time(), "kind": kind, "data": data}
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return entry

    def entries(self) -> list[dict[str, Any]]:
        """All entries (empty if the file does not exist)."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


class ProfileGate:
    """Concurrency of one auth profile plus the reaction to its rate limits (docs/01 §4.1).

    `limit` is the number of turns that may run at once (`concurrency` in `arr.yaml`, None =
    unlimited). A `rate_limit` event with status `allowed_warning` throttles the profile to
    one turn at a time, any other non-`allowed` status pauses it; both until `resets_at`
    (Unix time) or `RATE_LIMIT_BACKOFF_S`. Running turns are not touched.
    """

    def __init__(
        self,
        name: str,
        limit: int | None,
        *,
        clock: Callable[[], float] = time.time,
        backoff_s: float = RATE_LIMIT_BACKOFF_S,
    ) -> None:
        self.name = name
        self.limit = limit
        self.active = 0
        self.paused_until = 0.0
        self.throttled_until = 0.0
        self._clock = clock
        self._backoff_s = backoff_s
        self._waiters: list[asyncio.Future[None]] = []

    def _capacity(self, now: float) -> int | None:
        return 1 if now < self.throttled_until else self.limit

    async def acquire(self, abort: Callable[[], bool]) -> bool:
        """Wait for a free slot; False (without a slot) as soon as `abort()` is true."""
        loop = asyncio.get_running_loop()
        while True:
            if abort():
                return False
            now = self._clock()
            deadlines = []
            if now < self.paused_until:
                deadlines.append(self.paused_until)
            else:
                capacity = self._capacity(now)
                if capacity is None or self.active < capacity:
                    self.active += 1
                    return True
                if now < self.throttled_until:
                    deadlines.append(self.throttled_until)
            waiter = loop.create_future()
            self._waiters.append(waiter)
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            try:
                await asyncio.wait_for(waiter, timeout)
            except TimeoutError:
                pass
            finally:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)

    def has_slot(self) -> bool:
        """True if `acquire` would get a slot at once (for the `waiting` entry)."""
        now = self._clock()
        if now < self.paused_until:
            return False
        capacity = self._capacity(now)
        return capacity is None or self.active < capacity

    def contended(self) -> bool:
        """True while a turn waits for a slot that a running turn could free (L12); not
        during a rate-limit pause, which no freed slot shortens."""
        waiting = any(not w.done() for w in self._waiters)
        return waiting and self._clock() >= self.paused_until

    def blocked_until(self) -> float | None:
        """End of a rate-limit pause or throttle, None if only the slots are taken."""
        now = self._clock()
        if now < self.paused_until:
            return self.paused_until
        if now < self.throttled_until:
            return self.throttled_until
        return None

    def release(self) -> None:
        """Give a slot back."""
        self.active -= 1
        self.kick()

    def kick(self) -> None:
        """Let all waiters check again (slot freed, stop)."""
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def report(self, data: Mapping[str, Any]) -> tuple[str, float] | None:
        """React to `rate_limit` data; returns `("throttle" | "pause", until)` or None."""
        status = str(data.get("status") or "")
        if status in ("", "allowed"):
            return None
        now = self._clock()
        resets_at = data.get("resets_at")
        if isinstance(resets_at, int | float) and resets_at > now:
            until = float(resets_at)
        else:
            until = now + self._backoff_s
        if status == "allowed_warning":
            self.throttled_until = max(self.throttled_until, until)
            return "throttle", self.throttled_until
        self.paused_until = max(self.paused_until, until)
        return "pause", self.paused_until


class _Listener(Protocol):
    def on_message(self, msg: Message) -> None: ...
    def on_turn_end(self, agent_id: str, undelivered: Sequence[Message]) -> None: ...
    def kick(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LongWait:
    """An agent ended its turn with `wait_until` (docs/09 §4.3); `until` and `since` are Unix
    times, `result_from` the agent whose next result ends the wait (L14), `mark` the newest
    message when its turn began (results are looked for only after it, W3-09.1 B11)."""

    until: float
    file: str | None = None
    result_from: str | None = None
    since: float = 0.0
    mark: int = 0


@dataclass(slots=True)
class _ActiveTurn:
    backend: AgentBackend
    gate: HintGate
    last_event: float = 0.0
    """Loop time of the last event of the turn (warning after `turn_silence_warn_s`)."""
    silent_warned: bool = False
    tool_calls: int = 0
    pending: list[Message] = field(default_factory=list)
    blocked: bool = False
    retry_at_calls: int = 0
    delivery: asyncio.Task[None] | None = None
    """The hint on its way to this agent (at most one at a time, W2-05)."""
    ending: int = 0
    """arr ends the turn, the agent called `wait_for_messages` again after `nobody` (L7) or
    `capacity` (L12): 1 after the first `interrupt()`, 2 after the second, 3 cancelled."""
    ending_since: float = 0.0
    """Loop time of the last step of `ending`."""
    consume: asyncio.Task[None] | None = None
    """The task that reads the backend's events; cancelling it ends the turn hard."""


@dataclass(slots=True)
class _TurnRecord:
    """What the orchestrator collects from the events of one turn."""

    number: int | None = None
    status: TurnStatus | None = None
    text: str | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    session: str | None = None
    num_turns: int | None = None


class Orchestrator(RunContext):
    """`RunContext` of one running run (W2-03); built and driven by the RunManager.

    Besides the `RunContext` API, topologies can use `discuss` for a discussion phase in a
    channel (docs/06 §5). The RunManager uses `watch`, `halt`, `pause`/`resume` and
    `halted`. All public methods run on the event loop of the run.
    """

    def __init__(
        self,
        prepared: PreparedRun,
        *,
        room: RoomStore,
        backends: Mapping[str, AgentBackend],
        loggers: Mapping[str, EventLogger],
        inbox: ApprovalInbox,
        concurrency: Mapping[str, int] | None = None,
        stages: Sequence[str] = (),
        pacing: PacingSettings | None = None,
        log: OrchestratorLog | None = None,
        on_pause: Callable[[bool], None] | None = None,
        gates: MutableMapping[str, ProfileGate] | None = None,
    ) -> None:
        self.prepared = prepared
        self.room = room
        self.backends = dict(backends)
        self.loggers = dict(loggers)
        self.inbox = inbox
        self.pacing = pacing or PacingSettings()
        self.log = log or OrchestratorLog(prepared.run_dir)
        self.plan = list(stages)
        self.stage: str | None = None
        self.halted = asyncio.Event()
        self.cost_usd = 0.0
        self._on_pause = on_pause
        self._handles = {a.id: AgentHandle(a) for a in prepared.resolved.agents}
        self._locks = {agent_id: asyncio.Lock() for agent_id in self._handles}
        concurrency = concurrency or {}
        # `gates` shared by all runs of a process (the service, W3-01): a profile's
        # concurrency and rate-limit pauses then hold across runs (W2-03 review G6).
        shared = gates if gates is not None else {}
        self._gates: dict[str, ProfileGate] = {}
        for agent in prepared.resolved.agents:
            gate = shared.get(agent.auth)
            if gate is None:
                gate = shared[agent.auth] = ProfileGate(agent.auth, concurrency.get(agent.auth))
            self._gates[agent.auth] = gate
        rubric = prepared.resolved.rubric or Rubric()
        self._views: dict[str, AgentRoom] = {
            agent_id: room.for_agent(agent_id, agent.work_dir, rubric=rubric)
            for agent_id, agent in prepared.agents.items()
        }
        self._waiting = {c.name for c in prepared.resolved.groupchats if c.open_from}
        self._active: dict[str, _ActiveTurn] = {}
        self._listeners: list[_Listener] = []
        self._senders: dict[int, str] = {}
        self._wakeups: dict[tuple[str, str], int] = {}
        self._pause_reasons: set[str] = set()
        self._runnable = asyncio.Event()
        self._runnable.set()
        self._stop_reason: StopReason | None = None
        self._limit: LimitKind | None = None
        self._halt_task: asyncio.Task[None] | None = None
        self.wall_end: float | None = None
        """Unix time at which `wall_time` ends (set by the RunManager); caps `wait_until`."""
        self._long_waits: dict[str, LongWait] = {}
        self._waits: dict[str, WaitState] = {}
        self._capacity: set[str] = set()
        self._exhausted: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        """Interrupts of turns arr ends itself (a reference keeps them alive)."""
        self._yielding: dict[str, str] = {}
        """Agents asked to free their place (`wait_guard.yield_to`, L12), with the profile."""
        self._until_streak: dict[str, int] = {}
        """`wait_until` in a row that ended by their deadline (`wait_guard.until_streak`, L10)."""
        self._turn_marks: dict[str, int] = {}
        """Newest message ID when the agent's latest turn began: its inbox showed all up to it."""
        self._questions: dict[int, str] = {}
        """Open questions to the user (L16): message ID → item of the approval inbox."""
        self._last_seen = 0
        for msg in room.messages_after(0):
            self._senders[msg.id] = msg.sender
            self._last_seen = msg.id

    # ----------------------------------------------------------------------------------------
    # RunContext: properties
    # ----------------------------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        """ID of the run."""
        return self.prepared.run_id

    @property
    def config(self) -> ResolvedRun:
        """Resolved configuration of the run (as in `run.yaml`)."""
        return self.prepared.resolved

    @property
    def params(self) -> Mapping[str, Any]:
        """Final parameters (`config.topology.params`)."""
        return self.prepared.resolved.params

    @property
    def agents(self) -> Mapping[str, AgentHandle]:
        """Agents by ID in configuration order; `turns_used` and `status` are kept current."""
        return self._handles

    @property
    def stop_requested(self) -> bool:
        """True after `halt`: a user stop or a reached run limit."""
        return self._stop_reason is not None

    @property
    def stop_reason(self) -> StopReason | None:
        """Why the run stops, None while it runs normally."""
        return self._stop_reason

    @property
    def limit_reached(self) -> LimitKind | None:
        """The limit that stopped the run, if any."""
        return self._limit

    @property
    def paused(self) -> bool:
        """True while new turns wait (user pause or escalation)."""
        return bool(self._pause_reasons) and not self.stop_requested

    # ----------------------------------------------------------------------------------------
    # RunContext: turns
    # ----------------------------------------------------------------------------------------

    def _handle(self, agent_id: str) -> AgentHandle:
        try:
            return self._handles[agent_id]
        except KeyError:
            known = ", ".join(self._handles)
            raise ArrError(
                f"Den Agenten „{agent_id}“ gibt es in diesem Run nicht ({known})."
            ) from None

    async def turn(
        self,
        agent_id: str,
        prompt: str,
        *,
        stage: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> TurnOutcome:
        """Give one agent a turn and wait for it (docs/01 §6).

        - Turns of one agent run one after the other; a turn starts only while the run is not
          paused and a slot of the agent's auth profile is free (`concurrency`, rate limits).
        - The prompt gets the agent's inbox in front (`pacing.render_turn_prompt`); `prompt`,
          `stage` and `context` are stored as the assignment (`get_assignment()`).
          `context["attempt"]` becomes the `attempt` of results submitted in this turn.
        - A `stage` opens channels whose `open_from` is reached (like `checkpoint`).
        - The outcome holds status, last text, usage, the results submitted in the turn and
          the session. Status `limit`: a run limit stopped or prevented the turn;
          `interrupted`: the user stopped the run. In both cases `stop_requested` is true and
          further turns return at once without running. Errors of the backend come back as
          status `error`, they are not raised.
        """
        handle = self._handle(agent_id)
        if stage is not None:
            self._reach_stage(stage)
        async with self._locks[agent_id]:
            await self._runnable.wait()
            if (refused := self._refuse(handle)) is not None:
                return refused
            gate = self._gates[handle.config.auth]
            if not gate.has_slot():  # docs/09 §4.5: waiting for a free place is visible
                self._capacity.add(agent_id)
                self.log.write(
                    "waiting",
                    agent=agent_id,
                    reason="capacity",
                    profile=gate.name,
                    until=gate.blocked_until(),
                )
            try:
                acquired = await gate.acquire(lambda: self.stop_requested)
            finally:
                self._capacity.discard(agent_id)
            if not acquired:
                return self._not_run(handle)
            try:
                await self._runnable.wait()
                if (refused := self._refuse(handle)) is not None:
                    return refused
                return await self._run_turn(handle, gate, prompt, stage, context)
            finally:
                gate.release()

    async def turns(
        self, jobs: Sequence[TurnJob | tuple[str, str]], *, stage: str | None = None
    ) -> list[TurnOutcome]:
        """Run several turns at once (each as `turn`); outcomes in the order of `jobs`.

        Limits, concurrency and pause apply per turn; two jobs of one agent run in sequence.
        """
        normalized = [job if isinstance(job, TurnJob) else TurnJob(*job) for job in jobs]
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(self.turn(j.agent_id, j.prompt, stage=stage, context=j.context))
                for j in normalized
            ]
        return [task.result() for task in tasks]

    def _not_run(self, handle: AgentHandle) -> TurnOutcome:
        if self._limit is not None:
            status, error = TurnStatus.LIMIT, _LIMIT_TEXT[self._limit]
        else:
            status, error = TurnStatus.INTERRUPTED, "Der Run wurde gestoppt."
        return TurnOutcome(
            agent_id=handle.id, turn=handle.turns_used + 1, status=status, error=error
        )

    def _refuse(self, handle: AgentHandle) -> TurnOutcome | None:
        """An outcome without running if the run stops or a limit forbids the turn.

        `turns_per_agent` ends only this agent (L4, docs/09 §4.5): it is `done` and its turn
        returns `limit`; the run stops only when every agent has used up its turns.
        """
        if not self.stop_requested:
            limits = self.config.limits
            if limits.turns_per_agent is not None and handle.turns_used >= limits.turns_per_agent:
                self._exhaust(handle)
                if not self.stop_requested:
                    return TurnOutcome(
                        agent_id=handle.id,
                        turn=handle.turns_used + 1,
                        status=TurnStatus.LIMIT,
                        error=_AGENT_LIMIT_TEXT,
                    )
            elif limits.budget_usd is not None and self.cost_usd >= limits.budget_usd:
                self._start_halt(StopReason.LIMIT, LimitKind.BUDGET_USD, cost_usd=self.cost_usd)
        return self._not_run(handle) if self.stop_requested else None

    def _exhaust(self, handle: AgentHandle) -> None:
        """An exhausted agent would get another turn: it is `done` (reason `limit`, once);
        when every agent is exhausted, the run stops with `limit` (B3: not before)."""
        if handle.id not in self._exhausted:
            self._exhausted.add(handle.id)
            self._set_status(handle, AgentStatus.DONE, note="limit")
            self.log.write("agent_done", agent=handle.id, reason="limit", turns=handle.turns_used)
        if self._exhausted >= set(self._handles):
            self._start_halt(StopReason.LIMIT, LimitKind.TURNS_PER_AGENT, agent=handle.id)

    async def _run_turn(
        self,
        handle: AgentHandle,
        gate: ProfileGate,
        prompt: str,
        stage: str | None,
        context: Mapping[str, Any] | None,
    ) -> TurnOutcome:
        agent_id = handle.id
        backend = self.backends[agent_id]
        events_log = self.loggers[agent_id]
        handle.turns_used += 1
        number = handle.turns_used
        self._turn_marks[agent_id] = self.last_message_id()
        full_prompt = self._prompt_with_inbox(agent_id, prompt)
        self.room.add_assignment(agent_id, number, prompt, stage=stage, context=context)
        before = max((r.id for r in self.room.results(agent=agent_id)), default=0)
        was_done = handle.status is AgentStatus.DONE
        self._end_long_wait(agent_id)
        self._yielding.pop(agent_id, None)
        self.room.reset_wait_guard(agent_id)
        since, mark = time.time(), self.last_message_id()
        self._set_status(handle, AgentStatus.WORKING)
        loop = asyncio.get_running_loop()
        active = _ActiveTurn(
            backend,
            HintGate(self.pacing.hint_interval_s, self.pacing.hint_tool_calls),
            last_event=loop.time(),
        )
        self._active[agent_id] = active
        self.log.write("turn", agent=agent_id, turn=number, stage=stage)
        started = loop.time()
        record = _TurnRecord()

        async def consume() -> None:
            async with contextlib.aclosing(backend.run_turn(full_prompt, stage=stage)) as events:
                async for event in events:
                    events_log.log(event)
                    await self._on_event(handle, gate, active, record, event)

        active.consume = asyncio.create_task(consume())
        try:
            await active.consume
        except asyncio.CancelledError:
            if record.status is None:
                self._close_log(agent_id, record, TurnStatus.INTERRUPTED, "Turn abgebrochen.")
            outer = asyncio.current_task()
            if active.ending < 3 or (outer is not None and outer.cancelling()):
                raise  # the run stops; only arr's own hard end of the turn stays here
        except Exception as exc:
            logger.exception("Turn von %s fehlgeschlagen", agent_id)
            record.error = f"{type(exc).__name__}: {exc}"
            self._close_log(agent_id, record, TurnStatus.ERROR, record.error)
        finally:
            self._active.pop(agent_id, None)
            if active.delivery is not None and not active.delivery.done():
                active.delivery.cancel()  # the backend has already let the hint go
            self.room.clear_wait(agent_id, WaitKind.MESSAGES)
            self._waits.pop(agent_id, None)
            submitted = any(r.id > before for r in self.room.results(agent=agent_id))
            self._finish_status(handle, since=since, mark=mark, was_done=was_done, result=submitted)
            self._write_session(agent_id, backend)
            undelivered = self._unseen(agent_id, active.pending)
            for listener in list(self._listeners):
                listener.on_turn_end(agent_id, undelivered)
        if record.status is None:
            message = "Der Turn endete ohne Abschluss (turn_end fehlt)."
            record.error = record.error or message
            self._close_log(agent_id, record, TurnStatus.ERROR, message)
        status = record.status or TurnStatus.ERROR
        if status is TurnStatus.INTERRUPTED and self._limit is not None:
            status = TurnStatus.LIMIT
        results = [r for r in self.room.results(agent=agent_id) if r.id > before]
        self.log.write(
            "turn_end",
            agent=agent_id,
            turn=number,
            status=status.value,
            cost_usd=record.usage.cost_usd,
            results=[r.id for r in results],
        )
        return TurnOutcome(
            agent_id=agent_id,
            turn=number,
            status=status,
            text=record.text,
            usage=record.usage,
            results=results,
            session=record.session,
            num_turns=record.num_turns,
            duration_s=loop.time() - started,
            error=record.error,
        )

    async def _on_event(
        self,
        handle: AgentHandle,
        gate: ProfileGate,
        active: _ActiveTurn,
        record: _TurnRecord,
        event: AgentEvent,
    ) -> None:
        record.number = event.turn
        active.last_event = asyncio.get_running_loop().time()
        active.silent_warned = False
        data = event.data
        match event.kind:
            case EventKind.TOOL_CALL:
                active.tool_calls += 1
            case EventKind.TEXT:
                record.text = str(data.get("text", ""))
            case EventKind.ERROR:
                record.error = str(data.get("message", ""))
            case EventKind.RATE_LIMIT:
                reaction = gate.report(data)
                if reaction is not None:
                    self.log.write(
                        "rate_limit",
                        profile=gate.name,
                        agent=handle.id,
                        status=data.get("status"),
                        action=reaction[0],
                        until=reaction[1],
                    )
            case EventKind.TURN_END:
                record.status = TurnStatus(str(data.get("status", TurnStatus.ERROR)))
                tokens = {k: v for k, v in dict(data.get("usage") or {}).items() if k != "model"}
                record.usage = Usage(
                    **tokens, cost_usd=data.get("cost_usd"), cost_kind=data.get("cost_kind")
                )
                record.session = data.get("session")
                record.num_turns = data.get("num_turns")
                self.cost_usd += record.usage.cost_usd or 0.0
                budget = self.config.limits.budget_usd
                if budget is not None and self.cost_usd >= budget:
                    await self.halt(StopReason.LIMIT, LimitKind.BUDGET_USD, cost_usd=self.cost_usd)

    def _close_log(
        self, agent_id: str, record: _TurnRecord, status: TurnStatus, message: str
    ) -> None:
        """Log `error` + `turn_end` for a turn the backend did not close (docs/02 §6)."""
        record.status = status
        if record.number is None:
            return  # the turn never started: nothing to close in the transcript
        events_log = self.loggers[agent_id]
        for kind, data in (
            (EventKind.ERROR, ErrorData(message=message, type="orchestrator", recoverable=False)),
            (
                EventKind.TURN_END,
                TurnEndData(
                    status=status.value,
                    usage=Usage().tokens(),
                    cost_usd=None,
                    cost_kind=None,
                    session=None,
                    num_turns=None,
                ),
            ),
        ):
            events_log.log(AgentEvent(self.run_id, agent_id, record.number, kind, dict(data)))

    def _prompt_with_inbox(self, agent_id: str, prompt: str) -> str:
        view = self._views[agent_id]
        channels = [c for c in self.config.groupchats if agent_id in c.members]
        if not channels:
            return prompt
        inbox = view.inbox()
        user_messages = []
        for channel in channels:
            if channel.name in self._waiting:
                continue
            cursor = self.room.cursor(agent_id, channel.name)
            user_messages += [
                m for m in self.room.messages(channel.name, after_id=cursor) if m.sender == USER
            ]
        direct = [m for m in view.messages_for_me() if m.sender != ORCHESTRATOR]
        return render_turn_prompt(agent_id, prompt, inbox, user_messages, direct)

    def _set_status(self, handle: AgentHandle, status: AgentStatus, note: str = "") -> None:
        handle.status = status
        self.room.set_agent_status(handle.id, status, note)

    def _finish_status(
        self, handle: AgentHandle, *, since: float, mark: int, was_done: bool, result: bool
    ) -> None:
        """After a turn: `done`, `waiting` or `idle` (docs/09 §4.2, §4.3, §4.5).

        In this order: the turn quota is used up → `done` (`limit`, the run goes on: it stops
        only when an exhausted agent would get another turn, `_refuse`); the agent reported
        `working` in this turn → `waiting` after `wait_until`, else `idle`; it reported `done`
        → `done`; it called `wait_until` → `waiting`; it submitted a result (`submit_result`
        or a message `result`) and has no open question to another agent → `done`; it was
        `done` before the turn (woken by a message to it, L13) → `done` again; else `idle`.
        The status the agent reported comes from `agent_report`, so an approval in the same
        turn, which sets `blocked` and `working` in `agent_state`, cannot lose a `done` (Ist §1).

        An open question to the user does not keep it from `done` by a result (User 08.11.,
        W3-09.1): it worked on without the answer, so the question closes, in the room and in
        the inbox („hat ohne Antwort weitergearbeitet“); a later answer to it wakes it (L13).
        """
        agent_id = handle.id
        report = self.room.reports().get(agent_id)
        reported = report[0] if report is not None and report[1] >= since else None
        wait = self.room.waits(WaitKind.UNTIL).get(agent_id)
        long_wait = wait if wait is not None and wait.since >= since else None
        limit = self.config.limits.turns_per_agent
        reason = None
        if limit is not None and handle.turns_used >= limit:
            status, reason = AgentStatus.DONE, "limit"
            self._exhausted.add(agent_id)
        elif reported is AgentStatus.WORKING:
            status = AgentStatus.WAITING if long_wait else AgentStatus.IDLE
        elif reported is AgentStatus.DONE:
            status, reason = AgentStatus.DONE, "reported"
        elif long_wait is not None:
            status = AgentStatus.WAITING
        elif (result or self._posted_result(agent_id, mark)) and not [
            q for q in self.room.open_questions_by(agent_id) if not is_user_question(q)
        ]:
            status, reason = AgentStatus.DONE, "result"
            self._close_user_questions_of(agent_id, WORKED_ON.format(agent=agent_id), "result")
        elif was_done:
            status, reason = AgentStatus.DONE, "reported"
        else:
            status = AgentStatus.IDLE
        if status is AgentStatus.WAITING and long_wait is not None:
            self._start_long_wait(handle, long_wait)
            return
        self.room.clear_wait(agent_id, WaitKind.UNTIL)
        self._set_status(handle, status, note=reason or "")
        if reason is not None and not (was_done and reason == "reported"):
            self.log.write("agent_done", agent=agent_id, reason=reason, turn=handle.turns_used)

    def _posted_result(self, agent_id: str, mark: int) -> bool:
        return any(
            m.sender == agent_id and m.kind is MessageKind.RESULT
            for m in self.room.messages_after(mark)
        )

    def _start_long_wait(self, handle: AgentHandle, wait: WaitState) -> None:
        """`wait_until`: the agent waits with its turn ended, at most 30 min and never past
        `wall_time`; a discussion wakes it (`arr.runs.pacing`). With `until_result_from` the
        log says `result_from` and whose result (L14)."""
        until = min(wait.until, time.time() + WAIT_UNTIL_MAX_MIN * 60)
        if self.wall_end is not None:
            until = min(until, self.wall_end)
        mark = self._turn_marks.get(handle.id, 0)
        self._long_waits[handle.id] = LongWait(until, wait.file, wait.result_from, wait.since, mark)
        self._set_status(handle, AgentStatus.WAITING)
        if wait.result_from is not None:
            self.log.write(
                "waiting",
                agent=handle.id,
                reason="result_from",
                result_from=wait.result_from,
                until=until,
                file=wait.file,
            )
        else:
            self.log.write(
                "waiting", agent=handle.id, reason=WaitKind.UNTIL.value, until=until, file=wait.file
            )
        self.log.write("wake_scheduled", agent=handle.id, reason="timeout", due=until)

    def _end_long_wait(self, agent_id: str) -> None:
        self._long_waits.pop(agent_id, None)
        self.room.clear_wait(agent_id)

    def long_wait(self, agent_id: str) -> LongWait | None:
        """The `wait_until` of an agent whose turn has ended, None if it does not wait."""
        return self._long_waits.get(agent_id)

    def end_long_wait(self, agent_id: str) -> None:
        """End a `wait_until` early, e.g. before a wake-up turn or when no wake-up is left."""
        if self._long_waits.pop(agent_id, None) is not None:
            self.room.clear_wait(agent_id, WaitKind.UNTIL)
            handle = self._handles[agent_id]
            if handle.status is AgentStatus.WAITING and not self.is_busy(agent_id):
                self._set_status(handle, AgentStatus.IDLE)

    def until_file_exists(self, agent_id: str, rel: str) -> bool:
        """`until_file` of an agent is there (only the existence; links count as absent)."""
        try:
            return self._views[agent_id].until_file_exists(rel)
        except RoomError:
            return False

    def new_result(self, agent_id: str, since: float, after: int = 0) -> Result | Message | None:
        """The first result `agent_id` submitted after `since` (Unix time): `submit_result` or
        a message `result` with an ID above `after`, in any channel (L14); None if there is none
        yet. What of it the waiting agent may see decides `arr.runs.pacing`."""
        for result in self.room.results(agent=agent_id):
            if result.ts > since:
                return result
        for msg in self.room.messages_after(after):
            if msg.sender == agent_id and msg.kind is MessageKind.RESULT and msg.ts > since:
                return msg
        return None

    def view(self, agent_id: str) -> AgentRoom:
        """The room as the agent sees it (its channels, aliases)."""
        return self._views[agent_id]

    def turn_mark(self, agent_id: str) -> int:
        """Newest message ID when the agent's latest turn began (0 before its first turn)."""
        return self._turn_marks.get(agent_id, 0)

    def in_turn_wait(self, agent_id: str) -> WaitState | None:
        """The running `wait_for_messages` of an agent (as last seen by `poll`)."""
        return self._waits.get(agent_id)

    def exhausted(self, agent_id: str) -> bool:
        """True once the agent has used up `turns_per_agent` (it is `done`, reason `limit`)."""
        return agent_id in self._exhausted

    def waits_for_capacity(self, agent_id: str) -> bool:
        """True while a turn of the agent waits for a free place of its auth profile."""
        return agent_id in self._capacity

    def addressed_unread(self, agent_id: str, channel: str) -> int:
        """Unread messages in `channel` that concern the agent (`address`)."""
        cursor = self.room.cursor(agent_id, channel)
        return sum(
            1
            for m in self.room.messages(channel, after_id=cursor)
            if address(m, agent_id, self.sender_of) is not None
        )

    def _unseen(self, agent_id: str, messages: Sequence[Message]) -> list[Message]:
        """Messages the agent has not read yet (its cursor is below them)."""
        cursors: dict[str, int] = {}
        out = []
        for msg in messages:
            if msg.channel not in cursors:
                cursors[msg.channel] = self.room.cursor(agent_id, msg.channel)
            if msg.id > cursors[msg.channel]:
                out.append(msg)
        return out

    def _write_session(self, agent_id: str, backend: AgentBackend) -> None:
        try:
            ref = backend.session_ref()
        except Exception:
            logger.exception("session_ref von %s fehlgeschlagen", agent_id)
            return
        if ref:
            path = self.prepared.agents[agent_id].agent_dir / "session.json"
            path.write_text(json.dumps(ref, indent=2) + "\n", encoding="utf-8")

    # ----------------------------------------------------------------------------------------
    # RunContext: chat, results, checkpoints, escalation
    # ----------------------------------------------------------------------------------------

    async def post(
        self,
        channel: str,
        body: str,
        kind: MessageKind = MessageKind.NOTE,
        *,
        to: Sequence[str] = (),
        reply_to: int | None = None,
    ) -> int:
        """Post as `orchestrator` (also into waiting or closed channels); returns the ID.

        Messages of the orchestrator never wake agents; with `to` the addressees get a hint
        while they work, otherwise they see the message in their next inbox.
        """
        return self.room.post(channel, body, kind=kind, to=list(to), reply_to=reply_to).id

    def results(
        self,
        *,
        agent: str | None = None,
        stage: str | None = None,
        kind: str | None = None,
        attempt: int | None = None,
    ) -> list[Result]:
        """Submitted results matching all given filters, oldest first."""
        return [
            r
            for r in self.room.results(stage=stage, agent=agent)
            if (kind is None or r.kind == kind) and (attempt is None or r.attempt == attempt)
        ]

    def ratings(self, result_id: int) -> list[Rating]:
        """Ratings of one result, oldest first."""
        return self.room.ratings(result_id=result_id)

    async def checkpoint(self, stage: str, **info: Any) -> None:
        """Record a stage in `orchestrator.jsonl` (`{"kind": "checkpoint", "data": {stage, …}}`).

        `plan` with `stages=[…]` replaces the planned stages (the RunManager writes it at
        start). Any other stage becomes the current stage and opens every channel whose
        `open_from` is this stage or, in plan order, an earlier one. `info` must be JSON data.
        """
        self.log.write("checkpoint", stage=stage, **info)
        if stage == PLAN_STAGE:
            stages = info.get("stages")
            if isinstance(stages, list | tuple):
                self.plan = [str(s) for s in stages]
            return
        self._reach_stage(stage)

    def _reach_stage(self, stage: str) -> None:
        self.stage = stage
        for name in sorted(self._waiting):
            open_from = self.config.channel(name).open_from
            if open_from == stage or (
                open_from in self.plan
                and stage in self.plan
                and self.plan.index(stage) >= self.plan.index(open_from)
            ):
                self.room.open_channel(name)
                self._waiting.discard(name)
                self.log.write("channel_open", channel=name, stage=stage)

    async def escalate(
        self, stage: str, summary: str, options: Sequence[EscalationOption]
    ) -> EscalationOption:
        """Pause the run, ask the user in the approval inbox and wait (docs/07 §6).

        New turns wait meanwhile; running turns go on. Only the user can decide, once (E15).
        Afterwards the run continues (unless the user paused it as well). After a stop, or
        when the escalation becomes obsolete, the answer is `end`.
        """
        if self.stop_requested:
            return EscalationOption.END
        choices = [EscalationOption(o).value for o in options]
        self.log.write("escalation", stage=stage, summary=summary, options=choices)
        self.pause("escalation")
        try:
            decision = await self.inbox.ask_escalation(self.run_id, stage, summary, options)
        finally:
            self.resume("escalation")
        option = decision.option if decision is not None and decision.option else None
        chosen = option or EscalationOption.END
        self.log.write("escalation_decided", stage=stage, option=chosen.value, by_user=bool(option))
        return chosen

    # ----------------------------------------------------------------------------------------
    # Discussion phases (docs/06 §5)
    # ----------------------------------------------------------------------------------------

    async def discuss(
        self,
        channel: str,
        prompt: str | None = None,
        *,
        stage: str | None = None,
        rounds: int | None = None,
        context: Mapping[str, Any] | None = None,
        kickoff: bool = True,
    ) -> DiscussionOutcome:
        """Discussion phase as specified in `RunContext.discuss`; logs its start and end."""
        try:
            config = self.config.channel(channel)
        except KeyError:
            raise ArrError(f"Den Kanal „{channel}“ gibt es in diesem Run nicht.") from None
        if stage is not None:
            await self.checkpoint(stage, channel=channel, phase="discussion")
        if channel in self._waiting:
            raise ArrError(
                f"Der Kanal #{channel} ist noch geschlossen. Er öffnet ab Stufe "
                f"„{config.open_from}“."
            )
        self.log.write("discussion_start", channel=channel, mode=config.mode.value, stage=stage)
        outcome = await run_discussion(
            self, channel, prompt, stage=stage, rounds=rounds, context=context, kickoff=kickoff
        )
        self.log.write(
            "discussion_end",
            channel=channel,
            ended_by=outcome.ended_by.value,
            rounds=outcome.rounds,
            wakeups=outcome.wakeups,
            turns=len(outcome.outcomes),
        )
        return outcome

    # helpers for arr.runs.pacing

    def agent_status(self, agent_id: str) -> AgentStatus:
        """Current status of an agent (`done` once it reported so at the end of a turn)."""
        return self._handles[agent_id].status

    def is_busy(self, agent_id: str) -> bool:
        """True while the agent has a turn running or waiting to start."""
        return self._locks[agent_id].locked()

    def last_message_id(self) -> int:
        """Highest message ID in the room right now."""
        newer = self.room.messages_after(self._last_seen)
        return newer[-1].id if newer else self._last_seen

    def sender_of(self, msg_id: int) -> str | None:
        """Sender of a message (real ID), None if unknown."""
        if msg_id not in self._senders:
            with contextlib.suppress(ArrError):
                self._senders[msg_id] = self.room.message(msg_id).sender
        return self._senders.get(msg_id)

    def wakeups(self, channel: str, agent_id: str) -> int:
        """Automatic wake-ups of an agent in a channel so far."""
        return self._wakeups.get((channel, agent_id), 0)

    def count_wakeup(self, channel: str, agent_id: str) -> int:
        """Count a wake-up (also in `room.sqlite agent_state.wakeups`); returns the new count."""
        count = self._wakeups[(channel, agent_id)] = self.wakeups(channel, agent_id) + 1
        self.room.add_wakeup(agent_id)
        return count

    def mark_delivered(self, msg_ids: Sequence[int], status: DeliveryStatus, agent_id: str) -> None:
        """Set the delivery state of the `user` messages that reached `agent_id` (docs/08 §5.0).

        The state comes from the addressees: an agent the message is not for changes nothing,
        and a hint does not take back „geweckt“ of another addressee.
        """
        for msg_id in msg_ids:
            if self.sender_of(msg_id) != USER:
                continue
            msg = self.room.message(msg_id)
            if msg.to and agent_id not in msg.to:
                continue
            woken = msg.delivery is not None and msg.delivery.status is DeliveryStatus.WOKEN
            if status is not DeliveryStatus.HINT_MID_TURN or not woken:
                self.room.set_delivery(msg_id, status)

    def listen(self, listener: _Listener) -> None:
        """Register a discussion for new messages, turn ends and stops."""
        self._listeners.append(listener)

    def unlisten(self, listener: _Listener) -> None:
        """Remove a registered discussion."""
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    # ----------------------------------------------------------------------------------------
    # Watching the room: hints, blocked agents, delivery state
    # ----------------------------------------------------------------------------------------

    async def watch(self) -> None:
        """Poll the room until cancelled (the RunManager runs this as a task)."""
        while True:
            try:
                self.poll()
                await self.deliver_hints()
            except Exception:
                logger.exception("Beobachten des Gruppenchats fehlgeschlagen")
            await asyncio.sleep(self.pacing.poll_interval_s)

    def _recipients(self, msg: Message) -> list[str]:
        if msg.channel in self._waiting:
            return []
        if msg.sender == ORCHESTRATOR:
            return list(msg.to)
        members = self.config.channel(msg.channel).members
        return [m for m in members if m != msg.sender]

    def poll(self) -> None:
        """Take new messages: queue hints, tell discussions, set `user` delivery states."""
        for msg in self.room.messages_after(self._last_seen):
            self._last_seen = msg.id
            self._senders[msg.id] = msg.sender
            for agent_id in self._recipients(msg):
                if agent_id in self._active:
                    self._active[agent_id].pending.append(msg)
            if msg.sender == USER:
                self.room.set_delivery(msg.id, DeliveryStatus.NEXT_INBOX)
            if msg.sender != ORCHESTRATOR:
                for agent_id in self._recipients(msg):
                    if address(msg, agent_id, self.sender_of) is not None:
                        self.reset_until_streak(agent_id)  # L10: a message to it
            if is_user_question(msg) and msg.open and msg.sender in self._handles:
                self._open_user_question(msg)
            elif msg.sender == USER and self._questions:
                self._close_answered_questions(msg)
            for listener in list(self._listeners):
                listener.on_message(msg)
        self._sync_blocked()
        self._sync_waits()
        self._sync_yield()
        self._end_requested_turns()
        self._check_silence()

    def _open_user_question(self, msg: Message) -> None:
        """L16: a question of an agent to the user becomes an item „Frage“ in the inbox."""
        try:
            item = self.inbox.open_question(self.run_id, msg.sender, msg.channel, msg.id, msg.body)
        except Exception:  # the question stays in the channel; nothing else may fail
            logger.exception("Frage an den User (#%s) nicht in die Inbox gelegt", msg.id)
            return
        self._questions[msg.id] = item.id
        self.log.write(
            "user_question",
            agent=msg.sender,
            channel=msg.channel,
            msg=msg.id,
            item=item.id,
            state="open",
        )

    def _close_answered_questions(self, answer: Message) -> None:
        """The user wrote in the channel: the questions this message answers (`reply_to` on
        it, or to its asker; the room has closed them) are answered (L16). A note to nobody
        answers nothing, also when an answer follows in the same poll (W3-09.1)."""
        for msg_id, item_id in list(self._questions.items()):
            if answer.reply_to != msg_id and self.sender_of(msg_id) not in answer.to:
                continue
            del self._questions[msg_id]
            self.inbox.answered(item_id, answer.body, via="channel")  # False if in the inbox
            self.log.write(
                "user_question", msg=msg_id, item=item_id, state="answered", answer=answer.id
            )

    def close_user_questions(self, note: str) -> None:
        """The run ends: questions to the user nobody answered are closed (L16), in the inbox
        and in the room (nobody answers them any more)."""
        for msg_id, item_id in list(self._questions.items()):
            del self._questions[msg_id]
            self.room.close_question(msg_id)
            if self.inbox.close_question(item_id, note):
                self.log.write("user_question", msg=msg_id, item=item_id, state="closed")

    def _close_user_questions_of(self, agent_id: str, note: str, reason: str) -> None:
        """The agent worked on without the user's answer: its open questions to the user close
        in the room and, if they are items already, in the inbox with `note` (W3-09.1)."""
        for question in self.room.open_questions_by(agent_id):
            if not is_user_question(question):
                continue
            if question.id not in self._questions:  # asked and worked on before a poll
                self._open_user_question(question)
            self.room.close_question(question.id)
            item_id = self._questions.pop(question.id, None)
            if item_id is not None and self.inbox.close_question(item_id, note):
                self.log.write(
                    "user_question", msg=question.id, item=item_id, state="closed", reason=reason
                )

    def _sync_yield(self) -> None:
        """L12: ask working agents to free their place while another turn waits for one of
        their profile; `wait_for_messages` then returns at once (`arr.room.waiting`)."""
        wanted = {
            agent_id: gate.name
            for agent_id in self._active
            if (gate := self._gates[self._handles[agent_id].config.auth]).contended()
        }
        for agent_id in set(self._yielding) | set(wanted):
            if self._yielding.get(agent_id) != wanted.get(agent_id):
                self.room.set_wait_guard(agent_id, yield_to=wanted.get(agent_id))
        self._yielding = wanted

    def _end_requested_turns(self) -> None:
        """End the turns whose agent called `wait_for_messages` once too often (L7, L12).

        First with `interrupt()`; is the turn still running after `END_TURN_RETRY_S`, with a
        second one; after another `END_TURN_HARD_S` arr cancels the task that reads the
        backend's events, like the stop of a run does (W3-07.2 NB2). Each step is logged.
        """
        requests = {
            agent_id: guard.end_turn
            for agent_id, guard in self.room.wait_guards().items()
            if guard.end_turn is not None
        }
        now = asyncio.get_running_loop().time()
        for agent_id, reason in requests.items():
            active = self._active.get(agent_id)
            if active is None or active.ending >= 3:
                continue
            if active.ending == 0:
                self.log.write("end_turn", agent=agent_id, reason=reason)
            elif active.ending == 1 and now - active.ending_since >= END_TURN_RETRY_S:
                self.log.write("end_turn", agent=agent_id, reason=reason, attempt=2)
            elif active.ending == 2 and now - active.ending_since >= END_TURN_HARD_S:
                self.log.write("end_turn", agent=agent_id, reason=reason, hard=True)
                active.ending, active.ending_since = 3, now
                if active.consume is not None:
                    active.consume.cancel()  # like the stop of a run: the turn is cancelled
                continue
            else:
                continue
            active.ending, active.ending_since = active.ending + 1, now
            task = asyncio.get_running_loop().create_task(self._interrupt(agent_id, active.backend))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def count_until_timeout(self, agent_id: str) -> int:
        """A `wait_until` of the agent ended by its deadline: one more in a row (L10)."""
        count = self._until_streak.get(agent_id, 0) + 1
        self._until_streak[agent_id] = count
        self.room.set_wait_guard(agent_id, until_streak=count)
        return count

    def reset_until_streak(self, agent_id: str) -> None:
        """A message to the agent or its file came: `wait_until` is allowed again (L10)."""
        if self._until_streak.pop(agent_id, 0):
            self.room.set_wait_guard(agent_id, until_streak=0)

    def _sync_waits(self) -> None:
        """Log every new `wait_for_messages` of a working agent as `waiting` (docs/09 §4.6)."""
        waits = {
            agent: wait
            for agent, wait in self.room.waits(WaitKind.MESSAGES).items()
            if agent in self._active
        }
        for agent_id, wait in waits.items():
            known = self._waits.get(agent_id)
            if known is None or known.since != wait.since:
                self.log.write(
                    "waiting",
                    agent=agent_id,
                    reason=WaitKind.MESSAGES.value,
                    until=wait.until,
                    file=wait.file,
                )
        self._waits = waits

    def _check_silence(self) -> None:
        """Warn once per silence when a turn has no event for `turn_silence_warn_s` (L5)."""
        now = asyncio.get_running_loop().time()
        for agent_id, active in self._active.items():
            if active.blocked or active.silent_warned:
                continue
            silent = now - active.last_event
            if silent >= self.pacing.turn_silence_warn_s:
                active.silent_warned = True
                self.log.write(
                    "turn_silent",
                    agent=agent_id,
                    seconds=round(silent),
                    since=time.time() - silent,
                )

    def _sync_blocked(self) -> None:
        """`blocked` while a permission request of the agent waits (docs/06 §5.2)."""
        waiting = {
            item.agent_id for item in self.inbox.pending() if item.type is ApprovalType.PERMISSION
        }
        deadlines = {
            item.agent_id: item.deadline
            for item in self.inbox.pending()
            if item.type is ApprovalType.PERMISSION
        }
        for agent_id, active in list(self._active.items()):
            blocked = agent_id in waiting
            if blocked != active.blocked:
                active.blocked = blocked
                status = AgentStatus.BLOCKED if blocked else AgentStatus.WORKING
                self._set_status(self._handles[agent_id], status)
                if blocked:
                    self.log.write(
                        "waiting", agent=agent_id, reason="approval", until=deadlines.get(agent_id)
                    )

    async def deliver_hints(self) -> None:
        """Start the delivery of queued hints to working agents (docs/06 §4).

        `deliver_hint` of a backend may wait, with Claude until the next tool result (W2-01
        review B1). So every delivery runs as its own task, at most one per agent at a time,
        and neither the watching of the room nor the hints of other agents wait for it.
        """
        for agent_id, active in list(self._active.items()):
            if active.delivery is not None:
                if not active.delivery.done():
                    continue
                active.delivery = None
            if self.in_turn_wait(agent_id) is not None:
                continue  # `wait_for_messages` hands over what concerns the agent itself
            active.pending[:] = self._unseen(agent_id, active.pending)
            if not active.pending or active.blocked or active.tool_calls < active.retry_at_calls:
                continue
            urgent = any(is_question_to(m, agent_id) for m in active.pending)
            if not active.gate.allows(active.tool_calls, urgent=urgent):
                continue
            messages = list(active.pending)
            active.delivery = asyncio.create_task(self._deliver(agent_id, active, messages))

    async def _deliver(self, agent_id: str, active: _ActiveTurn, messages: list[Message]) -> None:
        text = compose_hint(agent_id, messages)
        try:
            delivered = await active.backend.deliver_hint(text)
        except Exception:
            logger.exception("Hinweis an %s fehlgeschlagen", agent_id)
            delivered = False
        if self._active.get(agent_id) is not active:
            return  # the turn ended meanwhile; the messages are in the next inbox
        if not delivered:
            active.retry_at_calls = active.tool_calls + 1
            return
        active.gate.delivered(active.tool_calls)
        del active.pending[: len(messages)]
        ids = [m.id for m in messages]
        self.log.write("hint", agent=agent_id, text=text, messages=ids)
        self.mark_delivered(ids, DeliveryStatus.HINT_MID_TURN, agent_id)

    # ----------------------------------------------------------------------------------------
    # Stop, limits, pause
    # ----------------------------------------------------------------------------------------

    def _start_halt(self, reason: StopReason, limit: LimitKind | None = None, **info: Any) -> None:
        """Like `halt`, for sync code: the stop counts at once, the rest runs as a task."""
        if self._stop_reason is not None:
            return
        self._stop_reason, self._limit = reason, limit
        self._halt_task = asyncio.get_running_loop().create_task(self._halt(info))

    async def wait_halted(self) -> None:
        """Wait for a halt started by `_start_halt` (a limit found in sync code) to finish."""
        task = self._halt_task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await task

    async def halt(self, reason: StopReason, limit: LimitKind | None = None, **info: Any) -> None:
        """Stop the run: cancel open approvals, then interrupt all running turns at once.

        See docs/01 §6. Each `interrupt()` has its own deadline of `INTERRUPT_TIMEOUT_S`.

        After that `stop_requested` is true and every further turn returns at once (status
        `interrupted` or `limit`). Pause waiters, profile waiters and discussions are released.
        Safe to call more than once; only the first call counts.
        """
        if self._stop_reason is not None:
            return
        self._stop_reason, self._limit = reason, limit
        await self._halt(info)

    async def _halt(self, info: Mapping[str, Any]) -> None:
        if self._limit is not None:
            self.log.write("limit", limit=self._limit.value, **info)
            note = _LIMIT_TEXT[self._limit]
        else:
            self.log.write("stop", reason=StopReason.USER.value, **info)
            note = "Der Run wurde gestoppt."
        self.inbox.cancel_all(note)  # before interrupt(): Codex ignores it while a handler waits
        self._runnable.set()
        self.halted.set()
        for gate in self._gates.values():
            gate.kick()
        for listener in list(self._listeners):
            listener.kick()
        # All at once, each with its own deadline: the stop must not grow with the agents.
        await asyncio.gather(
            *(self._interrupt(agent_id, a.backend) for agent_id, a in self._active.items())
        )

    @staticmethod
    async def _interrupt(agent_id: str, backend: AgentBackend) -> None:
        try:
            await asyncio.wait_for(backend.interrupt(), INTERRUPT_TIMEOUT_S)
        except Exception:
            logger.exception("interrupt() von %s fehlgeschlagen", agent_id)

    def pause(self, reason: str = "user") -> None:
        """Let new turns wait (running turns go on); `reason` `user` or `escalation`."""
        if self.stop_requested or reason in self._pause_reasons:
            return
        self._pause_reasons.add(reason)
        self.log.write("pause", reason=reason)
        if len(self._pause_reasons) == 1:
            self._runnable.clear()
            if self._on_pause is not None:
                self._on_pause(True)

    def resume(self, reason: str = "user") -> None:
        """Lift a pause of this `reason`; the run goes on when no pause is left."""
        if reason not in self._pause_reasons:
            return
        self._pause_reasons.discard(reason)
        self.log.write("resume", reason=reason)
        if not self._pause_reasons:
            self._runnable.set()
            if self._on_pause is not None and not self.stop_requested:
                self._on_pause(False)

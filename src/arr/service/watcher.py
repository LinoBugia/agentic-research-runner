"""The observer of the run folders: new lines and rows become live events (W3-01 §3.4).

Every `interval_s` (0.25 s) the watcher looks at the runs of the workfolder. Runs that are
over cost nothing but a look at the folder list and, every `full_every_s`, a `stat` of their
`meta.json`. For active runs it reads what has grown since the last tick: `meta.json`
(status → `run.status`, `counts.changed`), `orchestrator.jsonl` (checkpoints →
`stage.checkpoint`, turns → `agent.state`), `approvals.jsonl` (`approval.*`), every
`transcript.jsonl` (`agent.event`, `agent.activity`), and `room.sqlite` by the newest IDs
(`channel.message`, `result.submitted`, `rating.submitted`, agent rows → `agent.state`).
Running runs also get a `usage.tick` every second and a `limit.warning` once per limit at 80 %.

Half-written lines wait for their newline, truncated and unreadable files are tolerated
(`JsonlTail`, `TranscriptIndex`), a failing tick is logged and the next one runs. The watcher
shares the `RunStore` with the API methods, so both see the same files; it keeps its own
cursors (lengths and IDs) and never consumes what the API needs. Latency from the write to the
event stays below one second (docs/08 §6.6).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from arr.errors import ArrError
from arr.models import RunStatus
from arr.permissions.types import ApprovalStatus
from arr.service.api_models import (
    AgentActivityEvent,
    AgentState,
    AgentStateEvent,
    AgentTranscriptEvent,
    ApprovalEvent,
    ChannelMessageEvent,
    CountsChangedEvent,
    LimitKind,
    LimitWarningEvent,
    RatingSubmittedEvent,
    ResultSubmittedEvent,
    RunStatusEvent,
    StageCheckpointEvent,
    UsageTickEvent,
)
from arr.service.real_api.approvals import ApprovalReader
from arr.service.real_api.base import ServiceState
from arr.service.real_api.rundir import (
    RunRecord,
    _activity,
    _limits,
    agent_states,
    run_results,
    run_summary,
    run_usage,
    stage_infos,
    to_message,
    transcript_event,
)
from arr.service.real_api.transcript import TOOL_RESULT
from arr.service.real_api.workfolder import counts

log = logging.getLogger("arr.service.watcher")

INTERVAL_S = 0.25
FULL_EVERY_S = 5.0
TICK_EVERY_S = 1.0
WARN_RATIO = 0.8
_AGENT_KINDS = frozenset(
    {"turn", "turn_end", "rate_limit", "wake", "hint", "waiting", "agent_done", "turn_silent"}
    | {"wait_end", "end_turn"}
)
"""Log kinds that change an agent card (docs/09 §4.6: waits, done, silent turns)."""
_RUN_KINDS = frozenset(
    {"limit", "stop", "pause", "resume", "error", "round", "discussion_start", "discussion_end"}
    | {"secret_scan"}
)
"""Log kinds that change the head of the run detail or the run in lists."""
APPROVAL_EVENTS = {
    "open": "approval.created",
    "decision": "approval.updated",
    "decided": "approval.decided",
    "expired": "approval.expired",
    "obsolete": "approval.expired",
    "revoked": "approval.revoked",
}


@dataclass
class _Seen:
    """Cursors of one run: what the watcher has already turned into events."""

    status: RunStatus
    active: bool
    log_len: int = 0
    approvals_len: int = 0
    own_len: int = 0
    """Cursor in the item lines in memory of a run of this service (`RunRecord.item_lines`)."""
    open_ids: set[str] = field(default_factory=set)
    counts_dirty: bool = False
    transcript_lines: dict[str, int] = field(default_factory=dict)
    msg_id: int = 0
    result_id: int = 0
    rating_id: int = 0
    agent_rows: dict[str, tuple[str, float, int]] = field(default_factory=dict)
    last_tick: float = 0.0
    warned: set[LimitKind] = field(default_factory=set)


class RunWatcher:
    """Turns changes in the run folders into `LiveEvent`s on the hub (see the module docstring)."""

    def __init__(
        self,
        state: ServiceState,
        *,
        interval_s: float = INTERVAL_S,
        full_every_s: float = FULL_EVERY_S,
        tick_every_s: float = TICK_EVERY_S,
    ) -> None:
        self.state = state
        self.approvals = ApprovalReader(state)
        self.interval_s = interval_s
        self.full_every_s = full_every_s
        self.tick_every_s = tick_every_s
        self._seen: dict[str, _Seen] = {}
        self._primed = False
        self._last_full = 0.0
        self.published = 0

    async def run(self) -> None:
        """Tick until cancelled."""
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("Beobachter: ein Durchlauf ist fehlgeschlagen")
            await asyncio.sleep(self.interval_s)

    # -- one pass ---------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> int:
        """One pass over the runs; returns the number of events published."""
        now = time.time() if now is None else now
        before = self.published
        full = not self._primed or now - self._last_full >= self.full_every_s
        records = self.state.store.scan(refresh=full)
        if full:
            self._last_full = now
        ids = {r.run_id for r in records}
        for run_id in [r for r in self._seen if r not in ids]:
            del self._seen[run_id]
        counts_dirty = False
        for record in records:
            seen = self._seen.get(record.run_id)
            if seen is None:
                if not self._primed:
                    self._baseline(record)  # what exists at start is not news
                    continue
                # a run that appeared while the service runs: everything in it is news
                record.refresh()
                seen = self._seen[record.run_id] = _Seen(status=record.status, active=True)
                self._observe(record, seen, now, previous=None, force_status=True)
                seen.active = record.status.is_active
                counts_dirty = True
                continue
            if not (seen.active or record.status.is_active):
                if full and record.status is not seen.status:
                    self._publish_status(record, seen.status)
                    seen.status = record.status
                    counts_dirty = True
                continue
            if not full:
                record.refresh_meta()
            previous = seen.status if record.status is not seen.status else None
            self._observe(record, seen, now, previous=previous, force_status=previous is not None)
            if previous is not None:
                counts_dirty = True
                seen.status = record.status
            # a finished run is followed until it settles: the secret scan comes after it (U3)
            seen.active = record.status.is_active or not record.settled
            counts_dirty = counts_dirty or seen.counts_dirty
            seen.counts_dirty = False
        if counts_dirty:
            self._emit(CountsChangedEvent, run_id=None, counts=counts(self.state))
        self._primed = True
        return self.published - before

    def _baseline(self, record: RunRecord) -> _Seen:
        """Take note of what a run already holds, without events for it."""
        record.refresh()
        seen = _Seen(status=record.status, active=record.status.is_active)
        seen.log_len = len(record.log.entries)
        seen.approvals_len = len(record.approvals.entries)
        if seen.active:
            seen.open_ids = self._open_ids(record)
        seen.transcript_lines = {a: len(r.transcript.lines) for a, r in record.agents.items()}
        room = record.room
        if room.available:
            seen.msg_id = room.max_message_id()
            seen.result_id = room.max_result_id()
            seen.rating_id = room.max_rating_id()
            seen.agent_rows = self._agent_rows(record)
        self._seen[record.run_id] = seen
        return seen

    @staticmethod
    def _agent_rows(record: RunRecord) -> dict[str, tuple[str, float, int]]:
        store = record.room.store
        if store is None:
            return {}
        try:
            return {s.agent: (s.status.value, s.ts, s.wakeups) for s in store.agent_states()}
        except ArrError:
            return {}

    # -- events of one active run -----------------------------------------------------------

    def _observe(
        self,
        record: RunRecord,
        seen: _Seen,
        now: float,
        *,
        previous: RunStatus | None,
        force_status: bool,
    ) -> None:
        """Events for what an active run gained since the cursors in `seen`."""
        record.refresh()
        agents_dirty: set[str] = set()
        run_dirty = force_status
        resolved = record.load_resolved()

        # orchestrator.jsonl
        entries = record.log.entries
        if len(entries) < seen.log_len:
            seen.log_len = 0
        new_entries = entries[seen.log_len :]
        seen.log_len = len(entries)
        for entry in new_entries:
            kind = entry.get("kind")
            data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
            if kind == "checkpoint" and data.get("stage") not in (None, "plan"):
                for stage in stage_infos(record, resolved):
                    if stage.key == data.get("stage"):
                        self._emit(StageCheckpointEvent, run_id=record.run_id, stage=stage)
                run_dirty = True
            elif kind in _AGENT_KINDS:
                agent = data.get("agent")
                if isinstance(agent, str):
                    agents_dirty.add(agent)
                if kind == "turn_end":
                    run_dirty = True
                if kind == "waiting" and data.get("channel"):
                    run_dirty = True  # the state of the discussion in the head (docs/09 §4.6)
                    channels = {c.name: c for c in resolved.groupchats} if resolved else {}
                    if data["channel"] in channels:  # idle members show the next round
                        agents_dirty.update(channels[data["channel"]].members)
            elif kind in _RUN_KINDS:
                run_dirty = True

        # approvals.jsonl
        approvals = record.approvals.entries
        new_approvals: list[dict[str, Any]] = []
        if len(approvals) < seen.approvals_len:
            seen.approvals_len = 0
        if len(approvals) != seen.approvals_len:
            new_approvals = approvals[seen.approvals_len :]
            for entry in new_approvals:
                request = entry.get("request")
                if isinstance(request, dict) and isinstance(request.get("agent_id"), str):
                    agents_dirty.add(request["agent_id"])
            seen.approvals_len = len(approvals)
            run_dirty = True
        own = record.item_lines()
        if own is not None:  # the file of a run of this service is no source of events
            new_approvals = own[seen.own_len :]
            seen.own_len = len(own)
        if (new_approvals or seen.open_ids) and self._approval_events(record, seen, new_approvals):
            run_dirty = True

        # transcripts
        for agent_id, agent in record.agents.items():
            lines = agent.transcript.lines
            start = seen.transcript_lines.get(agent_id, 0)
            if len(lines) < start:
                start = 0
            if len(lines) == start:
                continue
            for index in range(start, len(lines)):
                line = lines[index]
                target = index
                if line.kind == TOOL_RESULT:
                    call = agent.transcript.call_index(line)
                    if call is None:
                        continue
                    target = call
                event = transcript_event(record, agent_id, target)
                if event is not None:
                    self._emit(
                        AgentTranscriptEvent, run_id=record.run_id, agent_id=agent_id, event=event
                    )
            seen.transcript_lines[agent_id] = len(lines)
            activity = _activity(agent, final=record.final)
            if activity is not None:
                self._emit(
                    AgentActivityEvent, run_id=record.run_id, agent_id=agent_id, activity=activity
                )
            marks = ("turn_start", "turn_end", "error")
            if any(lines[i].kind in marks for i in range(start, len(lines))):
                agents_dirty.add(agent_id)

        # room
        room = record.room
        if room.available and room.store is not None:
            store = room.store
            max_msg = room.max_message_id()
            if max_msg > seen.msg_id:
                try:
                    for msg in store.messages_after(seen.msg_id):
                        self._emit(
                            ChannelMessageEvent, run_id=record.run_id, message=to_message(msg)
                        )
                except ArrError:
                    pass
                seen.msg_id = max_msg
                run_dirty = True
            max_result = room.max_result_id()
            max_rating = room.max_rating_id()
            if max_result > seen.result_id or max_rating > seen.rating_id:
                results = run_results(record, resolved).items
                by_id = {r.id: r for r in results}
                for result in results:
                    if result.id > seen.result_id:
                        self._emit(ResultSubmittedEvent, run_id=record.run_id, result=result)
                if max_rating > seen.rating_id:
                    try:
                        ratings = store.ratings()
                    except ArrError:
                        ratings = []
                    for rating in ratings:
                        if rating.id <= seen.rating_id:
                            continue
                        result = by_id.get(rating.result_id)
                        if result is None:
                            continue
                        info = next((r for r in result.ratings if r.rater == rating.rater), None)
                        if info is None:
                            continue
                        self._emit(
                            RatingSubmittedEvent,
                            run_id=record.run_id,
                            result_id=result.id,
                            rating=info,
                            avg_score=result.avg_score,
                            status=result.status,
                        )
                seen.result_id, seen.rating_id = max_result, max_rating
                agents_dirty.update(r.agent for r in results)
            rows = self._agent_rows(record)
            for agent_id, row in rows.items():
                if seen.agent_rows.get(agent_id) != row:
                    agents_dirty.add(agent_id)
            seen.agent_rows = rows

        states: list[AgentState] | None = None
        if agents_dirty or run_dirty:
            states = agent_states(record, resolved)
        if agents_dirty and states is not None:
            for state in states:
                if state.id in agents_dirty:
                    self._emit(AgentStateEvent, run_id=record.run_id, agent=state)
        if run_dirty:
            self._publish_status(record, previous, states=states)

        # usage tick and limit warnings of running runs
        if record.status is RunStatus.RUNNING and now - seen.last_tick >= self.tick_every_s:
            seen.last_tick = now
            limits = _limits(record, record.peek(), now)
            tokens, cost, kind = run_usage(record)
            self._emit(
                UsageTickEvent,
                run_id=record.run_id,
                elapsed_s=limits.elapsed_s,
                spent_usd=limits.spent_usd,
                cost_kind=kind if limits.spent_usd is not None else None,
                tokens_total=tokens,
            )
            self._warn_limits(record, seen, limits)

    def _open_ids(self, record: RunRecord) -> set[str]:
        items = self.approvals.items(record)
        return {i.id for i in items.values() if i.status is ApprovalStatus.OPEN}

    def _approval_events(
        self, record: RunRecord, seen: _Seen, entries: list[dict[str, Any]]
    ) -> bool:
        """`approval.*` for new lines, and for open items of a run that died without a line."""
        items = self.approvals.items(record)
        emitted = False
        told: set[str] = set()
        for entry in entries:
            item = items.get(str(entry.get("item_id")))
            kind = APPROVAL_EVENTS.get(str(entry.get("event")))
            if item is None or kind is None:
                continue
            still_open = item.status is ApprovalStatus.OPEN
            if kind in ("approval.created", "approval.updated") and not still_open:
                continue  # opened and closed within one tick: only the closing counts
            self._emit(ApprovalEvent, run_id=record.run_id, type=kind, item=item)
            told.add(item.id)
            emitted = True
        open_now = {i.id for i in items.values() if i.status is ApprovalStatus.OPEN}
        for item_id in seen.open_ids - open_now - told:
            if item_id in items:
                self._emit(
                    ApprovalEvent,
                    run_id=record.run_id,
                    type="approval.expired",
                    item=items[item_id],
                )
                emitted = True
        seen.open_ids = open_now
        seen.counts_dirty = seen.counts_dirty or emitted
        return emitted

    def _warn_limits(self, record: RunRecord, seen: _Seen, limits: Any) -> None:
        checks = [
            (LimitKind.BUDGET, limits.spent_usd, limits.budget_usd),
            (LimitKind.TURNS, limits.max_turns_used, limits.turns_per_agent),
            (LimitKind.WALL_TIME, limits.elapsed_s, limits.wall_time_s),
        ]
        for kind, used, limit in checks:
            if kind in seen.warned or not limit or used is None:
                continue
            ratio = used / limit
            if ratio >= WARN_RATIO:
                seen.warned.add(kind)
                self._emit(
                    LimitWarningEvent,
                    run_id=record.run_id,
                    limit=kind,
                    used=float(used),
                    limit_value=float(limit),
                    ratio=ratio,
                )

    def _publish_status(
        self,
        record: RunRecord,
        previous: RunStatus | None,
        *,
        states: list[AgentState] | None = None,
    ) -> None:
        try:
            summary = run_summary(
                record,
                base_url=self.state.base_url,
                now=time.time(),
                agents=states if states is not None else agent_states(record),
            )
        except (ArrError, ValueError):
            return
        self._emit(RunStatusEvent, run_id=record.run_id, run=summary, previous=previous)

    def _emit(self, cls: type[Any], *, run_id: str | None, **fields: Any) -> None:
        event = cls(seq=self.state.hub.next_seq(), ts=time.time(), run_id=run_id, **fields)
        self.state.hub.publish(event)
        self.published += 1

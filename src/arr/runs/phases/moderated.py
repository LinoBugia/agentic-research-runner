"""One pass of a phase `moderated` (W4b-04; docs/10 §4.5, Entscheidungen 10, 12, 79).

- **The lead** is the one agent with an action (`pass_.slots == [lead]`, with `context` and
  `mark`): gate, rating, `book`, feedback and the inputs of later phases read only its
  submission. Its first turn shows its assignment (`render_assignment`), then like
  `PhaseRunner.act` at most `turns` turns without a submission and one follow-up question,
  `runner.wait` after `wait_until`. **While assignments run or wait** no counted turn begins
  after a turn of the lead: the runner waits for the next return, a direct message to the lead
  (`waker.take_message`) or the stop. A return gives a return turn that does not count against
  `turns` (one for all returns that came meanwhile, at most one per assignment); a direct
  message gives a counted turn with the reason „message“ while `turns` allows, else the lead
  waits on. Only when nothing runs and no return is open, continuation and follow-up apply
  again. Before every turn the lead's package is built anew (`assignments`, `assign_rules`).
- **Assignments** are read from the table `delegation` (`RunContext.delegations`) every
  `runner.WAIT_POLL_S`, also during a turn of the lead, and run per recipient one after the
  other in the order of their number (Entscheidung 10), different recipients at the same time.
  One assignment is a `Slot` with the action `assign-<number>` and a `ResolvedTask` built here
  (`task` = arr's frame, `submission` = the format, `turns` = `DEFAULT_TURNS`). Before it:
  the recipient leaves the sleepers of the waker (`Waker.take_out`: a running wake-up turn ends
  first, a due one lapses), `runner.transition` on a normal entry (for every assignment, never
  before the agent's very first action), `enter_phase`, the package. Its turns are those of
  `PhaseRunner.act` with an `abort` that reads the table (a cancelled one gets no further turn).
  Without a submission and with an open question of the recipient to the user, B3 holds for it
  (`hold` with the same `abort`) before it counts as `missing`. Then `returned`/`missing`, a
  return for the lead, and the recipient goes back to the sleepers (`Waker.put_back`) when its
  queue is empty.
- **The end of the phase**: an accepted submission of the lead, or its turns and the follow-up
  are used up and nothing runs, or the stop. Has the lead then an open question to the user,
  the moderation does not close yet (W4b-04.1 H1, Entscheidung 82): it holds by the rule of B3,
  the answer gives the lead a turn, and assignments from it run as usual. Then the pass is
  closed in the room (`cancel_delegations`: `assign` refuses from now on) and every queued and
  running assignment cancelled (a running turn ends through `RunContext.end_turn`), the
  waking ends, the runner waits for the end of the turns of the assignments, and
  `runner.after_actions` holds B3 for the lead. The second `submit` of the lead
  (`AgentRoom.submit`) cancels in the room itself.
- Names: the lead sees every recipient as `inputs.shown` shows it (an alias where there is
  one), in its package, the return turns and every answer of `assign` (W4b-04.1 M1). The
  packages keep their limit (the list of the lead first, then `fit_package`; the returned ones
  are cut with a note, N3, NP-N1).

Log entries (all with `stage` and `attempt`; the text of an assignment is never logged):
`assign {number, lead, agent, format}` (seen in the table), `assign_start {number, agent}`,
`assign_end {number, agent, status, submission, turns}` (also for a queued one that was
cancelled, then `turns: 0`), `submit_held {agent, named}` (the numbers arr named to the lead at
a refused `submit`), and from the waker `wake_skipped {reason: assignment, messages}`.
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from arr.backends.base import TurnStatus
from arr.models import DEFAULT_TURNS, ResolvedPhase, ResolvedSubmission, ResolvedTask
from arr.plan.read import ACTION_WORK
from arr.room.store import ASSIGN_FORMATS
from arr.room.types import Delegation, DelegationStatus, Submission
from arr.runs.phases.inputs import item, shown
from arr.runs.phases.state import HOW_NORMAL, Pass, Slot
from arr.runs.phases.text import (
    MAX_INBOX_LINE,
    MAX_PACKAGE_CHARS,
    fit_package,
    line,
    package_size,
)
from arr.runs.phases.view import (
    ask_again_prompt,
    assignment_frame,
    continue_prompt,
    render_assignment,
    return_prompt,
)
from arr.runs.phases.wake import hold
from arr.submissions.formats import BUILTIN

if TYPE_CHECKING:
    from arr.runs.phases.runner import PhaseRunner

__all__ = ["END_REASON", "run_pass"]

logger = logging.getLogger(__name__)

MAX_RETURN_CHARS = 20_000
"""The returned assignments of one return turn as JSON (W4b-04.1, N3): above it the summaries
are cut to one inbox line, files are dropped, then the oldest entries; the turn says so."""

END_REASON = "assignment_cancelled"
"""`wait_guard.end_turn` (log `end_turn`) when arr ends the turn of a cancelled assignment."""


def _poll_s() -> float:
    from arr.runs.phases import runner  # late: the runner imports this module

    return runner.WAIT_POLL_S


async def run_pass(runner: "PhaseRunner", pass_: Pass) -> None:
    """Run one pass of a phase `moderated` (see the module docstring)."""
    phase = runner.ctx.config.phase(pass_.phase)
    action = phase.actions[0]  # `check`: exactly one agent with an action, the lead
    lead = Slot(action.agent, ACTION_WORK, action, runner.task_text(phase, action))
    pass_.slots = [lead]
    await _Moderation(runner, phase, pass_, lead).run()


class _Moderation:
    """The state of one pass of a phase `moderated`."""

    def __init__(
        self, runner: "PhaseRunner", phase: ResolvedPhase, pass_: Pass, lead: Slot
    ) -> None:
        self.runner = runner
        self.ctx = runner.ctx
        self.phase = phase
        self.pass_ = pass_
        self.lead = lead
        self.recipients = [a for a in phase.standby if a not in phase.agents]
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.current: dict[str, int] = {}
        """Recipient → the assignment it works on now (between start and end)."""
        self.seen: set[int] = set()
        self.named: set[int] = set()
        self.logged_end: set[int] = set()
        self.returns: list[int] = []
        """Assignments back (returned or missing) that no turn of the lead has shown yet."""
        self.returned = asyncio.Event()
        self.closing = False
        self.error: BaseException | None = None
        self.mark = 0

    # -- the pass ----------------------------------------------------------------------------

    async def run(self) -> None:
        ctx, runner, phase, pass_, lead = self.ctx, self.runner, self.phase, self.pass_, self.lead
        if ctx.stop_requested:
            return
        if pass_.how == HOW_NORMAL:
            await runner.transition(lead.agent, phase, pass_.attempt)
        await ctx.enter_phase(phase.name, pass_.attempt, [lead.agent])
        if ctx.stop_requested:
            return
        poller = asyncio.get_running_loop().create_task(self._poll())
        try:
            await self._lead_turns()
            await self._close()
            self._raise()
            if not ctx.stop_requested:
                await runner.after_actions(phase, pass_)
        finally:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller
            for task in self.workers.values():
                if not task.done():
                    task.cancel()
            for task in list(self.workers.values()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):  # nothing stays open after the pass
                self._cancel_open()

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    # -- the table ---------------------------------------------------------------------------

    def _rows(self, agent: str | None = None) -> list[Delegation]:
        return self.ctx.delegations(stage=self.phase.name, attempt=self.pass_.attempt, agent=agent)

    def _log(self, kind: str, **data: Any) -> None:
        log = getattr(self.ctx, "log", None)
        if log is not None:
            log.write(kind, stage=self.phase.name, attempt=self.pass_.attempt, **data)

    def _sync(self) -> list[Delegation]:
        """Read the assignments of the pass: log new ones and the ones named to the lead, end
        the turn of a cancelled running one, start a worker for every recipient with a queued
        one (not while the pass closes)."""
        rows = self._rows()
        for d in rows:
            if d.id not in self.seen:
                self.seen.add(d.id)
                self._log("assign", number=d.number, lead=d.lead, agent=d.agent, format=d.format)
        named = [d.number for d in rows if d.named and d.id not in self.named]
        if named:
            self.named.update(d.id for d in rows if d.named)
            self._log("submit_held", agent=self.lead.agent, named=named)
        for d in rows:
            if d.status is not DelegationStatus.CANCELLED:
                continue
            if self.current.get(d.agent) == d.id:
                self.ctx.end_turn(d.agent, END_REASON)
            elif d.started is None and d.id not in self.logged_end:
                self.logged_end.add(d.id)
                self._log(
                    "assign_end",
                    number=d.number,
                    agent=d.agent,
                    status=d.status.value,
                    submission=None,
                    turns=0,
                )
        if not self.closing and not self.ctx.stop_requested:
            queued = [d.agent for d in rows if d.status is DelegationStatus.QUEUED]
            for agent in dict.fromkeys(queued):
                task = self.workers.get(agent)
                if task is None or task.done():
                    loop = asyncio.get_running_loop()
                    self.workers[agent] = loop.create_task(self._work(agent))
        return rows

    async def _poll(self) -> None:
        while True:
            try:
                self._sync()
            except Exception:  # a read error ends nothing; the next poll tries again
                logger.exception("Aufträge der Phase %s nicht gelesen", self.phase.name)
            await asyncio.sleep(_poll_s())

    def _cancel_open(self) -> None:
        """Cancel every queued and running assignment of the pass and log it (idempotent)."""
        self.ctx.cancel_delegations(self.phase.name, self.pass_.attempt)
        self._sync()

    def _cancelled(self, delegation_id: int) -> bool:
        found = self.ctx.delegation(delegation_id)
        return found is None or found.status is DelegationStatus.CANCELLED

    async def _close(self) -> None:
        """The end of the phase: cancel what is open, no more wake-ups, wait for the end of
        the turns of the assignments (ended through `end_turn`), not longer."""
        self.closing = True
        self._cancel_open()
        waker = self.runner.waker
        if waker is not None:
            await waker.end_actions()
        while True:
            running = [t for t in self.workers.values() if not t.done()]
            if not running:
                return
            for agent, delegation_id in list(self.current.items()):
                if self._cancelled(delegation_id):
                    self.ctx.end_turn(agent, END_REASON)  # again: a turn that began late
            await asyncio.wait(running, timeout=_poll_s())

    # -- the lead ----------------------------------------------------------------------------

    def _submission(self, d: Delegation) -> Submission | None:
        if d.submission_id is None:
            return None
        found = self.ctx.submissions(
            agent=d.agent, stage=d.stage, attempt=d.attempt, action=d.action
        )
        return next((s for s in found if s.id == d.submission_id), None)

    def _listing(self, d: Delegation) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "number": d.number,
            "agent": shown(self.ctx, self.lead.agent, d.agent),
            "format": d.format,
            "status": d.status.value,
        }
        sub = self._submission(d)
        if sub is not None:
            entry["result"] = sub.status
            entry["summary"] = line(sub.summary, MAX_INBOX_LINE)
            entry["path"] = str(self.ctx.run_folder / sub.path)
        return entry

    def _formats(self) -> list[str]:
        own = [f for f in self.ctx.config.formats if f not in BUILTIN]
        return [*ASSIGN_FORMATS, *own]

    def _lead_package(self, rows: list[Delegation]) -> dict[str, Any]:
        lead = self.lead
        package = self.runner.package(self.phase, self.pass_, lead, repetition=1, mark=self.mark)
        package["assignments"] = [self._listing(d) for d in rows]
        package["assign_rules"] = {
            "max_assignments": self.phase.max_assignments,
            "given": len(rows),
            "standby": [shown(self.ctx, lead.agent, a) for a in self.recipients],
            "formats": self._formats(),
        }
        return self._fit_lead(package)

    @staticmethod
    def _fit_lead(package: dict[str, Any]) -> dict[str, Any]:
        """Keep the lead's package within `MAX_PACKAGE_CHARS` (W4b-04.1 N3, order W4b-04.2
        NP-N1): first the lead's own list, its summaries (copies of what lies in the result
        folder, the path stays), then entries from its end, each marked `assignments_cut`;
        only then the cuts of `fit_package` (inputs, feedback, peers, objections)."""
        items = package["assignments"]
        for entry in items:
            if package_size(package) <= MAX_PACKAGE_CHARS:
                break
            if entry.pop("summary", None) is not None:
                package["assignments_cut"] = True
        while package_size(package) > MAX_PACKAGE_CHARS and items:
            items.pop()
            package["assignments_cut"] = True
        return fit_package(package)

    def _returned(self, ids: list[int], rows: list[Delegation]) -> list[dict[str, Any]]:
        by_id = {d.id: d for d in rows}
        out = []
        for d in sorted((by_id[i] for i in ids if i in by_id), key=lambda d: d.number):
            entry: dict[str, Any] = {
                "number": d.number,
                "agent": shown(self.ctx, self.lead.agent, d.agent),
                "format": d.format,
                "status": d.status.value,
            }
            sub = self._submission(d)
            if sub is not None:
                data = item(self.ctx, self.lead.agent, sub, source=f"assign:{d.number}")
                entry["id"] = sub.id
                entry["result"] = sub.status
                for key in ("summary", "path", "files", "verdict"):
                    if key in data:
                        entry[key] = data[key]
            out.append(entry)
        return out

    @staticmethod
    def _fit_returned(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """The returned assignments within `MAX_RETURN_CHARS` (see there); True if cut."""

        def size() -> int:  # as `view.return_prompt` writes the block
            return len(json.dumps({"returned": items}, ensure_ascii=False, indent=1))

        if size() <= MAX_RETURN_CHARS:
            return items, False
        for entry in items:
            if isinstance(entry.get("summary"), str):
                entry["summary"] = line(entry["summary"], MAX_INBOX_LINE)
            entry.pop("files", None)
        while size() > MAX_RETURN_CHARS and len(items) > 1:
            items.pop(0)
        return items, True

    async def _lead_turns(self) -> None:
        """The turns of the lead until its submission, its turns used up with nothing running,
        a turn with status `limit` or the stop (see the module docstring)."""
        ctx, lead, phase, pass_ = self.ctx, self.lead, self.phase, self.pass_
        turns = lead.task.turns
        self.runner.acted.add(lead.agent)
        self.mark = lead.mark = self.runner.mark()
        used, asked = 0, False
        step: tuple[str, str | None, str] | None = ("first", None, "")
        while step is not None and not ctx.stop_requested:
            self._raise()
            kind, why, detail = step
            rows = self._sync()
            context = self._lead_package(rows)
            lead.context = context
            if kind == "first":
                prompt = render_assignment(context)
            elif kind == "return":
                back, self.returns = self.returns, []
                self.returned.clear()
                running = [d.number for d in rows if d.status is DelegationStatus.RUNNING]
                waiting = [d.number for d in rows if d.status is DelegationStatus.QUEUED]
                items, cut = self._fit_returned(self._returned(back, rows))
                prompt = return_prompt(context, items, running, waiting, cut=cut)
            elif kind == "ask":
                asked = True
                prompt = ask_again_prompt(context, why, detail)
            else:
                prompt = continue_prompt(context, used + 1, turns, why, detail)
            outcome = await ctx.turn(lead.agent, prompt, stage=phase.name, context=context)
            if kind != "return":
                used += 1
            lead.turns += 1
            found = self.runner._newest(lead, phase.name, pass_.attempt, self.mark)
            if found is not None:
                lead.found = found
                return
            if outcome.status is TurnStatus.LIMIT:
                return
            step = await self._next(used, turns, asked)

    async def _next(self, used: int, turns: int, asked: bool) -> tuple[str, str | None, str] | None:
        """What the lead gets next: a return turn, a counted turn (`continue` with its reason,
        `ask`) or nothing (None: its action is over)."""
        ctx, lead, waker = self.ctx, self.lead, self.runner.waker
        while not ctx.stop_requested:
            self._raise()
            if lead.found is not None:
                return None  # handed in in an answer turn (B3)
            rows = self._sync()
            if self.returns:
                return "return", None, ""
            if any(d.open for d in rows):
                self._end_lead_wait()
                if waker is not None and waker.take_message(lead.agent) is not None:
                    if used < turns:
                        return "continue", "message", ""
                    continue  # no turn left: it waits on for the returns
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.returned.wait(), _poll_s())
                continue
            if used >= turns and asked:
                if await self._hold_for_answer():
                    continue  # the answer turn gave assignments or a submission
                return None
            why, detail = await self.runner.wait(lead.agent)  # its `wait_until`
            return ("continue" if used < turns else "ask"), why, detail
        return None

    async def _hold_for_answer(self) -> bool:
        """B3 inside the moderation (W4b-04.1 H1, Entscheidung 82, Variante a): the lead's
        turns and its follow-up are used up, nothing runs, and it has an open question to the
        user. The moderation does not close: `hold` keeps the phase by the rule of B3 (at most
        `user_question_hold_s()` after the question, a stop ends it at once), and the answer
        gives the lead one turn with its package as it is now. Assignments of that turn run
        as usual (return turns, Entscheidung 79, `max_assignments`): the holding ends as soon
        as one is open or back. True if the answer turn left something to do (an assignment
        or a submission), False if the phase ends."""
        lead = self.lead
        lead.context = self._lead_package(self._rows())

        def busy() -> bool:
            return bool(self.returns) or any(d.open for d in self._rows())

        await hold(self.runner, self.phase, self.pass_, [lead], abort=busy)
        return lead.found is not None or busy()

    def _end_lead_wait(self) -> None:
        """While assignments run the lead waits for their returns, not for its `wait_until`."""
        get = getattr(self.ctx, "long_wait", None)
        end = getattr(self.ctx, "end_long_wait", None)
        if callable(get) and callable(end) and get(self.lead.agent) is not None:
            end(self.lead.agent)

    # -- the assignments ---------------------------------------------------------------------

    async def _work(self, agent: str) -> None:
        """The assignments of one recipient, one after the other in the order of their number."""
        waker = self.runner.waker
        try:
            while not self.ctx.stop_requested and not self.closing:
                queued = [d for d in self._rows(agent) if d.status is DelegationStatus.QUEUED]
                if not queued:
                    break
                await self._one(queued[0])
        except Exception as exc:  # e.g. an `ArrError` of a transition: ends the run
            self.error = self.error or exc
            self.returned.set()
        finally:
            self.current.pop(agent, None)
            if waker is not None:
                waker.put_back(agent)

    def _slot(self, d: Delegation) -> Slot:
        lead = shown(self.ctx, d.agent, d.lead)
        frame = assignment_frame(d.number, self.phase.name, d.attempt, lead, d.format)
        task = ResolvedTask(
            task=frame, submission=ResolvedSubmission(format=d.format), turns=DEFAULT_TURNS
        )
        return Slot(d.agent, d.action, task, frame)

    async def _one(self, d: Delegation) -> None:
        ctx, runner, phase, pass_ = self.ctx, self.runner, self.phase, self.pass_
        waker = runner.waker
        if waker is not None:
            await waker.take_out(d.agent)  # after a running wake-up turn
        if ctx.stop_requested or self.closing or not ctx.start_delegation(d.id):
            return  # cancelled meanwhile: it never begins
        self.current[d.agent] = d.id
        self._log("assign_start", number=d.number, agent=d.agent)
        slot = self._slot(d)

        def cancelled() -> bool:
            return self._cancelled(d.id)

        found: Submission | None = None
        try:
            if pass_.how == HOW_NORMAL and not cancelled():
                await runner.transition(d.agent, phase, pass_.attempt)
            if not cancelled() and not ctx.stop_requested:
                await ctx.enter_phase(phase.name, pass_.attempt, [d.agent])
                context = self._package(slot, d)
                slot.context, slot.mark = context, runner.mark()
                found = await runner.act(slot, phase.name, pass_.attempt, context, abort=cancelled)
                if found is None and not ctx.stop_requested and not cancelled():
                    await hold(runner, phase, pass_, [slot], abort=cancelled)  # B3
                    found = slot.found
        finally:
            self.current.pop(d.agent, None)
            self._end(d, slot, found)

    def _package(self, slot: Slot, d: Delegation) -> dict[str, Any]:
        package = self.runner.package(
            self.phase, self.pass_, slot, repetition=1, mark=self.runner.mark()
        )
        package["assigned"] = {
            "number": d.number,
            "from": shown(self.ctx, d.agent, d.lead),
            "text": d.task,
        }
        return fit_package(package)  # the text of the lead counts within the limit (N3)

    def _end(self, d: Delegation, slot: Slot, found: Submission | None) -> None:
        """`returned`/`missing` and a return for the lead; on a stop or a cancel the pass
        cancels it (no return)."""
        ctx = self.ctx
        ended = False
        if found is not None or not (ctx.stop_requested or self._cancelled(d.id)):
            ended = ctx.end_delegation(d.id, found.id if found is not None else None)
        final = ctx.delegation(d.id)
        if final is not None and final.open:  # a stop: the pass ends, nothing stays open
            ctx.cancel_delegations(self.phase.name, self.pass_.attempt)
            final = ctx.delegation(d.id)
        if d.id not in self.logged_end:
            self.logged_end.add(d.id)
            self._log(
                "assign_end",
                number=d.number,
                agent=d.agent,
                status=(final.status if final is not None else d.status).value,
                submission=found.id if found is not None else None,
                turns=slot.turns,
            )
        if ended:
            self.returns.append(d.id)
            self.returned.set()

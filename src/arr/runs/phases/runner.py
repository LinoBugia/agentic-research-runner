"""The phase runner: `topology: phases` (W4b-02b; docs/10 §4).

`run(ctx)` checks the run again (`ensure_valid`, the task of the run), reads every assignment
file (`arr.runs.phases.tasks`), adds every `seed` as version 1 and then runs the phases:

1. **Enter** a phase only through `arr.plan.read.follow` and `arr.plan.read.enter`; the runner
   counts the visits of every phase (every entry, the first too) and has no rule of its own
   for `max_visits`, `on_exhausted` or dropped phases. The user's choice „noch ein Durchlauf“
   enters the phase without `enter` (Entscheidung 31).
2. **A pass**: checkpoint `pass_start`, open exactly the channels of the phase and close all
   others, `enter_phase` for every participant, then the topology of the phase (`PASSES`).
3. **The gate** without a model (`arr.runs.phases.gates`); a gate `judge` first has its rating
   actions. The result of a gate picks its route (`on_tie` on a tie). A gate `user`, the route
   `escalate`, exhausted phases with `on_exhausted: escalate` and a gate without a result ask
   the user (`escalate_with_hint`): `continue`, `retry` (with a hint) or `end`.
4. **Bookkeeping**: the valid submission of the phase (Entscheidung 6), the mean scores, the
   run's objections; checkpoints `gate`, `route` and `stage_end`.

Checkpoints (`orchestrator.jsonl`, the contract for W4b-07; `stage` is the phase):

- `{phase: "pass_start", attempt, how, agents}`; `how` is `normal`, `retry` or `user_retry`,
- `{phase: "gate", attempt, gate, value, tie, decision, reason?, score?, scores?, ranking?,
  results, agent?, missing, valid}`: `decision` `passed`/`failed`/`escalated` as the old
  topologies write it (`stage_infos` and `summary.md` read it), `value` the result of the gate,
  `results` the newest submission of every action of the pass, `valid` the valid submissions
  of the phase,
- `{phase: "route", attempt, route, entered, exhausted, asked_user?, choice?, hint?}`,
- `{phase: "stage_end", attempt, outcome, accepted, valid, score?, attempts}` when the run leaves
  the phase (another phase or the end); `accepted` is the first valid submission.
- `{phase: "seed", seeds}` at the first phase, before its first pass, with the seed versions.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from arr.backends.base import TurnStatus
from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import (
    ContextMode,
    FolderMode,
    GateRule,
    GateType,
    Goto,
    InputContent,
    PhaseTopology,
    ResolvedAction,
    ResolvedPhase,
    ResolvedRun,
    Route,
    RouteKeyword,
)
from arr.permissions.types import EscalationOption
from arr.plan.read import ACTION_GATE, build_expectation, enter, follow
from arr.room.types import Submission
from arr.runs.context import RunContext
from arr.runs.phases import chat, work
from arr.runs.phases.gates import NO_RATING, GateOutcome, judge_mean, judge_rank, signal
from arr.runs.phases.inputs import (
    build_feedback,
    build_inputs,
    inbox_summary,
    item,
    shown,
)
from arr.runs.phases.start import start_issues
from arr.runs.phases.state import (
    HOW_NORMAL,
    HOW_RETRY,
    HOW_USER,
    Pass,
    PassRecord,
    RunState,
    Slot,
)
from arr.runs.phases.tasks import KIND_GATE, KIND_WORK, Tasks, load_tasks
from arr.runs.phases.text import MAX_SUMMARY_CHARS, MAX_TITLE_CHARS, fit_package, line
from arr.runs.phases.view import ask_again_prompt, continue_prompt, render_assignment
from arr.runs.phases.wake import Waker, hold
from arr.runs.topologies import SOURCE, require_task

__all__ = ["OPTIONS", "PASSES", "WAIT_POLL_S", "PhaseRunner", "check", "run"]

logger = logging.getLogger(__name__)

PassFunction = Callable[["PhaseRunner", Pass], Awaitable[None]]

PASSES: dict[PhaseTopology, PassFunction] = {
    PhaseTopology.SOLO: work.run_pass,
    PhaseTopology.PARALLEL: work.run_pass,
    PhaseTopology.CHAT: chat.run_pass,
}
"""The topology of a phase → the function that runs one pass of it (W4b-04 adds `moderated`)."""

OPTIONS = (EscalationOption.CONTINUE, EscalationOption.RETRY, EscalationOption.END)
"""What the user may choose at a gate `user`, after `escalate` and without a result."""

WAIT_POLL_S = 0.2
"""How often a wait after `wait_until` looks at its deadline, file and awaited agent."""

MAX_SEED_TITLE = 80
"""A seed entry given as text gets its first line as title, cut to this length."""


def _context_class() -> type:
    from arr.runs.orchestrator import Orchestrator  # late: the orchestrator imports topologies

    return Orchestrator


def check(resolved: ResolvedRun) -> list[ConfigIssue]:
    """`check_topology` of a phases run: what keeps it from starting with the `Orchestrator`."""
    return start_issues(resolved, _context_class())


async def run(ctx: RunContext) -> None:
    """The topology function of `topology: phases`."""
    await PhaseRunner(ctx).run()


def _route_text(route: Route | None) -> str | None:
    if route is None:
        return None
    if isinstance(route, Goto):
        return f"goto:{route.goto}"
    return route.value


def seed_items(name: str, entries: list[Any]) -> list[dict[str, str]]:
    """Entries of a seed list as `{title, text}`: a text becomes an entry whose title is its
    first line (cut to `MAX_SEED_TITLE`) and whose text is the whole text; `{title, text}`
    stays. Anything else raises `ArrError`."""
    out = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str) and entry.strip():
            first = next(t for t in entry.splitlines() if t.strip())
            out.append({"title": line(first, MAX_SEED_TITLE), "text": entry})
        elif (
            isinstance(entry, dict)
            and set(entry) == {"title", "text"}
            and all(isinstance(v, str) and v.strip() for v in entry.values())
        ):
            out.append({"title": entry["title"], "text": entry["text"]})
        else:
            raise ArrError(
                f"Der Seed „{name}“: Eintrag {index + 1} ist weder ein Text noch "
                "„{title, text}“ mit zwei Texten."
            )
    return out


class PhaseRunner:
    """Runs the phases of one run on a `RunContext` (see the module docstring)."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.run_config = ctx.config
        self.state = RunState()
        self.tasks: Tasks = {}
        self.question = ""
        self.acted: set[str] = set()
        """Agents that have begun an action in this run (no transition before the first); a
        sleeper's wake-up turn puts it here as well (W4b-03, N-G4)."""
        self.waker: Waker | None = None
        """The waking of the running pass (W4b-03): sleepers, the end of `wait_until`."""

    # -- start -------------------------------------------------------------------------------

    def ensure_valid(self) -> None:
        """The checks of `check_topology`, again with the class of this context."""
        issues = start_issues(self.run_config, type(self.ctx))
        if issues:
            raise ConfigError(issues, source=SOURCE)

    async def run(self) -> None:
        """Check, read the assignments, add the seeds, run the phases until the end."""
        self.ensure_valid()
        self.question = require_task(self.ctx)
        self.tasks = await asyncio.to_thread(
            load_tasks, self.ctx.run_folder, self.run_config, self.ctx.params
        )
        if not self.run_config.phases:
            return
        self.ctx.open_private_channels()  # W4b-03: before the first turn, one per agent
        await self.plant_seeds()
        nxt = await self.enter(self.run_config.phases[0].name, HOW_NORMAL, {})
        while nxt is not None and not self.ctx.stop_requested:
            name, how, hint = nxt
            record = await self.one_pass(name, how, hint)
            if record is None:
                return
            nxt = await self.after(record)

    async def plant_seeds(self) -> None:
        """Every `seed` as version 1 (`add_version`, outside the event loop), before the
        first phase; a seed that does not fit ends the run with a clear message."""
        planted: dict[str, int] = {}
        for name, seed in self.run_config.seed.items():
            try:
                if seed.file is not None:
                    version = await asyncio.to_thread(
                        self.ctx.add_version,
                        name,
                        summary=f"Datei „{line(seed.file, MAX_TITLE_CHARS)}“ aus dem Projekt.",
                        format="artifact",
                        files=[seed.file],
                    )
                elif seed.items is not None:
                    items = seed_items(name, seed.items)
                    source = f"Parameter „{seed.param}“" if seed.param else "der Group"
                    version = await asyncio.to_thread(
                        self.ctx.add_version,
                        name,
                        summary=f"Liste mit {len(items)} Einträgen aus {source}.",
                        format="list",
                        fields={"items": items},
                    )
                else:
                    continue
            except ArrError as exc:
                text = str(exc).removeprefix(f"Der Seed „{name}“: ")
                raise ArrError(f"Der Seed „{name}“ lässt sich nicht anlegen: {text}") from None
            planted[name] = version.id
        if planted:
            await self.ctx.checkpoint(self.run_config.phases[0].name, phase="seed", seeds=planted)

    # -- entering phases ---------------------------------------------------------------------

    async def enter(
        self, target: str | None, how: str, info: dict[str, Any]
    ) -> tuple[str, str, str | None] | None:
        """The phase a route to `target` really enters (`arr.plan.read.enter`), asking the user
        when an exhausted phase escalates; None: the run ends. `info` collects `entered` and
        `exhausted` for the checkpoint `route`."""
        entry = enter(self.run_config, target, self.state.visits)
        info.setdefault("exhausted", []).extend(entry.exhausted)
        if entry.ask_user:
            assert entry.phase is not None
            last = self.state.last(entry.phase)
            option, hint = await self.ask_user(
                entry.phase,
                self.state.visits.get(entry.phase, 0),
                "exhausted",
                "",
                last.latest if last is not None else [],
            )
            info.update(asked_user="exhausted", choice=option.value)
            if option is EscalationOption.RETRY:
                info["entered"] = entry.phase
                info["hint"] = bool(hint)
                return entry.phase, HOW_USER, hint
            if option is EscalationOption.CONTINUE and not self.ctx.stop_requested:
                successor = follow(self.run_config, entry.phase, RouteKeyword.NEXT)
                return await self.enter(successor, HOW_NORMAL, info)
            info["entered"] = None
            return None
        info["entered"] = entry.phase
        if entry.phase is None:
            return None
        if how == HOW_RETRY and entry.phase != target:
            how = HOW_NORMAL  # redirected: a normal entry of another phase
        return entry.phase, how, None

    def _channels(self, phase: ResolvedPhase) -> None:
        """Open exactly the channels of the phase and close all others of `run.yaml`. The
        private channels to the user are not in `run.yaml`: they stay open in every phase,
        also an isolated one (W4b-03, Entscheidung 30)."""
        for channel in self.run_config.groupchats:
            if channel.name in phase.channels:
                self.ctx.open_channel(channel.name)
            else:
                self.ctx.close_channel(channel.name)

    async def one_pass(self, name: str, how: str, hint: str | None) -> PassRecord | None:
        """Run one pass of `name`; None if the run stopped meanwhile."""
        ctx = self.ctx
        phase = self.run_config.phase(name)
        attempt = self.state.visits[name] = self.state.visits.get(name, 0) + 1
        await ctx.checkpoint(
            name, phase="pass_start", attempt=attempt, how=how, agents=phase.agents
        )
        pass_ = Pass(name, attempt, how, hint)
        self._channels(phase)
        ctx.set_participants(phase.participants)  # Entscheidung 64: the others hear nothing
        await ctx.enter_phase(name, attempt, phase.participants)  # before any turn (§6.1)
        self.waker = Waker.attach(self, phase, pass_)
        try:
            await PASSES[phase.topology](self, pass_)
            if ctx.stop_requested:
                return None
            rater_subs: list[Submission] = []
            if phase.gate.type is GateType.JUDGE and pass_.list_missing is None:
                await work.rate(self, phase, pass_)
                if ctx.stop_requested:
                    return None
                await hold(self, phase, pass_, pass_.raters)  # B3 after the rating actions
                if ctx.stop_requested:
                    return None
                rater_subs = [s.found for s in pass_.raters if s.found is not None]
        finally:
            if self.waker is not None:
                await self.waker.detach()
            self.waker = None
        return self.book(phase, pass_, rater_subs)

    async def after_actions(self, phase: ResolvedPhase, pass_: Pass) -> None:
        """The actions of a pass are done (W4b-03): no sleeper is woken any more (a running
        wake-up turn ends first, §6.4), then B3 holds the phase while a question of an agent
        with an action to the user is open (§6.6). The topologies call it before they release
        an isolated phase, so a turn after the user's answer sees no other submission."""
        if self.waker is not None:
            await self.waker.end_actions()
        if not self.ctx.stop_requested:
            await hold(self, phase, pass_, pass_.slots)

    # -- gates -------------------------------------------------------------------------------

    def evaluate(
        self, phase: ResolvedPhase, pass_: Pass, rater_subs: list[Submission]
    ) -> tuple[GateOutcome, list[dict[str, Any]]]:
        """The outcome of the gate of a pass and the ratings it used."""
        gate = phase.gate
        if pass_.list_missing is not None:
            return GateOutcome(reason=pass_.list_missing), []
        latest = [s.found for s in pass_.slots]
        if gate.type is GateType.DONE:
            return GateOutcome("done"), []
        if gate.type is GateType.USER:
            return GateOutcome(), []
        if gate.type is GateType.SIGNAL:
            assert gate.field is not None
            values: list[Any] = []
            for s in latest:
                if gate.field == "status":
                    values.append(s.status if s is not None else "not_solved")
                else:
                    values.append(None if s is None else s.fields.get(gate.field))
            return signal(gate.field, gate.rule, values), []
        rated = [s for s in latest if s is not None]
        if gate.rule in (GateRule.MEAN, GateRule.RANK):
            ratings = work.ratings_of(rater_subs, rated)
            by_ref: dict[int, list[float]] = {}
            for r in ratings:
                by_ref.setdefault(r["ref"], []).append(r["score"])
            if gate.rule is GateRule.RANK:
                return judge_rank(by_ref, [s.id for s in rated]), ratings
            pass_score = gate.pass_score
            if pass_score is None:
                rubric = self.run_config.rubric
                pass_score = rubric.pass_score if rubric is not None else None
            if pass_score is None:
                raise ArrError(f"Das Gate „judge“ der Phase „{phase.name}“ hat keinen pass_score.")
            return judge_mean(by_ref, pass_score), ratings
        by_rater = {s.agent: s for s in rater_subs}
        verdicts = [
            by_rater[slot.agent].fields.get("verdict") if slot.agent in by_rater else None
            for slot in pass_.raters
        ]
        if not rater_subs:
            return GateOutcome(reason=NO_RATING), []
        rule = gate.rule if gate.rule is not None else GateRule.MAJORITY
        return signal("verdict", rule, verdicts), []

    def book(self, phase: ResolvedPhase, pass_: Pass, rater_subs: list[Submission]) -> PassRecord:
        """Evaluate the gate and keep the pass: scores, valid submission, objections."""
        outcome, ratings = self.evaluate(phase, pass_, rater_subs)
        latest = [s.found for s in pass_.slots if s.found is not None]
        record = PassRecord(
            phase=phase.name,
            attempt=pass_.attempt,
            how=pass_.how,
            latest=latest,
            missing=[f"{s.agent}/{s.action}" for s in pass_.slots if s.found is None],
            gate=phase.gate.type.value,
            outcome=outcome,
            ratings=ratings,
            rater_reviews=[s for s in rater_subs if s.format == "review"],
            hint=pass_.hint,
        )
        state = self.state
        state.scores.update(outcome.scores)
        state.records.setdefault(phase.name, []).append(record)
        state.valid[phase.name] = self.valid(phase, record)
        if phase.name in state.finished:
            state.finished.remove(phase.name)
        state.finished.append(phase.name)
        reviews = [s for s in [*latest, *rater_subs] if s.format == "review"]
        state.objections.take([(s.agent, s.fields) for s in reviews], phase.name, pass_.attempt)
        return record

    def valid(self, phase: ResolvedPhase, record: PassRecord) -> list[Submission]:
        """The valid submission of a phase (Entscheidung 6): at a gate `judge` with `mean` the
        best rated version over all passes, on a tie the newer; else the newest of the pass
        (with `rank` in the order of the ranking)."""
        gate = phase.gate
        if gate.type is GateType.JUDGE and gate.rule is GateRule.MEAN:
            candidates = [
                s
                for r in self.state.records.get(phase.name, [])
                for s in r.latest
                if s.id in self.state.scores
            ]
            if candidates:
                return [max(candidates, key=lambda s: (self.state.scores[s.id], s.id))]
        if gate.type is GateType.JUDGE and gate.rule is GateRule.RANK:
            order = {ref: i for i, ref in enumerate(record.outcome.ranking)}
            return sorted(record.latest, key=lambda s: order.get(s.id, len(order)))
        return list(record.latest)

    # -- routes ------------------------------------------------------------------------------

    def _decision(
        self, phase: ResolvedPhase, record: PassRecord, route: Route | None, asks: bool
    ) -> str:
        if asks:
            return "escalated"
        if phase.gate.type is GateType.JUDGE and phase.gate.rule is GateRule.MEAN:
            return "passed" if record.outcome.result == "pass" else "failed"
        return "failed" if route is RouteKeyword.RETRY else "passed"

    async def after(self, record: PassRecord) -> tuple[str, str, str | None] | None:
        """Gate checkpoint, the route (asking the user where needed), route and stage_end
        checkpoints; returns the next pass or None at the end."""
        phase = self.run_config.phase(record.phase)
        gate = phase.gate
        outcome = record.outcome
        route: Route | None = None
        reason: str | None = None
        detail = ""
        if gate.type is GateType.USER:
            reason = "gate_user"
        elif outcome.result is None and not outcome.tie:
            reason, detail = "no_result", outcome.reason
        else:
            route = gate.on_tie if outcome.tie else gate.routes.get(str(outcome.result))
            if route is None:
                reason = "no_result"
                detail = (
                    "Gleichstand, und das Gate hat keine Route „on_tie“."
                    if outcome.tie
                    else f"Für das Ergebnis „{line(outcome.result, 40)}“ gibt es keine Route."
                )
            elif route is RouteKeyword.ESCALATE:
                reason = "escalate"
        await self._gate_checkpoint(phase, record, route, reason)
        info: dict[str, Any] = {"attempt": record.attempt, "route": _route_text(route)}
        nxt: tuple[str, str, str | None] | None
        if reason is not None:
            option, hint = await self.ask_user(
                phase.name, record.attempt, reason, detail, record.latest
            )
            info.update(asked_user=reason, choice=option.value)
            if option is EscalationOption.RETRY:
                info.update(entered=phase.name, exhausted=[], hint=bool(hint))
                nxt = (phase.name, HOW_USER, hint)
            elif option is EscalationOption.CONTINUE and not self.ctx.stop_requested:
                successor = follow(self.run_config, phase.name, RouteKeyword.NEXT)
                nxt = await self.enter(successor, HOW_NORMAL, info)
            else:
                info.update(entered=None, exhausted=[])
                nxt = None
        else:
            assert route is not None
            target = follow(self.run_config, phase.name, route)
            how = HOW_RETRY if route is RouteKeyword.RETRY else HOW_NORMAL
            nxt = await self.enter(target, how, info)
        await self.ctx.checkpoint(phase.name, phase="route", **info)
        if nxt is None or nxt[0] != phase.name:
            ends = nxt is None and not self._left_by_next(route, info)
            await self._stage_end(phase, record, ends)
        return nxt

    async def _gate_checkpoint(
        self, phase: ResolvedPhase, record: PassRecord, route: Route | None, reason: str | None
    ) -> None:
        outcome = record.outcome
        data: dict[str, Any] = {
            "phase": "gate",
            "attempt": record.attempt,
            "gate": record.gate,
            "value": outcome.result,
            "tie": outcome.tie,
            "decision": self._decision(phase, record, route, reason is not None),
            "results": [s.id for s in record.latest],
            "missing": record.missing,
            "valid": [s.id for s in self.state.valid.get(phase.name, [])],
        }
        if reason == "no_result":
            data["reason"] = "no_rating" if outcome.reason == NO_RATING else "no_result"
        if len(phase.agents) == 1:
            data["agent"] = phase.agents[0]
        if outcome.score is not None:
            data["score"] = outcome.score
        if outcome.scores:
            data["scores"] = {str(k): v for k, v in outcome.scores.items()}
        if outcome.ranking:
            data["ranking"] = outcome.ranking
        await self.ctx.checkpoint(phase.name, **data)

    def _left_by_next(self, route: Route | None, info: Mapping[str, Any]) -> bool:
        """True if the run leaves the phase over `next`, also past the end of the plan (its
        best attempt counts, as `on_fail: continue` of the old `waterfall`); False if it ends
        there: route `stop`, `on_exhausted: stop` or the user's „Beenden“ (W4b-06a.1, Entscheidung
        81)."""
        if info.get("asked_user"):
            return info.get("choice") == EscalationOption.CONTINUE.value
        exhausted = info.get("exhausted") or []
        if exhausted:
            route = self.run_config.phase(exhausted[-1]).on_exhausted
        return route is RouteKeyword.NEXT

    async def _stage_end(self, phase: ResolvedPhase, record: PassRecord, ends: bool) -> None:
        """Checkpoint `stage_end` when a phase is left. With a threshold gate the outcome is
        `passed`, `best_accepted` (left over `next`) or `stage_failed` (`ends`: the run ends here
        because the phase did not pass; then nothing is accepted, as in the old `waterfall`)."""
        valid = self.state.valid.get(phase.name, [])
        outcome = "done"
        if phase.gate.type is GateType.JUDGE and phase.gate.rule is GateRule.MEAN:
            if record.outcome.result == "pass":
                outcome = "passed"
            else:
                outcome = "stage_failed" if ends else "best_accepted"
        failed = outcome == "stage_failed"
        data: dict[str, Any] = {
            "phase": "stage_end",
            "attempt": record.attempt,
            "outcome": outcome,
            "accepted": valid[0].id if valid and not failed else None,
            "valid": [s.id for s in valid],
            "attempts": self.state.visits.get(phase.name, 0),
        }
        if valid and valid[0].id in self.state.scores:
            data["score"] = self.state.scores[valid[0].id]
        await self.ctx.checkpoint(phase.name, **data)

    async def ask_user(
        self, phase: str, attempt: int, reason: str, detail: str, subs: list[Submission]
    ) -> tuple[EscalationOption, str | None]:
        """The user decides: `continue`, `retry` (with a hint) or `end`. A stop during the
        question gives `end`."""
        if self.ctx.stop_requested:
            return EscalationOption.END, None
        summary = inbox_summary(phase, attempt, reason, subs, detail)
        option, hint = await self.ctx.escalate_with_hint(phase, summary, OPTIONS)
        if self.ctx.stop_requested:
            return EscalationOption.END, None
        return option, hint

    # -- actions -----------------------------------------------------------------------------

    def task_text(self, phase: ResolvedPhase, action: ResolvedAction | None) -> str:
        """The assignment text of an action (None: the rating action of the gate)."""
        if action is None:
            return self.tasks[(phase.name, KIND_GATE, "")].text
        return self.tasks[(phase.name, KIND_WORK, action.key)].text

    def mark(self) -> int:
        """The highest submission number so far: a submission counts for an action only when
        it is newer than the mark taken at the action's start."""
        return max((s.id for s in self.ctx.submissions()), default=0)

    def release(self, phase: str) -> None:
        """Release a sealed phase; an error of the room ends the run (Entscheidung 36)."""
        self.ctx.release_results(phase)

    async def transition(self, agent: str, phase: ResolvedPhase, attempt: int) -> None:
        """Context and folder at the start of an action (docs/10 §4.6): first the session
        (`reset_session` or `compact_session` with the agent's own `note_next`), then the
        folder (`fresh_folder`). An `ArrError` of these ends the run. Before the very first
        action of an agent in the run nothing happens (Entscheidung 58)."""
        if agent not in self.acted:
            return  # before its very first action there is no session and nothing to file
        mode = phase.context.get(agent, ContextMode.KEEP)
        folder = phase.folder.get(agent, FolderMode.KEEP)
        if mode is ContextMode.RESET:
            await self.ctx.reset_session(agent)
        elif mode is ContextMode.COMPACT:
            own = self.ctx.submissions(agent=agent)
            note = own[-1].note_next if own else None
            await self.ctx.compact_session(agent, note=note)
        if folder is FolderMode.FRESH:
            await self.ctx.fresh_folder(agent, stage=phase.name, attempt=attempt)

    def _expected(
        self, phase: ResolvedPhase, task: Any, action: str, rate: list[int] | None = None
    ) -> dict[str, Any]:
        fmt = task.submission.format
        known = self.state.objections.ids() if fmt == "review" else None
        try:
            expected = build_expectation(
                self.run_config, phase, task, action=action, rate=rate, known_objections=known
            )
        except ValidationError as exc:
            raise ArrError(
                f"Die erwartete Abgabe in der Phase „{phase.name}“ lässt sich nicht bilden: "
                f"{exc.errors()[0]['msg'] if exc.errors() else exc}"
            ) from None
        return expected.model_dump(mode="json", exclude_none=True)

    def _base(
        self, phase: ResolvedPhase, pass_: Pass, slot: Slot, expected: dict[str, Any]
    ) -> dict[str, Any]:
        base = {
            "phase": phase.name,
            "attempt": pass_.attempt,
            "task": slot.text,
            "research_question": self.question,
            "submission": expected,
        }
        private = self.ctx.private_channel(slot.agent)
        if private is not None:  # W4b-03: only in the package of its own agent
            base["user_channel"] = private
        return base

    def _objections(self, package: dict[str, Any], task: Any) -> None:
        relevant = task.submission.format == "review" or task.submission.name is not None
        if relevant and self.state.objections.items:
            package["objections"] = self.state.objections.as_data()

    def _feedback(self, package: dict[str, Any], pass_: Pass, viewer: str) -> None:
        if pass_.how == HOW_NORMAL:
            return
        record = self.state.last(pass_.phase)
        if record is not None:
            hint = pass_.hint if pass_.how == HOW_USER else None
            package["feedback"] = build_feedback(self.ctx, record, viewer, hint)

    def peers(self, pass_: Pass, slot: Slot, mark: int) -> list[dict[str, Any]]:
        """`peers: summary` (Entscheidung 58, like today's `digest`): per other agent its newest
        summary of this pass handed in before the running repetition began (`mark`); never the
        agent's own, never more than one per agent."""
        newest: dict[str, Submission] = {}
        for s in self.ctx.submissions(stage=pass_.phase, attempt=pass_.attempt):
            if s.agent == slot.agent or s.id > mark or s.action is None:
                continue
            if not (s.action == "work" or s.action.startswith("work-")):
                continue
            if s.agent not in newest or s.id > newest[s.agent].id:
                newest[s.agent] = s
        out = []
        for s in sorted(newest.values(), key=lambda s: s.id):
            entry: dict[str, Any] = {
                "from": shown(self.ctx, slot.agent, s.agent, s.shown_as),
                "status": s.status,
                "summary": line(s.summary, MAX_SUMMARY_CHARS),
            }
            number = s.action.removeprefix("work-") if s.action else ""
            if number.isdigit():
                entry["entry"] = int(number)
            out.append(entry)
        return out

    def package(
        self, phase: ResolvedPhase, pass_: Pass, slot: Slot, *, repetition: int, mark: int
    ) -> dict[str, Any]:
        """The context of an action (keys in `arr.runs.phases.view`)."""
        package = self._base(phase, pass_, slot, self._expected(phase, slot.task, slot.action))
        if phase.repeat > 1:
            package["repetition"] = {"number": repetition, "of": phase.repeat}
        if slot.entry is not None and slot.item is not None:
            package["entry"] = {"number": slot.entry + 1, **slot.item}
        inputs, peers = build_inputs(self.ctx, self.state, phase, slot.agent)
        if inputs:
            package["inputs"] = inputs
        if peers:
            package["peers"] = self.peers(pass_, slot, mark)
        self._feedback(package, pass_, slot.agent)
        self._objections(package, slot.task)
        return fit_package(package)

    def rating_package(
        self,
        phase: ResolvedPhase,
        pass_: Pass,
        slot: Slot,
        subs: list[Submission],
        rate: list[int],
        mark: int,
    ) -> dict[str, Any]:
        """The context of a rating action: the submissions to rate in full."""
        task = phase.gate.action
        assert task is not None
        is_rating = task.submission.format == "rating"
        expected = self._expected(phase, task, ACTION_GATE, rate if is_rating else None)
        package = self._base(phase, pass_, slot, expected)
        package["inputs"] = [
            item(self.ctx, slot.agent, s, source=f"phase:{phase.name}", content=InputContent.FULL)
            for s in subs
            if s.id in rate
        ]
        self._feedback(package, pass_, slot.agent)
        self._objections(package, task)
        return fit_package(package)

    def _newest(self, slot: Slot, stage: str, attempt: int, mark: int) -> Submission | None:
        found = self.ctx.submissions(
            agent=slot.agent, stage=stage, attempt=attempt, action=slot.action, latest=True
        )
        newer = [s for s in found if s.id > mark]
        return newer[-1] if newer else None

    async def act(
        self, slot: Slot, stage: str, attempt: int, context: Mapping[str, Any]
    ) -> Submission | None:
        """Turns of one action until its submission is there (W4b-02b §6.4): at most `turns`
        turns, then one follow-up question; a turn with status `limit` ends the action, one
        with `error` counts as a turn without submission. After `wait_until` the next turn
        starts when the wait ends. A stop ends it at once. None: the submission is missing."""
        ctx = self.ctx
        turns = slot.task.turns
        self.acted.add(slot.agent)
        mark = self.mark()
        used, asked = 0, False
        why: str | None = None
        detail = ""
        while not ctx.stop_requested:
            if used == 0:
                prompt = render_assignment(context)
            elif used < turns:
                prompt = continue_prompt(context, used + 1, turns, why, detail)
            elif not asked:
                asked = True
                prompt = ask_again_prompt(context, why, detail)
            else:
                break
            outcome = await ctx.turn(slot.agent, prompt, stage=stage, context=context)
            used += 1
            slot.turns = used
            found = self._newest(slot, stage, attempt, mark)
            if found is not None:
                return found
            if outcome.status is TurnStatus.LIMIT or asked:
                break
            why, detail = await self.wait(slot.agent)
        return None

    async def wait(self, agent: str) -> tuple[str | None, str]:
        """After `wait_until`: wait for the deadline, the file, a submission of the awaited
        agent that `agent` may read (a sealed one does not count) or (W4b-03 §6.4) a direct
        message to it in an open channel of the phase or its private channel, which the waker
        of the pass hands over (no second read of the room); a stop ends the wait. Uses
        `long_wait`, `until_file_exists` and `end_long_wait` of the orchestrator."""
        ctx = self.ctx
        waker = self.waker
        get = getattr(ctx, "long_wait", None)
        wait = get(agent) if callable(get) else None
        if wait is None:
            if waker is not None:
                waker.clear(agent)  # its next turn shows them in the inbox
            return None, ""
        why: str | None = None
        detail = ""
        try:
            while not ctx.stop_requested:
                if waker is not None and waker.take_message(agent) is not None:
                    why = "message"
                    break
                if time.time() >= wait.until:
                    why = "deadline"
                    break
                if wait.file and ctx.until_file_exists(agent, wait.file):  # type: ignore[attr-defined]
                    why, detail = "file", line(wait.file, MAX_TITLE_CHARS)
                    break
                if wait.result_from and any(
                    not s.sealed and s.ts > wait.since
                    for s in ctx.submissions(agent=wait.result_from)
                ):
                    why, detail = "submission", shown(ctx, agent, wait.result_from)
                    break
                await asyncio.sleep(WAIT_POLL_S)
        finally:
            end = getattr(ctx, "end_long_wait", None)
            if callable(end):
                end(agent)
            if waker is not None:
                waker.clear(agent)
        return why, detail

"""One pass of a phase `solo` or `parallel` (W4b-02b §6.4, §6.5; docs/10 §4.5).

- `solo`: one agent, one action. `parallel`: every agent with an action at the same time;
  the limits per login profile are the orchestrator's.
- `over`: the list comes from the newest version of the named submission (`fields.items`) or
  from a `seed`. Entry i goes to agent i mod n (round robin); every entry is an action of its
  own (`Expectation.action` = `work-<i+1>`); an agent works its entries in order.
- `repeat: N`: every action runs N times in a row, the agents in lockstep: all of them the
  first repetition, then all the second. With `over` a block of entries (as many as agents)
  goes through every repetition, then the next block. The newest submission of an action
  counts. Without `over` every agent is one block.
- `isolated`: the phase is sealed at the start of the pass and released after the last
  repetition of the last action, before a gate `judge` rates; a stop or an error in between
  releases it as well (nothing stays half sealed). An error of `release` ends the run.
- A gate `judge`: every rater gets one rating action (`action: gate`) with `rate` = the newest
  submissions of the pass that are not its own; the raters work at the same time.

Context and folder of a phase (`reset`, `compact`, `fresh`) apply at the start of every
action of an agent, also of every entry of `over`, never in a repeated pass (`retry`, the
user's choice) and never for the repetitions 2 to N of `repeat` (`runner.transition`).
"""

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from arr.errors import ArrError
from arr.models import GateType, ResolvedPhase
from arr.plan.read import ACTION_GATE, ACTION_WORK
from arr.room.types import Submission
from arr.runs.phases.state import HOW_NORMAL, Pass, Slot
from arr.runs.phases.text import MAX_ENTRY_TEXT_CHARS, MAX_TITLE_CHARS, block, line

if TYPE_CHECKING:
    from arr.runs.phases.runner import PhaseRunner

__all__ = ["MAX_RATE", "over_items", "rate", "ratings_of", "run_pass", "slots", "together"]

logger = logging.getLogger(__name__)

MAX_RATE = 100
"""Submissions one rating action can rate (`Expectation.rate`, `ratings` of `rating`)."""


def over_items(runner: "PhaseRunner", phase: ResolvedPhase) -> tuple[list[dict[str, str]], str]:
    """The entries of the `over` list: the newest version of the name (a list or a seed),
    each `{title, text}`; an empty list with the reason when there is none."""
    assert phase.over is not None
    latest = runner.ctx.latest_version(phase.over)
    if latest is None:
        return [], f"Die Liste „{phase.over}“ für „over“ gibt es noch nicht."
    entries = latest.fields.get("items")
    if not isinstance(entries, list) or not entries:
        return [], f"Die Liste „{phase.over}“ für „over“ ist leer."
    items = []
    for entry in entries:
        if isinstance(entry, dict):
            title, text = entry.get("title"), entry.get("text")
        else:
            title, text = None, entry
        items.append(
            {
                "title": line(title or text or "", MAX_TITLE_CHARS),
                "text": block(text or "", MAX_ENTRY_TEXT_CHARS),
            }
        )
    return items, ""


def slots(runner: "PhaseRunner", phase: ResolvedPhase, pass_: Pass) -> list[list[Slot]]:
    """The actions of a pass in blocks (see the module docstring)."""
    agents = phase.agents
    actions = {a.agent: a for a in phase.actions}
    if phase.over is None:
        block_ = [
            Slot(agent, ACTION_WORK, actions[agent], runner.task_text(phase, actions[agent]))
            for agent in agents
        ]
        pass_.slots = block_
        return [block_] if block_ else []
    items, why = over_items(runner, phase)
    if not items:
        pass_.list_missing = why
        return []
    out: list[list[Slot]] = []
    for start in range(0, len(items), len(agents)):
        block_ = []
        for i in range(start, min(start + len(agents), len(items))):
            agent = agents[i % len(agents)]
            action = actions[agent]
            block_.append(
                Slot(
                    agent,
                    f"{ACTION_WORK}-{i + 1}",
                    action,
                    runner.task_text(phase, action),
                    entry=i,
                    item=items[i],
                )
            )
        out.append(block_)
    pass_.slots = [s for b in out for s in b]
    return out


async def together(coroutines: list[Coroutine[Any, Any, None]]) -> None:
    """Run the actions of a round at the same time. An error of one cancels the others and is
    raised as itself (not as an exception group), so the run ends with its message."""
    try:
        async with asyncio.TaskGroup() as group:
            for coroutine in coroutines:
                group.create_task(coroutine)
    except BaseExceptionGroup as errors:
        first: BaseException = errors
        while isinstance(first, BaseExceptionGroup):
            first = first.exceptions[0]
        raise first from None


async def _action(
    runner: "PhaseRunner", phase: ResolvedPhase, pass_: Pass, slot: Slot, repetition: int, mark: int
) -> None:
    ctx = runner.ctx
    if ctx.stop_requested:
        return
    if repetition == 1 and pass_.how == HOW_NORMAL:
        await runner.transition(slot.agent, phase, pass_.attempt)
    await ctx.enter_phase(phase.name, pass_.attempt, [slot.agent])
    context = runner.package(phase, pass_, slot, repetition=repetition, mark=mark)
    found = await runner.act(slot, phase.name, pass_.attempt, context)
    if found is not None:
        slot.found = found


async def run_pass(runner: "PhaseRunner", pass_: Pass) -> None:
    """Run the actions of a pass of a phase `solo` or `parallel` (and seal, release)."""
    ctx = runner.ctx
    phase = ctx.config.phase(pass_.phase)
    if phase.isolated:
        ctx.seal_results(phase.name)
        pass_.sealed = True
    released = False
    try:
        for block_ in slots(runner, phase, pass_):
            for repetition in range(1, phase.repeat + 1):
                if ctx.stop_requested:
                    break
                mark = runner.mark()
                await together(
                    [_action(runner, phase, pass_, slot, repetition, mark) for slot in block_]
                )
        if pass_.sealed:
            released = True
            runner.release(phase.name)
    finally:
        if pass_.sealed and not released:
            # a stop (cancel) or an error: nothing stays half sealed; the first error counts
            with contextlib.suppress(Exception):
                runner.release(phase.name)


def _rate_list(subs: list[Submission], rater: str) -> list[int]:
    return [s.id for s in subs if s.agent != rater]


async def rate(runner: "PhaseRunner", phase: ResolvedPhase, pass_: Pass) -> list[Submission]:
    """The rating actions of a gate `judge`: one per rater, at the same time; returns the
    newest submission of every rater that handed one in."""
    ctx = runner.ctx
    gate = phase.gate
    assert gate.type is GateType.JUDGE and gate.action is not None
    subs = [s.found for s in pass_.slots if s.found is not None]
    if not subs:
        return []
    if len(subs) > MAX_RATE:
        raise ArrError(
            f"Das Gate „judge“ der Phase „{phase.name}“ soll {len(subs)} Abgaben bewerten; eine "
            f"Bewertungs-Action fasst höchstens {MAX_RATE}. Verteile die Liste auf mehrere "
            "Phasen oder kürze sie."
        )
    text = runner.task_text(phase, None)
    raters: list[Slot] = []
    for rater in gate.raters:
        if gate.action.submission.format == "rating" and not _rate_list(subs, rater):
            continue  # nothing to rate but its own submission
        raters.append(Slot(rater, ACTION_GATE, gate.action, text))
    pass_.raters = raters
    mark = runner.mark()

    async def one(slot: Slot) -> None:
        if ctx.stop_requested:
            return
        if pass_.how == HOW_NORMAL:
            await runner.transition(slot.agent, phase, pass_.attempt)
        await ctx.enter_phase(phase.name, pass_.attempt, [slot.agent])
        rate_ids = _rate_list(subs, slot.agent)
        context = runner.rating_package(phase, pass_, slot, subs, rate_ids, mark)
        found = await runner.act(slot, phase.name, pass_.attempt, context)
        if found is not None:
            slot.found = found

    await together([one(slot) for slot in raters])
    return [s.found for s in raters if s.found is not None]


def ratings_of(reviews: list[Submission], rated: list[Submission]) -> list[dict[str, Any]]:
    """Every rating of the rating submissions: rater, ref, score, comment (only refs of the
    pass that are not the rater's own)."""
    allowed = {s.id: s.agent for s in rated}
    out = []
    for review in reviews:
        entries = review.fields.get("ratings")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            ref, score = entry.get("ref"), entry.get("score")
            if ref not in allowed or allowed[ref] == review.agent:
                continue
            if isinstance(score, bool) or not isinstance(score, int | float):
                continue
            out.append(
                {
                    "rater": review.agent,
                    "ref": ref,
                    "score": float(score),
                    "comment": entry.get("comment")
                    if isinstance(entry.get("comment"), str)
                    else None,
                }
            )
    return out

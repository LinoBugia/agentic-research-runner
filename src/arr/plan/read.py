"""Reading helpers over a resolved `phases` run for the runner (W4b-02a §6.3; docs/10 §4.3).

`expectation(run, phase, agent)` is the expected submission of an agent's action in a phase,
as `context["submission"]` of its assignment (`Expectation` of W4b-01): format, `spec` of an
own format, `name`, the action's identifier and `choices`. What only the running phase knows
(`rate`, `criteria` of the gate if the runner wants others, `known_objections`) the runner
adds. `follow(run, phase, route)` says where a route leads, `enter(run, phase, visits)` which
phase is really entered there when phases are exhausted (the same rule as `arr.plan.graph` and
the estimate use).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from arr.models import (
    Goto,
    ResolvedPhase,
    ResolvedRun,
    ResolvedTask,
    Route,
    RouteKeyword,
)
from arr.plan import graph as graphs
from arr.submissions.formats import BUILTIN, Expectation

__all__ = [
    "ACTION_GATE",
    "ACTION_WORK",
    "Entry",
    "build_expectation",
    "enter",
    "expectation",
    "follow",
]

ACTION_WORK = "work"
"""`Expectation.action` of the action an agent has in a phase (`actions`)."""
ACTION_GATE = "gate"
"""`Expectation.action` of the rating action of a rater at a gate `judge`."""


def build_expectation(
    run: ResolvedRun,
    phase: ResolvedPhase,
    task: ResolvedTask,
    *,
    action: str,
    rate: Sequence[int] | None = None,
    criteria: Sequence[str] | None = None,
    known_objections: Sequence[str] | None = None,
) -> Expectation:
    """The `Expectation` of one action; raises pydantic's `ValidationError` if it cannot be
    built (`arr.plan.check` reports that as B8 before a run starts)."""
    fmt = task.submission.format
    gate_criteria = phase.gate.criteria if task is phase.gate.action else None
    data = {
        "format": fmt,
        "spec": None if fmt in BUILTIN else run.formats[fmt],
        "name": task.submission.name,
        "action": action,
        "choices": task.submission.choices,
        "rate": list(rate) if rate is not None else None,
        "criteria": list(criteria) if criteria is not None else gate_criteria,
        "known_objections": list(known_objections) if known_objections is not None else None,
    }
    return Expectation.model_validate(data)


def expectation(
    run: ResolvedRun,
    phase: str,
    agent: str,
    *,
    rate: Sequence[int] | None = None,
    criteria: Sequence[str] | None = None,
    known_objections: Sequence[str] | None = None,
) -> Expectation:
    """The expected submission of `agent` in `phase`: its action (`action: work`) or, for a
    rater of the gate `judge`, the rating action (`action: gate`; give `rate`, the IDs of the
    submissions to rate). `KeyError` if the phase does not exist or the agent has no action
    there."""
    resolved = run.phase(phase)
    for item in resolved.actions:
        if item.agent == agent:
            return build_expectation(
                run,
                resolved,
                item,
                action=ACTION_WORK,
                rate=rate,
                criteria=criteria,
                known_objections=known_objections,
            )
    if agent in resolved.gate.raters and resolved.gate.action is not None:
        return build_expectation(
            run,
            resolved,
            resolved.gate.action,
            action=ACTION_GATE,
            rate=rate,
            criteria=criteria,
            known_objections=known_objections,
        )
    raise KeyError(agent)


def follow(run: ResolvedRun, phase: str, route: Route) -> str | None:
    """The phase a route of `phase` leads to, None if the run ends there.

    `next`: the following phase (None after the last); `retry`: `phase` itself; `goto`: its
    target; `stop`: None. `escalate` raises `ValueError`: the user decides. Before a route is
    taken, the runner checks `max_visits` of the target and takes its `on_exhausted` instead
    when they are used up (docs/10 §4.7).
    """
    names = [p.name for p in run.phases]
    index = names.index(phase)
    if isinstance(route, Goto):
        return route.goto
    if route is RouteKeyword.NEXT:
        return names[index + 1] if index + 1 < len(names) else None
    if route is RouteKeyword.RETRY:
        return phase
    if route is RouteKeyword.ESCALATE:
        raise ValueError("escalate: der User entscheidet")
    return None


@dataclass(frozen=True)
class Entry:
    """Result of `enter`: the phase that is really entered (`phase`), or the end of the run
    (`phase` None), or the user decides (`ask_user`: an exhausted phase has
    `on_exhausted: escalate`; `phase` is that phase, the user chooses next, once more or
    stop). `exhausted` are the phases passed because their visits were used up, in order."""

    phase: str | None
    ask_user: bool = False
    exhausted: tuple[str, ...] = ()


def enter(run: ResolvedRun, phase: str | None, visits: Mapping[str, int]) -> Entry:
    """A route leads to `phase` (None: the run ends); `visits` counts the visits of every
    phase so far (missing: 0). Which phase is entered? A phase whose `max_visits` are used up
    takes its `on_exhausted` instead, also through several exhausted phases; `next` there is
    the successor of the exhausted phase; `max_visits: 0` is never entered (docs/10 §4.7,
    Entscheidung 40, 45). A choice of the user beyond `max_visits` (Entscheidung 31) is the
    runner's: it enters the phase without asking this function."""
    names = [p.name for p in run.phases]
    if phase is None:
        return Entry(None)
    counts = [visits.get(name, 0) for name in names]
    node, passed = graphs.entry(list(run.phases), names.index(phase), counts)
    exhausted = tuple(names[i] for i in passed)
    if node == graphs.USER:
        return Entry(exhausted[-1], ask_user=True, exhausted=exhausted)
    return Entry(names[node] if node >= 0 else None, exhausted=exhausted)

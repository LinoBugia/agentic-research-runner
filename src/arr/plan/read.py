"""Reading helpers over a resolved `phases` run for the runner (W4b-02a §6.3; docs/10 §4.3).

`expectation(run, phase, agent)` is the expected submission of an agent's action in a phase,
as `context["submission"]` of its assignment (`Expectation` of W4b-01): format, `spec` of an
own format, `name`, the action's identifier and `choices`. What only the running phase knows
(`rate`, `criteria` of the gate if the runner wants others, `known_objections`) the runner
adds. `follow(run, phase, route)` says where a route leads.
"""

from collections.abc import Sequence

from arr.models import (
    Goto,
    ResolvedPhase,
    ResolvedRun,
    ResolvedTask,
    Route,
    RouteKeyword,
)
from arr.submissions.formats import BUILTIN, Expectation

__all__ = ["ACTION_GATE", "ACTION_WORK", "build_expectation", "expectation", "follow"]

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

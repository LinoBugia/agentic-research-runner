"""The phase plan of a group or run with `topology: phases` (W4b-07 §6.2, docs/10 §4.9).

One function builds the rows from a `ResolvedRun`; the skills page, the run detail (tab
„Ablauf“), the run-start card and the tools of Claude Code (`describe_skill`, `estimate_run`)
show the same rows. Columns: phase, topology (with `isolated`, `over`, `repeat`, `rounds`,
`max_visits`, `max_assignments`), actions (key → assignment → format, name), on standby,
channels, context and folder, gate and routes (with `on_exhausted`, `on_tie`) and the decider
(`decider: user` of the phase, of an agent of the phase, else the decider of the run).

Every text from the YAML (paths, format names, route targets, values of `choices`) is made one
short line here; the GUI escapes it, the tools to Claude Code pass it through `agent_text`.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from arr.models import (
    ContextMode,
    Decider,
    FolderMode,
    GateRule,
    GateType,
    Goto,
    PhaseTopology,
    ResolvedPhase,
    ResolvedRun,
    Route,
    RouteKeyword,
)
from arr.resolve import line_of_path
from arr.service.agent_view import line
from arr.service.api_models import PhaseActionInfo, PhasePlan, PhaseRow, PhaseUsage
from arr.service.gui import format as fmt

log = logging.getLogger(__name__)

TEXT_CHARS = 120
"""Longest text from the YAML in one cell (paths, names, values)."""


ROUTE_TEXT = {
    RouteKeyword.NEXT: "weiter",
    RouteKeyword.RETRY: "wiederholen",
    RouteKeyword.STOP: "Stopp",
    RouteKeyword.ESCALATE: "du entscheidest",
}

RULE_TEXT = {
    GateRule.ALL: "alle",
    GateRule.ANY: "einer genügt",
    GateRule.MAJORITY: "Mehrheit",
    GateRule.UNANIMOUS: "einstimmig",
    GateRule.MEAN: "Mittel",
    GateRule.RANK: "Rangliste",
}

TOPOLOGY_WORD = {
    PhaseTopology.SOLO: "solo",
    PhaseTopology.PARALLEL: "parallel",
    PhaseTopology.CHAT: "chat",
    PhaseTopology.MODERATED: "moderated",
}

DECIDER_SHORT = {
    Decider.USER: "du",
    Decider.GATEKEEPER: "Aufsichts-Agent",
    Decider.GATEKEEPER_THEN_USER: "Aufsichts-Agent, bei Zweifel du",
}

GATE_USER_TEXT = "user: weiter, wiederholen mit Hinweis oder Stopp"
OPEN_TEXT = "offen"
DROPPED_TEXT = "entfällt mit diesen Parametern: {names}"


def one_line(value: Any, limit: int = TEXT_CHARS) -> str:
    """`value` as one short line by the cleaning rule of `arr.service.agent_view.line`."""
    return line(value, limit)


def _number(value: float) -> str:
    return fmt.score(value) if value != int(value) else str(int(value))


def route_text(run: ResolvedRun, phase: ResolvedPhase, route: Route | None) -> str:
    """„weiter“, „Ende“ (next after the last phase), „wiederholen“, „Stopp“, „→ revision“."""
    if route is None:
        return "–"
    if isinstance(route, Goto):
        return one_line(route.goto, 64)
    if route is RouteKeyword.NEXT:
        names = [p.name for p in run.phases]
        if phase.name in names and names.index(phase.name) == len(names) - 1:
            return "Ende"
    return ROUTE_TEXT.get(route, one_line(route, 64))


def _routes(run: ResolvedRun, phase: ResolvedPhase) -> str:
    """„done → weiter, not_solved → Stopp“; results with the same route share it."""
    by_target: dict[str, list[str]] = {}
    for result, route in phase.gate.routes.items():
        by_target.setdefault(route_text(run, phase, route), []).append(one_line(result, 40))
    return ", ".join(f"{', '.join(results)} → {target}" for target, results in by_target.items())


def gate_text(run: ResolvedRun, phase: ResolvedPhase) -> str:
    """The gate and its routes as in the table of docs/10 §4.9."""
    gate = phase.gate
    if gate.type is GateType.USER:
        text = GATE_USER_TEXT
    else:
        head = gate.type.value
        details: list[str] = []
        if gate.type is GateType.SIGNAL and gate.field:
            head += f" auf „{one_line(gate.field, 40)}“"
        if gate.type is GateType.JUDGE:
            if gate.raters:
                details.append(", ".join(gate.raters))
            if gate.action is not None:
                details.append(one_line(gate.action.submission.format, 40))
        if gate.rule is not None:
            rule = RULE_TEXT.get(gate.rule, gate.rule.value)
            if gate.rule is GateRule.MEAN and gate.pass_score is not None:
                rule += f" ≥ {_number(gate.pass_score)}"
            details.append(rule)
        if details:
            head += f" ({' · '.join(details)})"
        routes = _routes(run, phase)
        if gate.type is GateType.DONE and set(gate.routes) == {"done"}:
            text = f"done → {route_text(run, phase, gate.routes['done'])}"
        else:
            text = f"{head}: {routes}" if routes else head
    if gate.on_tie is not None:
        text += f"; Gleichstand → {route_text(run, phase, gate.on_tie)}"
    if phase.max_visits is not None and phase.on_exhausted is not None:
        text += f"; danach → {route_text(run, phase, phase.on_exhausted)}"
    return text


def topology_text(phase: ResolvedPhase) -> str:
    """„parallel, isoliert, über „ideen““, „chat, 1 Runde“, „solo, höchstens 2×“."""
    parts = [TOPOLOGY_WORD.get(phase.topology, phase.topology.value)]
    if phase.isolated:
        parts.append("isoliert")
    if phase.over:
        parts.append(f"über „{one_line(phase.over, 40)}“")
    if phase.repeat > 1:
        parts.append(f"{phase.repeat}× wiederholt")
    if phase.topology is PhaseTopology.CHAT and phase.rounds:
        parts.append(f"{phase.rounds} Runde" + ("n" if phase.rounds != 1 else ""))
    if phase.topology is PhaseTopology.MODERATED and phase.max_assignments:
        parts.append(f"höchstens {phase.max_assignments} Aufträge")
    if phase.max_visits is not None:
        parts.append(f"höchstens {phase.max_visits}×")
    return ", ".join(parts)


_FOLLOW = {ContextMode.RESET: FolderMode.FRESH}


def context_text(phase: ResolvedPhase) -> str:
    """„keep“, „reset“ or „judge-1: reset, judge-2: keep“; a folder that does not follow the
    context is named („keep, Ordner frisch“)."""
    agents = phase.participants
    values: dict[str, str] = {}
    for agent in agents:
        context = phase.context.get(agent, ContextMode.KEEP)
        folder = phase.folder.get(agent, _FOLLOW.get(context, FolderMode.KEEP))
        text = context.value
        if folder is not _FOLLOW.get(context, FolderMode.KEEP):
            text += ", Ordner frisch" if folder is FolderMode.FRESH else ", Ordner bleibt"
        values[agent] = text
    distinct = set(values.values())
    if len(distinct) <= 1:
        return next(iter(distinct), ContextMode.KEEP.value)
    return "; ".join(f"{agent}: {text}" for agent, text in values.items())


def decider_of(run: ResolvedRun, phase: ResolvedPhase) -> tuple[str, bool]:
    """Who decides the permission requests in this phase (docs/10 §4.11, P14), and whether the
    phase or one of its agents tightens it to the user."""
    run_text = DECIDER_SHORT.get(run.approvals.decider, run.approvals.decider.value)
    if phase.decider == "user":
        return ("du" if run.approvals.decider is Decider.USER else "du (Phase)"), True
    tightened = []
    for agent_id in phase.participants:
        try:
            if run.agent(agent_id).decider == "user":
                tightened.append(agent_id)
        except KeyError:
            continue
    if tightened:
        if run.approvals.decider is Decider.USER:
            return "du", True
        return f"du für {', '.join(tightened)}; sonst {run_text}", True
    return run_text, False


def _actions(phase: ResolvedPhase) -> list[PhaseActionInfo]:
    by_key: dict[str, PhaseActionInfo] = {}
    for action in phase.actions:
        info = by_key.get(action.key)
        if info is None:
            info = PhaseActionInfo(
                key=one_line(action.key, 64),
                task=one_line(action.task),
                format=one_line(action.submission.format, 64),
                name=one_line(action.submission.name, 64) if action.submission.name else None,
            )
            by_key[action.key] = info
        if action.agent not in info.agents:
            info.agents.append(action.agent)
    return list(by_key.values())


def row_of(
    run: ResolvedRun, phase: ResolvedPhase, lines: Mapping[Any, int] | None = None
) -> PhaseRow:
    """One row of the plan (without the columns of a running run)."""
    decider, user = decider_of(run, phase)
    field = f"phases[{phase.index}]"
    return PhaseRow(
        index=phase.index,
        name=phase.name,
        topology=phase.topology.value,
        topology_text=topology_text(phase),
        actions=_actions(phase),
        standby=list(phase.standby),
        channels=[one_line(c, 64) for c in phase.channels],
        context_text=context_text(phase),
        gate=phase.gate.type.value,
        gate_text=gate_text(run, phase),
        decider_text=decider,
        decider_user=user,
        participants=list(phase.participants),
        field=field,
        line=line_of_path(field, lines) if lines else None,
        max_visits=phase.max_visits,
    )


def dropped_phases(run: ResolvedRun, names: Iterable[str] | None) -> list[str]:
    """Phases of the input that `when` drops with the current parameters (not in `run`)."""
    if names is None:
        return []
    present = {p.name for p in run.phases}
    return [one_line(n, 64) for n in names if isinstance(n, str) and n not in present]


def estimate_texts(run: ResolvedRun) -> tuple[dict[str, str], list[str]]:
    """Turns per phase (`phase_turns` per pass times `most_visits`) and the notes of
    `turn_range`. The upper bound is open when a loop has no bound or the user may repeat a
    phase (`Graph.asks_user`). Never raises: a plan without an estimate is still a plan."""
    from arr.plan import graph as graphs
    from arr.plan.estimate import most_visits, phase_turns, turn_range

    out: dict[str, str] = {}
    notes: list[str] = []
    try:
        graph = graphs.build(run)
        visits = most_visits(graph)
        open_end = visits is None or graph.asks_user()
        for node, phase in enumerate(run.phases):
            least, most, _notes = phase_turns(run, phase)
            if open_end:
                span = f"{least}–{most}" if least != most else f"{most}"
                out[phase.name] = f"{span} je Durchlauf, Obergrenze {OPEN_TEXT}"
                continue
            assert visits is not None
            times = visits[node] if node < len(visits) else 1
            top = most * max(times, 1)
            span = f"{least}–{top}" if least != top else f"{top}"
            out[phase.name] = span + (f" ({times} Durchläufe)" if times > 1 else "")
        notes = list(dict.fromkeys(turn_range(run)[2]))
    except Exception:  # noqa: BLE001 - the plan never fails because of the estimate
        log.warning("Schätzung je Phase nicht möglich", exc_info=True)
    return out, notes


def phase_plan(
    run: ResolvedRun,
    *,
    names: Sequence[str] | None = None,
    lines: Mapping[Any, int] | None = None,
    estimate: bool = True,
) -> PhasePlan | None:
    """The phase plan of `run` (None for the old topologies). `names`: the phase names of the
    input, for the line „entfällt mit diesen Parametern“; `lines`: lines of the `group.yaml`."""
    if not run.phases:
        return None
    rows = [row_of(run, phase, lines) for phase in run.phases]
    notes: list[str] = []
    if estimate:
        texts, notes = estimate_texts(run)
        for row in rows:
            row.estimate_text = texts.get(row.name)
    return PhasePlan(
        rows=rows,
        dropped=dropped_phases(run, names),
        notes=[one_line(n, 400) for n in notes],
        run_decider=DECIDER_SHORT.get(run.approvals.decider, run.approvals.decider.value),
    )


def dropped_line(plan: PhasePlan) -> str | None:
    """„entfällt mit diesen Parametern: a, b“ or None."""
    if not plan.dropped:
        return None
    return DROPPED_TEXT.format(names=", ".join(plan.dropped))


# --------------------------------------------------------------------------------------------
# Progress of a run (tab „Ablauf“): passes, gate result, usage
# --------------------------------------------------------------------------------------------


CHOICE_TEXT = {"retry": "noch ein Versuch", "continue": "weiter", "end": "beendet"}
"""What the user chose at a gate `user` or an escalation (route checkpoint `choice`)."""


def gate_result_text(data: Mapping[str, Any]) -> str | None:
    """The result of a `gate` checkpoint: its `value`, with the score at a gate with one."""
    value = data.get("value")
    score = data.get("score")
    text = one_line(value, 40) if isinstance(value, str) and value else None
    if isinstance(score, int | float) and not isinstance(score, bool):
        number = fmt.score(float(score))
        text = f"{text} · {number}" if text else number
    if text is None and data.get("decision") == "escalated":
        return "bei dir"
    return text


def progress(
    plan: PhasePlan, entries: Iterable[Mapping[str, Any]], usage: Mapping[str, PhaseUsage]
) -> PhasePlan:
    """The plan with the passes so far, the result of the last gate and the usage per phase."""
    visits: dict[str, int] = {}
    results: dict[str, str | None] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("kind") != "checkpoint":
            continue
        data = entry.get("data")
        if not isinstance(data, Mapping):
            continue
        stage = data.get("stage")
        if not isinstance(stage, str):
            continue
        if data.get("phase") == "pass_start":
            attempt = data.get("attempt")
            count = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
            visits[stage] = max(visits.get(stage, 0) + 1, count)
        elif data.get("phase") == "gate":
            results[stage] = gate_result_text(data)
        elif data.get("phase") == "route" and str(data.get("choice")) in CHOICE_TEXT:
            results[stage] = CHOICE_TEXT[str(data.get("choice"))]  # the user's choice
    rows = [
        row.model_copy(
            update={
                "visits": visits.get(row.name, 0),
                "gate_result": results.get(row.name),
                "usage": usage.get(row.name),
            }
        )
        for row in plan.rows
    ]
    return plan.model_copy(update={"rows": rows})


def as_dict(plan: PhasePlan) -> dict[str, Any]:
    """The plan for Claude Code: plain values; the caller passes texts through `agent_text`."""
    return plan.model_dump(mode="json", exclude_none=True)


def task_stem(task: str) -> str:
    """The assignment as in the table of docs/10 §4.9: the file name without the extension."""
    return one_line(PurePosixPath(task).stem or task, 64)

"""Fewest and most turns of a resolved run, without a model (docs/08 §5.4 „Schätzung“,
docs/10 §4.7, W4b-02a §6.6).

`turn_range(resolved)` gives the fewest turns, the most (None: open) and German notes. The
service (`arr.service.real_api.runs.estimate_run`) builds the estimate shown to the user from
it: costs, duration and the cap `turns_per_agent` × agents.

The five old topologies are counted as before W4b-02a (moved here from the service unchanged).
A `phases` run is counted per pass of a phase: every action at least one turn and at most
`turns` plus one asking again, times `repeat`, with `over` times the entries of the list (known
from a `seed`, else the most a `list` may hold), one rating action per rater of a gate `judge`
(also asked once more), a discussion for `chat` (rounds or wake-ups, new in every pass,
Entscheidung 13), the assignments of the lead for `moderated` and the wake-ups of agents on
standby. The upper bound counts every pass `max_visits` allows, also for phases that only repeat
through a loop with another bounded phase, and is never below a possible run (`arr.plan.graph`).
With a gate `user` or a route `escalate`, the user may repeat beyond `max_visits` (Entscheidung
31): the upper bound is open. The lower bound is the cheapest way from the start to the end.
"""

import fnmatch
import heapq
from typing import Any

from arr.models import (
    ChannelMode,
    GateType,
    PhaseTopology,
    ResolvedPhase,
    ResolvedRun,
    TopologyKind,
)
from arr.plan import graph as graphs
from arr.submissions.formats import DEFAULT_LIST_ITEMS

__all__ = ["phase_turns", "turn_range"]

ASSIGNMENT_TURNS = 2
"""Most turns of one assignment of a lead in `moderated`: one turn and one asking again."""


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return int(value)


def turn_range(resolved: ResolvedRun) -> tuple[int, int | None, list[str]]:
    """Fewest and most turns the topology itself gives (None: open), with German notes.

    Follows the topologies (docs/03 §4): `solo` asks again up to `max_turns`; `roundtable`
    has one turn per member and round (`event`: kick-off, at most `max_wakeups_per_agent`
    wake-ups and `rounds` idle rounds) plus the summarizer; `waterfall` one attempt per stage
    (author + raters, each asked once more at most), up to `1 + max_rework` attempts;
    `review` and `exploration` see `_w4_03_turns`; `phases` see `_phases_turns`.
    """
    params = resolved.params
    agents = len(resolved.agents)
    kind = resolved.topology.kind
    if kind is TopologyKind.SOLO:
        most = max(1, _int(params.get("max_turns"), 3))
        if params.get("require_result") is False:
            most = 1
        return 1, most, []
    if kind is TopologyKind.ROUNDTABLE:
        rounds = max(1, _int(params.get("rounds"), 3))
        name = params.get("channel")
        if name is None and len(resolved.groupchats) == 1:
            name = resolved.groupchats[0].name
        try:
            channel = resolved.channel(str(name))
        except KeyError:
            return agents, None, []
        members = len(channel.members)
        extra = 1 if params.get("summarizer") else 0
        if channel.mode is ChannelMode.ROUNDS:
            return members + extra, members * rounds + extra, []
        wakeups = channel.max_wakeups_per_agent
        return (
            members + extra,
            members * (1 + wakeups + rounds) + extra,
            ["Ereignisgesteuert: Wie oft Agenten geweckt werden, hängt von der Diskussion ab."],
        )
    if kind is TopologyKind.WATERFALL:
        rework = max(0, _int(params.get("max_rework"), 2))
        least = most = 0
        for stage in resolved.stages:
            raters = sum(1 for r in resolved.raters if r != stage.agent)
            least += 1 + raters
            most += (1 + rework) * (2 + 2 * raters)
        notes = []
        if str(params.get("on_fail") or "continue") == "escalate":
            notes.append("Nach einer Eskalation kann es weitere Versuche geben.")
            return least, None, notes
        return least, most, notes
    if kind in (TopologyKind.REVIEW, TopologyKind.EXPLORATION):
        return _w4_03_turns(resolved)
    if kind is TopologyKind.PHASES:
        return _phases_turns(resolved)
    return agents, None, []


def _discussion_turns(resolved: ResolvedRun, name: Any, members: int, rounds: int) -> int:
    """Most turns of one discussion phase of `members` agents (see `turn_range`)."""
    try:
        channel = resolved.channel(str(name))
    except KeyError:
        return members * rounds
    if channel.mode is ChannelMode.ROUNDS:
        return members * rounds
    return members * (1 + channel.max_wakeups_per_agent + rounds)


def _w4_03_turns(resolved: ResolvedRun) -> tuple[int, int | None, list[str]]:
    """Fewest and most turns of `review` and `exploration` (docs/03 §4.3, §4.5, W4-03).

    `review`: the author writes the artifact (unless `artifact`, 1 + 1 asking again), per
    round every judge reviews (+ asking again) and, with two or more judges, discusses
    `discussion_rounds`; the author revises up to `max_revisions` times; a summarizer adds
    one. `exploration`: the ideator (unless `ideas`, 1 + 1), `turns_per_idea` per idea (+ one
    asking again), every ranker once (+ once again), a summarizer one.
    """
    params = resolved.params
    ids = [a.id for a in resolved.agents]
    extra = 1 if params.get("summarizer") else 0
    if resolved.topology.kind is TopologyKind.REVIEW:
        name = params.get("channel") or "judges"
        try:
            members = list(resolved.channel(str(name)).members)
        except KeyError:
            members = []
        author = params.get("author") or next(
            (a.id for a in resolved.agents if "author" in (a.id, a.base_id)), None
        )
        judges = len([m for m in members if m not in (author, params.get("summarizer"))])
        revisions = max(0, _int(params.get("max_revisions"), 2))
        rounds = max(0, _int(params.get("discussion_rounds"), 1))
        writes = 0 if params.get("artifact") else 1
        talk = _discussion_turns(resolved, name, judges, rounds) if judges > 1 else 0
        least = writes + judges + (judges * min(rounds, 1) if judges > 1 else 0) + extra
        most = 2 * writes + (1 + revisions) * (2 * judges + talk) + 2 * revisions + extra
        notes = ["Ein Review endet früher, wenn die Judges das Artefakt annehmen."]
        return least, most, notes
    ideas = params.get("ideas")
    n_ideas = len(ideas) if isinstance(ideas, list) and ideas else _int(params.get("n_ideas"), 3)
    per_idea = max(1, _int(params.get("turns_per_idea"), 1))
    ideator = 0 if isinstance(ideas, list) and ideas else 1
    rankers = params.get("rankers")
    if isinstance(rankers, str):
        rankers = [rankers]
    patterns = rankers if isinstance(rankers, list) else ["ranker*"]
    ranker_count = len([i for i in ids if any(fnmatch.fnmatchcase(i, p) for p in patterns)])
    least = ideator + n_ideas * per_idea + ranker_count + extra
    most = 2 * ideator + n_ideas * (per_idea + 1) + 2 * ranker_count + extra
    return least, most, []


# --------------------------------------------------------------------------------------------
# `topology: phases` (W4b-02a §6.6)
# --------------------------------------------------------------------------------------------


def _wakeups(resolved: ResolvedRun, phase: ResolvedPhase, agent: str) -> int:
    """Most wake-ups of an agent on standby: per open channel it is in, the channel's limit
    (counted anew in every pass, Entscheidung 13)."""
    total = 0
    for name in phase.channels:
        try:
            channel = resolved.channel(name)
        except KeyError:
            continue
        if agent in channel.members:
            total += channel.max_wakeups_per_agent
    return total


def phase_turns(resolved: ResolvedRun, phase: ResolvedPhase) -> tuple[int, int, list[str]]:
    """Fewest and most turns of one pass of `phase`, with German notes."""
    notes: list[str] = []
    agents = phase.agents
    turns = {a.agent: a.turns for a in phase.actions}
    if phase.topology is PhaseTopology.CHAT:
        name = phase.channels[0] if phase.channels else None
        talk = _discussion_turns(resolved, name, len(agents), phase.rounds or 1)
        least, most = len(agents), talk + len(agents)
    elif phase.topology is PhaseTopology.MODERATED:
        lead = max(turns.values(), default=1) + 1
        least = 1
        most = lead + (phase.max_assignments or 0) * ASSIGNMENT_TURNS
    elif phase.over is not None:
        seed = resolved.seed.get(phase.over)
        known = seed.items if seed is not None and isinstance(seed.items, list) else None
        entries = len(known) if known is not None else DEFAULT_LIST_ITEMS
        if known is None:
            notes.append(
                f"Phase „{phase.name}“: Die Zahl der Einträge von „{phase.over}“ steht erst im "
                f"Lauf fest; gerechnet mit höchstens {DEFAULT_LIST_ITEMS}."
            )
        per = max(turns.values(), default=1) + 1
        least = (len(known) if known is not None else 1) * phase.repeat
        most = entries * phase.repeat * per
    else:
        least = len(agents) * phase.repeat
        most = sum(t + 1 for t in turns.values()) * phase.repeat
    most += sum(_wakeups(resolved, phase, agent) for agent in phase.standby)
    if phase.gate.type is GateType.JUDGE and phase.gate.action is not None:
        raters = len(phase.gate.raters)
        least += raters
        most += raters * (phase.gate.action.turns + 1)
    return least, most, notes


def _phases_turns(resolved: ResolvedRun) -> tuple[int, int | None, list[str]]:
    phases = resolved.phases
    if not phases:
        return 0, 0, []
    graph = graphs.build(resolved)
    costs = [phase_turns(resolved, p) for p in phases]
    notes = [note for _least, _most, found in costs for note in found]
    least = _cheapest(graph, [c[0] for c in costs])
    if graph.asks_user():
        notes.append(
            "Ein Gate „user“ oder „escalate“ lässt dich Phasen auch über „max_visits“ hinaus "
            "wiederholen; die Obergrenze ist offen."
        )
        return least, None, notes
    visits = most_visits(graph)
    if visits is None:
        notes.append("Eine Schleife hat keine Grenze; die Obergrenze ist offen.")
        return least, None, notes
    most = sum(v * c[1] for v, c in zip(visits, costs, strict=True))
    if any(p.max_visits is not None for p in phases):
        notes.append("Schleifen: gezählt ist jeder Durchlauf, den „max_visits“ zulässt.")
    return least, most, notes


def most_visits(graph: graphs.Graph) -> list[int] | None:
    """The most visits of every phase (W4b-02a §6.6), or None if a loop has no bound.

    A bounded phase is visited at most `max_visits` times. A phase without `max_visits` at
    most once per entry: from the start and from every visit of a phase with a route to it,
    exhausted phases followed through. Phases without bound form no loop (L2), so this is
    one pass in topological order.
    """
    size = graph.size
    free = [i for i in range(size) if not graph.bounded(i)]
    preds: dict[int, list[int]] = {i: [] for i in free}
    for node in range(size):
        for nxt in graph.auto_edges(node):
            if nxt in preds:
                preds[nxt].append(node)
    order = _topological(free, preds)
    if order is None:
        return None
    visits = [graph.limit(i) if graph.bounded(i) else 0 for i in range(size)]
    start = graph.start()
    for node in order:
        visits[node] = (1 if node in start else 0) + sum(visits[p] for p in preds[node])
    return visits


def _topological(free: list[int], preds: dict[int, list[int]]) -> list[int] | None:
    inside = set(free)
    indegree = {i: sum(1 for p in preds[i] if p in inside) for i in free}
    succ: dict[int, list[int]] = {i: [] for i in free}
    for node in free:
        for pred in preds[node]:
            if pred in inside:
                succ[pred].append(node)
    ready = [i for i in free if indegree[i] == 0]
    order = []
    while ready:
        node = ready.pop()
        order.append(node)
        for nxt in succ[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    return order if len(order) == len(free) else None


CHEAPEST_STATES = 20_000
"""States the exact search for the fewest turns looks at before it takes the looser bound."""


def _cheapest(graph: graphs.Graph, least: list[int]) -> int:
    """The fewest turns from the start to the end (W4b-02a §6.6), never above a possible run.

    Exact: Dijkstra over states (phase, visits of the bounded phases), a visit costs the
    fewest turns of one pass, an exhausted phase sends on by its `on_exhausted`, the user may
    stop where it is asked. If that needs more than `CHEAPEST_STATES` states, the looser
    bound of `_relaxed` stands in.
    """
    exact = _exact(graph, least)
    return exact if exact is not None else _relaxed(graph, least)


def _attempt(
    graph: graphs.Graph, node: int, visits: tuple[int, ...]
) -> list[tuple[int, tuple[int, ...]]]:
    """Where an attempt to enter `node` leads with these visits (`graph.entry`, the same rule
    as the runner's): `(phase, visits after)` for a visit, `(END, visits)` for the end or where
    the user may stop after `escalate`."""
    if node < 0:
        return [(graphs.END, visits)]
    entered, _passed = graphs.entry(graph.phases, node, visits)
    if entered < 0:
        return [(graphs.END, visits)]  # the end, or the user may stop
    return [(entered, visits[:entered] + (visits[entered] + 1,) + visits[entered + 1 :])]


def _exact(graph: graphs.Graph, least: list[int]) -> int | None:
    start = (0,) * graph.size
    heap: list[tuple[int, int, tuple[int, ...]]] = []
    for node, visits in _attempt(graph, 0, start):
        heapq.heappush(heap, (least[node] if node >= 0 else 0, node, visits))
    done: set[tuple[int, tuple[int, ...]]] = set()
    while heap:
        cost, node, visits = heapq.heappop(heap)
        if node < 0:
            return cost
        if (node, visits) in done:
            continue
        done.add((node, visits))
        if len(done) > CHEAPEST_STATES:
            return None
        if graph.user[node]:
            heapq.heappush(heap, (cost, graphs.END, visits))
        for target in graph.auto[node] | graph.user[node]:
            for nxt, after in _attempt(graph, target, visits):
                if nxt < 0:
                    heapq.heappush(heap, (cost, graphs.END, after))
                elif (nxt, after) not in done:
                    heapq.heappush(heap, (cost + least[nxt], nxt, after))
    return None


def _relaxed(graph: graphs.Graph, least: list[int]) -> int:
    """A bound that never exceeds a possible run: as `_exact`, but any bounded phase may be
    exhausted at once (its `on_exhausted` costs nothing)."""
    best: dict[int, int] = {}
    heap: list[tuple[int, int]] = []
    for node in graph.start():
        heapq.heappush(heap, (least[node] if node >= 0 else 0, node))
    while heap:
        cost, node = heapq.heappop(heap)
        if node < 0:
            return cost
        if node in best:
            continue
        best[node] = cost
        for nxt in graph.all_edges(node) | ({graphs.END} if graph.user[node] else set()):
            if nxt < 0:
                heapq.heappush(heap, (cost, graphs.END))
            elif nxt not in best:
                heapq.heappush(heap, (cost + least[nxt], nxt))
    return min(best.values(), default=0)

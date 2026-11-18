"""The routes of a resolved `phases` run as a graph (docs/10 §4.7, W4b-02a §6.4 L1–L4, §6.6).

Nodes are the phases of `ResolvedRun.phases` by position. A route leads to a phase or ends the
run (`END`); `escalate` and a gate `user` hand the choice to the user (`USER`). Entering a phase
whose `max_visits` are used up takes its `on_exhausted` instead (docs/10 §4.7), possibly through
a chain of such phases: the graph adds these redirects as edges of their own, because a loop
that only runs through exhausted phases visits no bounded phase and never ends.

Pure functions over the resolved run, used by `arr.plan.check` and `arr.plan.estimate`.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from arr.models import GateType, Goto, ResolvedPhase, ResolvedRun, Route, RouteKeyword

__all__ = [
    "END",
    "USER",
    "Graph",
    "build",
    "exhaust_cycle",
    "reachable",
    "target",
    "unbounded_cycle",
]

END = -1
"""The run ends: `stop`, `next` after the last phase, a goto to a phase that does not exist."""
USER = -2
"""The user decides (route `escalate`, gate `user`): next, once more or stop."""


@dataclass
class Graph:
    """Edges of a resolved run.

    `auto[i]`: targets of the routes arr takes without the user (gate routes and `on_tie`);
    `user[i]`: what the user may choose after phase `i` (gate `user`, `escalate`): next, the
    phase again, stop; `redirect[j]`: where an entry into the exhausted phase `j` leads
    (`on_exhausted`, followed through further exhausted phases; empty without `max_visits`);
    `chain[j]`: the bounded phases that chain passes through, `j` first.
    """

    phases: list[ResolvedPhase]
    auto: list[set[int]] = field(default_factory=list)
    user: list[set[int]] = field(default_factory=list)
    redirect: list[set[int]] = field(default_factory=list)
    chain: list[list[int]] = field(default_factory=list)

    @property
    def size(self) -> int:
        """Number of phases."""
        return len(self.phases)

    def bounded(self, node: int) -> bool:
        """True if the phase has `max_visits`."""
        return self.phases[node].max_visits is not None

    def limit(self, node: int) -> int:
        """`max_visits` of a bounded phase."""
        visits = self.phases[node].max_visits
        assert visits is not None
        return visits

    def entries(self, target: int, *, with_user: bool = False) -> set[int]:
        """Where an attempt to enter `target` can lead: the phase itself (unless it has
        `max_visits: 0`) and, if it is bounded, where its redirects lead. With `with_user`, an
        `escalate` among the redirects becomes what the user may choose there."""
        if target < 0:
            return {target}
        out = set() if self.phases[target].max_visits == 0 else {target}
        if self.bounded(target):
            out |= self.redirect[target]
            if with_user:
                for chained in self.chain[target]:
                    if self.phases[chained].on_exhausted is RouteKeyword.ESCALATE:
                        out |= self._choices(chained)
        return out

    def _choices(self, node: int) -> set[int]:
        """What the user may choose at phase `node`: the next phase, `node` again, stop."""
        return {node + 1 if node + 1 < self.size else END, node, END}

    def auto_edges(self, node: int) -> set[int]:
        """Every phase (or `END`, `USER`) a visit of `node` can lead to without the user,
        exhausted phases followed through."""
        out: set[int] = set()
        for target in self.auto[node]:
            out |= self.entries(target)
        return out

    def all_edges(self, node: int) -> set[int]:
        """As `auto_edges`, plus every choice of the user (gate `user`, `escalate`)."""
        out: set[int] = set()
        for target in self.auto[node] | self.user[node]:
            out |= self.entries(target, with_user=True)
        return out - {USER}

    def asks_user(self) -> bool:
        """True if a gate `user` or a route `escalate` occurs (also as `on_exhausted`): the
        user may repeat a phase beyond `max_visits` (Entscheidung 31)."""
        return any(
            self.user[i]
            or (self.bounded(i) and self.phases[i].on_exhausted is RouteKeyword.ESCALATE)
            for i in range(self.size)
        )

    def start(self) -> set[int]:
        """Where the run begins: the first phase, or its redirect."""
        return self.entries(0) if self.phases else {END}


def target(phases: list[ResolvedPhase], node: int, route: Route) -> int:
    """The phase a route of phase `node` leads to, `END` or `USER`."""
    if isinstance(route, Goto):
        names = [p.name for p in phases]
        return names.index(route.goto) if route.goto in names else END
    if route is RouteKeyword.NEXT:
        return node + 1 if node + 1 < len(phases) else END
    if route is RouteKeyword.RETRY:
        return node
    if route is RouteKeyword.ESCALATE:
        return USER
    return END


def _routes(phase: ResolvedPhase) -> Iterator[Route]:
    yield from phase.gate.routes.values()
    if phase.gate.on_tie is not None:
        yield phase.gate.on_tie


def build(run: ResolvedRun) -> Graph:
    """The graph of the phases of `run` (see `Graph`)."""
    phases = list(run.phases)
    graph = Graph(phases)
    for node, phase in enumerate(phases):
        auto = {target(phases, node, route) for route in _routes(phase)}
        user: set[int] = set()
        if phase.gate.type is GateType.USER or USER in auto:
            user = graph._choices(node)
        graph.auto.append(auto - {USER})
        graph.user.append(user)
    for node in range(len(phases)):
        chain, out = _redirects(phases, node)
        graph.chain.append(chain)
        graph.redirect.append(out)
    return graph


def _redirects(phases: list[ResolvedPhase], node: int) -> tuple[list[int], set[int]]:
    """The chain of `on_exhausted` from phase `node` through further phases with
    `max_visits` (they may be exhausted too), and where it can lead. A chain that comes back
    is cut (L3 reports it)."""
    out: set[int] = set()
    chain = [node]
    todo = [node]
    while todo:
        current = todo.pop()
        phase = phases[current]
        if phase.max_visits is None or phase.on_exhausted is None:
            continue
        nxt = target(phases, current, phase.on_exhausted)
        out.add(nxt)
        if nxt >= 0 and phases[nxt].max_visits is not None and nxt not in chain:
            chain.append(nxt)
            todo.append(nxt)
    return chain, out


def exhaust_cycle(phases: list[ResolvedPhase], node: int) -> list[int] | None:
    """The phases of a chain of `on_exhausted` from `node` that comes back to a phase of the
    chain (L3), or None. Only bounded phases chain on."""
    path = [node]
    current = node
    while True:
        phase = phases[current]
        if phase.max_visits is None or phase.on_exhausted is None:
            return None
        nxt = target(phases, current, phase.on_exhausted)
        if nxt < 0:
            return None
        if nxt in path:
            return path[path.index(nxt) :]
        path.append(nxt)
        current = nxt


def unbounded_cycle(graph: Graph) -> list[int] | None:
    """A loop without a phase with `max_visits` among the edges arr takes alone (L2), as the
    list of its phases, or None. Iterative depth-first search, linear in the graph."""
    free = [i for i in range(graph.size) if not graph.bounded(i)]
    allowed = set(free)
    color = dict.fromkeys(free, 0)  # 0 new, 1 on the stack, 2 done
    for root in free:
        if color[root]:
            continue
        stack: list[tuple[int, list[int]]] = [(root, sorted(graph.auto_edges(root) & allowed))]
        path = [root]
        color[root] = 1
        while stack:
            node, todo = stack[-1]
            if not todo:
                color[node] = 2
                stack.pop()
                path.pop()
                continue
            nxt = todo.pop()
            if color[nxt] == 1:
                return path[path.index(nxt) :]
            if color[nxt] == 0:
                color[nxt] = 1
                path.append(nxt)
                stack.append((nxt, sorted(graph.auto_edges(nxt) & allowed)))
    return None


def reachable(graph: Graph) -> set[int]:
    """Phases a run can reach from its start, over every route (the user's included). A
    phase with `max_visits: 0` counts as reached when a route leads to it, although it is
    never entered (Entscheidung 40)."""
    reached: set[int] = set()
    visited: set[int] = set()
    attempts = [0] if graph.phases else []
    while attempts:
        node = attempts.pop()
        if node < 0:
            continue
        reached.add(node)
        targets = graph.entries(node, with_user=True)
        for nxt in targets - {node}:
            if nxt >= 0 and nxt not in reached:
                attempts.append(nxt)
        if node in targets and node not in visited:
            visited.add(node)
            for target in graph.auto[node] | graph.user[node]:
                if target >= 0 and (target not in reached or target not in visited):
                    attempts.append(target)
    return reached

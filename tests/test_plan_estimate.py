"""W4b-02a A10: the estimate of `phases` runs with loops (docs/10 §4.7, W4b-02a §6.6).

The old topologies are covered unchanged by the existing estimate tests (`test_skill_editor`).
Here: the passes of a phase, loops counted by `max_visits`, an open upper bound with the user,
and many small generated graphs whose every possible run is walked: the upper bound is never
below the longest run, the lower bound never above the shortest. No model, no run.
"""

import functools
import random
from typing import Any

from plan_kit import findings, load, resolve

from arr.models import Goto, ResolvedRun, RouteKeyword
from arr.plan.check import check
from arr.plan.estimate import phase_turns, turn_range
from arr.plan.graph import END, target
from arr.service.real_api.runs import estimate_run


def test_a10_passes_of_phases() -> None:
    resolved = resolve(load("exploration"), {"ideas": ["a", "b", "c"]})
    exploration = resolved.phase("exploration")
    # 3 ideas × repeat 2, each 1 turn and one asking again; one ranker rates once (+ once)
    assert phase_turns(resolved, exploration) == (3 * 2 + 1, 3 * 2 * 2 + 2, [])
    least, most, notes = turn_range(resolved)
    assert (least, most) == (8, 16) and notes == []
    unknown = resolve(load("exploration"))
    _least, _most, notes = phase_turns(unknown, unknown.phase("exploration"))
    assert "höchstens 50" in notes[0]


def test_a10_waterfall_as_phases_counts_like_the_old_topology() -> None:
    # old `waterfall` with 3 stages, 2 raters, max_rework 1: 9 to 36 turns
    least, most, notes = turn_range(resolve(load("waterfall")))
    assert (least, most) == (9, 36)
    assert any("max_visits" in n for n in notes)
    least, most, _ = turn_range(resolve(load("waterfall"), {"on_fail": "escalate"}))
    # escalate: the user may repeat, the bound is open; or stop after two failed attempts of
    # the first stage (2 × (1 + 2 raters)), which the old estimate did not count
    assert (least, most) == (6, None)
    assert turn_range(resolve(load("waterfall"), {"on_fail": "stop"}))[:2] == (6, 36)


def test_a10_loop_through_another_bounded_phase() -> None:
    resolved = resolve(load("review"))
    least, most, _ = turn_range(resolved)
    # fassung 2, pruefung and diskussion visited 1 + 1 (revision max_visits 1) times, 4 each
    assert (least, most) == (5, 2 + 2 * 4 + 2 * 4 + 2)
    assert turn_range(resolve(load("review"), {"max_revisions": 0}))[1] == 2 + 4 + 4


def test_a10_gate_user_leaves_the_upper_bound_open_and_the_service_caps_it() -> None:
    resolved = resolve(load())
    least, most, notes = turn_range(resolved)
    assert most is None and any("Gate „user“" in n for n in notes)
    assert least == 1, "the user may stop after the first phase"
    estimate = estimate_run(resolved)
    assert estimate.turns.max == len(resolved.agents) * 40  # turns_per_agent of arr's limits
    assert not estimate.turns_open


# --------------------------------------------------------------------------------------------
# Generated graphs: every possible run walked
# --------------------------------------------------------------------------------------------

ROUTES = ["next", "stop", "retry", "goto"]


def _route(rng: random.Random, names: list[str], bounded: bool) -> Any:
    kind = rng.choice(ROUTES if bounded else ["next", "stop", "goto"])
    return {"goto": rng.choice(names)} if kind == "goto" else kind


def _graph(rng: random.Random) -> dict[str, Any]:
    size = rng.randint(2, 5)
    names = [f"p{i}" for i in range(size)]
    phases = []
    for name in names:
        visits = rng.choice([None, None, 0, 1, 2, 3])
        bounded = visits is not None
        entry: dict[str, Any] = {
            "name": name,
            "topology": "solo",
            "actions": {
                "a": {"task": "t.md", "submission": {"format": "text"}, "turns": rng.randint(1, 3)}
            },
        }
        if bounded:
            entry["max_visits"] = visits
            entry["on_exhausted"] = _route(rng, names, False)
        if rng.random() < 0.5:
            entry["gate"] = {"type": "done", "routes": {"done": _route(rng, names, bounded)}}
        else:
            entry["gate"] = {
                "type": "signal",
                "field": "status",
                "routes": {
                    "done": _route(rng, names, bounded),
                    "not_solved": _route(rng, names, bounded),
                },
            }
        phases.append(entry)
    return {"topology": "phases", "agents": [{"id": "a"}], "phases": phases}


def _walk(resolved: ResolvedRun) -> tuple[int, int]:
    """Fewest and most turns over every run the routes allow, visits counted as arr does."""
    phases = resolved.phases
    costs = [phase_turns(resolved, p)[:2] for p in phases]

    @functools.cache
    def enter(node: int, visits: tuple[int, ...], depth: int) -> tuple[int, int]:
        assert depth < 500, "a run that never ends"
        if node == END:
            return 0, 0
        phase = phases[node]
        if phase.max_visits is not None and visits[node] >= phase.max_visits:
            assert phase.on_exhausted is not None
            return enter(target(phases, node, phase.on_exhausted), visits, depth + 1)
        after = visits[:node] + (visits[node] + 1,) + visits[node + 1 :]
        routes = list(phase.gate.routes.values())
        results = [enter(target(phases, node, r), after, depth + 1) for r in routes]
        least, most = costs[node]
        return least + min(r[0] for r in results), most + max(r[1] for r in results)

    return enter(0, (0,) * len(phases), 0)


def test_a10_upper_bound_never_below_a_possible_run() -> None:
    rng = random.Random(4202)
    checked = loops = 0
    for _ in range(600):
        group = _graph(rng)
        found = findings(group)
        if found.errors or found.resolved is None:
            continue
        resolved = found.resolved
        assert check(resolved).ok
        shortest, longest = _walk(resolved)
        least, most, _notes = turn_range(resolved)
        assert most is not None
        assert most >= longest, (group, most, longest)
        assert least <= shortest, (group, least, shortest)
        checked += 1
        routes = [r for p in resolved.phases for r in p.gate.routes.values()]
        if any(r is RouteKeyword.RETRY or isinstance(r, Goto) for r in routes):
            loops += 1
    assert checked >= 100 and loops >= 50, (checked, loops)

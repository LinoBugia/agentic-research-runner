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

from arr.models import DEFAULT_TURNS, Goto, ResolvedRun
from arr.plan.estimate import phase_turns, turn_range
from arr.plan.graph import END
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
# Generated groups: every possible run walked, costs computed here from the input (W4b-02a.1)
# --------------------------------------------------------------------------------------------

WAKE = 2
"""`max_wakeups_per_agent` of the two channels of the generated groups."""
CHANNELS = [
    {"name": "rund", "members": ["a", "b", "s"], "mode": "rounds", "max_wakeups_per_agent": WAKE},
    {"name": "los", "members": ["a", "b", "s"], "mode": "event", "max_wakeups_per_agent": WAKE},
]
LIST_MOST = 50


def _act(turns: int, fmt: str = "text", name: str | None = None) -> dict[str, Any]:
    sub: dict[str, Any] = {"format": fmt}
    if name:
        sub["name"] = name
    return {"task": "t.md", "submission": sub, "turns": turns}


def _pick_route(rng: random.Random, names: list[str], retry: bool) -> Any:
    kinds = ["next", "stop", "goto", "goto"] + (["retry"] if retry else [])
    if rng.random() < 0.08:
        kinds.append("escalate")
    kind = rng.choice(kinds)
    return {"goto": rng.choice(names)} if kind == "goto" else kind


def _make(rng: random.Random) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    """A group with all four topologies, `over`, `repeat`, sleepers, gates `done`, `signal`,
    `judge`, `user`, `escalate` and `max_visits` 0–3, and the fewest and most turns of one
    pass of every phase, computed here by W4b-02a §6.6."""
    size = rng.randint(1, 5)
    names = [f"p{i}" for i in range(1, size + 1)]
    known = rng.choice([None, 2, 3])
    phases: list[dict[str, Any]] = [
        {"name": "p0", "topology": "solo", "actions": {"c": _act(1, "list", "liste")}}
    ]
    costs = [(1, 2)]
    for name in names:
        topo = rng.choice(["solo", "parallel", "parallel", "chat", "moderated"])
        entry: dict[str, Any] = {"name": name, "topology": topo}
        if topo == "solo":
            turns, rep = rng.randint(1, 3), rng.choice([1, 1, 2])
            entry["actions"] = {"a": _act(turns)}
            entry["repeat"] = rep
            least, most = rep, rep * (turns + 1)
        elif topo == "parallel":
            turns = {x: rng.randint(1, 3) for x in ("a", "b")}
            rep = rng.choice([1, 2])
            entry["actions"] = {x: _act(t) for x, t in turns.items()}
            entry["repeat"] = rep
            if rng.random() < 0.35:
                entry["over"] = "ideen" if known else "liste"
                lo, hi = (known, known) if known else (1, LIST_MOST)
                least, most = lo * rep, hi * rep * (max(turns.values()) + 1)
            else:
                least, most = 2 * rep, rep * sum(t + 1 for t in turns.values())
        elif topo == "chat":
            channel = rng.choice(["rund", "los"])
            rounds = rng.choice([None, 1, 2])
            entry["channels"] = [channel]
            if rounds:
                entry["rounds"] = rounds
            entry["actions"] = {"a": _act(1), "b": _act(1)}
            r = rounds or 3
            talk = 2 * r if channel == "rund" else 2 * (1 + WAKE + r)
            least, most = 2, talk + 2
        else:
            turns, jobs = rng.randint(1, 3), rng.randint(1, 3)
            entry["actions"] = {"a": _act(turns)}
            entry["standby"] = ["b"]
            entry["max_assignments"] = jobs
            # W4b-04 §6.8: per assignment its turns (DEFAULT_TURNS + 1) and a return turn
            least, most = 1, turns + 1 + jobs * (DEFAULT_TURNS + 1 + 1)
        if topo != "chat" and rng.random() < 0.3:
            entry["channels"] = [rng.choice(["rund", "los"])]
            if topo != "moderated":
                entry["standby"] = ["s"]
            most += WAKE  # the sleeper is in the open channel
        visits = rng.choice([None, None, None, 0, 1, 2, 3])
        if visits is not None:
            entry["max_visits"] = visits
            entry["on_exhausted"] = _pick_route(rng, names, False)
        bounded = visits is not None
        kind = rng.random()
        many = len(entry["actions"]) > 1 or "over" in entry
        if kind < 0.3:
            entry["gate"] = {"type": "done", "routes": {"done": _pick_route(rng, names, bounded)}}
        elif kind < 0.55:
            entry["gate"] = {
                "type": "signal",
                "field": "status",
                "routes": {
                    "done": _pick_route(rng, names, bounded),
                    "not_solved": _pick_route(rng, names, bounded),
                },
            }
            if many:
                entry["gate"]["rule"] = "all"
        elif kind < 0.85 and topo in ("solo", "parallel"):
            raters = rng.choice([["r1"], ["r1", "r2"]])
            rt = rng.randint(1, 2)
            gate: dict[str, Any] = {
                "type": "judge",
                "raters": raters,
                "action": _act(rt, "rating"),
            }
            if not many and rng.random() < 0.5:
                gate |= {"rule": "mean", "pass_score": 5}
                gate["routes"] = {
                    "pass": _pick_route(rng, names, bounded),
                    "fail": _pick_route(rng, names, bounded),
                }
            else:
                gate |= {"rule": "rank", "routes": {"ranked": _pick_route(rng, names, bounded)}}
            entry["gate"] = gate
            least += len(raters)
            most += len(raters) * (rt + 1)
        elif kind < 0.92:
            entry["gate"] = {"type": "user"}
        else:
            entry["gate"] = {"type": "done", "routes": {"done": _pick_route(rng, names, bounded)}}
        phases.append(entry)
        costs.append((least, most))
    group = {
        "topology": "phases",
        "rubric": {"pass_score": 6},
        "params": {"ideen": {"default": [f"i{k}" for k in range(known or 0)]}},
        "seed": {"ideen": {"param": "ideen"}},
        "agents": [{"id": x} for x in ("a", "b", "c", "s", "r1", "r2")],
        "channels": CHANNELS,
        "phases": phases,
    }
    return group, costs


def _runs(resolved: ResolvedRun, costs: list[tuple[int, int]]) -> tuple[int, int | None]:
    """Fewest and most turns over every run the routes allow (None: the user may repeat),
    with visits counted and exhausted phases followed by this test's own rule."""
    phases = resolved.phases
    index = {p.name: i for i, p in enumerate(phases)}
    open_end = [False]

    def to(i: int, route: Any) -> int:
        if isinstance(route, Goto):
            return index[route.goto]
        word = route.value
        if word == "next":
            return i + 1 if i + 1 < len(phases) else END
        return {"retry": i, "stop": END}.get(word, -2)

    @functools.cache
    def enter(i: int, visits: tuple[int, ...], depth: int) -> tuple[int, int | None]:
        assert depth < 300, "a run that never ends"
        if i == END:
            return 0, 0
        if i == -2:
            open_end[0] = True
            return 0, None
        phase = phases[i]
        if phase.max_visits is not None and visits[i] >= phase.max_visits:
            assert phase.on_exhausted is not None
            return enter(to(i, phase.on_exhausted), visits, depth + 1)
        after = visits[:i] + (visits[i] + 1,) + visits[i + 1 :]
        outs = [enter(to(i, r), after, depth + 1) for r in phase.gate.routes.values()]
        if phase.gate.type.value == "user":
            open_end[0] = True
            outs.append((0, None))
        least, most = costs[i]
        tail = [o[1] for o in outs]
        return (
            least + min(o[0] for o in outs),
            None if None in tail else most + max(t for t in tail if t is not None),
        )

    least, most = enter(0, (0,) * len(phases), 0)
    return least, None if open_end[0] else most


def test_a10_bounds_hold_against_every_run_of_generated_groups() -> None:
    rng = random.Random(4202)
    seen = {"valid": 0, "loops": 0, "open": 0, "over": 0, "chat": 0, "moderated": 0}
    seen |= {"judge": 0, "max0": 0, "sleeper": 0, "escalate": 0}
    for _ in range(1500):
        group, costs = _make(rng)
        found = findings(group)
        if found.errors or found.resolved is None:
            continue
        resolved = found.resolved
        if [p.name for p in resolved.phases] != [p["name"] for p in group["phases"]]:
            continue
        shortest, longest = _runs(resolved, costs)
        least, most, _notes = turn_range(resolved)
        assert least <= shortest, (group, least, shortest)
        if longest is None:
            assert most is None, (group, most)
        elif most is not None:
            assert most >= longest, (group, most, longest)
        phases = group["phases"]
        seen["valid"] += 1
        seen["loops"] += any("max_visits" in p for p in phases)
        seen["open"] += longest is None
        seen["over"] += any("over" in p for p in phases)
        seen["chat"] += any(p["topology"] == "chat" for p in phases)
        seen["moderated"] += any(p["topology"] == "moderated" for p in phases)
        seen["judge"] += any(p.get("gate", {}).get("type") == "judge" for p in phases)
        seen["max0"] += any(p.get("max_visits") == 0 for p in phases)
        seen["sleeper"] += any("s" in p.get("standby", []) for p in phases)
        seen["escalate"] += "escalate" in str(phases)
    assert seen["valid"] >= 150 and all(v >= 10 for v in seen.values()), seen

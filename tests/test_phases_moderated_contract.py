"""W4b-04 A16 and A18 against the real `Orchestrator`.

A18 (§6.10): a note turn of auto-compact before an assignment turn or a return turn counts for
`turns_per_agent`, not as a turn of the assignment and not as a return turn; the room refuses
`assign` and `submit` in it. A turn that returns late (held before it begins) keeps count and
order.

A16 (§6.8): random runs of a phase `moderated` with FakeBackends (the lead gives up to
`max_assignments` at random, recipients hand in, stay silent or wait, the lead hands in early,
twice or never) never use more turns in a pass than the upper bound of `phase_turns`."""

import asyncio
import random
from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    errors,
    give,
    hand_in,
    is_assignment,
    is_return,
    manager,
    moderated_phase,
    of_kind,
    run,
    silent,
    submit,
)
from run_kit import make_workfolder, room

from arr.models import RunStatus
from arr.plan.estimate import ASSIGNMENT_TURNS, phase_turns
from arr.runs.compact import NOTE_HEADING
from arr.runs.orchestrator import Orchestrator

NOTE = [{"do": "text", "text": "### Stand\nWeiter.\n### Offene Punkte\nKeine."}]
FILL = {"do": "usage", "input": 600, "output": 5}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def first(view: TurnView) -> bool:
    return "# Auftrag – Phase" in view.prompt and view.action == "work"


def purposes(mgr: Any, agent: str) -> list[str | None]:
    return [d.get("purpose") for d in of_kind(mgr, "turn", agent=agent)]


# --------------------------------------------------------------------------------------------
# A18
# --------------------------------------------------------------------------------------------


async def test_a18_a_note_turn_before_an_assignment_turn(workfolder: Path) -> None:
    """b crosses its threshold in its first assignment turn: the note turn comes before its
    second one (in it `assign` and `submit` are refused); the assignment counts two turns, the
    agent three."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [give("b", "1")] if first(view) else hand_in(view)

    seen = {"b": 0}

    def b(view: TurnView) -> list[dict[str, Any]]:
        if view.prompt.startswith(NOTE_HEADING):
            return [give("c", "x"), submit(status="done", summary="s", body="t"), *NOTE]
        seen["b"] += 1  # the rebuilt session repeats the assignment: count the turns
        return [FILL] if seen["b"] == 1 else hand_in(view)

    agents = [{"id": "a"}, {"id": "b", "compact": {"at": 500}}, {"id": "c"}]
    mgr, log = manager(
        workfolder,
        {"agents": agents, "phases": [moderated_phase("a", ["b", "c"])]},
        {"a": a, "b": b},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert purposes(mgr, "b") == [None, "compact", None]
    [end] = of_kind(mgr, "assign_end")
    assert (end["status"], end["turns"]) == ("returned", 2)
    assert mgr.orchestrator.agents["b"].turns_used == 3
    assert len(mgr.room.delegations()) == 1, "assign was refused in the note turn"
    assert len(mgr.room.submissions(agent="b")) == 1, "submit was refused in the note turn"


async def test_a18_a_note_turn_before_a_return_turn(workfolder: Path) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.prompt.startswith(NOTE_HEADING):
            return NOTE
        if is_return(view):  # the rebuilt session repeats the assignment in front
            return hand_in(view)
        return [give("b", "1"), FILL]

    agents = [{"id": "a", "compact": {"at": 500}}, {"id": "b"}, {"id": "c"}]
    mgr, log = manager(
        workfolder,
        {"agents": agents, "phases": [moderated_phase("a", ["b", "c"], turns=1)]},
        {"a": a, "b": lambda v: hand_in(v)},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert purposes(mgr, "a") == [None, "compact", None]
    views = [v for v in log.of("a") if not v.prompt.startswith(NOTE_HEADING)]
    assert [is_return(v) for v in views] == [False, True]
    assert not any("Nachfrage" in v.prompt for v in views), "the note turn is no counted turn"


async def test_a18_a_turn_that_begins_late_keeps_count_and_order(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """b's first assignment turn is held before it begins (as when it waits for a place):
    the lead waits, gets exactly one return turn after b's turn, and the assignment counts
    one turn."""
    release = asyncio.Event()
    original = Orchestrator.turn

    async def turn(self: Any, agent_id: str, prompt: str, **kwargs: Any) -> Any:
        if agent_id == "b":
            self.log.write("test_held", agent=agent_id)
            await release.wait()
        return await original(self, agent_id, prompt, **kwargs)

    monkeypatch.setattr(Orchestrator, "turn", turn)

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [give("b", "1")] if first(view) else hand_in(view)

    mgr, log = manager(
        workfolder,
        {"agents": [{"id": x} for x in "abc"], "phases": [moderated_phase("a", ["b", "c"])]},
        {"a": a, "b": lambda v: hand_in(v)},
    )
    task = asyncio.create_task(run(mgr))
    from run_kit import eventually

    await eventually(lambda: bool(of_kind(mgr, "test_held")), within=30)
    await eventually(lambda: len(of_kind(mgr, "turn_end", agent="a")) == 1, within=30)
    assert len(log.of("a")) == 1, "no counted turn of the lead while #1 runs"
    release.set()
    assert await task is RunStatus.DONE, errors(mgr)
    order = [(k, d.get("agent")) for k, d in _turns(mgr)]
    assert order == [
        ("turn", "a"),
        ("turn_end", "a"),
        ("turn", "b"),
        ("turn_end", "b"),
        ("turn", "a"),
        ("turn_end", "a"),
    ]
    [end] = of_kind(mgr, "assign_end")
    assert end["turns"] == 1


def _turns(mgr: Any) -> list[tuple[str, dict[str, Any]]]:
    from phases_kit import entries

    return entries(mgr, "turn", "turn_end")


# --------------------------------------------------------------------------------------------
# A16: random runs against the upper bound
# --------------------------------------------------------------------------------------------


def _random_policies(rng: random.Random, jobs: int) -> dict[str, Any]:
    lead_mode = rng.choice(["early", "twice", "never", "after"])
    gives = rng.randint(0, jobs + 1)  # one more than allowed now and then
    recipients = {a: rng.choice(["hand_in", "silent", "wait"]) for a in ("b", "c")}

    def lead(view: TurnView) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        if first(view):
            steps += [give(rng.choice(["b", "c"]), f"Teil {i}") for i in range(gives)]
            if lead_mode == "early":
                steps += hand_in(view)
            elif lead_mode == "twice":
                steps += [*hand_in(view), *hand_in(view)]
            return steps
        if lead_mode == "never":
            return silent(view)
        if lead_mode == "twice":
            return [*hand_in(view), *hand_in(view)]
        if lead_mode == "after" and is_return(view) and rng.random() < 0.5:
            return [give(rng.choice(["b", "c"]), "noch einer")]
        return hand_in(view) if rng.random() < 0.5 else silent(view)

    def recipient(mode: str) -> Any:
        def policy(view: TurnView) -> list[dict[str, Any]]:
            if not is_assignment(view):
                return silent(view)
            if mode == "hand_in":
                return hand_in(view)
            if mode == "wait":
                return [room("wait_for_messages", seconds=0.05)]
            return silent(view)

        return policy

    return {"a": lead, "b": recipient(recipients["b"]), "c": recipient(recipients["c"])}


@pytest.mark.parametrize("seed", range(12))
async def test_a16_random_runs_stay_below_the_upper_bound(workfolder: Path, seed: int) -> None:
    rng = random.Random(seed)
    jobs, turns = rng.randint(1, 4), rng.randint(1, 3)
    phase = moderated_phase("a", ["b", "c"], jobs=jobs, turns=turns)
    mgr, log = manager(
        workfolder,
        {"agents": [{"id": x} for x in "abc"], "phases": [phase]},
        _random_policies(rng, jobs),
        limits={"turns_per_agent": 10**6},
    )
    assert await run(mgr, within=120) is RunStatus.DONE, errors(mgr)
    resolved_phase = mgr.resolved.phase("m")
    _least, most, _notes = phase_turns(mgr.resolved, resolved_phase)
    used = len([v for v in log.views if v.stage == "m"])
    assert used <= most, (seed, used, most)
    assert most == turns + 1 + jobs * (ASSIGNMENT_TURNS + 1)
    per_assignment = {}
    for v in log.views:
        if is_assignment(v):
            per_assignment[(v.agent, v.action)] = per_assignment.get((v.agent, v.action), 0) + 1
    assert all(n <= ASSIGNMENT_TURNS for n in per_assignment.values())

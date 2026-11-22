"""W4b-04: a phase `moderated` end to end (A1, A8, A9, A11, A12, A13, A24) with the real
RunManager, Orchestrator and room; agents are FakeBackends driven by policies (`phases_kit`).
No model, no CLI. Orders are read from the log and the turn log, never from durations."""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    answer,
    checkpoints,
    delegations,
    entered,
    errors,
    fixture_manager,
    give,
    hand_in,
    is_assignment,
    is_return,
    manager,
    moderated_phase,
    of_kind,
    run,
    silent,
)
from run_kit import make_workfolder

from arr.models import RunStatus
from arr.room.types import DelegationStatus

AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c", "d")]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def lead_policy(*plan: list[dict[str, Any]], then: str = "submit") -> Any:
    """The lead's turns: the steps of `plan` in order (its counted and return turns), then
    `then` (`submit` or `silent`)."""
    steps = list(plan)

    def policy(view: TurnView) -> list[dict[str, Any]]:
        if steps:
            return steps.pop(0)
        return hand_in(view) if then == "submit" else silent(view)

    return policy


async def test_a1_a_moderated_phase_runs_assign_return_submit(workfolder: Path) -> None:
    """The lead gives two assignments, both come back in one or two return turns, the lead
    hands in; the gate reads only the lead."""
    lead = lead_policy([give("b", "Teil 1"), give("c", "Teil 2", "list")], then="submit")

    def lead_waits(view: TurnView) -> list[dict[str, Any]]:
        if is_return(view) and len(delegations_now(view)) < 2:
            return silent(view)
        return lead(view) if not is_return(view) else hand_in(view)

    def delegations_now(view: TurnView) -> list[int]:
        return [
            r["number"]
            for r in view.context.get("assignments") or []
            if r["status"] in ("returned", "missing")
        ]

    group = {**AGENTS, "phases": [moderated_phase("a", ["b", "c"])]}
    mgr, log = manager(workfolder, group, {"a": lead_waits, "b": answer(), "c": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    rows = delegations(mgr)
    assert [(d.number, d.agent, d.format, d.status) for d in rows] == [
        (1, "b", "text", DelegationStatus.RETURNED),
        (2, "c", "list", DelegationStatus.RETURNED),
    ]
    gate = checkpoints(mgr, "gate")[0]
    assert gate["agent"] == "a" and len(gate["results"]) == 1
    subs = {s.action: s for s in mgr.orchestrator.submissions()}
    assert set(subs) == {"work", "assign-1", "assign-2"}
    assert gate["results"] == [subs["work"].id]
    assert [k for k, _ in [(e, 0) for e in ("assign", "assign_start", "assign_end")]]
    assert len(of_kind(mgr, "assign_end", status="returned")) == 2
    assert any(is_assignment(v) for v in log.of("b"))
    assert entered(mgr) == ["m"]


async def test_a24_fixture_runs_to_the_end(workfolder: Path) -> None:
    """`tests/fixtures/phases/moderated.yaml` from the start to the end."""

    def coordinator(view: TurnView) -> list[dict[str, Any]]:
        if view.stage != "zerlegung":
            return hand_in(view)
        if view.turn == 1:
            return [give("helper-1", "Teil A"), give("helper-2", "Teil B", "list")]
        rows = view.context.get("assignments") or []
        if all(r["status"] == "returned" for r in rows):
            return hand_in(view)
        return silent(view)

    policies = {
        "coordinator": coordinator,
        "helper-1": answer(),
        "helper-2": answer(),
        "writer": answer(),
    }
    mgr, log = fixture_manager(workfolder, "moderated", policies)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["zerlegung", "zusammenfassung"]
    print(log.turns)

"""W4b-09 at run level: the real `RunManager`, `Orchestrator` and phase runner, FakeBackend
agents that ask during a turn, a fake model for the supervising agent (A5, A9, A10)."""

import json
from pathlib import Path
from typing import Any

import pytest
from decider_kit import (
    DECIDERS,
    KINDS,
    MAIN_LINES,
    asking_run,
    comparison_run,
    inside,
    prepare_workfolder,
)
from phases_kit import errors, hand_in, judge_gate, run, solo
from run_kit import make_workfolder

from arr.models import RunStatus
from arr.permissions.types import DecidedBy, TightenedBy, TightenedSource
from arr.runs.phases import runner as phase_runner
from arr.runs.topologies import solo as solo_topology
from arr.service.real_api import RealApi

USER, GATEKEEPER = DecidedBy.USER, DecidedBy.GATEKEEPER


@pytest.fixture
def workfolder(tmp_path: Path, arr_home: Path) -> Path:
    folder = make_workfolder(tmp_path)
    prepare_workfolder(folder, arr_home)
    return folder


def tight(stage: str, attempt: int) -> TightenedBy:
    return TightenedBy(source=TightenedSource.PHASE, stage=stage, attempt=attempt)


def approval_lines(run_dir: Path) -> list[dict[str, Any]]:
    text = (run_dir / "approvals.jsonl").read_text()
    return [e for e in map(json.loads, text.splitlines()) if e.get("item_id")]


# --------------------------------------------------------------------------------------------
# A5: phase changes at the real orchestrator
# --------------------------------------------------------------------------------------------


def beweis_choice(view: Any) -> list[dict[str, Any]]:
    """Pass 1 of `beweis`: once more; pass 2: back to `erkunden`; pass 3: done."""
    if view.stage != "beweis":
        return hand_in(view)
    return hand_in(view, choice={1: "nochmal", 2: "zurueck"}.get(view.attempt, "fertig"))


LOOP = {
    "agents": [{"id": "p"}],
    "phases": [
        {**solo("erkunden", "p"), "max_visits": 3, "on_exhausted": "stop"},
        {
            **solo("beweis", "p", "decision", choices=["nochmal", "zurueck", "fertig"]),
            "decider": "user",
            "max_visits": 5,
            "on_exhausted": "stop",
            "gate": {
                "type": "signal",
                "field": "choice",
                "routes": {"nochmal": "retry", "zurueck": {"goto": "erkunden"}, "fertig": "stop"},
            },
        },
    ],
}


@pytest.mark.parametrize("decider", ["gatekeeper_then_user", "gatekeeper"])
async def test_a5_the_same_request_goes_to_the_model_or_the_user_by_phase(
    workfolder: Path, arr_home: Path, decider: str
) -> None:
    case = asking_run(
        workfolder,
        LOOP,
        want=lambda agent, _stage: inside(agent),
        decider=decider,
        topology=phase_runner.run,
        policies={"p": beweis_choice},
    )
    assert await run(case.mgr) is RunStatus.DONE, errors(case.mgr)
    seen = [(a.stage, a.phase, a.decision.decided_by) for a in case.asked]
    assert seen == [
        ("erkunden", ("erkunden", 1), GATEKEEPER),
        ("beweis", ("beweis", 1), USER),
        ("beweis", ("beweis", 2), USER),  # retry
        ("erkunden", ("erkunden", 2), GATEKEEPER),  # goto back: the model again
        ("beweis", ("beweis", 3), USER),
    ]
    assert len(case.model.prompts) == 2
    assert [i.tightened_by for i in case.user.items] == [tight("beweis", n) for n in (1, 2, 3)]
    assert case.user.calls_at_open == [1, 1, 2], "the user saw each before any further call"
    calls = (case.mgr.run_dir / "gatekeeper.jsonl").read_text().splitlines()
    assert len(calls) == 2
    opened = [e["tightened_by"] for e in approval_lines(case.mgr.run_dir) if "tightened_by" in e]
    assert opened == [{"source": "phase", "stage": "beweis", "attempt": n} for n in (1, 2, 3)]


async def test_a5_a_rater_of_a_judge_gate_in_a_tightened_phase(
    workfolder: Path, arr_home: Path
) -> None:
    data = {
        "agents": [{"id": "p"}, {"id": "r1"}],
        "phases": [
            {**solo("erkunden", "p"), "gate": judge_gate("r1", "rank", routes={"ranked": "next"})},
            {
                **solo("beweis", "p"),
                "decider": "user",
                "gate": judge_gate("r1", "rank", routes={"ranked": "next"}),
            },
        ],
    }
    case = asking_run(
        workfolder,
        data,
        want=lambda agent, _stage: inside(agent) if agent == "r1" else None,
        decider="gatekeeper_then_user",
        topology=phase_runner.run,
        policies={"p": hand_in, "r1": hand_in},
    )
    assert await run(case.mgr) is RunStatus.DONE, errors(case.mgr)
    seen = [(a.agent, a.phase, a.decision.decided_by) for a in case.asked]
    assert seen == [("r1", ("erkunden", 1), GATEKEEPER), ("r1", ("beweis", 1), USER)]
    assert len(case.model.prompts) == 1


async def test_a9_the_service_reads_a_log_with_the_field(workfolder: Path, arr_home: Path) -> None:
    case = asking_run(
        workfolder,
        LOOP,
        want=lambda agent, _stage: inside(agent),
        decider="gatekeeper_then_user",
        topology=phase_runner.run,
        policies={"p": beweis_choice},
    )
    assert await run(case.mgr) is RunStatus.DONE, errors(case.mgr)
    api = RealApi(workfolder, base_url="http://127.0.0.1:8765")
    try:
        items = await api.list_approvals(run_id=case.mgr.run_dir.name)
    finally:
        await api.close()
    deciders = sorted(str(i.decision.decided_by) for i in items if i.decision is not None)
    assert len(items) == 5 and deciders == ["gatekeeper"] * 2 + ["user"] * 3


# --------------------------------------------------------------------------------------------
# A10: without `decider` at agents and phases the lines are those of `main`
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("decider", DECIDERS)
async def test_a10_without_the_fields_the_lines_are_those_of_main(
    tmp_path: Path, arr_home: Path, decider: str, kind: str
) -> None:
    status, lines, case = await comparison_run(make_workfolder(tmp_path), arr_home, decider, kind)
    expected = json.loads(MAIN_LINES.read_text(encoding="utf-8"))[f"{decider}/{kind}"]
    assert status.value == expected.pop("status") == "done", errors(case.mgr)
    assert lines == expected
    assert all("tightened_by" not in e for e in lines["approvals.jsonl"])


@pytest.mark.parametrize("tightened", [True, False], ids=["decider-user", "ohne"])
async def test_a10_decider_user_at_an_agent_holds_in_an_old_topology(
    workfolder: Path, arr_home: Path, tightened: bool
) -> None:
    agent: dict[str, Any] = {"id": "p", "decider": "user"} if tightened else {"id": "p"}
    data = {
        "agents": [agent],
        "topology": {"kind": "solo", "params": {"require_result": False, "max_turns": 1}},
    }
    case = asking_run(
        workfolder,
        data,
        want=lambda a, _s: inside(a),
        decider="gatekeeper_then_user",
        topology=solo_topology.run,
        scripts={"p": {"turns": [{"steps": [{"do": "text", "text": "ok"}]}]}},
    )
    assert await run(case.mgr) is RunStatus.DONE, errors(case.mgr)
    [asked] = case.asked
    if tightened:
        assert asked.decision.decided_by is USER and case.model.prompts == []
        assert case.user.items[0].tightened_by == TightenedBy(source=TightenedSource.AGENT)
    else:
        assert asked.decision.decided_by is GATEKEEPER and len(case.model.prompts) == 1

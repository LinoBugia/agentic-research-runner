"""W4b-03.1 N11: assembly with W4b-09. An agent on standby in a phase with `decider: user` is
woken and asks a request of the grey zone in its wake-up turn: the item goes to the user and
carries `tightened_by` with this phase and pass, the model double gets no call. Counter-check:
the same phase without `decider: user` asks the model. This also shows A23 at work: the
sleeper's last phase with an action (`vorher`) is not tightened, so the runner must have
entered it into `beweis` before its wake-up turn (`enter_phase` for every participant).

Helpers of `tests/decider_kit.py` (W4b-09), unchanged."""

import json
from pathlib import Path
from typing import Any

import pytest
from decider_kit import asking_run, inside, prepare_workfolder
from phases_kit import errors, hand_in, run, say, solo
from run_kit import make_workfolder, room

from arr.models import RunStatus
from arr.permissions.types import DecidedBy, TightenedBy, TightenedSource
from arr.runs.phases import runner as phase_runner


@pytest.fixture
def workfolder(tmp_path: Path, arr_home: Path) -> Path:
    folder = make_workfolder(tmp_path)
    prepare_workfolder(folder, arr_home)
    return folder


def prover(view: Any) -> list[dict[str, Any]]:
    if view.stage != "beweis":
        return hand_in(view)
    return [
        say("k", "@s Welcher Ansatz?", kind="question"),
        room("wait_for_messages", seconds=40),
        *hand_in(view),
    ]


def strategist(view: Any) -> list[dict[str, Any]]:
    if not view.context.get("standby"):
        return hand_in(view)
    return [
        room("inbox", save_as="box"),
        say("k", "Induktion.", kind="answer", reply_to="${box.open_questions.0.id}"),
    ]


@pytest.mark.parametrize("tightened", [True, False], ids=["decider-user", "ohne"])
async def test_n11_a_woken_sleeper_asks_under_the_decider_of_its_phase(
    workfolder: Path, arr_home: Path, tightened: bool
) -> None:
    beweis = {**solo("beweis", "a"), "standby": ["s"], "channels": ["k"]}
    if tightened:
        beweis["decider"] = "user"
    data = {
        "agents": [{"id": "a"}, {"id": "s"}],
        "channels": [{"name": "k", "members": ["a", "s"]}],
        "phases": [solo("vorher", "s"), beweis],
    }
    case = asking_run(
        workfolder,
        data,
        want=lambda agent, stage: inside(agent) if agent == "s" else None,
        decider="gatekeeper_then_user",
        topology=phase_runner.run,
        policies={"a": prover, "s": strategist},
    )
    assert await run(case.mgr) is RunStatus.DONE, errors(case.mgr)
    seen = [(x.agent, x.stage, x.phase, x.decision.decided_by) for x in case.asked]
    # `s` asks once in `vorher` (its action) and once in its wake-up turn in `beweis`
    assert [x[:3] for x in seen] == [("s", "vorher", ("vorher", 1)), ("s", "beweis", ("beweis", 1))]
    assert seen[0][3] is DecidedBy.GATEKEEPER
    if tightened:
        assert seen[1][3] is DecidedBy.USER
        assert len(case.model.prompts) == 1, "the model was not asked in `beweis`"
        expected = TightenedBy(source=TightenedSource.PHASE, stage="beweis", attempt=1)
        assert [i.tightened_by for i in case.user.items] == [expected]
        text = (case.mgr.run_dir / "approvals.jsonl").read_text("utf-8")
        firsts = [e for e in map(json.loads, text.splitlines()) if "tightened_by" in e]
        assert [e["tightened_by"] for e in firsts] == [
            {"source": "phase", "stage": "beweis", "attempt": 1}
        ]
    else:
        assert seen[1][3] is DecidedBy.GATEKEEPER
        assert len(case.model.prompts) == 2, "without `decider: user` the model is asked"


async def test_n11_counter_check_without_enter_phase_for_the_sleeper(
    workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner that leaves agents on standby out of `enter_phase` (what A23 forbids): the
    wake-up turn runs in the sleeper's old phase, and the model decides although `beweis` says
    `decider: user`. The test above would see it."""
    from arr.runs.orchestrator import Orchestrator

    enter = Orchestrator.enter_phase

    async def without_standby(self: Any, stage: str, attempt: int, agents: Any) -> None:
        await enter(self, stage, attempt, [x for x in agents if stage != "beweis" or x != "s"])

    monkeypatch.setattr(Orchestrator, "enter_phase", without_standby)
    beweis = {**solo("beweis", "a"), "standby": ["s"], "channels": ["k"], "decider": "user"}
    data = {
        "agents": [{"id": "a"}, {"id": "s"}],
        "channels": [{"name": "k", "members": ["a", "s"]}],
        "phases": [solo("vorher", "s"), beweis],
    }
    case = asking_run(
        workfolder,
        data,
        want=lambda agent, stage: inside(agent) if agent == "s" else None,
        decider="gatekeeper_then_user",
        topology=phase_runner.run,
        policies={"a": prover, "s": strategist},
    )
    assert await run(case.mgr) is RunStatus.DONE, errors(case.mgr)
    [_, woken] = case.asked
    assert (woken.stage, woken.phase, woken.decision.decided_by) == (
        "beweis",
        ("vorher", 1),
        DecidedBy.GATEKEEPER,
    )

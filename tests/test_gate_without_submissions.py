"""A gate `done` without submissions (W4b-06b §6.8, Codeplan §8 line 318, A18).

In the reviewer's live run of W4b-07 two judges handed in nothing, because their fields had
unknown keys, and the flow said „Gate „done“ ergibt „done“ – bestanden, es fehlt: …“. Here the
same case with FakeBackend agents, shaped like the phase `pruefung` of the start skill
`proof-review` (`parallel`, isolated, one turn per judge, no gate: `done`).

What it shows (the cause, for the report): `submit` refuses unknown keys with a German message
that names the allowed ones; with `turns: 1` the judge has its turn and one follow-up to correct
it; and a gate `done` passes a pass whatever is missing (`PhaseRunner.evaluate`), so the run
goes on without any review. No change to the runner here (W4b-06b §6.8).
"""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import TurnView, entries, hand_in, manager, parallel, run, solo, submit
from run_kit import make_workfolder

from arr.models import RunStatus
from arr.runs.launch import describe_entry

WRONG = {"urteil": "accept", "einwaende": []}
"""Fields with keys a model might guess; the format `review` has `verdict` and `objections`."""


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def judge(view: TurnView) -> list[dict[str, Any]]:
    return [submit(status="done", summary="Geprüft.", fields=WRONG)]


GROUP = {
    "agents": [{"id": "author"}, {"id": "judge", "count": 2}],
    "phases": [
        solo("fassung", "author", "artifact", name="beweis"),
        {**parallel("pruefung", "judge-*", "review", turns=1), "isolated": True},
        solo("revision", "author"),
    ],
}


async def test_a18_a_gate_done_passes_a_pass_in_which_nobody_handed_in(
    workfolder: Path,
) -> None:
    policies = {"author": lambda view: hand_in(view), "judge-1": judge, "judge-2": judge}
    mgr, log = manager(workfolder, GROUP, policies)
    assert await run(mgr) is RunStatus.DONE
    # the refusal names the allowed keys
    answers = [
        e["data"]["content"]
        for agent in ("judge-1", "judge-2")
        for e in _events(mgr, agent)
        if e["kind"] == "tool_result" and e["data"].get("is_error")
    ]
    assert len(answers) == 4, answers  # two turns per judge, each `submit` refused
    for answer in answers:
        assert answer.startswith("Die Abgabe passt nicht:")
        assert "„fields.urteil“: unbekanntes Feld. Erlaubt: verdict, objections." in answer
    # the turn of the action (`turns: 1`) and the one follow-up („Nachfrage“), then nothing
    views = [v for v in log.views if v.agent == "judge-1"]
    assert len(views) == 2 and views[1].follow_up
    # the gate `done` passes the pass with both submissions missing
    [gate] = [
        data
        for kind, data in entries(mgr, "checkpoint")
        if data.get("stage") == "pruefung" and data.get("phase") == "gate"
    ]
    assert gate["value"] == "done" and gate["decision"] == "passed"
    assert sorted(gate["missing"]) == ["judge-1/work", "judge-2/work"], gate["missing"]
    text = describe_entry({"kind": "checkpoint", "data": gate}, phases=True)
    assert "Gate „done“ ergibt „done“ – bestanden, es fehlt: " in text, text
    # and the run goes on to the revision without any review
    assert any(t[:2] == ("author", "revision") for t in log.turns)


def _events(mgr: Any, agent: str) -> list[dict[str, Any]]:
    from run_kit import events

    return events(mgr.run_dir, agent)

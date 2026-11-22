"""W4b-06a A5, A6: the start skills as phases against the target flows of the old topologies.

Every case of `templates_kit.CASES` runs the real start skill from the package (`solo` as
`tests/fixtures/phases/solo.yaml`) with FakeBackend agents and compares its flow with
`tests/fixtures/template_flows/<template>-<case>.json`, up to the deviations
`templates_kit.DEVIATIONS` names one by one (table of §6.3 of the order). Nothing here needs
the old topologies; the file runs unchanged after W4b-06b.
"""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import errors, run
from run_kit import make_workfolder
from templates_kit import (
    CASES,
    DEVIATIONS,
    FIXTURE_OF,
    FLOWS,
    Case,
    compare,
    deviations_for,
    new_manager,
    new_policies,
    run_new,
    structure_issues,
)

from arr.skills import start_skill_files


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def test_a5_every_case_has_its_target_flow() -> None:
    names = sorted(p.name for p in FLOWS.glob("*.json"))
    assert names == sorted(f"{c.id}.json" for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_a6_the_start_skill_meets_its_target_flow(workfolder: Path, case: Case) -> None:
    mgr, flow = await run_new(workfolder, case)
    assert errors(mgr) == []
    applied = compare(case, flow)
    assert applied == [d.key for d in deviations_for(case)]


def test_a6_every_deviation_is_used_by_a_case() -> None:
    used = {d.key for c in CASES for d in deviations_for(c)}
    assert used == {d.key for d in DEVIATIONS}


def _falsified(name: str, old: str, new: str) -> dict[str, bytes]:
    files = start_skill_files(name)
    text = files["group.yaml"].decode()
    assert old in text, old
    files["group.yaml"] = text.replace(old, new).encode()
    return files


FALSIFIED = {
    "revision-ohne-goto": (
        "proof-review-revision",
        "proof-review",
        "gate: { type: done, routes: { done: { goto: pruefung } } }",
        "gate: { type: done, routes: { done: next } }",
    ),
    "fail-next-statt-retry": (
        "proof-nacharbeit",
        "proof",
        "routes: { pass: next, fail: retry }",
        "routes: { pass: next, fail: next }",
    ),
}


@pytest.mark.parametrize("what", sorted(FALSIFIED))
async def test_a6_a_falsified_template_fails_the_comparison(workfolder: Path, what: str) -> None:
    """Counter-check: a template with a wrong route does not meet the target flow."""
    case_id, name, old, new = FALSIFIED[what]
    [case] = [c for c in CASES if c.id == case_id]
    mgr, flow = await run_new(workfolder, case, files=_falsified(name, old, new))
    with pytest.raises(AssertionError, match="Abweichung ohne Ausnahme"):
        compare(case, flow)


async def test_a6_note_next_stays_private(workfolder: Path) -> None:
    """Deviation `notiz-bleibt-privat` (Entscheidung 28): the note of one judge reaches no
    other judge, also not after a revision."""
    [case] = [c for c in CASES if c.id == "proof-review-revision"]
    policies = new_policies(case)
    judge = policies["judge-1"]

    def noting(view: Any) -> list[dict[str, Any]]:
        steps = judge(view)
        steps[-1]["args"]["note_next"] = f"geheime Notiz von {view.agent}"
        return steps

    policies["judge-1"] = noting
    mgr, log = new_manager(workfolder, "proof-review", policies)
    assert await run(mgr) == "done", errors(mgr)
    others = [v for v in log.views if v.agent != "judge-1"]
    assert others and not any("geheime Notiz" in v.prompt for v in others)
    assert not any("geheime Notiz" in str(v.context) for v in others)


@pytest.mark.parametrize("name", sorted(FIXTURE_OF))
def test_a6_the_templates_keep_the_agreed_structure(name: str) -> None:
    """M2 c: phases as in the test data of W4b-02a, `turns: 1` everywhere, isolated checks,
    every task file there."""
    assert structure_issues(name) == []


@pytest.mark.parametrize(
    ("name", "old", "new", "what"),
    [
        ("proof-review", "turns: 1 }", "turns: 3 }", "turns 3 statt 1"),
        ("proof-review", "    isolated: true\n", "", "nicht isoliert"),
        ("proof", "tasks/skizze.md", "tasks/beweis.md", "phases weichen"),
        ("exploration", "      - { peers: summary }\n", "", "phases weichen"),
    ],
)
def test_a6_the_structure_check_finds_changes(name: str, old: str, new: str, what: str) -> None:
    files = start_skill_files(name)
    text = files["group.yaml"].decode()
    assert old in text
    files["group.yaml"] = text.replace(old, new, 1).encode()
    assert any(what in issue for issue in structure_issues(name, files))


# --------------------------------------------------------------------------------------------
# W4b-06a.1 point 7: the end of a phase that did not pass (H3, M1, Entscheidung 81)
# --------------------------------------------------------------------------------------------


def _case(case_id: str) -> Case:
    [case] = [c for c in CASES if c.id == case_id]
    return case


async def test_p7_the_old_line_in_stage_end_turns_the_comparison_red(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-check: with the old rule („stage_failed“ whenever no phase follows) the last
    stage of `proof` with `next` differs from the old topology."""
    from arr.runs.phases.runner import PhaseRunner

    monkeypatch.setattr(PhaseRunner, "_left_by_next", lambda self, route, info: False)
    case = _case("proof-hohe-schwelle")
    _mgr, flow = await run_new(workfolder, case)
    with pytest.raises(AssertionError, match="Abweichung ohne Ausnahme"):
        compare(case, flow)


@pytest.mark.parametrize(
    ("case_id", "reason", "line"),
    [
        (
            "proof-aufgebraucht-stopp",
            "Die Phase „skizze“ wurde nicht bestanden (stage_failed).",
            "- **skizze:** nicht bestanden, Run beendet, akzeptiert: keins",
        ),
        (
            "proof-hohe-schwelle",
            "Die Topologie ist regulär durchgelaufen.",
            "- **beweis:** bester Versuch übernommen, akzeptiert: #",
        ),
    ],
)
async def test_p7_summary_and_run_detail_name_the_end(
    workfolder: Path, case_id: str, reason: str, line: str
) -> None:
    from arr.runs.inspect import run_reason
    from arr.runs.orchestrator import OrchestratorLog
    from arr.runs.prepare import load_run_meta
    from arr.service.api_models import StageStatus
    from arr.service.real_api import RealApi

    mgr, _flow = await run_new(workfolder, _case(case_id))
    summary = (mgr.run_dir / "output" / "summary.md").read_text("utf-8")
    assert f"- **Grund:** {reason}" in summary
    assert line in summary
    entries = list(OrchestratorLog(mgr.run_dir).entries())
    assert run_reason(load_run_meta(mgr.run_dir), entries) == reason
    detail = await RealApi(workfolder, base_url="http://test").get_run(mgr.run_dir.name)
    stages = {s.key: s.status for s in detail.stages}
    if case_id == "proof-aufgebraucht-stopp":
        assert stages["skizze"] is StageStatus.FAILED
    else:
        assert stages["beweis"] is StageStatus.DONE, "best attempt, not „nicht bestanden“"

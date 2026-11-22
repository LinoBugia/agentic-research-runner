"""W4b-06a A5: the old side of the comparison alt gegen neu (P6).

Every case of `templates_kit.CASES` runs the old topology (the old start skill from
`tests/fixtures/start_skills_old/`, `solo` as run data) with FakeBackend agents and checks that
its flow, mapped onto the phases, is exactly the target flow in
`tests/fixtures/template_flows/<template>-<case>.json`. With `ARR_WRITE_TEMPLATE_FLOWS=1` the
test writes the file instead (an explicit step, never a side effect of a normal run).

This file is the only one that needs the old topologies: W4b-06b deletes it as a whole. The
target flows stay; `test_templates_flows.py` compares the real start skills with them.
"""

from pathlib import Path

import pytest
from phases_kit import errors, run
from run_kit import make_workfolder
from templates_kit import CASES, Case, old_manager, old_policies, run_old, write_or_check
from test_templates_texts import MAIN_JUDGES_BRIEF

from arr.models import RunStatus
from arr.runs import prepare


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_a5_the_old_topology_produces_the_target_flow(workfolder: Path, case: Case) -> None:
    mgr, flow = await run_old(workfolder, case)
    assert errors(mgr) == []
    write_or_check(case.template, case.name, flow, case.params)


def _standard() -> Case:
    [case] = [c for c in CASES if c.id == "proof-review-standard"]
    return case


async def test_a9_an_old_run_writes_the_brief_of_main(workfolder: Path) -> None:
    """A9: the brief of a run without phases, character for character as on `main` (the purpose
    and the roles are those of the old `proof-review`)."""
    mgr, _log = old_manager(workfolder, "proof-review", old_policies(_standard()))
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    brief = mgr.room.messages("judges")[0].body
    expected = MAIN_JUDGES_BRIEF.replace(
        "Zweck: Diskussion der Judges\n",
        "Zweck: Diskussion der Judges nach der unabhängigen Prüfung\n",
    ).replace("- Mitglied B\n", "- Mitglied B (judge)\n")
    assert brief == expected


async def test_a12_an_old_run_names_submit_result(workfolder: Path) -> None:
    mgr, _log = old_manager(workfolder, "proof-review", old_policies(_standard()))
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    context = (mgr.run_dir / "agents" / "author" / "context.md").read_text("utf-8")
    assert "- Ergebnisse gibst du mit `submit_result` ab." in context
    assert prepare.default_role(mgr.prepared.resolved) == prepare.DEFAULT_ROLE


async def test_p7_an_old_run_keeps_its_wording(workfolder: Path) -> None:
    """W4b-06a.1 point 7: in a run without phases the reason reads as before."""
    from arr.runs.inspect import run_reason
    from arr.runs.orchestrator import OrchestratorLog
    from arr.runs.prepare import load_run_meta

    [case] = [c for c in CASES if c.id == "proof-aufgebraucht-stopp"]
    mgr, _flow = await run_old(workfolder, case)
    reason = "Die Stufe „skizze“ wurde nicht bestanden (stage_failed)."
    summary = (mgr.run_dir / "output" / "summary.md").read_text("utf-8")
    assert f"- **Grund:** {reason}" in summary
    entries = list(OrchestratorLog(mgr.run_dir).entries())
    assert run_reason(load_run_meta(mgr.run_dir), entries) == reason

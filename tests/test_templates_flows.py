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
    FLOWS,
    Case,
    compare,
    deviations_for,
    new_manager,
    new_policies,
    run_new,
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

"""Finished runs of the old topologies stay readable after their removal (W4b-06b §6.6, A13).

The run folders in `tests/fixtures/old_runs/` were written once by the code of `main` before
the removal (`review`, `waterfall` with a failed stage and with ratings, `solo`); the files in
`expected/` hold what the reading code of `main` gave for them. After the removal the reading
code must give exactly the same: API (`get_run`, `get_results`, `get_log`, channels, agents),
the line of every log entry in the old wording, `run_reason`, `read_results` and
`read_run_summary` of Claude Code. The run page of the GUI and `failed_stage_text` are checked
on the same folders. No old topology runs here; no model.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_kit import make_api, prepare_workfolder
from old_runs_kit import (
    NAMES,
    OLD_RUNS,
    REVIEW,
    SOLO,
    WATERFALL,
    WATERFALL_FAILED,
    readings,
    restore,
)
from run_kit import write_profiles
from service_kit import make_workfolder

from arr.runs.finalize import failed_stage_text
from arr.runs.orchestrator import OrchestratorLog


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = prepare_workfolder(make_workfolder(tmp_path))
    for name in NAMES:
        restore(workfolder, name)
    return workfolder


def expected(name: str) -> dict[str, Any]:
    return json.loads((OLD_RUNS / "expected" / f"{name}.json").read_text("utf-8"))


@pytest.mark.parametrize("name", NAMES)
async def test_an_old_run_reads_as_on_main(wf: Path, name: str) -> None:
    found = await readings(make_api(wf), wf, name)
    want = expected(name)
    for key in want:
        assert found[key] == want[key], f"{name}: {key} differs from main"
    assert found.keys() == want.keys()


async def test_the_old_wording_stays(wf: Path) -> None:
    """The lines that differ between old and phases runs, spelled out (the snapshot holds
    them as well): „Stufe“, the escalation of a stage, the reason of a failed stage."""
    failed = await readings(make_api(wf), wf, WATERFALL_FAILED)
    assert failed["run_reason"] == "Die Stufe „skizze“ wurde nicht bestanden (stage_failed)."
    lines = [line for line in failed["describe_entry"] if line]
    assert "▶ p: Turn 1 · Stufe „skizze“" in lines
    escalation = "Eskalation in Stufe „skizze“: Stufe „skizze“ (p)"
    assert any(line.startswith(escalation) for line in lines)
    assert "● Stufe „skizze“ abgeschlossen: nicht bestanden" in lines
    review = await readings(make_api(wf), wf, REVIEW)
    assert "Kanal #judges geöffnet (Stufe „pruefung“)" in review["describe_entry"]
    assert "● Stufe „diskussion“: Diskussion in #judges" in review["describe_entry"]


async def test_read_results_of_an_old_review_keeps_the_verdicts(wf: Path) -> None:
    page = (await readings(make_api(wf), wf, REVIEW))["read_results"]
    reviews = [r for r in page["results"] if r["kind"] == "review"]
    assert [r["agent_text"].split(" ", 2)[:2] for r in reviews] == [
        ["Urteil:", "major"],
        ["Urteil:", "minor"],
        ["Urteil:", "accept"],
        ["Urteil:", "accept"],
    ]
    assert page["total"] == 6


async def test_results_and_ratings_of_an_old_waterfall(wf: Path) -> None:
    results = (await readings(make_api(wf), wf, WATERFALL))["get_results"]
    rated = [r for r in results["items"] if r["ratings"]]
    assert rated, "the ratings of the old run are read"
    assert {"korrektheit", "klarheit"} <= set(rated[-1]["ratings"][0]["criteria"])


def test_failed_stage_text_reads_the_old_checkpoint(wf: Path) -> None:
    """`failed_stage_text` keeps its old branch (§6.4): the checkpoint `reason: stage_failed`
    of an old `waterfall` run (A6)."""
    entries = OrchestratorLog(wf / "runs" / WATERFALL_FAILED).entries()
    assert failed_stage_text(entries) == "Die Stufe „skizze“ wurde nicht bestanden (stage_failed)."
    assert failed_stage_text(OrchestratorLog(wf / "runs" / SOLO).entries()) is None


async def test_the_run_page_of_an_old_review(wf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The run page of the GUI shows the old run: its stages, the reviews with their verdict
    and the summary; the failed waterfall its reason."""
    from test_gui_real import see
    from test_gui_runs_real import real_user

    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{REVIEW}?ansicht=ergebnisse")
        html = await see(user, "Gutachten", retries=200)
        assert "Urteil: major" in html
        await user.open(f"/runs/{WATERFALL_FAILED}")
        await see(user, "Die Stufe „skizze“ wurde nicht bestanden", retries=200)

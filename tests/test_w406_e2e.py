"""W4-06 E2E M2: the small fixes found in the live run of `proof-review` (report §3).

1. The GUI shows the table and the list of `summary.md` (overview of a review) as a table and a
   list, not as running text with `|`; HTML in them stays escaped.
2. After a run, `read_results` names the copy `finalize` writes for a result without a file of
   its own (a review), so Claude Code can read the whole text, not only the excerpt. A link at
   that place or a missing copy gives no path.

Real files and the MCP service of `mcp_kit`; no model.
"""

import os
from pathlib import Path

import pytest
from mcp_kit import Service, make_api, prepare_workfolder, result_data
from run_kit import write_profiles
from service_kit import make_run_folder, make_workfolder

from arr.models import RunStatus
from arr.room.store import RoomStore
from arr.service.gui.components.base import markdown_html

# the overview of `summary.md` as the review topology writes it (live run W4-06)
SUMMARY = """## Übersicht

**Urteil:** minor (Regel majority) · keine Revision mehr übrig
**Letzte Fassung:** v2, `output/artifacts/v2/artefakt.md`

| Runde | Fassung | Urteile | Ergebnis |
|---|---|---|---|
| 1 | v1 | judge-1 (Mitglied A): major | <b>major</b> |
| 2 | v2 | judge-1 (Mitglied A): minor | minor |

**Offene Einwände:** 1 von 2
- E2 (offen, judge-1 (Mitglied A)): <script>alert(1)</script> Fall n ≡ 2 fehlt
"""


def test_the_overview_table_and_the_list_of_open_objections_are_html() -> None:
    out = markdown_html(SUMMARY, tables=True)  # only the summary gets tables (W4-06.1)
    assert "<table>" in out and "<th>Runde</th>" in out and "<td>v2</td>" in out
    assert "| Runde |" not in out, "no running text with pipes"
    assert "<ul>" in out and "<li>E2 (offen" in out, "the list right after a line is a list"
    assert "<b>" not in out and "<script>" not in out, "HTML of agents stays escaped"
    assert "&lt;b&gt;major&lt;/b&gt;" in out and "&lt;script&gt;" in out
    assert "<code>output/artifacts/v2/artefakt.md</code>" in out


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return prepare_workfolder(make_workfolder(tmp_path))


async def test_read_results_names_the_copy_of_a_review_after_the_run(
    wf: Path, tmp_path: Path
) -> None:
    folder = make_run_folder(wf, agents=["judge-1", "author"], status=RunStatus.DONE)
    store = RoomStore.create(folder)
    judge = store.for_agent("judge-1")
    long_review = "Urteil: major\n\n## Einwände\n\n- " + "Lücke bei n ≡ 2 (mod 5). " * 60
    judge.submit_result(kind="review", title="Gutachten Runde 1", body=long_review)
    judge.submit_result(kind="review", title="Gutachten Runde 2", body="Urteil: minor")
    judge.submit_result(kind="review", title="Gutachten Runde 3", body="Urteil: accept")
    results = folder / "output" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "001-judge-1-review.md").write_text(long_review, "utf-8")  # as finalize does
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("nicht von arr", "utf-8")
    os.symlink(elsewhere, results / "002-judge-1-review.md")  # a link is no copy of arr's
    async with (
        Service(make_api(wf)) as service,
        service.client(wf / "projects" / "demo") as client,
    ):
        page = result_data(
            await client.call_tool("read_results", {"run_id": folder.name, "kind": "review"})
        )
    by_id = {r["id"]: r for r in page["results"]}
    assert by_id[1]["path"] == "output/results/001-judge-1-review.md"
    assert len(by_id[1]["agent_text"]) < len(long_review), "the excerpt alone is not the review"
    assert by_id[2]["path"] is None, "a link at the place of the copy gives no path"
    assert by_id[3]["path"] is None, "no copy (run not finalized) gives no path"

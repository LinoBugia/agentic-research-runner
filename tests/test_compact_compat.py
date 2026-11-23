"""W4b-05b: the new fields and the new event kind for the existing readers (A2). The tests of
the old topologies below and above the threshold (A11) went with them in W4b-06b.

A2: `turn_end` carries `context_tokens` / `context_window`, `usage` `context_window` where the
engine reports it; the transcript as Markdown, the usage sums and the service read such a run
(with a note turn and an engine compaction) without a change to `service/` or `usage.py`.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from compact_kit import LOW, compacts, fill, good_note, say, script
from run_kit import (
    agent,
    events,
    make_manager,
    make_workfolder,
    write_profiles,
)

from arr.models import RunStatus
from arr.runs.context import RunContext
from arr.runs.eventlog import render_transcript
from arr.usage import compute_run_usage

OLD_TURN_END_KEYS = {"status", "usage", "cost_usd", "cost_kind", "session", "num_turns"}
NEW_TURN_END_KEYS = {"context_tokens", "context_window"}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# A2: the readers
# --------------------------------------------------------------------------------------------


async def compacted_run(workfolder: Path) -> Any:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins", stage="s")
        await ctx.turn("solo", "zwei", stage="s")
        await ctx.turn("solo", "drei", stage="s")

    turns = script(
        [say("x"), fill(600, cost_usd=0.01, context_window=1_000_000)],
        [say("y"), {"do": "compaction", "tokens_before": 700}, fill(10, cost_usd=0.01)],
        [say("z"), fill(20, cost_usd=0.01)],
        note_turns=[good_note(cost_usd=0.01)],
    )
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager


async def test_a2_new_fields_on_turn_end_and_usage(workfolder: Path) -> None:
    manager = await compacted_run(workfolder)
    log = events(manager.run_dir, "solo")
    for end in (e["data"] for e in log if e["kind"] == "turn_end"):
        assert set(end) == OLD_TURN_END_KEYS | NEW_TURN_END_KEYS
    usages = [e["data"] for e in log if e["kind"] == "usage"]
    assert usages[0]["context_window"] == 1_000_000
    assert "context_window" not in usages[1]  # only where the engine reports it
    kinds = [e["kind"] for e in log]
    assert kinds.count("compaction") == 1
    # the engine's compaction is in the log too, with reason `engine`
    reasons = [e["reason"] for e in compacts(manager.run_dir)]
    assert reasons == ["auto", "engine"]
    assert compacts(manager.run_dir)[1]["tokens"] == 700


async def test_a2_markdown_usage_and_service_read_the_run(
    workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await compacted_run(workfolder)
    run_dir = manager.run_dir
    log = events(run_dir, "solo")

    md = render_transcript(log)
    assert md.count("## Turn ") == 4 and "Die Engine hat die Sitzung verdichtet" in md
    assert "nicht darstellbar" not in md

    usage = compute_run_usage(run_dir, manager.resolved.agents)
    assert usage.by_agent["solo"].turns == 4  # three working turns and the note turn
    assert usage.total.usage.cost_usd == pytest.approx(0.04)

    from arr.service.real_api import RealApi

    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    api = RealApi(workfolder, base_url="http://127.0.0.1:8765", scan=False)
    run_id = manager.prepared.run_id
    runs = await api.list_runs()
    assert run_id in [r.id for r in runs.items]
    detail = await api.get_run(run_id)
    assert detail.run.id == run_id
    agents = await api.list_agents(run_id)
    assert [a.id for a in agents] == ["solo"]
    page = await api.get_transcript(run_id, "solo")
    shown = [str(e.kind) for e in page.items]
    assert shown.count("turn_end") == 4 and "compaction" not in shown  # unknown kinds skipped
    entries = await api.get_log(run_id)
    assert [e.kind for e in entries].count("compact") == 2

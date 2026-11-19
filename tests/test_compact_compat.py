"""W4b-05b: the new fields and the new event kind for the existing readers (A2), and the old
topologies below and above the threshold (A11).

A2: `turn_end` carries `context_tokens` / `context_window`, `usage` `context_window` where the
engine reports it; the transcript as Markdown, the usage sums and the service read such a run
(with a note turn and an engine compaction) without a change to `service/` or `usage.py`.
A11: below the threshold a run is event for event what it is with auto-compact off (`at:
100%`), apart from nothing; above it, arr compacts in `solo` and in a discussion too.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from compact_kit import LOW, compacts, fill, good_note, prompts, say, script, turn_log
from run_kit import (
    agent,
    channel,
    events,
    log_entries,
    make_manager,
    make_workfolder,
    post,
    room,
    write_profiles,
)

from arr.models import RunStatus
from arr.runs.compact import PACKAGE_HEADING, PURPOSE
from arr.runs.context import RunContext
from arr.runs.eventlog import render_transcript
from arr.runs.topologies import solo as solo_topology
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


# --------------------------------------------------------------------------------------------
# A11: old topologies
# --------------------------------------------------------------------------------------------


_TS = re.compile(r'(\\?"ts\\?": )[0-9.]+')


def normalized(run_dir: Path, agent_id: str) -> list[tuple[str, int, Any]]:
    """Events without their times (also those inside a tool result)."""
    return [
        (e["kind"], e["turn"], json.loads(_TS.sub(r"\g<1>0", json.dumps(e["data"]))))
        for e in events(run_dir, agent_id)
    ]


async def solo_run(workfolder: Path, compact: dict[str, Any], first: int) -> Any:
    turns = script(
        [say("noch nichts"), fill(first)],
        [room("submit_result", kind="proof", title="Beweis", body="b"), fill(10)],
        note_turns=[good_note()],
    )
    manager = make_manager(
        workfolder, solo_topology.run, {"solo": turns}, agents=[agent("solo", compact=compact)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager


async def test_a11_solo_below_the_threshold_is_unchanged(workfolder: Path) -> None:
    below = await solo_run(workfolder, {"at": "60%"}, 600)
    off = await solo_run(workfolder, {"at": "100%"}, 600)
    assert normalized(below.run_dir, "solo") == normalized(off.run_dir, "solo")
    kinds = [e["kind"] for e in log_entries(below.run_dir)]
    assert kinds == [e["kind"] for e in log_entries(off.run_dir)]
    assert "compact" not in kinds and "compact_no_window" not in kinds
    assert [k for k, _, _ in normalized(below.run_dir, "solo")] == [
        "turn_start",
        "text",
        "usage",
        "turn_end",
        "turn_start",
        "tool_call",
        "tool_result",
        "usage",
        "turn_end",
    ]
    ends = [d for k, _, d in normalized(below.run_dir, "solo") if k == "turn_end"]
    assert all(set(d) - NEW_TURN_END_KEYS == OLD_TURN_END_KEYS for d in ends)


async def test_a11_solo_above_the_threshold_compacts(workfolder: Path) -> None:
    above = await solo_run(workfolder, LOW, 600)
    assert [p for k, _, p in turn_log(above.run_dir, "solo") if k == "turn"] == [
        None,
        PURPOSE,
        None,
    ]
    assert prompts(above.run_dir, "solo")[2].startswith(PACKAGE_HEADING)
    [result] = above.room.results(agent="solo")
    assert result.title == "Beweis"


async def discussion_run(workfolder: Path, compact: dict[str, Any]) -> tuple[Any, list[Any]]:
    found: list[Any] = []

    async def topology(ctx: RunContext) -> None:
        found.append(await ctx.discuss("main", "Tauscht euch aus.", stage="austausch", rounds=2))

    scripts = {
        "a": script(
            [post("main", "a1"), fill(600)],
            [post("main", "a2"), fill(10)],
            note_turns=[good_note()],
        ),
        "b": script([post("main", "b1"), fill(10)], [post("main", "b2"), fill(10)]),
    }
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        agents=[agent("a", compact=compact), agent("b", compact=compact)],
        groupchats=[channel("main", ["a", "b"], mode="rounds")],
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager, found


async def test_a11_discussion_below_and_above_the_threshold(workfolder: Path) -> None:
    below, found_below = await discussion_run(workfolder, {"at": "60%"})
    off, found_off = await discussion_run(workfolder, {"at": "100%"})
    for agent_id in ("a", "b"):
        assert normalized(below.run_dir, agent_id) == normalized(off.run_dir, agent_id)
    assert [e["kind"] for e in log_entries(below.run_dir)] == [
        e["kind"] for e in log_entries(off.run_dir)
    ]
    above, found_above = await discussion_run(workfolder, LOW)
    [outcome] = found_above
    assert outcome.rounds == 2 and len(outcome.outcomes) == 4  # the note turn is no outcome
    assert len(found_below[0].outcomes) == 4
    purposes = [p for k, _, p in turn_log(above.run_dir, "a") if k == "turn"]
    assert purposes == [None, PURPOSE, None]
    assert [p for k, _, p in turn_log(above.run_dir, "b") if k == "turn"] == [None, None]
    sent = prompts(above.run_dir, "a")[2]
    assert sent.startswith(PACKAGE_HEADING) and "## Posteingang" in sent
    states = {s.agent: s.wakeups for s in above.room.agent_states()}
    assert states.get("a", 0) == 0
    assert sorted(m.body for m in above.room.messages("main"))[-4:] == ["a1", "a2", "b1", "b2"]

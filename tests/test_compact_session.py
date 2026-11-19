"""W4b-05b: `Orchestrator.compact_session` (A8), the rebuild package in a run of an old
topology (A7), one log entry per compaction and a note that reaches nobody else (A12)."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from compact_kit import (
    LOW,
    compacts,
    ends,
    fill,
    good_note,
    prompts,
    say,
    script,
    turn_log,
)
from run_kit import (
    agent,
    channel,
    eventually,
    log_entries,
    make_manager,
    make_workfolder,
    post,
    room,
    wait,
)

from arr.errors import ArrError
from arr.models import RunStatus
from arr.runs.compact import PACKAGE_HEADING
from arr.runs.context import RunContext
from arr.runs.orchestrator import Orchestrator
from arr.runs.topologies import solo as solo_topology

PRIVATE = "GEHEIM-NOTIZ-4711"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# A8: compact_session
# --------------------------------------------------------------------------------------------


async def run_phase(workfolder: Path, between: Any, *, fills: tuple[int, ...] = (600, 10)) -> Any:
    """Two turns of `solo` (stage a, then stage b) with `between(ctx)` in between."""

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "Phase A.", stage="a")
        await between(ctx)
        await ctx.turn("solo", "Phase B.", stage="b")

    turns = script(*[[say("x"), fill(n)] for n in fills], note_turns=[good_note()])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager


async def test_a8_compact_session_with_a_note(workfolder: Path) -> None:
    async def between(ctx: RunContext) -> None:
        await ctx.compact_session("solo", note=f"Merke: {PRIVATE}‮")

    manager = await run_phase(workfolder, between)
    run_dir = manager.run_dir
    # no note turn, although the fill level (600) was over the threshold: the session is new
    assert turn_log(run_dir, "solo") == [
        ("turn", 1, None),
        ("turn_end", 1, None),
        ("compact", 0, "phase"),
        ("turn", 2, None),
        ("turn_end", 2, None),
    ]
    [entry] = compacts(run_dir)
    assert entry == {
        "agent": "solo",
        "reason": "phase",
        "stage": "a",  # the agent's stage when its session ended
        "tokens": 600,
        "window": 1_000_000,
        "threshold": None,
        "note_chars": len(f"Merke: {PRIVATE}"),
        "ok": True,
    }
    sent = prompts(run_dir, "solo")[1]
    assert sent.startswith(PACKAGE_HEADING) and f"Merke: {PRIVATE}" in sent
    assert "‮" not in sent  # cleaned
    assert "Er steht unten" in sent and sent.endswith("Phase B.")  # a new action: this turn
    assert manager.backends["solo"].history == [sent]
    assert [e["session"] for e in ends(run_dir, "solo")] == ["fake-solo", "fake-solo-2"]


async def test_a8_compact_session_without_a_note(workfolder: Path) -> None:
    async def between(ctx: RunContext) -> None:
        await ctx.compact_session("solo")

    manager = await run_phase(workfolder, between)
    sent = prompts(manager.run_dir, "solo")[1]
    assert sent.startswith(PACKAGE_HEADING) and "### Deine eigene Notiz" not in sent
    [entry] = compacts(manager.run_dir)
    assert (entry["note_chars"], entry["ok"]) == (0, True)


async def test_a8_called_twice_the_last_note_counts(workfolder: Path) -> None:
    async def between(ctx: RunContext) -> None:
        await ctx.compact_session("solo", note="erste Notiz")
        await ctx.compact_session("solo", note="zweite Notiz")

    manager = await run_phase(workfolder, between)
    sent = prompts(manager.run_dir, "solo")[1]
    assert "zweite Notiz" in sent and "erste Notiz" not in sent
    assert [e["reason"] for e in compacts(manager.run_dir)] == ["phase", "phase"]
    assert sent.count(PACKAGE_HEADING) == 1


async def test_a8_waits_for_the_running_turn(workfolder: Path) -> None:
    order: list[str] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        turn = asyncio.create_task(ctx.turn("solo", "lang"))
        await eventually(lambda: "solo" in ctx._active)
        await ctx.compact_session("solo", note="nach dem Turn")
        order.append("compacted")
        assert turn.done()
        order.append((await turn).status.value)
        await ctx.turn("solo", "danach")

    turns = script([say("x"), wait(0.3), fill(10)], [say("y"), fill(10)])
    manager = make_manager(workfolder, topology, {"solo": turns})
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert order == ["compacted", "ok"]
    turn_end = next(e for e in log_entries(manager.run_dir, "turn_end"))
    [compact] = log_entries(manager.run_dir, "compact")
    assert compact["ts"] >= turn_end["ts"]
    assert "nach dem Turn" in prompts(manager.run_dir, "solo")[1]


async def test_a8_unknown_agent(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        with pytest.raises(ArrError, match="„niemand“ gibt es in diesem Run nicht"):
            await ctx.compact_session("niemand", note="x")
        with pytest.raises(ArrError, match="„niemand“ gibt es in diesem Run nicht"):
            await ctx.reset_session("niemand")  # the same error as `reset_session`

    manager = make_manager(workfolder, topology, {"solo": script()})
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert compacts(manager.run_dir) == []


async def test_a8_reset_session_makes_the_fill_level_unknown(workfolder: Path) -> None:
    """After `reset_session` there is no measurement: no note turn before the next turn."""

    async def between(ctx: RunContext) -> None:
        await ctx.reset_session("solo")

    manager = await run_phase(workfolder, between)
    assert compacts(manager.run_dir) == []
    assert not prompts(manager.run_dir, "solo")[1].startswith(PACKAGE_HEADING)


# --------------------------------------------------------------------------------------------
# A7: package in an old topology (results instead of submissions)
# --------------------------------------------------------------------------------------------


async def test_a7_package_in_the_old_topology_solo(workfolder: Path) -> None:
    """The real `solo` topology: turn 1 without a result, over the threshold; before turn 2
    the note turn; turn 2 (the same prompt, the task) gets the package."""
    turns = script(
        [say("noch nichts"), fill(600)],
        [room("submit_result", kind="proof", title="Beweis", body="b"), fill(10)],
        note_turns=[good_note()],
    )
    manager = make_manager(
        workfolder, solo_topology.run, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    sent = prompts(manager.run_dir, "solo")
    assert len(sent) == 3 and sent[2].startswith(PACKAGE_HEADING)
    package = sent[2]
    assert f"### Dein laufender Auftrag\n\n{sent[0]}" in package  # the task, first prompt
    reminder = manager.room.assignment("solo").prompt
    assert reminder != sent[0] and package.endswith(reminder), "this turn's prompt follows"
    assert "### Deine Abgaben\n\nKeine." in package
    [result] = manager.room.results(agent="solo")
    assert result.title == "Beweis"


async def test_a7_old_results_in_the_package(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "Sammle Ideen.", stage="ideen")
        await ctx.turn("solo", "Weiter.", stage="ideen")

    turns = script(
        [room("submit_result", kind="idea", title="Ansatz B", body="b"), fill(600)],
        [fill(10)],
        note_turns=[good_note()],
    )
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    package = prompts(manager.run_dir, "solo")[2]
    [result] = manager.room.results(agent="solo")
    assert f"- #{result.id} · idea · „Ansatz B“ · Phase ideen" in package
    assert result.path is None and "Pfad" not in package.split("### Deine eigene Notiz")[0]
    assert "Sammle Ideen." in package and package.endswith("Weiter.")


# --------------------------------------------------------------------------------------------
# A12: one entry per compaction, the note only in the agent's own transcript
# --------------------------------------------------------------------------------------------


async def test_a12_note_reaches_nobody_else(workfolder: Path) -> None:
    secret_note = f"### Stand\n{PRIVATE} – nur für mich."

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins", stage="s")
        await ctx.turn("peer", "Lies mit.", stage="s")
        await ctx.turn("solo", "zwei", stage="s")  # auto-compact with the secret note
        await ctx.compact_session("solo", note=f"{PRIVATE} phase")
        await ctx.turn("peer", "Lies mit.", stage="s")
        await ctx.turn("solo", "drei", stage="s")

    turns = script(
        [post("main", "Hallo"), fill(600)],
        [post("main", "Ich bin wieder da"), fill(10)],
        [fill(10)],
        note_turns=[[say(secret_note), fill(650)]],
    )
    peer = script([post("main", "Hallo zurück")], [post("main", "Gut")])
    manager = make_manager(
        workfolder,
        topology,
        {"solo": turns, "peer": peer},
        agents=[agent("solo", compact=LOW), agent("peer")],
        groupchats=[channel("main", ["solo", "peer"])],
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir
    assert [e["reason"] for e in compacts(run_dir)] == ["auto", "phase"]  # one entry each

    hits = [
        str(path.relative_to(run_dir))
        for path in run_dir.rglob("*")
        if path.is_file() and PRIVATE.encode() in path.read_bytes()
    ]
    # only the agent's own transcript (normalized and raw); not orchestrator.jsonl, not the
    # room, not the other agent, not session.json, not the assignment
    own = {"agents/solo/raw.jsonl", "agents/solo/transcript.jsonl", "agents/solo/transcript.md"}
    assert {"agents/solo/raw.jsonl", "agents/solo/transcript.jsonl"} <= set(hits) <= own, hits
    assert all(PRIVATE not in p for p in prompts(run_dir, "peer"))
    with sqlite3.connect(manager.room.db_path) as con:
        for table, column in (("msg", "body"), ("assignment", "prompt")):
            rows = con.execute(f"SELECT {column} FROM {table}").fetchall()  # noqa: S608
            assert all(PRIVATE not in str(row[0]) for row in rows), table
    log = json.dumps(log_entries(run_dir))
    assert PRIVATE not in log
    # but the agent itself got both notes, each in the first prompt of its new session
    sent = prompts(run_dir, "solo")
    assert PRIVATE in sent[2] and f"{PRIVATE} phase" in sent[3]

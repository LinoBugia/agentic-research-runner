"""W4b-05b.1: the rework after the review, in a running run (FakeBackend, the real room).

Point 1 (M1): a rate limit from the note turn holds the working turn after it.
Point 2 (M2): a turn with requests but no fill level is logged once (`compact_no_fill`).
Point 4 (M4, Entscheidung 53): in the note turn the room refuses the agent's tools.
Point 5 (E6): `reset_session` drops a package of `compact_session`.
Point 6 (E7): a compaction of the engine starts the count of failed notes over.
Point 7 (G2, G3, G4, G6): sealed submissions, `compact_session` without a session or after
the run, a package that survives a turn that never began, the note turn in Markdown.
Point 8 (G7): the mutations M20, M25 that survived the review.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from compact_kit import (
    LOW,
    NOTE,
    FailingResetBackend,
    FailingStartBackend,
    compacts,
    ends,
    fill,
    good_note,
    prompts,
    say,
    script,
    starts,
    submit,
    swap_backends,
    turn_log,
)
from run_kit import (
    agent,
    channel,
    events,
    eventually,
    log_entries,
    make_manager,
    make_workfolder,
    post,
    room,
    wait,
)

from arr.backends.base import TurnStatus
from arr.models import RunStatus
from arr.room.store import NOTE_TURN_REFUSED
from arr.room.types import WaitKind
from arr.runs.compact import PACKAGE_HEADING, PURPOSE
from arr.runs.context import RunContext
from arr.runs.eventlog import render_transcript
from arr.runs.orchestrator import Orchestrator, StopReason

OFF = {"at": "100%"}
SUB = {"submission": {"format": "text"}}
PAUSE_S = 3.0
"""Rate-limit pause in the tests of point 1."""


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def note_active(orch: Orchestrator, agent_id: str = "solo") -> bool:
    return orch.room.in_note_turn(agent_id) and agent_id in orch._active


def turn_starts(run_dir: Path, agent_id: str) -> list[tuple[int, str | None, float]]:
    return [(s["turn"], s["data"].get("purpose"), s["ts"]) for s in starts(run_dir, agent_id)]


# --------------------------------------------------------------------------------------------
# Point 1: rate limit from the note turn
# --------------------------------------------------------------------------------------------


async def rate_limit_run(workfolder: Path, where: str) -> tuple[Any, float]:
    """`where`: `note` (rate limit in the note turn), `work` (in working turn 1, without
    compaction: the counter-check) or `none` (compaction without a rate limit)."""
    marks: dict[str, float] = {}

    def solo() -> dict[str, Any]:
        marks["resets_at"] = time.time() + PAUSE_S
        limit = {"do": "rate_limit", "status": "rejected", "resets_at": marks["resets_at"]}
        first = [say("eins"), fill(600)] if where != "work" else [say("eins"), limit, fill(600)]
        note = [limit, *good_note()] if where == "note" else good_note()
        return script(first, [say("zwei"), fill(10)], note_turns=[note])

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        await ctx.turn("solo", "zwei")

    compact = OFF if where == "work" else LOW
    manager = make_manager(
        workfolder, topology, {"solo": solo}, agents=[agent("solo", compact=compact)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager, marks["resets_at"]


@pytest.mark.parametrize("where", ["note", "work"])
async def test_p1_rate_limit_holds_the_next_working_turn(workfolder: Path, where: str) -> None:
    manager, resets_at = await rate_limit_run(workfolder, where)
    run_dir = manager.run_dir
    [limit] = log_entries(run_dir, "rate_limit")
    assert limit["data"]["action"] == "pause"
    work = [ts for _, purpose, ts in turn_starts(run_dir, "solo") if purpose is None]
    assert work[-1] >= resets_at - 0.05, "the working turn waited for the end of the pause"
    if where == "note":  # the wait is visible, as for any new turn
        waits = [e["data"] for e in log_entries(run_dir, "waiting") if e["data"]["agent"] == "solo"]
        assert [w["reason"] for w in waits] == ["capacity"]
        assert waits[0]["until"] == pytest.approx(resets_at, abs=0.05)
    assert manager.orchestrator._gates["fake-auth"].active == 0


async def test_p1_note_turn_without_rate_limit_does_not_delay(workfolder: Path) -> None:
    """Without a rate limit the working turn follows the note turn without waiting for the
    profile: no entry `waiting` (an order and an absent wait, no duration; N7)."""
    manager, _ = await rate_limit_run(workfolder, "none")
    run_dir = manager.run_dir
    assert not log_entries(run_dir, "rate_limit") and not log_entries(run_dir, "waiting")
    starts = turn_starts(run_dir, "solo")
    assert [purpose for _, purpose, _ in starts] == [None, PURPOSE, None]
    note_end = next(
        e["ts"] for e in events(run_dir, "solo") if e["kind"] == "turn_end" and e["turn"] == 2
    )
    assert starts[2][2] >= note_end


async def test_p1_emergency_stop_while_waiting_after_the_note_turn(workfolder: Path) -> None:
    outcomes: list[Any] = []
    long_pause = 30.0

    def solo() -> dict[str, Any]:
        limit = {"do": "rate_limit", "status": "rejected", "resets_at": time.time() + long_pause}
        return script(
            [say("eins"), fill(600)], [say("zwei"), fill(10)], note_turns=[[limit, *good_note()]]
        )

    async def topology(ctx: RunContext) -> None:
        outcomes.append(await ctx.turn("solo", "eins"))
        outcomes.append(await ctx.turn("solo", "zwei"))

    manager = make_manager(
        workfolder, topology, {"solo": solo}, agents=[agent("solo", compact=LOW)]
    )
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and manager.orchestrator.waits_for_capacity("solo")
    )
    started = time.monotonic()
    await manager.orchestrator.halt(StopReason.PROFILE_BLOCKED, profile="fake-auth")
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert time.monotonic() - started < long_pause / 2
    assert [o.status for o in outcomes] == [TurnStatus.OK, TurnStatus.INTERRUPTED]
    assert len(prompts(manager.run_dir, "solo")) == 2  # no working turn after the note turn
    assert manager.orchestrator._gates["fake-auth"].active == 0  # the slot count stays right


# --------------------------------------------------------------------------------------------
# Point 2: a fill level that stays unknown is logged once
# --------------------------------------------------------------------------------------------


async def test_p2_unknown_fill_level_is_logged_once(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        for n in range(3):
            await ctx.turn("solo", f"Turn {n}.")

    other = fill(900, model="ein-anderes-modell")  # no request of the agent's own model
    turns = script([other], [other], [fill(10)], note_turns=[good_note()])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    [entry] = log_entries(manager.run_dir, "compact_no_fill")
    assert entry["data"] == {"agent": "solo", "model": "fake-model"}
    assert [e["context_tokens"] for e in ends(manager.run_dir, "solo")] == [None, None, 10]
    assert compacts(manager.run_dir) == []

    # counter-check: unknown after an engine compaction is no finding
    async def two(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        await ctx.turn("solo", "zwei")

    compacted = script([fill(10), {"do": "compaction"}], [fill(10)])
    again = make_manager(make_workfolder(workfolder.parent / "b"), two, {"solo": compacted})
    assert await asyncio.wait_for(again.run(), 30) is RunStatus.DONE
    assert not log_entries(again.run_dir, "compact_no_fill")


# --------------------------------------------------------------------------------------------
# Point 4: the room refuses the agent's tools in the note turn (Entscheidung 53)
# --------------------------------------------------------------------------------------------

REFUSED_TOOLS = [
    room("submit", status="done", summary="im Notiz-Turn", body="b"),
    post("main", "im Notiz-Turn"),
    post("main", "Frage im Notiz-Turn?", kind="question", to=["user"]),
    room("set_status", status="done", note="fertig"),
    room("wait_for_messages", seconds=1),
    room("wait_until", minutes=1),
    room("read_messages"),
]


async def test_p4_note_turn_refuses_the_tools_and_leaves_nothing(workfolder: Path) -> None:
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        await ctx.turn("solo", "eins", stage="s", context=SUB)
        ctx.view("peer").post_message("main", "Nachricht an solo", to=["solo"])
        seen["cursor"] = ctx.room.cursor("solo", "main")

        async def peer_during_note_turn() -> None:
            await eventually(lambda: note_active(ctx), within=10)
            # another agent's room works while solo writes its note
            seen["peer"] = ctx.view("peer").post_message("main", "Peer im Notiz-Turn von solo")
            seen["cursor_after_note"] = None

        task = asyncio.create_task(peer_during_note_turn())
        await ctx.turn("solo", "zwei", stage="s", context=SUB)
        await task
        seen["flag_after"] = ctx.room.in_note_turn("solo")

    solo = script(
        [say("eins"), fill(600)],
        [  # the working turn after the note turn: the same tools work again
            submit(status="done", summary="im Arbeits-Turn", body="b"),
            post("main", "im Arbeits-Turn"),
            room("read_messages"),
            fill(10),
        ],
        note_turns=[[wait(0.3), *REFUSED_TOOLS, say(NOTE), fill(650)]],
    )
    manager = make_manager(
        workfolder,
        topology,
        {"solo": solo, "peer": script()},
        agents=[agent("solo", compact=LOW), agent("peer")],
        groupchats=[channel("main", ["solo", "peer"])],
        run_extra={"topology": {"kind": "phases"}},
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir, store = manager.run_dir, manager.room
    log = events(run_dir, "solo")
    note_results = [e["data"] for e in log if e["turn"] == 2 and e["kind"] == "tool_result"]
    assert len(note_results) == len(REFUSED_TOOLS)
    assert all(r["is_error"] and r["content"] == NOTE_TURN_REFUSED for r in note_results)
    # nothing left behind by the note turn
    assert [s.summary for s in store.submissions(agent="solo")] == ["im Arbeits-Turn"]
    assert store.results(agent="solo") == [] and store.ratings() == []
    bodies = [m.body for m in store.messages("main") if m.sender == "solo"]
    assert bodies == ["im Arbeits-Turn"]
    assert store.open_questions_by("solo") == [] and manager.inbox.items() == []
    assert "solo" not in store.reports()
    assert not any(store.waits(kind) for kind in WaitKind)
    # the read position did not move in the note turn: the working turn still saw the message
    assert "Nachricht an solo" in prompts(run_dir, "solo")[2]
    work_results = [e["data"] for e in log if e["turn"] == 3 and e["kind"] == "tool_result"]
    assert [r["is_error"] for r in work_results] == [False, False, False]
    assert seen["peer"].body == "Peer im Notiz-Turn von solo"
    assert seen["flag_after"] is False


@pytest.mark.parametrize("how", ["error", "stop"])
async def test_p4_the_flag_is_gone_after_a_failed_or_stopped_note_turn(
    workfolder: Path, how: str
) -> None:
    flags: list[bool] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        await ctx.turn("solo", "eins")
        await ctx.turn("solo", "zwei")
        flags.append(ctx.room.in_note_turn("solo"))

    if how == "error":
        note = [post("main", "x"), {"do": "end", "status": "error", "message": "kaputt"}]
    else:
        note = [post("main", "x"), wait(30), *good_note()]
    solo = script(
        [say("eins"), fill(600)], [post("main", "im Arbeits-Turn"), fill(10)], note_turns=[note]
    )
    manager = make_manager(
        workfolder,
        topology,
        {"solo": solo},
        agents=[agent("solo", compact=LOW)],
        groupchats=[channel("main", ["solo"])],
    )
    running = asyncio.create_task(manager.run())
    if how == "stop":
        await eventually(
            lambda: manager.orchestrator is not None and note_active(manager.orchestrator)
        )
        manager.stop()
        assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    else:
        assert await asyncio.wait_for(running, 30) is RunStatus.DONE
        assert flags == [False]
        # the working turn after the failed note turn posts as before
        bodies = [m.body for m in manager.room.messages("main") if m.sender == "solo"]
        assert bodies == ["im Arbeits-Turn"]
    assert manager.room.in_note_turn("solo") is False


# --------------------------------------------------------------------------------------------
# Point 5: reset_session drops a package
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["compact_then_reset", "reset_then_compact"])
async def test_p5_reset_session_drops_the_package(workfolder: Path, order: str) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        if order == "compact_then_reset":
            await ctx.compact_session("solo", note="NOTIZ-P5")
            await ctx.reset_session("solo")
        else:
            await ctx.reset_session("solo")
            await ctx.compact_session("solo", note="NOTIZ-P5")
        await ctx.turn("solo", "zwei")

    manager = make_manager(workfolder, topology, {"solo": script([fill(10)], [fill(10)])})
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    sent = prompts(manager.run_dir, "solo")[1]
    if order == "compact_then_reset":
        assert sent == "zwei" and "NOTIZ-P5" not in sent
    else:
        assert sent.startswith(PACKAGE_HEADING) and "NOTIZ-P5" in sent


# --------------------------------------------------------------------------------------------
# Point 6: an engine compaction starts the count of failed notes over
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", [True, False])
async def test_p6_engine_compaction_resets_the_failures(workfolder: Path, engine: bool) -> None:
    async def topology(ctx: RunContext) -> None:
        for n in range(4):
            await ctx.turn("solo", f"Turn {n}.")

    failing = [{"do": "end", "status": "error", "message": "kaputt"}]
    third = [{"do": "compaction"}, fill(900)] if engine else [fill(900)]
    turns = script(
        [fill(900)], [fill(900)], third, [fill(10)], note_turns=[failing, failing, good_note()]
    )
    turns["notes"]["repeat_last"] = False
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    auto = [
        (e["ok"], e.get("failures")) for e in compacts(manager.run_dir) if e["reason"] == "auto"
    ]
    if engine:  # after the engine's compaction arr tries again, and it works
        assert auto == [(False, 1), (False, 2), (True, None)]
    else:  # counter-check: two failures in a row, then never again
        assert auto == [(False, 1), (False, 2)]


# --------------------------------------------------------------------------------------------
# Point 7: sealed, no session, after the run, a turn that never began, Markdown
# --------------------------------------------------------------------------------------------


async def test_g2_sealed_own_submission_without_a_path(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        ctx.seal_results("s")
        await ctx.turn("solo", "eins", stage="s", context=SUB)
        await ctx.turn("solo", "zwei", stage="s", context=SUB)

    solo = script(
        [submit(status="done", summary="Versiegelt.", body="b"), fill(600)],
        [fill(10)],
        note_turns=[good_note()],
    )
    manager = make_manager(
        workfolder,
        topology,
        {"solo": solo},
        agents=[agent("solo", compact=LOW)],
        run_extra={"topology": {"kind": "phases"}},
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    [sub] = manager.room.submissions(agent="solo")
    assert sub.sealed and "sealed" in sub.path  # positive control
    package = prompts(manager.run_dir, "solo")[2]
    assert "versiegelt bis zur Freigabe" in package and "Kurzfassung: Versiegelt." in package
    assert "groupchats/sealed" not in package and "Pfad:" not in package


async def test_g3_compact_session_without_a_session_and_after_the_run(workfolder: Path) -> None:
    holder: list[Orchestrator] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        holder.append(ctx)
        await ctx.compact_session("solo", note="vor dem ersten Turn")
        await ctx.turn("solo", "eins")

    manager = make_manager(workfolder, topology, {"solo": script([fill(10)])})
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert prompts(manager.run_dir, "solo") == ["eins"]  # no package without a session
    assert compacts(manager.run_dir) == []
    lines = len(log_entries(manager.run_dir))
    await asyncio.wait_for(holder[0].compact_session("solo", note="nach dem Ende"), 5)
    assert len(log_entries(manager.run_dir)) == lines  # nothing written after the end


async def test_g4_package_waits_for_a_turn_that_began(workfolder: Path) -> None:
    outcomes: list[Any] = []

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        await ctx.compact_session("solo", note="NOTIZ-G4")
        outcomes.append(await ctx.turn("solo", "zwei"))  # fails before its turn_start
        outcomes.append(await ctx.turn("solo", "drei"))

    manager = make_manager(workfolder, topology, {"solo": script([fill(10)], [fill(10)])})
    swap_backends(manager, FailingStartBackend, fails=2)
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert outcomes[0].status is TurnStatus.ERROR
    sent = prompts(manager.run_dir, "solo")
    assert sent[-1].startswith(PACKAGE_HEADING) and "NOTIZ-G4" in sent[-1]
    assert sent[-1].endswith("drei")


async def test_g6_markdown_marks_the_note_turn(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        await ctx.turn("solo", "zwei")

    turns = script([fill(600)], [fill(10)], note_turns=[good_note()])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    md = render_transcript(events(manager.run_dir, "solo"))
    assert "## Turn 2 (Übergabe-Notiz)" in md
    assert "## Turn 1 ·" in md and "## Turn 3 ·" in md  # the others unchanged


# --------------------------------------------------------------------------------------------
# Point 8: mutations M20 and M25
# --------------------------------------------------------------------------------------------


async def test_m25_reset_fails_after_a_good_note(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        for n in range(3):
            await ctx.turn("solo", f"Turn {n}.")

    turns = script([fill(900)], [fill(900)], [fill(10)], note_turns=[good_note()])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    swap_backends(manager, FailingResetBackend)
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    entries = compacts(manager.run_dir)
    assert [(e["ok"], e["failures"], e["note_chars"]) for e in entries] == [
        (False, 1, 0),
        (False, 2, 0),
    ]
    assert not any(p.startswith(PACKAGE_HEADING) for p in prompts(manager.run_dir, "solo"))
    assert {e["session"] for e in ends(manager.run_dir, "solo")} == {"fake-solo"}
    purposes = [p for k, _, p in turn_log(manager.run_dir, "solo") if k == "turn"]
    assert purposes == [None, PURPOSE, None, PURPOSE, None]

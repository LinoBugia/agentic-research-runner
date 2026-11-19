"""W4b-05b: the note turn is a technical turn (A5), a failed one keeps the session (A6), and
stop, emergency stop, pause, budget and turn limit work during it (A10).

Each technical property is checked against the same run below the threshold (`at: 100%`).
"""

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from compact_kit import (
    LOW,
    NOTE,
    compacts,
    ends,
    fill,
    good_note,
    is_note_prompt,
    prompts,
    say,
    script,
    starts,
    submit,
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
    room,
    wait,
)

from arr.backends.base import TurnStatus
from arr.models import RunStatus
from arr.runs.compact import PACKAGE_HEADING, PURPOSE
from arr.runs.context import RunContext
from arr.runs.orchestrator import Orchestrator, StopReason
from arr.usage import compute_run_usage

OFF = {"at": "100%"}
SUB = {"submission": {"format": "text"}}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


class TurnEnds:
    """A pacing listener: which turn ends reach the discussions."""

    def __init__(self) -> None:
        self.ends: list[str] = []

    def on_message(self, msg: Any) -> None:
        pass

    def on_turn_end(self, agent_id: str, undelivered: Any) -> None:
        self.ends.append(agent_id)

    def kick(self) -> None:
        pass


def note_active(orch: Orchestrator, agent_id: str = "solo") -> bool:
    active = orch._active.get(agent_id)
    return active is not None and active.retry_at_calls == sys.maxsize


# --------------------------------------------------------------------------------------------
# A5: technical turn, with the counter-check below the threshold
# --------------------------------------------------------------------------------------------


async def technical_run(workfolder: Path, compact: dict[str, Any]) -> tuple[Any, TurnEnds]:
    listener = TurnEnds()

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        ctx.listen(listener)
        await ctx.turn("solo", "Auftrag.", stage="s", context={**SUB, "attempt": 1})
        ctx.view("peer").post_message("main", "Nachricht an solo", to=["solo"])
        await ctx.turn("solo", "Weiter.", stage="s", context={**SUB, "attempt": 2})
        ctx.unlisten(listener)

    solo = script(
        [
            submit(status="done", summary="Fassung 1.", body="B1"),
            room("set_status", status="done"),
            say("eins"),
            fill(600),
        ],
        [say("zwei"), fill(100, cost_usd=0.02)],
        note_turns=[
            [
                submit(status="done", summary="Im Notiz-Turn.", body="B2"),  # the old assignment
                say(NOTE),
                fill(650, cost_usd=0.01),
            ]
        ],
    )
    manager = make_manager(
        workfolder,
        topology,
        {"solo": solo, "peer": script()},
        agents=[agent("solo", compact=compact), agent("peer")],
        groupchats=[channel("main", ["solo", "peer"])],
        run_extra={"topology": {"kind": "phases"}},
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager, listener


def assignments(manager: Any) -> list[tuple[int, str, str]]:
    with sqlite3.connect(manager.room.db_path) as con:
        rows = con.execute("SELECT turn, stage, prompt FROM assignment WHERE agent='solo'")
        return [tuple(row) for row in rows]


async def test_a5_note_turn_is_technical(workfolder: Path) -> None:
    compacted, listener = await technical_run(workfolder, LOW)
    below, below_listener = await technical_run(workfolder, OFF)
    run_dir, base_dir = compacted.run_dir, below.run_dir

    # numbered, flagged, with the agent's stage; in the counter-check there is none
    assert turn_log(run_dir, "solo") == [
        ("turn", 1, None),
        ("turn_end", 1, None),
        ("turn", 2, PURPOSE),
        ("turn_end", 2, PURPOSE),
        ("compact", 0, "auto"),
        ("turn", 3, None),
        ("turn_end", 3, None),
    ]
    assert [e["data"]["stage"] for e in log_entries(run_dir, "turn")] == ["s", "s", "s"]
    assert [s["data"].get("stage") for s in starts(run_dir, "solo")] == ["s", "s", "s"]
    assert turn_log(base_dir, "solo")[2:] == [("turn", 2, None), ("turn_end", 2, None)]

    # no assignment: the same two assignments as below the threshold
    assert [(s, p) for _, s, p in assignments(compacted)] == [("s", "Auftrag."), ("s", "Weiter.")]
    assert [(s, p) for _, s, p in assignments(below)] == [("s", "Auftrag."), ("s", "Weiter.")]
    assert [t for t, _, _ in assignments(compacted)] == [1, 3]

    # a `submit` in the note turn hits the assignment from before (attempt 1)
    subs = compacted.room.submissions(agent="solo")
    assert [(s.summary, s.stage, s.attempt) for s in subs] == [
        ("Fassung 1.", "s", 1),
        ("Im Notiz-Turn.", "s", 1),
    ]

    # no inbox in the note turn; the message waits for the turn after it
    sent = prompts(run_dir, "solo")
    assert is_note_prompt(sent[1]) and "Posteingang" not in sent[1]
    assert "Nachricht an solo" not in sent[1]
    assert "## Posteingang" in sent[2] and sent[2].startswith(PACKAGE_HEADING)
    assert "## Posteingang" in prompts(base_dir, "solo")[1]

    # phase and `done` unchanged, no wake-up, no turn end for the pacing listeners
    def done_reasons(run_dir: Path) -> list[str]:
        return [e["data"]["reason"] for e in log_entries(run_dir, "agent_done")]

    assert done_reasons(run_dir) == ["reported"]
    assert done_reasons(base_dir) == ["reported"]
    assert compacted.orchestrator.agents["solo"].status is below.orchestrator.agents["solo"].status
    assert not log_entries(run_dir, "agent_phase") and not log_entries(base_dir, "agent_phase")
    states = {s.agent: s for s in compacted.room.agent_states()}
    assert states["solo"].wakeups == 0
    assert listener.ends == below_listener.ends == ["solo", "solo"]

    # counted: usage, cost and budget, `turns_per_agent`
    usage = compute_run_usage(run_dir, compacted.resolved.agents)
    base_usage = compute_run_usage(base_dir, below.resolved.agents)
    assert usage.by_agent["solo"].turns == base_usage.by_agent["solo"].turns + 1
    assert usage.by_agent["solo"].usage.input == base_usage.by_agent["solo"].usage.input + 650
    assert compacted.orchestrator.cost_usd == pytest.approx(below.orchestrator.cost_usd + 0.01)
    assert compacted.orchestrator.agents["solo"].turns_used == 3
    assert below.orchestrator.agents["solo"].turns_used == 2


async def test_a5_no_hints_during_the_note_turn(workfolder: Path) -> None:
    """A message during the note turn is no hint; the same message during a working turn is
    one (counter-check)."""

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)

        async def post_when(check: Any) -> None:
            await eventually(lambda: check(ctx), within=10)
            ctx.view("peer").post_message("main", "Mitten im Turn", to=["solo"])

        await ctx.turn("solo", "eins")
        poster = asyncio.create_task(post_when(note_active))
        await ctx.turn("solo", "zwei")
        await poster
        poster = asyncio.create_task(post_when(lambda o: "solo" in o._active))
        await ctx.turn("solo", "drei")
        await poster

    solo = script(
        [say("eins"), fill(600)],
        [say("zwei"), fill(10)],
        [say("drei"), wait(0.6), fill(10)],
        note_turns=[[wait(0.6), say(NOTE), fill(650)]],
    )
    manager = make_manager(
        workfolder,
        topology,
        {"solo": solo, "peer": script()},
        agents=[agent("solo", compact=LOW), agent("peer")],
        groupchats=[channel("main", ["solo", "peer"])],
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    log = events(manager.run_dir, "solo")
    hints = [e["turn"] for e in log if e["kind"] == "hint"]
    assert hints == [4], "only the working turn after the note turn got the hint"
    assert "Mitten im Turn" in prompts(manager.run_dir, "solo")[2]  # unread, in the inbox


# --------------------------------------------------------------------------------------------
# A6: a failed note turn
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failing",
    [
        [{"do": "end", "status": "error", "message": "kaputt"}],
        [fill(650)],  # an empty answer
        [say("  ​ "), fill(650)],  # only invisible text
    ],
)
async def test_a6_failed_note_keeps_the_session_twice_at_most(
    workfolder: Path, failing: list[dict[str, Any]]
) -> None:
    async def topology(ctx: RunContext) -> None:
        for n in range(4):
            await ctx.turn("solo", f"Turn {n}.")

    turns = script(*[[say("x"), fill(900)] for _ in range(4)], note_turns=[failing])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir
    purposes = [p for k, _, p in turn_log(run_dir, "solo") if k == "turn"]
    assert purposes == [None, PURPOSE, None, PURPOSE, None, None]
    entries = compacts(run_dir)
    assert [(e["ok"], e["failures"], e["note_chars"]) for e in entries] == [
        (False, 1, 0),
        (False, 2, 0),
    ]
    assert {e["session"] for e in ends(run_dir, "solo")} == {"fake-solo"}  # the session stays
    assert not any(p.startswith(PACKAGE_HEADING) for p in prompts(run_dir, "solo"))
    assert len(manager.backends["solo"].history) == 6


async def test_a6_a_failure_counts_only_in_a_row(workfolder: Path) -> None:
    """Fail, succeed, fail: after a new session the count starts over."""

    async def topology(ctx: RunContext) -> None:
        for n in range(4):
            await ctx.turn("solo", f"Turn {n}.")

    failing = [{"do": "end", "status": "error", "message": "kaputt"}]
    turns = script(
        *[[say("x"), fill(900)] for _ in range(4)],
        note_turns=[failing, good_note(), failing, failing],
    )
    turns["notes"]["repeat_last"] = False
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    entries = compacts(manager.run_dir)
    assert [(e["ok"], e.get("failures")) for e in entries] == [
        (False, 1),
        (True, None),
        (False, 1),
    ]


@pytest.mark.parametrize(("limit", "expected"), [(2, [None, None]), (3, [None, PURPOSE, None])])
async def test_a6_turn_quota_must_fit_note_and_working_turn(
    workfolder: Path, limit: int, expected: list[str | None]
) -> None:
    outcomes = []

    async def topology(ctx: RunContext) -> None:
        for n in range(3):
            outcomes.append(await ctx.turn("solo", f"Turn {n}."))

    turns = script([say("x"), fill(900)], repeat_last=True, note_turns=[good_note()])
    manager = make_manager(
        workfolder,
        topology,
        {"solo": turns},
        agents=[agent("solo", compact=LOW)],
        run_extra={"limits": {"turns_per_agent": limit}},
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    purposes = [p for k, _, p in turn_log(manager.run_dir, "solo") if k == "turn"]
    assert purposes == expected
    assert manager.orchestrator.agents["solo"].turns_used == limit
    assert [o.status for o in outcomes][-1] is TurnStatus.LIMIT


# --------------------------------------------------------------------------------------------
# A10: stop, emergency stop, pause, budget during the note turn
# --------------------------------------------------------------------------------------------


def long_note_run(workfolder: Path, outcomes: list[Any], **kwargs: Any) -> Any:
    async def topology(ctx: RunContext) -> None:
        outcomes.append(await ctx.turn("solo", "eins"))
        outcomes.append(await ctx.turn("solo", "zwei"))

    note = kwargs.pop("note", [say(NOTE), wait(30), fill(650)])
    turns = script([say("x"), fill(900)], [say("y"), fill(10)], note_turns=[note])
    return make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)], **kwargs
    )


@pytest.mark.parametrize("how", ["stop", "not_aus"])
async def test_a10_stop_during_the_note_turn(workfolder: Path, how: str) -> None:
    outcomes: list[Any] = []
    manager = long_note_run(workfolder, outcomes)
    running = asyncio.create_task(manager.run())
    await eventually(lambda: manager.orchestrator is not None and note_active(manager.orchestrator))
    started = time.monotonic()
    if how == "stop":
        manager.stop()
    else:
        await manager.orchestrator.halt(StopReason.PROFILE_BLOCKED, profile="fake-auth")
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert time.monotonic() - started < 5, "the note turn was interrupted"
    assert [o.status for o in outcomes] == [TurnStatus.OK, TurnStatus.INTERRUPTED]
    assert ends(manager.run_dir, "solo")[-1]["status"] == "interrupted"
    [entry] = compacts(manager.run_dir)
    assert entry["ok"] is False  # interrupted: the session stays
    assert len(prompts(manager.run_dir, "solo")) == 2  # no working turn after it


async def test_a10_hanging_note_turn_is_cancelled_without_hanging_tasks(workfolder: Path) -> None:
    outcomes: list[Any] = []
    manager = long_note_run(
        workfolder, outcomes, note=[say(NOTE), {"do": "hang", "seconds": 30}], stop_grace_s=0.2
    )
    running = asyncio.create_task(manager.run())
    await eventually(lambda: manager.orchestrator is not None and note_active(manager.orchestrator))
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    log = events(manager.run_dir, "solo")
    assert log[-1]["kind"] == "turn_end" and log[-1]["data"]["status"] == "interrupted"
    backend = manager.backends["solo"]
    assert not backend._running and backend._closed
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == [], "no hanging tasks after the stop"


async def test_a10_pause_during_the_note_turn_holds_the_working_turn(workfolder: Path) -> None:
    outcomes: list[Any] = []
    manager = long_note_run(workfolder, outcomes, note=[say(NOTE), wait(0.3), fill(650)])
    running = asyncio.create_task(manager.run())
    await eventually(lambda: manager.orchestrator is not None and note_active(manager.orchestrator))
    manager.pause()
    await eventually(lambda: len(compacts(manager.run_dir)) == 1)
    await asyncio.sleep(0.3)
    assert len(prompts(manager.run_dir, "solo")) == 2, "the working turn waits for the resume"
    manager.resume()
    assert await asyncio.wait_for(running, 10) is RunStatus.DONE
    assert [o.status for o in outcomes] == [TurnStatus.OK, TurnStatus.OK]
    assert prompts(manager.run_dir, "solo")[2].startswith(PACKAGE_HEADING)


async def test_a10_budget_reached_by_the_note_turn(workfolder: Path) -> None:
    outcomes: list[Any] = []
    manager = long_note_run(
        workfolder,
        outcomes,
        note=[say(NOTE), fill(650, cost_usd=0.05)],
        run_extra={"limits": {"budget_usd": 0.04}},
    )
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    assert [o.status for o in outcomes] == [TurnStatus.OK, TurnStatus.LIMIT]
    assert log_entries(manager.run_dir, "limit")[0]["data"]["limit"] == "budget_usd"
    assert len(prompts(manager.run_dir, "solo")) == 2  # the working turn did not run

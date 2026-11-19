"""W4b-05b in a running run (FakeBackend, the real room): auto-compact and the threshold.

A3 (the decision in the run: share, tokens, exactly at the threshold, only between two turns,
at most one compaction per working turn, the threshold from every layer, `at: 100%`), A4 (the
flow: note turn, new session, package in front of the next prompt).
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from compact_kit import (
    LAYERS,
    LOW,
    NOTE,
    compacts,
    ends,
    fill,
    good_note,
    is_note_prompt,
    layered_manager,
    prompts,
    say,
    script,
    starts,
    submit,
    turn_log,
)
from run_kit import agent, channel, log_entries, make_manager, make_workfolder, post

from arr.models import RunStatus
from arr.runs.compact import PACKAGE_HEADING, PURPOSE
from arr.runs.context import RunContext

SUB = {"submission": {"format": "text"}}
"""Context of an assignment that expects a submission (W4b-01)."""


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def solo(**fields: Any) -> list[dict[str, Any]]:
    return [agent("solo", **fields)]


# --------------------------------------------------------------------------------------------
# A4: the flow
# --------------------------------------------------------------------------------------------


async def test_a4_note_turn_new_session_package_in_this_order(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "Auftrag A: beweise Lemma 1.", stage="s", context=SUB)
        await ctx.turn("solo", "Weiter 1.", stage="s", context=SUB)
        await ctx.turn("solo", "Weiter 2.", stage="s", context=SUB)

    turns = script(
        [submit(status="done", summary="Erste Fassung.", body="Beweis."), say("eins"), fill(100)],
        [
            post("main", "Gilt n ≡ 1 mod 4?", kind="question", to=["user"]),
            say("zwei"),
            fill(600),  # over the threshold of 500: compact before the next turn
        ],
        [say("drei"), fill(40)],
        note_turns=[good_note()],
    )
    manager = make_manager(
        workfolder,
        topology,
        {"solo": turns},
        agents=solo(compact=LOW),
        groupchats=[channel("main", ["solo"])],
        run_extra={"topology": {"kind": "phases"}},
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir

    # order: work, work, note turn, compact, work in a new session
    assert turn_log(run_dir, "solo") == [
        ("turn", 1, None),
        ("turn_end", 1, None),
        ("turn", 2, None),
        ("turn_end", 2, None),
        ("turn", 3, PURPOSE),
        ("turn_end", 3, PURPOSE),
        ("compact", 0, "auto"),
        ("turn", 4, None),
        ("turn_end", 4, None),
    ]
    [entry] = compacts(run_dir)
    assert entry == {
        "agent": "solo",
        "reason": "auto",
        "stage": "s",
        "tokens": 600,
        "window": 1_000_000,
        "threshold": 500,
        "note_chars": len(NOTE),
        "ok": True,
    }
    sent = prompts(run_dir, "solo")
    assert is_note_prompt(sent[2]) and starts(run_dir, "solo")[2]["data"]["purpose"] == PURPOSE
    sessions = [e["session"] for e in ends(run_dir, "solo")]
    assert sessions == ["fake-solo", "fake-solo", "fake-solo", "fake-solo-2"]

    package = sent[3]
    assert package.startswith(PACKAGE_HEADING)
    order = [
        "neu begonnen",
        "Auftrag A: beweise Lemma 1.",  # the action: first prompt of stage s
        "#1 · Format text · Status done · Phase s",
        "Kurzfassung: Erste Fassung.",
        "Gilt n ≡ 1 mod 4?",  # its open question to the user
        NOTE.splitlines()[1],  # its note
        "Weiter 2.",  # then this turn's own prompt
    ]
    positions = [package.index(part) for part in order]
    assert positions == sorted(positions), package
    assert f"Pfad: {run_dir / 'output'}" in package
    # the new session knows nothing of the old one, only what arr put in again
    backend = manager.backends["solo"]
    assert backend.history == [package]
    assert "Weiter 1." not in package and "eins" not in package
    # fill level: the note turn measured 650, the new session 40
    assert [e["context_tokens"] for e in ends(run_dir, "solo")] == [100, 600, 650, 40]


async def test_a4_the_package_goes_out_once(workfolder: Path) -> None:
    """After the package went out once, the next turn of the new session has none."""

    async def topology(ctx: RunContext) -> None:
        for n in range(4):
            await ctx.turn("solo", f"Turn {n}.")

    turns = script([fill(600)], [fill(100)], [fill(100)], note_turns=[good_note()])
    manager = make_manager(workfolder, topology, {"solo": turns}, agents=solo(compact=LOW))
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    sent = prompts(manager.run_dir, "solo")
    assert [p.startswith(PACKAGE_HEADING) for p in sent] == [False, False, True, False, False]
    assert sent[4] == "Turn 3."


# --------------------------------------------------------------------------------------------
# A3: the decision in the run
# --------------------------------------------------------------------------------------------


async def run_with(
    workfolder: Path, compact: dict[str, Any] | None, *fills: int, **script_extra: Any
) -> Any:
    async def topology(ctx: RunContext) -> None:
        for n in range(len(fills)):
            await ctx.turn("solo", f"Turn {n + 1}.")

    turns = script(*[[say("x"), fill(n)] for n in fills], note_turns=[good_note()], **script_extra)
    fields = {"compact": compact} if compact is not None else {}
    manager = make_manager(workfolder, topology, {"solo": turns}, agents=solo(**fields))
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager


def note_turns(manager: Any) -> list[int]:
    """Numbers of the agent's note turns."""
    return [
        e["turn"]
        for e in (x["data"] for x in log_entries(manager.run_dir, "turn"))
        if e.get("purpose") == PURPOSE
    ]


@pytest.mark.parametrize(
    ("compact", "fills", "expected"),
    [
        ({"at": 500}, (499, 499), []),  # below
        ({"at": 500}, (500, 10), [2]),  # exactly at the threshold
        ({"at": "60%"}, (599_999, 10), []),  # share of the FakeBackend's 1 000 000
        ({"at": "60%"}, (600_000, 10), [2]),
        (None, (600_000, 10), [2]),  # the built-in default is 60 %
        ({"at": "100%"}, (1_000_000, 10), []),  # off, also at a full window
        ({"at": "99%"}, (1_000_000, 10), [2]),  # counter-check
    ],
)
async def test_a3_threshold_in_the_run(
    workfolder: Path, compact: dict[str, Any] | None, fills: tuple[int, ...], expected: list[int]
) -> None:
    manager = await run_with(workfolder, compact, *fills)
    assert note_turns(manager) == expected
    assert len(compacts(manager.run_dir)) == len(expected)


async def test_a3_no_turn_no_compaction_and_one_per_working_turn(workfolder: Path) -> None:
    """Only right before a next turn: the last turn over the threshold compacts nothing. The
    note turn itself is over the threshold, but right after it there is no second one."""
    manager = await run_with(workfolder, LOW, 900, 900, 900)
    assert note_turns(manager) == [2, 4]  # before turn 2 and before turn 3, never after the last
    log = turn_log(manager.run_dir, "solo")
    assert [k for k, _, _ in log].count("compact") == 2
    assert log[-1] == ("turn_end", 5, None)


async def test_a3_window_unknown_share_does_not_apply_tokens_do(workfolder: Path) -> None:
    manager = await run_with(workfolder, None, 900_000, 900_000, 10, context_window=None)
    assert note_turns(manager) == []
    [noted] = log_entries(manager.run_dir, "compact_no_window")  # once per agent
    assert noted["data"] == {"agent": "solo", "at": "60%", "tokens": 900_000}
    tokens = await run_with(workfolder, {"at": 500}, 900, 10, context_window=None)
    assert note_turns(tokens) == [2]
    assert not log_entries(tokens.run_dir, "compact_no_window")


async def test_a3_window_from_a_request_like_codex(workfolder: Path) -> None:
    """The window reported with a request (Codex) counts as well as the one of the turn end."""
    manager = await run_with(
        workfolder, {"at": "50%"}, 600, 10, context_window=None, repeat_last=False
    )
    assert note_turns(manager) == []  # unknown window
    turns = script([fill(600, context_window=1_000)], [fill(10)], note_turns=[good_note()])

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        await ctx.turn("solo", "zwei")

    turns["context_window"] = None
    again = make_manager(workfolder, topology, {"solo": turns}, agents=solo(compact={"at": "50%"}))
    assert await asyncio.wait_for(again.run(), 30) is RunStatus.DONE
    assert note_turns(again) == [2]
    [entry] = compacts(again.run_dir)
    assert (entry["window"], entry["threshold"]) == (1_000, 500)


@pytest.mark.parametrize("layer", LAYERS)
async def test_a3_threshold_from_every_layer_reaches_the_run(workfolder: Path, layer: str) -> None:
    async def topology(ctx: RunContext) -> None:
        agent_id = next(iter(ctx.agents))
        await ctx.turn(agent_id, "eins")
        await ctx.turn(agent_id, "zwei")

    agent_id = "solo-1" if layer == "group mix" else "solo"
    turns = script([fill(600)], [fill(10)], note_turns=[good_note()])
    manager = layered_manager(workfolder, layer, topology, {agent_id: turns})
    assert manager.resolved.agents[0].compact.at == 500
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    [entry] = compacts(manager.run_dir)
    assert (entry["agent"], entry["threshold"], entry["ok"]) == (agent_id, 500, True)
    # counter-check without the layer: the default 60 % of 1 000 000 is far away
    other = layered_manager(workfolder, layer, topology, {agent_id: turns}, compact={"at": "60%"})
    assert await asyncio.wait_for(other.run(), 30) is RunStatus.DONE
    assert compacts(other.run_dir) == []

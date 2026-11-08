"""Hang scenarios of the W3-07 review (H1–H18) as fixed tests, by the rules of W3-07.1.

Every scenario runs a real RunManager with FakeBackend agents and the real room under a short
`wall_time`. The yardstick (docs/09 §3, principle 1): no state ends only by `wall_time`. Each
new guard has a counter-check (`naive`) that switches it off and shows the hang it prevents:

- L7: `wait_for_messages` waits at most `STREAK_MAX_S` per turn without a new message (H1).
- L10: `wait_until` wake-ups count against `max_wakeups_per_agent`; after three deadlines in
  a row the agent is idle (H2b).
- L11: rounds go on for the others while one waits long (H2, H18).
- L12: `wait_for_messages` returns at once while another agent waits for the place (H10).
- B3: a quota used up exactly ends with `all_done`, not `limit` (H14, H14b).
- B5: `wait_for_messages` after `wait_until` in the same turn keeps the `wait_until` (H15).

W3-09 (docs/09 L13–L16) adds H17 anew and H19–H24, each ending within its own deadline:

- L13: every direct message wakes a finished agent; ping-pong guard and wake-up limit bound it
  (H17, H19).
- L14: `until_result_from` for a result that never comes, and two agents waiting for each
  other, end by their deadlines and L10 (H20, H21).
- L15: an unread direct message to an agent that never reads wakes it once (H22).
- L16: a question to the user nobody answers ends by L7 and with the run; a late answer wakes
  the asker (H23, H24).
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    channel,
    done,
    events,
    eventually,
    log_entries,
    make_manager,
    make_settings,
    make_workfolder,
    post,
    room,
    say,
    turns,
    wait,
)

from arr.backends.fake import FakeBackend
from arr.models import AgentStatus, RunStatus
from arr.permissions.types import PermissionRequest
from arr.room import store as room_store
from arr.room import waiting
from arr.room.store import RoomStore
from arr.room.types import MessageVia
from arr.runs import orchestrator, pacing
from arr.runs.context import DiscussionEnd, DiscussionOutcome, RunContext
from arr.runs.orchestrator import Orchestrator, ProfileGate

D1, D2, D3 = "diskutant-1", "diskutant-2", "diskutant-3"
IDLE = {"idle_after_s": 0.2}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def summary(manager: Any, outcome: DiscussionOutcome | None, took: float) -> dict[str, Any]:
    """How a scenario ended; `hang` is true when only `wall_time` ended it."""
    limits = [e["data"].get("limit") for e in log_entries(manager.run_dir, "limit")]
    return {
        "ended_by": outcome.ended_by if outcome else None,
        "took_s": took,
        "limit": limits,
        "hang": "wall_time" in limits,
        "wakes": len(log_entries(manager.run_dir, "wake")),
    }


async def run_case(
    workfolder: Path,
    scripts: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    during: Callable[[Any], Any] | None = None,
    wall: str = "8s",
    limits: dict[str, Any] | None = None,
    within: float = 40,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    results: list[DiscussionOutcome] = []
    holder: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        helper = asyncio.create_task(during(holder["m"])) if during is not None else None
        results.append(await ctx.discuss("main", "Löst die Aufgabe."))
        if helper is not None:
            helper.cancel()

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", list(scripts), mode="event", **(config or {}))],
        run_extra={"limits": {"wall_time": wall, **(limits or {})}},
        **kwargs,
    )
    holder["m"] = manager
    started = time.monotonic()
    await asyncio.wait_for(manager.run(), within)
    took = time.monotonic() - started
    return manager, summary(manager, results[0] if results else None, took)


def tool_results(manager: Any, agent_id: str) -> list[dict[str, Any]]:
    """The JSON results of the agent's `wait_for_messages` calls."""
    return [
        json.loads(e["data"]["content"])
        for e in events(manager.run_dir, agent_id)
        if e["kind"] == "tool_result" and e["data"]["content"].startswith('{"ended_by"')
    ]


# --- H1: wait_for_messages again and again (L7) ----------------------------------------------


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_h1_wait_for_messages_in_a_loop_ends_the_turn(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """An agent waits for an answer that never comes, call after call. After the limit the
    call says „nobody answers“, the next one ends the turn; the agent is idle, the rounds
    take over. Counter-check: without the limit only `wall_time` ends it."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1e9 if naive else 1.5)
    loop = [room("wait_for_messages", seconds=0.5) for _ in range(60)]  # 30 s of waiting
    scripts = {D1: turns(loop + [done()], [done()]), D2: turns([done()])}
    manager, line = await run_case(workfolder, scripts, IDLE, wall="4s" if naive else "8s")
    if naive:
        assert line["hang"]
        return
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    ends = [r["ended_by"] for r in tool_results(manager, D1)]
    assert "nobody" in ends and ends[-1] == "turn_end"
    assert ends.index("nobody") == len(ends) - 2, "the call after „nobody“ ends the turn"
    [end] = log_entries(manager.run_dir, "end_turn")
    assert end["data"] == {"agent": D1, "reason": "nobody"}
    assert manager.orchestrator.agents[D1].turns_used == 2, "woken by the round, then done"


async def test_h1_a_new_message_restarts_the_limit(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message to the agent sets the time waited back to zero (L7)."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.2)
    loop = [room("wait_for_messages", seconds=0.5) for _ in range(5)]
    scripts = {
        D1: turns(loop + [done()]),
        D2: turns([wait(0.9), post("main", "@diskutant-1 noch da?"), done()]),
    }
    manager, line = await run_case(workfolder, scripts, IDLE)
    assert not line["hang"]
    ends = [r["ended_by"] for r in tool_results(manager, D1)]
    assert "message" in ends and "turn_end" not in ends, ends
    assert log_entries(manager.run_dir, "end_turn") == []


# --- H2, H2b, H2c: wait_until again and again (L10, L11) -------------------------------------


async def test_h2_wait_until_loop_does_not_starve_the_other(workfolder: Path) -> None:
    """L11: the idle agent gets its rounds while the other waits long; the rounds end it."""
    scripts = {
        D1: turns([room("wait_until", minutes=0.005)], repeat_last=True),  # 0.3 s each
        D2: turns([say("nichts")], repeat_last=True),  # idle, never done
    }
    manager, line = await run_case(workfolder, scripts, IDLE)
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ROUNDS
    rounds = log_entries(manager.run_dir, "round")
    assert [r["data"]["number"] for r in rounds] == [1, 2, 3]
    assert all(D2 in r["data"]["agents"] for r in rounds)
    ended = [e["data"] for e in log_entries(manager.run_dir, "wait_end")]
    assert all(e["reason"] == "discussion_end" for e in ended)


@pytest.mark.parametrize("naive", ["guarded", "wake_count", "neither"])
async def test_h2b_wait_until_loop_other_done(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: str
) -> None:
    """L10: after three deadlines in a row the agent is idle, another `wait_until` is
    refused, the rounds end it. Counter-checks: without the three, the wake-up limit ends it
    (`wake_count`); without both only `wall_time` does (`neither`)."""
    config = dict(IDLE)
    if naive != "guarded":
        monkeypatch.setattr(pacing, "WAIT_UNTIL_REPEAT", 10**6)
        monkeypatch.setattr(room_store, "WAIT_UNTIL_REPEAT", 10**6)
    if naive == "neither":
        config["max_wakeups_per_agent"] = 10**6
    scripts = {
        D1: turns([room("wait_until", minutes=0.005)], repeat_last=True),
        D2: turns([post("main", "Ergebnis", kind="result")]),
    }
    manager, line = await run_case(
        workfolder, scripts, config, wall="4s" if naive == "neither" else "8s"
    )
    skipped = [e["data"]["reason"] for e in log_entries(manager.run_dir, "wake_skipped")]
    if naive == "neither":
        assert line["hang"]
        return
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ROUNDS
    if naive == "wake_count":
        assert skipped == ["max_wakeups_per_agent"], "the wake-ups of wait_until count"
        return
    assert skipped == ["wait_until_repeat"]
    assert line["wakes"] == 2, "two wake-ups, the third deadline makes it idle"
    refused = [
        e
        for e in events(manager.run_dir, D1)
        if e["kind"] == "tool_result" and "hintereinander mit `wait_until`" in e["data"]["content"]
    ]
    assert refused, "the round turns get the refusal"


async def test_h2c_wait_until_loop_with_turn_limit(workfolder: Path) -> None:
    scripts = {
        D1: turns([room("wait_until", minutes=0.005)], repeat_last=True),
        D2: turns([post("main", "Ergebnis", kind="result")]),
    }
    _, line = await run_case(workfolder, scripts, IDLE, limits={"turns_per_agent": 5}, wall="30s")
    assert not line["hang"]


async def test_h2_a_message_allows_wait_until_again(workfolder: Path) -> None:
    """L10: a message to the agent resets the count of deadlines in a row."""
    scripts = {
        D1: turns(
            [room("wait_until", minutes=0.005)],
            [room("wait_until", minutes=0.005)],
            [room("wait_until", minutes=0.005)],
            [room("wait_until", minutes=0.005)],
            [done()],
        ),
        D2: turns([wait(0.45), post("main", "@diskutant-1 schau mal"), done()]),
    }
    manager, line = await run_case(workfolder, scripts, IDLE)
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    assert log_entries(manager.run_dir, "wake_skipped") == []


# --- H3: @alle / self / ping-pong ------------------------------------------------------------


async def test_h3_at_all_questions_in_a_loop_three_agents(workfolder: Path) -> None:
    ask = post("main", "@alle was meint ihr?", kind="question")
    scripts = {D1: turns([ask], repeat_last=True), D2: turns([ask], repeat_last=True),
               D3: turns([ask], repeat_last=True)}  # fmt: skip
    _, line = await run_case(workfolder, scripts, IDLE, wall="60s", within=90)
    assert not line["hang"]


async def test_h3b_two_agents_ping_pong(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "@diskutant-2 und?", kind="question")], repeat_last=True),
        D2: turns([post("main", "@diskutant-1 und?", kind="question")], repeat_last=True),
    }
    manager, line = await run_case(workfolder, scripts, IDLE, wall="60s", within=90)
    assert not line["hang"] and log_entries(manager.run_dir, "ping_pong")


async def test_h3c_agent_asks_itself(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "@diskutant-1 frage ich mich selbst?", kind="question")],
                  repeat_last=True),
        D2: turns([say("nichts")], repeat_last=True),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"]
    woken = {e["data"]["agent"] for e in log_entries(manager.run_dir, "wake")}
    assert D1 not in woken, "its own message never wakes it"


# --- H4, H5: unanswered question, user notes during rounds -----------------------------------


async def test_h4_question_to_finished_agent_never_answered(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "Mein Ergebnis", kind="result")], [say("keine Lust")],
                  repeat_last=True),
        D2: turns([wait(0.3), post("main", "@diskutant-1 stimmt das?", kind="question"),
                   room("wait_for_messages", seconds=1)], [say("warte weiter")],
                  repeat_last=True),
    }  # fmt: skip
    _, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"]


async def test_h5_user_messages_during_idle_rounds(workfolder: Path) -> None:
    async def user(manager: Any) -> None:
        await eventually(lambda: bool(log_entries(manager.run_dir, "round")), within=10)
        manager.orchestrator.room.post("main", "Notiz ohne Empfänger", sender="user",
                                       via=MessageVia.GUI)  # fmt: skip
        await asyncio.sleep(0.5)
        manager.orchestrator.room.post("main", "@diskutant-2 und du?", sender="user",
                                       via=MessageVia.GUI)  # fmt: skip

    scripts = {D1: turns([say("nichts")], repeat_last=True),
               D2: turns([say("nichts")], repeat_last=True)}  # fmt: skip
    _, line = await run_case(workfolder, scripts, IDLE, during=user, wall="30s")
    assert not line["hang"]


# --- H6, H7, H8: stop and pause while waiting ------------------------------------------------


async def test_h6_stop_during_wait_for_messages(workfolder: Path) -> None:
    stopped_at: list[float] = []

    async def stop(manager: Any) -> None:
        await eventually(lambda: manager.orchestrator.in_turn_wait(D1) is not None, within=10)
        stopped_at.append(time.monotonic())
        manager.stop()

    scripts = {D1: turns([room("wait_for_messages", seconds=40), done()]), D2: turns([done()])}
    await run_case(workfolder, scripts, during=stop, wall="60s")
    assert time.monotonic() - stopped_at[0] < 5


async def test_h7_stop_during_wait_until(workfolder: Path) -> None:
    stopped_at: list[float] = []

    async def stop(manager: Any) -> None:
        await eventually(lambda: manager.orchestrator.long_wait(D1) is not None, within=10)
        stopped_at.append(time.monotonic())
        manager.stop()

    scripts = {D1: turns([room("wait_until", minutes=30)], [done()]), D2: turns([done()])}
    manager, _ = await run_case(workfolder, scripts, during=stop, wall="60s")
    assert time.monotonic() - stopped_at[0] < 5
    [ended] = log_entries(manager.run_dir, "wait_end")
    assert ended["data"]["agent"] == D1 and ended["data"]["ended_by"] == "stopped"


async def test_h8_pause_during_wait_until(workfolder: Path) -> None:
    seen: dict[str, Any] = {}

    async def pause(manager: Any) -> None:
        orch = manager.orchestrator
        await eventually(lambda: orch.long_wait(D1) is not None, within=10)
        manager.pause()
        turns_before = orch.agents[D1].turns_used
        await asyncio.sleep(1.5)  # the deadline (0.6 s) passes during the pause
        seen["turn_during_pause"] = orch.agents[D1].turns_used != turns_before
        manager.resume()

    scripts = {D1: turns([room("wait_until", minutes=0.01)], [done()]), D2: turns([done()])}
    _, line = await run_case(workfolder, scripts, during=pause, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    assert seen == {"turn_during_pause": False}


async def test_h8b_pause_during_wait_for_messages(workfolder: Path) -> None:
    async def pause(manager: Any) -> None:
        orch = manager.orchestrator
        await eventually(lambda: orch.in_turn_wait(D1) is not None, within=10)
        manager.pause()
        await asyncio.sleep(1.5)
        manager.resume()

    scripts = {D1: turns([room("wait_for_messages", seconds=1), done()]), D2: turns([done()])}
    _, line = await run_case(workfolder, scripts, during=pause, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE


# --- H9: approval open until its deadline while the other waits (and B9) ----------------------


class AskingFirstTurn(FakeBackend):
    holder: dict[str, Any] = {}

    async def run_turn(self, prompt: str, *, stage: str | None = None) -> Any:
        if self._turn == 0:
            request = PermissionRequest(run_id=self.run_id, agent_id=self.config.id,
                                        provider="claude", tool="SandboxNetworkAccess",
                                        hosts=["oeis.org"], reason="nachschlagen")  # fmt: skip
            await self.holder["manager"].inbox.handle(request)
        async for event in super().run_turn(prompt, stage=stage):
            yield event


async def test_h9_approval_open_until_timeout_other_waits(workfolder: Path) -> None:
    """Ends after the approval deadline; meanwhile the channel says what it waits for, both
    parts (B9: an approval and an agent waiting in its turn)."""
    scripts = {
        D1: turns([post("main", "Antwort an @diskutant-2"), done()]),
        D2: turns([post("main", "@diskutant-1 bitte antworte", kind="question"),
                   room("wait_for_messages", seconds=1), room("wait_for_messages", seconds=1),
                   room("wait_for_messages", seconds=1)], [done()]),
    }  # fmt: skip
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los."))

    manager = make_manager(workfolder, topology, scripts,
                           groupchats=[channel("main", [D1, D2], mode="event", **IDLE)],
                           run_extra={"approvals": {"timeout": "2s"},
                                      "limits": {"wall_time": "30s"}})  # fmt: skip
    AskingFirstTurn.holder = {"manager": manager}

    def factory(prepared: Any) -> FakeBackend:
        view = RoomStore(prepared.run_dir).for_agent(prepared.id, prepared.work_dir)
        cls = AskingFirstTurn if prepared.id == D1 else FakeBackend
        return cls(prepared.file.config, scripts[prepared.id], run_id="r", room=view)

    manager.backend_factory = factory
    await asyncio.wait_for(manager.run(), 40)
    assert results[0].ended_by is DiscussionEnd.ALL_DONE
    assert [e for e in log_entries(manager.run_dir, "limit")] == []
    channel_waits = [
        e["data"] for e in log_entries(manager.run_dir, "waiting") if e["data"].get("channel")
    ]
    assert any(w.get("parts") == ["agents", "approval"] for w in channel_waits), channel_waits


# --- H10: capacity 1, three agents, one waits for the answer of a queued one (L12) ----------


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_h10_capacity_one_wait_gives_the_place_free(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """D1 asks D2 and D3 and waits in its turn, call after call, like a model in a loop;
    D2 and D3 wait for D1's place. `wait_for_messages` returns at once, the next call ends the
    turn, D2 and D3 answer. Counter-check: without L12 only `wall_time` ends it."""
    if naive:
        monkeypatch.setattr(ProfileGate, "contended", lambda self: False)
    ask = post("main", "@diskutant-2 @diskutant-3 eure Meinung?", kind="question")
    scripts = {
        D1: turns([ask] + [room("wait_for_messages", seconds=1) for _ in range(60)], [done()]),
        D2: turns([post("main", "Meinung 2"), done()]),
        D3: turns([post("main", "Meinung 3"), done()]),
    }
    manager, line = await run_case(
        workfolder,
        scripts,
        IDLE,
        settings=make_settings(**{"fake-auth": 1}),
        wall="4s" if naive else "30s",
    )
    if naive:
        assert line["hang"]
        return
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    assert line["took_s"] < 10
    ends = [r["ended_by"] for r in tool_results(manager, D1)]
    assert ends == ["capacity", "turn_end"], ends
    [end] = log_entries(manager.run_dir, "end_turn")
    assert end["data"] == {"agent": D1, "reason": "capacity"}


# --- H11, H12, H13 ---------------------------------------------------------------------------


async def test_h11_turn_limit_while_other_waits_long(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "Gedanke")], repeat_last=True),
        D2: turns([room("wait_until", minutes=0.02)], [post("main", "Ergebnis", kind="result")]),
    }
    manager, line = await run_case(
        workfolder, scripts, IDLE, limits={"turns_per_agent": 2}, wall="30s"
    )
    assert not line["hang"]
    done_reasons = {e["data"]["agent"]: e["data"]["reason"]
                    for e in log_entries(manager.run_dir, "agent_done")}  # fmt: skip
    assert done_reasons[D1] == "limit"


async def test_h12_turns_without_result_or_wait(workfolder: Path) -> None:
    scripts = {D1: turns([say("hm")], repeat_last=True), D2: turns([say("hm")], repeat_last=True)}
    _, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ROUNDS


async def test_h13_codex_wait_is_capped_at_25_s(workfolder: Path) -> None:
    scripts = {D1: turns([room("wait_for_messages", seconds=600), done()])}
    agents = [{"id": D1, "provider": "codex", "model": "fake-model", "auth": "fake-auth"}]
    manager, _ = await run_case(workfolder, scripts, agents=agents, wall="60s", within=60)
    [result] = tool_results(manager, D1)
    assert result["ended_by"] == "timeout" and 24.5 <= result["waited_s"] <= 26.5


# --- H14: turns_per_agent used up exactly (B3) -----------------------------------------------


async def test_h14_turn_limit_exactly_used_up_by_a_normal_discussion(workfolder: Path) -> None:
    scripts = {D1: turns([post("main", "Ergebnis 1", kind="result")]),
               D2: turns([post("main", "Ergebnis 2", kind="result")])}  # fmt: skip
    manager, line = await run_case(
        workfolder, scripts, IDLE, limits={"turns_per_agent": 1}, wall="30s"
    )
    assert line["ended_by"] is DiscussionEnd.ALL_DONE and line["limit"] == []


async def test_h14b_solo_turn_limit_one(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn(D1, "Mach es.")

    manager = make_manager(workfolder, topology, {D1: turns([say("fertig")])},
                           run_extra={"limits": {"turns_per_agent": 1}})  # fmt: skip
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.DONE
    assert log_entries(manager.run_dir, "limit") == []


# --- H15: wait_until, then wait_for_messages in the same turn (B5) ---------------------------


async def test_h15_wait_until_then_wait_for_messages_same_turn(workfolder: Path) -> None:
    scripts = {
        D1: turns([room("wait_until", minutes=0.05), room("wait_for_messages", seconds=0.2)],
                  [done()]),
        D2: turns([say("nichts")], [done()]),
    }  # fmt: skip
    seen: dict[str, Any] = {}

    async def look(manager: Any) -> None:
        orch = manager.orchestrator
        await eventually(lambda: orch.agents[D1].turns_used >= 1 and not orch.is_busy(D1),
                         within=10)  # fmt: skip
        seen["status"] = orch.agent_status(D1)
        seen["long_wait"] = orch.long_wait(D1) is not None

    manager, line = await run_case(workfolder, scripts, IDLE, during=look, wall="30s")
    assert not line["hang"]
    assert seen == {"status": AgentStatus.WAITING, "long_wait": True}
    wakes = [e["data"]["reasons"] for e in log_entries(manager.run_dir, "wake")]
    assert ["timeout"] in wakes, "the promised wake-up comes"


# --- H16, H17, H18 ---------------------------------------------------------------------------


async def test_h16_until_file_becomes_a_link(workfolder: Path, tmp_path: Path) -> None:
    outside = tmp_path / "draussen.flag"
    outside.write_text("x")

    async def swap(manager: Any) -> None:
        orch = manager.orchestrator
        await eventually(lambda: orch.long_wait(D1) is not None, within=10)
        work = orch.prepared.agents[D1].work_dir
        (work / "out").mkdir(exist_ok=True)
        os.symlink(outside, work / "out" / "fertig.flag")

    scripts = {D1: turns([room("wait_until", minutes=0.03, until_file="out/fertig.flag")],
                         [done()]), D2: turns([done()])}  # fmt: skip
    manager, line = await run_case(workfolder, scripts, during=swap, wall="30s")
    wakes = [e["data"]["reasons"] for e in log_entries(manager.run_dir, "wake")]
    assert wakes == [["timeout"]], "the link does not count as the file"
    assert line["took_s"] >= 1.5


async def test_h17_done_agent_woken_by_mentions_and_done_again(workfolder: Path) -> None:
    """L13: notes with `@name` wake the finished agent (until W3-09 they did not); after each
    turn it is done again, and the discussion ends with `all_done`."""
    scripts = {
        D1: turns([post("main", "Ergebnis", kind="result")], [say("?")], repeat_last=True),
        D2: turns([wait(0.3)] + [post("main", f"@diskutant-1 Notiz {i}") for i in range(5)]
                  + [done()]),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    assert 2 <= manager.orchestrator.agents[D1].turns_used <= 6, "woken, but bounded"
    assert manager.orchestrator.agent_status(D1) is AgentStatus.DONE


async def test_h18_rounds_of_the_other_go_on_during_a_long_wait(workfolder: Path) -> None:
    """L11: the first round for D2 comes after `idle_after_s`, not after D1's long wait."""
    scripts = {
        D1: turns([room("wait_until", minutes=0.1)], [post("main", "Ergebnis", kind="result")]),
        D2: turns([say("nichts")], [post("main", "Ergebnis", kind="result")]),
    }
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    rounds = log_entries(manager.run_dir, "round")
    waits = [e for e in log_entries(manager.run_dir, "waiting") if e["data"].get("agent") == D1]
    assert rounds and rounds[0]["data"]["agents"] == [D2]
    assert rounds[0]["ts"] - waits[0]["ts"] < 4.5, "not held up by D1's 6 s wait"


# --- W3-07.2 NB2: a backend that ignores interrupt() --------------------------------------------


class IgnoresInterrupt(FakeBackend):
    """Like a CLI that does not react to `interrupt()` (review W3-07.1, R2)."""

    async def interrupt(self) -> None:
        return None


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_nb2_a_turn_that_ignores_the_interrupt_ends_hard(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """arr ends the turn (L7), the backend ignores `interrupt()`: a second try, then a hard
    stop, each in the log; the round turn after it runs. Counter-check: with one `interrupt()`
    only, as on `dcb40b5`, only `wall_time` ends it."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.0)
    monkeypatch.setattr(orchestrator, "END_TURN_RETRY_S", 1e9 if naive else 0.5)
    monkeypatch.setattr(orchestrator, "END_TURN_HARD_S", 0.5)
    loop = [room("wait_for_messages", seconds=0.5) for _ in range(200)]
    scripts = {D1: turns(loop + [done()], [done()]), D2: turns([done()])}
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los."))

    manager = make_manager(
        workfolder, topology, scripts,
        groupchats=[channel("main", [D1, D2], mode="event", **IDLE)],
        run_extra={"limits": {"wall_time": "5s" if naive else "30s"}},
    )  # fmt: skip

    def factory(prepared: Any) -> FakeBackend:
        view = RoomStore(prepared.run_dir).for_agent(prepared.id, prepared.work_dir)
        return IgnoresInterrupt(prepared.file.config, scripts[prepared.id], run_id="r", room=view)

    manager.backend_factory = factory
    await asyncio.wait_for(manager.run(), 40)
    line = summary(manager, results[0] if results else None, 0.0)
    ends = [e["data"] for e in log_entries(manager.run_dir, "end_turn")]
    if naive:
        assert line["hang"] and ends == [{"agent": D1, "reason": "nobody"}]
        return
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    assert ends == [
        {"agent": D1, "reason": "nobody"},
        {"agent": D1, "reason": "nobody", "attempt": 2},
        {"agent": D1, "reason": "nobody", "hard": True},
    ]
    statuses = [e["data"]["status"] for e in log_entries(manager.run_dir, "turn_end")
                if e["data"]["agent"] == D1]  # fmt: skip
    assert statuses == ["interrupted", "ok"], "the next turn runs normally"


# --- W3-09: H19–H24 for L13–L16 ------------------------------------------------------------


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_h19_two_finished_agents_reply_to_each_other_forever(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """L13 lets finished agents wake each other with every reply; the ping-pong guard and the
    wake-up limit stay and end it. Counter-check without both: only `wall_time` ends it."""
    config = dict(IDLE)
    if naive:
        monkeypatch.setattr(pacing.PingPongTracker, "observe", lambda self, msg: None)
        config["max_wakeups_per_agent"] = 10**6
    scripts = {  # each answers after the other's turn is over, so every reply wakes
        D1: turns([post("main", "Ergebnis 1", kind="result")],
                  [wait(0.1), post("main", "@diskutant-2 und du?")], repeat_last=True),
        D2: turns([wait(0.2), post("main", "@diskutant-1 noch etwas?"), done()],
                  [wait(0.1), post("main", "@diskutant-1 und du?")], repeat_last=True),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, config, wall="4s" if naive else "30s")
    if naive:
        assert line["hang"]
        return
    assert not line["hang"]
    stopped = log_entries(manager.run_dir, "ping_pong") or log_entries(
        manager.run_dir, "wake_skipped"
    )
    assert stopped, "a guard ended it"


async def test_h20_waiting_for_a_result_that_never_comes(workfolder: Path) -> None:
    """L14 with L10: the awaited agent never delivers and is never done; three deadlines in a
    row make the waiting agent idle, another `wait_until` is refused, the rounds end it."""
    scripts = {
        D1: turns([room("wait_until", minutes=0.005, until_result_from=D2)], repeat_last=True),
        D2: turns([say("nichts")], repeat_last=True),
    }
    manager, line = await run_case(workfolder, scripts, IDLE)
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ROUNDS
    skipped = [e["data"]["reason"] for e in log_entries(manager.run_dir, "wake_skipped")]
    assert skipped == ["wait_until_repeat"]
    reasons = {e["data"]["reason"] for e in log_entries(manager.run_dir, "waiting")
               if e["data"].get("agent") == D1}  # fmt: skip
    assert "result_from" in reasons


async def test_h21_two_agents_wait_for_each_others_result(workfolder: Path) -> None:
    """L14: a wait for each other ends by the deadlines; neither waits past it, no hang."""
    scripts = {
        D1: turns([room("wait_until", minutes=0.01, until_result_from=D2)], repeat_last=True),
        D2: turns([room("wait_until", minutes=0.01, until_result_from=D1)], repeat_last=True),
    }
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"] and line["took_s"] < 25
    reasons = {r for e in log_entries(manager.run_dir, "wake") for r in e["data"]["reasons"]}
    assert reasons == {"timeout"}


async def test_h22_an_agent_that_never_reads_its_message(workfolder: Path) -> None:
    """L15: the unread request wakes its finished addressee once, then the discussion ends,
    although it never reads it."""
    scripts = {
        D1: turns([wait(0.2), post("main", "@diskutant-2 bitte lies das"), done()]),
        D2: turns([wait(0.8), post("main", "Ergebnis", kind="result")], [say("hm")],
                  repeat_last=True),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    assert manager.orchestrator.agents[D2].turns_used == 2


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_h23_question_to_the_user_nobody_answers(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """L16 with L7: the agent waits for the user call after call; after the limit its turn
    ends, the discussion goes on and ends, the question closes with the run. Counter-check
    without L7: only `wall_time` ends it."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1e9 if naive else 1.2)
    ask = post("main", "@user welches Band?", kind="question")
    loop = [room("wait_for_messages", seconds=0.5) for _ in range(60)]
    scripts = {D1: turns([ask, *loop], [done()]), D2: turns([done()])}
    manager, line = await run_case(workfolder, scripts, IDLE, wall="4s" if naive else "30s")
    if naive:
        assert line["hang"]
        return
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    [item] = manager.inbox.items()
    assert item.status.value == "obsolete"


async def test_h24_a_late_answer_of_the_user_wakes_the_asker(workfolder: Path) -> None:
    """L16 with L13: the asker has ended its turn (its open question keeps it from `done`);
    the user's answer in the channel goes to it and wakes it at once."""

    async def user(manager: Any) -> None:
        orch = manager.orchestrator
        await eventually(lambda: bool(log_entries(manager.run_dir, "round")), within=10)
        orch.room.post("main", "Band 10–35.", sender="user", via=MessageVia.GUI)

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {
        D1: turns([ask, post("main", "Vorläufig 10–35.", kind="result")], [say("warte")],
                  [post("main", "Danke, bleibt so.", kind="result")]),
        D2: turns([post("main", "Mein Teil.", kind="result")]),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, IDLE, during=user, wall="30s")
    assert not line["hang"] and line["ended_by"] is DiscussionEnd.ALL_DONE
    woken = [e["data"] for e in log_entries(manager.run_dir, "wake") if e["data"]["agent"] == D1]
    assert woken and "mention" in woken[-1]["reasons"]
    [item] = manager.inbox.items()
    assert item.status.value == "decided"

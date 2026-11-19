"""W4b-05b.2: the second rework after the follow-up review.

Point 1 (N1, N4, Entscheidung 59): a turn never begins while its profile is paused or the
throttle has no room for it, and the slot count is right in every exit (also a cancel at
each place a turn waits). Point 2 (G-N3): `wait_for_messages` refuses in the note turn on
every path. Point 3 (G-N4): the note-turn flag never stays behind silently, and in doubt the
room refuses. Point 4 (G-N2): the first fitting main request fixes the model. Point 5 (G-N5):
the surviving mutations M02, M04, M05, M11, M13.
"""

import asyncio
import contextlib
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from compact_kit import LOW, NOTE, compacts, fill, good_note, prompts, say, script
from run_kit import (
    agent,
    eventually,
    log_entries,
    make_manager,
    make_settings,
    make_workfolder,
    wait,
)

from arr.backends.base import CostKind, TurnStatus
from arr.backends.claude_events import ClaudeEvents
from arr.models import RunStatus
from arr.room.store import RoomStore
from arr.room.types import RoomError
from arr.room.waiting import wait_for_messages
from arr.runs.compact import PURPOSE
from arr.runs.context import RunContext, TurnJob
from arr.runs.orchestrator import Orchestrator, ProfileGate, StopReason

OFF = {"at": "100%"}
GATE = "fake-auth"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def gate_of(ctx: Orchestrator) -> ProfileGate:
    return ctx._gates[GATE]


def note_running(ctx: Orchestrator, agent_id: str = "solo") -> bool:
    return agent_id in ctx._active and ctx.room.in_note_turn(agent_id)


def starts_of(run_dir: Path, agent_id: str) -> list[float]:
    return [e["ts"] for e in log_entries(run_dir, "turn") if e["data"]["agent"] == agent_id]


# --------------------------------------------------------------------------------------------
# Point 1, N1: a cancel at every place a turn waits leaves the slot count at 0
# --------------------------------------------------------------------------------------------

WAIT_PLACES = ["slot", "runnable", "note_turn", "again_after_note", "again_after_pause"]


async def cancel_run(workfolder: Path, where: str, cancel: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    long_pause = 30.0
    other_task: list[asyncio.Task[Any]] = []

    async def slot_in_pause(ctx: Orchestrator) -> asyncio.Task[Any]:
        """solo waits for the only slot, the run pauses, `other` ends: solo holds the slot
        and waits for the resume (the way a turn holds a slot through a user pause)."""
        other_task.append(asyncio.create_task(ctx.turn("other", "lang")))
        await eventually(lambda: "other" in ctx._active)
        task = asyncio.create_task(ctx.turn("solo", "eins"))
        await eventually(lambda: ctx.waits_for_capacity("solo"))
        ctx.pause()
        await eventually(lambda: "other" not in ctx._active and gate_of(ctx).active == 1)
        assert not ctx.waits_for_capacity("solo")
        return task

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        gate = gate_of(ctx)
        if where == "slot":  # `other` holds the only slot, solo waits for it
            other_task.append(asyncio.create_task(ctx.turn("other", "lang")))
            await eventually(lambda: "other" in ctx._active)
            task = asyncio.create_task(ctx.turn("solo", "eins"))
            await eventually(lambda: ctx.waits_for_capacity("solo"))
        elif where == "runnable":  # solo gets its slot in a pause and waits for the resume
            task = await slot_in_pause(ctx)
        elif where == "note_turn":
            await ctx.turn("solo", "eins")
            task = asyncio.create_task(ctx.turn("solo", "zwei"))
            await eventually(lambda: note_running(ctx))
        elif where == "again_after_note":  # rate limit in the note turn, waits again
            await ctx.turn("solo", "eins")
            task = asyncio.create_task(ctx.turn("solo", "zwei"))
            await eventually(lambda: ctx.waits_for_capacity("solo"))
        else:  # again_after_pause: holds the slot in a pause, the profile pauses meanwhile
            task = await slot_in_pause(ctx)
            gate.report({"status": "rejected", "resets_at": time.time() + long_pause})
            ctx.resume()
            await eventually(lambda: ctx.waits_for_capacity("solo"))
        out["waiting"] = gate.active
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if where in ("runnable",):
                ctx.resume()
        else:
            if where in ("runnable",):
                ctx.resume()
            if where in ("again_after_note", "again_after_pause"):
                gate.paused_until = time.time()  # end the pause early: the turn goes on
                gate.kick()
            out["outcome"] = await task
        if where in ("slot", "runnable", "again_after_pause"):
            await other_task.pop()
        out["after"] = gate.active
        out["flag"] = ctx.room.in_note_turn("solo")

    limit = {"do": "rate_limit", "status": "rejected", "resets_at": time.time() + long_pause}
    notes = {
        "note_turn": [say(NOTE), wait(30 if cancel else 0.3), fill(650)],
        "again_after_note": [limit, *good_note()],
    }.get(where, good_note())
    solo = script([say("x"), fill(600)], [say("y"), fill(10)], note_turns=[notes])
    scripts: dict[str, Any] = {"solo": solo}
    agents = [agent("solo", compact=LOW if where.startswith(("note", "again_after_n")) else OFF)]
    if where in ("slot", "runnable", "again_after_pause"):
        scripts["other"] = script([wait(0.3), fill(10)])
        agents.append(agent("other", compact=OFF))
    manager = make_manager(
        workfolder, topology, scripts, agents=agents, settings=make_settings(**{GATE: 1})
    )
    out["status"] = await asyncio.wait_for(manager.run(), 30)
    out["manager"] = manager
    return out


@pytest.mark.parametrize("where", WAIT_PLACES)
async def test_n1_cancel_at_every_wait_leaves_the_count_right(workfolder: Path, where: str) -> None:
    out = await cancel_run(workfolder, where, cancel=True)
    holds = {"slot": 1, "runnable": 1, "note_turn": 1}.get(where, 0)  # `other`, or solo itself
    assert out["waiting"] == holds  # positive control: where it waits, it holds this
    assert out["after"] == 0, f"slot count after the cancel: {out['after']}"
    assert out["flag"] is False  # M04: a note turn cancelled from outside leaves no flag
    assert out["status"] is RunStatus.DONE


@pytest.mark.parametrize("where", WAIT_PLACES)
async def test_n1_counter_check_without_cancel(tmp_path: Path, where: str) -> None:
    out = await cancel_run(make_workfolder(tmp_path), where, cancel=False)
    assert out["outcome"].status is TurnStatus.OK
    assert out["after"] == 0 and out["flag"] is False


# --------------------------------------------------------------------------------------------
# Point 1, N4: no start into a pause or a full throttle of the profile after a user pause
# --------------------------------------------------------------------------------------------


async def overlap_run(workfolder: Path, status: str | None) -> dict[str, Any]:
    """After the reviewer's `np_pause_overlap.py`: capacity 2, a and c run, b waits for a slot;
    the run pauses; c ends, so b gets its slot inside the pause; a reports `status`
    (`rejected` = pause of the profile, `allowed_warning` = throttle to one turn, None =
    nothing); the user resumes while a still runs."""
    marks: dict[str, float] = {}

    def a_script() -> dict[str, Any]:
        marks["resets_at"] = time.time() + 2.5
        steps: list[dict[str, Any]] = [wait(0.8)]
        if status is not None:
            steps.append({"do": "rate_limit", "status": status, "resets_at": marks["resets_at"]})
        steps += [wait(1.0), say("a")]
        return script(steps)

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)

        async def b_and_pause() -> None:
            await eventually(lambda: "a" in ctx._active and "c" in ctx._active)
            b = asyncio.create_task(ctx.turn("b", "B"))
            await eventually(lambda: ctx.waits_for_capacity("b"))
            ctx.pause()
            await b

        async def user() -> None:
            await eventually(lambda: ctx.paused and "c" not in ctx._active)
            await eventually(lambda: gate_of(ctx).active == 2)  # b holds c's slot now
            await asyncio.sleep(0.7)  # a reports meanwhile
            marks["resume"] = time.time()
            ctx.resume()

        await asyncio.gather(ctx.turn("a", "A"), ctx.turn("c", "C"), b_and_pause(), user())

    manager = make_manager(
        workfolder,
        topology,
        {"a": a_script, "b": script([say("b")]), "c": script([wait(0.3), say("c")])},
        agents=[agent(x, compact=OFF) for x in ("a", "c", "b")],
        settings=make_settings(**{GATE: 2}),
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir
    a_end = next(e["ts"] for e in log_entries(run_dir, "turn_end") if e["data"]["agent"] == "a")
    return {"b_start": starts_of(run_dir, "b")[0], "a_end": a_end, "manager": manager, **marks}


async def test_n4_resume_inside_a_rate_limit_pause_waits_for_the_profile(
    workfolder: Path,
) -> None:
    out = await overlap_run(workfolder, "rejected")
    assert out["b_start"] >= out["resets_at"] - 0.02, "b waited for the end of the pause"
    waits = [e["data"] for e in log_entries(out["manager"].run_dir, "waiting")]
    # b waited for a slot before the pause, and again after the resume
    assert [(w["agent"], w["reason"]) for w in waits] == [("b", "capacity")] * 2
    assert out["manager"].orchestrator._gates[GATE].active == 0


async def test_n4_counter_check_without_rate_limit_starts_at_resume(workfolder: Path) -> None:
    out = await overlap_run(workfolder, None)
    assert out["b_start"] - out["resume"] < 0.3, "b starts at once on resume"
    assert out["b_start"] < out["a_end"]  # beside a: capacity 2
    assert len(log_entries(out["manager"].run_dir, "waiting")) == 1  # only before the pause


async def test_m02_throttle_after_the_pause_waits_for_room(workfolder: Path) -> None:
    """Throttle to one turn while a still runs: b does not run beside a (Mutation M02)."""
    out = await overlap_run(workfolder, "allowed_warning")
    assert out["b_start"] >= out["a_end"] - 0.02, "b began only after a gave its slot back"
    assert out["manager"].orchestrator._gates[GATE].active == 0


# --------------------------------------------------------------------------------------------
# Point 1: random runs (after the reviewer's `np_gate.py`), fixed seeds
# --------------------------------------------------------------------------------------------


def limit_step(r: random.Random) -> list[dict[str, Any]]:
    x = r.random()
    if x < 0.35:
        return [{"do": "rate_limit", "status": "rejected"}]  # pause of the backoff
    if x < 0.5:
        return [{"do": "rate_limit", "status": "allowed_warning"}]
    return []


@pytest.mark.parametrize("seed", [1, 4, 9, 13, 17, 22, 27, 31])
async def test_random_runs_keep_the_count_and_never_start_in_a_pause(
    tmp_path: Path, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    violations: list[str] = []
    acquire, release, init = ProfileGate.acquire, ProfileGate.release, ProfileGate.__init__

    async def spy_acquire(self: ProfileGate, abort: Any) -> bool:
        got = await acquire(self, abort)
        if self.limit is not None and self.active > self.limit:
            violations.append(f"active {self.active} > {self.limit}")
        return got

    def spy_release(self: ProfileGate) -> None:
        release(self)
        if self.active < 0:
            violations.append(f"active {self.active} < 0")

    def short_backoff(self: ProfileGate, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        self._backoff_s = 0.3

    monkeypatch.setattr(ProfileGate, "acquire", spy_acquire)
    monkeypatch.setattr(ProfileGate, "release", spy_release)
    monkeypatch.setattr(ProfileGate, "__init__", short_backoff)

    r = random.Random(seed)
    ids = [f"a{i}" for i in range(r.choice([2, 3, 4]))]
    capacity = r.choice([1, 2, None])
    event = r.choice(["none", "pause", "pause", "stop", "notaus", "cancel"])

    def agent_script() -> dict[str, Any]:
        turns = [[say("w"), wait(r.uniform(0, 0.05)), *limit_step(r), fill(600)] for _ in range(4)]
        notes = [[*limit_step(r), say(NOTE), wait(r.uniform(0, 0.05)), fill(650)] for _ in range(4)]
        return script(*turns, note_turns=notes)

    async def topology(ctx: RunContext) -> None:
        for k in range(3):
            try:
                await ctx.turns([TurnJob(a, f"T{k}") for a in ids])
            except asyncio.CancelledError:  # a turn of this round was cancelled (event)
                if event != "cancel":
                    raise

    settings = make_settings(**({GATE: capacity} if capacity else {}))
    scripts = {a: agent_script() for a in ids}
    manager = make_manager(
        make_workfolder(tmp_path),
        topology,
        scripts,
        agents=[agent(a, compact=LOW) for a in ids],
        settings=settings,
    )
    running = asyncio.create_task(manager.run())
    if event != "none":
        await eventually(lambda: manager.orchestrator is not None, within=10)
        orch = manager.orchestrator
        with contextlib.suppress(AssertionError):
            await eventually(lambda: bool(orch._capacity) or orch.paused, within=2)
        if event == "pause":
            orch.pause()
            await asyncio.sleep(r.uniform(0.05, 0.3))
            orch.resume()
        elif event == "stop":
            manager.stop()
        elif event == "notaus":
            await orch.halt(StopReason.PROFILE_BLOCKED, profile=GATE)
        else:  # cancel the turns that wait or run right now
            for task in asyncio.all_tasks():
                if getattr(task.get_coro(), "__qualname__", "") == "Orchestrator.turn":
                    task.cancel()
    status = await asyncio.wait_for(running, 40)
    orch = manager.orchestrator
    run_dir = manager.run_dir
    pauses = [
        (e["ts"], e["data"]["until"])
        for e in log_entries(run_dir, "rate_limit")
        if e["data"]["action"] == "pause"
    ]
    starts = [(e["ts"], e["data"]["agent"]) for e in log_entries(run_dir, "turn")]
    in_pause = [
        (a, round(t - p0, 3)) for t, a in starts for p0, until in pauses if p0 < t < until - 0.02
    ]
    assert status in (RunStatus.DONE, RunStatus.STOPPED, RunStatus.FAILED)
    assert violations == []
    assert orch._gates[GATE].active == 0
    assert in_pause == [], f"seed {seed}: start inside a pause of the profile"


# --------------------------------------------------------------------------------------------
# Point 2: wait_for_messages refuses in the note turn on every path (G-N3)
# --------------------------------------------------------------------------------------------


async def finished_run(workfolder: Path) -> Path:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")

    manager = make_manager(workfolder, topology, {"solo": script([fill(10)])})
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    return manager.run_dir


@pytest.mark.parametrize("path", ["plain", "yield_to", "streak_full"])
async def test_gn3_wait_for_messages_refuses_on_every_path(workfolder: Path, path: str) -> None:
    store = RoomStore(await finished_run(workfolder))
    room = store.for_agent("solo")
    if path == "yield_to":
        store.set_wait_guard("solo", yield_to=GATE)  # L12: another turn waits for the profile
    elif path == "streak_full":
        store.set_wait_guard("solo", waited=10_000.0)  # L7: the 5 min of the turn are used
    store.begin_note_turn("solo")
    for _ in range(2):  # also the second call, which would end the turn (`turn_end`)
        with pytest.raises(RoomError, match="Übergabe-Notiz"):
            await wait_for_messages(room, 0.2, None, max_s=1, abort=lambda: True)
    assert room.wait_guard().end_turn is None  # nothing written: the note turn goes on
    store.end_note_turn("solo")
    # counter-check: after the note turn the tool answers as before
    outcome = await wait_for_messages(room, 0.2, None, max_s=1, abort=lambda: True)
    assert outcome.ended_by.value in ("timeout", "capacity", "nobody", "turn_end")


# --------------------------------------------------------------------------------------------
# Point 3: the flag never stays behind silently (G-N4)
# --------------------------------------------------------------------------------------------


async def test_gn4_flag_end_fails_is_logged_and_retried(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fails = {"left": 2}  # both tries at the end of the note turn fail

    real_end = RoomStore.end_note_turn

    def flaky_end(self: RoomStore, agent: str) -> None:
        if fails["left"] > 0:
            fails["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        real_end(self, agent)

    monkeypatch.setattr(RoomStore, "end_note_turn", flaky_end)
    flags: list[bool] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        await ctx.turn("solo", "eins")
        await ctx.turn("solo", "zwei")  # note turn; its flag stays after two tries
        flags.append(ctx.room.in_note_turn("solo"))
        await ctx.turn("solo", "drei")  # tries again first
        flags.append(ctx.room.in_note_turn("solo"))

    turns = script([fill(600)], [fill(10)], [fill(10)], note_turns=[good_note()])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert flags == [True, False]
    [entry] = log_entries(manager.run_dir, "note_turn_flag")
    assert entry["data"] == {"agent": "solo", "action": "end", "error": "database is locked"}


async def test_gn4_begin_fails_no_note_turn_and_state_unchanged(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_begin(self: RoomStore, agent: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(RoomStore, "begin_note_turn", broken_begin)
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        await ctx.turn("solo", "eins")
        seen["status"] = ctx.agents["solo"].status
        outcome = await ctx.turn("solo", "zwei")
        seen["outcome"] = outcome
        seen["active"] = dict(ctx._active)

    turns = script([fill(600)], [fill(10)], note_turns=[good_note()])
    manager = make_manager(
        workfolder, topology, {"solo": turns}, agents=[agent("solo", compact=LOW)]
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert seen["outcome"].status is TurnStatus.OK and seen["active"] == {}
    assert len(prompts(manager.run_dir, "solo")) == 2  # no note turn ran, no model request
    assert manager.orchestrator.agents["solo"].turns_used == 2
    [entry] = compacts(manager.run_dir)
    assert (entry["ok"], entry["failures"]) == (False, 1)
    assert log_entries(manager.run_dir, "note_turn_flag")[0]["data"]["action"] == "begin"


async def test_gn4_unreadable_flag_refuses_missing_table_does_not(workfolder: Path) -> None:
    run_dir = await finished_run(workfolder)
    store = RoomStore(run_dir)
    room = store.for_agent("solo")

    class Locked:
        def __enter__(self) -> Any:
            raise sqlite3.OperationalError("database is locked")

        def __exit__(self, *args: Any) -> None:
            return None

    real_read = store._read
    store._read = lambda: Locked()  # type: ignore[method-assign]
    assert store.in_note_turn("solo") is True  # in doubt: refuse
    with pytest.raises(RoomError, match="Übergabe-Notiz"):
        room.not_in_note_turn()
    store._read = real_read  # type: ignore[method-assign]
    with sqlite3.connect(store.db_path) as con:
        con.execute("DROP TABLE note_turn")  # a room from before W4b-05b.1
    assert store.in_note_turn("solo") is False
    store.end_note_turn("solo")  # a missing table is no error
    room.not_in_note_turn()  # nobody is in a note turn: no refusal


# --------------------------------------------------------------------------------------------
# Point 4: the first fitting main request fixes the model (G-N2); M13
# --------------------------------------------------------------------------------------------


def request(model: str, mid: str, tokens: int) -> AssistantMessage:
    return AssistantMessage([TextBlock("t")], model, message_id=mid, usage={"input_tokens": tokens})


def fill_and_window(configured: str, messages: list[Any], usage: dict[str, Any]) -> tuple:
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.0,
        model_usage=usage,
    )
    mapper = ClaudeEvents("r", "a", configured, CostKind.ESTIMATED)
    mapper.start_turn(1)
    for msg in [*messages, result]:
        mapper.feed(msg)
    data = mapper.finish()[-1].data
    return data["context_tokens"], data["context_window"]


OPUS, OLD_OPUS = "claude-opus-5-5", "claude-opus-4-1"
USAGE = {
    OPUS: {"contextWindow": 1_000_000, "canonicalModel": OPUS},
    OLD_OPUS: {"contextWindow": 200_000, "canonicalModel": OLD_OPUS},
}


@pytest.mark.parametrize(
    ("configured", "old_first"),
    [
        ("opus", (300, 200_000)),  # a short name decides only about the first request
        ("claude", (300, 200_000)),
        (OPUS, (90_000, 1_000_000)),  # the full name never fits the other Opus
        ("5", (90_000, 1_000_000)),  # `5` is no part of `claude-opus-4-1`
    ],
)
def test_gn2_first_fitting_request_fixes_the_model(
    configured: str, old_first: tuple[int, int]
) -> None:
    messages = [request(OPUS, "m1", 90_000), request(OLD_OPUS, "m2", 300)]
    # the other Opus after the fixed one counts neither for the fill level nor the window
    assert fill_and_window(configured, messages, USAGE) == (90_000, 1_000_000)
    assert fill_and_window(configured, messages[::-1], USAGE) == old_first


def test_m13_date_suffix_of_the_api_name() -> None:
    dated = "claude-sonnet-5-5-20261001"
    usage = {dated: {"contextWindow": 1_000_000}}
    assert fill_and_window("claude-sonnet-5-5", [request(dated, "m1", 5_000)], usage) == (
        5_000,
        1_000_000,
    )
    # counter-check: another model with a date suffix does not fit
    other = "claude-haiku-5-5-20261001"
    assert fill_and_window("claude-sonnet-5-5", [request(other, "m1", 5_000)], usage)[0] is None


# --------------------------------------------------------------------------------------------
# Point 5: M05 (compact_session after a stop), M11 (two note turns at once)
# --------------------------------------------------------------------------------------------


async def test_m05_compact_session_after_a_stop_does_nothing(workfolder: Path) -> None:
    holder: list[Orchestrator] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        holder.append(ctx)
        await ctx.turn("solo", "eins")
        await ctx.halt(StopReason.USER)
        await ctx.compact_session("solo", note="nach dem Stopp")

    manager = make_manager(workfolder, topology, {"solo": script([fill(10)])})
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.STOPPED
    assert compacts(manager.run_dir) == []
    assert holder[0]._context_of("solo").pending is None

    # counter-check: before the stop the same call compacts
    async def before(ctx: RunContext) -> None:
        await ctx.turn("solo", "eins")
        await ctx.compact_session("solo", note="vor dem Stopp")

    again = make_manager(
        make_workfolder(workfolder.parent / "b"), before, {"solo": script([fill(10)])}
    )
    assert await asyncio.wait_for(again.run(), 30) is RunStatus.DONE
    assert [e["reason"] for e in compacts(again.run_dir)] == ["phase"]


async def test_m11_two_note_turns_at_once_keep_their_own_flags(workfolder: Path) -> None:
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        await ctx.turns([("a", "eins"), ("b", "eins")])

        async def watch() -> None:
            await eventually(lambda: note_running(ctx, "a") and note_running(ctx, "b"))
            seen["both"] = True
            await eventually(lambda: not ctx.room.in_note_turn("a"))
            seen["b_while_a_done"] = ctx.room.in_note_turn("b")

        watcher = asyncio.create_task(watch())
        await ctx.turns([("a", "zwei"), ("b", "zwei")])
        await watcher

    fast = script([fill(600)], [fill(10)], note_turns=[[say(NOTE), wait(0.2), fill(650)]])
    slow = script([fill(600)], [fill(10)], note_turns=[[say(NOTE), wait(0.8), fill(650)]])
    manager = make_manager(
        workfolder,
        topology,
        {"a": fast, "b": slow},
        agents=[agent("a", compact=LOW), agent("b", compact=LOW)],
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert seen == {"both": True, "b_while_a_done": True}
    purposes = [e["data"].get("purpose") for e in log_entries(manager.run_dir, "turn")]
    assert purposes.count(PURPOSE) == 2

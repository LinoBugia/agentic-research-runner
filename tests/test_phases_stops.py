"""W4b-03 A17: nothing hangs (the pattern of `test_phases_hangs.py`). A stop in every state of
the new mechanics ends the runner by itself, within the manager's grace (it is not cancelled:
no error `where: topology`). Cases that could loop end after finitely many turns, never by
`wall_time`; the counter-checks (`naive`) show the loop the guard prevents."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    errors,
    hand_in,
    manager,
    of_kind,
    parallel,
    private,
    run,
    say,
    solo,
    waker_of,
    woken,
)
from run_kit import eventually, log_entries, make_workfolder, room

from arr.models import RunStatus
from arr.runs.compact import NOTE_HEADING

WALL = "8s"
ENDLESS = 10**6
AGENTS = {"agents": [{"id": a} for a in ("a", "b", "s", "t")]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def blocking(view: TurnView) -> list[dict[str, Any]]:
    return [{"do": "delay", "seconds": 120}]


def hand_in_at_once(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view)


def topology_errors(mgr: Any) -> list[Any]:
    return [e for e in log_entries(mgr.run_dir, "error") if e["data"].get("where") == "topology"]


async def stopped(mgr: Any, ready: Any) -> RunStatus:
    """Run, stop as soon as `ready()` holds, return the status."""
    task = asyncio.create_task(run(mgr, within=60))
    await eventually(ready, within=30)
    mgr.stop()
    return await task


def k(*members: str, **extra: Any) -> dict[str, Any]:
    return {"name": "k", "members": list(members), **extra}


# --------------------------------------------------------------------------------------------
# A stop in every state
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", ["discussion_turn", "between_rounds", "wake_up_turn", "b3", "capacity", "note_turn"]
)
async def test_a17_a_stop_in_every_state_ends_the_runner(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    group: dict[str, Any] = {**AGENTS}
    policies: dict[str, Any] = {x: hand_in_at_once for x in ("a", "b", "s", "t")}
    chat = {**parallel("p", ["a", "b"]), "topology": "chat", "channels": ["k"], "rounds": 3}
    if state == "discussion_turn":
        group |= {"channels": [k("a", "b")], "phases": [chat]}
        policies["a"] = blocking
        ready: Any = lambda: bool(log.of("a"))  # noqa: E731
    elif state == "between_rounds":
        group |= {"channels": [k("a", "b", idle_after_s=60)], "phases": [chat]}
        policies["a"] = policies["b"] = lambda v: [say("k", "Ich denke.")]
        ready = lambda: any(d.get("reason") == "idle" for d in of_kind(mgr, "waiting"))  # noqa: E731
    elif state == "wake_up_turn":
        phase = {**solo("p", "a"), "standby": ["s"], "channels": ["k"]}
        group |= {"channels": [k("a", "s")], "phases": [phase]}
        policies["a"] = lambda v: [say("k", "@s Hilfe?"), {"do": "delay", "seconds": 120}]
        policies["s"] = blocking
        ready = lambda: bool(log.of("s"))  # noqa: E731
    elif state == "b3":
        group |= {"phases": [solo("p", "a", turns=1)]}
        policies["a"] = lambda v: [say(private("a"), "@user Frage?", kind="question")]
        ready = lambda: any(d.get("reason") == "user" for d in of_kind(mgr, "waiting"))  # noqa: E731
    elif state == "capacity":
        import phases_kit
        import run_kit

        monkeypatch.setattr(
            phases_kit, "make_settings", lambda: run_kit.make_settings(**{"fake-auth": 1})
        )
        group |= {"phases": [parallel("p", ["a", "b"])]}
        policies["a"] = policies["b"] = blocking
        ready = lambda: mgr.orchestrator is not None and bool(mgr.orchestrator._capacity)  # noqa: E731
    else:  # a note turn of auto-compact before a wake-up turn of `s`
        group = {
            "agents": [{"id": "a"}, {"id": "s", "compact": {"at": 500}}],
            "channels": [k("a", "s")],
            "phases": [{**solo("p", "a"), "standby": ["s"], "channels": ["k"]}],
        }

        def a(view: TurnView) -> list[dict[str, Any]]:
            return [
                say("k", "@s Eins?"),
                room("wait_for_messages", seconds=40),
                say("k", "@s Zwei?"),
                {"do": "delay", "seconds": 120},
            ]

        def s(view: TurnView) -> list[dict[str, Any]]:
            if view.prompt.startswith(NOTE_HEADING):
                return [{"do": "delay", "seconds": 120}]
            return [say("k", "@a Ja."), {"do": "usage", "input": 600, "output": 5}]

        policies = {"a": a, "s": s}
        ready = lambda: mgr.orchestrator is not None and mgr.orchestrator.room.in_note_turn("s")  # noqa: E731
    mgr, log = manager(
        workfolder, group, policies, limits={"wall_time": "60s", "turns_per_agent": ENDLESS}
    )
    assert await stopped(mgr, ready) is RunStatus.STOPPED, errors(mgr)
    assert topology_errors(mgr) == [], "the runner ended by itself, it was not cancelled"
    assert waker_of(mgr) is None, "the waker is gone"


# --------------------------------------------------------------------------------------------
# Finitely many turns, never `wall_time`
# --------------------------------------------------------------------------------------------


async def case(workfolder: Path, group: dict[str, Any], policies: dict[str, Any]) -> tuple:
    mgr, log = manager(
        workfolder, group, policies, limits={"wall_time": WALL, "turns_per_agent": ENDLESS}
    )
    status = await run(mgr, within=60)
    limits = [e["data"].get("limit") for e in log_entries(mgr.run_dir, "limit")]
    return mgr, log, {"status": status, "hang": "wall_time" in limits}


async def test_a17_a_discussing_agent_that_never_hands_in_ends(workfolder: Path) -> None:
    chat = {**parallel("p", ["a", "b"]), "topology": "chat", "channels": ["k"], "rounds": 2}
    group = {**AGENTS, "channels": [k("a", "b", idle_after_s=0.05)], "phases": [chat]}
    mgr, log, found = await case(workfolder, group, {"a": lambda v: [], "b": hand_in_at_once})
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    [gate] = of_kind(mgr, "checkpoint", phase="gate")
    assert gate["missing"] == ["a/work"]
    assert len(log.of("a")) == 1 + 2 + 1, "kick-off, two idle rounds, the one follow-up"


async def test_a17_a_sleeper_that_never_answers_ends(workfolder: Path) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "@s Hilfe?", kind="question"), room("wait_until", minutes=0.005)]

    phase = {**solo("p", "a", turns=2), "standby": ["s"], "channels": ["k"]}
    group = {**AGENTS, "channels": [k("a", "s")], "phases": [phase]}
    mgr, log, found = await case(workfolder, group, {"a": a, "s": lambda v: []})
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    assert len(log.of("a")) == 3 and 1 <= len(woken(log, "s")) <= 3


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_a17_two_sleepers_that_talk_forever_hold_nothing(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """`s` and `t` (both on standby) address each other in every wake-up, without a limit of
    wake-ups and without the ping-pong guard. The phase ends with the action of `a`.
    Counter-check: a phase that waits for its sleepers to be quiet runs until `wall_time`."""
    from arr.runs.pacing import PingPongTracker
    from arr.runs.phases import wake

    monkeypatch.setattr(PingPongTracker, "observe", lambda self, msg: None)
    if naive:

        async def wait_for_quiet(self: Any) -> None:
            quiet = 0
            while quiet < 100:  # a second without a wake-up (the naive phase, on purpose)
                quiet = 0 if self.due or self.turns else quiet + 1
                await asyncio.sleep(0.01)
            self.waking = False

        monkeypatch.setattr(wake.Waker, "end_actions", wait_for_quiet)

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "@s Fang an."), {"do": "delay", "seconds": 0.3}, *hand_in(view)]

    def talks_to(other: str) -> Any:
        # the other one has ended its turn when the message comes, so it is woken
        return lambda view: [{"do": "delay", "seconds": 0.05}, say("k", f"@{other} Weiter so.")]

    phase = {**solo("p", "a"), "standby": ["s", "t"], "channels": ["k"]}
    group = {
        **AGENTS,
        "channels": [k("a", "s", "t", max_wakeups_per_agent=10**6)],
        "phases": [phase],
    }
    mgr, log, found = await case(
        workfolder, group, {"a": a, "s": talks_to("t"), "t": talks_to("s")}
    )
    if naive:
        assert found["hang"], found
        return
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    assert of_kind(mgr, "checkpoint", phase="gate")


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_a17_ping_pong_in_the_direct_channel_of_a_solo_phase_ends(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """`a` asks `s` again after every answer, `s` asks back: the ping-pong guard stops the
    wake-ups of `s`. Counter-check without the guard: `s` is woken for every question."""
    from arr.runs.pacing import PingPongTracker

    if naive:
        monkeypatch.setattr(PingPongTracker, "observe", lambda self, msg: None)

    def a(view: TurnView) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for n in range(20):
            steps += [say("k", f"@s Frage {n}?"), room("wait_for_messages", seconds=0.3)]
        return [*steps, *hand_in(view)]

    phase = {**solo("p", "a"), "standby": ["s"], "channels": ["k"]}
    group = {**AGENTS, "channels": [k("a", "s", max_wakeups_per_agent=10**6)], "phases": [phase]}
    mgr, log, found = await case(
        workfolder, group, {"a": a, "s": lambda v: [say("k", "@a Und du?")]}
    )
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    if naive:
        assert len(woken(log, "s")) > 10
    else:
        assert of_kind(mgr, "ping_pong") and len(woken(log, "s")) <= 4


async def test_a17_many_messages_at_once_wake_at_most_once_each(workfolder: Path) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        burst = [say("k", f"@s Nachricht {n}") for n in range(30)]
        return [*burst, room("wait_for_messages", seconds=1), *hand_in(view)]

    phase = {**solo("p", "a"), "standby": ["s"], "channels": ["k"]}
    group = {**AGENTS, "channels": [k("a", "s", max_wakeups_per_agent=3)], "phases": [phase]}
    mgr, log, found = await case(workfolder, group, {"a": a, "s": lambda v: []})
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    messages = [m for w in of_kind(mgr, "wake", agent="s") for m in w["messages"]]
    assert len(messages) == len(set(messages)) and len(woken(log, "s")) <= 3


async def test_a17_questions_nobody_answers_end(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question to a sleeper at its limit, to an agent not in the phase and to the user
    (whose hold is shortened): the phase ends after finitely many turns."""
    from arr.runs.phases import wake

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [
            say("k", "@s Eins?", kind="question"),
            say("k", "@s Zwei?", kind="question"),
            say("k", "@b Drei?", kind="question"),
            say(private("a"), "@user Vier?", kind="question"),
            room("wait_until", minutes=0.005),
        ]

    phase = {**solo("p", "a", turns=2), "standby": ["s"], "channels": ["k"]}
    group = {**AGENTS, "channels": [k("a", "s", "b", max_wakeups_per_agent=1)], "phases": [phase]}
    monkeypatch.setattr(wake, "user_question_hold_s", lambda: 0.2)
    mgr, log, found = await case(workfolder, group, {"a": a, "s": lambda v: []})
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    assert len(log.of("a")) == 3 and log.of("b") == []
    assert of_kind(mgr, "wake_skipped", agent="s", reason="max_wakeups_per_agent")

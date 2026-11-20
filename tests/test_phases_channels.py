"""W4b-03 §6.3: who gets what in a phase (A6 channels per phase, A7 not in the phase), with
FakeBackend agents and the real RunManager, Orchestrator and room."""

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
    run_with,
    say,
    solo,
    waker_of,
    woken,
)
from run_kit import eventually, make_workfolder, room

from arr.models import RunStatus
from arr.room.store import USER, ChannelState
from arr.room.types import MessageKind, MessageVia, RoomError

AGENTS = {"agents": [{"id": a} for a in ("a", "s", "b")]}
K = {"name": "k", "members": ["a", "s", "b"]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def hand_in_at_once(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view)


def answers_or_work(view: TurnView) -> list[dict[str, Any]]:
    if view.context.get("standby"):
        return [room("read_messages"), say("k", "@a Gelesen.")]
    return hand_in(view)


# --------------------------------------------------------------------------------------------
# A6: open are exactly the channels of the phase and the private ones
# --------------------------------------------------------------------------------------------


async def test_a6_a_channel_the_phase_does_not_name_is_closed_and_quiet(workfolder: Path) -> None:
    """`p1` opens `#k`, `p2` does not: there `a` can neither read nor write it, a message of
    the user to `s` in it neither wakes nor reaches anyone; the private channels stay open.
    `p3` opens `#k` again: a new message to `s` wakes it, and it finds the old one unread."""
    holder: dict[str, Any] = {}
    seen: dict[str, Any] = {}

    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.stage == "p2":
            mine = holder["mgr"].room.for_agent("a")
            for name, call in (
                ("post", lambda: mine.post_message("k", "Darf ich?")),
                ("read", lambda: mine.read_messages("k")),
            ):
                try:
                    call()
                    seen[name] = "ok"
                except RoomError as exc:
                    seen[name] = str(exc)
            seen["private"] = mine.post_message(private("a"), "Notiz an mich.").channel
            states = {c.name: c.open for c in holder["mgr"].room.channels()}
            seen["states"] = states
            return [say("k", "Versuch über das Werkzeug."), *hand_in(view)]
        if view.stage == "p3":
            return [say("k", "@s Bist du da?"), room("wait_for_messages", seconds=40)] + hand_in(
                view
            )
        return hand_in(view)

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "phase_enter", stage="p2")), within=30)
        msg = mgr.room.post("k", "@s Alte Frage?", sender=USER, via=MessageVia.GUI)
        seen["old"] = msg.id

    phases = [
        {**solo("p1", "a"), "channels": ["k"]},
        {**solo("p2", "a"), "standby": ["s"]},
        {**solo("p3", "a"), "standby": ["s"], "channels": ["k"]},
    ]
    mgr, log = manager(
        workfolder, {**AGENTS, "channels": [K], "phases": phases}, {"a": a, "s": answers_or_work}
    )
    holder["mgr"] = mgr
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    closed = "Der Kanal #k ist gerade geschlossen."
    assert seen["post"].startswith(closed) and seen["read"].startswith(closed)
    assert seen["private"] == private("a")
    assert seen["states"] == {
        "k": False,
        private("a"): True,
        private("s"): True,
        private("b"): True,
    }
    assert not [m for m in mgr.room.messages("k") if m.sender == "a" and "Versuch" in m.body]
    wakes = of_kind(mgr, "wake", agent="s")
    assert [w["stage"] for w in wakes] == ["p3"], "nothing in p2"
    [wake] = woken(log, "s")
    assert "Alte Frage?" in wake.prompt, "the old message waited unread"
    assert seen["old"] not in wakes[0]["messages"]


# --------------------------------------------------------------------------------------------
# A7: an agent not in the phase gets nothing (Entscheidung 64)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("topology", ["solo", "parallel", "chat"])
async def test_a7_not_in_the_phase_no_turn_no_wake_up_then_unread(
    workfolder: Path, topology: str
) -> None:
    """`b` is a member of the open `#k` but neither has an action nor is on standby: a
    question and a mention to it change nothing (no turn, no wake-up, the phase ends as
    usual); in its own later phase it finds them unread."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.stage == "p" and "# Auftrag – Phase „p“" in view.prompt:
            return [
                say("k", "@b Was meinst du?", kind="question"),
                say("k", "Siehe oben.", to=["b"]),
                *hand_in(view),
            ]
        return hand_in(view)

    if topology == "chat":
        first = {**parallel("p", ["a", "s"]), "topology": "chat", "channels": ["k"], "rounds": 1}
    elif topology == "parallel":
        first = {**parallel("p", ["a", "s"]), "channels": ["k"]}
    else:
        first = {**solo("p", "a"), "channels": ["k"]}
    phases = [first, {**solo("q", "b"), "channels": ["k"]}]
    policies = {"a": a, "s": hand_in_at_once, "b": hand_in_at_once}
    mgr, log = manager(workfolder, {**AGENTS, "channels": [K], "phases": phases}, policies)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert [v.stage for v in log.of("b")] == ["q"]
    assert of_kind(mgr, "wake_scheduled", agent="b") == [] and of_kind(mgr, "wake", agent="b") == []
    prompt = log.of("b")[0].prompt
    assert "Offene Fragen an dich (beantworte sie zuerst)" in prompt and "Was meinst du?" in prompt
    assert "Siehe oben." in prompt


async def test_a7_only_participants_hear_of_a_message(workfolder: Path) -> None:
    """While a pass runs, the orchestrator gives hints only to its participants: a message in
    an open channel reaches `a` and `s` (standby), not `b`; in a private channel only its
    owner."""
    seen: dict[str, Any] = {}

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "Notiz an alle."), room("wait_for_messages", seconds=40, until_file="x")]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: waker_of(mgr) is not None, within=30)
        await eventually(lambda: any(m.sender == "a" for m in mgr.room.messages("k")), 30)
        orch = mgr.orchestrator
        note = next(m for m in mgr.room.messages("k") if m.sender == "a")
        seen["k"] = orch._recipients(note)
        user = mgr.room.post(private("s"), "Hallo s.", sender=USER, via=MessageVia.GUI)
        seen["private"] = orch._recipients(user)
        (orch.prepared.agents["a"].work_dir / "x").write_text("los", "utf-8")

    phase = {**solo("p", "a"), "standby": ["s"], "channels": ["k"]}
    mgr, log = manager(
        workfolder,
        {**AGENTS, "channels": [K], "phases": [phase]},
        {"a": lambda v: a(v) + hand_in(v)},
    )
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    assert seen == {"k": ["s"], "private": ["s"]}
    assert mgr.room.channel_state("k") is ChannelState.OPEN
    assert all(m.kind is not MessageKind.QUESTION for m in mgr.room.messages("k"))

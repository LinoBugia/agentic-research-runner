"""W4b-03 §6.4: waking in every phase (A8–A11, A23) with FakeBackend agents and the real
RunManager, Orchestrator and room. Orders and states, no durations: a turn that has to wait
for the test waits for a file (`until_file`) the test writes when the log says so."""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    errors,
    hand_in,
    manager,
    of_kind,
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
from arr.room.store import USER
from arr.room.types import MessageKind, MessageVia

AGENTS = {"agents": [{"id": a} for a in ("a", "s", "b")]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def group(*phases: dict[str, Any], **channel: Any) -> dict[str, Any]:
    members = channel.pop("members", ["a", "s"])
    return {**AGENTS, "channels": [{"name": "k", "members": members, **channel}], "phases": phases}


def beweis(name: str = "p", **extra: Any) -> dict[str, Any]:
    """A phase `solo`: `a` has the action, `s` is on standby, `#k` is open."""
    return {**solo(name, "a"), "standby": ["s"], "channels": ["k"], **extra}


def flag(name: str) -> dict[str, Any]:
    """Wait inside the turn until the test writes the file `name` into the work folder."""
    return room("wait_for_messages", seconds=40, until_file=name)


def release(mgr: Any, agent: str, name: str) -> None:
    orch = mgr.orchestrator
    (orch.prepared.agents[agent].work_dir / name).write_text("los", "utf-8")


def answers(view: TurnView) -> list[dict[str, Any]]:
    """A sleeper that answers the open question to it (and tries `submit`, refused)."""
    return [
        room("inbox", save_as="box"),
        say(
            "k",
            "Ja, Lemma 2 gilt.",
            kind="answer",
            reply_to="${box.open_questions.0.id}",
        ),
        room("submit", status="done", summary="geht nicht"),
    ]


def asks_then_hands_in(view: TurnView) -> list[dict[str, Any]]:
    """`a` asks `@s`, waits for the answer in the turn, then hands in."""
    if view.turn > 1:
        return hand_in(view)
    return [
        say("k", "@s Gilt Lemma 2?", kind="question"),
        room("wait_for_messages", seconds=40),
        *hand_in(view),
    ]


# --------------------------------------------------------------------------------------------
# A8: a direct message wakes a sleeper
# --------------------------------------------------------------------------------------------


async def test_a8_a_question_wakes_the_sleeper_once_without_a_submission(
    workfolder: Path,
) -> None:
    mgr, log = manager(workfolder, group(beweis()), {"a": asks_then_hands_in, "s": answers})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [wake] = woken(log, "s")
    assert wake.context == {"phase": "p", "attempt": 1, "standby": True}
    assert "submission" not in wake.context and wake.stage == "p"
    assert "auf Abruf" in wake.prompt and "#k" in wake.prompt and "`submit`" in wake.prompt
    assert mgr.room.submissions(agent="s") == [], "the room refuses `submit` in a wake-up"
    [entry] = of_kind(mgr, "wake", agent="s")
    question = next(m for m in mgr.room.messages("k") if m.kind is MessageKind.QUESTION)
    assert entry["reasons"] == ["question"] and entry["messages"] == [question.id]
    assert (entry["channel"], entry["stage"], entry["attempt"], entry["count"]) == ("k", "p", 1, 1)
    assert [s.agent for s in mgr.room.submissions()] == ["a"]
    assert not question.open or not mgr.room.message(question.id).open, "s answered"


@pytest.mark.parametrize("how", ["mention", "reply", "private"])
async def test_a8_every_kind_of_direct_message_wakes(workfolder: Path, how: str) -> None:
    """`to`, a reply to its message (without `to` or `@name`) and a message of the user with
    `@s` in its private channel each wake the sleeper."""
    seen: list[str] = []

    def sleeper(view: TurnView) -> list[dict[str, Any]]:
        seen.append(view.prompt)
        return [say("k", "Erledigt.")] if not seen[:-1] else [say("k", "Noch einmal da.")]

    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.turn > 1:
            return hand_in(view)
        steps: list[dict[str, Any]] = []
        if how == "mention":
            steps.append(say("k", "Schau bitte auf Lemma 2.", to=["s"]))
        elif how == "reply":
            steps += [say("k", "@s Hallo."), flag("go")]
            steps.append(room("read_messages", channel="k", only_new=False, limit=1, save_as="m"))
            steps.append(say("k", "Und noch eine Sache.", reply_to="${m.0.id}"))
        steps += [flag("done"), *hand_in(view)]
        return steps

    async def helper(mgr: Any) -> None:
        await eventually(lambda: mgr.orchestrator is not None, within=20)
        if how == "private":
            await eventually(lambda: waker_of(mgr) is not None, within=20)
            mgr.room.post(private("s"), "@s Bitte melde dich.", sender=USER, via=MessageVia.GUI)
        if how == "reply":
            await eventually(lambda: bool(of_kind(mgr, "turn_end", agent="s")), within=20)
            release(mgr, "a", "go")
        target = 2 if how == "reply" else 1
        await eventually(lambda: len(of_kind(mgr, "turn_end", agent="s")) >= target, within=20)
        release(mgr, "a", "done")

    phase = beweis()
    mgr, log = manager(workfolder, group(phase), {"a": a, "s": sleeper})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    reasons = [e["reasons"] for e in of_kind(mgr, "wake", agent="s")]
    expected = {
        "mention": [["mention"]],
        "reply": [["mention"], ["reply"]],
        "private": [["mention"]],
    }
    assert reasons == expected[how]
    if how == "private":
        assert of_kind(mgr, "wake", agent="s")[0]["channel"] == private("s")
        assert f"#{private('s')}" in woken(log, "s")[0].prompt

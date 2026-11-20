"""W4b-03 §6.6 (A14): an open question to the user holds the phase (B3) in `solo`,
`parallel`, `chat` and after the rating actions of a gate `judge`, with FakeBackend agents and
the real RunManager, Orchestrator and room.

The deadline `user_question_hold_s` is replaced by a value the test controls: it is long
while the test answers, and the test sets it to 0 to let it pass. No test waits real minutes,
and none measures a duration."""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    entries,
    errors,
    hand_in,
    judge_gate,
    manager,
    of_kind,
    parallel,
    private,
    run_with,
    say,
    solo,
)
from run_kit import eventually, make_workfolder

from arr.models import RunStatus
from arr.room.store import USER
from arr.room.types import MessageKind, MessageVia
from arr.runs import pacing
from arr.runs.orchestrator import LimitKind, StopReason
from arr.runs.phases import wake

AGENTS = {"agents": [{"id": a} for a in ("a", "b", "r")]}
ASK = "@user Darf ich annehmen, dass n prim ist?"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """The deadline of B3, controlled by the test (seconds after the question)."""
    value = {"s": 600.0}
    monkeypatch.setattr(wake, "user_question_hold_s", lambda: value["s"])
    monkeypatch.setattr(pacing, "user_question_hold_s", lambda: value["s"])
    return value


def asks(view: TurnView) -> list[dict[str, Any]]:
    """Asks the user in its private channel and does not hand in; after the answer it does."""
    if "Der User hat deine Frage beantwortet" in view.prompt:
        return hand_in(view, summary=f"{view.agent} nach der Antwort")
    if view.follow_up:
        return [{"do": "text", "text": "Ich warte noch."}]
    return [say(private(view.agent), ASK, kind="question")]


def hand_in_at_once(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view)


def waits_for_user(mgr: Any, agent: str = "a") -> bool:
    return any(
        d.get("reason") == "user" and d.get("channel") == private(agent)
        for d in of_kind(mgr, "waiting")
    )


def answer(mgr: Any, agent: str = "a", text: str = "Ja, n ist prim.") -> None:
    """The user answers in the channel (`Antwort an <agent>`: `reply_to` and `to`)."""
    [question] = [m for m in mgr.room.messages(private(agent)) if m.kind is MessageKind.QUESTION]
    mgr.room.post(
        private(agent),
        text,
        sender=USER,
        kind=MessageKind.ANSWER,
        to=[agent],
        reply_to=question.id,
        via=MessageVia.GUI,
    )


def order(mgr: Any) -> list[str]:
    """The waits of the phase and the gate, in order."""
    out = []
    for kind, data in entries(mgr, "waiting", "checkpoint", "turn"):
        if kind == "waiting" and data.get("stage") and data.get("channel", "").startswith("user."):
            out.append(f"waiting:{data['reason']}")
        elif kind == "checkpoint" and data.get("phase") == "gate":
            out.append("gate")
        elif kind == "turn":
            out.append(f"turn:{data['agent']}")
    return out


# --------------------------------------------------------------------------------------------
# solo and parallel
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("topology", ["solo", "parallel"])
async def test_a14_an_answer_gives_exactly_one_turn_and_its_submission_counts(
    workfolder: Path, hold: dict[str, float], topology: str
) -> None:
    async def user(mgr: Any) -> None:
        await eventually(lambda: waits_for_user(mgr), within=30)
        answer(mgr)

    phases = {"solo": solo("p", "a", turns=1), "parallel": parallel("p", ["a", "b"], turns=1)}
    phase = phases[topology]
    mgr, log = manager(workfolder, {**AGENTS, "phases": [phase]}, {"a": asks, "b": hand_in_at_once})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    after = [v for v in log.of("a") if "Der User hat deine Frage beantwortet" in v.prompt]
    assert len(after) == 1 and after[0].context.get("submission"), "one turn with the package"
    assert order(mgr)[-4:] == ["waiting:user", "turn:a", "waiting:none", "gate"]
    [gate] = of_kind(mgr, "checkpoint", phase="gate")
    newest = [s for s in mgr.room.submissions(agent="a")]
    assert [s.summary for s in newest] == ["a nach der Antwort"] and newest[0].id in gate["results"]
    [woke] = of_kind(mgr, "wake", agent="a")
    assert (woke["reasons"], woke["channel"], woke["count"]) == (["answer"], private("a"), 1)
    states = [d["state"] for d in of_kind(mgr, "user_question")]
    assert states == ["open", "answered"]


async def test_a14_the_deadline_passes_the_phase_goes_on_the_question_stays_open(
    workfolder: Path, hold: dict[str, float]
) -> None:
    async def user(mgr: Any) -> None:
        await eventually(lambda: waits_for_user(mgr), within=30)
        hold["s"] = 0.0  # the deadline has passed

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a", turns=1)]}, {"a": asks})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    assert order(mgr)[-3:] == ["waiting:user", "waiting:none", "gate"]
    [gate] = of_kind(mgr, "checkpoint", phase="gate")
    assert gate["missing"] == ["a/work"]
    assert [d["state"] for d in of_kind(mgr, "user_question")] == ["open", "closed"], (
        "open until the end of the run"
    )


@pytest.mark.parametrize("how", ["stop", "limit"])
async def test_a14_stop_and_limit_end_the_wait_at_once(
    workfolder: Path, hold: dict[str, float], how: str
) -> None:
    async def user(mgr: Any) -> None:
        await eventually(lambda: waits_for_user(mgr), within=30)
        if how == "stop":
            mgr.stop()
        else:
            await mgr.orchestrator.halt(StopReason.LIMIT, LimitKind.BUDGET_USD, cost_usd=9.0)

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a", turns=1)]}, {"a": asks})
    status = await run_with(mgr, user)
    assert status in (RunStatus.STOPPED, RunStatus.DONE), errors(mgr)
    assert "gate" not in order(mgr)
    assert order(mgr)[-2:] == ["waiting:user", "waiting:none"]


async def test_a14_a_submission_while_the_question_is_open_closes_it(
    workfolder: Path, hold: dict[str, float]
) -> None:
    """L18: the asker hands in although its question is open: the question closes („hat ohne
    Antwort weitergearbeitet“) and nothing holds the phase."""

    def asks_and_hands_in(view: TurnView) -> list[dict[str, Any]]:
        return [say(private("a"), ASK, kind="question"), *hand_in(view)]

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a")]}, {"a": asks_and_hands_in})
    assert await run_with(mgr, lambda m: _nothing()) is RunStatus.DONE, errors(mgr)
    assert not any(o.startswith("waiting") for o in order(mgr))
    states = [(d["state"], d.get("reason")) for d in of_kind(mgr, "user_question")]
    assert states == [("open", None), ("closed", "result")]


async def _nothing() -> None:
    return None


# --------------------------------------------------------------------------------------------
# chat and the rating of a gate `judge`
# --------------------------------------------------------------------------------------------


async def test_a14_in_chat_a_question_in_the_private_channel_holds_after_the_discussion(
    workfolder: Path, hold: dict[str, float]
) -> None:
    """`a` asks the user in its private channel during the discussion and does not hand in,
    also not when asked again: after the discussion and the follow-up the phase waits; the
    answer gives `a` one turn with its package."""

    async def user(mgr: Any) -> None:
        await eventually(lambda: waits_for_user(mgr), within=30)
        answer(mgr)

    phase = {**parallel("p", ["a", "b"]), "topology": "chat", "channels": ["k"], "rounds": 1}
    group = {**AGENTS, "channels": [{"name": "k", "members": ["a", "b"], "mode": "rounds"}]}
    mgr, log = manager(workfolder, {**group, "phases": [phase]}, {"a": asks, "b": hand_in_at_once})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    after = [v for v in log.of("a") if "Der User hat deine Frage beantwortet" in v.prompt]
    assert len(after) == 1
    assert [s.summary for s in mgr.room.submissions(agent="a")] == ["a nach der Antwort"]
    discussion_end = next(i for i, (k, _) in enumerate(entries(mgr)) if k == "discussion_end")
    waits = [i for i, (k, d) in enumerate(entries(mgr)) if k == "waiting" and d.get("stage")]
    assert waits and min(waits) > discussion_end


async def test_a14_in_chat_an_answer_in_the_discussion_wakes_its_asker_once(
    workfolder: Path, hold: dict[str, float]
) -> None:
    """The question in the discussion channel holds the discussion (as in `discuss`); its
    answer wakes the asker once – the hold of the phase gives it no second turn."""

    def asks_in_k(view: TurnView) -> list[dict[str, Any]]:
        if "Der User hat deine Frage beantwortet" in view.prompt:
            return hand_in(view, summary="a nach der Antwort")
        if "# Auftrag –" in view.prompt:
            return [say("k", ASK, kind="question"), room_done()]
        return [{"do": "text", "text": "…"}]

    async def user(mgr: Any) -> None:
        await eventually(
            lambda: any(d.get("reason") == "user" for d in of_kind(mgr, "waiting")), 30
        )
        [question] = [m for m in mgr.room.messages("k") if m.kind is MessageKind.QUESTION]
        mgr.room.post(
            "k",
            "Ja.",
            sender=USER,
            kind=MessageKind.ANSWER,
            to=["a"],
            reply_to=question.id,
            via=MessageVia.GUI,
        )

    phase = {**parallel("p", ["a", "b"]), "topology": "chat", "channels": ["k"], "rounds": 1}
    group = {**AGENTS, "channels": [{"name": "k", "members": ["a", "b"], "mode": "rounds"}]}
    mgr, log = manager(
        workfolder, {**group, "phases": [phase]}, {"a": asks_in_k, "b": hand_in_at_once}
    )
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    after = [v for v in log.of("a") if "Der User hat deine Frage beantwortet" in v.prompt]
    assert len(after) == 1, "one turn for the answer"
    assert [d["reasons"] for d in of_kind(mgr, "wake", agent="a")] == [["answer"]]


def room_done() -> dict[str, Any]:
    from run_kit import room

    return room("set_status", status="done")


async def test_a14_after_the_rating_a_question_of_a_rater_holds_the_gate(
    workfolder: Path, hold: dict[str, float]
) -> None:
    def rater(view: TurnView) -> list[dict[str, Any]]:
        if view.action != "gate":
            return hand_in(view)
        return asks(view)

    async def user(mgr: Any) -> None:
        await eventually(lambda: waits_for_user(mgr, "r"), within=30)
        answer(mgr, "r", "Bewerte nur die Korrektheit.")

    phase = {**solo("p", "a"), "gate": judge_gate("r", rule="mean", pass_score=5)}
    phase["gate"]["routes"] = {"pass": "next", "fail": "next"}
    phase["gate"]["action"]["turns"] = 1
    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [phase]}, {"a": hand_in_at_once, "r": rater}
    )
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    after = [v for v in log.of("r") if "Der User hat deine Frage beantwortet" in v.prompt]
    assert len(after) == 1 and after[0].context["submission"]["action"] == "gate"
    [gate] = of_kind(mgr, "checkpoint", phase="gate")
    assert gate["value"] == "pass" and gate["score"] == 8

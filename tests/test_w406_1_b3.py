"""W4-06.1 B3 (User 11.11., option a): an open question to the user holds its discussion phase.

Live run `20261111-1530_…_7e7e`: judge-2 asked `@user` in #judges (rounds) and reported
`done` 6 s later; both judges were done, the discussion ended at once and the run with it, so
the user never had the chance to answer. Now the phase waits until the user answers or the L7
deadline after the question (`waiting.STREAK_MAX_S`, 5 min) passed; an answer wakes the
asker once (L13); a stop ends the wait at once. FakeBackend only, no model.

Counter-check: `naive` replaces the hold by nothing (the code before W4-06.1).
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    channel,
    done,
    eventually,
    log_entries,
    make_manager,
    make_workfolder,
    post,
    turns,
    wait,
)

from arr.models import RunStatus
from arr.permissions.types import ApprovalStatus, ApprovalType, DecisionAction
from arr.room import waiting
from arr.room.store import USER
from arr.room.types import MessageKind, MessageVia
from arr.runs import pacing
from arr.runs.context import DiscussionEnd, DiscussionOutcome, RunContext
from arr.runs.manager import QUESTION_CLOSED
from arr.runs.orchestrator import Orchestrator

J1, J2 = "judge-1", "judge-2"
ASK = post("judges", "@user Soll die Aussage nur für n ≥ 1 beurteilt werden?", kind="question")


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def naive(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(pacing, "_hold_for_user_questions", nothing)


async def judges(
    workfolder: Path,
    scripts: dict[str, Any],
    *,
    mode: str = "rounds",
    during: Any = None,
    within: float = 30,
) -> tuple[Any, DiscussionOutcome, float]:
    """One discussion in #judges like the review topology (one round), then the run ends."""
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        helper = asyncio.create_task(during(ctx)) if during is not None else None
        results.append(await ctx.discuss("judges", "Besprecht die Gutachten.", rounds=1))
        if helper is not None:
            await helper

    extra = {"idle_after_s": 0.1} if mode == "event" else {}
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("judges", [J1, J2], mode=mode, **extra)],
        run_extra={"limits": {"wall_time": "60s"}},
    )
    started = time.monotonic()
    status = await asyncio.wait_for(manager.run(), within)
    assert status in (RunStatus.DONE, RunStatus.STOPPED)
    return manager, results[0], time.monotonic() - started


def waits(manager: Any) -> list[str]:
    return [e["data"]["reason"] for e in log_entries(manager.run_dir, "waiting")
            if e["data"].get("channel") == "judges"]  # fmt: skip


def question_item(manager: Any) -> Any:
    [item] = [i for i in manager.inbox.items() if i.type is ApprovalType.QUESTION]
    return item


@pytest.mark.parametrize("old", [False, True], ids=["b3", "naive"])
async def test_run_7e7e_the_question_holds_the_phase_until_the_l7_deadline(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, old: bool
) -> None:
    """Run …_7e7e (rounds): judge-1 done, judge-2 asks and reports `done` at once. Without an
    answer the phase waits the L7 deadline (shortened to 1.5 s), then ends; the question
    closes with the run (V10 c: the agent worked with an assumption)."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.5)
    if old:
        naive(monkeypatch)
    scripts = {J1: turns([done("major")]), J2: turns([ASK, done("major, Annahme n ≥ 1")])}
    manager, outcome, took = await judges(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    item = question_item(manager)
    assert item.status is ApprovalStatus.OBSOLETE
    obsolete = [e for e in log_entries(manager.run_dir, "user_question")]
    assert [e["data"]["state"] for e in obsolete] == ["open", "closed"]
    lines = (manager.run_dir / "approvals.jsonl").read_text("utf-8")
    assert QUESTION_CLOSED in lines
    if old:
        assert took < 1.5 and waits(manager) == [], "the old end: at once, no wait"
        return
    assert took >= 1.4, f"the phase ended after {took:.2f} s, before the deadline"
    assert waits(manager) == ["user", "none"]
    [start] = [e for e in log_entries(manager.run_dir, "waiting") if e["data"]["reason"] == "user"]
    asked = next(m.ts for m in manager.room.messages("judges") if m.kind is MessageKind.QUESTION)
    assert abs(start["data"]["until"] - (asked + 1.5)) < 0.5, "until = question + L7 deadline"


@pytest.mark.parametrize("mode", ["rounds", "event"])
async def test_an_answer_within_the_deadline_wakes_the_asker_and_ends_the_wait(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The user answers through „Antwort an X“ (kind `answer`, `to` the asker, `reply_to` the
    question, via the GUI) while the phase waits: the asker gets one turn with the answer in
    its inbox (L13), then the phase ends long before the deadline."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 30.0)

    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: bool(orch.inbox.pending()), within=10)
        await eventually(lambda: "user" in waits_of(orch), within=10)
        [question] = [m for m in orch.room.messages("judges") if m.kind is MessageKind.QUESTION]
        orch.room.post(
            "judges",
            "Nur n ≥ 1.",
            sender=USER,
            kind=MessageKind.ANSWER,
            to=[J2],
            reply_to=question.id,
            via=MessageVia.GUI,
        )

    scripts = {
        J1: turns([done("major")]),
        J2: turns([ASK, done("major, Annahme n ≥ 1")], [done("Antwort gelesen, Urteil bleibt")]),
    }
    manager, outcome, took = await judges(workfolder, scripts, mode=mode, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 15
    item = question_item(manager)
    assert item.status is ApprovalStatus.DECIDED
    assert item.decision is not None and item.decision.action is DecisionAction.ANSWER
    starts = [e for e in _events(manager, J2) if e["kind"] == "turn_start"]
    assert len(starts) == 2, "one more turn for the asker"
    assert "Nur n ≥ 1." in starts[1]["data"]["prompt"], "the answer is in its inbox"
    [wake] = [e["data"] for e in log_entries(manager.run_dir, "wake") if e["data"]["agent"] == J2]
    assert wake["count"] == 1
    if mode == "rounds":  # after the rounds: the hold's own wake-up
        assert wake["reasons"] == ["answer"]
        assert "Der User hat deine Frage beantwortet" in starts[1]["data"]["prompt"]
    else:  # event: back in the discussion, L15 wakes for the unread answer (N2)
        assert wake["reasons"] != ["answer"]
        assert "Du wurdest in #judges geweckt" in starts[1]["data"]["prompt"]
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert end["data"]["wakeups"] == {J2: 1}, "B13 counts the wake-up of the answer"
    assert outcome.wakeups == {J2: 1}
    assert waits(manager)[-1] == "none"


@pytest.mark.parametrize("old", [False, True], ids=["n2", "naive"])
async def test_n2_in_event_mode_the_asker_can_wake_another_member_after_the_answer(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, old: bool
) -> None:
    """W4-06.2 N2 (review): after the answer the asker writes `@judge-1 …`. In `event` this
    wakes judge-1 like during the discussion (L13). Counter-check `naive`: the hold's own
    wake-up of W4-06.1, after which nothing wakes judge-1."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 30.0)
    if old:

        async def w4061(orch, config, stage, context, outcome, rounds, answered):  # noqa: ANN001
            for question in answered:  # the hold's own wake-up, as in W4-06.1
                await pacing._wake_for_answer(orch, config, stage, context, question, outcome)

        monkeypatch.setattr(pacing, "_answered_in_event", w4061)

    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: "user" in waits_of(orch), within=10)
        [question] = [m for m in orch.room.messages("judges") if m.kind is MessageKind.QUESTION]
        orch.room.post(
            "judges", "Nur n ≥ 1.", sender=USER, kind=MessageKind.ANSWER, to=[J2],
            reply_to=question.id, via=MessageVia.GUI,
        )  # fmt: skip

    scripts = {
        J1: turns([done("major")], [done("geprüft")]),
        J2: turns([ASK, done("major")], [post("judges", "@judge-1 prüfst du n ≥ 1?"), done()]),
    }
    manager, outcome, _ = await judges(workfolder, scripts, mode="event", during=user)
    j1 = [e for e in _events(manager, J1) if e["kind"] == "turn_start"]
    if old:
        assert len(j1) == 1, "W4-06.1: the request to judge-1 waits"
        return
    assert len(j1) == 2, "the request of the asker woke judge-1"
    assert outcome.ended_by is DiscussionEnd.ALL_DONE


async def test_a_question_in_another_channel_does_not_hold(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4 (c): only questions of the phase's own channel hold it."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 30.0)
    other = post("andere", "@user Frage dort?", kind="question")
    scripts = {J1: turns([done("major")]), J2: turns([other, done("major")])}
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("judges", "Besprecht.", rounds=1))

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[
            channel("judges", [J1, J2], mode="rounds"),
            channel("andere", [J1, J2], mode="rounds"),
        ],
        run_extra={"limits": {"wall_time": "60s"}},
    )
    started = time.monotonic()
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.DONE
    assert time.monotonic() - started < 5, "the question in #andere did not hold #judges"
    assert results[0].ended_by is DiscussionEnd.ALL_DONE
    assert waits(manager) == []


async def test_a_stop_while_the_phase_waits_ends_at_once(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop and Not-Aus are not held up: a stop during the wait ends the phase and the run at
    once; the question closes as stopped."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 60.0)
    stopped_at: list[float] = []

    async def stopper(orch: Orchestrator) -> None:
        await eventually(lambda: "user" in waits_of(orch), within=10)
        stopped_at.append(time.monotonic())
        manager_of[0].stop()

    manager_of: list[Any] = []
    scripts = {J1: turns([done("major")]), J2: turns([ASK, done("major")])}
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        helper = asyncio.create_task(stopper(ctx))
        results.append(await ctx.discuss("judges", "Besprecht.", rounds=1))
        await helper

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("judges", [J1, J2], mode="rounds")],
        run_extra={"limits": {"wall_time": "120s"}},
    )
    manager_of.append(manager)
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.STOPPED
    assert time.monotonic() - stopped_at[0] < 3, "the stop ended the wait at once"
    assert results[0].ended_by is DiscussionEnd.STOPPED
    assert question_item(manager).status is ApprovalStatus.OBSOLETE


async def test_without_a_question_nothing_waits(workfolder: Path) -> None:
    """Only an open question to the user holds; a plain `done` ends the phase as before."""
    scripts = {J1: turns([done("major")]), J2: turns([wait(0.1), done("major")])}
    manager, outcome, took = await judges(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 5
    assert waits(manager) == []


def waits_of(orch: Orchestrator) -> list[str]:
    return [e["data"]["reason"] for e in log_entries(orch.prepared.run_dir, "waiting")
            if e["data"].get("channel") == "judges"]  # fmt: skip


def _events(manager: Any, agent_id: str) -> list[dict[str, Any]]:
    from run_kit import events

    return events(manager.run_dir, agent_id)

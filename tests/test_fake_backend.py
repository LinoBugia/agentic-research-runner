"""FakeBackend (W1-04 A3, A4): all step kinds, room calls through RoomAPI, interrupt, hints."""

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import AgentBackend, AgentEvent, EventKind
from arr.backends.fake import ROOM_TOOL_PREFIX, FakeBackend, FakeScript, load_script
from arr.errors import ArrError
from arr.models import AgentConfig, AgentStatus
from arr.resolve import ConfigError
from arr.room.types import (
    Assignment,
    ChannelInfo,
    Inbox,
    Message,
    MessageKind,
    Rating,
    Result,
    RoomError,
    Vote,
    VoteChoice,
    VoteRule,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fake"


class StubRoom:
    """In-memory `RoomAPI` for one agent; records every call as `(method, kwargs)`."""

    def __init__(self, agent_id: str, *, open_questions: list[Message] | None = None) -> None:
        self._agent_id = agent_id
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages: list[Message] = []
        self.results: list[Result] = []
        self.ratings: list[Rating] = []
        self.votes: list[Vote] = []
        self.status: tuple[AgentStatus, str] | None = None
        self.open_questions = open_questions or []
        self.assignment: Assignment | None = None
        self.fail_next: str | None = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def _record(self, method: str, /, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            raise RoomError(message)

    def inbox(self) -> Inbox:
        self._record("inbox")
        return Inbox(open_questions=self.open_questions, unread={"main": 2})

    def list_channels(self) -> list[ChannelInfo]:
        self._record("list_channels")
        return []

    def read_messages(
        self,
        channel: str | None = None,
        only_new: bool = True,
        limit: int = 50,
        thread: int | None = None,
    ) -> list[Message]:
        self._record("read_messages", channel=channel, only_new=only_new, limit=limit)
        return list(self.messages)

    def post_message(
        self,
        channel: str,
        body: str,
        kind: MessageKind = MessageKind.NOTE,
        to: list[str] | None = None,
        reply_to: int | None = None,
        refs: list[str] | None = None,
    ) -> Message:
        self._record(
            "post_message", channel=channel, body=body, kind=kind, to=to, reply_to=reply_to
        )
        message = Message(
            id=len(self.messages) + 1,
            ts=1.0,
            channel=channel,
            sender=self._agent_id,
            kind=kind,
            body=body,
            to=to or [],
            reply_to=reply_to,
            open=kind is MessageKind.QUESTION and bool(to),
        )
        self.messages.append(message)
        return message

    def get_attachment(self, msg_id: int, name: str) -> Path:
        self._record("get_attachment", msg_id=msg_id, name=name)
        return Path("/nonexistent") / name

    def vote(self, proposal_id: int, choice: VoteChoice, reason: str = "") -> Vote:
        self._record("vote", proposal_id=proposal_id, choice=choice, reason=reason)
        vote = Vote(
            proposal_id=proposal_id, agent=self._agent_id, choice=choice, reason=reason, ts=1.0
        )
        self.votes.append(vote)
        return vote

    def set_status(self, status: AgentStatus, note: str = "") -> None:
        self._record("set_status", status=status, note=note)
        self.status = (status, note)

    def submit_result(
        self, kind: str, title: str, body: str | None = None, path: str | None = None
    ) -> Result:
        self._record("submit_result", kind=kind, title=title, body=body, path=path)
        result = Result(
            id=len(self.results) + 1,
            ts=1.0,
            agent=self._agent_id,
            kind=kind,
            title=title,
            body=body,
        )
        self.results.append(result)
        return result

    def submit_rating(
        self,
        result_id: int,
        score: float,
        comment: str,
        criteria: dict[str, float] | None = None,
    ) -> Rating:
        self._record(
            "submit_rating", result_id=result_id, score=score, comment=comment, criteria=criteria
        )
        rating = Rating(
            id=len(self.ratings) + 1,
            ts=1.0,
            rater=self._agent_id,
            result_id=result_id,
            score=score,
            criteria=criteria or {},
            comment=comment,
        )
        self.ratings.append(rating)
        return rating

    def get_assignment(self) -> Assignment | None:
        self._record("get_assignment")
        return self.assignment

    def give_floor(self, agent: str, prompt: str, channel: str | None = None) -> None:
        self._record("give_floor", agent=agent, prompt=prompt, channel=channel)

    def call_vote(self, proposal_id: int, rule: VoteRule = VoteRule.MAJORITY) -> None:
        self._record("call_vote", proposal_id=proposal_id, rule=rule)

    def pin(self, msg_id: int) -> None:
        self._record("pin", msg_id=msg_id)

    def close_discussion(self, decision: str, channel: str | None = None) -> Message:
        self._record("close_discussion", decision=decision, channel=channel)
        return self.post_message(channel or "main", decision, MessageKind.DECISION)


def make_config(agent_id: str = "explorer-1", provider: str = "claude") -> AgentConfig:
    return AgentConfig(
        id=agent_id, base_id=agent_id.split("-")[0], provider=provider, model="fake-model", auth="p"
    )


async def play(
    backend: FakeBackend, prompt: str = "los", stage: str | None = None
) -> list[AgentEvent]:
    return [event async for event in backend.run_turn(prompt, stage=stage)]


async def started(
    script: Any,
    room: StubRoom | None = None,
    *,
    provider: str = "claude",
    agent: str = "explorer-1",
) -> FakeBackend:
    backend = FakeBackend(make_config(agent, provider), script, run_id="run-1", room=room)
    await backend.start()
    return backend


def kinds(events: list[AgentEvent]) -> list[str]:
    return [str(e.kind) for e in events]


# --------------------------------------------------------------------------------------------
# Protocol, events, plain steps
# --------------------------------------------------------------------------------------------


async def test_fake_backend_implements_the_protocol() -> None:
    backend = await started({"turns": []})
    assert isinstance(backend, AgentBackend)
    assert backend.config.id == "explorer-1"


async def test_turn_always_starts_with_turn_start_and_ends_with_turn_end() -> None:
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "thinking", "text": "denke"},
                        {"do": "text", "text": "antwort"},
                    ]
                }
            ]
        }
    )
    events = await play(backend, "Mach X", stage="explore")
    assert kinds(events) == ["turn_start", "thinking", "text", "turn_end"]
    assert events[0].data == {"prompt": "Mach X", "stage": "explore"}
    assert events[1].data == {"text": "denke", "summarized": True}
    assert events[3].data["status"] == "ok"
    assert {(e.run_id, e.agent_id, e.turn) for e in events} == {("run-1", "explorer-1", 1)}
    assert all(e.seq == 0 for e in events)  # seq is the EventLogger's job
    json.dumps([e.to_record() for e in events])  # normalized records are JSON-serializable


async def test_turn_numbers_count_up_and_script_entries_are_played_in_order() -> None:
    backend = await started(
        {
            "turns": [
                {"steps": [{"do": "text", "text": "eins"}]},
                {"steps": [{"do": "text", "text": "zwei"}]},
            ]
        }
    )
    first, second = await play(backend), await play(backend)
    assert [e.turn for e in first + second] == [1, 1, 1, 2, 2, 2]
    assert second[1].data["text"] == "zwei"


async def test_script_exhausted_gives_error_then_turn_end_error() -> None:
    backend = await started({"turns": [{"steps": []}]})
    await play(backend)
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert "keinen Eintrag für Turn 2" in events[1].data["message"]
    assert events[1].data["recoverable"] is False
    assert events[2].data["status"] == "error"


def texts(events: list[AgentEvent]) -> list[str]:
    return [e.data["text"] for e in events if e.kind is EventKind.TEXT]


async def test_by_stage_picks_turns_per_stage_and_keeps_the_main_order() -> None:
    def turn(text: str) -> dict[str, Any]:
        return {"steps": [{"do": "text", "text": text}]}

    backend = await started(
        {
            "turns": [turn("haupt 1"), turn("haupt 2")],
            "by_stage": {
                "discussion": {"repeat_last": True, "turns": [turn("weck 1"), turn("weck n")]},
                "review": {"turns": [turn("review 1")]},
            },
        }
    )
    played = []
    for stage in ("explore", "discussion", "discussion", "discussion", None, "review"):
        played += texts(await play(backend, stage=stage))
    assert played == ["haupt 1", "weck 1", "weck n", "weck n", "haupt 2", "review 1"]

    events = await play(backend, stage="review")
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert "keinen Eintrag für Turn 2 in Stufe „review“" in events[1].data["message"]
    assert events[-1].turn == 7, "turn numbers count all turns"


async def test_repeat_last_plays_the_last_turn_again() -> None:
    backend = await started(
        {"repeat_last": True, "turns": [{"steps": [{"do": "text", "text": "immer"}]}]}
    )
    for _ in range(3):
        assert kinds(await play(backend)) == ["turn_start", "text", "turn_end"]


async def test_events_carry_the_step_as_raw() -> None:
    backend = await started({"turns": [{"steps": [{"do": "text", "text": "a"}]}]})
    events = await play(backend)
    assert events[1].raw == {"fake_step": {"do": "text", "text": "a"}}
    assert events[0].raw is None


async def test_unrecoverable_error_ends_the_turn_with_status_error() -> None:
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "error", "message": "kaputt", "type": "boom"},
                        {"do": "text", "text": "kommt nicht mehr"},
                    ]
                }
            ]
        }
    )
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert events[1].data == {"message": "kaputt", "type": "boom", "recoverable": False}
    assert events[2].data["status"] == "error"


async def test_recoverable_error_lets_the_turn_continue() -> None:
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "error", "message": "lahm", "recoverable": True},
                        {"do": "text", "text": "weiter"},
                    ]
                }
            ]
        }
    )
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "text", "turn_end"]
    assert events[-1].data["status"] == "ok"


async def test_lifecycle_rules() -> None:
    backend = FakeBackend(make_config(), {"turns": [{"steps": []}]})
    with pytest.raises(RuntimeError, match="start"):
        await play(backend)
    await backend.start()
    assert backend.session_ref() == {"session_id": "fake-explorer-1"}
    await backend.close()
    await backend.close()
    with pytest.raises(RuntimeError):
        await play(backend)
    codex = FakeBackend(make_config("c-1", "codex"), {"turns": []})
    assert codex.session_ref() == {}
    await codex.start()
    assert codex.session_ref() == {"thread_id": "fake-c-1"}


async def test_second_turn_while_one_runs_is_rejected() -> None:
    backend = await started({"turns": [{"steps": [{"do": "delay", "seconds": 5}]}] * 2})
    stream = backend.run_turn("a")
    await anext(stream)
    with pytest.raises(RuntimeError, match="läuft schon"):
        await anext(backend.run_turn("b"))
    await backend.interrupt()
    assert [e.kind async for e in stream][-1] is EventKind.TURN_END


# --------------------------------------------------------------------------------------------
# A3: all step kinds, room calls
# --------------------------------------------------------------------------------------------


async def test_all_step_kinds_script_plays_and_calls_the_room() -> None:
    room = StubRoom("explorer-1")
    backend = await started(FIXTURES / "all_steps.yaml", room)
    events = await play(backend, "alles")

    assert kinds(events) == [
        "turn_start",
        "thinking",
        "text",
        "tool_call",  # Read
        "tool_result",
        "tool_call",  # Bash, failing
        "tool_result",
        *["tool_call", "tool_result"]
        * 7,  # 3x post_message, submit_result, submit_rating, vote, set_status
        "rate_limit",
        "usage",
        "error",
        "text",
        "turn_end",
    ]
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert [r.data["is_error"] for r in results[:2]] == [False, True]

    # room calls reached the RoomAPI with the right arguments
    assert [name for name, _ in room.calls] == [
        "post_message",
        "post_message",
        "post_message",
        "submit_result",
        "submit_rating",
        "vote",
        "set_status",
    ]
    idea, question, answer = room.messages
    assert (idea.kind, idea.body) == (MessageKind.IDEA, "Idee A")
    assert (question.kind, question.to, question.open) == (MessageKind.QUESTION, ["critic-1"], True)
    assert (answer.kind, answer.reply_to) == (MessageKind.ANSWER, idea.id)  # ${idea.id} resolved
    assert room.results[0].title == "Idee A"
    rating = room.ratings[0]
    assert (rating.result_id, rating.score, rating.criteria) == (1, 8, {"clarity": 9})
    assert room.votes[0].choice is VoteChoice.NO and room.votes[0].reason == "zu vage"
    assert room.status == (AgentStatus.WORKING, "rechnet")
    # enum arguments arrive as enums, not as bare strings
    assert isinstance(room.calls[0][1]["kind"], MessageKind)
    assert isinstance(room.calls[5][1]["choice"], VoteChoice)

    # room actions show up as tool calls of the room MCP server, with the result as JSON
    call = next(e for e in events if e.data.get("name") == ROOM_TOOL_PREFIX + "post_message")
    assert call.data["input"]["body"] == "Idee A"
    posted = json.loads(next(r for r in results if r.data["id"] == call.data["id"]).data["content"])
    assert posted["id"] == 1 and posted["kind"] == "idea"
    set_status_result = results[-1]
    assert set_status_result.data["content"] == "ok"  # None result

    rate = next(e for e in events if e.kind is EventKind.RATE_LIMIT)
    assert rate.data == {"status": "allowed_warning", "utilization": 0.82, "resets_at": 1761240000}
    end = events[-1].data
    assert end["status"] == "ok"
    assert end["usage"] == {
        "input": 1000, "output": 200, "cache_read": None, "cache_write": None,
        "reasoning": None, "model": "fake-model",
    }  # fmt: skip
    assert (end["cost_usd"], end["cost_kind"], end["session"]) == (
        0.05,
        "billed",
        "fake-explorer-1",
    )
    assert end["num_turns"] == 1 + 9  # 2 plain tools + 7 room calls


async def test_tool_ids_pair_call_and_result() -> None:
    backend = await started(FIXTURES / "all_steps.yaml", StubRoom("explorer-1"))
    events = await play(backend)
    calls = [e.data["id"] for e in events if e.kind is EventKind.TOOL_CALL]
    results = [e.data["id"] for e in events if e.kind is EventKind.TOOL_RESULT]
    assert calls == results and len(set(calls)) == len(calls)


async def test_room_error_becomes_failed_tool_result_and_turn_goes_on() -> None:
    room = StubRoom("explorer-1")
    room.fail_next = "Du bist kein Mitglied des Kanals „main“."
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {
                            "do": "room",
                            "action": "post_message",
                            "args": {"channel": "main", "body": "x"},
                        },
                        {"do": "text", "text": "gut"},
                    ]
                }
            ]
        },
        room,
    )
    events = await play(backend)
    assert kinds(events) == ["turn_start", "tool_call", "tool_result", "text", "turn_end"]
    assert events[2].data["is_error"] is True
    assert events[2].data["content"] == "Du bist kein Mitglied des Kanals „main“."
    assert events[-1].data["status"] == "ok"


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"do": "room", "action": "pin", "args": {"msg": 1}}, "ungültige Argumente"),
        (
            {"do": "room", "action": "vote", "args": {"proposal_id": 1, "choice": "vielleicht"}},
            "kein gültiger Wert",
        ),
        (
            {"do": "room", "action": "pin", "args": {"msg_id": "${gibt_es_nicht.id}"}},
            "save_as",
        ),
    ],
)
async def test_script_bugs_end_the_turn_with_error_and_turn_end(
    step: dict[str, Any], message: str
) -> None:
    backend = await started(
        {"turns": [{"steps": [step, {"do": "text", "text": "nie"}]}]}, StubRoom("x")
    )
    events = await play(backend)
    assert kinds(events)[-2:] == ["error", "turn_end"]
    assert message in events[-2].data["message"]
    assert events[-1].data["status"] == "error"


async def test_room_step_without_room_is_a_clear_error() -> None:
    backend = await started({"turns": [{"steps": [{"do": "room", "action": "inbox"}]}]})
    events = await play(backend)
    assert "kein RoomAPI" in events[-2].data["message"]
    assert events[-1].data["status"] == "error"


async def test_references_walk_dicts_and_lists() -> None:
    room = StubRoom(
        "critic-1",
        open_questions=[
            Message(
                id=7,
                ts=1.0,
                channel="main",
                sender="explorer-1",
                body="?",
                kind=MessageKind.QUESTION,
            )
        ],
    )
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "room", "action": "inbox", "save_as": "inbox"},
                        {
                            "do": "room",
                            "action": "post_message",
                            "args": {
                                "channel": "main",
                                "kind": "answer",
                                "body": "ja",
                                "reply_to": "${inbox.open_questions.0.id}",
                            },
                        },
                    ]
                }
            ]
        },
        room,
        agent="critic-1",
    )
    await play(backend)
    assert room.messages[0].reply_to == 7


# --------------------------------------------------------------------------------------------
# Example scripts for the roles
# --------------------------------------------------------------------------------------------


async def test_explorer_script_posts_an_idea_submits_a_result_and_answers_later() -> None:
    room = StubRoom("explorer-1")
    backend = await started(FIXTURES / "explorer.yaml", room)
    first = await play(backend, "Stufe 1")
    assert [m.kind for m in room.messages] == [MessageKind.IDEA]
    assert room.results[0].kind == "idea"
    assert room.status == (AgentStatus.DONE, "Idee abgegeben")
    assert first[-1].data["cost_kind"] == "estimated"

    room.open_questions = [
        Message(
            id=5, ts=1.0, channel="main", sender="critic-1", body="?", kind=MessageKind.QUESTION
        )
    ]
    await play(backend, "Antworte")
    assert room.messages[-1].kind is MessageKind.ANSWER and room.messages[-1].reply_to == 5


async def test_critic_script_objects_and_asks() -> None:
    room = StubRoom("critic-1")
    backend = await started(FIXTURES / "critic.yaml", room, agent="critic-1")
    await play(backend)
    assert [m.kind for m in room.messages] == [MessageKind.OBJECTION, MessageKind.QUESTION]
    assert room.messages[1].to == ["explorer-1"] and room.messages[1].open


async def test_rater_script_rates_the_result_from_the_assignment_context() -> None:
    room = StubRoom("judge-1")
    room.assignment = Assignment(
        agent="judge-1",
        turn=1,
        prompt="Bewerte",
        context={"result_ids": [4], "proposal_id": 9},
        ts=1.0,
    )
    backend = await started(FIXTURES / "rater.yaml", room, agent="judge-1")
    await play(backend)
    assert room.ratings[0].result_id == 4 and room.ratings[0].score == 7.5
    assert room.votes[0].proposal_id == 9 and room.votes[0].choice is VoteChoice.YES


# --------------------------------------------------------------------------------------------
# Script loading
# --------------------------------------------------------------------------------------------


def test_scripts_load_from_yaml_dict_and_model() -> None:
    from_file = load_script(FIXTURES / "explorer.yaml")
    from_dict = load_script({"turns": [{"steps": [{"do": "text", "text": "a"}]}]})
    assert len(from_file.turns) == 2 and len(from_dict.turns) == 1
    assert load_script(from_dict) is from_dict
    assert isinstance(from_file, FakeScript)


def test_invalid_script_gives_german_error_with_path() -> None:
    with pytest.raises(ConfigError) as info:
        load_script({"turns": [{"steps": [{"do": "text", "txt": "a"}]}]})
    assert "turns[0].steps[0]" in str(info.value) and "text" in str(info.value)
    with pytest.raises(ArrError):
        load_script({"turns": [{"steps": [{"do": "tanzen"}]}]})


# --------------------------------------------------------------------------------------------
# A4: interrupt
# --------------------------------------------------------------------------------------------


async def test_interrupt_ends_a_running_turn_within_one_second() -> None:
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "text", "text": "fange an"},
                        {"do": "delay", "seconds": 30},
                        {"do": "text", "text": "kommt nie"},
                    ]
                }
            ]
        }
    )
    events: list[AgentEvent] = []
    text_seen = asyncio.Event()

    async def consume() -> None:
        async for event in backend.run_turn("lang"):
            events.append(event)
            if event.kind is EventKind.TEXT:
                text_seen.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(text_seen.wait(), timeout=1)
    started_at = time.monotonic()
    await backend.interrupt()
    await asyncio.wait_for(task, timeout=1)
    assert time.monotonic() - started_at < 1
    assert kinds(events) == ["turn_start", "text", "turn_end"]
    assert events[-1].data["status"] == "interrupted"


async def test_interrupt_during_slow_tool_gives_call_without_result() -> None:
    backend = await started(
        {"turns": [{"steps": [{"do": "tool", "name": "Bash", "duration_s": 30}]}]}
    )
    stream = backend.run_turn("x")
    seen = [await anext(stream), await anext(stream)]  # turn_start, tool_call
    await backend.interrupt()
    seen += [event async for event in stream]
    assert kinds(seen) == ["turn_start", "tool_call", "turn_end"]
    assert seen[-1].data["status"] == "interrupted"


async def test_interrupt_when_idle_is_a_noop_and_does_not_leak_into_the_next_turn() -> None:
    backend = await started(
        {"repeat_last": True, "turns": [{"steps": [{"do": "text", "text": "a"}]}]}
    )
    await backend.interrupt()  # idle
    events = await play(backend)
    assert kinds(events) == ["turn_start", "text", "turn_end"]
    assert events[-1].data["status"] == "ok"
    await backend.interrupt()  # idle again, after the turn
    assert (await play(backend))[-1].data["status"] == "ok"


async def test_interrupt_before_first_step_still_ends_with_turn_end() -> None:
    backend = await started({"turns": [{"steps": [{"do": "text", "text": "a"}]}]})
    stream = backend.run_turn("x")
    first = await anext(stream)  # turn_start
    await backend.interrupt()
    rest = [event async for event in stream]
    assert kinds([first, *rest]) == ["turn_start", "turn_end"]
    assert rest[-1].data["status"] == "interrupted"


async def test_close_interrupts_a_running_turn() -> None:
    backend = await started({"turns": [{"steps": [{"do": "delay", "seconds": 30}]}]})
    stream = backend.run_turn("x")
    await anext(stream)
    await backend.close()
    assert [e async for e in stream][-1].data["status"] == "interrupted"


async def test_abandoned_turn_frees_the_backend() -> None:
    backend = await started(
        {"repeat_last": True, "turns": [{"steps": [{"do": "text", "text": "a"}]}]}
    )
    stream = backend.run_turn("x")
    await anext(stream)
    await stream.aclose()
    assert (await play(backend))[-1].data["status"] == "ok"


# --------------------------------------------------------------------------------------------
# Hints
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("provider", "method"), [("claude", "post_tool_use"), ("codex", "steer")])
async def test_hint_during_a_turn_becomes_a_hint_event(provider: str, method: str) -> None:
    backend = await started(
        {"turns": [{"steps": [{"do": "delay", "seconds": 0.3}, {"do": "text", "text": "danach"}]}]},
        provider=provider,
    )
    events: list[AgentEvent] = []

    async def consume() -> None:
        async for event in backend.run_turn("x"):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    assert await backend.deliver_hint("1 Frage an dich") is True
    await task
    assert kinds(events) == ["turn_start", "hint", "text", "turn_end"]
    assert events[1].data == {"text": "1 Frage an dich", "channel": None, "method": method}


async def test_hint_when_idle_is_refused() -> None:
    backend = await started({"turns": [{"steps": []}]})
    assert await backend.deliver_hint("x") is False
    await play(backend)
    assert await backend.deliver_hint("x") is False


# --------------------------------------------------------------------------------------------
# Usage in turn_end
# --------------------------------------------------------------------------------------------


async def test_turn_end_sums_usage_steps_and_cost_kinds() -> None:
    backend = await started(
        {
            "cost_kind": "estimated",
            "turns": [
                {
                    "steps": [
                        {"do": "usage", "input": 100, "output": 10, "cost_usd": 0.5},
                        {
                            "do": "usage",
                            "input": 50,
                            "cache_read": 7,
                            "cost_usd": 0.25,
                            "cost_kind": "billed",
                        },
                    ]
                },
                {"steps": [{"do": "usage", "input": 5, "model": "andere-gpt"}]},
                {"steps": []},
            ],
        }
    )
    summed, no_cost, empty = (
        (await play(backend))[-1],
        (await play(backend))[-1],
        (await play(backend))[-1],
    )
    assert summed.data["usage"] == {
        "input": 150, "output": 10, "cache_read": 7, "cache_write": None,
        "reasoning": None, "model": "fake-model",
    }  # fmt: skip
    assert summed.data["cost_usd"] == 0.75 and summed.data["cost_kind"] == "estimated"  # mixed
    assert no_cost.data["cost_usd"] is None and no_cost.data["cost_kind"] is None
    assert no_cost.data["usage"]["model"] == "andere-gpt"
    assert empty.data["usage"]["input"] is None and empty.data["num_turns"] == 1


async def test_usage_events_have_tokens_and_model_but_no_cost() -> None:
    backend = await started({"turns": [{"steps": [{"do": "usage", "input": 3, "cost_usd": 1}]}]})
    usage_event = (await play(backend))[1]
    assert usage_event.kind is EventKind.USAGE
    assert usage_event.data == {
        "input": 3, "output": None, "cache_read": None, "cache_write": None,
        "reasoning": None, "model": "fake-model",
    }  # fmt: skip


# --------------------------------------------------------------------------------------------
# W1-04.1
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type"), [("ok", None), ("limit", "limit"), ("error", "fake_error")]
)
async def test_end_step_ends_the_turn_with_the_given_status(
    status: str, error_type: str | None
) -> None:
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "text", "text": "vorher"},
                        {"do": "end", "status": status, "message": "Budget erreicht"},
                        {"do": "text", "text": "nie"},
                    ]
                }
            ]
        }
    )
    events = await play(backend)
    if error_type is None:
        assert kinds(events) == ["turn_start", "text", "turn_end"]
    else:
        assert kinds(events) == ["turn_start", "text", "error", "turn_end"]
        assert events[2].data == {
            "message": "Budget erreicht",
            "type": error_type,
            "recoverable": False,
        }
    assert events[-1].data["status"] == status


async def test_end_step_without_message_uses_a_default() -> None:
    backend = await started({"turns": [{"steps": [{"do": "end", "status": "limit"}]}]})
    events = await play(backend)
    assert events[1].data["message"] == "Limit erreicht"
    assert events[-1].data["status"] == "limit"


def test_end_step_rejects_other_statuses() -> None:
    with pytest.raises(ConfigError):
        load_script({"turns": [{"steps": [{"do": "end", "status": "interrupted"}]}]})


async def test_leaving_a_turn_with_break_does_not_block_the_next_turn() -> None:
    backend = await started(
        {
            "turns": [
                {"steps": [{"do": "text", "text": "a"}, {"do": "delay", "seconds": 30}]},
                {"steps": [{"do": "text", "text": "b"}]},
            ],
        }
    )
    async for event in backend.run_turn("erster"):
        if event.kind is EventKind.TEXT:
            break  # no aclose(): the iterator is simply dropped
    events = await asyncio.wait_for(play(backend, "zweiter"), timeout=1)  # no "läuft schon"
    assert kinds(events) == ["turn_start", "text", "turn_end"]
    assert events[0].turn == 2 and events[1].data["text"] == "b"
    assert events[-1].data["status"] == "ok"
    await asyncio.sleep(0.01)  # the loop finalizes the abandoned generator meanwhile
    assert await backend.deliver_hint("x") is False  # its late cleanup left the state idle


async def test_a_live_but_paused_turn_still_blocks_a_second_one() -> None:
    backend = await started({"turns": [{"steps": [{"do": "text", "text": "a"}]}] * 2})
    stream = backend.run_turn("a")  # still referenced: not abandoned
    await anext(stream)
    with pytest.raises(RuntimeError, match="läuft schon"):
        await anext(backend.run_turn("b"))
    await stream.aclose()


async def test_aclosing_ends_an_early_left_turn() -> None:
    backend = await started(
        {"repeat_last": True, "turns": [{"steps": [{"do": "text", "text": "a"}]}] * 1}
    )
    async with contextlib.aclosing(backend.run_turn("x")) as stream:
        async for _ in stream:
            break
    assert await backend.deliver_hint("x") is False  # no turn runs any more
    assert (await play(backend))[-1].data["status"] == "ok"


async def test_hang_ignores_interrupt_until_its_time_is_up() -> None:
    backend = await started(
        {
            "turns": [
                {
                    "steps": [
                        {"do": "hang", "seconds": 0.3},
                        {"do": "text", "text": "kommt nicht mehr"},
                    ]
                }
            ]
        }
    )
    stream = backend.run_turn("x")
    await anext(stream)  # turn_start
    started_at = time.monotonic()
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.05)
    await backend.interrupt()
    with pytest.raises(TimeoutError):  # interrupt() does not cut the hang short
        await asyncio.wait_for(asyncio.shield(next_event), timeout=0.1)
    end = await next_event
    assert time.monotonic() - started_at >= 0.3
    assert end.kind is EventKind.TURN_END and end.data["status"] == "interrupted"


async def test_moderator_and_reading_actions_run_against_the_room() -> None:
    room = StubRoom("mod-1")
    room.messages.append(
        Message(
            id=1, ts=1.0, channel="main", sender="explorer-1", body="Vorschlag", kind="proposal"
        )
    )
    steps = [
        {"do": "room", "action": "inbox", "save_as": "inbox"},
        {"do": "room", "action": "list_channels"},
        {"do": "room", "action": "get_attachment", "args": {"msg_id": 1, "name": "beweis.pdf"}},
        {"do": "room", "action": "give_floor", "args": {"agent": "explorer-1", "prompt": "Bitte"}},
        {"do": "room", "action": "call_vote", "args": {"proposal_id": 1, "rule": "unanimous"}},
        {"do": "room", "action": "pin", "args": {"msg_id": 1}},
        {
            "do": "room",
            "action": "close_discussion",
            "args": {"decision": "Wir nehmen Idee A.", "channel": "main"},
            "save_as": "decision",
        },
    ]
    backend = await started({"turns": [{"steps": steps}]}, room, agent="mod-1")
    events = await play(backend)

    assert [name for name, _ in room.calls] == [
        "inbox",
        "list_channels",
        "get_attachment",
        "give_floor",
        "call_vote",
        "pin",
        "close_discussion",
        "post_message",  # the stub posts the decision
    ]
    results = [e for e in events if e.kind is EventKind.TOOL_RESULT]
    assert len(results) == 7 and not any(r.data["is_error"] for r in results)
    assert json.loads(results[0].data["content"])["unread"] == {"main": 2}
    assert json.loads(results[1].data["content"]) == []
    assert json.loads(results[2].data["content"]).endswith("beweis.pdf")  # Path → str
    assert [r.data["content"] for r in results[3:6]] == ["ok", "ok", "ok"]
    assert json.loads(results[6].data["content"])["kind"] == "decision"
    assert room.calls[3][1] == {"agent": "explorer-1", "prompt": "Bitte", "channel": None}
    assert room.calls[4][1]["rule"] is VoteRule.UNANIMOUS
    assert room.calls[5][1] == {"msg_id": 1}
    assert events[-1].data["status"] == "ok"

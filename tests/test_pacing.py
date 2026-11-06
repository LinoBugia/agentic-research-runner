"""Chat pacing (W2-03): inbox at turn start, hints mid-turn, `rounds`, `event`, ping-pong,
`open_from`, M1 criterion 6. FakeBackend agents with the real room."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    FAST,
    channel,
    done,
    events,
    eventually,
    log_entries,
    make_manager,
    make_workfolder,
    post,
    room,
    say,
    turns,
    wait,
)

from arr.models import AgentStatus, RunStatus
from arr.room.types import DeliveryStatus, Inbox, Message, MessageKind, MessageVia
from arr.runs.context import DiscussionEnd, DiscussionOutcome, RunContext
from arr.runs.orchestrator import Orchestrator
from arr.runs.pacing import (
    HintGate,
    PacingSettings,
    PingPongTracker,
    compose_hint,
    render_turn_prompt,
)


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def msg(id: int, sender: str, body: str = "x", **fields: Any) -> Message:
    return Message(
        id=id, ts=0.0, channel=fields.pop("channel", "main"), sender=sender, body=body, **fields
    )


def prompts(run_dir: Path, agent_id: str) -> list[str]:
    return [e["data"]["prompt"] for e in events(run_dir, agent_id) if e["kind"] == "turn_start"]


def hints(run_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    return [e for e in events(run_dir, agent_id) if e["kind"] == "hint"]


def orch(ctx: RunContext) -> Orchestrator:
    assert isinstance(ctx, Orchestrator)
    return ctx


# --------------------------------------------------------------------------------------------
# Units: inbox, hint text, hint gate, ping-pong
# --------------------------------------------------------------------------------------------


def test_render_turn_prompt_lists_questions_user_messages_and_counts() -> None:
    question = msg(
        4, "critic", "Gilt das für n = 9?", kind=MessageKind.QUESTION, to=["e1"], open=True
    )
    user_question = msg(6, "user", "Und n = 27?", kind=MessageKind.QUESTION, to=["e1"], open=True)
    user_note = msg(7, "user", "Bitte zuerst n ≤ 100 prüfen.")
    inbox = Inbox(open_questions=[question, user_question], unread={"main": 4, "side": 0})
    text = render_turn_prompt("e1", "Arbeite weiter.", inbox, [user_question, user_note])
    assert text.startswith("## Posteingang\n\nOffene Fragen an dich (beantworte sie zuerst):")
    assert text.index("★ Vom User: [#6 user · question · an dich · #main]") < text.index(
        "[#4 critic · question · an dich · #main]\nGilt das für n = 9?"
    )
    assert "★ Nachrichten vom User (haben Vorrang):\n\n[#7 user · note · #main]" in text
    assert text.count("Und n = 27?") == 1, "a user question is listed once"
    assert "Neue Nachrichten: #main 4 (davon 2 vom User), #side 0" in text
    assert text.endswith("## Auftrag\n\nArbeite weiter.")
    assert render_turn_prompt("e1", "Nur das.", None) == "Nur das."
    empty = render_turn_prompt("e1", "x", Inbox(unread={"main": 0}))
    assert "Offene Fragen an dich: keine" in empty and "read_messages" not in empty


def test_compose_hint_counts_without_naming_senders() -> None:
    text = compose_hint(
        "e1",
        [
            msg(1, "critic", kind=MessageKind.QUESTION, to=["e1"]),
            msg(2, "e2"),
            msg(3, "user", channel="side"),
        ],
    )
    assert text == (
        "📨 1 Frage an dich in #main · 1 neue Nachricht in #main · 1 neue Nachricht in #side "
        "(davon 1 vom User in #side). Lies sie mit inbox() oder read_messages()."
    )
    assert "critic" not in text


def test_hint_gate_one_per_interval_or_per_tool_calls() -> None:
    now = [0.0]
    gate = HintGate(interval_s=60, tool_calls=5, clock=lambda: now[0])
    assert gate.allows(0)
    gate.delivered(0)
    assert not gate.allows(4)
    assert gate.allows(5), "5 tool calls since the last hint"
    gate.delivered(5)
    now[0] = 59.9
    assert not gate.allows(6)
    now[0] = 65.0
    assert gate.allows(6), "60 s since the last hint"


def test_ping_pong_tracker() -> None:
    senders = {1: "a"}
    tracker = PingPongTracker(2, senders.get)
    assert tracker.observe(msg(2, "b", reply_to=1)) is None  # b → a (reply)
    assert tracker.observe(msg(3, "a", to=["b"])) is None
    assert tracker.observe(msg(4, "b", kind=MessageKind.CLAIM, to=["a"])) is None  # progress
    assert tracker.observe(msg(5, "a", to=["b"])) is None
    assert tracker.observe(msg(6, "b", to=["a"])) is None
    assert tracker.observe(msg(7, "a", to=["b"])) == frozenset({"a", "b"})
    assert tracker.observe(msg(8, "b", to=["a"])) is None, "flagged only once"
    assert tracker.observe(msg(9, "c", to=["a", "b"])) is None, "not to one agent"
    assert tracker.observe(msg(10, "user", to=["a"])) is None


# --------------------------------------------------------------------------------------------
# A7 inbox at turn start, open_from
# --------------------------------------------------------------------------------------------


async def test_turn_prompt_has_the_inbox_and_user_messages_stand_out(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("b", "Frag a.")
        rooms = orch(ctx).room
        rooms.post("main", "Bitte zuerst n ≤ 100 prüfen.", sender="user", via=MessageVia.GUI)
        rooms.post(
            "main", "Und n = 27?", sender="user", via=MessageVia.GUI, kind="question", to=["a"]
        )
        await ctx.turn("a", "Arbeite.")
        await ctx.turn("c", "Ohne Kanal.")

    scripts = {
        "a": turns([say("ok")]),
        "b": turns(
            [
                post("main", "Gilt das für n = 9?", kind="question", to=["a"]),
                post("main", "Zwischenstand: Lemma A"),
            ]
        ),
        "c": turns([say("ok")]),
    }
    manager = make_manager(workfolder, topology, scripts, groupchats=[channel("main", ["a", "b"])])
    assert await manager.run() is RunStatus.DONE
    [prompt] = prompts(manager.run_dir, "a")
    assert prompt.startswith("## Posteingang\n\nOffene Fragen an dich (beantworte sie zuerst):")
    assert prompt.index("★ Vom User: [#") < prompt.index("[#2 b · question · an dich · #main]")
    assert "★ Nachrichten vom User (haben Vorrang):" in prompt
    assert "Bitte zuerst n ≤ 100 prüfen." in prompt
    assert "Zwischenstand" not in prompt, "other messages are only counted"
    assert "Neue Nachrichten: #main 5 (davon 2 vom User)" in prompt  # 4 + brief
    assert prompt.endswith("## Auftrag\n\nArbeite.")
    assert prompts(manager.run_dir, "c") == ["Ohne Kanal."]


async def test_channels_open_from_their_stage(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("a", "vorher", stage="review")
        await ctx.checkpoint("diskussion")
        await ctx.turn("a", "nachher", stage="diskussion")

    scripts = {
        "a": turns(
            [post("jury", "zu früh")],
            [post("jury", "jetzt"), post("spaet", "auch")],
        ),
        "b": turns(),
    }
    groupchats = [
        channel("main", ["a", "b"]),
        channel("jury", ["a", "b"], open_from="diskussion"),
        channel("spaet", ["a", "b"], open_from="review2"),
    ]
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=groupchats,
        stages=["review", "review2", "diskussion"],
    )
    assert await manager.run() is RunStatus.DONE
    first, second = prompts(manager.run_dir, "a")
    assert "#jury" not in first and "#spaet" not in first
    assert "#jury 1" in second and "#spaet 1" in second  # the brief
    results = [e["data"] for e in events(manager.run_dir, "a") if e["kind"] == "tool_result"]
    assert results[0]["is_error"] and "öffnet ab Phase „diskussion“" in results[0]["content"]
    assert not results[1]["is_error"] and not results[2]["is_error"]
    opened = [
        (e["data"]["channel"], e["data"]["stage"])
        for e in log_entries(manager.run_dir, "channel_open")
    ]
    assert opened == [("jury", "diskussion"), ("spaet", "diskussion")]


# --------------------------------------------------------------------------------------------
# A5 rounds
# --------------------------------------------------------------------------------------------


async def run_discussion(
    workfolder: Path,
    scripts: dict[str, Any],
    config: dict[str, Any],
    *,
    pacing: PacingSettings = FAST,
    **discuss: Any,
) -> tuple[Any, DiscussionOutcome]:
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", **discuss))

    members = list(scripts)
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", members, **config)],
        pacing=pacing,
    )
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.DONE
    return manager, results[0]


async def test_rounds_end_when_everybody_passes(workfolder: Path) -> None:
    passing = room("post_message", channel="main", body="", kind="pass")
    scripts = {
        "a": turns([post("main", "a1")], [post("main", "a2")], [passing]),
        "b": turns([post("main", "b1")], [passing], [passing]),
    }
    manager, outcome = await run_discussion(
        workfolder, scripts, {"mode": "rounds"}, rounds=5, stage="runde"
    )
    assert (outcome.ended_by, outcome.rounds, len(outcome.outcomes)) == (
        DiscussionEnd.ALL_PASSED,
        3,
        6,
    )
    first = prompts(manager.run_dir, "a")[0]
    assert "Runde 1 von 5 in #main." in first and "poste `pass`" in first
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert end["data"]["ended_by"] == "all_passed" and end["data"]["rounds"] == 3


async def test_rounds_sequential_skip_done_members_and_stop_after_n(workfolder: Path) -> None:
    scripts = {
        "a": turns([post("main", "a1"), done()]),
        "b": turns([wait(0.05), post("main", "b1")], [post("main", "b2")]),
    }
    manager, outcome = await run_discussion(
        workfolder, scripts, {"mode": "rounds", "order": "sequential"}, rounds=2
    )
    assert (outcome.ended_by, outcome.rounds) == (DiscussionEnd.ROUNDS, 2)
    assert [o.agent_id for o in outcome.outcomes] == ["a", "b", "b"]
    a_end = events(manager.run_dir, "a")[-1]["ts"]
    b_start = events(manager.run_dir, "b")[0]["ts"]
    assert b_start >= a_end, "sequential: one after the other"


async def test_rounds_end_when_all_are_done(workfolder: Path) -> None:
    scripts = {"a": turns([done()]), "b": turns([post("main", "fertig"), done()])}
    _, outcome = await run_discussion(workfolder, scripts, {"mode": "rounds"}, rounds=4)
    assert (outcome.ended_by, outcome.rounds) == (DiscussionEnd.ALL_DONE, 1)


async def test_w2_03_1_topology_discusses_through_the_run_context_contract(
    workfolder: Path,
) -> None:
    """W2-03.1 B1: `discuss` is part of `RunContext`; a topology needs nothing else."""
    assert "discuss" in RunContext.__abstractmethods__

    class WithoutDiscuss(RunContext):  # an implementation must provide `discuss`
        locals().update({name: None for name in RunContext.__abstractmethods__ - {"discuss"}})

    with pytest.raises(TypeError, match="discuss"):
        WithoutDiscuss()  # type: ignore[abstract]

    found: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:  # uses the contract only, no isinstance
        found.append(await ctx.discuss("main", "Tauscht euch aus.", stage="austausch", rounds=2))

    scripts = {
        "a": turns([post("main", "a1")], [post("main", "a2")]),
        "b": turns([post("main", "b1")], [post("main", "b2")]),
    }
    manager = make_manager(
        workfolder, topology, scripts, groupchats=[channel("main", ["a", "b"], mode="rounds")]
    )
    assert await manager.run() is RunStatus.DONE
    [outcome] = found
    assert (outcome.channel, outcome.ended_by, outcome.rounds) == ("main", DiscussionEnd.ROUNDS, 2)
    assert len(outcome.outcomes) == 4
    checkpoint = log_entries(manager.run_dir, "checkpoint")[-1]["data"]
    assert checkpoint == {"stage": "austausch", "channel": "main", "phase": "discussion"}


# --------------------------------------------------------------------------------------------
# A5 event: wake-up, debounce, limit, own messages, ping-pong, end
# --------------------------------------------------------------------------------------------


EVENT = {"mode": "event", "debounce_s": 0.2, "quiet_after_s": 0.4}
IDLE = {"idle_after_s": 0.1}  # idle rounds come quickly (docs/09 §4.4)


async def test_event_wakes_at_once_for_a_question_without_debounce(workfolder: Path) -> None:
    """L8: a message to an idle agent wakes it at once, not after `debounce_s`."""
    scripts = {
        "a": turns(
            [
                wait(0.1),
                post("main", "Gilt Lemma A für n = 9?", kind="question", to=["b"]),
                post("main", "Siehe auch check.py", to=["b"]),
                post("main", "Und n = 13?", to=["b"]),
                done(),
            ]
        ),
        "b": turns([say("bereit")], [post("main", "Ja, beides geprüft."), done()]),
    }
    # a debounce far above the bound below, so a slow event loop cannot blur the two (B11)
    config = {**EVENT, "debounce_s": 5.0}
    manager, outcome = await run_discussion(
        workfolder, scripts, config, prompt="Prüft Lemma A.", stage="diskussion"
    )
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and outcome.wakeups == {"b": 1}
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == "b" and wake["data"]["reasons"][0] == "question"
    kickoff, woken = prompts(manager.run_dir, "b")
    assert kickoff.endswith("## Auftrag\n\nPrüft Lemma A.")
    assert "Du wurdest in #main geweckt: eine Frage an dich" in woken
    assert "Gilt Lemma A für n = 9?" in woken, "the open question is in the inbox"
    first_post = min(m.ts for m in manager.room.messages("main") if m.sender == "a")
    wake_start = [e for e in events(manager.run_dir, "b") if e["kind"] == "turn_start"][1]["ts"]
    assert wake_start - first_post < 2, "woken at once, not after the debounce time"
    [scheduled] = log_entries(manager.run_dir, "wake_scheduled")
    assert scheduled["data"]["agent"] == "b" and scheduled["data"]["reason"] == "question"
    checkpoint = log_entries(manager.run_dir, "checkpoint")[-1]["data"]
    assert checkpoint == {"stage": "diskussion", "channel": "main", "phase": "discussion"}


async def test_event_any_never_wakes_the_sender(workfolder: Path) -> None:
    scripts = {
        "a": turns([wait(0.1), post("main", "Notiz für alle")], [say("nichts")], repeat_last=True),
        "b": turns([say("bereit")], [done()]),
    }
    manager, outcome = await run_discussion(
        workfolder,
        scripts,
        {**EVENT, **IDLE, "wake_on": ["any"], "debounce_s": 0.05, "quiet_after_s": 30},
    )
    assert outcome.ended_by is DiscussionEnd.ROUNDS and outcome.rounds == 3
    assert outcome.wakeups == {"b": 1}
    assert [e["data"]["agent"] for e in log_entries(manager.run_dir, "wake")] == ["b"]


async def test_event_respects_max_wakeups_per_agent(workfolder: Path) -> None:
    scripts = {
        "a": turns(
            [
                wait(0.1),
                post("main", "Erste Frage?", to=["b"]),
                wait(0.8),
                post("main", "Zweite Frage?", to=["b"]),
            ],
            [say("nichts")],
            repeat_last=True,
        ),
        "b": turns([say("bereit")], [say("geweckt")], repeat_last=True),
    }
    manager, outcome = await run_discussion(
        workfolder, scripts, {**EVENT, **IDLE, "debounce_s": 0.05, "max_wakeups_per_agent": 1}
    )
    assert outcome.wakeups == {"b": 1}
    [skipped] = log_entries(manager.run_dir, "wake_skipped")
    assert skipped["data"] == {
        "channel": "main",
        "agent": "b",
        "reason": "max_wakeups_per_agent",
        "limit": 1,
    }
    assert manager.room.agent_states()[1].wakeups == 1


async def test_event_ping_pong_guard_stops_automatic_wakeups(workfolder: Path) -> None:
    reply_a = post("main", "Nein, doch nicht.", to=["b"])
    reply_b = post("main", "Doch!", to=["a"])
    scripts = {
        "a": turns([wait(0.1), reply_a], [reply_a], repeat_last=True),
        "b": turns([say("bereit")], [reply_b], repeat_last=True),
    }
    pacing = PacingSettings(poll_interval_s=0.01, ping_pong_limit=2)
    manager, outcome = await run_discussion(
        workfolder, scripts, {**EVENT, **IDLE, "debounce_s": 0.05}, pacing=pacing
    )
    assert outcome.ended_by is DiscussionEnd.ROUNDS, "idle rounds still give both a turn"
    [guard] = log_entries(manager.run_dir, "ping_pong")
    assert guard["data"] == {"channel": "main", "agents": ["a", "b"]}
    notes = [m for m in manager.room.messages("main") if m.sender == "orchestrator" and m.to]
    assert [(n.body, n.to) for n in notes] == [
        ("Bitte Stand zusammenfassen oder Dissens festhalten.", ["a", "b"])
    ]
    assert sum(outcome.wakeups.values()) <= 3, "no more automatic wake-ups after the guard"


async def test_event_discussion_ends_on_stop(workfolder: Path) -> None:
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los."))

    scripts = {"a": turns([wait(30)]), "b": turns([wait(30)])}
    manager = make_manager(
        workfolder, topology, scripts, groupchats=[channel("main", ["a", "b"], **EVENT)]
    )
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and len(manager.orchestrator._active) == 2
    )
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert results[0].ended_by is DiscussionEnd.STOPPED


# --------------------------------------------------------------------------------------------
# A6 M1 criterion 6: answer as woken agent and mid-turn; attachment readable for the other
# --------------------------------------------------------------------------------------------


async def test_m1_criterion_6_answers_by_wakeup_and_mid_turn_with_attachment(
    workfolder: Path,
) -> None:
    open_question = "${box.open_questions.0.id}"
    explorer = {
        "turns": [
            {  # kick-off: works for a while; the critic's first question arrives as a hint
                "steps": [
                    wait(0.6),
                    room("inbox", save_as="box"),
                    post(
                        "main",
                        "n = 9 erfüllt die Voraussetzung nicht.",
                        kind="answer",
                        reply_to=open_question,
                    ),
                ]
            },
            {  # woken by the second question: answers with an attachment
                "steps": [
                    room("inbox", save_as="box"),
                    post(
                        "main",
                        "Rechnung im Anhang.",
                        kind="answer",
                        reply_to=open_question,
                        to=["critic"],
                        refs=["check.py"],
                    ),
                    done(),
                ]
            },
        ]
    }
    critic = {
        "turns": [
            {
                "steps": [
                    wait(0.1),
                    post("main", "Gegenbeispiel n = 9?", kind="question", to=["explorer-1"]),
                    wait(1.5),
                    post("main", "Wie hast du n = 13 geprüft?", kind="question", to=["explorer-1"]),
                ]
            },
            {  # woken by the answer: reads the attachment of the latest message
                "steps": [
                    room("read_messages", save_as="new", channel="main"),
                    room(
                        "get_attachment",
                        msg_id="${new.-1.id}",
                        name="${new.-1.refs.0.name}",
                    ),
                    done(),
                ]
            },
        ]
    }
    scripts = {"explorer-1": explorer, "critic": critic}
    config = {"mode": "event", "debounce_s": 0.05, "quiet_after_s": 0.5}
    manager, outcome = await run_discussion(
        workfolder, scripts, config, prompt="Prüft Lemma A.", stage="diskussion"
    )
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    assert outcome.wakeups == {"explorer-1": 1, "critic": 1}
    run_dir = manager.run_dir
    messages = manager.room.messages("main")
    q1, q2 = [m for m in messages if m.kind is MessageKind.QUESTION]
    a1, a2 = [m for m in messages if m.kind is MessageKind.ANSWER]

    # mid-turn: the hint reached the working explorer, it answered in the same turn
    explorer_log = events(run_dir, "explorer-1")
    [hint] = hints(run_dir, "explorer-1")
    assert hint["turn"] == 1 and "1 Frage an dich in #main" in hint["data"]["text"]
    assert (a1.reply_to, a1.sender) == (q1.id, "explorer-1")
    answer_call = next(
        e
        for e in explorer_log
        if e["kind"] == "tool_call" and e["data"]["input"].get("kind") == "answer"
    )
    assert answer_call["turn"] == 1 and hint["seq"] < answer_call["seq"]

    # wake-up: the idle explorer was woken by the second question and answered it
    assert a2.reply_to == q2.id and not manager.room.message(q2.id).open
    assert len(prompts(run_dir, "explorer-1")) == 2
    wakes = {e["data"]["agent"]: e["data"] for e in log_entries(run_dir, "wake")}
    assert (
        wakes["explorer-1"]["reasons"] == ["question"] and q2.id in wakes["explorer-1"]["messages"]
    )

    # attachment: stored per channel and readable for the critic
    [attachment] = a2.refs
    assert attachment.path == f"groupchats/attachments/main/{a2.id}/check.py"
    assert (run_dir / attachment.path).read_text() == "print('ok')\n"
    critic_results = [e["data"] for e in events(run_dir, "critic") if e["kind"] == "tool_result"]
    read = critic_results[-2]
    assert not read["is_error"] and read["content"].endswith(f'{a2.id}/check.py"')


# --------------------------------------------------------------------------------------------
# Hints mid-turn (docs/06 §4), delivery state of user messages (docs/06 §10)
# --------------------------------------------------------------------------------------------


async def test_hints_at_most_one_per_interval_or_five_tool_calls(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turns([("a", "arbeite"), ("b", "schreibe")])

    tool = {"do": "tool", "name": "Bash", "input": {"command": "true"}, "duration_s": 0.01}
    scripts = {
        "a": turns([wait(0.4), *[tool] * 5, wait(0.4)]),
        "b": turns([wait(0.05), post("main", "eins"), wait(0.1), post("main", "zwei")]),
    }
    manager = make_manager(workfolder, topology, scripts, groupchats=[channel("main", ["a", "b"])])
    assert await manager.run() is RunStatus.DONE
    log = events(manager.run_dir, "a")
    first, second = hints(manager.run_dir, "a")
    calls = [e["seq"] for e in log if e["kind"] == "tool_call"]
    assert first["seq"] < calls[0], "first hint at once"
    assert second["seq"] > calls[4], "second hint only after five tool calls"
    assert [e["data"]["messages"] for e in log_entries(manager.run_dir, "hint")] == [[2], [3]]


def test_hint_gate_lets_an_urgent_hint_through_the_window() -> None:
    now = [0.0]
    gate = HintGate(interval_s=60, tool_calls=5, clock=lambda: now[0])
    gate.delivered(0)
    assert not gate.allows(1)
    assert gate.allows(1, urgent=True)
    gate.delivered(1)
    assert not gate.allows(2), "an urgent hint starts a new window"


def _question_run(workfolder: Path, to: str) -> Any:
    """`a` works; `b` posts a note (first hint at once) and then a question to `to`."""

    async def topology(ctx: RunContext) -> None:
        await ctx.turns([("a", "arbeite"), ("b", "schreibe")])

    scripts = {
        "a": turns([wait(0.5)]),
        "b": turns(
            [
                wait(0.05),
                post("main", "eins"),
                wait(0.1),
                post("main", "?", kind="question", to=[to]),
            ]
        ),
        "c": turns([say("still")]),
    }
    return make_manager(
        workfolder, topology, scripts, groupchats=[channel("main", ["a", "b", "c"])]
    )


async def test_question_to_the_agent_skips_the_hint_window(workfolder: Path) -> None:
    """W2-05 §7: the question reaches `a` in the same turn, not through its own inbox()."""
    manager = _question_run(workfolder, "a")
    assert await manager.run() is RunStatus.DONE
    assert [e["data"]["messages"] for e in log_entries(manager.run_dir, "hint")] == [[2], [3]]
    second = hints(manager.run_dir, "a")[1]
    assert "1 Frage an dich in #main" in second["data"]["text"]


async def test_question_to_someone_else_stays_in_the_hint_window(workfolder: Path) -> None:
    manager = _question_run(workfolder, "c")
    assert await manager.run() is RunStatus.DONE
    assert len(hints(manager.run_dir, "a")) == 1


async def test_user_messages_wake_or_hint_and_record_the_delivery(workfolder: Path) -> None:
    ids: dict[str, int] = {}
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los.", kickoff=True))

    scripts = {"a": turns([wait(0.5)], [done()]), "b": turns([done()])}
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", ["a", "b"], mode="event", debounce_s=0.05, quiet_after_s=2)],
    )
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and "a" in manager.orchestrator._active
    )
    room_ = manager.room
    ids["hint"] = room_.post(
        "main", "Kurze Frage?", sender="user", via="gui", kind="question", to=["a"]
    ).id
    await eventually(lambda: manager.orchestrator.agent_status("a") is AgentStatus.IDLE)
    ids["wake"] = room_.post("main", "Bitte melde dich.", sender="user", via="gui", to=["a"]).id
    assert await asyncio.wait_for(running, 10) is RunStatus.DONE
    assert room_.message(ids["hint"]).delivery.status is DeliveryStatus.HINT_MID_TURN
    assert room_.message(ids["wake"]).delivery.status is DeliveryStatus.WOKEN
    woken = prompts(manager.run_dir, "a")[1]
    assert "★ Vom User: [#" in woken and "Bitte melde dich." in woken
    assert results[0].ended_by is DiscussionEnd.ALL_DONE


async def test_w2_05_slow_hint_delivery_blocks_neither_the_room_nor_other_agents(
    workfolder: Path,
) -> None:
    """Review W2-01 B1: `deliver_hint` may wait (Claude: until the next tool result). The
    orchestrator delivers per agent in its own task, so a slow agent delays nobody."""

    async def topology(ctx: RunContext) -> None:
        await ctx.turns([("a", "arbeite"), ("b", "arbeite"), ("c", "schreibe")])

    scripts = {
        "a": turns([wait(1.6)]),
        "b": turns([wait(1.6)]),
        "c": turns([wait(0.2), post("main", "hallo zusammen"), wait(0.2)]),
    }
    manager = make_manager(
        workfolder, topology, scripts, groupchats=[channel("main", ["a", "b", "c"])]
    )
    original = manager.backend_factory

    def factory(prepared: Any) -> Any:
        backend = original(prepared)
        if prepared.id == "a":
            inner = backend.deliver_hint

            async def slow(text: str) -> bool:
                await asyncio.sleep(1.0)  # like Claude during a long tool call
                return await inner(text)

            backend.deliver_hint = slow
        return backend

    manager.backend_factory = factory
    assert await manager.run() is RunStatus.DONE
    [message] = [m for m in manager.room.messages("main") if m.sender == "c"]
    by_agent = {e["data"]["agent"]: e for e in log_entries(manager.run_dir, "hint")}
    assert set(by_agent) == {"a", "b"}
    assert by_agent["b"]["ts"] - message.ts < 0.5, "b waited for a's slow delivery"
    assert by_agent["a"]["ts"] - message.ts >= 1.0
    assert hints(manager.run_dir, "a") and hints(manager.run_dir, "b")


@pytest.mark.parametrize("to", [["a"], ["a", "b"]])
async def test_delivery_state_comes_from_the_addressee(workfolder: Path, to: list[str]) -> None:
    """Review W3-02 M1: `a` is woken, the hint to `b` arrives after that and changes nothing."""

    async def topology(ctx: RunContext) -> None:
        await ctx.discuss("main", "Los.", kickoff=True)

    scripts = {"a": turns([say("bereit")], [done()]), "b": turns([wait(1.0), done()])}
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", ["a", "b"], mode="event", debounce_s=0.05, quiet_after_s=2)],
    )
    original = manager.backend_factory

    def factory(prepared: Any) -> Any:
        backend = original(prepared)
        if prepared.id == "b":
            inner = backend.deliver_hint

            async def slow(text: str) -> bool:
                await asyncio.sleep(0.3)
                return await inner(text)

            backend.deliver_hint = slow
        return backend

    manager.backend_factory = factory
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: (
            manager.orchestrator is not None
            and manager.orchestrator.agent_status("a") is AgentStatus.IDLE
            and "b" in manager.orchestrator._active
        )
    )
    message = manager.room.post("main", "Bitte melde dich.", sender="user", via="gui", to=to)
    assert await asyncio.wait_for(running, 10) is RunStatus.DONE
    assert [e["data"]["agent"] for e in log_entries(manager.run_dir, "hint")] == ["b"]
    assert manager.room.message(message.id).delivery.status is DeliveryStatus.WOKEN


async def test_hint_to_another_agent_is_no_delivery_to_the_addressee(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turns([("b", "arbeite")])

    scripts = {"a": turns([done()]), "b": turns([wait(0.6), done()])}
    manager = make_manager(workfolder, topology, scripts, groupchats=[channel("main", ["a", "b"])])
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and "b" in manager.orchestrator._active
    )
    message = manager.room.post("main", "Nur für a.", sender="user", via="gui", to=["a"])
    assert await asyncio.wait_for(running, 10) is RunStatus.DONE
    assert log_entries(manager.run_dir, "hint"), "b got the message as a hint"
    assert manager.room.message(message.id).delivery.status is DeliveryStatus.NEXT_INBOX

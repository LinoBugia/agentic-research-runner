"""W4-00: V10 (c) and the honest way out (docs/09 §5 V10, SECURITY.md layer 0).

On a question to the user an agent waits only with `wait_for_messages` (L7), never with
`wait_until`; without an answer it works on with a reasoned assumption. Prompts, conversation
rule 4 and the returns of the room say so; a `wait_until` is still accepted (no hard limit,
option a is not decided), its answer advises against it. Both backends get the same honest way
out in their fixed protocol. Whole runs use FakeBackend agents: N7a with a model that follows
the advice, and one that ignores it and stays bounded by L10 and the deadlines.
"""

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from run_kit import log_entries, make_workfolder, post, room, turns
from test_agent_loop_hangs import IDLE, run_case, tool_results

from arr.models import GroupChatConfig
from arr.permissions.types import ApprovalStatus
from arr.room import waiting
from arr.room.server import USER_MAY_NOT_ANSWER, USER_WAITED_LONG, build_server
from arr.room.store import CONVERSATION_RULES, RoomStore
from arr.room.types import MessageKind, MessageVia
from arr.runs.context import DiscussionEnd
from arr.runs.manager import QUESTION_CLOSED
from arr.runs.orchestrator import WORKED_ON

D1, D2 = "diskutant-1", "diskutant-2"
BACKENDS = ["claude.md", "codex.md"]
USER_WAIT = (
    "Auf den User wartest du nur mit `wait_for_messages`, nie mit `wait_until`: Antwortet er "
    "nicht, arbeite mit einer begründeten Annahme weiter und nenne sie in deinem Ergebnis. Eine "
    "späte Antwort weckt dich; prüfe dann deine Annahme."
)


def prompt_text(name: str) -> str:
    return resources.files("arr").joinpath("templates", "prompts", name).read_text("utf-8")


def bullet(text: str, title: str) -> str:
    [line] = [line for line in text.splitlines() if line.startswith(f"- **{title}")]
    return line


# --------------------------------------------------------------------------------------------
# Prompts and conversation rule 4
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", BACKENDS)
def test_v10_the_prompt_waits_for_the_user_only_with_wait_for_messages(name: str) -> None:
    text = prompt_text(name)
    assert USER_WAIT in text
    assert "beim User mit einer begründeten Annahme" not in text, "the old, weaker advice"
    waiting_part = bullet(text, "Ansprechen und warten")
    assert "5 min" in waiting_part and "`@user`" in waiting_part


def test_v10_conversation_rule_4_says_the_same() -> None:
    rule = CONVERSATION_RULES[3]
    assert rule.startswith("Wer jemanden fragt")
    assert "auf den User nie mit `wait_until`" in rule
    assert "begründeten Annahme" in rule and "in deinem Ergebnis" in rule


def test_the_honest_way_out_is_the_same_in_both_backends() -> None:
    """A7: one place in each fixed protocol, word for word the same."""
    claude, codex = (bullet(prompt_text(name), "Ehrlicher Ausweg") for name in BACKENDS)
    assert claude == codex
    for part in (
        "„Nicht gelöst“ und Teilergebnisse sind gültige Ergebnisse.",
        "eine Freigabe abgelehnt wurde",
        "ein Werkzeug gesperrt ist",
        "die Aufgabe so nicht lösbar ist",
        "halte an",
        "Sag das in deinem Ergebnis oder frag nach",
        "statt einen anderen Weg um die Grenze zu suchen",
    ):
        assert part in claude, part


@pytest.mark.parametrize("name", BACKENDS)
def test_the_honest_way_out_is_said_once(name: str) -> None:
    """What `claude.md` had in three places is drawn together; nothing doubles it."""
    text = prompt_text(name)
    assert text.count("Nicht gelöst") == 1
    assert "Versuche es dann nicht auf anderem Weg" not in text
    assert "Versuche nicht, die Sperre zu umgehen" not in text
    assert text.count("Teilergebnis") == 2, "„Warten auf ein Teilergebnis“ and the way out"


# --------------------------------------------------------------------------------------------
# Returns of the room server, for both backends
# --------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> RoomStore:
    run = tmp_path / "run"
    for agent_id in ("a", "b"):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels([GroupChatConfig(name="main", members=["a", "b"])])
    return store


def _text(result: Any) -> str:
    return result.content[0].text  # type: ignore[no-any-return]


async def _nobody(store: RoomStore) -> str:
    async with Client(build_server(store.for_agent("a"))) as client:
        return _text(await client.call_tool("wait_for_messages", {"seconds": 5}))


@pytest.fixture(params=["claude", "codex"])
def provider(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(waiting.PROVIDER_ENV, request.param)
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 0.3)
    return str(request.param)


async def test_v10_nobody_answers_advises_an_assumption_while_the_user_is_asked(
    store: RoomStore, provider: str
) -> None:
    store.for_agent("a").post_message("main", "@user welches Band?", MessageKind.QUESTION)
    text = await _nobody(store)
    assert "Niemand antwortet" in text and USER_MAY_NOT_ANSWER in text
    assert "auch nicht mit `wait_until`" in text
    assert "oder warte mit `wait_until`" not in text, "no advice to wait long"


async def test_v10_nobody_answers_keeps_wait_until_for_agents(
    store: RoomStore, provider: str
) -> None:
    """A question to an agent, or one of another agent to the user: the old advice."""
    store.for_agent("a").post_message("main", "@b stimmt das?", MessageKind.QUESTION)
    store.for_agent("b").post_message("main", "@user und du?", MessageKind.QUESTION)
    text = await _nobody(store)
    assert "oder warte mit `wait_until`" in text and USER_MAY_NOT_ANSWER not in text


async def test_v10_an_answered_question_no_longer_counts(store: RoomStore, provider: str) -> None:
    asked = store.for_agent("a").post_message("main", "@user Band?", MessageKind.QUESTION)
    store.post("main", "Ja.", sender="user", reply_to=asked.id, via=MessageVia.GUI)
    assert not store.for_agent("a").asked_the_user()


async def test_v10_wait_until_is_accepted_but_advised_against(
    store: RoomStore, provider: str
) -> None:
    a = store.for_agent("a")
    async with Client(build_server(a)) as client:
        plain = _text(await client.call_tool("wait_until", {"minutes": 2}))
        a.post_message("main", "@user welches Band?", MessageKind.QUESTION)
        asked = await client.call_tool("wait_until", {"minutes": 2})
    assert USER_WAITED_LONG not in plain and plain.startswith("Gut. Beende jetzt deinen Turn")
    assert not asked.is_error, "accepted: no hard limit (V10 option a is not decided)"
    text = _text(asked)
    assert text.startswith("Gut. Beende jetzt deinen Turn") and USER_WAITED_LONG in text
    assert "antwortet vielleicht lange nicht" in text and "begründeten Annahme" in text
    assert store.waits(waiting.WaitKind.UNTIL)["a"].until > 0, "the wait stands"


# --------------------------------------------------------------------------------------------
# N7a with FakeBackend agents
# --------------------------------------------------------------------------------------------


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


ASK = post("main", "@user Welches Höhenband hat Vorrang?", kind="question")


async def test_v10_n7a_the_agent_follows_the_advice(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N7a: question to `@user`, no answer. After the L7 limit the asker works on with an
    assumption and gives its result; the question closes with the note of L18, the other one
    waits for that result, and the discussion ends with `all_done`, not by `wall_time`."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.0)
    assumption = post(
        "main", "Ergebnis mit Annahme: Band 10–35 (der User hat nicht geantwortet).", kind="result"
    )
    scripts = {
        D1: turns([ASK, room("wait_for_messages", seconds=5), assumption]),
        D2: turns([room("wait_until", minutes=0.5, until_result_from=D1)],
                  [post("main", "Mein Teil, mit der Annahme von D1.", kind="result")]),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert line["ended_by"] is DiscussionEnd.ALL_DONE and not line["hang"] and line["limit"] == []
    assert [r["ended_by"] for r in tool_results(manager, D1)] == ["nobody"]
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    lines = (manager.run_dir / "approvals.jsonl").read_text("utf-8").splitlines()
    [closed] = [e for e in map(json.loads, lines) if e["event"] == "obsolete"]
    assert closed["note"] == WORKED_ON.format(agent=D1)
    states = [(e["data"]["state"], e["data"].get("reason")) for e in log_entries(
        manager.run_dir, "user_question")]  # fmt: skip
    assert states == [("open", None), ("closed", "result")]
    woken = [e["data"]["reasons"] for e in log_entries(manager.run_dir, "wake")
             if e["data"]["agent"] == D2]  # fmt: skip
    assert woken == [["result_from"]], "the result with the assumption wakes the other one"
    assert RoomStore(manager.run_dir).open_user_questions() == []


async def test_v10_n7a_an_agent_that_ignores_the_advice_stays_bounded(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asker waits with `wait_until` anyway, again and again, and the other one waits for
    its result (N7a as on 09.11.). arr accepts it; L10 and the deadlines end it without
    `wall_time`, and the question closes with the run."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 0.6)
    long_wait = room("wait_until", minutes=0.01)
    scripts = {
        D1: turns([ASK, room("wait_for_messages", seconds=5), long_wait], [long_wait],
                  repeat_last=True),
        D2: turns([room("wait_until", minutes=0.01, until_result_from=D1)], repeat_last=True),
    }  # fmt: skip
    manager, line = await run_case(workfolder, scripts, IDLE, wall="40s", within=60)
    assert not line["hang"] and line["limit"] == [] and line["took_s"] < 30
    assert line["ended_by"] in (DiscussionEnd.ROUNDS, DiscussionEnd.ALL_DONE)
    skipped = {e["data"]["agent"] for e in log_entries(manager.run_dir, "wake_skipped")
               if e["data"]["reason"] == "wait_until_repeat"}  # fmt: skip
    assert D1 in skipped, "L10: after three deadlines in a row no more wake-up"
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    lines = (manager.run_dir / "approvals.jsonl").read_text("utf-8").splitlines()
    [closed] = [e for e in map(json.loads, lines) if e["event"] == "obsolete"]
    assert closed["note"] == QUESTION_CLOSED
    print(f"\nV10 ungehorsam: {line}, Turns D1 {manager.orchestrator.agents[D1].turns_used}")

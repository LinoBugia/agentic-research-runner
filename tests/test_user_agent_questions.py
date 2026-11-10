"""E15 (W4-01 H1, Lead): Claude Code never answers a question to the user (W4-03.1 point 4).

A message of the user agent (`via: user_agent`, from `post_to_run`) addresses and wakes like
every message, but it closes no open question to the user: not in the room, not in the inbox,
also with `to` = the asker, `@name` or `reply_to` on the question. Messages from the GUI answer
as before (L17). The test through `arr mcp` follows with W4-01.1.
"""

from pathlib import Path

import pytest
from run_kit import eventually, log_entries, make_workfolder, post, say, turns
from test_agent_loop import IDLE, discuss

from arr.models import GroupChatConfig
from arr.permissions.types import ApprovalStatus
from arr.room.store import USER, RoomStore
from arr.room.types import MessageKind, MessageVia
from arr.runs.orchestrator import Orchestrator

D1, D2 = "diskutant-1", "diskutant-2"


@pytest.fixture
def store(tmp_path: Path) -> RoomStore:
    run = tmp_path / "run"
    for agent_id in ("a", "b"):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels([GroupChatConfig(name="main", members=["a", "b"])])
    return store


@pytest.mark.parametrize("how", ["to", "at_name", "reply_to"], ids=["to", "@name", "reply_to"])
def test_e15_the_user_agent_answers_no_question_in_the_room(store: RoomStore, how: str) -> None:
    """Every way of answering, sent by Claude Code: the question stays open. Counter-check:
    the same message from the GUI closes it."""
    asked = store.for_agent("a").post_message("main", "@user welches Band?", MessageKind.QUESTION)
    ways = {
        "to": {"body": "Band 10.", "to": ["a"]},
        "at_name": {"body": "@a Band 10."},
        "reply_to": {"body": "Band 10.", "reply_to": asked.id},
    }
    sent = store.post("main", sender="user", via=MessageVia.USER_AGENT, **ways[how])
    assert store.message(asked.id).open and [q.id for q in store.open_user_questions()] == [
        asked.id
    ]
    if how != "reply_to":
        assert sent.to == ["a"], "it still addresses the asker (and so wakes it)"
    store.post("main", sender="user", via=MessageVia.GUI, **ways[how])
    assert not store.message(asked.id).open, "counter-check: the GUI answers it"


async def test_e15_the_user_agent_wakes_the_asker_but_the_inbox_item_stays_open(
    workfolder: Path,
) -> None:
    """In a run: Claude Code writes `@diskutant-1 Band 10.` The asker is woken and reads it,
    the item „Frage“ in the inbox stays open (no `answered`), the room question too. Then the
    user answers in the GUI: the item is answered with the user's text (counter-check)."""

    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: bool(orch.inbox.pending()), within=10)
        orch.room.post("main", "@diskutant-1 Band 10.", sender="user", via=MessageVia.USER_AGENT)
        await eventually(lambda: orch.agents[D1].turns_used >= 2, within=10)
        state["after_agent"] = (
            [i.status for i in orch.inbox.items()],
            [q.id for q in orch.room.open_user_questions()],
        )
        orch.room.post("main", "@diskutant-1 Band 12.", sender="user", via=MessageVia.GUI)

    state: dict[str, object] = {}
    ask = post("main", "@user welches Band?", kind="question")
    scripts = {
        D1: turns(
            [ask, say("Ich warte.")],
            [say("gelesen")],
            [post("main", "Danke.", kind="result")],
            repeat_last=True,
        ),
        D2: turns([post("main", "Mein Teil.", kind="result")]),
    }
    manager, _, _ = await discuss(workfolder, scripts, IDLE, during=user)
    statuses, still_open = state["after_agent"]  # type: ignore[misc]
    assert statuses == [ApprovalStatus.OPEN] and len(still_open) == 1
    claude, gui = [m for m in manager.room.messages("main") if m.sender == USER]
    woken = [e["data"] for e in log_entries(manager.run_dir, "wake") if e["data"]["agent"] == D1]
    assert any(claude.id in w["messages"] for w in woken), "Claude Code's message woke it"
    answered = [
        e["data"]
        for e in log_entries(manager.run_dir, "user_question")
        if e["data"].get("state") == "answered"
    ]
    assert [a["answer"] for a in answered] == [gui.id], "only the GUI message answered"
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.DECIDED and item.decision.comment == gui.body


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)

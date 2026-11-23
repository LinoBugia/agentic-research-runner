"""In-process tests of the room MCP server with `mcp.Client` (W1-03, docs/06 §9, S4 §5)."""

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from arr.models import GroupChatConfig
from arr.room import server as room_server
from arr.room.server import FOOTER_SEPARATOR, INTERNAL_ERROR, build_server
from arr.room.store import RoomStore
from arr.room.types import MessageVia

AGENT_TOOLS = {
    "inbox",
    "list_channels",
    "read_messages",
    "post_message",
    "get_attachment",
    "set_status",
    "submit",  # W4b-06b: every run, also without `run.yaml`
    "assign",
    "get_assignment",
    "wait_for_messages",
    "wait_until",
}
REMOVED_TOOLS = {
    *("vote", "give_floor", "call_vote", "pin", "close_discussion"),
    *("submit_result", "submit_rating"),
}
"""Gone for every run since W4b-03 (§6.7, Entscheidung 11, 12, 27); the old tools to hand in
since W4b-06b."""
FOOTER = re.compile(r"\n\n---\nungelesen: (nichts|.+)$")


@pytest.fixture
def store(tmp_path: Path) -> RoomStore:
    run = tmp_path / "run"
    for agent in ("explorer-1", "critic", "judge-1"):
        (run / "agents" / agent / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels(
        [
            GroupChatConfig(name="main", members=["explorer-1", "critic"]),
            GroupChatConfig(name="judges", members=["judge-1", "critic"], open_from="judge"),
            GroupChatConfig(
                name="seminar",
                members=["critic", "explorer-1"],
                mode="moderated",
                moderator="critic",
            ),
            GroupChatConfig(name="private", members=["judge-1"]),
        ]
    )
    return store


class Agent:
    """Small helper around an in-process client: returns (is_error, text without footer)."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.footers: list[str] = []

    async def call(self, tool: str, **arguments: Any) -> tuple[bool, str]:
        result = await self.client.call_tool(tool, arguments)
        text = result.content[0].text
        match = FOOTER.search(text)
        assert match, f"{tool}: no footer in {text!r}"
        self.footers.append(match.group(0).removeprefix(FOOTER_SEPARATOR))
        return result.is_error, text[: match.start()]

    async def ok(self, tool: str, **arguments: Any) -> str:
        is_error, text = await self.call(tool, **arguments)
        assert not is_error, text
        return text

    async def error(self, tool: str, **arguments: Any) -> str:
        is_error, text = await self.call(tool, **arguments)
        assert is_error, text
        return text


async def tool_names(store: RoomStore, agent: str) -> set[str]:
    async with Client(build_server(store.for_agent(agent))) as client:
        return {t.name for t in (await client.list_tools()).tools}


async def test_tool_list_has_no_vote_and_no_moderator_tools(store: RoomStore) -> None:
    """W4b-03 A15: the moderator of a channel gets the same tools as everybody; without
    `run.yaml` every agent gets `submit` and `assign` (W4b-06b)."""
    assert await tool_names(store, "explorer-1") == AGENT_TOOLS
    assert await tool_names(store, "critic") == AGENT_TOOLS
    assert not (await tool_names(store, "critic")) & REMOVED_TOOLS


async def test_all_agent_tools_over_mcp_with_footer(store: RoomStore) -> None:
    (store.run_dir / "agents" / "explorer-1" / "work" / "lemma.md").write_text("# Lemma A")
    store.add_assignment("explorer-1", 1, "Finde Ideen", stage="explore", context={"k": 1})
    async with (
        Client(build_server(store.for_agent("explorer-1"))) as c1,
        Client(build_server(store.for_agent("critic"))) as c2,
    ):
        e1, critic = Agent(c1), Agent(c2)

        inbox = await e1.ok("inbox")
        assert "Offene Fragen an dich: keine" in inbox and "Steckbrief #main" in inbox
        assert e1.footers[-1] == "ungelesen: #main 1, #seminar 1"

        channels = await e1.ok("list_channels")
        assert "#main – event, offen, 1 Nachricht, 1 ungelesen" in channels
        assert "Moderator: critic" in channels

        assert "[#1 orchestrator · note · #main] (angeheftet)" in await e1.ok("read_messages")
        assert e1.footers[-1] == "ungelesen: nichts"
        assert await e1.ok("read_messages") == "Keine neuen Nachrichten."

        posted = await e1.ok(
            "post_message", channel="main", body="Lemma A", kind="claim", refs=["lemma.md"]
        )
        claim_id = int(re.search(r"#(\d+)", posted).group(1))
        assert posted == f"Nachricht #{claim_id} in #main gepostet. Anhänge: lemma.md."

        question = await critic.ok(
            "post_message",
            channel="main",
            body="Warum?",
            kind="question",
            to=["explorer-1"],
            reply_to=claim_id,
        )
        assert "Offene Frage an explorer-1." in question
        assert await e1.ok("set_status", status="working") == "Status gesetzt: working."
        assert e1.footers[-1] == "ungelesen: #main 1, Fragen an dich 1"
        assert "Offene Fragen an dich:\n\n[#" in await e1.ok("inbox")

        read = await critic.ok("read_messages", channel="main", only_new=False, limit=5)
        assert f"[#{claim_id} explorer-1 · claim · #main]" in read
        assert f"get_attachment({claim_id}, 'lemma.md')" in read
        attachment = await critic.ok("get_attachment", msg_id=claim_id, name="lemma.md")
        assert attachment.endswith("# Lemma A")
        assert f"groupchats/attachments/main/{claim_id}/lemma.md" in attachment

        thread = await critic.ok("read_messages", thread=claim_id)
        assert thread.count("[#") == 2

        proposal = await e1.ok("post_message", channel="main", body="Ansatz B", kind="proposal")
        assert "gepostet" in proposal

        assignment = await e1.ok("get_assignment")
        assert assignment.startswith("Auftrag (Turn 1, Stufe explore):\nFinde Ideen")
        assert '"k": 1' in assignment
        assert await critic.ok("get_assignment") == "Noch kein Auftrag."

        store.add_assignment(
            "explorer-1", 2, "Gib ab.", stage="explore", context={"submission": {"format": "text"}}
        )
        handed = await e1.ok("submit", status="done", summary="2-adisch", files=["lemma.md"])
        assert handed.startswith("Abgabe #1 angenommen"), handed
        assert "output/submissions/001-explore-explorer-1" in handed
        assert await e1.ok("set_status", status="done", note="fertig") == "Status gesetzt: done."

    assert {s.agent: s.status for s in store.agent_states()} == {"explorer-1": "done"}
    [sub] = store.submissions()
    assert (sub.agent, sub.summary, sub.files) == ("explorer-1", "2-adisch", ["lemma.md"])


async def test_removed_tools_cannot_be_called_over_mcp(store: RoomStore) -> None:
    """W4b-03 A15: a call of a removed tool is an error, also for the moderator of a channel;
    nothing is written."""
    before = len(store.messages_after(0))
    async with Client(build_server(store.for_agent("critic"))) as client:
        for tool, args in (
            ("vote", {"proposal_id": 1, "choice": "yes"}),
            ("give_floor", {"agent": "explorer-1", "prompt": "Schritt 3?"}),
            ("call_vote", {"proposal_id": 1}),
            ("pin", {"msg_id": 1}),
            ("close_discussion", {"decision": "B"}),
        ):
            result = await client.call_tool(tool, args)
            assert result.is_error, tool
    assert len(store.messages_after(0)) == before
    assert store.channel_state("seminar").value == "open"


async def test_rejections_over_mcp_are_german(store: RoomStore) -> None:
    async with (
        Client(build_server(store.for_agent("explorer-1"))) as c1,
        Client(build_server(store.for_agent("critic"))) as c2,
    ):
        e1, critic = Agent(c1), Agent(c2)
        # Foreign channel: neither writing nor reading.
        assert "Du bist nicht Mitglied im Kanal #private" in await e1.error(
            "post_message", channel="private", body="hallo"
        )
        assert "Du bist nicht Mitglied im Kanal #private" in await e1.error(
            "read_messages", channel="private"
        )
        # Closed channel (open_from).
        for tool, args in (("post_message", {"body": "x"}), ("read_messages", {})):
            text = await critic.error(tool, channel="judges", **args)
            assert "Der Kanal #judges ist noch geschlossen. Er öffnet ab Phase „judge“." in text
        # The removed moderator tools are offered to nobody (W4b-03).
        assert (await c1.call_tool("pin", {"msg_id": 1})).is_error
        assert (await c2.call_tool("pin", {"msg_id": 1})).is_error
        assert "Unbekannte Nachrichtenart „chat“" in await e1.error(
            "post_message", channel="main", body="x", kind="chat"
        )
        assert "Diesen Status setzt arr selbst" in await e1.error("set_status", status="blocked")
    rows = [
        m
        for m in store.messages("private") + store.messages("judges")
        if m.sender != "orchestrator"
    ]
    assert rows == []
    assert all(f.startswith("ungelesen: ") for f in e1.footers + critic.footers)


async def test_unexpected_errors_are_hidden_and_logged(
    store: RoomStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    room = store.for_agent("explorer-1")

    def boom() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(room, "inbox", boom)
    async with Client(build_server(room)) as client:
        text = await Agent(client).error("inbox")
    assert INTERNAL_ERROR in text and "database is locked" not in text
    assert any("database is locked" in (r.exc_text or "") for r in caplog.records)


async def test_user_message_is_shown_to_agent(store: RoomStore) -> None:
    store.post("main", "Bitte kürzer", sender="user", via=MessageVia.GUI, to=["critic"])
    async with Client(build_server(store.for_agent("critic"))) as client:
        text = await Agent(client).ok("read_messages", channel="main")
    assert "[#5 user · note · an critic · #main] (★ vom User, hat Vorrang)\nBitte kürzer" in text


def test_main_needs_environment(
    monkeypatch: pytest.MonkeyPatch, restore_root_logger: Any, tmp_path: Path
) -> None:
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.delenv("RUN_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        room_server.main()
    assert exc.value.code == 2
    monkeypatch.setenv("AGENT_ID", "critic")
    monkeypatch.setenv("RUN_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        room_server.main()
    assert exc.value.code == 1


async def test_wrong_argument_types_are_german(store: RoomStore) -> None:
    """W1-03.1 point 6: pydantic errors of the arguments reach the agent in German."""
    async with Client(build_server(store.for_agent("explorer-1"))) as client:
        agent = Agent(client)
        text = await agent.error("post_message", channel="main", body="x", to="critic")
        assert (
            text == "Ungültige Argumente für „post_message“: „to“ muss eine Liste sein, "
            'z. B. ["critic"].'
        )
        text = await agent.error("read_messages", limit="viele", only_new="vielleicht")
        assert "„limit“ muss eine ganze Zahl sein" in text
        assert "„only_new“ muss true oder false sein" in text
        assert await agent.error("post_message", body="x") == (
            "Ungültige Argumente für „post_message“: „channel“ fehlt."
        )
        text = await agent.error("submit", status="done", summary="x", fields=[1])
        assert "„fields“ muss ein Objekt sein" in text
    assert agent.footers[-1] == "ungelesen: #main 1, #seminar 1"
    assert all("validation error" not in f for f in agent.footers)
    assert [m.sender for m in store.messages("main")] == ["orchestrator"]


def test_invalid_log_level_falls_back_to_info(
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logger: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W1-03.1 point 7: no crash, a German warning on stderr, level INFO."""
    monkeypatch.setenv("ARR_LOG_LEVEL", "foo")
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.delenv("RUN_DIR", raising=False)
    with pytest.raises(SystemExit) as exc:
        room_server.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "WARNING  arr.room.server: Ungültiger Wert ARR_LOG_LEVEL='foo'" in err
    assert "nutzt INFO" in err
    assert restore_root_logger.level == logging.INFO
    monkeypatch.setenv("ARR_LOG_LEVEL", "debug")
    with pytest.raises(SystemExit):
        room_server.main()
    assert restore_root_logger.level == logging.DEBUG


def test_user_messages_are_marked_in_tool_output() -> None:
    """docs/06 §10: messages of `user` stand out in `inbox()` and `read_messages` (W2-03)."""
    from arr.room.types import Message, MessageKind

    user = Message(id=7, ts=0, channel="main", sender="user", body="Bitte n ≤ 100.")
    question = user.model_copy(
        update={"id": 8, "kind": MessageKind.QUESTION, "to": ["e1"], "open": True}
    )
    agent = user.model_copy(update={"id": 9, "sender": "critic"})
    assert room_server.render_message(user).startswith(
        "[#7 user · note · #main] (★ vom User, hat Vorrang)\n"
    )
    assert "(★ vom User, hat Vorrang, offene Frage)" in room_server.render_message(question)
    assert "★" not in room_server.render_message(agent)

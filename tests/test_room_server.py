"""In-process tests of the room MCP server with `mcp.Client` (W1-03, docs/06 §9, S4 §5)."""

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
    "vote",
    "set_status",
    "submit_result",
    "submit_rating",
    "get_assignment",
}
MODERATOR_TOOLS = {"give_floor", "call_vote", "pin", "close_discussion"}
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


async def test_tool_list_and_moderator_tools_only_for_moderator(store: RoomStore) -> None:
    assert await tool_names(store, "explorer-1") == AGENT_TOOLS
    assert await tool_names(store, "critic") == AGENT_TOOLS | MODERATOR_TOOLS


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
        assert f"groupchats/attachments/{claim_id}/lemma.md" in attachment

        thread = await critic.ok("read_messages", thread=claim_id)
        assert thread.count("[#") == 2

        proposal = await e1.ok("post_message", channel="main", body="Ansatz B", kind="proposal")
        proposal_id = int(re.search(r"#(\d+)", proposal).group(1))
        voted = await critic.ok("vote", proposal_id=proposal_id, choice="yes", reason="ok")
        assert voted == f"Stimme zu #{proposal_id} gespeichert: yes."

        assignment = await e1.ok("get_assignment")
        assert assignment.startswith("Auftrag (Turn 1, Stufe explore):\nFinde Ideen")
        assert '"k": 1' in assignment
        assert await critic.ok("get_assignment") == "Noch kein Auftrag."

        result = await e1.ok("submit_result", kind="idea", title="2-adisch", path="lemma.md")
        result_id = int(re.search(r"#(\d+)", result).group(1))
        assert f"output/results/{result_id:03d}-explorer-1-idea.md" in result
        rated = await critic.ok(
            "submit_rating", result_id=result_id, score=8, comment="gut", criteria={"Tiefe": 7}
        )
        assert rated.endswith(f"für Ergebnis #{result_id} gespeichert (8).")
        assert await e1.ok("set_status", status="done", note="fertig") == "Status gesetzt: done."

    assert {s.agent: s.status for s in store.agent_states()} == {"explorer-1": "done"}
    assert store.ratings()[0].criteria == {"Tiefe": 7}


async def test_moderator_tools_over_mcp(store: RoomStore) -> None:
    async with Client(build_server(store.for_agent("critic"))) as client:
        critic = Agent(client)
        assert await critic.ok("give_floor", agent="explorer-1", prompt="Schritt 3?") == (
            "explorer-1 hat das Wort."
        )
        proposal = store.for_agent("explorer-1").post_message("seminar", "B", kind="proposal")
        assert await critic.ok("call_vote", proposal_id=proposal.id, rule="unanimous") == (
            f"Abstimmung über #{proposal.id} gestartet."
        )
        assert await critic.ok("pin", msg_id=proposal.id) == f"Nachricht #{proposal.id} angeheftet."
        closed = await critic.ok("close_discussion", decision="Wir nehmen B")
        assert closed.startswith("Diskussion in #seminar beendet, Entscheidung #")
    assert store.floor_requests()[0].agent == "explorer-1"


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
        # Moderator tools: not offered to others, and only for own channels.
        result = await c1.call_tool("pin", {"msg_id": 1})
        assert result.is_error
        assert "Du bist nicht Moderator von #main" in await critic.error("pin", msg_id=1)
        assert "nicht Moderator von #main" in await critic.error(
            "close_discussion", decision="x", channel="main"
        )
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
    assert "[#5 user · note · an critic · #main]\nBitte kürzer" in text


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

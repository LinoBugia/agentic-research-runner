"""stdio MCP server that exposes the room tools to one research agent.

One process per agent (`python -m arr.room.server`): the identity comes from `AGENT_ID`,
the run folder (and with it `groupchats/room.sqlite`) from `RUN_DIR`. Tools return short
German text; every result ends with the footer „ungelesen: …“ (docs/06 §4). Expected
failures become a `ToolError` with a German message, everything else is logged to stderr as
plain lines (`arr.log`) so the orchestrator can copy it into the run log.

See docs/01-architektur.md §5, docs/06-gruppenchats.md §9, docs/spikes/S4_room.md §5.
"""

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from arr.errors import ArrError
from arr.log import setup_logging
from arr.room.store import AgentRoom, RoomStore
from arr.room.types import ChannelInfo, Inbox, Message, RoomError

log = logging.getLogger("arr.room.server")  # fixed name, also under `python -m`

AGENT_ID_ENV = "AGENT_ID"
RUN_DIR_ENV = "RUN_DIR"
LOG_LEVEL_ENV = "ARR_LOG_LEVEL"
MAX_INLINE_ATTACHMENT_CHARS = 20_000
FOOTER_SEPARATOR = "\n\n---\n"

INSTRUCTIONS = (
    "Gruppenchat dieses Runs. Lies zuerst `inbox()`. Neue Nachrichten holst du mit "
    "`read_messages()`, schreiben mit `post_message(channel, body, kind, to, reply_to, refs)`. "
    "Dateien aus deinem Arbeitsordner hängst du über `refs` an. Jedes Ergebnis endet mit "
    "„ungelesen: …“."
)
INTERNAL_ERROR = "Interner Fehler im Gruppenchat. Die Details stehen im Run-Log."


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------


def _size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} kB"


def render_message(msg: Message) -> str:
    """One message as text; the header starts with `[#<id> ` (parsed by tests and tools)."""
    head = f"[#{msg.id} {msg.sender} · {msg.kind.value}"
    if msg.reply_to is not None:
        head += f" → #{msg.reply_to}"
    if msg.to:
        head += " · an " + ", ".join(msg.to)
    head += f" · #{msg.channel}]"
    flags = [flag for flag, on in (("angeheftet", msg.pinned), ("offene Frage", msg.open)) if on]
    if flags:
        head += " (" + ", ".join(flags) + ")"
    lines = [head, msg.body]
    for att in msg.refs:
        lines.append(
            f"Anhang: {att.name} ({_size(att.size)}) – get_attachment({msg.id}, {att.name!r})"
        )
    return "\n".join(lines)


def render_messages(msgs: list[Message], empty: str = "Keine neuen Nachrichten.") -> str:
    """Messages separated by blank lines."""
    return "\n\n".join(render_message(m) for m in msgs) if msgs else empty


def render_inbox(inbox: Inbox) -> str:
    """Inbox: open questions in full, unread counts, pinned messages (docs/06 §4, §8)."""
    parts = []
    if inbox.open_questions:
        parts.append("Offene Fragen an dich:\n\n" + render_messages(inbox.open_questions))
    else:
        parts.append("Offene Fragen an dich: keine")
    unread = [f"#{ch} {n}" for ch, n in inbox.unread.items()]
    parts.append("Neue Nachrichten: " + (", ".join(unread) if unread else "keine Kanäle"))
    if inbox.pinned:
        parts.append("Angeheftet:\n\n" + render_messages(inbox.pinned))
    return "\n\n".join(parts)


def render_channels(channels: list[ChannelInfo]) -> str:
    """Channel list with purpose, members, mode and state."""
    if not channels:
        return "Du bist in keinem Kanal."
    blocks = []
    for ch in channels:
        state = "offen" if ch.open else "geschlossen"
        if not ch.open and ch.open_from:
            state += f" (öffnet ab Phase „{ch.open_from}“)"
        members = ", ".join(
            (f"{m.alias} (du)" if m.agent else m.alias) if m.alias else (m.agent or "?")
            for m in ch.members
        )
        noun = "Nachricht" if ch.message_count == 1 else "Nachrichten"
        counts = f"{ch.message_count} {noun}, {ch.unread} ungelesen"
        lines = [f"#{ch.name} – {ch.mode.value}, {state}, {counts}"]
        if ch.purpose:
            lines.append(f"Zweck: {ch.purpose}")
        lines.append(f"Mitglieder: {members}")
        if ch.moderator:
            lines.append(f"Moderator: {ch.moderator}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def footer(room: AgentRoom) -> str:
    """„ungelesen: #main 3, Fragen an dich 1“ (docs/06 §4)."""
    unread, questions = room.unread_counts()
    items = [f"#{ch} {n}" for ch, n in unread.items() if n]
    if questions:
        items.append(f"Fragen an dich {questions}")
    return "ungelesen: " + (", ".join(items) if items else "nichts")


def _with_footer(room: AgentRoom, text: str) -> str:
    try:
        return text + FOOTER_SEPARATOR + footer(room)
    except Exception:
        log.exception("agent %s: footer failed", room.agent_id)
        return text


def _respond(room: AgentRoom, tool: str, call: Callable[[], str]) -> str:
    """Run a tool, map expected errors to a German `ToolError`, append the footer."""
    started = time.perf_counter()
    try:
        text = call()
    except RoomError as exc:
        log.info("agent %s: %s rejected: %s", room.agent_id, tool, exc)
        raise ToolError(_with_footer(room, str(exc))) from None
    except Exception:
        log.exception("agent %s: %s failed", room.agent_id, tool)
        raise ToolError(_with_footer(room, INTERNAL_ERROR)) from None
    log.debug(
        "agent %s: %s ok (%.1f ms)", room.agent_id, tool, (time.perf_counter() - started) * 1000
    )
    return _with_footer(room, text)


def _attachment_text(path: Path) -> str:
    text = f"Anhang: {path} ({_size(path.stat().st_size)})"
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return text + "\nKeine Textdatei; lies sie direkt über den Pfad."
    if len(content) > MAX_INLINE_ATTACHMENT_CHARS:
        return text + f"\nZu lang für die Anzeige ({len(content)} Zeichen); lies sie über den Pfad."
    return text + "\n\n" + content


# --------------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------------


def build_server(room: AgentRoom) -> MCPServer:
    """MCP server with the room tools of docs/06 §9 bound to `room.agent_id`.

    Moderator tools are only registered if the agent moderates at least one channel; the
    store checks the moderator for every call as well.
    """
    server = MCPServer("arr-room", instructions=INSTRUCTIONS)

    @server.tool(
        description="Posteingang: offene Fragen an dich, neue Nachrichten pro Kanal, "
        "angeheftete Nachrichten (Steckbrief, Stand der Diskussion)."
    )
    def inbox() -> str:
        return _respond(room, "inbox", lambda: render_inbox(room.inbox()))

    @server.tool(description="Deine Kanäle mit Zweck, Mitgliedern, Taktung und Zustand.")
    def list_channels() -> str:
        return _respond(room, "list_channels", lambda: render_channels(room.list_channels()))

    @server.tool(
        description="Nachrichten lesen. Standard: nur neue Nachrichten anderer aus allen "
        "Kanälen (danach gelten sie als gelesen). `channel` für einen Kanal, "
        "`only_new=false` für die letzten `limit` Nachrichten, `thread=<id>` für einen Strang."
    )
    def read_messages(
        channel: str | None = None,
        only_new: bool = True,
        limit: int = 50,
        thread: int | None = None,
    ) -> str:
        return _respond(
            room,
            "read_messages",
            lambda: render_messages(room.read_messages(channel, only_new, limit, thread)),
        )

    @server.tool(
        description="Nachricht in einen deiner Kanäle schreiben. `kind`: note, idea, claim, "
        "proof, objection, question, answer, proposal, decision (nur Moderator), result, pass. "
        "`to`: Adressaten (leer = alle); eine `question` mit `to` bleibt offen, bis der "
        "Adressat mit `answer` und `reply_to` antwortet. `refs`: Dateien relativ zu deinem "
        "Arbeitsordner, die als Anhang für alle Mitglieder kopiert werden."
    )
    def post_message(
        channel: str,
        body: str,
        kind: str = "note",
        to: list[str] | None = None,
        reply_to: int | None = None,
        refs: list[str] | None = None,
    ) -> str:
        def call() -> str:
            msg = room.post_message(channel, body, kind, to, reply_to, refs)
            text = f"Nachricht #{msg.id} in #{msg.channel} gepostet."
            if msg.open:
                text += f" Offene Frage an {', '.join(msg.to)}."
            if msg.refs:
                text += " Anhänge: " + ", ".join(a.name for a in msg.refs) + "."
            return text

        return _respond(room, "post_message", call)

    @server.tool(
        description="Anhang einer Nachricht lesen: Pfad im Run-Ordner und bei Textdateien "
        "der Inhalt."
    )
    def get_attachment(msg_id: int, name: str) -> str:
        return _respond(
            room, "get_attachment", lambda: _attachment_text(room.get_attachment(msg_id, name))
        )

    @server.tool(
        description="Über einen Vorschlag (proposal) abstimmen: `choice` yes, no oder abstain, "
        "mit kurzer Begründung. Eine zweite Stimme ersetzt die erste."
    )
    def vote(proposal_id: int, choice: str, reason: str = "") -> str:
        def call() -> str:
            v = room.vote(proposal_id, choice, reason)
            return f"Stimme zu #{v.proposal_id} gespeichert: {v.choice.value}."

        return _respond(room, "vote", call)

    @server.tool(
        description="Eigenen Status melden: `working` oder `done` (mit kurzem Fazit in `note`)."
    )
    def set_status(status: str, note: str = "") -> str:
        def call() -> str:
            room.set_status(status, note)
            return f"Status gesetzt: {status}."

        return _respond(room, "set_status", call)

    @server.tool(
        description="Ergebnis der aktuellen Stufe abgeben, z. B. Idee, Lemma, Beweis oder "
        "Gutachten. Inhalt in `body` oder als Datei `path` relativ zu deinem Arbeitsordner."
    )
    def submit_result(
        kind: str, title: str, body: str | None = None, path: str | None = None
    ) -> str:
        def call() -> str:
            r = room.submit_result(kind, title, body, path)
            text = f"Ergebnis #{r.id} abgegeben (Stufe {r.stage or '–'}, Versuch {r.attempt})."
            return text + (f" Datei: {r.path}" if r.path else "")

        return _respond(room, "submit_result", call)

    @server.tool(
        description="Ein Ergebnis bewerten: `score` auf der Skala der Rubrik, `comment` als "
        "Begründung, optional `criteria` mit Punkten pro Kriterium."
    )
    def submit_rating(
        result_id: int, score: float, comment: str, criteria: dict[str, float] | None = None
    ) -> str:
        def call() -> str:
            r = room.submit_rating(result_id, score, comment, criteria)
            return f"Bewertung #{r.id} für Ergebnis #{r.result_id} gespeichert ({r.score:g})."

        return _respond(room, "submit_rating", call)

    @server.tool(description="Aktuellen Auftrag mit Stufe und Kontextpaket noch einmal abrufen.")
    def get_assignment() -> str:
        def call() -> str:
            a = room.get_assignment()
            if a is None:
                return "Noch kein Auftrag."
            text = f"Auftrag (Turn {a.turn}, Stufe {a.stage or '–'}):\n{a.prompt}"
            if a.context:
                text += "\n\nKontext:\n" + json.dumps(a.context, ensure_ascii=False, indent=2)
            return text

        return _respond(room, "get_assignment", call)

    if room.moderated_channels():
        _add_moderator_tools(server, room)
    return server


def _add_moderator_tools(server: MCPServer, room: AgentRoom) -> None:
    @server.tool(
        description="Nur Moderator: einem Mitglied das Wort geben, mit konkreter Frage in "
        "`prompt`. `channel` darf fehlen, wenn du genau einen Kanal moderierst."
    )
    def give_floor(agent: str, prompt: str, channel: str | None = None) -> str:
        def call() -> str:
            room.give_floor(agent, prompt, channel)
            return f"{agent} hat das Wort."

        return _respond(room, "give_floor", call)

    @server.tool(
        description="Nur Moderator: Abstimmung über einen Vorschlag starten, `rule` majority "
        "oder unanimous."
    )
    def call_vote(proposal_id: int, rule: str = "majority") -> str:
        def call() -> str:
            room.call_vote(proposal_id, rule)
            return f"Abstimmung über #{proposal_id} gestartet."

        return _respond(room, "call_vote", call)

    @server.tool(description="Nur Moderator: eine Nachricht anheften, z. B. einen Zwischenstand.")
    def pin(msg_id: int) -> str:
        def call() -> str:
            room.pin(msg_id)
            return f"Nachricht #{msg_id} angeheftet."

        return _respond(room, "pin", call)

    @server.tool(
        description="Nur Moderator: Diskussion beenden und das Ergebnis als `decision` posten. "
        "`channel` darf fehlen, wenn du genau einen Kanal moderierst."
    )
    def close_discussion(decision: str, channel: str | None = None) -> str:
        def call() -> str:
            msg = room.close_discussion(decision, channel)
            return f"Diskussion in #{msg.channel} beendet, Entscheidung #{msg.id}."

        return _respond(room, "close_discussion", call)


def main() -> None:
    """Entry point of the room process: read `AGENT_ID` and `RUN_DIR`, serve over stdio."""
    # Before MCPServer exists: its own logging setup then leaves our plain handler alone.
    setup_logging(os.environ.get(LOG_LEVEL_ENV, "INFO"))
    agent_id = os.environ.get(AGENT_ID_ENV)
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not agent_id or not run_dir:
        log.error(
            "Room-Server braucht die Umgebungsvariablen %s und %s.", AGENT_ID_ENV, RUN_DIR_ENV
        )
        sys.exit(2)
    try:
        room = RoomStore(Path(run_dir)).for_agent(agent_id)
    except (ArrError, ValueError) as exc:
        log.error("Room-Server für %s startet nicht: %s", agent_id, exc)
        sys.exit(1)
    log.info("room server for agent %s started (run %s)", agent_id, Path(run_dir).name)
    build_server(room).run()


if __name__ == "__main__":
    main()

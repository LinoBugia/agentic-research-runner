"""stdio MCP server that exposes the room tools to one research agent.

One process per agent (`python -m arr.room.server`): the identity comes from `AGENT_ID`,
the run folder (and with it `groupchats/room.sqlite`) from `RUN_DIR`. Tools return short
German text; every result ends with the footer „ungelesen: …“ (docs/06 §4). Expected
failures become a `ToolError` with a German message, everything else is logged to stderr as
plain lines (`arr.log`) so the orchestrator can copy it into the run log.

See docs/01-architektur.md §5, docs/06-gruppenchats.md §9, docs/spikes/S4_room.md §5.
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from arr.errors import ArrError
from arr.log import setup_logging
from arr.models import Provider
from arr.room.address import is_user_question
from arr.room.store import USER, AgentRoom, RoomStore
from arr.room.types import ChannelInfo, Inbox, Message, RoomError, Submission
from arr.room.waiting import PROVIDER_ENV, STREAK_MAX_S, WaitEnd, WaitOutcome, wait_max_s
from arr.room.waiting import wait_for_messages as wait_for_messages_
from arr.runs.phases.view import is_phase_context, render_assignment

log = logging.getLogger("arr.room.server")  # fixed name, also under `python -m`

AGENT_ID_ENV = "AGENT_ID"
RUN_DIR_ENV = "RUN_DIR"
LOG_LEVEL_ENV = "ARR_LOG_LEVEL"
MAX_INLINE_ATTACHMENT_CHARS = 20_000
FOOTER_SEPARATOR = "\n\n---\n"

INSTRUCTIONS = (
    "Gruppenchat dieses Runs. Lies zuerst `inbox()`. Neue Nachrichten holst du mit "
    "`read_messages()`, schreiben mit `post_message(channel, body, kind, to, reply_to, refs)`. "
    "Wen du ansprichst, nennst du mit `@name` im Text, den User mit `@user`. Auf eine Antwort "
    "wartest du mit `wait_for_messages`. Dateien aus deinem Arbeitsordner hängst du über `refs` "
    "an. Jedes Ergebnis endet mit „ungelesen: …“."
)
INTERNAL_ERROR = "Interner Fehler im Gruppenchat. Die Details stehen im Run-Log."
_EXPECTED = {
    "list_type": 'eine Liste sein, z. B. ["critic"]',
    "string_type": "ein Text sein",
    "int_type": "eine ganze Zahl sein",
    "int_parsing": "eine ganze Zahl sein",
    "int_from_float": "eine ganze Zahl sein",
    "float_type": "eine Zahl sein",
    "float_parsing": "eine Zahl sein",
    "bool_type": "true oder false sein",
    "bool_parsing": "true oder false sein",
    "dict_type": 'ein Objekt sein, z. B. {"Klarheit": 7}',
}


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------


def _size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} kB"


def render_message(msg: Message) -> str:
    """One message as text; the header starts with `[#<id> ` (parsed by tests and tools).

    Messages of `user` are marked „★ vom User, hat Vorrang“ (docs/06 §10), questions to the
    user „Frage an den User“ (only the user answers them, L16).
    """
    head = f"[#{msg.id} {msg.sender} · {msg.kind.value}"
    if msg.reply_to is not None:
        head += f" → #{msg.reply_to}"
    if msg.to:
        head += " · an " + ", ".join(msg.to)
    head += f" · #{msg.channel}]"
    marks = (
        ("★ vom User, hat Vorrang", msg.sender == USER),
        ("angeheftet", msg.pinned),
        ("Frage an den User", is_user_question(msg)),
        ("offene Frage", msg.open),
    )
    flags = [flag for flag, on in marks if on]
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
        # one log line, whatever the agent put into the message (W4b-01.1 G2)
        log.info(
            "agent %s: %s rejected: %s", room.agent_id, tool, " | ".join(str(exc).splitlines())
        )
        raise ToolError(_with_footer(room, str(exc))) from None
    except Exception:
        log.exception("agent %s: %s failed", room.agent_id, tool)
        raise ToolError(_with_footer(room, INTERNAL_ERROR)) from None
    log.debug(
        "agent %s: %s ok (%.1f ms)", room.agent_id, tool, (time.perf_counter() - started) * 1000
    )
    return _with_footer(room, text)


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


USER_MAY_NOT_ANSWER = (
    "Deine Frage an den User ist noch offen, und er antwortet vielleicht lange nicht. Warte "
    "nicht länger auf ihn, auch nicht mit `wait_until`: Arbeite mit einer begründeten Annahme "
    "weiter und nenne sie in deinem Ergebnis. Antwortet er später, weckt dich das; prüfe dann "
    "deine Annahme."
)
"""V10 (c), User 09.11.: on a question to the user, advise a reasoned assumption, not a
long wait. Advice only: a `wait_until` is still accepted (option a is not decided)."""

USER_WAITED_LONG = (
    "Beachte: Deine Frage an den User ist noch offen, und er antwortet vielleicht lange nicht. "
    "Warte danach nicht noch einmal auf ihn: Arbeite besser mit einer begründeten Annahme weiter "
    "und nenne sie in deinem Ergebnis. Eine späte Antwort weckt dich auch dann; prüfe dann deine "
    "Annahme."
)
"""The answer to a `wait_until` while the agent's question to the user is open (V10): the
wait stands, the turn ends as usual, and the advice is for the turn after it."""

USER_MAY_NOT_ANSWER_PHASES = (
    "Deine Frage an den User ist noch offen, und er antwortet vielleicht lange nicht. Brauchst "
    "du die Antwort, warte einmal mit `wait_until` bis zu fünf Minuten; eine Antwort oder Notiz "
    "des Users beendet das Warten. Antwortet er bis dahin nicht, arbeite mit einer begründeten "
    "Annahme weiter und nenne sie in deiner Abgabe."
)
"""`USER_MAY_NOT_ANSWER` in a run with phases (W4b-06a, Entscheidung 69): there the agent may
wait for the user once with `wait_until`, at most five minutes."""

USER_WAITED_LONG_PHASES = (
    "Beachte: Deine Frage an den User ist noch offen. Warte auf ihn höchstens fünf Minuten und "
    "nur einmal: Antwortet er bis dahin nicht, arbeite mit einer begründeten Annahme weiter und "
    "nenne sie in deiner Abgabe. Gibst du ab, gilt deine Frage als erledigt."
)
"""`USER_WAITED_LONG` in a run with phases (W4b-06a, Entscheidungen 67 and 69)."""


def render_wait(outcome: WaitOutcome, *, asked_user: bool = False, phases: bool = False) -> str:
    """Result of `wait_for_messages`: why it ended, how long it waited, the new messages.

    `asked_user`: a question of the agent to the user is open; then „nobody answers“ advises
    a reasoned assumption instead of `wait_until` (V10); in a run with `phases` one wait with
    `wait_until` of at most five minutes first (Entscheidung 69).
    """
    waited = f"nach {outcome.waited_s:.0f} s"
    if outcome.ended_by is WaitEnd.MESSAGE:
        head = f"Warten beendet {waited}: Es gibt eine Nachricht an dich."
        return head + "\n\n" + render_messages(outcome.messages)
    if outcome.ended_by is WaitEnd.FILE:
        return f"Warten beendet {waited}: Die Datei „{outcome.file}“ ist da."
    if outcome.ended_by is WaitEnd.NOBODY and asked_user:
        return (
            f"Warten beendet {waited}: Niemand antwortet, du hast in diesem Turn "
            f"{STREAK_MAX_S / 60:.0f} min ohne neue Nachricht an dich gewartet. "
            f"{USER_MAY_NOT_ANSWER_PHASES if phases else USER_MAY_NOT_ANSWER} Rufst du "
            "`wait_for_messages` noch einmal auf, beendet arr deinen Turn."
        )
    if outcome.ended_by is WaitEnd.NOBODY:
        return (
            f"Warten beendet {waited}: Niemand antwortet, du hast in diesem Turn "
            f"{STREAK_MAX_S / 60:.0f} min ohne neue Nachricht an dich gewartet. Arbeite weiter, "
            "gib dein Ergebnis ab oder warte mit `wait_until` (beendet deinen Turn; arr weckt "
            "dich bei einer Nachricht an dich). Rufst du `wait_for_messages` noch einmal auf, "
            "beendet arr deinen Turn."
        )
    if outcome.ended_by is WaitEnd.CAPACITY:
        return (
            f"Warten beendet {waited}: Ein anderer Agent wartet auf einen freien Platz in "
            "deinem Profil, und du belegst einen. Warte mit `wait_until`: Das beendet deinen "
            "Turn und gibt den Platz frei; arr weckt dich bei einer Nachricht an dich. Rufst "
            "du `wait_for_messages` noch einmal auf, beendet arr deinen Turn."
        )
    if outcome.ended_by is WaitEnd.TURN_END:
        return "arr beendet deinen Turn. arr weckt dich bei einer Nachricht an dich."
    text = f"Warten beendet {waited}: Frist abgelaufen, keine Nachricht an dich."
    if outcome.asked_s > outcome.limit_s:
        text += (
            f" Ein Aufruf wartet höchstens {outcome.limit_s:.0f} s. Rufe "
            "`wait_for_messages` noch einmal auf, wenn du weiter warten willst."
        )
    return text


def render_wait_until(
    until: float,
    file: str | None,
    result_from: str | None = None,
    *,
    asked_user: bool = False,
    phases: bool = False,
) -> str:
    """Answer of `wait_until`: end the turn now, arr wakes the agent. With a question of the
    agent to the user open, the wait is accepted, but the answer advises against a long one
    (V10); in a run with `phases` it allows one of at most five minutes (Entscheidung 69)."""
    minutes = max(1, round((until - time.time()) / 60))
    reasons = ["eine Nachricht an dich kommt"]
    if file:
        reasons.append(f"die Datei „{file}“ da ist")
    if result_from:
        reasons.append(f"{result_from} ein Ergebnis abgibt")
    text = (
        "Gut. Beende jetzt deinen Turn mit einer kurzen Zusammenfassung. arr weckt dich, "
        f"sobald {' oder '.join(reasons)}, spätestens um {_clock(until)} (in {minutes} min)."
    )
    if asked_user:
        text += f" {USER_WAITED_LONG_PHASES if phases else USER_WAITED_LONG}"
    return text


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


def argument_error(tool: str, exc: ValidationError) -> str:
    """German text for arguments that do not match the tool schema (wrong type, missing)."""
    problems: list[str] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "?"
        if err["type"] == "missing":
            text = f"„{field}“ fehlt"
        else:
            text = f"„{field}“ muss {_EXPECTED.get(err['type'], 'einen gültigen Wert haben')}"
        if text not in problems:
            problems.append(text)
    return f"Ungültige Argumente für „{tool}“: " + "; ".join(problems) + "."


class RoomServer(MCPServer):
    """`MCPServer` that reports argument errors in German with the footer (W1-03.1).

    `mcp` validates arguments before the tool runs and would pass pydantic's English text
    to the agent.
    """

    def __init__(self, room: AgentRoom) -> None:
        super().__init__("arr-room", instructions=INSTRUCTIONS)
        self.room = room

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        """Call a tool; a `ValidationError` of the arguments becomes a German `ToolError`."""
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            if not isinstance(exc.__cause__, ValidationError):
                raise
            text = argument_error(name, exc.__cause__)
            text = await asyncio.to_thread(_with_footer, self.room, text)
            raise ToolError(text) from exc.__cause__


def build_server(room: AgentRoom) -> MCPServer:
    """MCP server with the room tools of docs/06 §9 bound to `room.agent_id`.

    A run with the topology `phases` gets `submit` and not `submit_result`/`submit_rating`,
    the five old topologies the reverse (W4b-01). Since W4b-03 no run offers `vote`, `call_vote`,
    `give_floor`, `pin` or `close_discussion` (Entscheidung 11, 12, 27): gates read submissions,
    and the topology `moderated` (W4b-04) replaces the moderator.
    """
    server = RoomServer(room)

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
        "`to`: Adressaten (leer = alle), `user` für den User; `@name` und `@user` im Text "
        "adressieren genauso. Eine `question` mit `to` bleibt offen, bis der Adressat mit "
        "`answer` und `reply_to` antwortet; eine Frage an den User beantwortet nur der User. "
        "`refs`: Dateien relativ zu deinem Arbeitsordner, die als Anhang für alle Mitglieder "
        "kopiert werden."
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
        description="Eigenen Status melden: `working` oder `done` (mit kurzem Fazit in `note`)."
    )
    def set_status(status: str, note: str = "") -> str:
        def call() -> str:
            room.set_status(status, note)
            return f"Status gesetzt: {status}."

        return _respond(room, "set_status", call)

    if room.store.uses_phases():
        _add_submit(server, room)
    else:
        _add_old_submit_tools(server, room)

    limit = wait_max_s()

    @server.tool(
        description=f"Im Turn auf eine Antwort warten, höchstens {limit:.0f} s je Aufruf. Endet "
        "sofort bei einer Nachricht an dich (`@name`, `to`, Antwort auf deine Nachricht, "
        "Frage), wenn die Datei `until_file` (relativ zu deinem Arbeitsordner) da ist, oder "
        "nach `seconds`. Gibt die neuen Nachrichten zurück. Mehrmals hintereinander erlaubt, "
        f"in einem Turn zusammen höchstens {STREAK_MAX_S / 60:.0f} min ohne neue Nachricht an "
        "dich."
    )
    async def wait_for_messages(seconds: float = limit, until_file: str | None = None) -> str:
        try:
            outcome = await wait_for_messages_(room, seconds, until_file, max_s=limit)
        except RoomError as exc:
            log.info("agent %s: wait_for_messages rejected: %s", room.agent_id, exc)
            raise ToolError(await asyncio.to_thread(_with_footer, room, str(exc))) from None
        except Exception:
            log.exception("agent %s: wait_for_messages failed", room.agent_id)
            raise ToolError(await asyncio.to_thread(_with_footer, room, INTERNAL_ERROR)) from None
        asked = outcome.ended_by is WaitEnd.NOBODY and await asyncio.to_thread(room.asked_the_user)
        text = render_wait(outcome, asked_user=asked, phases=room.store.uses_phases())
        return await asyncio.to_thread(_with_footer, room, text)

    def _wait_until(minutes: float, until_file: str | None, result_from: str | None) -> str:
        def call() -> str:
            state = room.wait_until(minutes, until_file, result_from)
            shown = result_from.strip().removeprefix("@") if state.result_from else None
            return render_wait_until(
                state.until,
                state.file,
                shown,
                asked_user=room.asked_the_user(),
                phases=room.store.uses_phases(),
            )

        return _respond(room, "wait_until", call)

    if os.environ.get(PROVIDER_ENV) == Provider.CODEX:
        # Codex computes long scripts in the foreground; no `until_file` (L9, F1)
        @server.tool(
            description="Lange warten, wenn gerade niemand antwortet oder du auf das Ergebnis "
            "eines anderen Agenten wartest: `minutes` (höchstens 30), optional "
            "`until_result_from` (Name des Agenten). Danach beendest du deinen Turn; arr weckt "
            "dich bei einer Nachricht an dich, sobald der Agent ein Ergebnis abgibt, oder nach "
            "Ablauf."
        )
        def wait_until(minutes: float, until_result_from: str | None = None) -> str:
            return _wait_until(minutes, None, until_result_from)

    else:

        @server.tool(
            description="Lange warten, z. B. auf eine Rechnung, die im Hintergrund läuft, oder "
            "auf das Ergebnis eines anderen Agenten: `minutes` (höchstens 30), optional "
            "`until_file` (relativ zu deinem Arbeitsordner) und `until_result_from` (Name des "
            "Agenten). Danach beendest du deinen Turn; arr weckt dich bei einer Nachricht an "
            "dich, wenn die Datei da ist, sobald der Agent ein Ergebnis abgibt, oder nach Ablauf."
        )
        def wait_until(
            minutes: float, until_file: str | None = None, until_result_from: str | None = None
        ) -> str:
            return _wait_until(minutes, until_file, until_result_from)

    @server.tool(description="Aktuellen Auftrag mit Stufe und Kontextpaket noch einmal abrufen.")
    def get_assignment() -> str:
        def call() -> str:
            a = room.get_assignment()
            if a is None:
                return "Noch kein Auftrag."
            if is_phase_context(a.context):  # W4b-02b: assignment, format and package readable
                return f"Turn {a.turn}.\n\n" + render_assignment(a.context)
            text = f"Auftrag (Turn {a.turn}, Stufe {a.stage or '–'}):\n{a.prompt}"
            if a.context:
                text += "\n\nKontext:\n" + json.dumps(a.context, ensure_ascii=False, indent=2)
            return text

        return _respond(room, "get_assignment", call)

    return server


SUBMIT_DESCRIPTION = (
    "Deine Abgabe für den aktuellen Auftrag, im Format, das der Auftrag nennt "
    "(`get_assignment`, Kontext `submission`). Kopf: `status` done oder not_solved (nicht "
    "gelöst ist eine gültige Abgabe, dann ist `summary` die Begründung), `summary` (ein bis drei "
    "Sätze), `body` (Text), `files` (Pfade relativ zu deinem Arbeitsordner), `note_next` "
    "(Notiz an dich für die nächste Phase). `fields` hält die Felder des Formats: text und "
    "artifact keine; review `verdict` (accept, minor, major, reject) und `objections` (je "
    "`text`, bei einem bekannten Einwand dazu `ref` und `status` offen, behoben oder strittig); "
    "rating `ratings` (je `ref` = Nummer der Abgabe, `score`, optional `criteria`, `comment`); "
    "list `items` (je `title`, `text`); decision `choice`. arr prüft sofort und nennt, was "
    "fehlt; dann bessere nach und gib noch einmal ab. Eine neue Abgabe ersetzt die alte nicht, "
    "es zählt die neueste."
)


def render_submission(sub: Submission) -> str:
    """The answer of `submit`: number, place and what counts."""
    what = f"Abgabe #{sub.id} angenommen ({sub.format}, {sub.status}"
    what += f", Phase {sub.stage}" if sub.stage else ""
    what += f", Durchlauf {sub.attempt})."
    if sub.name:
        what += f" Fassung v{sub.version} von „{sub.name}“."
    if sub.sealed:
        what += " Sie bleibt bis zum Ende der Phase für die anderen verborgen."
    else:
        what += f" Ordner: {sub.path}."
    return what + " Gibst du noch einmal ab, zählt die neueste."


def _add_submit(server: MCPServer, room: AgentRoom) -> None:
    """`submit` of the phase toolkit (W4b-01), instead of `submit_result`/`submit_rating`."""

    @server.tool(description=SUBMIT_DESCRIPTION)
    def submit(
        status: str,
        summary: str,
        body: str | None = None,
        files: list[str] | None = None,
        note_next: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> str:
        return _respond(
            room,
            "submit",
            lambda: render_submission(room.submit(status, summary, body, files, note_next, fields)),
        )


def _add_old_submit_tools(server: MCPServer, room: AgentRoom) -> None:
    """`submit_result` and `submit_rating` of the five old topologies (until W4b-06)."""

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


def main() -> None:
    """Entry point of the room process: read `AGENT_ID` and `RUN_DIR`, serve over stdio."""
    # Before MCPServer exists: its own logging setup then leaves our plain handler alone.
    level = os.environ.get(LOG_LEVEL_ENV, "INFO").strip().upper()
    valid = level in logging.getLevelNamesMapping()
    setup_logging(level if valid else "INFO")
    if not valid:
        log.warning(
            "Ungültiger Wert %s=%r. Erlaubt: DEBUG, INFO, WARNING, ERROR, CRITICAL. "
            "Der Room-Server nutzt INFO.",
            LOG_LEVEL_ENV,
            os.environ.get(LOG_LEVEL_ENV),
        )
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

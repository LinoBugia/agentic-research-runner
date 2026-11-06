"""SQLite access for group chats, shared by the room server and the orchestrator.

`RoomStore` owns `groupchats/room.sqlite` of one run: schema, channels with their brief,
writes by `orchestrator` and `user`, reads for the GUI and the export per channel.
`AgentRoom` is the view of one research agent and implements `RoomAPI` with enforced
membership; the stdio MCP server (`arr.room.server`) wraps it.

Every write runs in `BEGIN IMMEDIATE` with a busy timeout; cursor updates are monotonic and
happen in the same transaction as the read (docs/spikes/S4_room.md §4). One connection per
call: the MCP server runs synchronous tools in worker threads.

See docs/01-architektur.md §5, docs/02-workfolder-und-runs.md §4-§5, docs/06-gruppenchats.md.
"""

import errno
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import stat
import string
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from arr.errors import ArrError
from arr.models import (
    RESERVED_SENDERS,
    AgentStatus,
    ChannelMode,
    GroupChatConfig,
    ResolvedRun,
    Rubric,
)
from arr.resolve import load_yaml_model
from arr.room.address import address, mentions
from arr.room.types import (
    AgentStatusRecord,
    Assignment,
    Attachment,
    ChannelInfo,
    ChannelMember,
    Delivery,
    DeliveryStatus,
    Inbox,
    Message,
    MessageKind,
    MessageVia,
    Rating,
    Result,
    RoomError,
    Vote,
    VoteChoice,
    VoteRule,
    WaitGuard,
    WaitKind,
    WaitState,
)
from arr.runs.prepare import load_agent_file

log = logging.getLogger(__name__)

ROOM_DIR = "groupchats"
DB_NAME = "room.sqlite"
ATTACHMENTS_DIR = "attachments"
RESULTS_DIR = "output/results"
ORCHESTRATOR = "orchestrator"
USER = "user"

BUSY_TIMEOUT_S = 30.0
MAX_READ_LIMIT = 200
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK  # NONBLOCK: a FIFO must not hang
AGENT_STATUSES = (AgentStatus.WORKING, AgentStatus.DONE)
"""Statuses an agent may set itself; `idle` and `blocked` are set by the orchestrator."""
WAIT_UNTIL_MAX_MIN = 30.0
"""`wait_until` waits at most this long (docs/09 §4.3); the orchestrator also caps it at the
wall time of the run."""
WAIT_UNTIL_REPEAT = 3
"""`wait_until` in a row that ended by their deadline, without a message to the agent and
without its file; after that arr refuses another one (L10)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel(name TEXT PRIMARY KEY, created REAL, config TEXT,
    state TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS member(channel TEXT, agent TEXT, alias TEXT,
    PRIMARY KEY (channel, agent));
CREATE TABLE IF NOT EXISTS msg(id INTEGER PRIMARY KEY, ts REAL, sender TEXT, channel TEXT,
    kind TEXT, body TEXT, to_agents TEXT, reply_to INTEGER, refs TEXT, pinned INTEGER,
    open INTEGER, via TEXT, delivery TEXT);
CREATE INDEX IF NOT EXISTS msg_channel ON msg(channel, id);
CREATE INDEX IF NOT EXISTS msg_reply ON msg(reply_to);
CREATE TABLE IF NOT EXISTS cursor(agent TEXT, channel TEXT, last_id INTEGER,
    PRIMARY KEY (agent, channel));
CREATE TABLE IF NOT EXISTS vote(proposal_id INTEGER, agent TEXT, choice TEXT, reason TEXT,
    ts REAL, PRIMARY KEY (proposal_id, agent));
CREATE TABLE IF NOT EXISTS agent_state(agent TEXT PRIMARY KEY, status TEXT, wakeups INTEGER,
    note TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS result(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, stage TEXT,
    kind TEXT, title TEXT, body TEXT, path TEXT, attempt INTEGER);
CREATE TABLE IF NOT EXISTS rating(id INTEGER PRIMARY KEY, ts REAL, rater TEXT,
    result_id INTEGER, score REAL, criteria TEXT, comment TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS rating_once ON rating(rater, result_id);
CREATE TABLE IF NOT EXISTS assignment(agent TEXT, turn INTEGER, stage TEXT, prompt TEXT,
    context TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS poll(proposal_id INTEGER PRIMARY KEY, channel TEXT, rule TEXT,
    called_by TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS floor(id INTEGER PRIMARY KEY, ts REAL, channel TEXT,
    moderator TEXT, agent TEXT, prompt TEXT, msg_id INTEGER, handled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS agent_report(agent TEXT PRIMARY KEY, status TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS agent_wait(agent TEXT, kind TEXT, since REAL, until REAL,
    file TEXT, PRIMARY KEY (agent, kind));
CREATE TABLE IF NOT EXISTS wait_guard(agent TEXT PRIMARY KEY, waited REAL NOT NULL DEFAULT 0,
    seen INTEGER, last_end TEXT, yield_to TEXT, end_turn TEXT,
    until_streak INTEGER NOT NULL DEFAULT 0);
"""
"""Tables of docs/02 §5 plus `channel.state`, `msg.via`/`msg.delivery`, `poll`, `floor`,
`agent_report` (the status an agent reported itself), `agent_wait` (one row per agent and
kind of wait, docs/09 §4.3) and `wait_guard` (the limits of waiting, L7, L10, L12). Rooms of
runs before W3-07 lack the last three; readers treat that as „no rows“."""

WAIT_GUARD_FIELDS = frozenset(
    {"waited", "seen", "last_end", "yield_to", "end_turn", "until_streak"}
)

CONVERSATION_RULES = (
    "Eine Nachricht, ein Gedanke. Setze `kind` korrekt.",
    "Behauptungen mit Begründung oder Beleg: Datei, Rechnung, Anhang.",
    "Beantworte Fragen an dich zuerst.",
    "Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle. Auf eine Antwort "
    "wartest du mit `wait_for_messages`.",
    "Widerspruch als `objection` mit `reply_to`, sachlich und konkret.",
    "Nicht wiederholen. Zustimmung nur, wenn sie etwas hinzufügt.",
    "Lange Inhalte (Beweise, Code, Tabellen) als Anhang, im Text nur die Kernaussage.",
    "Wer fertig ist, gibt sein Ergebnis ab (`submit_result` oder Nachricht `result`) oder "
    "meldet `done`. Danach weckt dich nur noch eine Frage an dich.",
)
"""Conversation rules of docs/06-gruppenchats.md §6 and docs/09 §4.7, in every channel brief."""

_MODE_TEXT = {
    ChannelMode.ROUNDS: "Runden: Alle Mitglieder kommen pro Runde dran, sonst `pass`.",
    ChannelMode.EVENT: "ereignisgesteuert: Wer mit `@name` angesprochen, gefragt oder mit "
    "`reply_to` beantwortet wird, wird sofort geweckt. Runden gibt es nur, wenn niemand mehr "
    "schreibt.",
    ChannelMode.MODERATED: "moderiert: Der Moderator erteilt das Wort und beendet die Diskussion.",
}
_END_TEXT = {
    ChannelMode.ROUNDS: "nach der letzten Runde oder wenn alle `pass`/`done` melden.",
    ChannelMode.EVENT: "wenn alle fertig sind (Ergebnis abgegeben oder `done`), nach den "
    "Leerlauf-Runden oder wenn es lange ruhig ist.",
    ChannelMode.MODERATED: "wenn der Moderator die Diskussion mit einer Entscheidung beendet.",
}


class ChannelState(StrEnum):
    """State of a channel in `channel.state` (docs/06-gruppenchats.md §2, §5.3).

    `waiting`: `open_from` not reached, agents can neither read nor write; `open`: normal;
    `closed`: discussion ended (`close_discussion`), readable but no new agent messages.
    """

    WAITING = "waiting"
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class FloorRequest:
    """A `give_floor` call of a moderator, picked up by the orchestrator (docs/06 §5.3)."""

    id: int
    ts: float
    channel: str
    moderator: str
    agent: str
    prompt: str
    msg_id: int
    handled: bool


@dataclass(frozen=True)
class ChannelExport:
    """Files written by `RoomStore.export_channel` (docs/02 §4: `groupchats/<kanal>.md|jsonl`)."""

    channel: str
    markdown: Path
    jsonl: Path
    messages: int


@dataclass
class _Channel:
    name: str
    config: GroupChatConfig
    state: ChannelState
    members: dict[str, str | None] = field(default_factory=dict)  # agent -> alias

    @property
    def anonymous(self) -> bool:
        return self.config.anonymous

    def display(self, agent: str) -> str:
        """Name shown to agents: the alias in anonymous channels, otherwise the ID."""
        return (self.members.get(agent) or agent) if self.anonymous else agent

    def resolve(self, name: str) -> str | None:
        """Agent ID for a name given by an agent, None if it names no member.

        Anonymous channels accept only aliases (`Mitglied A`, `A`), never real IDs, so the
        answer reveals nothing about who is behind an alias (W1-03.1).
        """
        if not self.anonymous:
            return name if name in self.members else None
        wanted = name.strip().casefold()
        for agent, alias in self.members.items():
            if alias and wanted in (alias.casefold(), alias.split()[-1].casefold()):
                return agent
        return None


def _alias(index: int) -> str:
    letters = string.ascii_uppercase
    label = letters[index % 26] * (index // 26 + 1)
    return f"Mitglied {label}"


def _channel_name(name: str) -> str:
    return name.strip().removeprefix("#")


def _enum[E: StrEnum](cls: type[E], value: Any, what: str) -> E:
    try:
        return cls(value)
    except ValueError:
        allowed = ", ".join(m.value for m in cls)
        raise RoomError(f"Unbekannte {what} „{value}“. Erlaubt: {allowed}.") from None


def _now() -> float:
    return time.time()


_HEADING_RE = re.compile(r"^(\s*)(#)")


def _quote(body: str) -> str:
    """A message body as a Markdown quote; `#` at a line start is escaped.

    No line of a body can then look like the heading of another message in the export.
    """
    return "\n".join(
        "> " + _HEADING_RE.sub(r"\1\\\2", line) if line.strip() else ">"
        for line in body.splitlines() or [""]
    )


def _format_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_message(row: sqlite3.Row) -> Message:
    delivery = json.loads(row["delivery"]) if row["delivery"] else None
    return Message(
        id=row["id"],
        ts=row["ts"],
        channel=row["channel"],
        sender=row["sender"],
        kind=MessageKind(row["kind"]),
        body=row["body"],
        to=json.loads(row["to_agents"] or "[]"),
        reply_to=row["reply_to"],
        refs=[Attachment.model_validate(a) for a in json.loads(row["refs"] or "[]")],
        pinned=bool(row["pinned"]),
        open=bool(row["open"]),
        via=MessageVia(row["via"]) if row["via"] else None,
        delivery=Delivery.model_validate(delivery) if delivery else None,
    )


def _row_to_result(row: sqlite3.Row) -> Result:
    return Result(
        id=row["id"],
        ts=row["ts"],
        agent=row["agent"],
        stage=row["stage"],
        kind=row["kind"],
        title=row["title"],
        body=row["body"],
        path=row["path"],
        attempt=row["attempt"],
    )


def _row_to_rating(row: sqlite3.Row) -> Rating:
    return Rating(
        id=row["id"],
        ts=row["ts"],
        rater=row["rater"],
        result_id=row["result_id"],
        score=row["score"],
        criteria=json.loads(row["criteria"] or "{}"),
        comment=row["comment"] or "",
    )


def _row_to_vote(row: sqlite3.Row) -> Vote:
    return Vote(
        proposal_id=row["proposal_id"],
        agent=row["agent"],
        choice=VoteChoice(row["choice"]),
        reason=row["reason"] or "",
        ts=row["ts"],
    )


def _row_to_assignment(row: sqlite3.Row) -> Assignment:
    return Assignment(
        agent=row["agent"],
        turn=row["turn"],
        stage=row["stage"],
        prompt=row["prompt"],
        context=json.loads(row["context"] or "{}"),
        ts=row["ts"],
    )


class RoomStore:
    """The group chat database of one run: `<run>/groupchats/room.sqlite` (docs/02 §4-§5).

    Used by the RunPreparer (channels), the orchestrator and the GUI (writes as
    `orchestrator`/`user`, reads with real agent IDs) and, via `for_agent`, by the room
    server of each agent. Open an existing room with `RoomStore(run_dir)`, create one with
    `RoomStore.create(run_dir)`.
    """

    def __init__(self, run_dir: Path, *, busy_timeout_s: float = BUSY_TIMEOUT_S) -> None:
        self.run_dir = Path(run_dir).absolute()
        self.db_path = self.run_dir / ROOM_DIR / DB_NAME
        self.attachments_dir = self.run_dir / ROOM_DIR / ATTACHMENTS_DIR
        self.busy_timeout_s = busy_timeout_s
        if not self.db_path.is_file():
            raise ArrError(
                f"Die Gruppenchat-Datenbank fehlt: {self.db_path}. "
                "Sie wird beim Vorbereiten des Runs angelegt."
            )

    @classmethod
    def create(cls, run_dir: Path, *, busy_timeout_s: float = BUSY_TIMEOUT_S) -> "RoomStore":
        """Create `groupchats/` with the schema (idempotent) and return the store."""
        db_path = Path(run_dir) / ROOM_DIR / DB_NAME
        (db_path.parent / ATTACHMENTS_DIR).mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path, timeout=busy_timeout_s, isolation_level=None)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
        finally:
            con.close()
        return cls(run_dir, busy_timeout_s=busy_timeout_s)

    # ----------------------------------------------------------------------------------------
    # Connections and transactions
    # ----------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # Autocommit mode: transactions are explicit (S4 §4.1).
        con = sqlite3.connect(self.db_path, timeout=self.busy_timeout_s, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_s * 1000)}")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Write transaction: `BEGIN IMMEDIATE` takes the write lock before the first read."""
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Read-only transaction for a consistent snapshot; it never upgrades to a write."""
        con = self._connect()
        try:
            con.execute("BEGIN")
            yield con
        finally:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()

    # ----------------------------------------------------------------------------------------
    # Channels
    # ----------------------------------------------------------------------------------------

    def create_channel(
        self,
        config: GroupChatConfig,
        *,
        roles: Mapping[str, str] | None = None,
        brief: str | None = None,
    ) -> Message:
        """Create a channel with resolved members and post its pinned brief (docs/06 §2).

        `roles` maps agent IDs to a short role text for the brief. Without `brief` the text
        is generated from purpose, members, roles, mode, rules and end condition. A channel
        with `open_from` starts in state `waiting`. Returns the brief message.
        """
        members = list(dict.fromkeys(config.members))
        for agent in members:
            if agent in RESERVED_SENDERS or "*" in agent:
                raise ArrError(f"Kanal „{config.name}“: „{agent}“ ist keine aufgelöste Agent-ID.")
        if config.moderator and config.moderator not in members:
            raise ArrError(
                f"Kanal „{config.name}“: Der Moderator „{config.moderator}“ ist kein Mitglied."
            )
        aliases = {
            agent: (_alias(i) if config.anonymous else None) for i, agent in enumerate(members)
        }
        state = ChannelState.WAITING if config.open_from else ChannelState.OPEN
        text = brief if brief is not None else self._brief(config, members, aliases, roles or {})
        with self._write() as con:
            if con.execute("SELECT 1 FROM channel WHERE name=?", (config.name,)).fetchone():
                raise ArrError(f"Den Kanal „{config.name}“ gibt es in diesem Run schon.")
            con.execute(
                "INSERT INTO channel(name, created, config, state) VALUES(?,?,?,?)",
                (config.name, _now(), config.model_dump_json(), state.value),
            )
            con.executemany(
                "INSERT INTO member(channel, agent, alias) VALUES(?,?,?)",
                [(config.name, a, aliases[a]) for a in members],
            )
            msg_id = self._insert_msg(
                con, config.name, ORCHESTRATOR, MessageKind.NOTE, text, pinned=True
            )
            return self._load_msg(con, msg_id)

    def setup_channels(
        self, groupchats: Sequence[GroupChatConfig], roles: Mapping[str, str] | None = None
    ) -> list[Message]:
        """Create all channels of a resolved run (docs/02 §7 `preparing`); returns the briefs."""
        return [self.create_channel(gc, roles=roles) for gc in groupchats]

    @staticmethod
    def _brief(
        config: GroupChatConfig,
        members: list[str],
        aliases: Mapping[str, str | None],
        roles: Mapping[str, str],
    ) -> str:
        lines = [f"Steckbrief #{config.name}"]
        if config.purpose:
            lines.append(f"Zweck: {config.purpose}")
        lines.append("Mitglieder:")
        for agent in members:
            shown = aliases[agent] if config.anonymous else agent
            extra = [roles[agent]] if agent in roles else []
            if agent == config.moderator:
                extra.append("Moderator")
            lines.append(f"- {shown}" + (f" ({', '.join(extra)})" if extra else ""))
        if config.anonymous:
            lines.append("Der Kanal ist anonym: Mitglieder erscheinen nur als „Mitglied A/B/…“.")
        lines.append(f"Taktung: {_MODE_TEXT[config.mode]}")
        lines.append(f"Länge einer Nachricht: höchstens {config.max_message_chars} Zeichen.")
        lines.append("Gesprächsregeln:")
        lines += [f"{i}. {rule}" for i, rule in enumerate(CONVERSATION_RULES, 1)]
        lines.append(f"Ende: {_END_TEXT[config.mode]}")
        return "\n".join(lines)

    def set_channel_state(self, channel: str, state: ChannelState) -> None:
        """Open a `waiting` channel (`open_from` reached) or close one (orchestrator)."""
        with self._write() as con:
            if not self._set_channel_state(con, channel, state):
                raise ArrError(f"Den Kanal „{channel}“ gibt es nicht.")

    @staticmethod
    def _set_channel_state(con: sqlite3.Connection, channel: str, state: ChannelState) -> bool:
        cur = con.execute("UPDATE channel SET state=? WHERE name=?", (state.value, channel))
        if state is ChannelState.CLOSED:
            # Nobody can answer in a closed channel, so its questions are no longer open.
            con.execute("UPDATE msg SET open=0 WHERE channel=? AND open=1", (channel,))
        return cur.rowcount > 0

    def rubric(self) -> Rubric:
        """Rubric of the run from `run.yaml` (docs/03 §3); the default scale without one."""
        if not hasattr(self, "_rubric"):
            run_yaml = self.run_dir / "run.yaml"
            run = load_yaml_model(run_yaml, ResolvedRun) if run_yaml.is_file() else None
            self._rubric = run.rubric if run and run.rubric else Rubric()
        return self._rubric

    def open_channel(self, channel: str) -> None:
        """Open a channel whose `open_from` stage has been reached (W2-03)."""
        self.set_channel_state(channel, ChannelState.OPEN)

    def _load_channel(self, con: sqlite3.Connection, name: str) -> _Channel | None:
        row = con.execute("SELECT config, state FROM channel WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        members = {
            r["agent"]: r["alias"]
            for r in con.execute(
                "SELECT agent, alias FROM member WHERE channel=? ORDER BY rowid", (name,)
            )
        }
        return _Channel(
            name=name,
            config=GroupChatConfig.model_validate_json(row["config"]),
            state=ChannelState(row["state"]),
            members=members,
        )

    def channel_names(self) -> list[str]:
        """Names of all channels in creation order."""
        with self._read() as con:
            return [r[0] for r in con.execute("SELECT name FROM channel ORDER BY created, name")]

    def channel_config(self, channel: str) -> GroupChatConfig:
        """Configuration of a channel as created."""
        with self._read() as con:
            ch = self._load_channel(con, channel)
        if ch is None:
            raise ArrError(f"Den Kanal „{channel}“ gibt es nicht.")
        return ch.config

    def channel_state(self, channel: str) -> ChannelState:
        """Current state of a channel."""
        with self._read() as con:
            ch = self._load_channel(con, channel)
        if ch is None:
            raise ArrError(f"Den Kanal „{channel}“ gibt es nicht.")
        return ch.state

    def channels(self) -> list[ChannelInfo]:
        """All channels with real member IDs and aliases, for the GUI (docs/08 §5.0)."""
        with self._read() as con:
            infos = []
            for name in [r[0] for r in con.execute("SELECT name FROM channel ORDER BY created")]:
                ch = self._load_channel(con, name)
                assert ch is not None
                count = con.execute("SELECT count(*) FROM msg WHERE channel=?", (name,))
                infos.append(
                    ChannelInfo(
                        name=name,
                        purpose=ch.config.purpose,
                        members=[ChannelMember(agent=a, alias=al) for a, al in ch.members.items()],
                        mode=ch.config.mode,
                        moderator=ch.config.moderator,
                        open=ch.state is ChannelState.OPEN,
                        open_from=ch.config.open_from,
                        anonymous=ch.config.anonymous,
                        message_count=count.fetchone()[0],
                    )
                )
            return infos

    # ----------------------------------------------------------------------------------------
    # Messages
    # ----------------------------------------------------------------------------------------

    def _insert_msg(
        self,
        con: sqlite3.Connection,
        channel: str,
        sender: str,
        kind: MessageKind,
        body: str,
        *,
        to: Sequence[str] = (),
        reply_to: int | None = None,
        refs: Sequence[Attachment] = (),
        pinned: bool = False,
        via: MessageVia | None = None,
        delivery: Delivery | None = None,
        msg_id: int | None = None,
    ) -> int:
        is_open = kind is MessageKind.QUESTION and bool(to)
        cur = con.execute(
            "INSERT INTO msg(id, ts, sender, channel, kind, body, to_agents, reply_to, refs,"
            " pinned, open, via, delivery) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                msg_id,
                _now(),
                sender,
                channel,
                kind.value,
                body,
                json.dumps(list(to)),
                reply_to,
                json.dumps([a.model_dump() for a in refs]),
                int(pinned),
                int(is_open),
                via.value if via else None,
                delivery.model_dump_json() if delivery else None,
            ),
        )
        assert cur.lastrowid is not None
        if kind is MessageKind.ANSWER and reply_to is not None:
            # An answer by an addressee closes the open question (docs/06 §3).
            row = con.execute(
                "SELECT to_agents FROM msg WHERE id=? AND kind='question' AND open=1",
                (reply_to,),
            ).fetchone()
            if row and sender in json.loads(row["to_agents"]):
                con.execute("UPDATE msg SET open=0 WHERE id=?", (reply_to,))
        return cur.lastrowid

    def _load_msg(self, con: sqlite3.Connection, msg_id: int) -> Message:
        row = con.execute("SELECT * FROM msg WHERE id=?", (msg_id,)).fetchone()
        if row is None:
            raise ArrError(f"Die Nachricht #{msg_id} gibt es nicht.")
        return _row_to_message(row)

    def post(
        self,
        channel: str,
        body: str,
        *,
        sender: Literal["orchestrator", "user"] = ORCHESTRATOR,
        kind: MessageKind = MessageKind.NOTE,
        to: Sequence[str] = (),
        reply_to: int | None = None,
        via: MessageVia | None = None,
        pinned: bool = False,
    ) -> Message:
        """Write as `orchestrator` or `user` (docs/06 §10), in every channel and state.

        Messages from `user` need `via` and start with delivery `pending`; the orchestrator
        updates it with `set_delivery`. `to` holds real agent IDs of channel members.
        """
        if sender not in RESERVED_SENDERS:
            raise ValueError(f"post() writes as orchestrator or user, not as {sender!r}")
        kind = MessageKind(kind)
        via = MessageVia(via) if via is not None else None
        if sender == USER and via is None:
            raise ValueError("messages from user need `via`")
        if sender == ORCHESTRATOR and via is not None:
            raise ValueError("`via` is only for messages from user")
        delivery = Delivery(status=DeliveryStatus.PENDING, ts=_now()) if sender == USER else None
        with self._write() as con:
            ch = self._load_channel(con, channel)
            if ch is None:
                raise ArrError(f"Den Kanal „{channel}“ gibt es nicht.")
            unknown = [a for a in to if a not in ch.members]
            if unknown:
                raise ArrError(
                    f"Kein Mitglied von #{channel}: {', '.join(unknown)}. "
                    f"Mitglieder: {', '.join(ch.members)}."
                )
            if sender == USER:  # `@name` of the user addresses like `to` (docs/09 §4.1)
                named = mentions(body, ch.members, USER, lambda n: n if n in ch.members else None)
                to = [*to, *named]
            if reply_to is not None:
                self._check_reply_to(con, ch.name, reply_to, ArrError)
            msg_id = self._insert_msg(
                con,
                channel,
                sender,
                kind,
                body,
                to=list(dict.fromkeys(to)),
                reply_to=reply_to,
                pinned=pinned,
                via=via,
                delivery=delivery,
            )
            return self._load_msg(con, msg_id)

    @staticmethod
    def _check_reply_to(
        con: sqlite3.Connection, channel: str, reply_to: int, error: type[Exception]
    ) -> sqlite3.Row:
        row = con.execute("SELECT * FROM msg WHERE id=?", (reply_to,)).fetchone()
        if row is None or row["channel"] != channel:
            raise error(f"Die Nachricht #{reply_to} gibt es in #{channel} nicht.")
        return row

    def set_delivery(self, msg_id: int, status: DeliveryStatus) -> Message:
        """Update the delivery state of a `user` message (docs/08 §5.0)."""
        delivery = Delivery(status=status, ts=_now())
        with self._write() as con:
            row = con.execute("SELECT sender FROM msg WHERE id=?", (msg_id,)).fetchone()
            if row is None or row["sender"] != USER:
                raise ArrError(f"Die Nachricht #{msg_id} ist keine Nachricht von „user“.")
            con.execute(
                "UPDATE msg SET delivery=? WHERE id=?", (delivery.model_dump_json(), msg_id)
            )
            return self._load_msg(con, msg_id)

    def pin(self, msg_id: int, pinned: bool = True) -> None:
        """Pin or unpin a message, e.g. the running summary „Stand der Diskussion“ (docs/06 §8)."""
        with self._write() as con:
            cur = con.execute("UPDATE msg SET pinned=? WHERE id=?", (int(pinned), msg_id))
            if cur.rowcount == 0:
                raise ArrError(f"Die Nachricht #{msg_id} gibt es nicht.")

    def message(self, msg_id: int) -> Message:
        """One message with the real sender ID and, in anonymous channels, its alias."""
        with self._read() as con:
            msg = self._load_msg(con, msg_id)
            ch = self._load_channel(con, msg.channel)
        return self._with_alias(msg, ch)

    def messages(
        self, channel: str, *, after_id: int = 0, limit: int | None = None
    ) -> list[Message]:
        """Messages of a channel in order, with real IDs and aliases (GUI, export)."""
        sql = "SELECT * FROM msg WHERE channel=? AND id>? ORDER BY id"
        args: tuple[Any, ...] = (channel, after_id)
        if limit is not None:
            sql += " LIMIT ?"
            args += (limit,)
        with self._read() as con:
            ch = self._load_channel(con, channel)
            if ch is None:
                raise ArrError(f"Den Kanal „{channel}“ gibt es nicht.")
            return [self._with_alias(_row_to_message(r), ch) for r in con.execute(sql, args)]

    def messages_after(self, after_id: int = 0) -> list[Message]:
        """Messages of all channels with an ID above `after_id`, in order (orchestrator)."""
        with self._read() as con:
            rows = con.execute("SELECT * FROM msg WHERE id>? ORDER BY id", (after_id,)).fetchall()
        return [_row_to_message(r) for r in rows]

    @staticmethod
    def _with_alias(msg: Message, ch: _Channel | None) -> Message:
        if ch is None or not ch.anonymous or msg.sender not in ch.members:
            return msg
        return msg.model_copy(update={"sender_alias": ch.members[msg.sender]})

    def cursor(self, agent: str, channel: str) -> int:
        """Last message ID the agent has read in the channel (0 = nothing)."""
        with self._read() as con:
            row = con.execute(
                "SELECT last_id FROM cursor WHERE agent=? AND channel=?", (agent, channel)
            ).fetchone()
        return row[0] if row else 0

    # ----------------------------------------------------------------------------------------
    # Votes, floor, agent state, assignments, results, ratings
    # ----------------------------------------------------------------------------------------

    def votes(self, proposal_id: int) -> list[Vote]:
        """All current votes on a proposal."""
        with self._read() as con:
            rows = con.execute(
                "SELECT * FROM vote WHERE proposal_id=? ORDER BY ts", (proposal_id,)
            ).fetchall()
        return [_row_to_vote(r) for r in rows]

    def vote_rule(self, proposal_id: int) -> VoteRule | None:
        """Rule of a vote started with `call_vote`, None if none was started."""
        with self._read() as con:
            row = con.execute(
                "SELECT rule FROM poll WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return VoteRule(row[0]) if row else None

    def floor_requests(self, *, pending_only: bool = True) -> list[FloorRequest]:
        """`give_floor` requests of moderators; the orchestrator turns them into turns."""
        sql = "SELECT * FROM floor" + (" WHERE handled=0" if pending_only else "") + " ORDER BY id"
        with self._read() as con:
            rows = con.execute(sql).fetchall()
        return [
            FloorRequest(
                id=r["id"],
                ts=r["ts"],
                channel=r["channel"],
                moderator=r["moderator"],
                agent=r["agent"],
                prompt=r["prompt"],
                msg_id=r["msg_id"],
                handled=bool(r["handled"]),
            )
            for r in rows
        ]

    def mark_floor_handled(self, request_id: int) -> None:
        """Mark a floor request as turned into a turn."""
        with self._write() as con:
            con.execute("UPDATE floor SET handled=1 WHERE id=?", (request_id,))

    def _set_state(
        self, con: sqlite3.Connection, agent: str, status: AgentStatus, note: str
    ) -> None:
        con.execute(
            "INSERT INTO agent_state(agent, status, wakeups, note, ts) VALUES(?,?,0,?,?) "
            "ON CONFLICT(agent) DO UPDATE SET status=excluded.status, note=excluded.note, "
            "ts=excluded.ts",
            (agent, status.value, note, _now()),
        )

    def set_agent_status(self, agent: str, status: AgentStatus, note: str = "") -> None:
        """Set any status of an agent, e.g. `idle` or `blocked` (orchestrator, docs/06 §5.2)."""
        with self._write() as con:
            self._set_state(con, agent, status, note)

    def add_wakeup(self, agent: str) -> int:
        """Count one wake-up of an idle agent; returns the new count (`max_wakeups_per_agent`)."""
        with self._write() as con:
            con.execute(
                "INSERT INTO agent_state(agent, status, wakeups, note, ts) VALUES(?,?,1,'',?) "
                "ON CONFLICT(agent) DO UPDATE SET wakeups=wakeups+1, ts=excluded.ts",
                (agent, AgentStatus.IDLE.value, _now()),
            )
            row = con.execute("SELECT wakeups FROM agent_state WHERE agent=?", (agent,))
            return row.fetchone()[0]

    def agent_states(self) -> list[AgentStatusRecord]:
        """All rows of `agent_state`."""
        with self._read() as con:
            rows = con.execute("SELECT * FROM agent_state ORDER BY agent").fetchall()
        return [
            AgentStatusRecord(
                agent=r["agent"],
                status=AgentStatus(r["status"]),
                wakeups=r["wakeups"] or 0,
                note=r["note"] or "",
                ts=r["ts"],
            )
            for r in rows
        ]

    def reports(self) -> dict[str, tuple[AgentStatus, float]]:
        """Status each agent reported itself with `set_status`, with its time (docs/09 §4.2)."""
        try:
            with self._read() as con:
                rows = con.execute("SELECT agent, status, ts FROM agent_report").fetchall()
        except sqlite3.OperationalError:  # a room from before W3-07
            return {}
        return {r["agent"]: (AgentStatus(r["status"]), r["ts"]) for r in rows}

    def waits(self, kind: WaitKind) -> dict[str, WaitState]:
        """Current waits of `kind` (`wait_for_messages` or `wait_until`), by agent."""
        try:
            with self._read() as con:
                rows = con.execute("SELECT * FROM agent_wait WHERE kind=?", (kind.value,))
                rows = rows.fetchall()
        except sqlite3.OperationalError:  # a room from before W3-07
            return {}
        return {
            r["agent"]: WaitState(
                agent=r["agent"],
                kind=WaitKind(r["kind"]),
                since=r["since"],
                until=r["until"],
                file=r["file"],
            )
            for r in rows
        }

    def clear_wait(self, agent: str, kind: WaitKind | None = None) -> None:
        """End the wait of an agent (only one of `kind`, if given)."""
        sql, args = "DELETE FROM agent_wait WHERE agent=?", [agent]
        if kind is not None:
            sql, args = sql + " AND kind=?", [*args, kind.value]
        with self._write() as con:
            con.execute(sql, args)

    def wait_guards(self) -> dict[str, WaitGuard]:
        """The limits of waiting per agent (table `wait_guard`, docs/09 §4.3)."""
        try:
            with self._read() as con:
                rows = con.execute("SELECT * FROM wait_guard").fetchall()
        except sqlite3.OperationalError:  # a room from before W3-07
            return {}
        return {r["agent"]: WaitGuard(**dict(r)) for r in rows}

    def set_wait_guard(self, agent: str, **fields: Any) -> None:
        """Set columns of `agent`'s row in `wait_guard` (`WAIT_GUARD_FIELDS`), creating it."""
        unknown = set(fields) - WAIT_GUARD_FIELDS
        if unknown or not fields:
            raise ValueError(f"wait_guard: unknown or no fields {sorted(unknown)}")
        names = sorted(fields)
        with self._write() as con:
            con.execute(
                f"INSERT INTO wait_guard(agent, {', '.join(names)}) "
                f"VALUES(?{', ?' * len(names)}) ON CONFLICT(agent) DO UPDATE SET "
                + ", ".join(f"{n}=excluded.{n}" for n in names),
                (agent, *(fields[n] for n in names)),
            )

    def reset_wait_guard(self, agent: str) -> None:
        """A new turn: the time waited in the turn, its last end and an end request start
        over; `yield_to` and `until_streak` belong to the orchestrator and stay."""
        self.set_wait_guard(agent, waited=0.0, seen=None, last_end=None, end_turn=None)

    def open_questions_by(self, agent: str) -> list[Message]:
        """Questions `agent` asked that are still open (no `answer` of the addressee yet)."""
        with self._read() as con:
            rows = con.execute(
                "SELECT * FROM msg WHERE sender=? AND open=1 ORDER BY id", (agent,)
            ).fetchall()
        return [_row_to_message(r) for r in rows]

    def add_assignment(
        self,
        agent: str,
        turn: int,
        prompt: str,
        *,
        stage: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Assignment:
        """Store the assignment of a turn so the agent can re-read it (docs/03 §4.5).

        `context["attempt"]` (int) becomes the `attempt` of results submitted in this turn.
        """
        assignment = Assignment(
            agent=agent,
            turn=turn,
            stage=stage,
            prompt=prompt,
            context=dict(context or {}),
            ts=_now(),
        )
        with self._write() as con:
            con.execute(
                "INSERT INTO assignment(agent, turn, stage, prompt, context, ts) "
                "VALUES(?,?,?,?,?,?)",
                (agent, turn, stage, prompt, json.dumps(assignment.context), assignment.ts),
            )
        return assignment

    def assignment(self, agent: str) -> Assignment | None:
        """Latest assignment of an agent."""
        with self._read() as con:
            return self._latest_assignment(con, agent)

    @staticmethod
    def _latest_assignment(con: sqlite3.Connection, agent: str) -> Assignment | None:
        row = con.execute(
            "SELECT * FROM assignment WHERE agent=? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (agent,),
        ).fetchone()
        return _row_to_assignment(row) if row else None

    def results(self, *, stage: str | None = None, agent: str | None = None) -> list[Result]:
        """Submitted results in order, optionally filtered."""
        sql, args = "SELECT * FROM result WHERE 1=1", []
        if stage is not None:
            sql, args = sql + " AND stage=?", [*args, stage]
        if agent is not None:
            sql, args = sql + " AND agent=?", [*args, agent]
        with self._read() as con:
            return [_row_to_result(r) for r in con.execute(sql + " ORDER BY id", args)]

    def result(self, result_id: int) -> Result:
        """One result by ID."""
        with self._read() as con:
            row = con.execute("SELECT * FROM result WHERE id=?", (result_id,)).fetchone()
        if row is None:
            raise ArrError(f"Das Ergebnis #{result_id} gibt es nicht.")
        return _row_to_result(row)

    def ratings(self, *, result_id: int | None = None) -> list[Rating]:
        """Submitted ratings in order, optionally for one result."""
        sql, args = "SELECT * FROM rating", ()
        if result_id is not None:
            sql, args = sql + " WHERE result_id=?", (result_id,)
        with self._read() as con:
            return [_row_to_rating(r) for r in con.execute(sql + " ORDER BY id", args)]

    # ----------------------------------------------------------------------------------------
    # Export
    # ----------------------------------------------------------------------------------------

    def export_channel(self, channel: str, dest: Path | None = None) -> ChannelExport:
        """Write `<kanal>.md` (readable) and `<kanal>.jsonl` (one `Message` per line).

        Default destination is `groupchats/` of the run (docs/02 §4, §5). Messages carry real
        sender IDs; in anonymous channels the alias is in `sender_alias`.
        """
        dest = Path(dest) if dest is not None else self.run_dir / ROOM_DIR
        dest.mkdir(parents=True, exist_ok=True)
        info = next((c for c in self.channels() if c.name == channel), None)
        if info is None:
            raise ArrError(f"Den Kanal „{channel}“ gibt es nicht.")
        msgs = self.messages(channel)
        jsonl = dest / f"{channel}.jsonl"
        jsonl.write_text("".join(m.model_dump_json() + "\n" for m in msgs), encoding="utf-8")
        markdown = dest / f"{channel}.md"
        markdown.write_text(
            self._render_markdown(info, msgs, self._link_base(dest)), encoding="utf-8"
        )
        return ChannelExport(channel=channel, markdown=markdown, jsonl=jsonl, messages=len(msgs))

    def export_all(self, dest: Path | None = None) -> list[ChannelExport]:
        """Export every channel (Finalizer, docs/02 §7 `finalizing`)."""
        return [self.export_channel(name, dest) for name in self.channel_names()]

    def _link_base(self, dest: Path) -> str:
        """Prefix that turns a run-relative path into a link relative to `dest`."""
        rel = os.path.relpath(self.run_dir, dest.absolute())
        return "" if rel == "." else Path(rel).as_posix() + "/"

    @staticmethod
    def _render_markdown(info: ChannelInfo, msgs: list[Message], link_base: str) -> str:
        def who(agent: str, alias: str | None) -> str:
            return f"{agent} ({alias})" if alias else agent

        aliases = {m.agent: m.alias for m in info.members if m.agent}
        lines = [f"# #{info.name}", ""]
        if info.purpose:
            lines += [f"Zweck: {info.purpose}", ""]
        lines.append(f"- Taktung: {info.mode.value}")
        lines.append(
            "- Mitglieder: " + ", ".join(who(m.agent or "?", m.alias) for m in info.members)
        )
        if info.moderator:
            lines.append(f"- Moderator: {info.moderator}")
        if info.anonymous:
            lines.append("- anonym: ja")
        lines.append(f"- Nachrichten: {len(msgs)}")
        for m in msgs:
            head = f"### #{m.id} · {_format_ts(m.ts)} · {who(m.sender, m.sender_alias)} · {m.kind}"
            lines += ["", head, ""]
            meta = []
            if m.to:
                meta.append("an: " + ", ".join(who(a, aliases.get(a)) for a in m.to))
            if m.reply_to is not None:
                meta.append(f"Antwort auf #{m.reply_to}")
            if m.pinned:
                meta.append("angeheftet")
            if m.open:
                meta.append("offene Frage")
            if m.via:
                meta.append(f"über {m.via.value}")
            if m.delivery:
                meta.append(f"Zustellung: {m.delivery.status.value}")
            if meta:
                lines += ["*" + " · ".join(meta) + "*", ""]
            lines.append(_quote(m.body))
            if m.refs:
                lines.append("")
                lines += [f"- Anhang: [{a.name}]({link_base}{a.path})" for a in m.refs]
        return "\n".join(lines) + "\n"

    # ----------------------------------------------------------------------------------------
    # Agent view
    # ----------------------------------------------------------------------------------------

    def for_agent(
        self, agent_id: str, work_dir: Path | None = None, *, rubric: Rubric | None = None
    ) -> "AgentRoom":
        """View of one research agent; `work_dir` defaults to the folder from `agent.json`.

        `rubric` defaults to the rubric of `run.yaml` (checked by `submit_rating`).
        """
        return AgentRoom(
            self,
            agent_id,
            work_dir or agent_work_dir(self.run_dir, agent_id),
            rubric=rubric or self.rubric(),
        )


def agent_work_dir(run_dir: Path, agent_id: str) -> Path:
    """Working folder of an agent: `work_dir` of its `agent.json` (`AgentFile`, docs/02 §4).

    That is `shared/<name>/` or `agents/<id>/work/`; without `agent.json` the latter.
    """
    run_dir = Path(run_dir)
    if (run_dir / "agents" / agent_id / "agent.json").is_file():
        work_dir = run_dir / load_agent_file(run_dir, agent_id).work_dir
        if not work_dir.resolve().is_relative_to(run_dir.resolve()):
            raise ArrError(f"Der Arbeitsordner von „{agent_id}“ liegt außerhalb des Runs.")
        return work_dir
    return run_dir / "agents" / agent_id / "work"


class AgentRoom:
    """Room tools bound to one agent; implements `RoomAPI` (docs/06-gruppenchats.md §9).

    Reading and writing only in own channels, not in `waiting` channels, writing not in
    `closed` ones. Anonymous channels show aliases instead of agent IDs. Expected failures
    raise `RoomError` with a German message.
    """

    def __init__(
        self, store: RoomStore, agent_id: str, work_dir: Path, *, rubric: Rubric | None = None
    ) -> None:
        if agent_id in RESERVED_SENDERS:
            raise ValueError(f"{agent_id!r} is reserved and cannot act as an agent")
        self.store = store
        self._agent_id = agent_id
        self.work_dir = Path(work_dir).absolute()
        self.rubric = rubric or Rubric()

    @property
    def agent_id(self) -> str:
        """ID of the agent this view acts for."""
        return self._agent_id

    # ----------------------------------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------------------------------

    def _my_channels(self, con: sqlite3.Connection) -> list[_Channel]:
        names = [
            r[0]
            for r in con.execute(
                "SELECT m.channel FROM member m JOIN channel c ON c.name=m.channel "
                "WHERE m.agent=? ORDER BY c.created, c.name",
                (self.agent_id,),
            )
        ]
        return [ch for n in names if (ch := self.store._load_channel(con, n)) is not None]

    def _readable(self, con: sqlite3.Connection) -> list[_Channel]:
        return [ch for ch in self._my_channels(con) if ch.state is not ChannelState.WAITING]

    def _channel(self, con: sqlite3.Connection, name: str, *, write: bool = False) -> _Channel:
        name = _channel_name(name)
        ch = self.store._load_channel(con, name)
        if ch is None or self.agent_id not in ch.members:
            mine = ", ".join(f"#{c.name}" for c in self._my_channels(con)) or "keine"
            if ch is None:
                raise RoomError(f"Den Kanal #{name} gibt es nicht. Deine Kanäle: {mine}.")
            raise RoomError(f"Du bist nicht Mitglied im Kanal #{name}. Deine Kanäle: {mine}.")
        if ch.state is ChannelState.WAITING:
            raise RoomError(
                f"Der Kanal #{name} ist noch geschlossen. "
                f"Er öffnet ab Phase „{ch.config.open_from}“."
            )
        if write and ch.state is ChannelState.CLOSED:
            raise RoomError(
                f"Die Diskussion in #{name} ist beendet. Du kannst den Kanal noch lesen, "
                "aber nicht mehr hineinschreiben."
            )
        return ch

    def _message_in_my_channel(
        self, con: sqlite3.Connection, msg_id: int, *, write: bool = False
    ) -> tuple[sqlite3.Row, _Channel]:
        row = con.execute("SELECT * FROM msg WHERE id=?", (msg_id,)).fetchone()
        if row is None:
            raise RoomError(f"Die Nachricht #{msg_id} gibt es nicht.")
        ch = self.store._load_channel(con, row["channel"])
        if ch is None or self.agent_id not in ch.members:
            raise RoomError(f"Die Nachricht #{msg_id} gibt es in deinen Kanälen nicht.")
        return row, self._channel(con, ch.name, write=write)

    def _view(self, msg: Message, ch: _Channel) -> Message:
        """Message as this agent sees it: aliases instead of IDs in anonymous channels."""
        if not ch.anonymous:
            return msg
        alias = ch.members.get(msg.sender)
        return msg.model_copy(
            update={
                "sender": ch.display(msg.sender),
                "sender_alias": alias,
                "to": [ch.display(a) for a in msg.to],
            }
        )

    def _moderated(self, con: sqlite3.Connection) -> list[_Channel]:
        return [ch for ch in self._my_channels(con) if ch.config.moderator == self.agent_id]

    def _moderated_channel(
        self, con: sqlite3.Connection, channel: str | None, tool: str
    ) -> _Channel:
        moderated = self._moderated(con)
        if not moderated:
            raise RoomError(f"„{tool}“ darf nur der Moderator eines Kanals benutzen.")
        if channel is None:
            if len(moderated) > 1:
                names = ", ".join(f"#{c.name}" for c in moderated)
                raise RoomError(f"Du moderierst mehrere Kanäle ({names}). Gib „channel“ an.")
            return self._channel(con, moderated[0].name, write=True)
        ch = self._channel(con, channel, write=True)
        if ch.config.moderator != self.agent_id:
            raise RoomError(
                f"Du bist nicht Moderator von #{ch.name}. „{tool}“ ist dort nicht erlaubt."
            )
        return ch

    def moderated_channels(self) -> list[str]:
        """Channels this agent moderates (the server offers moderator tools only then)."""
        with self.store._read() as con:
            return [ch.name for ch in self._moderated(con)]

    @staticmethod
    def _source_parts(rel: str) -> list[str]:
        if "\0" in rel:
            raise RoomError(
                "Der Pfad enthält ein Nullzeichen. Gib einen gewöhnlichen Pfad relativ zu "
                "deinem Arbeitsordner an, z. B. „notes/lemma.md“."
            )
        if not rel or rel.startswith("/"):
            raise RoomError(
                f"„{rel}“: Gib den Pfad relativ zu deinem Arbeitsordner an, z. B. „notes/lemma.md“."
            )
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise RoomError(f"„{rel}“ liegt nicht in deinem Arbeitsordner.")
        return parts

    @staticmethod
    def _open_error(
        rel: str, dir_fd: int, walked: list[str], exc: OSError, *, final: bool
    ) -> RoomError:
        """German message for a failed open; `lstat` only chooses the text, it checks nothing."""
        part = walked[-1]
        try:
            is_link = stat.S_ISLNK(os.stat(part, dir_fd=dir_fd, follow_symlinks=False).st_mode)
        except OSError:
            is_link = False
        log.info("open %r failed at %r: %s", rel, "/".join(walked), exc.strerror)
        if (is_link or exc.errno == errno.ELOOP) and not final:
            return RoomError(
                f"„{rel}“ führt über den Verweis (Symlink) „{'/'.join(walked)}“. "
                "Gib den Pfad zur eigentlichen Datei an."
            )
        return AgentRoom._errno_message(rel, errno.ELOOP if is_link else exc.errno)

    @staticmethod
    def _errno_message(rel: str, code: int | None) -> RoomError:
        """German message for an `errno`, without the English system text (W1-03.2)."""
        if code in (errno.EACCES, errno.EPERM):
            return RoomError(f"Für „{rel}“ fehlen dir die Leserechte.")
        if code in (errno.ENOENT, errno.ENOTDIR):
            return RoomError(f"Die Datei „{rel}“ gibt es in deinem Arbeitsordner nicht.")
        if code == errno.ELOOP:
            return RoomError(f"„{rel}“ ist ein Verweis (Symlink). Hänge die eigentliche Datei an.")
        if code == errno.EISDIR:
            return RoomError(f"„{rel}“ ist ein Ordner, keine Datei. Hänge nur Dateien an.")
        return RoomError(f"Die Datei „{rel}“ lässt sich nicht lesen.")

    def _open_work_dir(self) -> int:
        """Open the working folder without following links (W1-03.2).

        Below the run folder every component from the resolved run folder on is opened with
        `O_NOFOLLOW`; a working folder elsewhere (given explicitly) at least in its last one.
        """
        run_dir = self.store.run_dir
        if self.work_dir.is_relative_to(run_dir):
            anchor, parts = run_dir.resolve(), self.work_dir.relative_to(run_dir).parts
        else:
            anchor, parts = self.work_dir.parent.resolve(), (self.work_dir.name,)
        try:
            fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            log.warning("cannot open anchor of work dir %s: %s", anchor, exc.strerror)
            raise RoomError("Dein Arbeitsordner ist nicht lesbar.") from None
        for part in parts:
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                os.close(fd)
                log.warning("work dir %s refused at %r: %s", self.work_dir, part, exc.strerror)
                if exc.errno in (errno.EACCES, errno.EPERM):
                    raise RoomError("Dein Arbeitsordner ist nicht lesbar.") from None
                raise RoomError(
                    "Dein Arbeitsordner ist ein Verweis (Symlink) oder fehlt. "
                    "Anhänge gehen nur aus dem echten Arbeitsordner."
                ) from None
            os.close(fd)
            fd = next_fd
        return fd

    def _open_source(self, rel: str) -> tuple[int, str]:
        """Open a regular file below the working folder without following any symlink.

        Walks from the working folder component by component (`openat` with `O_NOFOLLOW`),
        so swapping a file or folder for a link between check and copy cannot lead outside
        (TOCTOU, E19). Returns the open descriptor and the file name; the caller copies from
        the descriptor and closes it.
        """
        parts = self._source_parts(rel)
        dir_fd = self._open_work_dir()
        try:
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                try:
                    fd = os.open(part, _FILE_FLAGS if last else _DIR_FLAGS, dir_fd=dir_fd)
                except OSError as exc:
                    raise self._open_error(rel, dir_fd, parts[: i + 1], exc, final=last) from None
                if last:
                    break
                os.close(dir_fd)
                dir_fd = fd
        finally:
            os.close(dir_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RoomError(f"„{rel}“ ist keine normale Datei. Hänge nur Dateien an.")
            if info.st_nlink > 1:
                # A hardlink is a regular file, but may point at a file outside (E19).
                raise RoomError(
                    f"„{rel}“ hat mehrere Verweise (Hardlink). "
                    "Hänge eine eigene Kopie der Datei an."
                )
            self._check_size(rel, info.st_size)
        except BaseException:
            os.close(fd)
            raise
        return fd, parts[-1]

    @staticmethod
    def _check_size(rel: str, size: int) -> None:
        if size > MAX_ATTACHMENT_BYTES:
            raise RoomError(
                f"„{rel}“ ist zu groß ({size // (1024 * 1024)} MB, erlaubt "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB)."
            )

    def _copy_source(self, rel: str, target: Path) -> str:
        """Copy a file of the working folder to `target` via its descriptor; returns its name."""
        fd, name = self._open_source(rel)
        try:
            with os.fdopen(fd, "rb") as src, target.open("xb") as dst:
                copied = 0
                while chunk := src.read(_COPY_CHUNK):
                    copied += len(chunk)
                    self._check_size(rel, copied)  # the file may grow after fstat
                    dst.write(chunk)
        except OSError as exc:
            target.unlink(missing_ok=True)
            log.info("copy of %r failed: %s", rel, exc.strerror)
            raise RoomError(
                f"Die Datei „{rel}“ ließ sich nicht kopieren. Hat sie sich beim Kopieren geändert?"
            ) from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return name

    def unread_counts(self) -> tuple[dict[str, int], int]:
        """Unread messages per readable channel and the number of open questions to me."""
        with self.store._read() as con:
            return self._unread(con), len(self._open_questions(con))

    def _unread(self, con: sqlite3.Connection) -> dict[str, int]:
        counts = {}
        for ch in self._readable(con):
            row = con.execute(
                "SELECT count(*) FROM msg WHERE channel=? AND sender!=? AND id > "
                "coalesce((SELECT last_id FROM cursor WHERE agent=? AND channel=?), 0)",
                (ch.name, self.agent_id, self.agent_id, ch.name),
            ).fetchone()
            counts[ch.name] = row[0]
        return counts

    def _open_questions(self, con: sqlite3.Connection) -> list[Message]:
        result = []
        for ch in self._readable(con):
            for row in con.execute(
                "SELECT * FROM msg WHERE channel=? AND open=1 ORDER BY id", (ch.name,)
            ):
                msg = _row_to_message(row)
                if self.agent_id in msg.to:
                    result.append(self._view(msg, ch))
        return sorted(result, key=lambda m: m.id)

    # ----------------------------------------------------------------------------------------
    # RoomAPI
    # ----------------------------------------------------------------------------------------

    def inbox(self) -> Inbox:
        """Open questions to me, unread messages per channel, pinned messages (docs/06 §4)."""
        with self.store._read() as con:
            pinned = []
            for ch in self._readable(con):
                rows = con.execute(
                    "SELECT * FROM msg WHERE channel=? AND pinned=1 ORDER BY id", (ch.name,)
                )
                pinned += [self._view(_row_to_message(r), ch) for r in rows]
            return Inbox(
                open_questions=self._open_questions(con),
                unread=self._unread(con),
                pinned=pinned,
            )

    def list_channels(self) -> list[ChannelInfo]:
        """Channels the agent is a member of, also `waiting` ones (marked as not open)."""
        with self.store._read() as con:
            unread = self._unread(con)
            infos = []
            for ch in self._my_channels(con):
                members = [
                    ChannelMember(
                        agent=a if not ch.anonymous or a == self.agent_id else None, alias=alias
                    )
                    for a, alias in ch.members.items()
                ]
                count = con.execute("SELECT count(*) FROM msg WHERE channel=?", (ch.name,))
                moderator = ch.config.moderator
                infos.append(
                    ChannelInfo(
                        name=ch.name,
                        purpose=ch.config.purpose,
                        members=members,
                        mode=ch.config.mode,
                        moderator=ch.display(moderator) if moderator else None,
                        open=ch.state is ChannelState.OPEN,
                        open_from=ch.config.open_from,
                        anonymous=ch.anonymous,
                        message_count=count.fetchone()[0],
                        unread=unread.get(ch.name, 0),
                    )
                )
            return infos

    def read_messages(
        self,
        channel: str | None = None,
        only_new: bool = True,
        limit: int = 50,
        thread: int | None = None,
    ) -> list[Message]:
        """Read messages; `only_new` returns unread ones of others and advances the cursor.

        Without `channel` all readable channels are read, merged by ID. With
        `only_new=False` the latest `limit` messages are returned. `thread` returns the
        strand of a message (root and all replies) and leaves the cursor alone.
        """
        if not 1 <= limit <= MAX_READ_LIMIT:
            raise RoomError(f"„limit“ muss zwischen 1 und {MAX_READ_LIMIT} liegen.")
        if thread is not None:
            return self._read_thread(thread, limit)
        # Reading and the cursor update form one write transaction (S4 §3).
        with self.store._write() as con:
            channels = [self._channel(con, channel)] if channel else self._readable(con)
            rows: list[tuple[sqlite3.Row, _Channel]] = []
            for ch in channels:
                cursor = self._cursor(con, ch.name)
                if only_new:
                    found = con.execute(
                        "SELECT * FROM msg WHERE channel=? AND id>? AND sender!=? "
                        "ORDER BY id LIMIT ?",
                        (ch.name, cursor, self.agent_id, limit),
                    ).fetchall()
                else:
                    found = con.execute(
                        "SELECT * FROM (SELECT * FROM msg WHERE channel=? ORDER BY id DESC "
                        "LIMIT ?) ORDER BY id",
                        (ch.name, limit),
                    ).fetchall()
                rows += [(r, ch) for r in found]
            rows.sort(key=lambda item: item[0]["id"])
            rows = rows[:limit] if only_new else rows[-limit:]
            for ch in channels:
                ids = [r["id"] for r, c in rows if c.name == ch.name]
                if not ids:
                    continue
                # The latest messages count as read only if no unread message is older (the
                # cursor must never skip one). New messages are always a prefix of the unread.
                if not only_new and self._unread_before(con, ch.name, min(ids)):
                    continue
                self._advance(con, ch.name, max(ids))
            return [self._view(_row_to_message(r), ch) for r, ch in rows]

    def _cursor(self, con: sqlite3.Connection, channel: str) -> int:
        row = con.execute(
            "SELECT last_id FROM cursor WHERE agent=? AND channel=?", (self.agent_id, channel)
        ).fetchone()
        return row[0] if row else 0

    def _unread_before(self, con: sqlite3.Connection, channel: str, msg_id: int) -> bool:
        row = con.execute(
            "SELECT 1 FROM msg WHERE channel=? AND id>? AND id<? AND sender!=? LIMIT 1",
            (channel, self._cursor(con, channel), msg_id, self.agent_id),
        ).fetchone()
        return row is not None

    def _advance(self, con: sqlite3.Connection, channel: str, last_id: int) -> None:
        """Move the cursor forward, never back (S4 §3: monotonic in the same transaction)."""
        con.execute(
            "INSERT INTO cursor(agent, channel, last_id) VALUES(?,?,?) "
            "ON CONFLICT(agent, channel) DO UPDATE SET last_id=max(last_id, excluded.last_id)",
            (self.agent_id, channel, last_id),
        )

    def _read_thread(self, msg_id: int, limit: int) -> list[Message]:
        with self.store._read() as con:
            row, ch = self._message_in_my_channel(con, msg_id)
            root = row["id"]
            while row["reply_to"] is not None:
                parent = con.execute("SELECT * FROM msg WHERE id=?", (row["reply_to"],)).fetchone()
                if parent is None or parent["channel"] != ch.name:
                    break
                row, root = parent, parent["id"]
            rows = con.execute(
                "WITH RECURSIVE t(id) AS (SELECT ? UNION SELECT m.id FROM msg m JOIN t "
                "ON m.reply_to=t.id WHERE m.channel=?) "
                "SELECT * FROM msg WHERE id IN (SELECT id FROM t) ORDER BY id LIMIT ?",
                (root, ch.name, limit),
            ).fetchall()
            return [self._view(_row_to_message(r), ch) for r in rows]

    def post_message(
        self,
        channel: str,
        body: str,
        kind: MessageKind = MessageKind.NOTE,
        to: list[str] | None = None,
        reply_to: int | None = None,
        refs: list[str] | None = None,
    ) -> Message:
        """Post into an own open channel; `refs` are copied to `attachments/<kanal>/<msg-id>/`.

        Attachments are kept per channel, so each agent can be given read access to exactly
        the attachments of its own channels (E19).
        """
        kind = _enum(MessageKind, kind, "Nachrichtenart")
        if kind is MessageKind.VOTE:
            raise RoomError("Stimmen gibst du mit dem Tool „vote“ ab, nicht als Nachricht.")
        if not body.strip() and kind is not MessageKind.PASS:
            raise RoomError("Die Nachricht ist leer.")
        # Copy before taking the write lock; inside the transaction only a rename remains.
        staging, names = self._stage_attachments(refs or [])
        final_dir: Path | None = None
        try:
            with self.store._write() as con:
                ch = self._channel(con, channel, write=True)
                limit = ch.config.max_message_chars
                if len(body) > limit:
                    raise RoomError(
                        f"Die Nachricht ist zu lang ({len(body)} Zeichen, erlaubt {limit}). "
                        "Lege Längeres als Datei ab und hänge sie mit „refs“ an."
                    )
                if kind is MessageKind.DECISION and ch.config.moderator != self.agent_id:
                    raise RoomError(
                        "Eine „decision“ dürfen nur der Moderator und der Orchestrator posten."
                    )
                targets = self._targets(ch, to or [])
                # `@name` in the text addresses like `to` (docs/09 §4.1)
                targets = list(
                    dict.fromkeys(
                        [*targets, *mentions(body, ch.members, self.agent_id, ch.resolve)]
                    )
                )
                if reply_to is not None:
                    self.store._check_reply_to(con, ch.name, reply_to, RoomError)
                # Inside BEGIN IMMEDIATE nobody else inserts: max+1 is the next ID.
                msg_id = con.execute("SELECT coalesce(max(id), 0) + 1 FROM msg").fetchone()[0]
                attachments = [
                    Attachment(
                        name=name,
                        size=(staging / name).stat().st_size,
                        path=f"{ROOM_DIR}/{ATTACHMENTS_DIR}/{ch.name}/{msg_id}/{name}",
                    )
                    for name in names
                    if staging is not None
                ]
                self.store._insert_msg(
                    con,
                    ch.name,
                    self.agent_id,
                    kind,
                    body,
                    to=targets,
                    reply_to=reply_to,
                    refs=attachments,
                    msg_id=msg_id,
                )
                if staging is not None:
                    final_dir = self.store.attachments_dir / ch.name / str(msg_id)
                    # A leftover folder can only stem from a transaction that never committed.
                    shutil.rmtree(final_dir, ignore_errors=True)
                    final_dir.parent.mkdir(exist_ok=True)
                    staging.rename(final_dir)
                    staging = None
                msg = self._view(self.store._load_msg(con, msg_id), ch)
            final_dir = None  # committed
            return msg
        finally:
            for leftover in (staging, final_dir):
                if leftover is not None:
                    shutil.rmtree(leftover, ignore_errors=True)

    def _targets(self, ch: _Channel, to: list[str]) -> list[str]:
        targets = []
        for name in to:
            agent = ch.resolve(name)
            if agent is None:
                shown = ", ".join(ch.display(a) for a in ch.members if a != self.agent_id)
                raise RoomError(f"„{name}“ ist kein Mitglied von #{ch.name}. Mitglieder: {shown}.")
            if agent == self.agent_id:
                raise RoomError("Du kannst dich nicht selbst adressieren.")
            targets.append(agent)
        return list(dict.fromkeys(targets))

    def _stage_attachments(self, refs: list[str]) -> tuple[Path | None, list[str]]:
        """Copy `refs` into a fresh staging folder; returns it (None without refs) and names."""
        if not refs:
            return None, []
        names = [self._source_parts(rel)[-1] for rel in refs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise RoomError(
                f"Mehrere Anhänge heißen gleich ({', '.join(dupes)}). Benenne sie eindeutig."
            )
        staging = self.store.attachments_dir / f".staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            for rel, name in zip(refs, names, strict=True):
                self._copy_source(rel, staging / name)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return staging, names

    def get_attachment(self, msg_id: int, name: str) -> Path:
        """Absolute path of an attachment of a message in one of my readable channels."""
        with self.store._read() as con:
            row, _ = self._message_in_my_channel(con, msg_id)
        msg = _row_to_message(row)
        for att in msg.refs:
            if att.name == name:
                path = self.store.run_dir / att.path
                if not path.is_file():
                    raise RoomError(f"Der Anhang „{name}“ von #{msg_id} fehlt im Run-Ordner.")
                return path
        names = ", ".join(a.name for a in msg.refs) or "keine"
        raise RoomError(f"Die Nachricht #{msg_id} hat keinen Anhang „{name}“. Anhänge: {names}.")

    def vote(self, proposal_id: int, choice: VoteChoice, reason: str = "") -> Vote:
        """Vote on a `proposal`; a second vote replaces the first. Also posts a `vote` message."""
        choice = _enum(VoteChoice, choice, "Stimme")
        with self.store._write() as con:
            row, ch = self._message_in_my_channel(con, proposal_id, write=True)
            if row["kind"] != MessageKind.PROPOSAL:
                raise RoomError(
                    f"#{proposal_id} ist kein Vorschlag (proposal), sondern „{row['kind']}“."
                )
            vote = Vote(
                proposal_id=proposal_id,
                agent=self.agent_id,
                choice=choice,
                reason=reason,
                ts=_now(),
            )
            con.execute(
                "INSERT INTO vote(proposal_id, agent, choice, reason, ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(proposal_id, agent) DO UPDATE SET choice=excluded.choice, "
                "reason=excluded.reason, ts=excluded.ts",
                (proposal_id, self.agent_id, choice.value, reason, vote.ts),
            )
            body = f"Stimme zu #{proposal_id}: {choice.value}" + (f" – {reason}" if reason else "")
            self.store._insert_msg(
                con, ch.name, self.agent_id, MessageKind.VOTE, body, reply_to=proposal_id
            )
            return vote

    def set_status(self, status: AgentStatus, note: str = "") -> None:
        """Report `working` or `done` (with a short summary)."""
        status = _enum(AgentStatus, status, "Status")
        if status not in AGENT_STATUSES:
            allowed = ", ".join(s.value for s in AGENT_STATUSES)
            raise RoomError(f"Diesen Status setzt arr selbst. Du kannst melden: {allowed}.")
        with self.store._write() as con:
            self.store._set_state(con, self.agent_id, status, note)
            con.execute(
                "INSERT INTO agent_report(agent, status, ts) VALUES(?,?,?) "
                "ON CONFLICT(agent) DO UPDATE SET status=excluded.status, ts=excluded.ts",
                (self.agent_id, status.value, _now()),
            )

    # ----------------------------------------------------------------------------------------
    # Waiting (docs/09 §4.3)
    # ----------------------------------------------------------------------------------------

    def until_file_exists(self, rel: str) -> bool:
        """True if `rel` exists below the working folder; only the existence is checked.

        `rel` must be relative and stay inside (no `/…`, no `..`). Every folder on the way is
        opened with `O_NOFOLLOW` and the last part is looked at with `lstat`: a link anywhere
        raises `RoomError`, so the check never leaves the working folder. Nothing is read,
        nothing is started (SECURITY.md principle 3).
        """
        parts = self._source_parts(rel)
        dir_fd = self._open_work_dir()
        try:
            for i, part in enumerate(parts):
                try:
                    info = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
                except (FileNotFoundError, NotADirectoryError):
                    return False
                except OSError as exc:
                    raise self._errno_message(rel, exc.errno) from None
                if stat.S_ISLNK(info.st_mode):
                    raise RoomError(
                        f"„{rel}“ führt über den Verweis (Symlink) „{'/'.join(parts[: i + 1])}“. "
                        "Warte nur auf Dateien in deinem Arbeitsordner."
                    )
                if i == len(parts) - 1:
                    return True
                if not stat.S_ISDIR(info.st_mode):
                    return False
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    return False  # replaced meanwhile: not there (yet)
                os.close(dir_fd)
                dir_fd = next_fd
            return False
        finally:
            os.close(dir_fd)

    def _check_until_file(self, until_file: str | None) -> str | None:
        if until_file is None or not until_file.strip():
            return None
        self.until_file_exists(until_file)  # rejects absolute paths, `..` and links
        return until_file

    def begin_wait(
        self, kind: WaitKind, seconds: float, until_file: str | None = None
    ) -> WaitState:
        """Record that this agent waits from now for `seconds` (and maybe `until_file`)."""
        until_file = self._check_until_file(until_file)
        now = _now()
        state = WaitState(
            agent=self.agent_id, kind=kind, since=now, until=now + seconds, file=until_file
        )
        with self.store._write() as con:
            con.execute(
                "INSERT INTO agent_wait(agent, kind, since, until, file) VALUES(?,?,?,?,?) "
                "ON CONFLICT(agent, kind) DO UPDATE SET since=excluded.since, "
                "until=excluded.until, file=excluded.file",
                (self.agent_id, kind.value, state.since, state.until, state.file),
            )
        return state

    def end_wait(self, kind: WaitKind = WaitKind.MESSAGES) -> None:
        """End this agent's wait of `kind`."""
        self.store.clear_wait(self.agent_id, kind)

    def wait_until(self, minutes: float, until_file: str | None = None) -> WaitState:
        """Ask to be woken after `minutes` (at most 30), on a message or when `until_file` is
        there; the agent ends its turn afterwards (docs/09 §4.3).

        Refused after `WAIT_UNTIL_REPEAT` of them in a row ended by their deadline (L10).
        """
        if not isinstance(minutes, int | float) or not math.isfinite(minutes) or minutes <= 0:
            raise RoomError("„minutes“ muss eine Zahl größer als 0 sein, z. B. 10.")
        if self.wait_guard().until_streak >= WAIT_UNTIL_REPEAT:
            raise RoomError(
                f"Du hast {WAIT_UNTIL_REPEAT}-mal hintereinander mit `wait_until` gewartet, ohne "
                "dass eine Nachricht an dich oder deine Datei kam. Ein weiteres `wait_until` "
                "lehnt arr ab: Arbeite weiter, gib dein Ergebnis ab oder beende den Turn. arr "
                "weckt dich bei einer Nachricht an dich."
            )
        minutes = min(float(minutes), WAIT_UNTIL_MAX_MIN)
        return self.begin_wait(WaitKind.UNTIL, minutes * 60, until_file)

    def wait_guard(self) -> WaitGuard:
        """My row of `wait_guard` (defaults without one)."""
        return self.store.wait_guards().get(self.agent_id) or WaitGuard(agent=self.agent_id)

    def update_wait_guard(self, **fields: Any) -> None:
        """Set columns of my row of `wait_guard`."""
        self.store.set_wait_guard(self.agent_id, **fields)

    def messages_for_me(self) -> list[Message]:
        """Unread messages in my readable channels that concern me (`address`).

        Unread, not „new since the call“: an answer that came before `wait_for_messages`
        was called must end the wait at once.
        """
        return self._for_me(unread=True)

    def newest_for_me(self) -> int:
        """Id of the newest message to me, read or not (0 without one): a new one resets the
        time `wait_for_messages` may wait in a turn (L7)."""
        return max((m.id for m in self._for_me(unread=False)), default=0)

    def _for_me(self, *, unread: bool) -> list[Message]:
        with self.store._read() as con:
            found = []
            for ch in self._readable(con):
                found += [
                    _row_to_message(r)
                    for r in con.execute(
                        "SELECT * FROM msg WHERE channel=? AND id>? AND sender!=? ORDER BY id",
                        (ch.name, self._cursor(con, ch.name) if unread else 0, self.agent_id),
                    )
                ]
            senders: dict[int, str] = {}

            def sender_of(msg_id: int) -> str | None:
                if msg_id not in senders:
                    row = con.execute("SELECT sender FROM msg WHERE id=?", (msg_id,)).fetchone()
                    senders[msg_id] = row["sender"] if row else ""
                return senders[msg_id] or None

            return [m for m in found if address(m, self.agent_id, sender_of) is not None]

    def submit_result(
        self,
        kind: str,
        title: str,
        body: str | None = None,
        path: str | None = None,
    ) -> Result:
        """Submit a result; a file in `path` is copied to `output/results/NNN-<agent>-<kind>`."""
        if not kind.strip() or not title.strip():
            raise RoomError("„kind“ und „title“ dürfen nicht leer sein.")
        if not (body and body.strip()) and not path:
            raise RoomError("Gib den Inhalt in „body“ an oder eine Datei in „path“.")
        results_dir = self.store.run_dir / RESULTS_DIR
        staged: Path | None = None
        final: Path | None = None
        suffix = ""
        if path:
            results_dir.mkdir(parents=True, exist_ok=True)
            staged = results_dir / f".staging-{uuid.uuid4().hex}"
            suffix = Path(self._copy_source(path, staged)).suffix
        try:
            with self.store._write() as con:
                assignment = self.store._latest_assignment(con, self.agent_id)
                attempt = 1
                if assignment and isinstance(assignment.context.get("attempt"), int):
                    attempt = max(1, assignment.context["attempt"])
                cur = con.execute(
                    "INSERT INTO result(ts, agent, stage, kind, title, body, path, attempt) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        _now(),
                        self.agent_id,
                        assignment.stage if assignment else None,
                        kind,
                        title,
                        body,
                        None,
                        attempt,
                    ),
                )
                result_id = cur.lastrowid
                assert result_id is not None
                if staged is not None:
                    safe_kind = "".join(c if c.isalnum() or c in "-_" else "-" for c in kind)
                    final = results_dir / f"{result_id:03d}-{self.agent_id}-{safe_kind}{suffix}"
                    staged.rename(final)
                    staged = None
                    rel = final.relative_to(self.store.run_dir).as_posix()
                    con.execute("UPDATE result SET path=? WHERE id=?", (rel, result_id))
                row = con.execute("SELECT * FROM result WHERE id=?", (result_id,)).fetchone()
                result = _row_to_result(row)
            final = None  # committed
            return result
        finally:
            for leftover in (staged, final):
                if leftover is not None:
                    leftover.unlink(missing_ok=True)

    def submit_rating(
        self,
        result_id: int,
        score: float,
        comment: str,
        criteria: dict[str, float] | None = None,
    ) -> Rating:
        """Rate a result of another agent; a second rating of the same result replaces the first.

        `score` and the values in `criteria` must lie on the scale of the rubric; with
        criteria in the rubric only those names are allowed.
        """
        criteria = dict(criteria or {})
        self._check_score("score", score)
        known = self.rubric.criteria
        unknown = sorted(set(criteria) - set(known)) if known else []
        if unknown:
            raise RoomError(
                f"Unbekannte Kriterien: {', '.join(unknown)}. Die Rubrik kennt: {', '.join(known)}."
            )
        for name, value in criteria.items():
            self._check_score(f"criteria[„{name}“]", value)
        with self.store._write() as con:
            row = con.execute("SELECT agent FROM result WHERE id=?", (result_id,)).fetchone()
            if row is None:
                raise RoomError(f"Das Ergebnis #{result_id} gibt es nicht.")
            if row["agent"] == self.agent_id:
                raise RoomError("Eigene Ergebnisse kannst du nicht bewerten.")
            con.execute(
                "INSERT INTO rating(ts, rater, result_id, score, criteria, comment) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(rater, result_id) DO UPDATE SET "
                "ts=excluded.ts, score=excluded.score, criteria=excluded.criteria, "
                "comment=excluded.comment",
                (_now(), self.agent_id, result_id, score, json.dumps(criteria), comment),
            )
            new = con.execute(
                "SELECT * FROM rating WHERE rater=? AND result_id=?", (self.agent_id, result_id)
            ).fetchone()
            return _row_to_rating(new)

    def _check_score(self, what: str, value: float) -> None:
        low, high = self.rubric.scale
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise RoomError(f"„{what}“ muss eine Zahl sein (Skala {low:g} bis {high:g}).")
        if not low <= value <= high:
            raise RoomError(f"„{what}“ = {value:g} liegt außerhalb der Skala {low:g} bis {high:g}.")

    def get_assignment(self) -> Assignment | None:
        """Current assignment with stage and context package."""
        return self.store.assignment(self.agent_id)

    # ----------------------------------------------------------------------------------------
    # Moderator tools (docs/06 §5.3)
    # ----------------------------------------------------------------------------------------

    def give_floor(self, agent: str, prompt: str, channel: str | None = None) -> None:
        """Give a member the floor; posts a note to them and queues a turn for the orchestrator."""
        if not prompt.strip():
            raise RoomError("Gib eine konkrete Frage oder Aufgabe in „prompt“ an.")
        with self.store._write() as con:
            ch = self._moderated_channel(con, channel, "give_floor")
            [target] = self._targets(ch, [agent])
            body = f"Das Wort hat {ch.display(target)}: {prompt}"
            msg_id = self.store._insert_msg(
                con, ch.name, self.agent_id, MessageKind.NOTE, body, to=[target]
            )
            con.execute(
                "INSERT INTO floor(ts, channel, moderator, agent, prompt, msg_id) "
                "VALUES(?,?,?,?,?,?)",
                (_now(), ch.name, self.agent_id, target, prompt, msg_id),
            )

    def call_vote(self, proposal_id: int, rule: VoteRule = VoteRule.MAJORITY) -> None:
        """Start a vote on a proposal in a channel I moderate."""
        rule = _enum(VoteRule, rule, "Regel")
        with self.store._write() as con:
            row, ch = self._message_in_my_channel(con, proposal_id, write=True)
            self._moderated_channel(con, ch.name, "call_vote")
            if row["kind"] != MessageKind.PROPOSAL:
                raise RoomError(
                    f"#{proposal_id} ist kein Vorschlag (proposal), sondern „{row['kind']}“."
                )
            con.execute(
                "INSERT INTO poll(proposal_id, channel, rule, called_by, ts) VALUES(?,?,?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE SET rule=excluded.rule, ts=excluded.ts",
                (proposal_id, ch.name, rule.value, self.agent_id, _now()),
            )
            rule_text = "Mehrheit" if rule is VoteRule.MAJORITY else "einstimmig"
            body = f"Abstimmung über #{proposal_id} ({rule_text}). Stimmt mit „vote“ ab."
            self.store._insert_msg(
                con, ch.name, self.agent_id, MessageKind.NOTE, body, reply_to=proposal_id
            )

    def pin(self, msg_id: int) -> None:
        """Pin a message in a channel I moderate."""
        with self.store._write() as con:
            _, ch = self._message_in_my_channel(con, msg_id, write=True)
            self._moderated_channel(con, ch.name, "pin")
            con.execute("UPDATE msg SET pinned=1 WHERE id=?", (msg_id,))

    def close_discussion(self, decision: str, channel: str | None = None) -> Message:
        """Post the `decision` and close the channel for further agent messages."""
        if not decision.strip():
            raise RoomError("Die Entscheidung („decision“) darf nicht leer sein.")
        with self.store._write() as con:
            ch = self._moderated_channel(con, channel, "close_discussion")
            msg_id = self.store._insert_msg(
                con, ch.name, self.agent_id, MessageKind.DECISION, decision, pinned=True
            )
            self.store._set_channel_state(con, ch.name, ChannelState.CLOSED)
            return self._view(self.store._load_msg(con, msg_id), ch)

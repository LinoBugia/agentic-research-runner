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
import fcntl
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from arr.errors import ArrError
from arr.models import (
    RESERVED_SENDERS,
    AgentStatus,
    ChannelMode,
    GroupChatConfig,
    ResolvedRun,
    Rubric,
    TopologyKind,
)
from arr.resolve import load_yaml_model
from arr.room.address import USER_ADDRESS, address, is_user_question, mentions
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
    Submission,
    WaitGuard,
    WaitKind,
    WaitState,
)
from arr.runs.prepare import load_agent_file
from arr.submissions.check import Checked, SubmissionError, check_submission
from arr.submissions.diff import diff_pairs
from arr.submissions.formats import _SLUG_RE, Expectation, SubmissionStatus

log = logging.getLogger(__name__)

ROOM_DIR = "groupchats"
DB_NAME = "room.sqlite"
ATTACHMENTS_DIR = "attachments"
RESULTS_DIR = "output/results"
SEALED_DIR = f"{ROOM_DIR}/sealed"
"""Result files of a sealed stage (W4-03 F4): beside `room.sqlite`, readable for no agent (the
read profile denies the run folder, the policy names it a hard block) until `release`."""
STAGING_DIR = f"{ROOM_DIR}/staging"
"""Where `submit_result` copies a file first; readable for no agent, so a sealed result never
passes through `output/` (W4-03 F4)."""
SUBMISSIONS_DIR = "output/submissions"
"""Unnamed submissions: `<nr>-<phase>-<agent or alias>/` (docs/10 §4.12, W4b-01)."""
OUTPUT_DIR = "output"
"""Named submissions: `output/<name>/v<n>/`, from v2 on with `diff.patch`."""
SUBMISSION_JSON = "submission.json"
SUBMISSION_BODY = "body.md"
SUBMISSION_FILES = "files"
SUBMISSION_DIFF = "diff.patch"
MAX_SUBMISSION_BYTES = 100 * 1024 * 1024
"""All files of one submission together (each at most `MAX_ATTACHMENT_BYTES`)."""
DIFF_TIMEOUT_S = 10.0
"""The diff of two versions runs at most this long in its own process (as in `review`)."""
MAX_DIFF_BYTES = 200 * 1024
MAX_DIFF_LINES = 2000
"""A file of a version gets a line diff up to this size (as in `review`), else a note."""
MAX_DIFF_TOTAL_BYTES = 2 * 1024 * 1024
"""Text of all files of two versions that goes to the diff process together."""
SEED_SHOWN = "arr"
"""How a version arr made itself (`add_version`, for `seed`) shows its author."""
PUBLISH_TRIES = 3
"""How often `submit` tries again when the stage was sealed or released, the previous version
moved, or another version of the same name came in between the diff and the commit."""
LOCK_WAIT_S = 60.0
"""How long a submission waits for the lock of its name (`_lock_name`)."""
LOCK_POLL_S = 0.05
ROOM_DB_FILES = (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm", f"{DB_NAME}-journal")
"""The database files below `groupchats/`: they hold every result body (W4-03 F4)."""
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
    file TEXT, result_from TEXT, PRIMARY KEY (agent, kind));
CREATE TABLE IF NOT EXISTS wait_guard(agent TEXT PRIMARY KEY, waited REAL NOT NULL DEFAULT 0,
    seen INTEGER, last_end TEXT, yield_to TEXT, end_turn TEXT,
    until_streak INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sealed_stage(stage TEXT PRIMARY KEY, channel TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS sealed_result(result_id INTEGER PRIMARY KEY, stage TEXT);
CREATE TABLE IF NOT EXISTS submission(id INTEGER PRIMARY KEY, ts REAL, agent TEXT,
    shown_as TEXT, stage TEXT, attempt INTEGER, action TEXT, format TEXT, name TEXT,
    version INTEGER, status TEXT, summary TEXT, data TEXT, path TEXT, final_path TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS submission_version ON submission(name, version);
CREATE TABLE IF NOT EXISTS sealed_submission(submission_id INTEGER PRIMARY KEY, stage TEXT);
CREATE TABLE IF NOT EXISTS note_turn(agent TEXT PRIMARY KEY, since REAL);
"""
"""Tables of docs/02 §5 plus `channel.state`, `msg.via`/`msg.delivery`, `poll`, `floor` (both
unused since W4b-03 removed the moderator tools; old rooms keep them readable),
`agent_report` (the status an agent reported itself), `agent_wait` (one row per agent and
kind of wait, docs/09 §4.3) and `wait_guard` (the limits of waiting, L7, L10, L12). Rooms of
runs before W3-07 lack the last three, rooms before W3-09 the column `agent_wait.result_from`
(L14); readers treat that as „no rows“ or „none“. Since W4-03: `sealed_stage` (stages whose
results stay hidden until `release`) and `sealed_result` (results not released yet); rooms
before W4-03 lack them, which means „nothing sealed“. Since W4b-01: `submission` (every
submission of `submit`, head and fields as JSON in `data`; `path` where its folder lies now,
`final_path` where it lies after `release`) and `sealed_submission`; rooms before W4b-01 lack
them, which means „no submissions“. Since W4b-05b.1: `note_turn` (agents in the note turn of
auto-compact, whose room tools refuse); rooms before lack it, which means „nobody“."""

NOTE_TURN_REFUSED = (
    "In diesem Turn schreibst du nur die Übergabe-Notiz als Antworttext. Die Werkzeuge des "
    "Gruppenchats stehen erst im nächsten Turn wieder bereit."
)
"""Refusal of a room tool during the note turn of auto-compact (W4b-05b.1, Entscheidung 53)."""
_NOTE_TURN_TABLE = "CREATE TABLE IF NOT EXISTS note_turn(agent TEXT PRIMARY KEY, since REAL)"


def _no_table(exc: sqlite3.OperationalError) -> bool:
    """The error says the table `note_turn` is missing (a room from before W4b-05b.1)."""
    return "no such table" in str(exc)


WAIT_GUARD_FIELDS = frozenset(
    {"waited", "seen", "last_end", "yield_to", "end_turn", "until_streak"}
)

CONVERSATION_RULES = (
    "Eine Nachricht, ein Gedanke. Setze `kind` korrekt.",
    "Behauptungen mit Begründung oder Beleg: Datei, Rechnung, Anhang.",
    "Beantworte Fragen an dich zuerst.",
    "Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User "
    "(nur er antwortet darauf). Auf eine Antwort wartest du mit `wait_for_messages`, auf den "
    "User nie mit `wait_until`: Antwortet er nicht, arbeite mit einer begründeten Annahme weiter "
    "und nenne sie in deinem Ergebnis.",
    "Widerspruch als `objection` mit `reply_to`, sachlich und konkret.",
    "Nicht wiederholen. Zustimmung nur, wenn sie etwas hinzufügt.",
    "Lange Inhalte (Beweise, Code, Tabellen) als Anhang, im Text nur die Kernaussage.",
    "Wer seinen Teil erledigt hat, gibt sein Ergebnis ab (`submit_result` oder Nachricht "
    "`result`) oder meldet `done`. Fertig heißt: Du wartest nicht mehr, bleibst aber "
    "ansprechbar; jede Nachricht an dich weckt dich. Wartest du auf das Ergebnis eines anderen, "
    'warte mit `wait_until(minutes, until_result_from="<name>")`.',
)
"""Conversation rules of docs/06-gruppenchats.md §6 and docs/09 §4.7, in every channel brief of
a run without phases (unchanged since W4b-03; W4b-06b drops them with the old topologies)."""

PHASES_CONVERSATION_RULES = (
    *CONVERSATION_RULES[:3],
    "Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User "
    "(nur er antwortet darauf). Auf einen Agenten wartest du mit `wait_for_messages`. Brauchst "
    "du die Antwort des Users, warte mit `wait_until` bis zu fünf Minuten; eine Antwort oder "
    "Notiz des Users beendet das Warten. Antwortet er nicht, arbeite mit einer begründeten "
    "Annahme weiter und nenne sie in deiner Abgabe. Wer abgibt, ist fertig; seine offene Frage "
    "gilt damit als erledigt.",
    *CONVERSATION_RULES[4:7],
    "Wer seinen Teil erledigt hat, gibt ab (`submit`). Fertig heißt: Du wartest nicht mehr, "
    "bleibst aber ansprechbar; jede Nachricht an dich weckt dich. Wartest du auf die Abgabe "
    'eines anderen, warte mit `wait_until(minutes, until_result_from="<name>")`.',
)
"""The rules in a run with phases (W4b-06a, Auflage W4b-03): rule 4 after Entscheidungen 67
and 69 (waiting for the user with `wait_until`, at most five minutes; handing in closes the
question), rule 8 names `submit` (G8: the room of a `phases` run offers no `submit_result`)."""

_MODE_TEXT = {
    ChannelMode.ROUNDS: "Runden: Alle Mitglieder kommen pro Runde dran, sonst `pass`.",
    ChannelMode.EVENT: "ereignisgesteuert: Wer mit `@name` angesprochen, gefragt oder mit "
    "`reply_to` beantwortet wird, wird sofort geweckt. Runden gibt es nur, wenn niemand mehr "
    "schreibt.",
    ChannelMode.MODERATED: "moderiert: Der Moderator erteilt das Wort und beendet die Diskussion.",
}
_END_TEXT = {
    ChannelMode.ROUNDS: "nach der letzten Runde oder wenn alle `pass`/`done` melden.",
    ChannelMode.EVENT: "wenn alle fertig sind (Ergebnis abgegeben oder `done`) und keine "
    "Nachricht an einen von ihnen ungelesen ist, nach den Leerlauf-Runden oder wenn es lange "
    "ruhig ist.",
    ChannelMode.MODERATED: "wenn der Moderator die Diskussion mit einer Entscheidung beendet.",
}


class ChannelState(StrEnum):
    """State of a channel in `channel.state` (docs/06-gruppenchats.md §2, §5.3).

    `waiting`: `open_from` not reached, agents can neither read nor write; `open`: normal;
    `closed`: discussion ended, readable but no new agent messages (rooms from before W4b-03,
    when the moderator tool `close_discussion` still existed; nothing closes a channel now).
    """

    WAITING = "waiting"
    OPEN = "open"
    CLOSED = "closed"


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


def channel_aliases(config: GroupChatConfig) -> dict[str, str | None]:
    """Agent ID → its alias in the channel `config` („Mitglied A“ …), None for every member of
    a channel that is not anonymous. The same order and names as `RoomStore.create_channel`
    stores them (W4b-06a, B2: the project context tells each agent its own alias)."""
    members = list(dict.fromkeys(config.members))
    return {agent: (_alias(i) if config.anonymous else None) for i, agent in enumerate(members)}


def _channel_name(name: str) -> str:
    return name.strip().removeprefix("#")


PRIVATE_PREFIX = "user."
"""The private channel of an agent to the user is `user.<agent-id>` (W4b-03, Entscheidung 30).

No channel of a group can have this name: a channel name is a slug (`_SLUG_RE`), which has no
dot (Entscheidung 45, no reserved name in the schema). An agent ID is a slug as well, so the
name is one safe part of a path (no `/`, no `..`, no dot at the start): it becomes the folder
`groupchats/attachments/<name>/` and the files `groupchats/<name>.md|jsonl` of the export."""

_PRIVATE_SLUG = "private"
"""Stands in for the name while the stored configuration is validated (`GroupChatConfig.name`
is a slug); the real name comes back afterwards."""

PRIVATE_BRIEF = (
    "Steckbrief #{name}\n"
    "Dein privater Kanal zum User: Hier fragst du ihn mit einer `question` an `@user`, in jeder "
    "Phase, auch in einer isolierten. Nur der User liest diesen Kanal; kein anderer Agent sieht "
    "ihn. Brauchst du die Antwort, warte mit `wait_until` bis zu fünf Minuten; eine Antwort oder "
    "Notiz des Users beendet das Warten. Gibst du ab, gilt deine Frage als erledigt. Die "
    "Annahme, mit der du ohne Antwort weiterarbeitest, nennst du in deiner Abgabe."
)
"""The pinned brief of a private channel: what it is for, who reads it, how to wait for the
answer (W4b-06a, Entscheidung 69: `wait_until`, at most five minutes; an answer or a note of the
user ends it) and that handing in closes an open question (Entscheidung 67). Not: „end the turn
without handing in, the phase waits“ (W4b-03.1 NR1: the next turn starts at once)."""


def private_channel(agent_id: str) -> str:
    """Name of the private channel of `agent_id` to the user."""
    return PRIVATE_PREFIX + agent_id


def private_owner(channel: str) -> str | None:
    """The agent whose private channel `channel` is, None for any other name."""
    if not channel.startswith(PRIVATE_PREFIX):
        return None
    owner = channel.removeprefix(PRIVATE_PREFIX)
    return owner if _SLUG_RE.fullmatch(owner) and owner not in RESERVED_SENDERS else None


def private_config(agent_id: str) -> GroupChatConfig:
    """The fixed settings of a private channel: the defaults of `GroupChatConfig` (event, not
    anonymous, wake-ups on mention and question), the agent its only member. The name is set
    without validation, because it is no slug on purpose."""
    config = GroupChatConfig(
        name=_PRIVATE_SLUG,
        purpose="Privater Kanal zum User",
        members=[agent_id],
    )
    return config.model_copy(update={"name": private_channel(agent_id)})


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


def _row_to_assignment(row: sqlite3.Row) -> Assignment:
    return Assignment(
        agent=row["agent"],
        turn=row["turn"],
        stage=row["stage"],
        prompt=row["prompt"],
        context=json.loads(row["context"] or "{}"),
        ts=row["ts"],
    )


_SUBMISSION_SELECT = (
    "SELECT s.*, (ss.submission_id IS NOT NULL) AS sealed FROM submission s "
    "LEFT JOIN sealed_submission ss ON ss.submission_id = s.id"
)


def _row_to_submission(row: sqlite3.Row) -> Submission:
    data = json.loads(row["data"] or "{}")
    return Submission(
        id=row["id"],
        ts=row["ts"],
        agent=row["agent"],
        shown_as=row["shown_as"],
        stage=row["stage"],
        attempt=row["attempt"],
        action=row["action"],
        format=row["format"],
        name=row["name"],
        version=row["version"],
        status=row["status"],
        summary=row["summary"],
        body=data.get("body"),
        files=data.get("files", []),
        note_next=data.get("note_next"),
        fields=data.get("fields", {}),
        path=row["path"],
        sealed=bool(row["sealed"]),
    )


def _folder_part(text: str | None, empty: str) -> str:
    """A piece of a folder name: lowercase letters, digits, `-` and `_`, at most 40."""
    part = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "-" for c in text or "")
    return part.lower().strip("-")[:40] or empty


def _read_head(path: Path, limit: int) -> bytes | None:
    """At most `limit` bytes of a regular file arr wrote; None for anything else."""
    try:
        fd = os.open(path, _FILE_FLAGS)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit)
    finally:
        os.close(fd)


HEAD_PART = "kopf.json"
"""The part of `diff.patch` with head and fields of a version (W4b-01.1 M2)."""


def head_text(format: str, status: str, summary: str, fields: Mapping[str, Any]) -> bytes:
    """Head and fields of a version for the diff: fixed, indented JSON without `id`, `ts`,
    `version`, `attempt` and never `note_next` (private, W4b-01.1 M3)."""
    data = {"format": format, "status": status, "summary": summary, "fields": fields}
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def _version_parts(folder: Path) -> dict[str, Path]:
    """The files of a version that get a diff: `body.md` and `files/<name>` (regular files)."""
    parts = {}
    if (folder / SUBMISSION_BODY).is_file():
        parts[SUBMISSION_BODY] = folder / SUBMISSION_BODY
    try:
        entries = sorted(os.scandir(folder / SUBMISSION_FILES), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if entry.is_file(follow_symlinks=False):
            parts[f"{SUBMISSION_FILES}/{entry.name}"] = Path(entry.path)
    return parts


def _same_file(a: Path, b: Path) -> bool:
    """True if two regular files arr wrote are equal as a whole: size, then the content in
    blocks, never all of it in memory; anything else (a link, unreadable) counts as changed."""
    fds = []
    try:
        for path in (a, b):
            fds.append(os.open(path, _FILE_FLAGS))
        infos = [os.fstat(fd) for fd in fds]
        if not all(stat.S_ISREG(i.st_mode) for i in infos) or infos[0].st_size != infos[1].st_size:
            return False
        while True:
            left, right = os.read(fds[0], _COPY_CHUNK), os.read(fds[1], _COPY_CHUNK)
            if left != right:
                return False
            if not left:
                return True
    except OSError:
        return False
    finally:
        for fd in fds:
            os.close(fd)


def _as_text(data: bytes | None) -> str | None:
    """Text of a part for the diff, or None (binary, too large or too many lines)."""
    if data is None or len(data) > MAX_DIFF_BYTES or b"\0" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if len(text.splitlines()) <= MAX_DIFF_LINES else None


def version_diff(
    old: Path, old_v: int, new: Path, new_v: int, old_head: bytes, new_head: bytes
) -> str:
    """`diff.patch` from version `old_v` (folder `old`) to `new_v` (folder `new`).

    The parts are head and fields (`HEAD_PART`, from `head_text`), `body.md` and every file
    of `files/`. Whether a part changed is decided on the whole part (`_same_file`). A changed
    text part up to `MAX_DIFF_BYTES` and `MAX_DIFF_LINES` gets a unified diff, made in a
    process of its own with `DIFF_TIMEOUT_S` (`diff_pairs`); any other changed part a note at
    the top. „Keine Änderung“ only when every part is the same.
    """
    before: dict[str, Path | bytes] = {HEAD_PART: old_head, **_version_parts(old)}
    after: dict[str, Path | bytes] = {HEAD_PART: new_head, **_version_parts(new)}
    names = [HEAD_PART, *sorted((set(before) | set(after)) - {HEAD_PART})]
    notes, pairs, total = [], [], 0
    for part in names:
        a_src, b_src = before.get(part), after.get(part)
        if a_src is not None and b_src is not None:
            if isinstance(a_src, bytes) or isinstance(b_src, bytes):
                same = a_src == b_src
            else:
                same = _same_file(a_src, b_src)
            if same:
                continue

        def load(src: Path | bytes | None) -> bytes | None:
            if src is None:
                return b""
            return src if isinstance(src, bytes) else _read_head(src, MAX_DIFF_BYTES + 1)

        a, b = load(a_src), load(b_src)
        text_a, text_b = _as_text(a), _as_text(b)
        if text_a is None or text_b is None:
            notes.append(
                f"# {part}: geändert, kein Text-Diff (keine Textdatei oder größer als "
                f"{MAX_DIFF_BYTES // 1024} KB oder {MAX_DIFF_LINES} Zeilen)."
            )
            continue
        total += len(a or b"") + len(b or b"")
        if total > MAX_DIFF_TOTAL_BYTES:
            notes.append(f"# {part}: geändert, kein Text-Diff (Fassungen zusammen zu groß).")
            continue
        old_name = f"v{old_v}/{part}" if a_src is not None else "/dev/null"
        new_name = f"v{new_v}/{part}" if b_src is not None else "/dev/null"
        pairs.append((text_a, text_b, old_name, new_name))
    diff = diff_pairs(pairs, timeout_s=DIFF_TIMEOUT_S) if pairs else ""
    if diff is None:
        notes.append(f"# Kein Diff: Er dauerte länger als {DIFF_TIMEOUT_S:g} s.")
        diff = ""
    if not notes and not diff:
        return f"# Keine Änderung von v{old_v} zu v{new_v}.\n"
    head = "\n".join(notes) + "\n" if notes else ""
    return head + diff


_COPY_VIEW = "arr-copy"
"""Agent ID of the view `add_version` copies files with; it writes nothing to the room."""


class _Stale(Exception):
    """Another version of the same name came in between: compute the diff again."""


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
        for folder in (SEALED_DIR, STAGING_DIR):
            (Path(run_dir) / folder).mkdir(parents=True, exist_ok=True)
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

    def _brief(
        self,
        config: GroupChatConfig,
        members: list[str],
        aliases: Mapping[str, str | None],
        roles: Mapping[str, str],
    ) -> str:
        """The generated brief; its rules follow the kind of run (`run.yaml`, written by
        `prepare_run` before `setup_channels`): without `run.yaml` or phases the old ones."""
        rules = PHASES_CONVERSATION_RULES if self.uses_phases() else CONVERSATION_RULES
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
        lines += [f"{i}. {rule}" for i, rule in enumerate(rules, 1)]
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

    # ----------------------------------------------------------------------------------------
    # Sealed stages (W4-03 F4: independent reviews)
    # ----------------------------------------------------------------------------------------

    def seal(self, stage: str, channel: str | None = None) -> None:
        """Results submitted in `stage` from now on stay hidden: their files go to
        `SEALED_DIR`, readable for no agent, until `release(stage)`. `channel` names the
        anonymous channel whose aliases the file names use (else the first anonymous channel
        of the agent, else its ID)."""
        with self._write() as con:
            con.execute(
                "INSERT INTO sealed_stage(stage, channel, ts) VALUES(?,?,?) "
                "ON CONFLICT(stage) DO UPDATE SET channel=excluded.channel, ts=excluded.ts",
                (stage, channel, _now()),
            )

    def sealed_stages(self) -> dict[str, str | None]:
        """Sealed stages with their alias channel; empty for a room before W4-03."""
        try:
            with self._read() as con:
                rows = con.execute("SELECT stage, channel FROM sealed_stage").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {r["stage"]: r["channel"] for r in rows}

    def is_sealed(self, result_id: int) -> bool:
        """True while a result of a sealed stage is not released."""
        try:
            with self._read() as con:
                row = con.execute(
                    "SELECT 1 FROM sealed_result WHERE result_id=?", (result_id,)
                ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def release(self, stage: str) -> list[Result]:
        """Unseal `stage`: its hidden result files move to `output/results/` (their names
        carry aliases, see `seal`), its hidden submissions to their place below `output/`
        (W4b-01); later results of the stage are no longer sealed. Returns the released
        results, oldest first."""
        results_dir = self.run_dir / RESULTS_DIR
        results_dir.mkdir(parents=True, exist_ok=True)
        released = []
        with self._write() as con:
            con.execute("DELETE FROM sealed_stage WHERE stage=?", (stage,))
            ids = [
                r[0]
                for r in con.execute(
                    "SELECT result_id FROM sealed_result WHERE stage=? ORDER BY result_id",
                    (stage,),
                )
            ]
            for result_id in ids:
                row = con.execute("SELECT * FROM result WHERE id=?", (result_id,)).fetchone()
                result = _row_to_result(row)
                if result.path:
                    source = self.run_dir / result.path
                    target = results_dir / source.name
                    if os.path.lexists(source):
                        os.replace(source, target)
                    elif not os.path.lexists(target):
                        raise ArrError(
                            f"Die versiegelte Datei „{result.path}“ von Ergebnis #{result_id} "
                            "fehlt. Das ist ein Fehler in arr."
                        )
                    # else: moved by a release that was killed before its commit (W4b-01.1)
                    rel = target.relative_to(self.run_dir).as_posix()
                    con.execute("UPDATE result SET path=? WHERE id=?", (rel, result_id))
                    result = result.model_copy(update={"path": rel})
                con.execute("DELETE FROM sealed_result WHERE result_id=?", (result_id,))
                released.append(result)
            self._release_submissions(con, stage)
        return released

    def _release_submissions(self, con: sqlite3.Connection, stage: str) -> None:
        """Move the sealed submissions of `stage` to their place below `output/` (W4b-01).

        A submission whose folder is already there and gone from `SEALED_DIR` was moved by a
        `release` killed before its commit: only its row is brought up to date (W4b-01.1 M1).
        """
        try:
            rows = con.execute(
                "SELECT s.id, s.path, s.final_path FROM sealed_submission ss "
                "JOIN submission s ON s.id = ss.submission_id WHERE ss.stage=? ORDER BY s.id",
                (stage,),
            ).fetchall()
        except sqlite3.OperationalError:  # a room from before W4b-01
            return
        for row in rows:
            source, target = self.run_dir / row["path"], self.run_dir / row["final_path"]
            if not os.path.lexists(source):
                if not os.path.lexists(target):
                    raise ArrError(
                        f"Die versiegelte Abgabe #{row['id']} fehlt in „{row['path']}“. Das ist "
                        "ein Fehler in arr."
                    )
                # moved by a release that was killed before its commit (W4b-01.1 M1)
            else:
                self._clear_orphan(con, row["final_path"], own=row["id"])
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
            con.execute("UPDATE submission SET path=final_path WHERE id=?", (row["id"],))
            con.execute("DELETE FROM sealed_submission WHERE submission_id=?", (row["id"],))

    def result_text(self, result_id: int, max_bytes: int) -> str:
        """Body of a result and the start of its file (also a sealed one) as text, for arr's
        own reading (verdicts, digests): together at most `max_bytes` characters (W4-03.1 H2:
        `body` is as long as the agent wants), never through a link."""
        result = self.result(result_id)
        body = (result.body or "")[:max_bytes]
        if not result.path or len(body) >= max_bytes:
            return body
        try:
            fd = os.open(self.run_dir / result.path, _FILE_FLAGS)
        except OSError:
            return body
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return body
            data = os.read(fd, max_bytes)
        finally:
            os.close(fd)
        text = data.decode("utf-8", errors="replace")
        return (f"{body}\n\n{text}" if body else text)[:max_bytes]

    def _resolved(self) -> ResolvedRun | None:
        if not hasattr(self, "_run"):
            run_yaml = self.run_dir / "run.yaml"
            self._run = load_yaml_model(run_yaml, ResolvedRun) if run_yaml.is_file() else None
        return self._run

    def uses_phases(self) -> bool:
        """True if `run.yaml` names the topology `phases` (W4b-01): its agents get `submit`
        instead of `submit_result` and `submit_rating`."""
        run = self._resolved()
        return run is not None and run.topology.kind is TopologyKind.PHASES

    def rubric(self) -> Rubric:
        """Rubric of the run from `run.yaml` (docs/03 §3); the default scale without one."""
        run = self._resolved()
        return run.rubric if run and run.rubric else Rubric()

    def agent_ids(self) -> list[str]:
        """Agents of the run from `run.yaml`, in order; without it the channel members."""
        run = self._resolved()
        if run is not None:
            return [a.id for a in run.agents]
        with self._read() as con:
            rows = con.execute("SELECT DISTINCT agent FROM member ORDER BY rowid").fetchall()
        return [r[0] for r in rows]

    def open_channel(self, channel: str) -> None:
        """Open a channel whose `open_from` stage has been reached (W2-03), or open it again."""
        self.set_channel_state(channel, ChannelState.OPEN)

    def hold_channel(self, channel: str) -> None:
        """Close a channel for the agents again (W4-03 F3): like `waiting`, they can neither
        read nor write it and get no hints; its history stays for GUI and export."""
        self.set_channel_state(channel, ChannelState.WAITING)

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
        if private_owner(name) is not None:
            # W4b-03: the name of a private channel is no slug; check the rest, keep the name
            data = json.loads(row["config"])
            data["name"] = _PRIVATE_SLUG
            config = GroupChatConfig.model_validate(data).model_copy(update={"name": name})
        else:
            config = GroupChatConfig.model_validate_json(row["config"])
        return _Channel(name=name, config=config, state=ChannelState(row["state"]), members=members)

    def create_private_channel(self, agent_id: str) -> Message | None:
        """Create the private channel of `agent_id` to the user (W4b-03, docs/10 §4.5): only
        the agent is a member, the user is the fixed address `user`; open in every phase.
        Returns its brief, None if it exists already (the phase runner calls this at its start,
        before the first turn)."""
        if agent_id in RESERVED_SENDERS or not _SLUG_RE.fullmatch(agent_id):
            raise ArrError(f"„{agent_id}“ ist keine Agent-ID für einen privaten Kanal.")
        config = private_config(agent_id)
        with self._read() as con:
            if con.execute("SELECT 1 FROM channel WHERE name=?", (config.name,)).fetchone():
                return None
        brief = self.create_channel(config, brief=PRIVATE_BRIEF.format(name=config.name))
        with self._write() as con:  # the brief counts as read: it stays pinned in `inbox()`
            con.execute(
                "INSERT INTO cursor(agent, channel, last_id) VALUES(?,?,?) "
                "ON CONFLICT(agent, channel) DO UPDATE SET last_id=max(last_id, excluded.last_id)",
                (agent_id, config.name, brief.id),
            )
        return brief

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
            # An answer by an addressee closes the open question (docs/06 §3); a question to
            # the user only the user (L16).
            row = con.execute(
                "SELECT to_agents FROM msg WHERE id=? AND kind='question' AND open=1",
                (reply_to,),
            ).fetchone()
            addressees = json.loads(row["to_agents"]) if row else []
            if sender in addressees and (USER not in addressees or sender == USER):
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

        The user answers open questions to the user in the channel (L16): a message with
        `reply_to` on such a question, or one to its asker (`to`, `@name`), closes it. A message
        without addressee and without `reply_to` answers nothing: a note to all stays a note to
        all (User 08.11., W3-09.1). A message with `via: user_agent` (Claude Code) answers no
        question to the user, whatever it addresses (E15, W4-01 H1); it still wakes.
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
            asked: list[Message] = []
            if sender == USER:  # `@name` of the user addresses like `to` (docs/09 §4.1)
                named = mentions(body, ch.members, USER, lambda n: n if n in ch.members else None)
                to = [*to, *named]
                # E15 (W4-01 H1): only the user answers a question to the user; a message of
                # the user agent (Claude Code, `post_to_run`) addresses and wakes, nothing more
                if via is not MessageVia.USER_AGENT:
                    asked = self._open_user_questions(con, channel)
            if reply_to is not None:
                self._check_reply_to(con, ch.name, reply_to, ArrError)
            for question in asked:
                if reply_to == question.id or question.sender in to:
                    con.execute("UPDATE msg SET open=0 WHERE id=?", (question.id,))
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
    def _open_user_questions(con: sqlite3.Connection, channel: str | None) -> list[Message]:
        sql, args = "SELECT * FROM msg WHERE open=1 AND kind='question'", ()
        if channel is not None:
            sql, args = sql + " AND channel=?", (channel,)
        found = [_row_to_message(r) for r in con.execute(sql + " ORDER BY id", args)]
        return [m for m in found if is_user_question(m)]

    def close_question(self, msg_id: int) -> None:
        """Close a question nobody will answer any more (its asker worked on, the run ended)."""
        with self._write() as con:
            con.execute("UPDATE msg SET open=0 WHERE id=? AND kind='question'", (msg_id,))

    def open_user_questions(self, channel: str | None = None) -> list[Message]:
        """Open questions to the user (L16), oldest first; real sender IDs."""
        with self._read() as con:
            return self._open_user_questions(con, channel)

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
    # Agent state, assignments, results, ratings
    # ----------------------------------------------------------------------------------------

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
                result_from=dict(r).get("result_from"),
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

    def begin_note_turn(self, agent: str) -> None:
        """The note turn of `agent` begins: its room tools that write, wait or move its read
        position refuse until `end_note_turn` (W4b-05b.1, Entscheidung 53)."""
        with self._write() as con:
            con.execute(_NOTE_TURN_TABLE)  # a room from before W4b-05b.1
            con.execute(
                "INSERT INTO note_turn(agent, since) VALUES(?, ?) "
                "ON CONFLICT(agent) DO UPDATE SET since=excluded.since",
                (agent, _now()),
            )

    def end_note_turn(self, agent: str) -> None:
        """The note turn of `agent` is over (in any case: success, error, stop). Only a missing
        table („nobody was in a note turn“) is passed over; any other error is raised, so the
        caller can log it and try again (W4b-05b.2)."""
        try:
            with self._write() as con:
                con.execute("DELETE FROM note_turn WHERE agent=?", (agent,))
        except sqlite3.OperationalError as exc:
            if not _no_table(exc):
                raise

    def in_note_turn(self, agent: str) -> bool:
        """True while the note turn of `agent` runs. A room without the table (before
        W4b-05b.1) means nobody; when the flag cannot be read (e.g. the database is locked),
        the answer is True: in doubt the tools refuse (W4b-05b.2)."""
        try:
            with self._read() as con:
                row = con.execute("SELECT 1 FROM note_turn WHERE agent=?", (agent,)).fetchone()
        except sqlite3.OperationalError as exc:
            return not _no_table(exc)
        return row is not None

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
    # Submissions (docs/10 §4.3, §4.12; W4b-01)
    # ----------------------------------------------------------------------------------------

    def submissions(
        self,
        *,
        agent: str | None = None,
        stage: str | None = None,
        attempt: int | None = None,
        name: str | None = None,
        action: str | None = None,
        format: str | None = None,
        latest: bool = False,
    ) -> list[Submission]:
        """Submissions in order, filtered by every argument given (sealed ones included).

        `latest`: only the newest per agent, stage, attempt and action (Entscheidung 16: an
        agent may hand in again, the newest counts). Empty for a room before W4b-01.
        """
        sql, args = _SUBMISSION_SELECT + " WHERE 1=1", []
        filters = (
            ("agent", agent),
            ("stage", stage),
            ("attempt", attempt),
            ("name", name),
            ("action", action),
            ("format", format),
        )
        for column, value in filters:
            if value is not None:
                sql, args = sql + f" AND s.{column}=?", [*args, value]
        try:
            with self._read() as con:
                found = [_row_to_submission(r) for r in con.execute(sql + " ORDER BY s.id", args)]
        except sqlite3.OperationalError:  # a room from before W4b-01
            return []
        if latest:
            newest = {(s.agent, s.stage, s.attempt, s.action): s for s in found}
            found = sorted(newest.values(), key=lambda s: s.id)
        return found

    def submission(self, submission_id: int) -> Submission:
        """One submission by its number."""
        try:
            with self._read() as con:
                row = con.execute(_SUBMISSION_SELECT + " WHERE s.id=?", (submission_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            raise ArrError(f"Die Abgabe #{submission_id} gibt es nicht.")
        return _row_to_submission(row)

    def latest_version(self, name: str) -> Submission | None:
        """The newest version of the named submission `name`, None without one."""
        found = self.submissions(name=name)
        return found[-1] if found else None

    def add_version(
        self,
        name: str,
        *,
        summary: str,
        format: str = "artifact",
        body: str | None = None,
        files: Sequence[str] = (),
        root: Path | None = None,
        fields: Mapping[str, Any] | None = None,
        stage: str | None = None,
    ) -> Submission:
        """A version of `name` that arr makes itself, without the tool (for `seed`, W4b-02b).

        `files` are paths relative to `root` (default `base/`, the project at the start),
        copied by the same rules as files of agents (`AgentRoom.copy_file`). The format is
        checked like a submission with status `done`; the author is `orchestrator`, shown as
        `arr`. Never sealed. `ArrError` with a German message if anything does not fit.
        """
        try:
            expectation = Expectation.model_validate({"format": format, "name": name})
            checked = check_submission(
                status=SubmissionStatus.DONE.value,
                summary=summary,
                body=body,
                files=list(files),
                fields=dict(fields or {}),
                expectation=expectation,
            )
        except ValidationError as exc:
            raise ArrError(f"Die Fassung „{name}“ lässt sich nicht anlegen: {exc}") from None
        except SubmissionError as exc:
            raise ArrError(str(exc)) from None
        view = AgentRoom(self, _COPY_VIEW, root or self.run_dir / "base")
        try:
            return self._publish(
                agent=ORCHESTRATOR,
                expectation=expectation,
                checked=checked,
                stage=stage,
                attempt=1,
                copy=view.copy_file,
                shown=lambda _con, _channel: SEED_SHOWN,
                current=None,
                seal=False,
            )
        except RoomError as exc:
            raise ArrError(str(exc)) from None

    def _check_name_place(self, name: str) -> None:
        """`output/<name>` is free or a plain folder of versions (no link, no file)."""
        place = self.run_dir / OUTPUT_DIR / name
        try:
            info = os.lstat(place)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise RoomError(
                f"„output/{name}“ ist schon belegt. Der Auftrag braucht einen anderen Namen."
            )

    def _clear_orphan(self, con: sqlite3.Connection, rel: str, *, own: int | None = None) -> None:
        """Make room at `rel` (a folder of a submission, relative to the run) under the write
        lock (W4b-01.1 M1).

        Something there that no row of `submission` points to (`path` or `final_path`; `own`
        is the row being moved) is the rest of a call killed between rename and commit: it is
        moved, without following a link, to `STAGING_DIR`, which no agent can read. Something
        a row points to stays, and arr refuses."""
        place = self.run_dir / rel
        if not os.path.lexists(place):
            return
        row = con.execute(
            "SELECT id FROM submission WHERE (path=? OR final_path=?) AND id IS NOT ?",
            (rel, rel, own),
        ).fetchone()
        if row is not None:
            log.error("submission folder %s belongs to submission #%s", rel, row["id"])
            raise RoomError(
                f"„{rel}“ gehört schon zu Abgabe #{row['id']}. Das ist ein Fehler in arr, nicht "
                "bei dir: Beende den Turn mit einer kurzen Notiz."
            )
        orphan = self.run_dir / STAGING_DIR / f".orphan-{uuid.uuid4().hex}"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        os.rename(place, orphan)
        log.warning("moved the orphaned folder %s to %s", rel, orphan.name)

    def _stage_submission(
        self, staging: Path, checked: Checked, copy: Callable[[str, Path], str] | None
    ) -> list[str]:
        """Body and files of a submission into `staging`; returns the names of the files."""
        if checked.body is not None:
            (staging / SUBMISSION_BODY).write_text(checked.body, encoding="utf-8")
        if not checked.files:
            return []
        if copy is None:
            raise RoomError("Diese Abgabe kann keine Dateien enthalten.")
        folder = staging / SUBMISSION_FILES
        folder.mkdir()
        names, total = [], 0
        for rel in checked.files:
            name = [p for p in rel.split("/") if p not in ("", ".")][-1]
            copy(rel, folder / name)
            total += (folder / name).stat().st_size
            if total > MAX_SUBMISSION_BYTES:
                raise RoomError(
                    f"Die Dateien der Abgabe sind zusammen zu groß (erlaubt "
                    f"{MAX_SUBMISSION_BYTES // (1024 * 1024)} MB)."
                )
            names.append(name)
        return names

    def _stage_sealed(self, stage: str | None) -> bool:
        with self._read() as con:
            return AgentRoom._sealed(con, stage)[0]

    @staticmethod
    def _rated(con: sqlite3.Connection, agent: str, checked: Checked) -> None:
        """Every rated submission exists and is not the rater's own (Entscheidung 7)."""
        for i, entry in enumerate(checked.fields.get("ratings") or []):
            ref = entry["ref"]
            row = con.execute("SELECT agent FROM submission WHERE id=?", (ref,)).fetchone()
            if row is None:
                raise RoomError(f"„fields.ratings[{i}].ref“: Die Abgabe #{ref} gibt es nicht.")
            if row["agent"] == agent:
                raise RoomError(
                    f"„fields.ratings[{i}].ref“: #{ref} ist deine eigene Abgabe. Eigene "
                    "Abgaben bewertest du nie."
                )

    def _publish(
        self,
        *,
        agent: str,
        expectation: Expectation,
        checked: Checked,
        stage: str | None,
        attempt: int,
        copy: Callable[[str, Path], str] | None,
        shown: Callable[[sqlite3.Connection, str | None], str],
        current: Callable[[sqlite3.Connection], None] | None,
        seal: bool = True,
    ) -> Submission:
        """File a checked submission exactly once (docs/10 §4.12).

        Everything is prepared in `STAGING_DIR` (body, files, `diff.patch` against the
        previous version, `submission.json`), then renamed in one step to its place: below
        `output/` or, in a sealed stage, below `SEALED_DIR`, in the same write transaction that
        numbers it. A failure leaves nothing below `output/` and no row. Versions of one name
        are made one after the other (`_lock_name`); if the stage was sealed or released
        meanwhile, the previous version moved or another version came in (a writer without the
        lock), the diff is made again (`PUBLISH_TRIES`).
        `note_next` stays in the database, never in the folder or the diff (W4b-01.1 M3).
        """
        name = expectation.name
        if name is not None:
            self._check_name_place(name)
        staging = self.run_dir / STAGING_DIR / f".submission-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        moved: Path | None = None
        lock: int | None = None
        try:
            names = self._stage_submission(staging, checked, copy)
            if name is not None:
                lock = self._lock_name(name)
            for _ in range(PUBLISH_TRIES):
                previous = self.latest_version(name) if name is not None else None
                sealed_now = seal and self._stage_sealed(stage)
                (staging / SUBMISSION_DIFF).unlink(missing_ok=True)
                if previous is not None:
                    assert previous.version is not None
                    if previous.sealed and not (sealed_now and previous.stage == stage):
                        text = (
                            f"# Kein Diff: Die vorige Fassung v{previous.version} ist noch "
                            "versiegelt.\n"
                        )
                    else:
                        text = version_diff(
                            self.run_dir / previous.path,
                            previous.version,
                            staging,
                            previous.version + 1,
                            head_text(
                                previous.format, previous.status, previous.summary, previous.fields
                            ),
                            head_text(
                                expectation.format,
                                checked.status.value,
                                checked.summary,
                                checked.fields,
                            ),
                        )
                    (staging / SUBMISSION_DIFF).write_text(text, encoding="utf-8")
                try:
                    with self._write() as con:
                        if current is not None:
                            current(con)
                        newest = (
                            con.execute(
                                "SELECT id FROM submission WHERE name=? "
                                "ORDER BY version DESC LIMIT 1",
                                (name,),
                            ).fetchone()
                            if name is not None
                            else None
                        )
                        if (newest["id"] if newest else None) != (
                            previous.id if previous else None
                        ):
                            raise _Stale
                        # The diff above read the previous version where it lay then. If
                        # another process released it while the diff ran, it moved, and the
                        # diff may name `/dev/null` as the previous version: make it again
                        # (W4b-01.2, N1). A release always moves the sealed version, and a change
                        # of the seal alone, without that, does not change the diff, so comparing
                        # the place of the previous version is enough.
                        sealed, channel = AgentRoom._sealed(con, stage) if seal else (False, None)
                        if previous is not None:
                            row = con.execute(
                                "SELECT path FROM submission WHERE id=?", (previous.id,)
                            ).fetchone()
                            if row is None or row["path"] != previous.path:
                                raise _Stale
                        if expectation.format == "rating" and expectation.spec is None:
                            self._rated(con, agent, checked)
                        who = shown(con, channel)
                        version = (previous.version or 0) + 1 if previous else 1
                        version = version if name is not None else None
                        data = {
                            "body": checked.body,
                            "files": names,
                            "sources": checked.files,
                            "note_next": checked.note_next,
                            "fields": checked.fields,
                        }
                        ts = _now()
                        cur = con.execute(
                            "INSERT INTO submission(ts, agent, shown_as, stage, attempt, action, "
                            "format, name, version, status, summary, data, path, final_path) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                ts,
                                agent,
                                who,
                                stage,
                                attempt,
                                expectation.action,
                                expectation.format,
                                name,
                                version,
                                checked.status.value,
                                checked.summary,
                                json.dumps(data, ensure_ascii=False),
                                "",
                                "",
                            ),
                        )
                        number = cur.lastrowid
                        assert number is not None
                        if name is not None:
                            final = f"{OUTPUT_DIR}/{name}/v{version}"
                        else:
                            phase = _folder_part(stage, "ohne-phase")
                            final = (
                                f"{SUBMISSIONS_DIR}/{number:03d}-{phase}-"
                                f"{_folder_part(who, 'agent')}"
                            )
                        where = f"{SEALED_DIR}/submission-{number:03d}" if sealed else final
                        record = Submission(
                            id=number,
                            ts=ts,
                            agent=agent,
                            shown_as=who,
                            stage=stage,
                            attempt=attempt,
                            action=expectation.action,
                            format=expectation.format,
                            name=name,
                            version=version,
                            status=checked.status.value,
                            summary=checked.summary,
                            body=checked.body,
                            files=names,
                            note_next=checked.note_next,
                            fields=checked.fields,
                            path=where,
                            sealed=sealed,
                        )
                        public = record.model_dump(
                            mode="json", exclude={"agent", "path", "sealed", "body", "note_next"}
                        )
                        public["agent"] = public.pop("shown_as")
                        public["body"] = SUBMISSION_BODY if checked.body is not None else None
                        (staging / SUBMISSION_JSON).write_text(
                            json.dumps(public, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        target = self.run_dir / where
                        # The place at `final` of a sealed one is cleared by `release`, which
                        # moves it there; nothing can appear there in between (W4b-01.2, N4).
                        self._clear_orphan(con, where)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        moved = target  # free now, under the write lock
                        os.rename(staging, target)
                        con.execute(
                            "UPDATE submission SET path=?, final_path=? WHERE id=?",
                            (where, final, number),
                        )
                        if sealed:
                            con.execute(
                                "INSERT INTO sealed_submission(submission_id, stage) VALUES(?,?)",
                                (number, stage),
                            )
                    moved = None  # committed
                    return record
                except _Stale:
                    continue
            raise RoomError(
                "Während deiner Abgabe hat sich der Stand der Phase geändert (versiegelt, "
                "freigegeben oder eine neue Fassung). Gib noch einmal ab."
            )
        finally:
            if lock is not None:
                os.close(lock)  # releases the lock
            if moved is not None:
                shutil.rmtree(moved, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)

    def _lock_name(self, name: str) -> int:
        """Hold the versions of `name` for this process until the descriptor is closed
        (`flock`; the system releases it when a process dies): two submissions of the same
        name number and diff one after the other, without holding the database lock while the
        diff runs. Waits at most `LOCK_WAIT_S` (W4b-01.1 G6), then `RoomError`. Returns the
        descriptor."""
        path = self.run_dir / STAGING_DIR / f".version-{name}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + LOCK_WAIT_S
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(LOCK_POLL_S)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        log.warning("version lock of %s still held after %.0f s", name, LOCK_WAIT_S)
        raise RoomError(
            f"Gerade legt arr eine andere Fassung von „{name}“ an, und das dauert zu lange. "
            "Gib noch einmal ab."
        )

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

    def not_in_note_turn(self) -> None:
        """Refuse a tool that writes, waits or moves the read position while the agent writes
        its handover note (W4b-05b.1, Entscheidung 53); reading tools stay. Raises `RoomError`
        with `NOTE_TURN_REFUSED`; also used by `arr.room.waiting.wait_for_messages`."""
        if self.store.in_note_turn(self.agent_id):
            raise RoomError(NOTE_TURN_REFUSED)

    def _my_channels(self, con: sqlite3.Connection) -> list[_Channel]:
        names = [
            r[0]
            for r in con.execute(
                "SELECT m.channel FROM member m JOIN channel c ON c.name=m.channel "
                "WHERE m.agent=? ORDER BY c.created, c.name",
                (self.agent_id,),
            )
        ]
        return [
            ch
            for n in names
            if (ch := self.store._load_channel(con, n)) is not None
            and not self._foreign_private(ch)
        ]

    def _readable(self, con: sqlite3.Connection) -> list[_Channel]:
        return [ch for ch in self._my_channels(con) if ch.state is not ChannelState.WAITING]

    def _channel(self, con: sqlite3.Connection, name: str, *, write: bool = False) -> _Channel:
        name = _channel_name(name)
        ch = self.store._load_channel(con, name)
        if ch is not None and self._foreign_private(ch):
            ch = None  # W4b-03: another agent's private channel answers like a missing one
        if ch is None or self.agent_id not in ch.members:
            mine = ", ".join(f"#{c.name}" for c in self._my_channels(con)) or "keine"
            if ch is None:
                raise RoomError(f"Den Kanal #{name} gibt es nicht. Deine Kanäle: {mine}.")
            raise RoomError(f"Du bist nicht Mitglied im Kanal #{name}. Deine Kanäle: {mine}.")
        if ch.state is ChannelState.WAITING:
            if ch.config.open_from:
                raise RoomError(
                    f"Der Kanal #{name} ist noch geschlossen. "
                    f"Er öffnet ab Phase „{ch.config.open_from}“."
                )
            raise RoomError(
                f"Der Kanal #{name} ist gerade geschlossen. arr öffnet ihn wieder, wenn die "
                "Phase es vorsieht."
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
        ch = self.store._load_channel(con, row["channel"]) if row is not None else None
        if row is None or (ch is not None and self._foreign_private(ch)):
            # W4b-03: a message in another agent's private channel answers like a missing one
            raise RoomError(f"Die Nachricht #{msg_id} gibt es nicht.")
        if ch is None or self.agent_id not in ch.members:
            raise RoomError(f"Die Nachricht #{msg_id} gibt es in deinen Kanälen nicht.")
        return row, self._channel(con, ch.name, write=write)

    def _foreign_private(self, ch: _Channel) -> bool:
        """The private channel of another agent to the user (W4b-03): for this agent it does
        not exist, whatever its member list says."""
        owner = private_owner(ch.name)
        return owner is not None and owner != self.agent_id

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

    def shown_name(self, agent: str) -> str:
        """How this agent sees `agent`: its alias when an anonymous channel of mine hides it,
        also one that is closed (W4-03 F4), else its ID (W3-09.1, B2; the same rule as
        `_check_result_from`)."""
        with self.store._read() as con:
            for ch in self._my_channels(con):
                if ch.anonymous and agent in ch.members:
                    return ch.display(agent)
        return agent

    def can_read(self, channel: str) -> bool:
        """True if this agent may read `channel` (a member, not waiting for `open_from`)."""
        with self.store._read() as con:
            return any(ch.name == channel for ch in self._readable(con))

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
                if self.agent_id in msg.to and not is_user_question(msg):
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
        self.not_in_note_turn()
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
        self.not_in_note_turn()
        kind = _enum(MessageKind, kind, "Nachrichtenart")
        if kind is MessageKind.VOTE:
            raise RoomError(
                "Abstimmungen im Kanal gibt es nicht mehr. Schreib deine Meinung als Nachricht, "
                "z. B. als „objection“ oder „answer“; was zählt, steht in den Abgaben."
            )
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
                named = mentions(body, ch.members, self.agent_id, ch.resolve)
                if kind is MessageKind.QUESTION and USER in (*targets, *named):
                    # a question to the user goes to the user, other `@name` in its text are
                    # only named, never asked instead of the user (L16, Nachtest V7)
                    named = [USER]
                targets = list(dict.fromkeys([*targets, *named]))
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
            if name.strip().removeprefix("@").casefold() == USER_ADDRESS:
                targets.append(USER)  # the user is a fixed address (L16)
                continue
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

    def set_status(self, status: AgentStatus, note: str = "") -> None:
        """Report `working` or `done` (with a short summary)."""
        self.not_in_note_turn()
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
        self,
        kind: WaitKind,
        seconds: float,
        until_file: str | None = None,
        result_from: str | None = None,
    ) -> WaitState:
        """Record that this agent waits from now for `seconds` (and maybe `until_file` or the
        next result of the agent `result_from`, a real ID)."""
        self.not_in_note_turn()
        until_file = self._check_until_file(until_file)
        now = _now()
        state = WaitState(
            agent=self.agent_id,
            kind=kind,
            since=now,
            until=now + seconds,
            file=until_file,
            result_from=result_from,
        )
        with self.store._write() as con:
            con.execute(
                "INSERT INTO agent_wait(agent, kind, since, until, file, result_from) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(agent, kind) DO UPDATE SET "
                "since=excluded.since, until=excluded.until, file=excluded.file, "
                "result_from=excluded.result_from",
                (self.agent_id, kind.value, state.since, state.until, state.file, result_from),
            )
        return state

    def end_wait(self, kind: WaitKind = WaitKind.MESSAGES) -> None:
        """End this agent's wait of `kind`."""
        self.store.clear_wait(self.agent_id, kind)

    def wait_until(
        self,
        minutes: float,
        until_file: str | None = None,
        until_result_from: str | None = None,
    ) -> WaitState:
        """Ask to be woken after `minutes` (at most 30), on a message, when `until_file` is
        there or when the agent `until_result_from` submits a result (L14); the agent ends its
        turn afterwards (docs/09 §4.3).

        Refused after `WAIT_UNTIL_REPEAT` of them in a row ended by their deadline (L10), for
        an unknown agent, for oneself and for an agent that is done already.
        """
        self.not_in_note_turn()
        if not isinstance(minutes, int | float) or not math.isfinite(minutes) or minutes <= 0:
            raise RoomError("„minutes“ muss eine Zahl größer als 0 sein, z. B. 10.")
        result_from = self._check_result_from(until_result_from)
        if self.wait_guard().until_streak >= WAIT_UNTIL_REPEAT:
            raise RoomError(
                f"Du hast {WAIT_UNTIL_REPEAT}-mal hintereinander mit `wait_until` gewartet, ohne "
                "dass eine Nachricht an dich oder deine Datei kam. Ein weiteres `wait_until` "
                "lehnt arr ab: Arbeite weiter, gib dein Ergebnis ab oder beende den Turn. arr "
                "weckt dich bei einer Nachricht an dich."
            )
        minutes = min(float(minutes), WAIT_UNTIL_MAX_MIN)
        return self.begin_wait(WaitKind.UNTIL, minutes * 60, until_file, result_from)

    def _check_result_from(self, name: str | None) -> str | None:
        """The real ID of the agent named in `until_result_from`, None without a name (L14).

        A name is resolved as in `to` (aliases in anonymous channels); an agent of the run that
        shares no channel with me counts by its ID, unless an anonymous channel of mine hides
        it. Unknown names, myself and agents that are done already raise `RoomError`.
        """
        if name is None or not str(name).strip():
            return None
        wanted = str(name).strip().removeprefix("@")
        if not wanted.strip("@ "):  # only `@`: no name (B4)
            raise RoomError(
                "„until_result_from“ nennt keinen Agenten. Nenne einen Agenten, z. B. so, wie "
                "du ihn mit `@name` ansprichst, oder lass das Feld weg."
            )
        with self.store._read() as con:
            channels = self._readable(con)
            # an anonymous channel hides its members also while it is closed (W4-03 F4)
            hidden = {a for ch in self._my_channels(con) if ch.anonymous for a in ch.members}
            agent = next((a for ch in channels if (a := ch.resolve(wanted)) is not None), None)
            if agent is None and wanted in self.store.agent_ids() and wanted not in hidden:
                agent = wanted
            if agent is None:
                shown = [ch.display(a) for ch in channels for a in ch.members]
                shown += [a for a in self.store.agent_ids() if a not in hidden]
                me = {self.agent_id} | {ch.display(self.agent_id) for ch in channels}  # B5
                names = [n for n in dict.fromkeys(shown) if n not in me]
                raise RoomError(
                    f"Den Agenten „{wanted}“ gibt es in diesem Run nicht. Du kannst auf das "
                    f"Ergebnis von {', '.join(names) or 'niemandem'} warten."
                )
            if agent == self.agent_id:
                raise RoomError(
                    "Auf dein eigenes Ergebnis kannst du nicht warten. Gib es ab oder arbeite "
                    "weiter."
                )
            row = con.execute("SELECT status FROM agent_state WHERE agent=?", (agent,)).fetchone()
        if row is not None and row["status"] == AgentStatus.DONE.value:
            raise RoomError(
                f"„{wanted}“ ist schon fertig, sein Ergebnis liegt vor. Lies es oder frag mit "
                f"@{wanted} nach: Eine Nachricht weckt auch einen fertigen Agenten."
            )
        return agent

    def asked_the_user(self) -> bool:
        """True while a question of mine to the user is open (V10, W4-00): then waiting is
        not advised, a reasoned assumption is."""
        return any(q.sender == self.agent_id for q in self.store.open_user_questions())

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
        """Messages that concern me, as I see them (aliases in anonymous channels)."""
        with self.store._read() as con:
            found: list[tuple[Message, _Channel]] = []
            for ch in self._readable(con):
                found += [
                    (_row_to_message(r), ch)
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

            return [
                self._view(m, ch)
                for m, ch in found
                if address(m, self.agent_id, sender_of) is not None
            ]

    def _file_label(self, con: sqlite3.Connection, channel: str | None) -> str:
        """How my result files are named: by my alias in `channel` or, without it, in my
        first anonymous channel (also a closed one), else by my ID (W4-03 F4)."""
        channels = self._my_channels(con)
        if channel is not None:
            channels = [ch for ch in channels if ch.name == channel] + channels
        for ch in channels:
            if ch.anonymous and ch.members.get(self.agent_id):
                alias = str(ch.members[self.agent_id])
                return "".join(c if c.isalnum() else "-" for c in alias.casefold())
        return self.agent_id

    @staticmethod
    def _sealed(con: sqlite3.Connection, stage: str | None) -> tuple[bool, str | None]:
        if stage is None:
            return False, None
        try:
            row = con.execute("SELECT channel FROM sealed_stage WHERE stage=?", (stage,))
            found = row.fetchone()
        except sqlite3.OperationalError:  # a room from before W4-03
            return False, None
        return (True, found["channel"]) if found is not None else (False, None)

    def submit_result(
        self,
        kind: str,
        title: str,
        body: str | None = None,
        path: str | None = None,
    ) -> Result:
        """Submit a result; a file in `path` is copied to `output/results/NNN-<name>-<kind>`.

        `<name>` is my alias if I am in an anonymous channel, else my ID (W4-03 F4). The copy
        is made in `STAGING_DIR` first; in a sealed stage (`RoomStore.seal`) the file stays in
        `SEALED_DIR` until the stage is released, so no other agent can read it before.
        """
        self.not_in_note_turn()
        if not kind.strip() or not title.strip():
            raise RoomError("„kind“ und „title“ dürfen nicht leer sein.")
        if not (body and body.strip()) and not path:
            raise RoomError("Gib den Inhalt in „body“ an oder eine Datei in „path“.")
        run_dir = self.store.run_dir
        staged: Path | None = None
        final: Path | None = None
        suffix = ""
        if path:
            staging = run_dir / STAGING_DIR
            staging.mkdir(parents=True, exist_ok=True)
            staged = staging / f".staging-{uuid.uuid4().hex}"
            suffix = Path(self._copy_source(path, staged)).suffix
        try:
            with self.store._write() as con:
                assignment = self.store._latest_assignment(con, self.agent_id)
                stage = assignment.stage if assignment else None
                attempt = 1
                if assignment and isinstance(assignment.context.get("attempt"), int):
                    attempt = max(1, assignment.context["attempt"])
                sealed, channel = self._sealed(con, stage)
                cur = con.execute(
                    "INSERT INTO result(ts, agent, stage, kind, title, body, path, attempt) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (_now(), self.agent_id, stage, kind, title, body, None, attempt),
                )
                result_id = cur.lastrowid
                assert result_id is not None
                if sealed:
                    con.execute(
                        "INSERT INTO sealed_result(result_id, stage) VALUES(?,?)",
                        (result_id, stage),
                    )
                if staged is not None:
                    folder = run_dir / (SEALED_DIR if sealed else RESULTS_DIR)
                    folder.mkdir(parents=True, exist_ok=True)
                    safe_kind = "".join(c if c.isalnum() or c in "-_" else "-" for c in kind)
                    label = self._file_label(con, channel)
                    final = folder / f"{result_id:03d}-{label}-{safe_kind}{suffix}"
                    staged.rename(final)
                    staged = None
                    rel = final.relative_to(run_dir).as_posix()
                    con.execute("UPDATE result SET path=? WHERE id=?", (rel, result_id))
                row = con.execute("SELECT * FROM result WHERE id=?", (result_id,)).fetchone()
                result = _row_to_result(row)
            final = None  # committed
            return result
        finally:
            for leftover in (staged, final):
                if leftover is not None:
                    leftover.unlink(missing_ok=True)

    def copy_file(self, rel: str, target: Path) -> str:
        """Copy a regular file of my working folder to `target` (no link followed, no FIFO,
        no hardlink, at most `MAX_ATTACHMENT_BYTES`); returns its name. `RoomError` with a
        German reason otherwise. For arr's own copies, e.g. versions of an artifact (W4-03)."""
        return self._copy_source(rel, target)

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
        self.not_in_note_turn()
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
    # Submissions (docs/10 §4.3, W4b-01)
    # ----------------------------------------------------------------------------------------

    def _shown_label(self, con: sqlite3.Connection, channel: str | None) -> str:
        """How my submissions show me: my alias in `channel` or, without it, in my first
        anonymous channel (also a closed one), else my ID (the rule of `_file_label`)."""
        channels = self._my_channels(con)
        if channel is not None:
            channels = [ch for ch in channels if ch.name == channel] + channels
        for ch in channels:
            if ch.anonymous and ch.members.get(self.agent_id):
                return str(ch.members[self.agent_id])
        return self.agent_id

    def _expectation(self, assignment: Assignment | None) -> Expectation:
        """The submission my newest assignment expects (`context["submission"]`)."""
        raw = assignment.context.get("submission") if assignment is not None else None
        if raw is None:
            raise RoomError(
                "Dein aktueller Auftrag erwartet keine Abgabe mit `submit`. Lies ihn mit "
                "`get_assignment`."
            )
        try:
            return Expectation.model_validate(raw)
        except ValidationError as exc:
            log.warning(
                "agent %s: invalid expectation in assignment (turn %s): %s",
                self.agent_id,
                assignment.turn if assignment else None,
                exc,
            )
            raise RoomError(
                "Dein Auftrag beschreibt die erwartete Abgabe nicht gültig. Das ist ein Fehler "
                "in arr, nicht bei dir: Beende den Turn mit einer kurzen Notiz."
            ) from None

    def submit(
        self,
        status: str,
        summary: str,
        body: str | None = None,
        files: list[str] | None = None,
        note_next: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> Submission:
        """Hand in the submission of my newest assignment (`context["submission"]`).

        Head and `fields` are checked at once against the expected format (`check_submission`);
        `RoomError` names every problem in German, and the agent can try again in the same
        turn. Files are copied from my working folder by the rules of attachments
        (`_copy_source`). Stage and attempt come from the assignment, as for `submit_result`.
        Every accepted submission is filed once (`RoomStore._publish`); in a sealed stage it
        stays in `SEALED_DIR` until `release`.
        """
        self.not_in_note_turn()
        assignment = self.store.assignment(self.agent_id)
        expectation = self._expectation(assignment)
        assert assignment is not None
        try:
            checked = check_submission(
                status=status,
                summary=summary,
                body=body,
                files=files,
                note_next=note_next,
                fields=fields,
                expectation=expectation,
                rubric=self.rubric,
            )
        except SubmissionError as exc:
            raise RoomError(str(exc)) from None
        attempt = assignment.context.get("attempt")
        attempt = (
            max(1, attempt) if isinstance(attempt, int) and not isinstance(attempt, bool) else 1
        )

        def current(con: sqlite3.Connection) -> None:
            latest = self.store._latest_assignment(con, self.agent_id)
            if latest is None or (latest.turn, latest.ts) != (assignment.turn, assignment.ts):
                raise RoomError(
                    "Dein Auftrag hat sich während der Abgabe geändert. Lies ihn mit "
                    "`get_assignment` und gib passend dazu ab."
                )

        return self.store._publish(
            agent=self.agent_id,
            expectation=expectation,
            checked=checked,
            stage=assignment.stage,
            attempt=attempt,
            copy=self._copy_source,
            shown=self._shown_label,
            current=current,
        )

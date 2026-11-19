"""Auto-compact and `context: compact` as data, without input or output (W4b-05b).

docs/10 §4.6, P12: arr knows the fill level of every agent's session and starts a new one in
time. Between two turns of an agent, when its fill level reaches its threshold
(`AgentConfig.compact`), arr asks it in a short turn for a handover note, starts a new session
and puts in front of the next prompt what arr itself knows (the rebuild package). A phase with
`context: compact` does the same with the agent's own `note_next`, without the note turn.

This module holds the parts that need no I/O, so they are tested on their own:

- `decide`: the threshold rule (share of the window or tokens; `at: 100%` turns auto-compact
  off; at most one compaction per working turn; the turn quota must fit note and work turn);
- `AgentContext`: what the orchestrator keeps per agent (fill level, window, failed notes,
  the running action, the package for the next turn);
- `note_from_text`: the note from the last text of the note turn (cleaned, cut);
- `rebuild_package`: the package in front of the first prompt of the new session.

It imports nothing from `arr.room`, `arr.service` or `arr.backends` (A13); the orchestrator
reads the templates and the room and passes plain data.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from arr.models import Compact
from arr.submissions.text import clean_text

__all__ = [
    "ACTION_MAX_CHARS",
    "MAX_NOTE_FAILURES",
    "NOTE_HEADING",
    "NOTE_MAX_CHARS",
    "PACKAGE_HEADING",
    "PACKAGE_MAX_CHARS",
    "PURPOSE",
    "QUESTIONS_MAX",
    "QUESTION_MAX_CHARS",
    "SUBMISSIONS_MAX",
    "SUMMARY_MAX_CHARS",
    "AgentContext",
    "CompactReason",
    "Decision",
    "OwnQuestion",
    "OwnResult",
    "OwnSubmission",
    "Pending",
    "Verdict",
    "cut",
    "decide",
    "note_from_text",
    "rebuild_package",
    "threshold",
]

PURPOSE = "compact"
"""`purpose` of a note turn in the transcript (`turn_start`) and in `orchestrator.jsonl`."""

NOTE_HEADING = "## Übergabe-Notiz"
"""First line of the note prompt (`templates/prompts/compact_note.md`); the FakeBackend
recognizes a note turn by it."""
PACKAGE_HEADING = "## Neue Sitzung"
"""First line of the rebuild package."""

MAX_NOTE_FAILURES = 2
"""Failed note turns in a row per agent; after that only the engine compacts (Entscheidung 48)."""

NOTE_MAX_CHARS = 8_000
ACTION_MAX_CHARS = 6_000
SUMMARY_MAX_CHARS = 400
SUBMISSIONS_MAX = 20
SUBMISSIONS_MAX_CHARS = 8_000
QUESTIONS_MAX = 10
QUESTION_MAX_CHARS = 400
QUESTIONS_MAX_CHARS = 3_000
HEAD_MAX_CHARS = 1_500
PACKAGE_MAX_CHARS = 28_000
"""Limits of the rebuild package in characters: each part and the whole (W4b-05b §6.4)."""

CUT_MARK = "[… gekürzt]"


class CompactReason(StrEnum):
    """Why a session was compacted (`reason` of the entry `compact` in `orchestrator.jsonl`)."""

    AUTO = "auto"
    """arr's auto-compact: the threshold was reached, note turn, new session."""
    PHASE = "phase"
    """`compact_session` at a phase transition (`context: compact`), no note turn."""
    ENGINE = "engine"
    """The engine compacted the session itself (safety net); arr only records it."""


class Verdict(StrEnum):
    """Outcome of `decide`."""

    COMPACT = "compact"
    OFF = "off"
    """`at: 100%`: arr never compacts by itself (Entscheidung 49)."""
    UNKNOWN = "unknown"
    """No fill level (new session, after a compaction, no request yet)."""
    NO_WINDOW = "no_window"
    """A threshold as a share, but the window is unknown."""
    BELOW = "below"
    GAVE_UP = "gave_up"
    """`MAX_NOTE_FAILURES` failed note turns in a row."""
    NO_TURNS = "no_turns"
    """`turns_per_agent` has no room for note turn and working turn."""


@dataclass(frozen=True, slots=True)
class Decision:
    """`decide`: whether to compact now, with the numbers for the log."""

    verdict: Verdict
    tokens: int | None
    window: int | None
    threshold: int | None

    @property
    def compact(self) -> bool:
        return self.verdict is Verdict.COMPACT


def threshold(compact: Compact, window: int | None) -> int | None:
    """The threshold in tokens: `tokens`, or the share of a known window rounded up; None if
    the share cannot be applied (window unknown)."""
    if compact.tokens is not None:
        return compact.tokens
    if window is None or window <= 0 or compact.share is None:
        return None
    percent = round(compact.share * 100)
    return -(-percent * window // 100)  # ceil without floats


def decide(
    compact: Compact,
    *,
    tokens: int | None,
    window: int | None,
    turns_left: int | None,
    failures: int = 0,
) -> Decision:
    """Whether arr compacts the session before the agent's next turn (docs/10 §4.6, P12).

    Reached means `tokens >= threshold`. `at: 100%` turns auto-compact off whatever the
    numbers (a fixed rule, Entscheidung 49). `tokens` is None right after a new session or a
    compaction, so there is never a second compaction before a measurement. `turns_left`
    (None: no limit) must leave room for the note turn and the working turn.
    """
    limit = threshold(compact, window)

    def verdict(value: Verdict) -> Decision:
        return Decision(value, tokens, window, limit)

    if compact.share is not None and compact.share >= 1:
        return verdict(Verdict.OFF)
    if tokens is None:
        return verdict(Verdict.UNKNOWN)
    if limit is None:
        return verdict(Verdict.NO_WINDOW)
    if tokens < limit:
        return verdict(Verdict.BELOW)
    if failures >= MAX_NOTE_FAILURES:
        return verdict(Verdict.GAVE_UP)
    if turns_left is not None and turns_left < 2:
        return verdict(Verdict.NO_TURNS)
    return verdict(Verdict.COMPACT)


@dataclass(frozen=True, slots=True)
class Pending:
    """A new session has begun: the next turn gets the rebuild package, with `note`."""

    reason: CompactReason
    note: str | None = None


@dataclass(slots=True)
class AgentContext:
    """What the orchestrator keeps per agent for compaction (no I/O).

    `session` is the backend's `session_ref()` when `tokens` was measured: if the backend's
    session differs before the next turn (`reset_session`), the fill level is unknown again.
    """

    tokens: int | None = None
    window: int | None = None
    session: Mapping[str, str] | None = None
    failures: int = 0
    window_noted: bool = False
    action_key: tuple[str | None, Any] | None = None
    action: str | None = None
    pending: Pending | None = None

    def measured(self, tokens: Any, window: Any) -> None:
        """A turn ended: its fill level (None stays unknown) and the window if reported."""
        self.tokens = _count(tokens)
        self.window = _count(window) or self.window

    def saw_window(self, window: Any) -> None:
        """A request reported the window (Codex)."""
        self.window = _count(window) or self.window

    def new_session(self) -> None:
        """The session is new: the fill level is unknown, the note failures start over."""
        self.tokens = None
        self.session = None
        self.failures = 0

    def follow(self, session: Mapping[str, str]) -> None:
        """Compare with the backend's session before a turn; another one means a new session."""
        if self.session is not None and dict(session) != dict(self.session):
            self.new_session()

    def start_action(self, stage: str | None, attempt: Any, prompt: str) -> bool:
        """Remember the prompt of the first turn of a stage and attempt; True if it is new."""
        key = (stage, attempt)
        if key == self.action_key and self.action is not None:
            return False
        self.action_key, self.action = key, prompt
        return True


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def cut(text: str, limit: int) -> str:
    """`text` with at most `limit` characters; a cut text ends with „[… gekürzt]“."""
    if len(text) <= limit:
        return text
    keep = max(limit - len(CUT_MARK) - 1, 0)
    return text[:keep].rstrip() + "\n" + CUT_MARK


def _clean(value: Any) -> str:
    return clean_text(value).strip()


def note_from_text(text: str | None) -> str | None:
    """The handover note: the last text of the note turn, cleaned and cut to
    `NOTE_MAX_CHARS`; None if it is empty (the note turn failed)."""
    note = _clean(text)
    return cut(note, NOTE_MAX_CHARS) if note else None


# --------------------------------------------------------------------------------------------
# Rebuild package
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnSubmission:
    """One of the agent's submissions (`RoomStore.submissions`), as plain data."""

    id: int
    format: str
    status: str
    summary: str
    path: str
    name: str | None = None
    version: int | None = None
    stage: str | None = None
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class OwnResult:
    """One of the agent's results of an old topology (`submit_result`)."""

    id: int
    kind: str
    title: str
    path: str | None = None
    stage: str | None = None


@dataclass(frozen=True, slots=True)
class OwnQuestion:
    """An open question of the agent to the user."""

    id: int
    channel: str
    text: str


@dataclass(slots=True)
class _Section:
    title: str
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        return f"### {self.title}\n\n" + "\n".join(self.lines)


def _line(value: Any, limit: int) -> str:
    """One line of agent text: cleaned, on one line, cut."""
    return cut(" ".join(_clean(value).split()), limit)


def _submission(entry: OwnSubmission) -> str:
    title = f"#{entry.id}"
    if entry.name:
        title += f" „{_line(entry.name, 80)}“"
        if entry.version is not None:
            title += f" Fassung {entry.version}"
    parts = [f"Format {_line(entry.format, 40)}", f"Status {_line(entry.status, 40)}"]
    if entry.stage:
        parts.append(f"Phase {_line(entry.stage, 60)}")
    if entry.attempt > 1:
        parts.append(f"Durchlauf {entry.attempt}")
    text = f"- {title} · " + " · ".join(parts) + f"\n  Pfad: {_line(entry.path, 300)}"
    summary = _line(entry.summary, SUMMARY_MAX_CHARS)
    return text + (f"\n  Kurzfassung: {summary}" if summary else "")


def _result(entry: OwnResult) -> str:
    text = f"- #{entry.id} · {_line(entry.kind, 40)} · „{_line(entry.title, 200)}“"
    if entry.stage:
        text += f" · Phase {_line(entry.stage, 60)}"
    return text + (f"\n  Pfad: {_line(entry.path, 300)}" if entry.path else "")


def _bounded(entries: Sequence[str], *, most: int, limit: int, dropped: str) -> list[str]:
    """The newest `most` entries that fit `limit` characters; a note on what was left out."""
    kept: list[str] = []
    size = 0
    for text in reversed(entries[-most:]):
        if size + len(text) + 1 > limit:
            break
        kept.insert(0, text)
        size += len(text) + 1
    left_out = len(entries) - len(kept)
    if left_out:
        kept.insert(0, dropped.format(n=left_out))
    return kept


def rebuild_package(
    head: str,
    *,
    action: str | None,
    action_below: bool = False,
    submissions: Sequence[OwnSubmission] = (),
    results: Sequence[OwnResult] = (),
    questions: Sequence[OwnQuestion] = (),
    note: str | None = None,
) -> str:
    """The package in front of the first prompt of a new session (W4b-05b §6.4).

    In this order: `head` (the session began anew, the files are unchanged), the running
    action (`action_below`: this turn's own prompt is that action, it follows below), the
    agent's own submissions (or results of an old topology) with paths, its open questions to
    the user and its own note. Every part and the whole have fixed limits; agent text is
    cleaned; a cut part says so. Unread messages come with the inbox, not here.
    """
    sections: list[_Section] = []
    if action_below:
        sections.append(_Section("Dein laufender Auftrag", ["Er steht unten (dieser Turn)."]))
    elif action and _clean(action):
        sections.append(_Section("Dein laufender Auftrag", [cut(_clean(action), ACTION_MAX_CHARS)]))
    own = [_submission(s) for s in submissions] + [_result(r) for r in results]
    lines = (
        _bounded(
            own,
            most=SUBMISSIONS_MAX,
            limit=SUBMISSIONS_MAX_CHARS,
            dropped="- [… {n} ältere Abgaben weggelassen]",
        )
        if own
        else ["Keine."]
    )
    sections.append(_Section("Deine Abgaben", lines))
    if questions:
        asked = [
            f"- #{q.id} in #{_line(q.channel, 60)}: {_line(q.text, QUESTION_MAX_CHARS)}"
            for q in questions
        ]
        sections.append(
            _Section(
                "Deine offenen Fragen an den User",
                _bounded(
                    asked,
                    most=QUESTIONS_MAX,
                    limit=QUESTIONS_MAX_CHARS,
                    dropped="- [… {n} ältere Fragen weggelassen]",
                ),
            )
        )
    if note and _clean(note):
        sections.append(
            _Section(
                "Deine eigene Notiz",
                [
                    "Diese Notiz hast du selbst geschrieben, bevor die Sitzung neu begann:",
                    "",
                    cut(_clean(note), NOTE_MAX_CHARS),
                ],
            )
        )
    parts = [PACKAGE_HEADING, cut(_clean(head), HEAD_MAX_CHARS)]
    parts += [section.render() for section in sections]
    return cut("\n\n".join(parts), PACKAGE_MAX_CHARS)

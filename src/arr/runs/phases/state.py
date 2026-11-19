"""The state of a phases run over its phases (W4b-02b §3.2): visits, passes, scores, the valid
submission of every phase and the list of objections. Only data; the runner fills it."""

from dataclasses import dataclass, field
from typing import Any

from arr.models import ResolvedTask
from arr.room.types import Submission
from arr.runs.phases.gates import GateOutcome, ObjectionList

__all__ = ["HOW_NORMAL", "HOW_RETRY", "HOW_USER", "Pass", "PassRecord", "RunState", "Slot"]

HOW_NORMAL = "normal"
"""A phase is entered by a route (`next`, `goto`, the start): context and folder apply."""
HOW_RETRY = "retry"
"""The same phase once more by the route `retry`: feedback, no change of context or folder."""
HOW_USER = "user_retry"
"""Once more because the user chose so (beyond `max_visits`): like `retry`, with the hint."""


@dataclass
class Slot:
    """One action of a pass: who, which (`Expectation.action`), the assignment, the entry of
    an `over` list (index from 0 and its data), and the newest submission found for it."""

    agent: str
    action: str
    task: ResolvedTask
    text: str
    entry: int | None = None
    item: dict[str, Any] | None = None
    found: Submission | None = None
    turns: int = 0


@dataclass
class Pass:
    """A pass of a phase while it runs."""

    phase: str
    attempt: int
    how: str
    hint: str | None = None
    slots: list[Slot] = field(default_factory=list)
    sealed: bool = False
    raters: list[Slot] = field(default_factory=list)
    list_missing: str | None = None
    """Why the `over` list is missing or empty (then the gate has no result)."""


@dataclass
class PassRecord:
    """A finished pass: its submissions, the gate and where the run went."""

    phase: str
    attempt: int
    how: str
    latest: list[Submission]
    missing: list[str]
    gate: str
    outcome: GateOutcome
    ratings: list[dict[str, Any]] = field(default_factory=list)
    rater_reviews: list[Submission] = field(default_factory=list)
    hint: str | None = None


@dataclass
class RunState:
    """Visits of every phase (every entry counts, the first too), the finished passes, the
    mean scores of rated submissions, the valid submissions of every phase in the order the
    phases were finished last, and the run's objections."""

    visits: dict[str, int] = field(default_factory=dict)
    records: dict[str, list[PassRecord]] = field(default_factory=dict)
    scores: dict[int, float] = field(default_factory=dict)
    valid: dict[str, list[Submission]] = field(default_factory=dict)
    finished: list[str] = field(default_factory=list)
    objections: ObjectionList = field(default_factory=ObjectionList)

    def last(self, phase: str) -> PassRecord | None:
        """The last finished pass of `phase`."""
        records = self.records.get(phase)
        return records[-1] if records else None

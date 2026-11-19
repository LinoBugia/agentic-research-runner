"""Agent text in the context package and in the inbox: data, cleaned and limited (W4b-02b §6.3;
docs/10 §7).

Summaries, texts, names of files, comments of raters and objections come from agents. They
enter a context package only as values of the package (JSON strings, never as Markdown of the
prompt), cleaned by `clean_text` (no control, invisible or tag characters) and cut to fixed
limits per part; the whole package has a limit as well (`fit_package`). Names and other short
values are also put on one line. The limits are the contract of the package; the report of
W4b-02b gives the reasons.
"""

import json
from collections.abc import Callable
from typing import Any

from arr.submissions.text import clean_text

__all__ = [
    "MAX_CHANNEL_MESSAGES",
    "MAX_COMMENT_CHARS",
    "MAX_ENTRY_TEXT_CHARS",
    "MAX_FULL_CHARS",
    "MAX_HINT_CHARS",
    "MAX_INBOX_CHARS",
    "MAX_INBOX_ITEMS",
    "MAX_INBOX_LINE",
    "MAX_INPUT_ITEMS",
    "MAX_MESSAGE_CHARS",
    "MAX_NAME_CHARS",
    "MAX_OBJECTION_CHARS",
    "MAX_OBJECTIONS",
    "MAX_PACKAGE_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TASK_BYTES",
    "FEEDBACK_CORE",
    "MAX_TITLE_CHARS",
    "TRUNCATED",
    "block",
    "fit_package",
    "line",
    "package_size",
]

MAX_TASK_BYTES = 64 * 1024
"""An assignment file (`task`): a long page of instructions; more is a mistake, not a task."""
MAX_NAME_CHARS = 100
"""Names of agents, files, formats, phases in the package."""
MAX_TITLE_CHARS = 200
"""Title of an entry of a list (the limit of the format `list`)."""
MAX_SUMMARY_CHARS = 800
"""A summary (the limit of `submit`)."""
MAX_ENTRY_TEXT_CHARS = 4_000
"""Text of an entry of a list (the limit of the format `list`)."""
MAX_FULL_CHARS = 20_000
"""`content: full`: body and text files of one submission together."""
MAX_COMMENT_CHARS = 1_000
"""Comment of a rater in the feedback."""
MAX_OBJECTION_CHARS = 300
"""One objection of the list (as `review.OBJECTION_CHARS` of the old topology)."""
MAX_OBJECTIONS = 100
"""Objections of a run (as `review.MAX_OBJECTIONS`)."""
MAX_HINT_CHARS = 2_000
"""The user's hint with „noch ein Durchlauf“."""
MAX_MESSAGE_CHARS = 2_000
"""One message of a channel given as input."""
MAX_CHANNEL_MESSAGES = 100
"""Messages of a channel given as input (the newest)."""
MAX_INPUT_ITEMS = 200
"""Submissions of all inputs of one package (`pick: all` over many passes)."""
MAX_PACKAGE_CHARS = 100_000
"""The whole package as JSON: about 25 000 tokens, a fair share of a context window."""
MAX_INBOX_LINE = 200
"""One submission in the summary of an escalation, on one line."""
MAX_INBOX_ITEMS = 20
"""Submissions listed in the summary of an escalation; the rest is counted."""
MAX_INBOX_CHARS = 4_000
"""The whole summary of an escalation."""

TRUNCATED = " …[gekürzt]"


def line(value: Any, limit: int = MAX_NAME_CHARS) -> str:
    """Agent text on one line: cleaned, white space joined, cut to `limit` characters."""
    text = " ".join(clean_text(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def block(value: Any, limit: int) -> str:
    """Agent text with its lines: cleaned, cut to `limit` characters with a mark."""
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATED))] + TRUNCATED


def package_size(package: Any) -> int:
    """Length of a package as the JSON the prompt shows."""
    return len(json.dumps(package, ensure_ascii=False))


FEEDBACK_CORE = ("attempt", "gate", "result", "score", "user_hint")
"""What stays of the feedback when everything else had to go: the result of the gate."""


def fit_package(package: dict[str, Any], limit: int = MAX_PACKAGE_CHARS) -> dict[str, Any]:
    """Make a package fit `limit` (W4b-02b.1, M2; Entscheidung 58). The parts are cut in a
    fixed order, each from its end, before the next part is touched, and each step only while
    the package is too large (so a later part may still be whole when an earlier one is gone):

    1. the full texts of inputs (longest first, each with a note to read it under `path`),
    2. inside the feedback: the comments of the raters (from the end), then the ratings, then
       the objections of the reviews, then the reviews, then the summaries of the submissions,
    3. the inputs from the end, 4. the peers from the end,
    5. the feedback down to its core (`FEEDBACK_CORE`: pass, gate, result, score, the user's
       hint), 6. the objections from the end.

    Task, research question and the expected submission always stay. Every list that was cut
    gets `<key>_cut: true` (`feedback_cut` for the feedback)."""
    if package_size(package) <= limit:
        return package
    inputs = [i for i in package.get("inputs") or [] if isinstance(i, dict)]
    for item in sorted(inputs, key=lambda i: -len(str(i.get("text", "")))):
        if not item.get("text"):
            continue
        item["text"] = None
        item["note"] = "Der Text ist für das Kontextpaket zu lang; lies ihn unter „path“."
        if package_size(package) <= limit:
            return package
    feedback = package.get("feedback")
    feedback = feedback if isinstance(feedback, dict) else {}
    ratings = feedback.get("ratings") if isinstance(feedback.get("ratings"), list) else []
    reviews = feedback.get("reviews") if isinstance(feedback.get("reviews"), list) else []
    steps: list[tuple[str, Callable[[], bool]]] = [
        ("feedback", lambda: _drop_last_key(ratings, "comment")),
        ("feedback", lambda: _drop_last(ratings)),
        ("feedback", lambda: _drop_last_key(reviews, "objections")),
        ("feedback", lambda: _drop_last(reviews)),
        ("feedback", lambda: _drop_last(feedback.get("submissions"))),
        ("inputs", lambda: _drop_last(package.get("inputs"))),
        ("peers", lambda: _drop_last(package.get("peers"))),
        ("feedback", lambda: _to_core(feedback)),
        ("objections", lambda: _drop_last(package.get("objections"))),
    ]
    for key, step in steps:
        while package_size(package) > limit and step():
            package[f"{key}_cut"] = True
    return package


def _drop_last_key(items: list[Any], key: str) -> bool:
    """Remove `key` from the last entry that still has it."""
    for entry in reversed(items):
        if isinstance(entry, dict) and key in entry:
            del entry[key]
            return True
    return False


def _to_core(feedback: dict[str, Any]) -> bool:
    extra = [k for k in feedback if k not in FEEDBACK_CORE and k != "note"]
    if not extra:
        return False
    for key in extra:
        del feedback[key]
    feedback["note"] = "Das Feedback war zu lang für das Kontextpaket; hier nur das Ergebnis."
    return True


def _drop_last(items: Any) -> bool:
    if isinstance(items, list) and items:
        items.pop()
        return True
    return False

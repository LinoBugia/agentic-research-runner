"""Agent text for the new displays of runs with phases (W4b-07.1, B4; docs/08 §7.4, docs/10 §7):
one cleaning rule.

Both helpers use `arr.submissions.text.clean_text`, the rule of the texts that go to Claude
Code (`user_api.agent_text`). It removes every control character but tab and line break,
every format character (bidi controls, zero-width characters, word joiner, BOM, U+061C) and the
tag characters. So the phase plan, the submission cards, the escalation lines and
`read_results` show the same text. Escaping for HTML stays with the caller (`esc`).
"""

from typing import Any

from arr.submissions.text import clean_text

__all__ = ["block", "line"]


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def line(value: Any, limit: int = 120) -> str:
    """One short line: cleaned, every run of white space (line breaks included) one space."""
    raw = str(value if value is not None else "")
    text = " ".join(clean_text(raw[: limit * 8 + 64]).split())
    return _cut(text, limit) if len(raw) <= limit * 8 + 64 else _cut(text, limit - 1) + "…"


def block(value: Any, limit: int = 2_000) -> str:
    """A text with its line breaks (and tabs), cleaned and cut after `limit` characters."""
    raw = str(value if value is not None else "")
    return _cut(clean_text(raw[: limit * 2 + 64]), limit)

"""Text of agents without invisible or controlling characters (G1, W4-01.1; docs/10 §7).

Moved here from `arr.service.user_api` in W4b-01, so the room (which imports nothing from
`arr.service`) cleans submissions by the same rule as the texts that go to Claude Code.
"""

import unicodedata
from typing import Any

__all__ = ["clean_text", "has_hidden"]

_KEEP = {"\t", "\n"}
_LINE_BREAKS = {"\r", "\u0085", " ", " "}


def _hidden(char: str) -> bool:
    return unicodedata.category(char) in ("Cc", "Cf") or 0xE0000 <= ord(char) <= 0xE01EF


def clean_text(value: Any) -> str:
    """Text without invisible or controlling characters (G1, W4-01.1).

    Removed: every C0 and C1 control character (category `Cc`) except tab and line break, and
    every format character (`Cf`): bidi controls, zero-width characters, word joiner, BOM, soft
    hyphen, the Unicode tag characters U+E0000–U+E007F and the supplementary variation
    selectors up to U+E01EF, which can carry instructions that no reader sees. Line and
    paragraph separators (and CR, NEL) become a plain line break.
    """
    out = []
    for char in str(value or ""):
        if char in _KEEP:
            out.append(char)
        elif char in _LINE_BREAKS:
            out.append("\n")
        elif _hidden(char):
            continue
        else:
            out.append(char)
    return "".join(out)


def has_hidden(value: str) -> bool:
    """True if `value` holds a character `clean_text` would change, tab and `\\n` included
    (for names and paths, which must be plain)."""
    return any(char in _KEEP or char in _LINE_BREAKS or _hidden(char) for char in value)

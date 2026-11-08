"""Building blocks shared by all components: escaping, HTML snippets, native buttons.

Static parts are rendered as escaped HTML (`raw`), interactive parts as native elements with
NiceGUI events (`button`), so focus and keyboard behaviour stay under our control
(docs/08-ux-konzept.md G2, K5, K8, §7.7).
"""

import html
import re
from collections.abc import Awaitable, Callable
from typing import Any

import markdown2
from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement

Handler = Callable[[], Any] | Callable[[], Awaitable[Any]]


def esc(value: object) -> str:
    """Escape text for HTML (also quotes, for attributes)."""
    return html.escape(str(value), quote=True)


def icon(name: str, *, small: bool = False, pulse: bool = False) -> str:
    """Material icon as HTML, hidden from screen readers (the word next to it carries meaning)."""
    cls = "ico" + (" ico-sm" if small else "") + (" pulse" if pulse else "")
    return f'<span class="{cls}" aria-hidden="true">{esc(name)}</span>'


def raw(markup: str, *, tag: str = "div", classes: str = "") -> ui.html:
    """Insert trusted, already escaped HTML."""
    element = ui.html(markup, sanitize=False, tag=tag)
    if classes:
        element.classes(classes)
    return element


def text(tag: str, content: str, *, classes: str = "") -> TextElement:
    """A native element with plain text, e.g. `text("h1", "Freigaben")`."""
    element = TextElement(tag=tag, text=content)
    if classes:
        element.classes(classes)
    return element


def button(
    label: str,
    on_click: Handler | None = None,
    *,
    classes: str = "btn",
    primary: bool = False,
    grant: bool = False,
    key_hint: str | None = None,
    disabled: bool = False,
    title: str | None = None,
    js: str | None = None,
) -> ui.element:
    """A native `<button>` (docs/08 §8.1: label is a verb).

    `primary` marks the card's or dialog's primary action for ⌘↩ (`data-primary`). `grant`
    makes Space/Enter on the focused button do nothing (G2: no single-key grants). `js` runs
    a client-side handler instead of a server roundtrip (e.g. closing a dialog).
    """
    element = ui.element("button").classes(classes).props('type="button"')
    if primary:
        element.props("data-primary")
    if grant:
        element.props("data-grant")
    if title:
        element.props(f'title="{esc(title)}"')
    with element:
        markup = esc(label)
        if key_hint:
            markup += f' <span class="k" aria-hidden="true">{esc(key_hint)}</span>'
        raw(markup, tag="span")
    if js and on_click is None:
        element.on("click", js_handler=js)
    elif on_click is not None:
        element.on("click", lambda _e: on_click())
    set_disabled(element, disabled)
    return element


def set_disabled(element: ui.element, disabled: bool) -> None:
    """Disable a native button (`disabled` + `aria-disabled`) or enable it again."""
    if disabled:
        element.props('disabled aria-disabled="true"')
    else:
        element.props(remove="disabled aria-disabled")


def link_html(label: str, href: str, *, classes: str = "") -> str:
    """An `<a>` element as HTML."""
    cls = f' class="{esc(classes)}"' if classes else ""
    return f'<a href="{esc(href)}"{cls}>{esc(label)}</a>'


# --- Markdown with formulas ---------------------------------------------------------------------
# `$…$` and `$$…$$` become a span with readable Unicode text (W1-06) that carries the LaTeX
# source in `data-tex`; pages that load static/math.js typeset it with KaTeX as MathML (G7,
# W1-07). Without JavaScript or KaTeX the Unicode text stays.
#
# Inline `$…$` follows Pandoc (W3-10 U9): the opening `$` has no space to its right, the
# closing one none to its left and no digit after it, and a formula never spans a blank line.
# So amounts like „≈ 0,28 $ … ≈ 0,14 $“ stay text, `$σ(n)$` is a formula. `\$` is a dollar,
# and code (`…`, fenced blocks) is never a formula.

_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$"
    r"|(?<![\\$])\$(?![\s$])((?:\\.|[^$\\])+?)(?<![\s\\])\$(?![\d$])",
    re.S,
)
_CODE_RE = re.compile(r"(`{3,}|~{3,}).*?\1|`[^`\n]+`", re.S)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_TEX = {
    r"\pmod": " mod ",
    r"\bmod": " mod ",
    r"\iff": " ⟺ ",
    r"\cdot": "·",
    r"\ln": "ln",
    r"\log": "log",
    r"\mathbb{N}": "ℕ",
    r"\mathbb{Z}": "ℤ",
    r"\le": "≤",
    r"\ge": "≥",
    r"\,": " ",
    r"\ ": " ",
    "{,}": ",",
}
_SUP = str.maketrans("0123456789+-k", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ᵏ")


def _tex_to_text(tex: str) -> str:
    out = tex
    for key, value in _TEX.items():
        out = out.replace(key, value)
    out = re.sub(r"\^\{([0-9+\-k]+)\}", lambda m: m.group(1).translate(_SUP), out)
    out = re.sub(r"\^([0-9k])", lambda m: m.group(1).translate(_SUP), out)
    out = re.sub(r"\\mod\{?(\w+)\}?", r"mod \1", out)
    out = out.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", out).strip()


def markdown_html(source: str) -> str:
    """Render Markdown safely (raw HTML escaped) with `$…$` shown as math text."""
    spans: list[str] = []

    def keep(match: re.Match[str]) -> str:
        display = match.group(1) is not None
        tex = match.group(1) or match.group(2) or ""
        cls = "math math-display" if display else "math"
        spans.append(
            f'<span class="{cls}" title="{esc(tex)}" data-tex="{esc(tex)}">'
            f"{esc(_tex_to_text(tex))}</span>"
        )
        return f"ARRMATH{len(spans) - 1}END"

    codes: list[str] = []

    def code(match: re.Match[str]) -> str:
        codes.append(match.group(0))
        return f"ARRCODE{len(codes) - 1}END"

    def keep_inline(match: re.Match[str]) -> str:
        if match.group(2) is not None and _BLANK_LINE.search(match.group(2)):
            return match.group(0)  # two paragraphs are no formula
        return keep(match)

    marked = _MATH_RE.sub(keep_inline, _CODE_RE.sub(code, source)).replace("\\$", "ARRDOLLAR")
    marked = re.sub(r"ARRCODE(\d+)END", lambda m: codes[int(m.group(1))], marked)
    # `_` inside a word is no emphasis: run IDs and snake_case keep their underscores
    extras = {"fenced-code-blocks": None, "middle-word-em": False}
    rendered = markdown2.markdown(marked, safe_mode="escape", extras=extras)
    rendered = rendered.replace("ARRDOLLAR", "$")
    return re.sub(r"ARRMATH(\d+)END", lambda m: spans[int(m.group(1))], rendered)

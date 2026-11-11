"""Building blocks shared by all components: escaping, HTML snippets, native buttons.

Static parts are rendered as escaped HTML (`raw`), interactive parts as native elements with
NiceGUI events (`button`), so focus and keyboard behaviour stay under our control
(docs/08-ux-konzept.md G2, K5, K8, §7.7).
"""

import html
import re
import secrets
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
#
# Code and formulas reach markdown2 as placeholders (W3-10.1): control characters with a random
# nonce per call, so no text of an agent can stand for one (`ARRMATH0END` in a summary made the
# page fail with 500), and an unknown index stays text. Code is rendered here, not by markdown2:
# its table of code pieces is replaced over the whole text once per piece, quadratic for long
# texts with many pieces (400 KB in 9 s); placeholders keep it linear.

_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$"
    r"|(?<![\\$])\$(?![\s$])((?:\\.|[^$\\])+?)(?<![\s\\])\$(?![\d$])",
    re.S,
)
_CODE_RE = re.compile(r"(`{3,}|~{3,}).*?\1|``(?:[^`\n]|`(?!`))+``|`[^`\n]+`", re.S)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_PH_OPEN, _PH_CLOSE = "\x02", "\x03"
"""Placeholders are `\\x02<kind><nonce><index>\\x03`; markdown2 passes them through as text."""
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


def _is_external(src: str) -> bool:
    """True for an image address the browser would fetch from elsewhere: any scheme
    (`https:`, `data:`, `javascript:` …) or a host (`//host`, `/\\host`). Whitespace and
    control characters are dropped and backslashes read as slashes, as browsers do."""
    url = re.sub(r"[\x00-\x20\x7f]", "", html.unescape(src)).replace("\\", "/").lower()
    return url.startswith("//") or re.match(r"[a-z][a-z0-9+.\-]*:", url) is not None


def _image_as_text(match: re.Match[str]) -> str:
    """An image with an external address as text, never loaded (W4-06.1, review finding 2):
    an agent without network would otherwise send data to any host through the user's
    browser. Relative images stay for `run_links_html` (files of the run)."""
    tag = match.group(0)
    src = _ATTR_SRC.search(tag)
    if src is not None and not _is_external(src.group(1)):
        return tag
    alt = _ATTR_ALT.search(tag)
    label = alt.group(1) if alt is not None and alt.group(1) else "ohne Titel"
    title = f' title="{src.group(1)}"' if src is not None else ""
    return f'<span class="ext-img"{title}>Bild: {label} (externe Adresse, nicht geladen)</span>'


_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_SRC = re.compile(r'\bsrc="([^"]*)"', re.I)
_ATTR_ALT = re.compile(r'\balt="([^"]*)"', re.I)
_TAG = re.compile(r"<[A-Za-z][^>]*>")
TABLE_LINE_MAX = 2000
"""Tables only when no line with `|` is longer (markdown2's table search is quadratic in the
length of such a line, review W4-06 finding 1)."""


def _table_safe(text: str) -> bool:
    return all(len(line) <= TABLE_LINE_MAX for line in text.split("\n") if "|" in line)


def markdown_html(source: str, *, tables: bool = False) -> str:
    """Render Markdown safely (raw HTML escaped) with `$…$` shown as math text.

    `tables` renders Markdown tables; only for text arr writes itself (`summary.md`), and only
    while no line with `|` exceeds `TABLE_LINE_MAX` (W4-06.1). Images with an external
    address become text (`_image_as_text`). A text markdown2 cannot render (e.g. 300 nested
    quotes: RecursionError) is shown escaped as it is, so the page still loads.
    """
    try:
        return _markdown_html(source, tables=tables and _table_safe(source))
    except RecursionError:
        return f'<pre class="md-raw">{esc(source)}</pre>'


def _markdown_html(source: str, *, tables: bool) -> str:
    nonce = secrets.token_hex(6)
    pieces: dict[str, list[str]] = {"B": [], "C": [], "M": []}
    raws: dict[str, list[str]] = {"B": [], "C": [], "M": []}  # source, for code in a formula
    # and for a placeholder inside an attribute (title, alt, href)

    def hold(kind: str, html_text: str, raw_text: str = "") -> str:
        pieces[kind].append(html_text)
        raws[kind].append(raw_text)
        return f"{_PH_OPEN}{kind}{nonce}{len(pieces[kind]) - 1}{_PH_CLOSE}"

    def code(match: re.Match[str]) -> str:
        whole, fence = match.group(0), match.group(1)
        if fence is None:  # `code` or ``code with ` inside``
            ticks = 2 if whole.startswith("``") else 1
            inner = whole[ticks:-ticks]
            if ticks == 2 and inner.startswith(" ") and inner.endswith(" ") and inner.strip():
                inner = inner[1:-1]
            return hold("C", f"<code>{esc(inner)}</code>", whole)
        line_start = source.rfind("\n", 0, match.start()) + 1
        indent = source[line_start : match.start()]
        inner = whole[len(fence) : -len(fence)]
        info, newline, body = inner.partition("\n")
        if indent.strip() or not newline:  # ``` inside a line: a code span
            return hold("C", f"<code>{esc(inner.strip())}</code>", whole)
        lines = body.split("\n")
        if lines and not lines[-1].strip():
            lines.pop()  # the indentation before the closing fence
        width = len(indent)
        text = "\n".join(line[width:] if line[:width].isspace() else line for line in lines)
        lang = info.strip().split(" ")[0]
        cls = f' class="language-{esc(lang)}"' if lang else ""
        block = hold("B", f"<pre><code{cls}>{esc(text)}\n</code></pre>", whole)
        return f"\n\n{indent}{block}\n\n"  # a block of its own, also inside a list item

    def unmask(text: str) -> str:
        """Code placeholders back to their source (code written inside a formula)."""

        def source_of(m: re.Match[str]) -> str:
            table = raws[m.group(1)]
            return table[int(m.group(2))] if int(m.group(2)) < len(table) else m.group(0)

        return re.sub(rf"{_PH_OPEN}([BC]){nonce}(\d+){_PH_CLOSE}", source_of, text)

    def keep(match: re.Match[str]) -> str:
        display = match.group(1) is not None
        tex = unmask(match.group(1) or match.group(2) or "")
        cls = "math math-display" if display else "math"
        return hold(
            "M",
            f'<span class="{cls}" title="{esc(tex)}" data-tex="{esc(tex)}">'
            f"{esc(_tex_to_text(tex))}</span>",
            unmask(match.group(0)),
        )

    def keep_inline(match: re.Match[str]) -> str:
        if match.group(2) is not None and _BLANK_LINE.search(match.group(2)):
            return match.group(0)  # two paragraphs are no formula
        return keep(match)

    dollar = f"{_PH_OPEN}D{nonce}{_PH_CLOSE}"
    marked = _MATH_RE.sub(keep_inline, _CODE_RE.sub(code, source))
    marked = marked.replace("\\$", dollar)
    # `_` inside a word is no emphasis: run IDs and snake_case keep their underscores.
    # Lists right after a line, as `summary.md` writes them (W4-06); tables only on request.
    extras: dict[str, Any] = {
        "fenced-code-blocks": None,
        "middle-word-em": False,
        "cuddled-lists": None,
    }
    if tables:
        extras["tables"] = None
    rendered = markdown2.markdown(marked, safe_mode="escape", extras=extras)
    placeholder = re.compile(rf"{_PH_OPEN}([BCM]){nonce}(\d+){_PH_CLOSE}")

    def back(match: re.Match[str]) -> str:
        table = pieces[match.group(1)]
        index = int(match.group(2))
        return table[index] if index < len(table) else match.group(0)

    def source_back(match: re.Match[str]) -> str:
        table = raws[match.group(1)]
        index = int(match.group(2))
        return esc(table[index]) if index < len(table) else ""

    def in_attributes(match: re.Match[str]) -> str:
        """Inside a tag only the source of a formula or code, never its HTML (review W4-06
        finding 4: a `<span>` in a link title broke out of the attribute)."""
        return placeholder.sub(source_back, match.group(0))

    rendered = re.sub(rf"<p>({_PH_OPEN}B{nonce}\d+{_PH_CLOSE})</p>", r"\1", rendered)
    rendered = _TAG.sub(in_attributes, rendered).replace(dollar, "$")
    rendered = _IMG_TAG.sub(_image_as_text, rendered)  # on the final address
    return placeholder.sub(back, rendered)

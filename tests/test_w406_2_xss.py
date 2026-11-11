"""W4-06.2 N1 (Nachprüfung W4-06.1): agent text never runs script in the GUI.

markdown2's safe mode escaped whole tags but left `<a` before another tag raw: `<a<b>
style="animation:…" onanimationstart="…" x=` became an element with an event handler that
fired without a click (measured in the built-in browser). Two walls now:

1. `markdown_html` escapes every `<` of the text before markdown2 and rebuilds its output
   from a list of tags and attributes (`clean_html`).
2. The CSP carries `script-src-attr 'none'`: no inline handler runs (`test_w406_1.py`).

The first two tests are the reviewer's (`W4-06_Review_Skripte/review_w4061_xss.py`).
Real files and the NiceGUI user simulation on a RealApi; no model.
"""

import random
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from service_kit import append, make_run_folder, transcript_line
from test_gui_real import see
from test_gui_runs_real import make_api, real_user, wf

from arr.models import RunStatus
from arr.room.store import RoomStore
from arr.service.gui.components.base import clean_html, markdown_html

__all__ = ["wf"]

MARKDOWN_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td",
    "ul", "ol", "li", "code", "pre", "em", "strong", "a", "blockquote", "hr", "br", "span",
    "del", "sup", "sub", "img",
}  # fmt: skip
PAYLOADS = [
    'Probe <a<b> style="animation:arr-pulse 1s" onanimationstart="window.__xss=1" x=',
    "Probe <x<i> tabindex=0 autofocus onfocus=window.__xss=2 x=",
    'Probe <a</b> onmouseover="window.__xss=3" x=',
    'Probe <a<br/> onclick="window.__xss=4" x=',
    'Probe <A<B> onpointerenter="window.__xss=5" x=',
    'Probe <a<!-- --> onanimationstart="window.__xss=6" x=',
]
HANDLERS = [
    "onanimationstart", "onanimationend", "ontransitionend", "onfocus", "onfocusin",
    "onblur", "onmouseover", "onmouseenter", "onpointerenter", "onpointerover", "onclick",
    "onauxclick", "onload", "onerror", "ontoggle", "onbeforetoggle", "onscroll", "onwheel",
    "onkeydown", "oninput", "onpaste", "oncopy", "onbegin", "onstart", "onwebkitanimationstart",
]  # fmt: skip
OWN = [
    *[f'<a<b> {h}="window.__xss=7" x=' for h in HANDLERS],
    *[f'<img src=x {h}="window.__xss=8">' for h in HANDLERS],
    "<svg><animate onbegin=window.__xss=9 attributeName=x dur=1s>",
    "<svg/onload=window.__xss=10>",
    "<math><mtext><table><mglyph><style><img src=x onerror=window.__xss=11>",
    "<!--><img src=x onerror=window.__xss=12>-->",
    "<!-- --!><img src=x onerror=window.__xss=13>",
    "</p><img src=x onerror=window.__xss=14>",
    "</ x><img src=x onerror=window.__xss=15>",
    "<?xml x?><img src=x onerror=window.__xss=16>",
    "<![CDATA[<img src=x onerror=window.__xss=17>]]>",
    "<!DOCTYPE html><img src=x onerror=window.__xss=18>",
    '<a href="&#106;avascript:window.__xss=19">x</a>',
    "[x](&#106;avascript:window.__xss=20) [y](JaVaScRiPt:window.__xss=21)",
    "[x](java\nscript:window.__xss=22) [y](data:text/html,<script>window.__xss=23</script>)",
    '<a\nhref=x\nonclick="window.__xss=24">x</a>',
    "<a\tonclick=window.__xss=25>x</a>",
    '<img src="x" alt="&quot; onerror=&quot;window.__xss=26">',
    '[x](y "t&quot; onclick=&quot;window.__xss=27")',
    "<style>*{animation:arr-pulse 1s}</style><p onanimationstart=window.__xss=28>",
    "<iframe srcdoc='<script>window.__xss=29</script>'>",
    "<form><button formaction=javascript:window.__xss=30>x",
    "<details open ontoggle=window.__xss=31>",
    "<scr<script>ipt>window.__xss=32</script>",
    "`<b>` und <b onmouseover=window.__xss=33>`code`</b>",
    "$<img src=x onerror=window.__xss=34>$",
    "| <img src=x onerror=window.__xss=35> | b |\n|---|---|\n| 1 | 2 |",
    "> <a<b> onanimationstart=window.__xss=36 x=",
    "- <a<b> onanimationstart=window.__xss=37 x=",
]  # fmt: skip


class Tags(HTMLParser):
    """Start tags inside `prose` elements (the rendered agent text)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, list[tuple[str, str]]]] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        pairs = [(k, v or "") for k, v in attrs]
        if self.depth:
            self.found.append((tag, pairs))
            if tag not in {"img", "br", "hr"}:
                self.depth += 1
        elif "prose" in (dict(attrs).get("class") or "").split():
            self.depth = 1

    def handle_endtag(self, tag):  # noqa: ANN001
        if self.depth and tag not in {"img", "br", "hr"}:
            self.depth -= 1


def smuggled(html: str, *, prose_only: bool = True) -> list[tuple[str, list[str]]]:
    parser = Tags()
    if not prose_only:
        parser.depth = 1
    parser.feed(html)
    bad = []
    for tag, pairs in parser.found:
        names = [k for k, _ in pairs]
        links = [v for k, v in pairs if k in {"href", "src", "formaction", "action"}]
        script = [u for u in links if re.sub(r"[\x00-\x20]", "", u).lower().startswith(
            ("javascript:", "vbscript:", "data:text"))]  # fmt: skip
        forbidden = {"style", "srcdoc", "formaction"} & set(names) and tag not in {"th", "td"}
        if (
            tag not in MARKDOWN_TAGS
            or any(n.startswith("on") for n in names)
            or script
            or forbidden
        ):
            bad.append((tag, names))
    return bad


# --- the reviewer's tests ------------------------------------------------------------------


@pytest.mark.parametrize("text", PAYLOADS)
@pytest.mark.parametrize("tables", [False, True])
def test_markdown_html_lets_no_element_with_a_handler_through(text: str, tables: bool) -> None:
    out = markdown_html(text, tables=tables)
    assert smuggled(out, prose_only=False) == [], out


async def test_no_handler_in_chat_transcript_result_and_task(wf: Path) -> None:
    payload = PAYLOADS[0]
    folder = make_run_folder(wf, status=RunStatus.DONE, agents=["a"], name="XSS")
    store = RoomStore.create(folder)
    store.for_agent("a").post_message("main", f"Kanal {payload}")
    store.for_agent("a").submit_result(kind="proof", title="Beweis", body=f"Ergebnis {payload}")
    (folder / "task.md").write_text(f"# Aufgabe\n\nAufgabe {payload}\n", "utf-8")
    lines = [
        transcript_line(1, "a", folder.name, "turn_start", {"turn": 1, "prompt": "Los."}),
        transcript_line(2, "a", folder.name, "text", {"text": f"Text {payload}"}),
        transcript_line(3, "a", folder.name, "thinking", {"text": f"Denken {payload}"}),
    ]
    append(folder / "agents" / "a" / "transcript.jsonl", "".join(f"{x}\n" for x in lines))
    found = {}
    async with real_user(make_api(wf)) as user:
        for query, word in [
            ("?kanal=main", "Kanal"),
            ("?agent=a", "Text"),
            ("?ansicht=ergebnisse", "Ergebnis"),
            ("?ansicht=eingaben", "Aufgabe"),
        ]:
            await user.open(f"/runs/{folder.name}{query}")
            html = await see(user, f"{word} Probe &lt;a&lt;b&gt;", retries=200)
            found[query] = smuggled(html)
            assert "<p>Kanal" in html or query != "?kanal=main"
    assert all(v == [] for v in found.values()), found


# --- own cases ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", OWN)
@pytest.mark.parametrize("tables", [False, True])
def test_no_handler_no_foreign_tag_no_script_link(text: str, tables: bool) -> None:
    out = markdown_html(text, tables=tables)
    assert smuggled(out, prose_only=False) == [], out
    assert "window.__xss" not in re.sub(r"&[a-z#0-9]+;|\"[^\"]*\"", "", _tags_only(out)), out


def _tags_only(html: str) -> str:
    return "".join(re.findall(r"<[^>]*>", html))


def test_random_texts_give_only_markdown_tags_and_no_handler() -> None:
    """A fixed-seed fuzz test over a hostile alphabet (like the reviewer's 40 000 texts)."""
    pieces = [
        "<", ">", "</", "<!", "<?", "<!--", "-->", "<a", "<b>", "<img", " src=x", " on",
        "onerror=", "onanimationstart=", '"', "'", "=", "&", "&#", "&lt;", "javascript:",
        "[", "]", "(", ")", "![", "`", "```\n", "$", "\\", "\n", "\n\n", "> ", "- ", "| ",
        "|---|", "*", "_", "x", " ", "<svg", "<math", "]]>", "<![CDATA[", "\t",
    ]  # fmt: skip
    rng = random.Random(4062)
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
        for tables in (False, True):
            out = markdown_html(text, tables=tables)
            assert smuggled(out, prose_only=False) == [], (text, out)


def test_n5_bogus_comments_do_not_swallow_text() -> None:
    """N5: `<!`, `</` + non-letter and `<?` are text; nothing up to the next `>` vanishes."""
    for text in ["vor <! weg > nach", "vor </ weg > nach", "vor <? weg > nach", "a <!--b--> c"]:
        out = markdown_html(text)
        visible = re.sub(r"<[^>]+>", "", out)
        assert "weg" in visible or "b" in visible, out
        assert "nach" in visible or " c" in visible, out
        assert "<!" not in out and "<?" not in out and "</ " not in out, out


def test_what_arr_needs_stays() -> None:
    """Formulas, code, tables of `summary.md`, lists, links, emphasis and relative images
    survive the list of tags (stop rule of W4-06.2)."""
    out = markdown_html(
        "# T\n\n**f** _k_ `c<d` $x^2$ $$y$$\n\n- a\n- b\n\n1. eins\n\n"
        '[l](https://a.example/x?y=1&z=2 "t") [m](mailto:a@b.example) [r](rel/x.md)\n\n'
        "![Plot](plots/a.png)\n\n> zitat\n\n```py\nx = 1 < 2\n```\n\n"
        "| a | b |\n|:--|--:|\n| 1 | 2 |\n",
        tables=True,
    )
    for part in (
        "<h1>T</h1>",
        "<strong>f</strong>",
        "<em>k</em>",
        "<code>c&lt;d</code>",
        '<span class="math" title="x^2" data-tex="x^2">',
        '<span class="math math-display"',
        "<li>a</li>",
        "<ol>",
        '<a href="https://a.example/x?y=1&amp;z=2" title="t">l</a>',
        '<a href="mailto:a@b.example">m</a>',
        '<a href="rel/x.md">r</a>',
        '<img src="plots/a.png" alt="Plot" />',
        "<blockquote>",
        '<pre><code class="language-py">x = 1 &lt; 2\n</code></pre>',
        '<th style="text-align:left;">a</th>',
        '<td style="text-align:right;">2</td>',
    ):
        assert part in out, (part, out)


def test_clean_html_keeps_entities_and_drops_unknown() -> None:
    assert clean_html("<p>a &amp; b &lt; c &#60; d</p>") == "<p>a &amp; b &lt; c &#60; d</p>"
    assert clean_html('<p onclick="x">t</p>') == "<p>t</p>"
    assert clean_html("<x-y>t</x-y>") == "&lt;x-y&gt;t&lt;/x-y&gt;"
    assert clean_html('<a href="javascript:x">t</a>') == "<a>t</a>"
    assert clean_html('<span class="evil" title="t">s</span>') == '<span title="t">s</span>'

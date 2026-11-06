"""Run detail (W1-07) with the NiceGUI user simulation: head, banner, progress bar, selection,
channel, input bar, transcript, results, overview, states and the live stream.

Browser behaviour (opening at „neu“, „Live folgen“, compact card lock, layout) is checked in
tests/test_gui_run_detail_browser.py. See docs/08-ux-konzept.md §6.10.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
from nicegui import ElementFilter, ui
from nicegui.element import Element
from nicegui.testing.user import User
from nicegui.testing.user_interaction import UserInteraction
from test_gui import gui_user  # same simulated user as the W1-06 GUI tests

from arr.permissions.types import ApprovalStatus
from arr.room.types import MessageKind
from arr.service.api_client import count_waiting
from arr.service.api_models import (
    AgentTranscriptEvent,
    ChannelMessageEvent,
    Message,
    Result,
    ResultKind,
    ResultStatus,
    TranscriptKind,
)
from arr.service.gui.app import STATIC_DIR, STATIC_URL
from arr.service.gui.components.approval_card import (
    COMPACT_LINE_CHARS,
    COMPACT_MAX_LINES,
    compact_lines,
)
from arr.service.gui.components.base import markdown_html
from arr.service.gui.components.stage_bar import stage_bar_html
from arr.service.gui.layout import Frame
from arr.service.gui.pages.run_detail import KATEX_URL
from arr.service.gui.pages.run_detail_channel import SENT_JS, SUGGEST_QUESTION_JS
from arr.service.gui.pages.run_detail_results import run_links_html
from arr.service.mock_api import R_EXPLORE, R_LEMMA, R_RAMSEY, R_SATZ2, Dataset, MockApi, _ev

GOLDBACH = "20261024-1230_collatz_exploration_90aa"


def _walk(element: Element) -> Iterator[Element]:
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def html(user: User, marker: str) -> str:
    """All HTML inside the elements with `marker` (content of nested `ui.html` included)."""
    with user.client:
        parts = []
        for el in ElementFilter(marker=marker):
            for node in _walk(el):
                content = getattr(node, "content", None)
                if isinstance(content, str) and content:
                    parts.append(content)
                for key, value in node._props.items():
                    if key.startswith("data-") or key in ("title", "aria-current"):
                        parts.append(f'{key}="{value}"')
        return "\n".join(parts)


async def until(check: Callable[[], bool], seconds: float = 3.0) -> None:
    """Wait until the page's timers made `check` true."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.05)


@pytest.fixture
def api() -> MockApi:
    return MockApi()


@pytest.fixture
def load_api() -> MockApi:
    return MockApi(Dataset.LOAD)


@pytest.fixture
async def scenario(api: MockApi) -> AsyncIterator[User]:
    async with gui_user(api) as user:
        yield user


# --- 1 head ---------------------------------------------------------------------------------


async def test_head_shows_status_actions_and_figures(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    await scenario.should_see(marker="run-detail")
    assert "Läuft" in html(scenario, "run-status")
    actions = html(scenario, "actions")
    assert "Pausieren" in actions and "Stoppen …" in actions
    menu = html(scenario, "menu-copy")
    assert "Für Claude Code kopieren" in menu and f"Run {R_EXPLORE} läuft noch" in menu
    meters = html(scenario, "meters")
    assert "1:12 h von 3 h" in meters
    assert "≈ 6,42 $ von 25 $" in meters
    assert "höchstens 9 von 40 je Agent" in meters
    with scenario.client:
        title = [e for e in ElementFilter(kind=ui.html) if 'id="run-title"' in e.content]
    assert title and "Collatz – Ideen-Exploration" in title[0].content


# --- 2 banner and „Fragen an dich“ (A4) ----------------------------------------------------


async def test_questions_to_you_do_not_count_as_waiting(load_api: MockApi) -> None:
    """A4 (docs/08 §8.3): the question of critic-2 is a hint of its own, not „wartet auf dich“."""
    async with gui_user(load_api) as user:
        await user.open(f"/runs/{R_SATZ2}")
        await user.should_see(marker="run-detail")
        attention = html(user, "attention")
        assert "Wartet auf dich" in attention and "prover-3" in attention
        assert "weitere" not in attention  # one inbox item in this run
        assert "? 1 Frage an dich" in attention
        assert 'data-sel="msg:kritik:96"' in attention
        assert "6 warten auf dich" in html(user, "chips")  # inbox only (§8.3)
        overview = html(user, "overview")
        assert "Wartet auf dich · 1" in overview and "Fragen an dich · 1" in overview
        critic = html(user, "agent-critic-2")
        assert "? an dich" in critic and "wartet</span>" in critic
        assert "wartet auf dich" not in critic
        # a new question to you arrives live: the hint grows, the inbox count stays
        waiting = count_waiting(load_api.state.approvals.values())
        load_api._publish(
            ChannelMessageEvent(
                seq=0,
                ts=load_api.now(),
                run_id=R_SATZ2,
                message=Message(
                    id=200,
                    ts=load_api.now(),
                    channel="kritik",
                    sender="critic-1",
                    kind=MessageKind.QUESTION,
                    body="Reicht dir eine Skizze für Fall D?",
                    to=["user"],
                    open=True,
                ),
            )
        )
        await until(lambda: "? 2 Fragen an dich" in html(user, "attention"))
        assert count_waiting(load_api.state.approvals.values()) == waiting == 6
        assert "Fragen an dich · 2" in html(user, "overview")
        assert "6 warten auf dich" in html(user, "chips")
        assert "Wartet auf dich · 1" in html(user, "overview")


async def test_banner_deep_link_goes_to_the_card(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    attention = html(scenario, "attention")
    assert "<b>explorer-1</b> möchte eine Datei von oeis.org laden" in attention
    assert "Risiko mittel" in attention and "läuft in" in attention
    assert 'href="/freigaben/apr_0042">Ansehen' in attention
    assert 'role="status"' in attention


# --- 3 progress bar --------------------------------------------------------------------------


def test_stage_bar_for_exploration_and_waterfall() -> None:
    api = MockApi(Dataset.LOAD)
    detail = asyncio.run(api.get_run(R_SATZ2))
    bar = stage_bar_html(detail.stages, detail.run.topology)
    assert "1–3 erledigt" in bar and 'class="stage done sum"' in bar
    assert "V1 läuft" in bar and 'aria-current="step"' in bar
    assert "3 Lemma B V1 5,5, V2 8,0" in bar  # single values in the tooltip
    lemma = asyncio.run(MockApi().get_run(R_LEMMA))
    bar = stage_bar_html(lemma.stages, lemma.run.topology)
    assert "V1 6,0" in bar and "att fail" in bar and "V2 8,5" in bar and "att pass" in bar
    explore = asyncio.run(MockApi().get_run(R_EXPLORE))
    bar = stage_bar_html(explore.stages, explore.run.topology)
    assert "3 Ideen · Turn 5–7 von 8" in bar and "stage done sum" not in bar


# --- 4 selection column ----------------------------------------------------------------------


async def test_agents_waiting_for_you_come_first(load_api: MockApi) -> None:
    async with gui_user(load_api) as user:
        await user.open(f"/runs/{R_SATZ2}")
        await user.should_see(marker="agents")
        with user.client:
            box = next(iter(ElementFilter(marker="agents")))
            order = [c._props.get("data-sel") for c in box.default_slot.children]
        assert order[0] == "agent:prover-3"
        assert order[1] == "agent:rater-gpt"  # blocked before working
        assert order[-1] == "agent:strategist"  # done last
        tip = html(user, "agent-prover-1")
        assert "jetzt: Bash · sage beweis_teil2.sage" in tip  # full „jetzt“ in the tooltip


async def test_channel_list_counts_new_messages(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    main = html(scenario, "channel-main")
    assert "3 neu" in main and 'aria-current="true"' in main
    assert "ab Stufe 2" in html(scenario, "channel-ranking")


# --- 5 channel ---------------------------------------------------------------------------------


async def test_channel_opens_at_the_separator(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    view = html(scenario, "run-detail")
    assert 'data-open-at="rd-new"' in view and 'data-follow="0"' in view
    sep = view.index("3 neu seit deinem letzten Blick")
    assert view.index('id="m-main-6"') < sep < view.index('id="m-main-7"')
    # message kinds of the scenario, open question, own message with delivery state
    for word in ["Behauptung", "Idee", "Einwand", "Frage", "Antwort", "Notiz"]:
        assert f"{word}</span>" in view
    assert re.search(r'class="msg open-q" id="m-main-7"', view)
    assert 'st-wait"><span aria-hidden="true">?</span> offen' in view
    assert "✓ zugestellt als Hinweis mitten im Turn · 15:12" in view
    assert "explorer-1 wartet gerade auf eine Freigabe und sieht die Frage danach" in view
    assert "angeheftet, aufklappen" in view  # profile collapsed, counted
    assert 'data-tex="' in view  # formulas carry their LaTeX source


async def test_load_channel_counts_older_messages(load_api: MockApi) -> None:
    async with gui_user(load_api) as user:
        await user.open(f"/runs/{R_SATZ2}")
        view = html(user, "run-detail")
        assert "12 neu seit deinem letzten Blick" in view
        older = int(re.search(r"▲</span> ([\d  ]+) ältere", view).group(1).replace(" ", ""))
        assert older + view.count('class="msg') >= 9_870
        assert "⤼</span> prover-2 passt" in view


async def test_send_message_to_one_agent_shows_delivery(
    api: MockApi, scenario: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts: list[str] = []
    original = Frame.js

    def record(frame: Frame, code: str) -> None:
        scripts.append(code)
        original(frame, code)

    monkeypatch.setattr(Frame, "js", record)
    await scenario.open(f"/runs/{R_EXPLORE}")
    composer = html(scenario, "run-detail")
    assert '<option value="explorer-2">explorer-2</option>' in composer
    assert "Notiz" in composer and "Einwand" in composer
    scenario.find(marker="send").trigger(
        "click", {"to": "explorer-2", "kind": "question", "body": "Gilt das auch bis $10^9$?"}
    )
    await until(lambda: "Gilt das auch bis" in html(scenario, "run-detail"))
    view = html(scenario, "run-detail")
    assert "✓ zugestellt als Hinweis mitten im Turn" in view  # explorer-2 works
    msg = api.state.runs[R_EXPLORE].channels["main"].extra[-1]
    assert msg.to == ["explorer-2"] and msg.kind is MessageKind.QUESTION
    assert SENT_JS in scripts, "the composer is emptied and its hint updated (W3-08.1 N3)"


# --- 6 transcript ------------------------------------------------------------------------------


async def test_transcript_of_an_agent(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}?agent=explorer-1")
    view = html(scenario, "run-detail")
    assert '<b class="mono">explorer-1</b> · Transkript · Claude Opus 5.5' in view
    assert "Turn 6 · 15:10 · Stufe Exploration" in view
    assert "Auftrag</span> · „Stufe Exploration, Turn 6 von 8." in view
    assert "Thinking (Zusammenfassung)" in view and '<details class="ev thinking"' in view
    assert "✓ 38 s · 3 Zeilen" in view
    assert "Hinweis an explorer-1 (mitten im Turn)" in view
    assert "wartet auf deine Freigabe" in view
    assert html(scenario, "tc-card-apr_0042")  # waiting tool call carries the compact card
    assert "▲</span> 15 ältere Ereignisse" in view
    assert re.search(r"Turn 1–5 · 14:\d\d–15:\d\d · 193 Ereignisse", view)  # collapsed, counted
    assert 'aria-current="true"' in html(scenario, "agent-explorer-1")


async def test_transcript_shows_the_last_200_and_follows_live(load_api: MockApi) -> None:
    async with gui_user(load_api) as user:
        await user.open(f"/runs/{R_SATZ2}?agent=prover-1")
        view = html(user, "run-detail")
        assert "49\u202f800 ältere Ereignisse" in view
        assert 'data-follow="1"' in view
        rows = lambda: html(user, "run-detail").count('id="seq-')  # noqa: E731
        assert rows() == 200
        # 60 new entries of prover-1 arrive live: the oldest ones leave the page
        for seq in range(50_000, 50_060):
            event = _ev(seq, load_api.now(), 31, TranscriptKind.TEXT, text=f"Schritt {seq}")
            load_api._publish(
                AgentTranscriptEvent(
                    seq=0, ts=load_api.now(), run_id=R_SATZ2, agent_id="prover-1", event=event
                )
            )
        await until(lambda: "Schritt 50059" in html(user, "run-detail"))
        assert rows() == 200
        assert "49\u202f860 ältere Ereignisse" in html(user, "run-detail")


async def test_jump_to_a_transcript_entry(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_LEMMA}?agent=prover&seq=301")
        view = html(user, "run-detail")
        assert 'data-open-at="seq-301"' in view and 'data-follow="0"' in view
        assert 'id="seq-301"' in view


# --- 7 results ---------------------------------------------------------------------------------


async def test_finished_run_opens_results(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_LEMMA}")
        assert "Fertig" in html(user, "run-status")
        assert "Für Claude Code kopieren" in html(user, "handoff")
        view = html(user, "run-detail")
        assert 'class="active" href="?ansicht=ergebnisse"' in view
        assert "Zusammenfassung</h2>" in view and "Übernehmen</h2>" in view
        assert f"Run {R_LEMMA} ist fertig" in view
        assert "004 · Beweis, Versuch 1" in view and "bestanden · Ø 8,0" in view
        assert "002 · Skizze, Versuch 1" in view and "Nacharbeit · Ø 6,0" in view
        assert "(bestanden ab 7)" in view and "rater-2</b> (7,8)" in view
        assert 'data-sel="seq:prover:301"' in view and "Im Transkript zeigen" in view
        assert "Der Run ist beendet" not in view  # no input bar in the results view
        assert "Pausieren" not in html(user, "actions")


async def test_results_link_jumps_to_a_result(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_LEMMA}?ansicht=ergebnisse&ergebnis=4")
        assert 'data-open-at="e004"' in html(user, "run-detail")


def _result(result_id: int, path: str | None) -> Result:
    return Result(
        id=result_id,
        ts=1.7e9,
        agent="diskutant-1",
        kind=ResultKind.OTHER,
        title="Ergebnis",
        path=path,
        status=ResultStatus.UNRATED,
    )


def test_w3_08_u4_relative_links_of_a_run_file_open_the_result_or_stay_text() -> None:
    """U4: `results/001-….md` in `output/summary.md` opens result 1 (stored there, or only
    copied there by the finalizer: by its number); other relative links and images are text
    with their path, external links and anchors stay."""
    results = [_result(1, "output/results/001-diskutant-2-result.md"), _result(2, None)]
    source = (
        "Siehe [Ergebnis 1](results/001-diskutant-2-result.md) und "
        "[Ergebnis 2](results/002-diskutant-1-summary.md#schluss).\n\n"
        "[Aufgabe](../task.md), [Skript](results/collatz.py), ![Graph](plots/g.png), "
        "[OEIS](https://oeis.org/A006577?a=1&b=2), [oben](#ergebnis), [Start](/runs)"
    )
    out = run_links_html(markdown_html(source), "output/summary.md", results)
    assert '<a href="?ansicht=ergebnisse&amp;ergebnis=1" data-sel="ergebnis:1"' in out
    assert '<a href="?ansicht=ergebnisse&amp;ergebnis=2" data-sel="ergebnis:2"' in out
    assert '<span class="run-file" title="Im Run-Ordner: task.md">Aufgabe</span>' in out
    assert 'title="Im Run-Ordner: output/results/collatz.py">Skript</span>' in out
    assert 'title="Bild im Run-Ordner: output/plots/g.png">Graph</span>' in out
    assert '<a href="https://oeis.org/A006577?a=1&b=2">OEIS</a>' in out  # unchanged
    assert '<a href="#ergebnis">oben</a>' in out and '<a href="/runs">Start</a>' in out
    assert 'href="results/' not in out and "<img" not in out and 'href="../' not in out
    # a result's own file links to its neighbour: relative to `output/results/`
    own = run_links_html(markdown_html("[davor](002-x.md)"), results[0].path, results)
    assert 'data-sel="ergebnis:2"' in own
    # W3-08.1: the number counts only in `output/results/`, not in any folder named `results`
    work = run_links_html(
        markdown_html("[Notiz](results/002-notiz.md)"), "agents/x/work/proof.md", results
    )
    assert "data-sel" not in work
    assert 'title="Im Run-Ordner: agents/x/work/results/002-notiz.md">Notiz</span>' in work


async def test_w3_08_u4_summary_link_opens_the_result_in_the_run(api: MockApi) -> None:
    """U4 in the page: the link in „Zusammenfassung“ selects the result (no 404 under /runs/)."""
    api._run(R_LEMMA).summary_md = "Der Beweis steht in [Ergebnis 4](results/004-prover-proof.md)."
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_LEMMA}")
        view = html(user, "run-detail")
        assert 'href="?ansicht=ergebnisse&amp;ergebnis=4" data-sel="ergebnis:4"' in view
        assert 'href="results/' not in view and 'data-open-at=""' in view
        user.find(marker="run-detail").trigger("click", "ergebnis:4")  # the link's data-sel
        await until(lambda: 'data-open-at="e004"' in html(user, "run-detail"))


# --- 8 overview ----------------------------------------------------------------------------------


async def test_overview_lists_waiting_questions_results(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    card = html(scenario, "cc-card-apr_0042")
    assert "Begründung:" in card and "Warum bei dir:" in card
    assert "mittel" in card and "läuft in" in card and "Erlauben (einmalig)" in card
    overview = html(scenario, "overview")
    assert "Offene Fragen · 1" in overview and "critic</span> → <span" in overview
    assert "Ergebnisse · 0" in overview and "Jeder Explorer gibt nach 8 Turns" in overview
    assert "2 arbeiten · 1 wartet auf dich · 2 warten" in overview


async def test_run_start_and_escalation_are_never_compact_decisions(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_RAMSEY}")
        card = html(user, "cc-card-apr_0040")
        assert "Entscheiden" in card and "Erlauben" not in card
        await user.open("/runs/20261024-1521_collatz_proof-review_e81b")
        card = html(user, "cc-card-apr_0041")
        assert "Ansehen" in card and "Freigeben" not in card and "Abweichungen" in card


async def test_compact_card_allows_once_and_shows_a_confirmation(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_EXPLORE}")
        with user.client:
            card = next(iter(ElementFilter(marker="cc-card-apr_0042")))
            allow = next(e for e in _walk(card) if e.tag == "button" and "data-grant" in e._props)
        UserInteraction(user, {allow}, None).trigger("click")
        await until(lambda: api.state.approvals["apr_0042"].status is ApprovalStatus.DECIDED)
        await until(lambda: "Erlaubt" in html(user, "overview"))  # confirmation, same card
        await until(lambda: "Wartet auf dich · 0" in html(user, "overview"), seconds=4)
        assert "2 warten auf dich" in html(user, "chips")


async def test_w3_08_u6_composer_says_what_a_note_and_a_question_do(scenario: User) -> None:
    """U6: the hint stands under the input (linked by aria-describedby) next to the delivery
    note; `@name` and a single addressee suggest „Frage“ in the browser (logic in Node:
    tests/test_gui_static_js.py)."""
    await scenario.open(f"/runs/{R_EXPLORE}?kanal=main")
    await until(lambda: "cmp-body" in html(scenario, "run-detail"))
    view = html(scenario, "run-detail")
    assert (
        '<span class="small muted cmp-hint" id="cmp-hint">Notiz weckt niemanden. Für eine '
        "Antwort: Frage wählen oder @name schreiben."
    ) in view
    assert 'aria-describedby="cmp-hint"' in view and 'class="cmp-suggest"' in view
    with scenario.client:
        [form] = [e for e in _walk(scenario.client.layout) if e.tag == "form"]
        handlers = {(lis.type, lis.js_handler) for lis in form._event_listeners.values()}
    assert ("input", SUGGEST_QUESTION_JS) in handlers
    assert ("change", SUGGEST_QUESTION_JS) in handlers
    assert "arr.rd.sent();" in SENT_JS and "new Event('input', {bubbles: true})" in SENT_JS
    scenario.find(marker="pause").click()
    await until(lambda: "Zustellung beim Fortsetzen" in html(scenario, "run-detail"))
    assert "@name schreiben. · Zustellung beim Fortsetzen" in html(scenario, "run-detail")


# --- 9 states ------------------------------------------------------------------------------------


async def test_pause_resume_and_stop_with_dialog(api: MockApi, scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    scenario.find(marker="pause").click()
    await until(lambda: "Pausiert" in html(scenario, "run-status"))
    note = html(scenario, "attention")
    assert "Pausiert um" in note and "2 laufende Turns enden noch" in note
    assert "Fortsetzen" in html(scenario, "actions")
    await until(lambda: "Zustellung beim Fortsetzen" in html(scenario, "run-detail"))
    scenario.find(marker="resume").click()
    await until(lambda: "Läuft" in html(scenario, "run-status"))
    scenario.find(marker="stop").click()
    await scenario.should_see("Run „Collatz – Ideen-Exploration“ stoppen?")
    await scenario.should_see("Stattdessen pausieren")
    scenario.find(marker="dialog-confirm").click()
    await until(lambda: "Gestoppt" in html(scenario, "run-status"))
    assert api.state.runs[R_EXPLORE].status.value == "stopped"


async def test_escalation_state_has_no_resume(scenario: User) -> None:
    await scenario.open(f"/runs/{R_RAMSEY}")
    assert "Wartet auf dich" in html(scenario, "run-status")
    actions = html(scenario, "actions")
    assert "Fortsetzen" not in actions and "Stoppen …" in actions
    attention = html(scenario, "attention")
    assert "Run pausiert, weiter nur mit deiner Entscheidung" in attention
    assert 'href="/freigaben/apr_0040">Ansehen' in attention
    view = html(scenario, "run-detail")
    assert "Zustellung, sobald du die Eskalation entschieden hast" in view  # no „Fortsetzen“
    assert "Zustellung beim Fortsetzen" not in view


# --- selection, ui_seen, live ------------------------------------------------------------------


async def test_selection_changes_the_view_without_reload(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    root = scenario.find(marker="run-detail")
    root.trigger("click", "kanal:ranking")
    await until(lambda: "#ranking öffnet in Stufe „Bewertung“" in html(scenario, "run-detail"))
    root.trigger("click", "agent:explorer-2")
    await until(lambda: "explorer-2</b> · Transkript" in html(scenario, "run-detail"))
    root.trigger("click", "msg:main:7")
    await until(lambda: 'data-open-at="m-main-7"' in html(scenario, "run-detail"))
    root.trigger("click", "ansicht:ergebnisse")
    await until(lambda: "Noch keine Ergebnisse" in html(scenario, "run-detail"))


async def test_seen_state_from_the_browser_moves_the_separator(scenario: User) -> None:
    await scenario.open(f"/runs/{R_EXPLORE}")
    scenario.find(marker="run-detail").trigger(
        "arrseen", {"channels": {"main": 8}, "at": {"main": 1_700_000_000}}
    )
    await until(lambda: "1 neu seit deinem letzten Blick" in html(scenario, "run-detail"))
    assert "1 neu" in html(scenario, "channel-main")


async def test_live_messages_and_cards_update(api: MockApi, scenario: User) -> None:
    api.stream_rate = 30
    await scenario.open(f"/runs/{R_EXPLORE}")
    before = html(scenario, "run-detail").count('class="msg')
    cards = html(scenario, "agents")
    for _ in range(20):
        api.advance(30.0)
        await asyncio.sleep(0.12)
    await until(lambda: html(scenario, "run-detail").count('class="msg') > before)
    assert "1:12 h von 3 h" not in html(scenario, "meters")  # time moved on
    assert html(scenario, "agents") != cards  # „jetzt“ lines follow the agents


# --- formulas (G7) -------------------------------------------------------------------------------


def test_formulas_carry_tex_and_katex_is_available() -> None:
    out = markdown_html(r"$σ(n) = 4 \iff n ≡ 3 \pmod{16}$ und $$T^4(n) < n$$")
    assert 'data-tex="σ(n) = 4 \\iff n ≡ 3 \\pmod{16}"' in out
    assert 'class="math math-display"' in out

    # a file of this package (W1-07 review), no longer out of NiceGUI's Mermaid bundle
    assert f"{STATIC_URL}/katex.mjs" == KATEX_URL
    module = STATIC_DIR / "katex.mjs"
    assert module.stat().st_size > 100_000 and "katex as default" in module.read_text("utf-8")
    assert "MIT License" in (STATIC_DIR / "katex.LICENSE.txt").read_text("utf-8")


async def test_katex_file_is_served_as_a_module(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_EXPLORE}")
        response = await user.http_client.get(KATEX_URL)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


async def test_generic_run_agents_follow_open_approvals(load_api: MockApi) -> None:
    async with gui_user(load_api) as user:
        await user.open(f"/runs/{GOLDBACH}")
        assert "wartet auf dich" in html(user, "agent-explorer-1")
        assert "wartet auf dich" in html(user, "agent-explorer-gpt")
        overview = html(user, "overview")
        assert "Wartet auf dich · 2" in overview


# --- W1-07.1 ---------------------------------------------------------------------------------


def test_compact_lines_counts_wrapped_lines() -> None:
    assert compact_lines("curl -sO https://oeis.org/A006577/b006577.txt") == 2
    assert compact_lines("a\nb") == 2
    assert (
        compact_lines("x" * (COMPACT_LINE_CHARS * COMPACT_MAX_LINES + 1)) == COMPACT_MAX_LINES + 1
    )


async def test_very_long_command_is_not_decided_in_the_compact_card(api: MockApi) -> None:
    """Point 1: more than 12 lines in the 300 px column → only „Details“ (Freigaben page)."""
    long = "python check.py " + " ".join(f"--fall {i}" for i in range(80))
    item = api.state.approvals["apr_0042"]
    api.state.approvals["apr_0042"] = item.model_copy(update={"command": long})
    async with gui_user(api) as user:
        await user.open(f"/runs/{R_EXPLORE}")
        card = html(user, "cc-card-apr_0042")
        assert long in card  # shown whole, nothing cut off
        assert "Zu lang für die kompakte Karte" in card and 'href="/freigaben/apr_0042"' in card
        with user.client:
            element = next(iter(ElementFilter(marker="cc-card-apr_0042")))
            grants = [e for e in _walk(element) if "data-grant" in e._props]
        assert not grants  # no „Erlauben“ here
        assert "Warum bei dir:</b> Netzwerk ist nur zu" in card  # not shortened (H1)


async def test_run_waiting_for_its_start_has_no_input_and_no_stop(api: MockApi) -> None:
    """Point 10: before the start only the run-start card decides (08 §6.7)."""
    async with gui_user(api) as user:
        await user.open("/runs/20261024-1521_collatz_proof-review_e81b")
        actions = html(user, "actions")
        assert "Run-Start ansehen" in actions and "/freigaben/apr_0041" in actions
        assert "Stoppen" not in actions
        view = html(user, "run-detail")
        assert "Nachrichten gehen erst, wenn der Run läuft." in view
        assert 'id="cmp-body"' not in view


async def test_paused_turns_end_and_the_note_follows(api: MockApi, scenario: User) -> None:
    """Point 9: „2 laufende Turns enden noch“ disappears once they have ended."""
    await scenario.open(f"/runs/{R_EXPLORE}")
    scenario.find(marker="pause").click()
    await until(lambda: "2 laufende Turns enden noch" in html(scenario, "attention"))
    api.advance(10)
    await until(lambda: "laufende Turn" not in html(scenario, "attention"))
    assert "Pausiert um" in html(scenario, "attention")
    assert "Run pausiert, Turn beendet" in html(scenario, "agent-explorer-2")
    scenario.find(marker="resume").click()
    await until(lambda: "arbeitet weiter" in html(scenario, "agent-explorer-2"))


async def test_status_change_keeps_the_channel_list(scenario: User) -> None:
    """Point 7: pausing redraws hints and input bar, not the list (separator stays)."""
    await scenario.open(f"/runs/{R_EXPLORE}")
    with scenario.client:
        before = {e.id for e in ElementFilter(marker="run-detail") for e in _walk(e)}
    scenario.find(marker="pause").click()
    await until(lambda: "Zustellung beim Fortsetzen" in html(scenario, "run-detail"))
    view = html(scenario, "run-detail")
    assert 'id="rd-new"' in view and 'data-open-at="rd-new"' in view
    with scenario.client:
        after = {e.id for e in ElementFilter(marker="run-detail") for e in _walk(e)}
    rows = {e for e in before if e in after}
    assert len(rows) > len(before) * 0.8  # list elements kept, only the input bar is new


async def test_waiting_agents_of_generic_runs_have_their_tool_call(load_api: MockApi) -> None:
    """Point 9: the agent waiting for you shows the waiting tool call in its transcript."""
    async with gui_user(load_api) as user:
        await user.open(f"/runs/{GOLDBACH}?agent=explorer-gpt")
        view = html(user, "run-detail")
        assert "wartet auf deine Freigabe" in view
        assert "Noch keine Ereignisse" not in view
        assert html(user, "tc-card-apr_1203")


def test_underscores_inside_a_word_are_no_emphasis() -> None:
    """Review W3-02 G8: run IDs in headings keep their underscores."""
    out = markdown_html("# Run 20261030-2128_demo_runde_629f\n\n_kursiv_ und snake_case_name")
    assert "20261030-2128_demo_runde_629f" in out and "snake_case_name" in out
    assert "<em>kursiv</em>" in out

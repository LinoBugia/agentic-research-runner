"""GUI with the NiceGUI user simulation: navigation, counters, approval decisions.

Browser behaviour (focus, keys) is checked in tests/test_gui_focus.py. See
docs/08-ux-konzept.md §4, §5.2, §5.3, §8.3 (W1-06 A3, A5).
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from nicegui import Client, ElementFilter, core, ui
from nicegui.functions.download import download
from nicegui.functions.navigate import Navigate
from nicegui.functions.notify import notify
from nicegui.testing.general import nicegui_reset_globals, prepare_simulation
from nicegui.testing.user import User
from nicegui.testing.user_interaction import UserInteraction

from arr.errors import ArrError
from arr.permissions.types import ApprovalStatus
from arr.service.gui import format as fmt
from arr.service.gui.app import setup
from arr.service.gui.layout import chips_html, nav_html, tab_title
from arr.service.mock_api import Dataset, MockApi


@asynccontextmanager
async def gui_user(api: MockApi) -> AsyncIterator[User]:
    """A simulated user on the arr GUI backed by `api` (like nicegui's `user_simulation`)."""
    with nicegui_reset_globals():
        os.environ["NICEGUI_USER_SIMULATION"] = "true"
        try:
            prepare_simulation()
            setup(api)
            ui.run(storage_secret="test")
            async with core.app.router.lifespan_context(core.app):
                transport = httpx.ASGITransport(core.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    yield User(client)
        finally:
            os.environ.pop("NICEGUI_USER_SIMULATION", None)
            ui.navigate = Navigate()
            ui.notify = notify
            ui.download = download
            # Our page functions live in arr.*; keep NiceGUI from unloading those modules.
            Client.page_routes.clear()


def _contents(user: User, marker: str) -> str:
    with user.client:
        return " ".join(
            str(c)
            for el in ElementFilter(marker=marker)
            for c in [getattr(el, "content", None) or el.props.get("innerHTML")]
            if c
        )


def _within(user: User, card: str, marker: str) -> UserInteraction:
    with user.client:
        elements = set(ElementFilter(marker=marker).within(marker=card))
    assert elements, f"{marker} not found in {card}"
    return UserInteraction(user, elements, None)


def _counters_in_page(user: User) -> tuple[str, str]:
    return _contents(user, "chips"), _contents(user, "nav")


@pytest.fixture
def api() -> MockApi:
    return MockApi()


async def test_navigation_reaches_every_page(api: MockApi) -> None:
    async with gui_user(api) as user:
        for path in [
            "/",
            "/freigaben",
            "/freigaben?ansicht=verlauf",
            "/runs",
            "/runs?status=fertig",
            "/runs/20261024-1412_collatz_exploration_a3f9",
            "/projekte",
            "/skills",
            "/profile",
            "/usage",
            "/speicher",
            "/einstellungen",
            "/neuer-run",
        ]:
            await user.open(path)
            await user.should_see(marker="nav")
        nav = _contents(user, "nav")
        for label in [
            "Start",
            "Freigaben",
            "Runs",
            "Projekte",
            "Skills",
            "Auth-Profile",
            "Usage",
            "Speicher",
            "Einstellungen",
        ]:
            assert f">{label}" in nav


async def test_counters_are_the_same_everywhere(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/runs")
        chips, nav = _counters_in_page(user)
        assert "3 aktiv" in chips and "3 warten auf dich" in chips
        assert 'data-badge="waiting">3<' in nav
        page = _contents(user, "filters")
        assert 'Aktiv <span class="n">3</span>' in page
        assert 'Wartet auf dich <span class="n">3</span>' in page
    assert tab_title("Freigaben", 3) == "(3) Freigaben – arr"
    assert tab_title("Freigaben", 0) == "Freigaben – arr"


async def test_allow_permission_updates_counters_and_history(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/freigaben/apr_0042")
        await user.should_see(marker="card-apr_0042")
        _within(user, "card-apr_0042", "comment").trigger("input", "Bitte nur diese Datei")
        _within(user, "card-apr_0042", "allow").click()
        await user.should_see(marker="decided-apr_0042")
        await user.should_not_see(marker="card-apr_0042", retries=60)
        item = await api.get_approval("apr_0042")
        assert item.status is ApprovalStatus.DECIDED
        assert item.decision.comment == "Bitte nur diese Datei"
        await user.should_see(marker="history-apr_0042")
        # the frame pumps live events every 0.25 s
        for _ in range(20):
            chips, nav = _counters_in_page(user)
            if "2 warten auf dich" in chips:
                break
            await asyncio.sleep(0.05)
        assert "2 warten auf dich" in chips
        assert 'data-badge="waiting">2<' in nav
        assert "3 aktiv" in chips


async def test_scope_always_asks_for_confirmation(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/freigaben")
        _within(user, "card-apr_0042", "scope-always").trigger("change")
        _within(user, "card-apr_0042", "allow").click()
        await user.should_see(marker="card-apr_0042")
        assert (await api.get_approval("apr_0042")).status is ApprovalStatus.OPEN
        await user.should_see(content="oeis.org immer erlauben?")
        user.find(marker="dialog-confirm").click()
        await user.should_see(marker="decided-apr_0042")
        await user.should_not_see(marker="card-apr_0042", retries=60)
        item = await api.get_approval("apr_0042")
        assert item.decision.scope.value == "always"


async def test_escalation_needs_an_option(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/freigaben/apr_0040")
        _within(user, "card-apr_0040", "decide").click()
        await user.should_see(marker="card-apr_0040")
        assert (await api.get_approval("apr_0040")).status is ApprovalStatus.OPEN
        _within(user, "card-apr_0040", "option-retry").trigger("change")
        _within(user, "card-apr_0040", "decide").click()
        await user.should_see(marker="decided-apr_0040", retries=20)
        await user.should_not_see(marker="card-apr_0040", retries=60)
        assert (await api.get_approval("apr_0040")).decision.option_key.value == "retry"


async def test_run_start_reject_uses_dialog_with_message(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/freigaben/apr_0041")
        _within(user, "card-apr_0041", "deny").click()
        await user.should_see(content="Run-Start ablehnen?")
        assert (await api.get_approval("apr_0041")).status is ApprovalStatus.OPEN
        user.find(marker="dialog-input").trigger("input", "Bitte ohne Web")
        user.find(marker="dialog-confirm").click()
        await user.should_see(marker="decided-apr_0041")
        await user.should_not_see(marker="card-apr_0041", retries=60)
        item = await api.get_approval("apr_0041")
        assert item.decision.action.value == "deny"
        assert item.decision.comment == "Bitte ohne Web"


async def test_revoke_from_history(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/freigaben")
        user.find(marker="revoke-apr_0039").click()
        await user.should_see(content="Entscheidung zurückziehen?")
        user.find(marker="dialog-confirm").click()
        await user.should_not_see(marker="revoke-apr_0039")
        assert (await api.get_approval("apr_0039")).status is ApprovalStatus.REVOKED


async def test_run_start_card_shows_deviations_and_vendors(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/freigaben")
        with user.client:
            card = next(iter(ElementFilter(marker="card-apr_0041")))
            html = " ".join(
                str(el.content) for el in ElementFilter(kind=ui.html).within(instance=card)
            )
        assert "Abweichungen von deinen Standards (3)" in html
        assert "Inhalte gehen an <b>Anthropic</b> (author, judge-1, judge-3)" in html
        assert "<b>OpenAI</b> (judge-2)" in html
        assert "Wie Standard: Arbeitsordner (Kopie von collatz" in html
        assert "keine Frist" in html


async def test_runs_table_and_filters(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/runs")
        table = _contents(user, "runs-table")
        assert table.index("Aktiv · 3") < table.index("Beendet")
        assert "/runs/20261024-1412_collatz_exploration_a3f9" in table
        assert "≈ 6,4" in table
        assert "/freigaben/apr_0041" in table  # status badge of the waiting run start
        await user.open("/runs?status=fehler")
        table = _contents(user, "runs-table")
        assert "Fehlgeschlagen" in table and "Abgebrochen" in table and "Läuft" not in table
        await user.open("/runs?status=fehler&projekt=ramsey-schranken")
        assert "Filter zurücksetzen" in _contents(user, "runs-table")


async def test_load_data_set_pages_runs() -> None:
    async with gui_user(MockApi(Dataset.LOAD)) as user:
        await user.open("/runs")
        table = _contents(user, "runs-table")
        assert table.count('data-href="/runs/') == 50
        await user.open("/freigaben")
        chips, nav = _counters_in_page(user)
        assert "6 warten auf dich" in chips and "5 aktiv" in chips


def test_format_rules_of_docs_08_8_7() -> None:
    assert fmt.money(6.42, None) == "6,42 $"
    assert fmt.size(38_000_000) == "38 MB"
    assert fmt.size(46_200_000_000) == "46,2 GB"
    assert fmt.tokens(412_000) == "412 k"
    assert fmt.tokens(1_900_000) == "1,9 M"
    assert fmt.duration_hm(112 * 60) == "1:52 h"
    assert fmt.duration(100) == "1 min 40 s"
    now = 1_000_000.0
    assert fmt.deadline(now + 14 * 60, now).startswith("läuft in 14 min ab")
    assert fmt.deadline(now + 95, now).startswith("läuft in 1 min 35 s ab")
    assert fmt.threshold(7.0) == "7"
    assert chips_html(type("C", (), {"active": 0, "waiting": 0, "warnings": 0})()) == ""  # type: ignore[arg-type]
    assert "count" not in nav_html("runs", 0)


def test_run_mock_opens_the_browser_on_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    import arr.service.gui as gui
    from arr.service.gui import app as gui_app

    calls: dict[str, object] = {}
    monkeypatch.setattr(gui_app, "setup", lambda api: calls.setdefault("api", api))
    monkeypatch.setattr(ui, "run", lambda **kw: calls.setdefault("run", kw))
    monkeypatch.setattr("nicegui.app.on_startup", lambda fn: calls.setdefault("startup", fn))
    gui.run(mock=True, port=None, open_browser=True)
    run_kwargs = calls["run"]
    assert isinstance(calls["api"], MockApi)
    assert run_kwargs["host"] == "127.0.0.1" and run_kwargs["port"] == 8765
    assert run_kwargs["show"] is True and run_kwargs["reload"] is False
    assert calls["api"].base_url == "http://127.0.0.1:8765"
    with pytest.raises(ArrError, match="arr serve"):
        gui.run(mock=False)


# --- W1-06.1 ------------------------------------------------------------------------------

SCENARIO_ONLY = ["oeis", "R(3,5)", "on_fail", "Skizze", "Mathe-Literatur", "Lemma 3"]


def _card_html(user: User, approval_id: str) -> str:
    with user.client:
        card = next(iter(ElementFilter(marker=f"card-{approval_id}")))
        parts = [str(el.content) for el in ElementFilter(kind=ui.html).within(instance=card)]
        parts += [
            str(el.props.get("placeholder", ""))
            for el in ElementFilter(marker="comment").within(instance=card)
        ]
    return " ".join(parts)


async def test_load_cards_show_no_scenario_text() -> None:
    """Point 3 (M2): cards are built from their data; no wireframe text in the load set."""
    api = MockApi(Dataset.LOAD)
    async with gui_user(api) as user:
        await user.open("/freigaben")
        for item in await api.list_approvals(status=[ApprovalStatus.OPEN]):
            html = _card_html(user, item.id)
            leaked = [text for text in SCENARIO_ONLY if text in html]
            assert leaked == [], (item.id, leaked)
        escalation = _card_html(user, "apr_1206")
        assert "Wasserfall · Stufe Konstruktion" in escalation
        assert "Kommentar an prover (optional)" in escalation
        assert "Inhalt geht an</dt><dd><b>OpenAI</b>" in _card_html(user, "apr_1203")


async def test_texts_use_dir_not_du(api: MockApi) -> None:
    """Point 6: dative forms in running text."""
    async with gui_user(api) as user:
        await user.open("/freigaben/apr_0037")
        await user.should_see(content="von dir um")
        await user.open("/runs/20261024-1412_collatz_exploration_a3f9")
        await user.should_see(content="von dir (GUI)")
        with user.client:
            texts = " ".join(str(el.content) for el in ElementFilter(kind=ui.html))
        assert "von du" not in texts


async def test_warnings_chip_leads_to_the_warnings(api: MockApi) -> None:
    """Point 7: „⚠ 2 Warnungen“ → /#warnungen shows both warnings with their action."""
    async with gui_user(api) as user:
        await user.open("/runs")
        assert 'href="/#warnungen"' in _contents(user, "chips")
        await user.open("/")
        await user.should_see(content='id="h-warn"')
        await user.should_see(content="Speicher knapp")
        await user.should_see(content="Speicher ansehen")
        await user.should_see(content="chatgpt-privat bei 82 % des Nutzungslimits")


async def test_load_more_appends_without_reload() -> None:
    """Point 9: „Weitere 50 laden“ is a button that appends in place."""
    async with gui_user(MockApi(Dataset.LOAD)) as user:
        await user.open("/runs")
        assert _contents(user, "runs-table").count('data-href="/runs/') == 50
        user.find(marker="more").click()
        for _ in range(20):
            if _contents(user, "runs-table").count('data-href="/runs/') == 100:
                break
            await asyncio.sleep(0.05)
        assert _contents(user, "runs-table").count('data-href="/runs/') == 100


def test_escalation_option_is_not_shown_as_done() -> None:
    """Point 11: a chosen way forward is neutral, never the colour of „fertig“ (§7.1)."""
    import asyncio as aio

    from arr.permissions.types import DecisionAction, EscalationOption
    from arr.service.api_models import DecisionRequest
    from arr.service.gui.texts import outcome

    api = MockApi()
    request = DecisionRequest(action=DecisionAction.OPTION, option_key=EscalationOption.RETRY)
    item = aio.run(api.decide_approval("apr_0040", request)).item
    status = outcome(item)
    assert status.word == "Noch ein Versuch" and status.css == "st-neutral"
    assert status.icon == "replay"

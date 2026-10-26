"""Pages of W1-08 in a real browser: deep links of docs/08 §4.3 (A3), layout at 1440 × 760 and
1280 × 680 in light and dark without cutting (A6), Start without scrolling (T1), keyboard.

Like tests/test_gui_focus.py: `arr gui --mock` in a subprocess, the installed Google Chrome
through Playwright. Start with:

    uv run --with playwright python -m pytest -m macos tests/test_gui_screens_browser.py
"""

import sys
from collections.abc import Iterator

import pytest

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="nur macOS (Google Chrome)"),
]
RUN_HINT = "uv run --with playwright python -m pytest -m macos tests/test_gui_screens_browser.py"
try:
    from playwright import sync_api
except ImportError:
    pytest.skip(
        f"Playwright fehlt in dieser Umgebung. Browser-Tests starten mit: {RUN_HINT}",
        allow_module_level=True,
    )

from test_gui_focus import _server  # noqa: E402 - the same mock server

SIZES = [(1440, 760), (1280, 680)]
EXPLORE = "/runs/20261024-1412_collatz_exploration_a3f9"
LEMMA = "/runs/20261024-0930_collatz_proof_7c21"

PAGES = [
    "/",
    "/neuer-run?skill=exploration",
    "/neuer-run?von=apr_0041",
    "/projekte",
    "/projekte/primzahl-luecken",
    "/skills/proof-review",
    "/skills/exploration?tab=yaml",
    "/profile",
    "/usage",
    "/usage?nach=run",
    "/speicher",
    "/einstellungen",
    "/einrichtung",
    "/einrichtung?schritt=2",
    "/einrichtung?schritt=5",
]
STATES = [f"{p}?zustand={s}" for p in ("/", "/projekte", "/profile", "/usage", "/speicher")
          for s in ("leer", "laedt", "fehler")]  # fmt: skip

# JavaScript: elements that stick out of the content area or of their card (A6 „ohne
# Abschneiden“); scrolling boxes (tables in .file-view, pre) are allowed to scroll inside.
OVERFLOW_JS = """() => {
  const doc = document.documentElement;
  const out = [];
  if (doc.scrollWidth > doc.clientWidth + 1) out.push('page scrolls sideways');
  const main = document.querySelector('#inhalt') || document.body;
  const right = main.getBoundingClientRect().right + 1;
  const scrolls = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      if (/(auto|scroll)/.test(getComputedStyle(p).overflowX)) return true;
    }
    return false;
  };
  for (const el of main.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (!r.width || getComputedStyle(el).visibility === 'hidden') continue;
    if (el.closest('dialog:not([open]), .toasts, .sr-only')) continue;
    if (r.right > right && !scrolls(el)) {
      out.push(el.tagName + '.' + el.className + ' ' + Math.round(r.right));
    }
    const card = el.parentElement && el.parentElement.closest('.card');
    if (card && !scrolls(el)) {
      const c = card.getBoundingClientRect();
      if (r.right > c.right + 1) out.push('in card: ' + el.tagName + '.' + el.className);
    }
    if (out.length > 5) break;
  }
  return out;
}"""


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        yield b
        b.close()


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _server(tmp_path_factory)


def _page(browser, size=(1440, 760), scheme="light"):  # noqa: ANN001, ANN202
    return browser.new_page(viewport={"width": size[0], "height": size[1]}, color_scheme=scheme)


# --- A3: deep links of docs/08 §4.3 ----------------------------------------------------------

DEEP_LINKS = [
    ("/freigaben/apr_0042", "a.id === 'apr_0042-h'"),
    (EXPLORE, "a.id === 'run-title'"),
    (f"{EXPLORE}?agent=explorer-1", "a.matches('.agent[data-sel=\"agent:explorer-1\"]')"),
    (f"{EXPLORE}?agent=explorer-1&seq=214", "a.id === 'seq-214'"),
    (f"{EXPLORE}?kanal=main#m7", "a.id === 'm-main-7'"),
    (f"{LEMMA}?ansicht=ergebnisse&ergebnis=4", "a.id === 'e004-h'"),
    ("/neuer-run?skill=proof-review&projekt=collatz", "a.tagName === 'TEXTAREA'"),
    ("/neuer-run?von=apr_0041", "a.tagName === 'H1' && a.textContent.includes('anpassen')"),
    ("/neuer-run?wie=20261021-2210_collatz_exploration_5d0e", "a.tagName === 'H1'"),
    ("/projekte/collatz", "a.id === 'project-title'"),
    ("/skills/proof-review", "a.id === 'skill-title'"),
    ("/profile/max-privat", "a.id === 'h-max-privat'"),
    ("/speicher?filter=verschlankbar", "a.tagName === 'H1'"),
    ("/einstellungen#vollmacht", "a.id === 'h-vollmacht'"),
]


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(("path", "check"), DEEP_LINKS)
def test_deep_links_focus_the_right_place(browser, base_url, path, check, size) -> None:  # noqa: ANN001
    """The focus lands where §4.3 says, inside the window, never on a button."""
    page = _page(browser, size)
    page.goto(base_url + path)
    page.wait_for_function(
        f"(() => {{ const a = document.activeElement; return {check}; }})()", timeout=6000
    )
    page.wait_for_timeout(300)  # layout settles; the focus stays
    info = page.evaluate(
        "(() => { const a = document.activeElement; const r = a.getBoundingClientRect(); "
        "return {tag: a.tagName, top: r.top, bottom: r.bottom, h: innerHeight}; })()"
    )
    assert info["tag"] != "BUTTON"
    assert 0 <= info["top"] < info["h"], (path, info)
    page.close()


def test_message_link_with_hash_opens_the_message(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}{EXPLORE}?kanal=main#m7")
    page.wait_for_url("**nachricht=7**", timeout=5000)
    page.wait_for_function("document.activeElement.id === 'm-main-7'", timeout=6000)
    page.close()


# --- A6 and T1: layout ---------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", SIZES)
def test_pages_fit_without_cutting(browser, base_url, size, scheme) -> None:  # noqa: ANN001
    page = _page(browser, size, scheme)
    problems = {}
    for path in PAGES + STATES:
        page.goto(base_url + path)
        page.wait_for_timeout(700)
        found = page.evaluate(OVERFLOW_JS)
        if found:
            problems[path] = found
    assert not problems, problems
    page.close()


@pytest.mark.parametrize("size", SIZES)
def test_start_shows_the_three_areas_without_scrolling(browser, base_url, size) -> None:  # noqa: ANN001
    """T1/P1: „Wartet auf dich“ (all entries), „Aktive Runs“ with the first run, „Warnungen“."""
    page = _page(browser, size)
    page.goto(base_url + "/")
    page.wait_for_selector(".run-card")
    page.wait_for_timeout(400)
    boxes = page.evaluate(
        """() => {
          const b = (el) => el && el.getBoundingClientRect().bottom;
          return {
            h: innerHeight,
            waiting: b(document.querySelector('.wait-list')),
            run: b(document.querySelector('.run-card')),
            warnings: b([...document.querySelectorAll('.warn-item')].pop()),
          };
        }"""
    )
    for key in ("waiting", "run", "warnings"):
        assert boxes[key] is not None and boxes[key] <= boxes["h"], (key, boxes)
    page.close()


# --- keyboard and dialogs ----------------------------------------------------------------------


def test_slim_dialog_focus_escape_and_trigger(browser, base_url) -> None:  # noqa: ANN001
    """K8: focus on the dialog heading, Esc cancels, focus back on the trigger."""
    page = _page(browser)
    page.goto(base_url + "/speicher")
    trigger = page.locator("button:has-text('8 Runs verschlanken …')")
    trigger.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("dialog[open]")
    page.wait_for_function("document.activeElement.tagName === 'H2'", timeout=3000)
    assert "verschlanken?" in page.evaluate("document.activeElement.textContent")
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('dialog[open]')", timeout=3000)
    page.wait_for_function(
        "document.activeElement.textContent.includes('8 Runs verschlanken')", timeout=3000
    )
    page.close()


def test_new_run_starts_with_cmd_enter(browser, base_url) -> None:  # noqa: ANN001
    """§7.6: ⌘↩ starts the run from the form; the run detail opens."""
    page = _page(browser)
    page.goto(base_url + "/neuer-run?skill=roundtable&projekt=collatz")
    area = page.locator("[id='f-task_md'] textarea")
    area.fill("# Aufgabe\n\nGibt es unendlich viele Primzahlzwillinge?")
    page.wait_for_function("!document.querySelector('#start-run').disabled", timeout=5000)
    area.focus()
    page.keyboard.press("Meta+Enter")
    page.wait_for_url("**/runs/**", timeout=6000)
    page.close()


def test_notification_leads_to_the_card_and_single_keys_grant_nothing(browser, base_url) -> None:  # noqa: ANN001
    """P13a: open the notification of the permission, press Space and ↩ at once: nothing is
    granted, the focus is on the card heading."""
    page = _page(browser)
    page.goto(base_url + "/usage")
    page.select_option("#note-select", "apr_0042")
    page.click(".toast >> text=Ansehen")
    page.wait_for_function("document.activeElement.id === 'apr_0042-h'", timeout=6000)
    page.keyboard.press("Space")
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    assert page.locator("#apr_0042").count() == 1
    assert page.evaluate("document.activeElement.id") == "apr_0042-h"
    page.close()


# --- W1-08.1: wide windows, fresh form, small fixes ----------------------------------------------

GEOMETRY_JS = """() => {
  const r = (sel) => document.querySelector(sel).getBoundingClientRect();
  const page = r('#inhalt > .page');
  const pad = parseFloat(getComputedStyle(document.querySelector('.page')).paddingLeft);
  return {
    main: r('#inhalt'), page, pad, crumbs: r('.crumbs'), chips: r('.global'),
    topPad: parseFloat(getComputedStyle(document.querySelector('.topbar')).paddingLeft),
    scrolls: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  };
}"""


@pytest.mark.parametrize("width", [1440, 2000, 2560])
@pytest.mark.parametrize("path", ["/", "/freigaben", "/speicher"])
def test_content_column_is_centred_on_wide_windows(browser, base_url, path, width) -> None:  # noqa: ANN001
    """M1: the column stands in the middle right of the navigation; crumbs and chips sit on the
    edges of its content; up to ≈ 1650 px nothing moves (1440 as before, at the navigation)."""
    page = _page(browser, (width, 900))
    page.goto(base_url + path)
    page.wait_for_timeout(700)
    g = page.evaluate(GEOMETRY_JS)
    left = g["page"]["left"] - g["main"]["left"]
    right = g["main"]["right"] - g["page"]["right"]
    assert abs(left - right) <= 2, g
    assert not g["scrolls"]
    if width == 1440:
        assert left == 0 and g["topPad"] == g["pad"]
    else:
        assert left > 100
    assert abs(g["crumbs"]["left"] - (g["page"]["left"] + g["pad"])) <= 2, g
    if g["chips"]["width"]:
        assert abs(g["chips"]["right"] - (g["page"]["right"] - g["pad"])) <= 2, g
    page.close()


def test_run_detail_keeps_the_full_width(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser, (2560, 900))
    page.goto(base_url + EXPLORE)
    page.wait_for_selector(".rd-page")
    g = page.evaluate(GEOMETRY_JS)
    assert abs(g["page"]["width"] - g["main"]["width"]) <= 1
    assert g["topPad"] == 24 and not g["scrolls"]
    page.close()


def test_fresh_new_run_form_shows_no_red_field(browser, base_url) -> None:  # noqa: ANN001
    """M2: nothing is red before you touch it; leaving the empty task shows its error."""
    page = _page(browser)
    page.goto(base_url + "/neuer-run?skill=exploration&projekt=collatz")
    page.wait_for_selector("text=Noch offen (1)")
    assert page.locator(".field.invalid, [aria-invalid]").count() == 0
    page.wait_for_function("document.activeElement.tagName === 'TEXTAREA'", timeout=5000)
    page.keyboard.press("Tab")  # the deep link focuses the task; leave it empty
    page.wait_for_selector("[id='f-task_md'].invalid", timeout=3000)
    page.wait_for_selector("text=1 Problem")
    page.fill("[id='f-task_md'] textarea", "# Aufgabe\n\nZwei Ideen prüfen.")
    page.wait_for_selector("[id='f-task_md']:not(.invalid)", timeout=3000)
    page.close()


def test_new_run_preview_opens_from_its_summary(browser, base_url) -> None:  # noqa: ANN001
    """G2: <summary> is the first child of <details> and opens the run.yaml preview."""
    page = _page(browser)
    page.goto(base_url + "/neuer-run?skill=exploration&projekt=collatz")
    summary = page.locator("details > summary", has_text="Vorschau: aufgelöste run.yaml")
    summary.click()
    page.wait_for_function("document.querySelector('details.card').open")
    page.close()


@pytest.mark.parametrize("path", ["/profile", "/einrichtung?schritt=2", "/einrichtung?schritt=5"])
def test_cards_of_a_grid_row_stand_level(browser, base_url, path) -> None:  # noqa: ANN001
    """G1: cards in a grid row start at the same height."""
    page = _page(browser)
    page.goto(base_url + path)
    page.wait_for_timeout(700)
    rows = page.evaluate(
        """() => [...document.querySelectorAll(':is(.grid-2e, .grid-3, .grid-4)')].map(
             (g) => [...g.children].map((c) => Math.round(c.getBoundingClientRect().top)))"""
    )
    tops = [row[:2] for row in rows if len(row) >= 2]
    assert tops and all(a == b for a, b in tops), rows
    page.close()


def test_setup_step_4_buttons_fit_1280x680(browser, base_url) -> None:  # noqa: ANN001
    """G10: the button row of step 4 is in the window at the minimum size."""
    page = _page(browser, (1280, 680))
    page.goto(base_url + "/einrichtung?schritt=4")
    button = page.locator("button", has_text="Projekt anlegen")
    button.wait_for()
    assert button.bounding_box()["y"] + button.bounding_box()["height"] <= 680
    page.close()

"""Focus rules of G2/K5/K8 in a real browser (docs/08-ux-konzept.md G2, K5, K8, §7.6; W1-06 A4).

Runs `arr gui --mock` in subprocesses (scenario and load data set) and drives the installed
Google Chrome with Playwright (no browser download). Start with:

    uv run --with playwright python -m pytest -m macos tests/test_gui_focus.py

(`python -m pytest`: the plain `pytest` script of the project venv cannot see the Playwright
that `--with` adds.) Skipped with this hint when Playwright is missing. Tests that decide
something come last, because the mock state is shared by the module's servers.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="nur macOS (Google Chrome)"),
]
RUN_HINT = "uv run --with playwright python -m pytest -m macos tests/test_gui_focus.py"
try:
    from playwright import sync_api
except ImportError:
    pytest.skip(
        f"Playwright fehlt in dieser Umgebung. Browser-Tests starten mit: {RUN_HINT}",
        allow_module_level=True,
    )

SIZES = [(1440, 760), (1280, 680)]
TOP_BAR = 52


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


ARM_WAIT_MS = 1700  # a card accepts ⌘↩ only after 1.5 s fully visible (W1-06.1 H1)


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _server(tmp_path_factory)


@pytest.fixture(scope="module")
def load_url(tmp_path_factory: pytest.TempPathFactory, browser) -> Iterator[str]:  # noqa: ANN001
    """A second server switched to the load data set (6 open approvals)."""
    for url in _server(tmp_path_factory):
        page = browser.new_page()
        page.goto(url + "/freigaben")
        page.select_option("#ds-select", "last")
        page.wait_for_function(
            "document.querySelector('[data-badge=\"waiting\"]')?.textContent === '6'",
            timeout=10000,
        )
        page.close()
        yield url


def _server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    port = _free_port()
    home = tmp_path_factory.mktemp("arr-home")
    code = "from arr.cli import app; app()"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "gui", "--mock", "--no-browser", "--port", str(port)],
        # without PYTEST_* variables, otherwise NiceGUI starts in its own test mode
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PYTEST")},
            "ARR_HOME": str(home),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=Path(__file__).parent,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "/runs", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("arr gui --mock did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        yield b
        b.close()


def _page(browser, size=(1440, 760)):  # noqa: ANN001, ANN202
    return browser.new_page(viewport={"width": size[0], "height": size[1]})


def _active(page) -> dict:  # noqa: ANN001
    return page.evaluate(
        "(() => { const a = document.activeElement; return {id: a.id, tag: a.tagName, "
        "text: a.textContent.trim().slice(0, 80)}; })()"
    )


def _card_open(page, approval_id: str) -> bool:  # noqa: ANN001
    return page.locator(f"#{approval_id}").count() == 1


def _waiting_badge(page) -> str:  # noqa: ANN001
    return page.locator('[data-badge="waiting"]').inner_text()


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("approval_id", ["apr_0041", "apr_0042", "apr_0040"])
def test_deep_link_focuses_heading_reliably(browser, base_url, size, approval_id) -> None:  # noqa: ANN001
    """Direct load of /freigaben/<id>: heading focused, card head visible below the top bar."""
    for _ in range(3):  # reliability (W0-05 Z13 rest)
        page = _page(browser, size)
        page.goto(f"{base_url}/freigaben/{approval_id}")
        page.wait_for_function(f"document.activeElement.id === '{approval_id}-h'", timeout=5000)
        page.wait_for_timeout(400)  # layout settles; focus must stay
        active = _active(page)
        assert active["id"] == f"{approval_id}-h" and active["tag"] == "H3"
        box = page.locator(f"#{approval_id}").bounding_box()
        assert TOP_BAR <= box["y"] <= TOP_BAR + 40, box  # card head (type, risk, deadline) visible
        assert page.locator(f"#{approval_id}").evaluate("e => e.classList.contains('focus')")
        page.close()


def test_hash_link_focuses_heading(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}/freigaben#apr_0041")
    page.wait_for_function("document.activeElement.id === 'apr_0041-h'", timeout=5000)
    page.close()


def test_run_deep_link_focuses_run_title(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}/runs/20261024-1412_collatz_exploration_a3f9")
    page.wait_for_function("document.activeElement.id === 'run-title'", timeout=5000)
    page.close()


@pytest.mark.parametrize("approval_id", ["apr_0042", "apr_0041"])
def test_space_and_enter_after_deep_link_grant_nothing(browser, base_url, approval_id) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}/freigaben/{approval_id}")
    page.wait_for_function(f"document.activeElement.id === '{approval_id}-h'", timeout=5000)
    for key in [" ", "Enter", "Enter", " "]:
        page.keyboard.press(key)
    page.wait_for_timeout(800)
    assert _card_open(page, approval_id)
    assert _waiting_badge(page) == "3"
    page.close()


def test_single_keys_on_grant_buttons_do_nothing(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}/freigaben")
    for card in ["apr_0042", "apr_0041"]:
        button = page.locator(f"#{card} [data-primary]")
        button.focus()
        page.keyboard.press("Enter")
        page.keyboard.press(" ")
        page.wait_for_timeout(500)
        assert _card_open(page, card)
    assert "⌘↩" in page.locator(".toasts").inner_text()
    assert _waiting_badge(page) == "3"
    page.close()


def test_cmd_enter_outside_a_card_does_nothing(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}/freigaben")
    page.locator("#inhalt h1").focus()
    page.keyboard.press("Meta+Enter")
    page.wait_for_timeout(600)
    assert _card_open(page, "apr_0042") and _card_open(page, "apr_0041")
    # escalation without a chosen option: ⌘↩ in the card does nothing either
    page.locator("#apr_0040-h").focus()
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    page.wait_for_timeout(600)
    assert _card_open(page, "apr_0040")
    assert _waiting_badge(page) == "3"
    page.close()


def test_dialog_focuses_heading_and_returns_focus(browser, base_url) -> None:  # noqa: ANN001
    page = _page(browser)
    page.goto(f"{base_url}/freigaben")
    # Run-Start ablehnen …: Esc cancels, focus back on the trigger
    trigger = page.locator("#apr_0041 [data-mark='deny'], #apr_0041 button:has-text('Ablehnen')")
    trigger.first.click()
    page.wait_for_function("document.activeElement.closest('dialog[open]') !== null", timeout=5000)
    assert _active(page)["tag"] == "H2"
    assert _active(page)["text"] == "Run-Start ablehnen?"
    page.keyboard.press("Escape")
    page.wait_for_function("!document.querySelector('dialog[open]')", timeout=5000)
    assert _active(page)["text"].startswith("Ablehnen")
    # „immer …“: dialog, „Abbrechen“ by keyboard, focus back on „Erlauben“
    page.evaluate("window.scrollTo(0, 0)")  # the card must be in view to accept ⌘↩
    page.locator("#apr_0042 input[value='always']").check()
    allow = page.locator("#apr_0042 [data-primary]")
    allow.focus()
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    page.wait_for_function("document.activeElement.tagName === 'H2'", timeout=5000)
    assert "immer erlauben?" in _active(page)["text"]
    page.keyboard.press("Tab")  # Abbrechen
    page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('dialog[open]')", timeout=5000)
    assert _active(page)["text"].startswith("Erlauben")
    assert _card_open(page, "apr_0042")
    page.close()


def test_cmd_enter_acts_on_the_focused_card_only(browser, base_url) -> None:  # noqa: ANN001
    """Last test: grants apr_0042 (state change)."""
    page = _page(browser)
    page.goto(f"{base_url}/freigaben/apr_0042")
    page.wait_for_function("document.activeElement.id === 'apr_0042-h'", timeout=5000)
    page.keyboard.press("Meta+Enter")  # right after the deep link: locked
    page.wait_for_timeout(600)
    assert _card_open(page, "apr_0042")
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    page.wait_for_function("!document.getElementById('apr_0042')", timeout=5000)
    assert _card_open(page, "apr_0041") and _card_open(page, "apr_0040")
    page.wait_for_function(
        "document.querySelector('[data-badge=\"waiting\"]').textContent === '2'", timeout=5000
    )
    # focus moved to the next card's heading, not to a button
    assert _active(page)["tag"] == "H3"
    assert "– arr" in page.title() and page.title().startswith("(2)")
    page.close()


def _decided(page, approval_id: str) -> None:  # noqa: ANN001
    page.wait_for_function(f"!document.getElementById('{approval_id}')", timeout=5000)


def test_second_cmd_enter_does_not_decide_the_next_permission(browser, load_url) -> None:  # noqa: ANN001
    """Review H1, way 1: ⌘↩ on apr_1201, 700 ms later ⌘↩ again → apr_1202 stays open."""
    page = _page(browser)
    page.goto(f"{load_url}/freigaben/apr_1201")
    page.wait_for_function("document.activeElement.id === 'apr_1201-h'", timeout=5000)
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    _decided(page, "apr_1201")
    page.wait_for_function("document.activeElement.id === 'apr_1202-h'", timeout=5000)
    page.wait_for_timeout(700)
    page.keyboard.press("Meta+Enter")
    page.wait_for_timeout(800)
    assert _card_open(page, "apr_1202"), "the next card was decided unread"
    assert _waiting_badge(page) == "5"
    # once the card has been in view for 1.5 s, a fresh ⌘↩ works again
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    _decided(page, "apr_1202")
    page.close()


def test_held_key_does_not_decide_the_next_card(browser, load_url) -> None:  # noqa: ANN001
    """Review H1, way 3: a held ⌘↩ (auto-repeat) decides at most the card it started on."""
    page = _page(browser)
    page.goto(f"{load_url}/freigaben/apr_1203")
    page.wait_for_function("document.activeElement.id === 'apr_1203-h'", timeout=5000)
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.down("Meta")
    page.keyboard.down("Enter")
    _decided(page, "apr_1203")
    page.wait_for_function("document.activeElement.id === 'apr_1204-h'", timeout=5000)
    page.wait_for_timeout(ARM_WAIT_MS)  # long enough to arm: only the repeat flag protects now
    for _ in range(5):
        page.keyboard.down("Enter")  # still held: auto-repeat events
        page.wait_for_timeout(50)
    page.keyboard.up("Enter")
    page.keyboard.up("Meta")
    page.wait_for_timeout(600)
    assert _card_open(page, "apr_1204"), "auto-repeat decided the next card"
    page.close()


def test_load_more_keeps_position_and_focuses_first_new_run(browser, load_url) -> None:  # noqa: ANN001
    """W1-06.1 point 9: no reload, no jump to the top, focus on the first new run."""
    page = _page(browser)
    page.goto(f"{load_url}/runs")
    page.wait_for_selector("table.tbl")
    page.evaluate("window.arrMarker = 'same page'")
    more = page.get_by_role("button", name="Weitere 50 laden")
    more.scroll_into_view_if_needed()
    before = page.evaluate("window.scrollY")
    more.click()
    page.wait_for_function("document.querySelectorAll('tr[data-href]').length === 100")
    page.wait_for_function("document.activeElement.closest('tr[data-href]') !== null")
    rows = page.evaluate("[...document.querySelectorAll('tr[data-href]')].map(r => r.id)")
    focused = page.evaluate("document.activeElement.closest('tr').id")
    assert focused == rows[50]
    assert page.evaluate("window.arrMarker") == "same page"  # no reload
    assert page.evaluate("window.scrollY") >= before > 0  # no jump to the top
    assert "n=100" in page.url
    page.close()


def test_no_horizontal_scrolling_light_and_dark(browser, base_url) -> None:  # noqa: ANN001
    """W1-06.1 B4: every page at both sizes, light and dark, without horizontal scrolling."""
    for scheme in ["light", "dark"]:
        for size in SIZES:
            page = browser.new_page(
                viewport={"width": size[0], "height": size[1]}, color_scheme=scheme
            )
            for path in [
                "/",
                "/freigaben",
                "/freigaben/apr_0041",
                "/freigaben?ansicht=verlauf",
                "/runs",
                "/runs?status=fertig",
                "/runs/20261024-1412_collatz_exploration_a3f9",
            ]:
                page.goto(base_url + path)
                page.wait_for_selector(".app")
                page.wait_for_timeout(300)
                widths = page.evaluate(
                    "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
                )
                assert widths[0] <= widths[1], (scheme, size, path, widths)
            page.close()


def test_second_cmd_enter_after_escalation_does_not_start_a_run(browser, base_url) -> None:  # noqa: ANN001
    """Review H1, way 2 (scenario): escalation decided by ⌘↩, the run start below stays open.

    Runs last: decides apr_0040.
    """
    page = _page(browser)
    page.goto(f"{base_url}/freigaben/apr_0040")
    page.wait_for_function("document.activeElement.id === 'apr_0040-h'", timeout=5000)
    page.locator("#apr_0040 input[value='retry']").check()
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    _decided(page, "apr_0040")
    page.wait_for_function("document.activeElement.id === 'apr_0041-h'", timeout=5000)
    page.wait_for_timeout(700)
    page.keyboard.press("Meta+Enter")
    page.wait_for_timeout(800)
    assert _card_open(page, "apr_0041"), "the run start was approved unread (E15)"
    page.close()


# --- W1-06.2: every way of granting needs a seen card ----------------------------------------


def _reset(browser, url: str, dataset: str) -> None:  # noqa: ANN001
    """Reload a data set (fresh mock state) through the switch in the navigation."""
    page = browser.new_page()
    page.goto(url + "/runs")
    page.wait_for_selector("#ds-select")
    for value in ("szenario" if dataset == "last" else "last", dataset):
        with page.expect_navigation():
            page.select_option("#ds-select", value)
        page.wait_for_selector("#ds-select")
    page.close()


def _center(page, selector: str) -> tuple[float, float]:  # noqa: ANN001
    box = page.locator(selector).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("pause_ms", [150, 400, 700])
def test_double_click_does_not_grant_the_next_card(browser, load_url, size, pause_ms) -> None:  # noqa: ANN001
    """N1: a double click on „Erlauben“ grants apr_1201 only; apr_1202 stays open."""
    _reset(browser, load_url, "last")
    page = _page(browser, size)
    page.goto(f"{load_url}/freigaben/apr_1201")
    page.wait_for_function("document.activeElement.id === 'apr_1201-h'", timeout=5000)
    page.wait_for_timeout(ARM_WAIT_MS)
    x, y = _center(page, "#apr_1201 [data-primary]")
    page.mouse.click(x, y)
    page.wait_for_timeout(pause_ms)
    page.mouse.click(x, y)
    _decided(page, "apr_1201")
    page.wait_for_timeout(800)  # whatever lies under the pointer now must not have been granted
    assert _card_open(page, "apr_1202"), "the next card was granted by the second click"
    assert _waiting_badge(page) == "5"
    page.close()


def test_click_on_a_card_that_just_moved_up_does_nothing(browser, load_url) -> None:  # noqa: ANN001
    """N1/B3: right after the next card moved up its grant button is locked, ~1.5 s later not."""
    _reset(browser, load_url, "last")
    page = _page(browser)
    page.goto(f"{load_url}/freigaben/apr_1201")
    page.wait_for_function("document.activeElement.id === 'apr_1201-h'", timeout=5000)
    page.mouse.click(*_center(page, "#apr_1201 [data-primary]"))  # too early after the load
    page.wait_for_timeout(600)
    assert _card_open(page, "apr_1201") and _waiting_badge(page) == "6"
    page.wait_for_timeout(ARM_WAIT_MS)
    page.mouse.click(*_center(page, "#apr_1201 [data-primary]"))
    _decided(page, "apr_1201")
    page.mouse.click(*_center(page, "#apr_1202 [data-primary]"))  # just moved up: locked
    page.wait_for_timeout(600)
    assert _card_open(page, "apr_1202")
    page.wait_for_timeout(ARM_WAIT_MS)  # seen long enough: a normal click works again
    page.mouse.click(*_center(page, "#apr_1202 [data-primary]"))
    _decided(page, "apr_1202")
    # „Ablehnen“ stays free: no lock on the safe way
    page.mouse.click(*_center(page, "#apr_1203 button:has-text('Ablehnen')"))
    _decided(page, "apr_1203")
    page.close()


@pytest.mark.parametrize("way", ["cmd-enter", "click"])
def test_run_start_after_deep_link_at_1280_can_be_approved(browser, base_url, way) -> None:  # noqa: ANN001
    """N2: at 1280 × 680 head and decision row are in view; approve after 1.5 s, no scrolling."""
    _reset(browser, base_url, "szenario")
    page = _page(browser, (1280, 680))
    page.goto(f"{base_url}/freigaben/apr_0041")
    page.wait_for_function("document.activeElement.id === 'apr_0041-h'", timeout=5000)
    page.wait_for_timeout(ARM_WAIT_MS)
    scroll = page.evaluate("window.scrollY")
    buttons = page.locator("#apr_0041 .af").bounding_box()
    assert buttons["y"] + buttons["height"] <= 680 + 1  # decision row visible
    if way == "cmd-enter":
        page.keyboard.press("Meta+Enter")
    else:
        page.mouse.click(*_center(page, "#apr_0041 [data-primary]"))
    page.wait_for_selector("#apr_0041 .appr-done")
    assert page.evaluate("window.scrollY") == scroll  # approved without scrolling
    _decided(page, "apr_0041")
    assert _waiting_badge(page) == "2"
    page.close()


def test_card_with_buttons_out_of_view_is_not_armed(browser, base_url) -> None:  # noqa: ANN001
    """N2: „seen“ means head and decision row in view; a card whose buttons lie below the
    window does not become armed (only cards taller than the window keep their buttons at the
    bottom edge)."""
    _reset(browser, base_url, "szenario")
    page = _page(browser)
    page.goto(f"{base_url}/freigaben")
    page.wait_for_selector("#apr_0040")
    page.wait_for_timeout(ARM_WAIT_MS + 300)
    armed = page.evaluate(
        "['apr_0042', 'apr_0040', 'apr_0041'].map(id => !!document.getElementById(id)._arrArmed)"
    )
    assert armed == [True, False, False]
    tall = page.evaluate(
        "['apr_0042', 'apr_0040'].map(id => document.getElementById(id).classList.contains('tall'))"
    )
    assert tall == [False, False]
    page.close()

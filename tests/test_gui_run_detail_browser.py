"""Run detail (W1-07) in a real browser: opening at „neu“, „Live folgen“, the lock of the compact
approval card, layout at 1440 × 760 and 1280 × 680, compact levels, formulas.

Like tests/test_gui_focus.py: `arr gui --mock` in a subprocess (here with a faster simulated
live stream), the installed Google Chrome through Playwright. Start with:

    uv run --with playwright python -m pytest -m macos tests/test_gui_run_detail_browser.py

The 5-minute load measurement (A5) runs only with ARR_STREAM_MINUTES set, e.g.
`ARR_STREAM_MINUTES=5 uv run --with playwright python -m pytest -m macos -s -k measurement …`.
"""

import json
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
RUN_HINT = "uv run --with playwright python -m pytest -m macos tests/test_gui_run_detail_browser.py"
try:
    from playwright import sync_api
except ImportError:
    pytest.skip(
        f"Playwright fehlt in dieser Umgebung. Browser-Tests starten mit: {RUN_HINT}",
        allow_module_level=True,
    )

SIZES = [(1440, 760), (1280, 680)]
ARM_WAIT_MS = 1700  # granting works after 1.5 s seen (08 G2)
EXPLORE = "/runs/20261024-1412_collatz_exploration_a3f9"
SATZ2 = "/runs/20261024-0800_collatz_proof_d41a"
GOLDBACH = "/runs/20261024-1230_collatz_exploration_90aa"
LEMMA = "/runs/20261024-0930_collatz_proof_7c21"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server(tmp_path_factory: pytest.TempPathFactory, rate: float) -> Iterator[str]:
    port = _free_port()
    home = tmp_path_factory.mktemp("arr-home")
    code = (
        "import arr.service.mock_api as m; "
        f"m.RUN_DETAIL_STREAM_RATE = {rate}; "
        "from arr.cli import app; app()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "gui", "--mock", "--no-browser", "--port", str(port)],
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


@pytest.fixture(scope="module")
def live_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Scenario with a fast live stream (about one channel message every 2 s per run)."""
    yield from _server(tmp_path_factory, rate=6.0)


@pytest.fixture(scope="module")
def calm_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A server with the normal pace, for layout and approval tests."""
    yield from _server(tmp_path_factory, rate=1.0)


def _page(browser, size=(1440, 760), scheme="light"):  # noqa: ANN001, ANN202
    return browser.new_page(viewport={"width": size[0], "height": size[1]}, color_scheme=scheme)


def _dataset(browser, url: str, dataset: str) -> None:  # noqa: ANN001
    """Switch the mock to a data set; switching always reloads it, so decisions are undone."""
    page = browser.new_page()
    page.goto(url + "/runs")
    other = "szenario" if dataset == "last" else "last"
    for value in (other, dataset):
        page.select_option("#ds-select", value)
        page.wait_for_function(
            f"document.querySelector('#ds-select')?.value === '{value}'", timeout=10000
        )
        page.wait_for_timeout(600)
    page.close()


def _open(page, url: str) -> None:  # noqa: ANN001
    page.goto(url)
    page.wait_for_selector(".rd .rd-view .rd-scroll", timeout=10000)
    page.wait_for_timeout(500)


def _scroll_state(page) -> dict:  # noqa: ANN001
    return page.evaluate(
        "(() => { const b = document.querySelector('.rd-view .rd-scroll'); "
        "const j = document.querySelector('.rd-view .jump'); "
        "const t = document.querySelector('.rd-view .follow-toggle'); "
        "return {top: b.scrollTop, height: b.scrollHeight, client: b.clientHeight, "
        "atEnd: b.scrollTop + b.clientHeight >= b.scrollHeight - 24, "
        "rows: b.querySelectorAll('.rd-list > *').length, "
        "jump: j && !j.hidden ? j.textContent.trim() : null, "
        "follow: t ? t.getAttribute('aria-pressed') : null}; })()"
    )


def _wait_new_rows(page, rows: int, timeout: float = 25.0) -> dict:  # noqa: ANN001
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _scroll_state(page)
        if state["rows"] > rows:
            page.wait_for_timeout(300)  # let following (rAF) finish
            return _scroll_state(page)
        page.wait_for_timeout(200)
    pytest.fail("no live entry arrived")


# --- A3: channel opens at „neu“, „Live folgen“ only at the end ---------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_channel_opens_at_the_separator(browser, live_url, size) -> None:  # noqa: ANN001
    page = _page(browser, size)
    _open(page, live_url + EXPLORE)
    page.wait_for_timeout(700)  # alignment settles (fonts, formulas)
    box = page.locator(".rd-view .rd-scroll").bounding_box()
    sep = page.locator("#rd-new").bounding_box()
    assert sep is not None, "separator „neu“ missing"
    # the separator lies in the upper half, one or two older messages are visible above it
    assert box["y"] + 20 <= sep["y"] <= box["y"] + box["height"] / 2, (box, sep)
    above = page.evaluate(
        "(() => { const sep = document.querySelector('.sep-row'); const box = "
        "document.querySelector('.rd-view .rd-scroll').getBoundingClientRect(); "
        "const prev = sep.previousElementSibling.getBoundingClientRect(); "
        "return prev.bottom > box.top + 10; })()"
    )
    assert above, "no older message visible above the separator"
    state = _scroll_state(page)
    assert state["follow"] == "false"
    # „▼ Ans Ende · Live folgen“, or the count if the live stream was quicker than the test
    assert state["jump"] == "▼ Ans Ende · Live folgen" or "neue Nachricht" in state["jump"]
    page.close()


def test_live_follow_starts_only_at_the_end(browser, live_url) -> None:  # noqa: ANN001
    page = _page(browser)
    _open(page, live_url + EXPLORE)
    page.wait_for_timeout(700)
    start = _scroll_state(page)
    assert start["follow"] == "false"
    # a new message arrives while you read at the separator: nothing moves, it is counted
    after = _wait_new_rows(page, start["rows"])
    assert abs(after["top"] - start["top"]) <= 2, (start, after)
    assert not after["atEnd"] and after["follow"] == "false"
    assert after["jump"].startswith("▼ ") and "neue Nachricht" in after["jump"]
    # „▼ … neue Nachrichten“ leads to the end, from then on the view follows
    page.locator(".rd-view .jump").click()
    page.wait_for_timeout(400)
    end = _scroll_state(page)
    assert end["atEnd"] and end["follow"] == "true" and end["jump"] is None
    later = _wait_new_rows(page, end["rows"])
    assert later["atEnd"] and later["follow"] == "true"
    # scrolling up switches following off again
    page.locator(".rd-view .rd-scroll").hover()
    page.mouse.wheel(0, -400)
    page.wait_for_timeout(400)
    up = _scroll_state(page)
    assert up["follow"] == "false" and not up["atEnd"]
    still = _wait_new_rows(page, up["rows"])
    assert abs(still["top"] - up["top"]) <= 2
    page.close()


def test_transcript_follows_live_at_the_end(browser, live_url) -> None:  # noqa: ANN001
    page = _page(browser)
    _open(page, live_url + EXPLORE + "?agent=explorer-2")
    page.wait_for_timeout(500)
    start = _scroll_state(page)
    assert start["atEnd"] and start["follow"] == "true"
    later = _wait_new_rows(page, start["rows"])
    assert later["atEnd"] and later["follow"] == "true"
    page.mouse.move(700, 500)
    page.mouse.wheel(0, -500)
    page.wait_for_timeout(400)
    up = _scroll_state(page)
    assert up["follow"] == "false"
    newer = _wait_new_rows(page, up["rows"])
    assert abs(newer["top"] - up["top"]) <= 2 and "neue" in (newer["jump"] or "")
    page.close()


# --- selection, URL, formulas, compact levels ------------------------------------------------


def test_selection_without_reload_keeps_the_url(browser, calm_url) -> None:  # noqa: ANN001
    page = _page(browser)
    _open(page, calm_url + EXPLORE)
    page.evaluate("window.__marker = 1")
    page.locator('.agent[data-sel="agent:explorer-2"]').click()
    page.wait_for_selector(".pane-info:has-text('explorer-2')")
    assert page.evaluate("window.__marker") == 1  # same page, no reload
    assert page.url.endswith("?agent=explorer-2")
    assert page.locator('.agent[data-sel="agent:explorer-2"]').get_attribute("aria-current")
    page.locator('a[data-sel="ansicht:ergebnisse"]').click()
    page.wait_for_selector("text=Noch keine Ergebnisse")
    assert page.url.endswith("?ansicht=ergebnisse")
    page.go_back()  # back reloads the previous selection
    page.wait_for_selector(".pane-info:has-text('explorer-2')")
    page.close()


def test_formulas_are_typeset(browser, calm_url) -> None:  # noqa: ANN001
    page = _page(browser)
    _open(page, calm_url + EXPLORE)
    page.wait_for_function("document.querySelectorAll('.math math').length > 3", timeout=5000)
    tex = page.evaluate(
        "document.querySelector('.math math annotation[encoding=\"application/x-tex\"]')"
        "?.textContent"
    )
    assert tex  # the LaTeX source stays for screen readers and copying (§7.7)
    page.close()


@pytest.mark.parametrize(
    ("path", "size", "level"),
    [
        (EXPLORE, (1440, 760), "lvl-2"),
        (EXPLORE, (1280, 1000), "lvl-full"),  # by space, not by window width
        (SATZ2, (1440, 760), "lvl-1"),
        (SATZ2, (1280, 680), "lvl-1"),
    ],
)
def test_agent_cards_compact_by_space(browser, calm_url, path, size, level) -> None:  # noqa: ANN001
    if path == SATZ2:
        _dataset(browser, calm_url, "last")
    else:
        _dataset(browser, calm_url, "szenario")
    page = _page(browser, size)
    _open(page, calm_url + path)
    page.wait_for_function(
        f"document.querySelector('.rd-left').classList.contains('{level}')", timeout=3000
    )
    first = page.locator(".agents > .agent").first
    assert "wartet auf dich" in first.inner_text()  # always on top, always visible
    box = first.bounding_box()
    assert box and box["y"] + box["height"] <= size[1]
    assert "jetzt:" in (first.get_attribute("title") or "")
    page.close()


# --- A2: what matters without scrolling, both sizes, light and dark --------------------------


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("dataset", ["szenario", "last"])
def test_layout_shows_the_important_parts(browser, calm_url, dataset, size, scheme) -> None:  # noqa: ANN001
    _dataset(browser, calm_url, dataset)
    page = _page(browser, size, scheme)
    _open(page, calm_url + (EXPLORE if dataset == "szenario" else SATZ2))
    page.wait_for_timeout(400)
    sizes = page.evaluate(
        "[document.documentElement.scrollWidth, document.documentElement.clientWidth, "
        "document.documentElement.scrollHeight, document.documentElement.clientHeight]"
    )
    assert sizes[0] <= sizes[1] and sizes[2] <= sizes[3], sizes  # the page itself never scrolls

    def visible(selector: str) -> bool:
        box = page.locator(selector).first.bounding_box()
        return bool(box) and box["y"] >= 0 and box["y"] + box["height"] <= size[1] + 1

    for selector in [
        "#run-title",
        ".rd-title .badge",
        ".rd-attn",  # „Wartet auf dich“ at every width
        ".rd-attn a.btn",  # „Ansehen“
        ".stagebar",
        ".rd-meta",
        ".agent.needs-me",
        ".sel-item.sel",
        "#rd-new",
        ".composer",
        ".rd-title .actions",
    ]:
        assert visible(selector), selector
    overview = page.locator(".rd-right")
    if size[0] >= 1440:
        assert visible(".rd-right .appr.compact h3") and visible(".rd-right .appr.compact .af")
    else:
        assert not overview.is_visible()
    if dataset == "last":
        assert visible(".rd-attn .qchip")  # „? 1 Frage an dich“ next to the banner
        assert page.locator(".stagebar").get_attribute("class") == "stagebar collapsed"
    page.close()


# --- G2: the compact card uses the lock of the Freigaben page --------------------------------


def _allow(page, approval_id: str):  # noqa: ANN001, ANN202
    return page.locator(f"#cc-{approval_id} [data-grant]")


def _open_count(page) -> int:  # noqa: ANN001
    text = page.locator(".rd-right .side-h", has_text="Wartet auf dich").inner_text()
    return int(text.rsplit("·", 1)[1])


@pytest.mark.parametrize("pause_ms", [150, 400, 700])
def test_double_click_does_not_grant_the_next_card(browser, calm_url, pause_ms) -> None:  # noqa: ANN001
    _dataset(browser, calm_url, "last")
    page = _page(browser)
    _open(page, calm_url + GOLDBACH)
    assert _open_count(page) == 2
    first = page.locator(".rd-right .appr.compact").first.get_attribute("id")
    second = page.locator(".rd-right .appr.compact").nth(1).get_attribute("id")
    page.wait_for_timeout(ARM_WAIT_MS)
    box = _allow(page, first.removeprefix("cc-")).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(x, y)
    page.wait_for_timeout(pause_ms)
    page.mouse.click(x, y)
    page.wait_for_timeout(2200)  # confirmation (1.5 s), then the next card moves up
    assert page.locator(f"#{second}").count() == 1  # still open
    assert _open_count(page) == 1
    assert page.locator('[data-badge="waiting"]').inner_text() == "5"
    page.close()


def test_click_right_after_load_does_nothing(browser, calm_url) -> None:  # noqa: ANN001
    _dataset(browser, calm_url, "last")
    page = _page(browser)
    _open(page, calm_url + GOLDBACH)
    card = page.locator(".rd-right .appr.compact").first.get_attribute("id")
    _allow(page, card.removeprefix("cc-")).click()
    page.wait_for_timeout(300)
    assert page.locator(f"#{card}.decided").count() == 0  # no confirmation: nothing granted
    page.wait_for_timeout(1900)
    assert _open_count(page) == 2 and page.locator(f"#{card}").count() == 1
    _allow(page, card.removeprefix("cc-")).click()
    page.wait_for_timeout(2200)
    assert _open_count(page) == 1 and page.locator(f"#{card}").count() == 0
    page.close()


def test_second_cmd_enter_does_not_decide_the_next_card(browser, calm_url) -> None:  # noqa: ANN001
    _dataset(browser, calm_url, "last")
    page = _page(browser)
    _open(page, calm_url + GOLDBACH)
    first = page.locator(".rd-right .appr.compact").first.get_attribute("id")
    second = page.locator(".rd-right .appr.compact").nth(1).get_attribute("id")
    page.locator(f"#{first} a.btn").focus()  # „Details“ inside the card
    page.wait_for_timeout(ARM_WAIT_MS)
    page.keyboard.press("Meta+Enter")
    page.wait_for_timeout(700)
    page.keyboard.press("Meta+Enter")  # focus is on the confirmation: nothing happens
    page.wait_for_timeout(1300)
    page.keyboard.press("Meta+Enter")  # focus moved to the next heading, not armed yet
    page.wait_for_timeout(600)
    assert page.locator(f"#{second}").count() == 1 and _open_count(page) == 1
    active = page.evaluate("document.activeElement.id")
    assert active == f"{second}-ch"  # focus on the next card's heading, never a button
    page.close()


def test_card_scrolled_out_of_its_column_is_not_armed(browser, calm_url) -> None:  # noqa: ANN001
    """The head of the card hidden by scrolling the overview: it must not count as seen."""
    _dataset(browser, calm_url, "last")
    page = _page(browser, (1440, 760))
    _open(page, calm_url + GOLDBACH)
    card = page.locator(".rd-right .appr.compact").first.get_attribute("id")
    page.evaluate("document.querySelector('.rd-right').scrollTop = 70")
    page.wait_for_timeout(ARM_WAIT_MS + 300)
    head = page.locator(f"#{card} .ah").bounding_box()
    column = page.locator(".rd-right").bounding_box()
    assert head["y"] < column["y"], "the card head should be hidden above the column"
    _allow(page, card.removeprefix("cc-")).click()
    page.wait_for_timeout(300)
    assert page.locator(f"#{card}.decided").count() == 0  # no confirmation: nothing granted
    page.wait_for_timeout(1900)
    assert page.locator(f"#{card}").count() == 1 and _open_count(page) == 2
    page.close()


# --- A5: 5 minutes of live stream on the load data set -------------------------------------


def _renderer_rss_kb(user_data_dir: str) -> int:
    out = subprocess.run(["ps", "-axo", "rss=,command="], capture_output=True, text=True).stdout
    return sum(
        int(line.split(None, 1)[0])
        for line in out.splitlines()
        if "--type=renderer" in line and user_data_dir in line
    )


@pytest.mark.skipif(not os.environ.get("ARR_STREAM_MINUTES"), reason="Messung nur auf Wunsch")
@pytest.mark.parametrize("view", ["kanal", "transkript"])
def test_live_stream_measurement(tmp_path_factory, view) -> None:  # noqa: ANN001
    minutes = float(os.environ["ARR_STREAM_MINUTES"])
    rate = float(os.environ.get("ARR_STREAM_RATE", "10"))
    profile = str(tmp_path_factory.mktemp("chrome-profile"))
    for url in _server(tmp_path_factory, rate=rate):
        with sync_api.sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                profile, channel="chrome", viewport={"width": 1440, "height": 760}
            )
            page = context.new_page()
            page.goto(url + "/runs")
            page.select_option("#ds-select", "last")
            page.wait_for_timeout(1500)
            path = SATZ2 + ("?agent=prover-1" if view == "transkript" else "")
            _open(page, url + path)
            if view == "kanal":
                page.locator(".rd-view .jump").click()  # follow the channel live
            page.evaluate(
                "window.__long = []; new PerformanceObserver((l) => l.getEntries().forEach("
                "(e) => window.__long.push(e.duration))).observe({type: 'longtask', "
                "buffered: false}); window.__frames = []; let last = performance.now(); "
                "const f = (t) => { window.__frames.push(t - last); last = t; "
                "requestAnimationFrame(f); }; requestAnimationFrame(f);"
            )
            cdp = context.new_cdp_session(page)
            cdp.send("Performance.enable")
            samples = []
            end = time.time() + minutes * 60
            while time.time() < end:
                metrics = {
                    m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]
                }
                samples.append(
                    {
                        "t": round(minutes * 60 - (end - time.time())),
                        "heap_mb": round(metrics["JSHeapUsedSize"] / 1e6, 1),
                        "nodes": int(metrics["Nodes"]),
                        "rss_mb": round(_renderer_rss_kb(profile) / 1000),
                        "rows": _scroll_state(page)["rows"],
                    }
                )
                page.wait_for_timeout(15000)
            stats = page.evaluate(
                "(() => { const f = window.__frames.slice(10); "
                "const slow = f.filter((d) => d > 50); return {frames: f.length, "
                "over50ms: slow.length, max_ms: Math.round(Math.max(...f)), "
                "long_tasks: window.__long.length, "
                "long_max_ms: Math.round(Math.max(0, ...window.__long))}; })()"
            )
            state = _scroll_state(page)
            print(
                json.dumps(
                    {"view": view, "rate": rate, "samples": samples, "stats": stats, "end": state},
                    ensure_ascii=False,
                )
            )
            assert state["rows"] <= 200 + 40  # the last 200 (+ context of the channel)
            assert state["atEnd"] and state["follow"] == "true"  # still following after 5 min
            assert stats["over50ms"] / max(1, stats["frames"]) < 0.02  # under 2 % slow frames
            context.close()


def test_one_line_cards_show_now_in_a_quick_tooltip(browser, calm_url) -> None:  # noqa: ANN001
    """P14: the one-line card shows „jetzt“ on hover after a moment and at once on focus."""
    _dataset(browser, calm_url, "last")
    page = _page(browser, (1280, 680))
    _open(page, calm_url + SATZ2)
    page.wait_for_function("document.querySelector('.rd-left').classList.contains('lvl-1')")
    card = page.locator('.agent[data-sel="agent:prover-1"]')
    card.hover()
    page.wait_for_selector(".rd-tip", timeout=2000)
    assert "jetzt:" in page.locator(".rd-tip").inner_text()
    assert card.get_attribute("aria-describedby") == "rd-tip"
    page.mouse.move(700, 600)
    page.wait_for_timeout(200)
    assert page.locator(".rd-tip").count() == 0
    assert "jetzt:" in (card.get_attribute("title") or "")  # title is back for screen readers
    page.locator('.agent[data-sel="agent:critic-2"]').focus()
    tip = page.locator(".rd-tip").inner_text()
    assert "1 Frage an dich" in tip
    page.close()


# --- W1-07.1 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_compact_and_transcript_cards_show_the_whole_command(browser, calm_url, size) -> None:  # noqa: ANN001
    """Point 1 (H1): command and path wrap; nothing hides behind a sideways scroll area."""
    _dataset(browser, calm_url, "last")
    page = _page(browser, size)
    paths = [GOLDBACH + "?agent=explorer-gpt", GOLDBACH + "?agent=explorer-1"]
    if size[0] >= 1440:
        paths.insert(0, GOLDBACH)  # overview, 300 px column
    for path in paths:
        _open(page, calm_url + path)
        commands = page.evaluate(
            "[...document.querySelectorAll('.appr.compact .cmd')].filter((c) => c.offsetParent)"
            ".map((c) => ({text: c.textContent, sw: c.scrollWidth, cw: c.clientWidth, "
            "right: c.getBoundingClientRect().right, "
            "col: c.closest('.rd-col').getBoundingClientRect().right}))"
        )
        assert commands, path
        for cmd in commands:
            assert cmd["sw"] <= cmd["cw"], cmd  # no sideways scrolling inside the command
            assert cmd["right"] <= cmd["col"] + 1, cmd
        texts = {c["text"] for c in commands}
        assert texts & {
            "wget https://primes.utm.edu/lists/small/millions/primes1.zip",
            "cat ~/Documents/Goldbach-Daten/stichprobe.csv",
        }, texts
    page.close()


def test_armed_card_scrolled_away_must_be_seen_again(browser, calm_url) -> None:  # noqa: ANN001
    """Point 2: leaving the visible part of the column starts the 1.5 s again."""
    _dataset(browser, calm_url, "last")
    page = _page(browser, (1440, 760))
    _open(page, calm_url + GOLDBACH)
    card = page.locator(".rd-right .appr.compact").first.get_attribute("id")
    page.wait_for_timeout(ARM_WAIT_MS)  # armed now
    page.evaluate("document.querySelector('.rd-right').scrollTop = 400")
    page.wait_for_timeout(400)
    page.evaluate("document.querySelector('.rd-right').scrollTop = 0")
    _allow(page, card.removeprefix("cc-")).click()  # at once after coming back: locked
    page.wait_for_timeout(300)
    assert page.locator(f"#{card}.decided").count() == 0
    page.wait_for_timeout(ARM_WAIT_MS)
    _allow(page, card.removeprefix("cc-")).click()
    page.wait_for_timeout(300)
    assert page.locator(f"#{card}.decided").count() == 1
    page.close()


@pytest.mark.parametrize("pace", ["calm", "live"])
def test_decision_in_the_transcript_shows_a_confirmation(browser, request, pace) -> None:  # noqa: ANN001
    """Point 3 (M1): the waiting tool call keeps a confirmation of the same height for 1.5 s,
    then the focus is on the tool call row (not on body) and stays there when the finished
    tool call redraws the row (W1-07.3). `calm`: the tool call ends mostly after the
    confirmation; `live`: mostly during it."""
    url = request.getfixturevalue(f"{pace}_url")
    _dataset(browser, url, "szenario")
    page = _page(browser, (1280, 680))  # no overview: the card in the transcript decides
    _open(page, url + EXPLORE + "?agent=explorer-1")
    card = page.locator("#tc-apr_0042")
    card.scroll_into_view_if_needed()
    page.wait_for_timeout(ARM_WAIT_MS)
    height = card.bounding_box()["height"]
    page.locator("#tc-apr_0042 [data-grant]").click()
    page.wait_for_selector("#tc-apr_0042 .appr-done", state="visible", timeout=2000)
    decided_at = time.monotonic()
    assert abs(card.bounding_box()["height"] - height) <= 2
    page.wait_for_timeout(1000)  # 1.0 s later the confirmation still stands (it lasts 1.5 s)
    assert page.locator("#tc-apr_0042 .appr-done").is_visible()
    page.wait_for_function("!document.getElementById('tc-apr_0042')", timeout=3000)
    assert time.monotonic() - decided_at >= 1.4  # not removed early by the live event
    on_row = (
        "(() => { const a = document.activeElement; return a.tagName === 'SUMMARY' && "
        "a.closest('[id^=seq-]')?.id === 'seq-214'; })()"
    )
    page.wait_for_function(on_row, timeout=3000)
    # the tool call runs and ends; its row is redrawn – the focus must stay on it
    page.wait_for_function(
        "document.querySelector('#seq-214 .res')?.textContent.trim().startsWith('✓')",
        timeout=30000,
    )
    page.wait_for_timeout(300)
    assert page.evaluate(on_row), page.evaluate(
        "({tag: document.activeElement.tagName, row: "
        "document.activeElement.closest('[id^=seq-]')?.id})"
    )
    page.close()


def test_reading_mark_moves_only_as_far_as_seen(browser, live_url) -> None:  # noqa: ANN001
    """Point 4 (M2): messages that arrive while you read above them stay „neu“."""
    page = _page(browser)
    _open(page, live_url + EXPLORE)
    page.wait_for_timeout(800)
    page.locator(".rd-view .rd-scroll").hover()
    page.mouse.wheel(0, -4000)  # read further up
    page.wait_for_timeout(400)
    start = _scroll_state(page)
    _wait_new_rows(page, start["rows"])  # a new message arrives below, unseen
    run = EXPLORE.rsplit("/", 1)[1]
    seen = page.evaluate(f"JSON.parse(localStorage.getItem('arr.seen.{run}')).channels.main")
    newest = page.evaluate(
        "Math.max(...[...document.querySelectorAll('.rd-list [id^=m-main-]')]"
        ".map((m) => +m.id.split('-').pop()))"
    )
    assert seen < newest, (seen, newest)
    page.reload()
    page.wait_for_selector("#rd-new", timeout=10000)
    page.wait_for_timeout(500)
    first_new = page.evaluate(
        "+document.querySelector('.sep-row').nextElementSibling.querySelector('[id^=m-main-]')"
        ".id.split('-').pop()"
    )
    assert first_new <= newest and first_new == seen + 1, (first_new, seen, newest)
    page.close()


def test_focus_after_changing_the_view(browser, calm_url) -> None:  # noqa: ANN001
    """Point 5: tab by keyboard → focus on the new tab; „Im Transkript zeigen“ → the entry."""
    _dataset(browser, calm_url, "szenario")
    page = _page(browser)
    _open(page, calm_url + EXPLORE)
    page.locator('a[data-sel="ansicht:ergebnisse"]').focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "document.activeElement?.matches('.tabs a.active[data-sel=\"ansicht:ergebnisse\"]')",
        timeout=3000,
    )
    _open(page, calm_url + LEMMA)
    page.locator('#e004 a:has-text("Im Transkript zeigen")').focus()
    page.keyboard.press("Enter")
    page.wait_for_function("document.activeElement?.closest('#seq-301') !== null", timeout=3000)
    page.close()


@pytest.mark.parametrize("dataset", ["szenario", "last"])
def test_agent_cards_fit_their_column_at_1280(browser, calm_url, dataset) -> None:  # noqa: ANN001
    """Point 8: the badge „wartet auf dich“ stays inside the card at 1280 × 680."""
    _dataset(browser, calm_url, dataset)
    page = _page(browser, (1280, 680))
    _open(page, calm_url + (EXPLORE if dataset == "szenario" else SATZ2))
    over = page.evaluate(
        "[...document.querySelectorAll('.agents .agent')].filter((a) => a.scrollWidth > "
        "a.clientWidth || [...a.querySelectorAll('.badge')].some((b) => "
        "b.getBoundingClientRect().right > a.getBoundingClientRect().right))"
        ".map((a) => a.dataset.sel)"
    )
    assert over == [], over
    page.close()


# --- W1-07.2 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("dataset", ["szenario", "last"])
def test_agent_ids_are_never_shortened(browser, calm_url, dataset, size) -> None:  # noqa: ANN001
    """The ID of every agent card is shown whole; the badge stays inside the card (it may
    move to the next line instead)."""
    _dataset(browser, calm_url, dataset)
    page = _page(browser, size)
    paths = [EXPLORE] if dataset == "szenario" else [SATZ2, GOLDBACH]
    for path in paths:
        _open(page, calm_url + path)
        page.wait_for_function(
            "['lvl-full', 'lvl-2', 'lvl-1'].some((l) => "
            "document.querySelector('.rd-left').classList.contains(l))"
        )
        cards = page.evaluate(
            "[...document.querySelectorAll('.agents .agent')].map((a) => { "
            "const id = a.querySelector('.id'); const r = a.getBoundingClientRect(); "
            "return {sel: a.dataset.sel, text: id.textContent, sw: id.scrollWidth, "
            "cw: id.clientWidth, idRight: id.getBoundingClientRect().right, right: r.right, "
            "badges: [...a.querySelectorAll('.badge, .q')].filter((b) => b.offsetParent)"
            ".map((b) => b.getBoundingClientRect().right)}; })"
        )
        assert cards, path
        for card in cards:
            assert card["sel"] == f"agent:{card['text']}", card  # the whole ID, no „…“
            assert not card["text"].endswith("…") and card["sw"] <= card["cw"], card
            assert card["idRight"] <= card["right"] + 0.5, card
            assert all(b <= card["right"] + 0.5 for b in card["badges"]), card
    page.close()

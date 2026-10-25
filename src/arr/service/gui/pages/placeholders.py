"""Pages of the navigation that follow in W1-08 (docs/08 §4.1, §5).

Each shows an empty state with the way to the screens that already exist, so the navigation
is complete and no link ends in an error.
"""

from nicegui import ui

from arr.service.api_client import ApiClient
from arr.service.gui.app import get_api
from arr.service.gui.components.banner import banner_html, empty_state
from arr.service.gui.components.base import esc, raw
from arr.service.gui.layout import Frame

PAGES = {
    "/": ("start", "Start", "home"),
    "/neuer-run": ("neuer-run", "Neuer Run", "add"),
    "/projekte": ("projekte", "Projekte", "folder"),
    "/skills": ("skills", "Skills", "auto_awesome"),
    "/profile": ("profile", "Auth-Profile", "key"),
    "/usage": ("usage", "Usage", "functions"),
    "/speicher": ("speicher", "Speicher", "storage"),
    "/einstellungen": ("einstellungen", "Einstellungen", "settings"),
    "/einrichtung": ("einrichtung", "Einrichtung", "rocket_launch"),
}


def register() -> None:
    """Register the placeholder pages."""
    for path, (key, title, symbol) in PAGES.items():
        _register(path, key, title, symbol)
        if path not in ("/", "/neuer-run", "/einrichtung"):
            _register(path + "/{name}", key, title, symbol)


def _register(path: str, key: str, title: str, symbol: str) -> None:
    @ui.page(path)
    async def page(name: str | None = None) -> None:
        api = get_api()
        frame = Frame(api, active=key, title=title)
        await frame.start()
        counts = frame.counts
        with frame.content:
            raw(f'<div class="page-head"><h1 tabindex="-1">{esc(title)}</h1></div>')
            with ui.element("div").classes("placeholder"):
                waiting = (
                    f"Freigaben ({counts.waiting} warten auf dich)"
                    if counts.waiting
                    else "Freigaben"
                )
                empty_state(
                    symbol,
                    f"{title} kommt im nächsten Schritt",
                    "Diese Seite baut der Prototyp in Auftrag W1-08. Freigaben und Runs sind "
                    "schon bedienbar.",
                    [
                        (waiting, "/freigaben", True),
                        (f"Runs ({counts.active} aktiv)", "/runs", False),
                    ],
                )
            if key == "start":
                await _warnings(api)


async def _warnings(api: ApiClient) -> None:
    """Warnings with their action, target of the top bar chip (docs/08 §4.2; full start: W1-08)."""
    warnings = await api.list_warnings()
    with (
        ui.element("section")
        .classes("section placeholder")
        .style("margin-top:24px")
        .props('id="warnungen" aria-labelledby="h-warn"')
    ):
        raw(
            '<div class="section-title"><h2 id="h-warn" tabindex="-1">Warnungen</h2>'
            f'<span class="faint">{len(warnings)}</span></div>'
        )
        if not warnings:
            raw('<p class="muted">Keine Warnungen.</p>')
        for warning in warnings:
            kind = "err" if warning.severity.value == "error" else "warn"
            raw(
                banner_html(
                    kind,
                    warning.title,
                    f" {warning.text}",
                    [(warning.action.label, warning.action.url)],
                    small=True,
                ),
                classes="stack-item",
            )

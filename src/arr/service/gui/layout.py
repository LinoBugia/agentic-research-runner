"""Page frame: navigation, top bar with counters, toasts, connection banner (docs/08 §4.1, §4.2).

Every page builds a `Frame` first. The frame subscribes to the global live stream and keeps
badge, status chips and tab title on the same number (docs/08 §8.3); pages register for the
events they need with `on_event`. DOM order is top bar → navigation → content, so Tab follows
docs/08 §7.6; a skip link leads to the content.
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui

from arr.service.api_client import ApiClient
from arr.service.api_models import GlobalCounts, LiveEvent, WorkfolderInfo
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, icon, raw, text
from arr.service.gui.components.toast import Toasts

NAV = [
    ("start", "/", "home", "Start"),
    ("freigaben", "/freigaben", "front_hand", "Freigaben"),
    ("runs", "/runs", "view_list", "Runs"),
    ("projekte", "/projekte", "folder", "Projekte"),
    ("skills", "/skills", "auto_awesome", "Skills"),
    None,
    ("profile", "/profile", "key", "Auth-Profile"),
    ("usage", "/usage", "functions", "Usage"),
    ("speicher", "/speicher", "storage", "Speicher"),
    ("einstellungen", "/einstellungen", "settings", "Einstellungen"),
]

EventHandler = Callable[[LiveEvent], Any]


def tab_title(title: str, waiting: int) -> str:
    """Tab title „(3) Freigaben – arr“; without the number when nothing waits (§4.2)."""
    return f"({waiting}) {title} – arr" if waiting else f"{title} – arr"


def chips_html(counts: GlobalCounts) -> str:
    """Status chips of the top bar; hidden at 0 (§4.2). The second equals the badge (§8.3)."""
    out = []
    if counts.active:
        out.append(
            '<a class="badge st-run" href="/runs?status=aktiv" title="Aktive Runs: alle nicht '
            'abgeschlossenen (läuft, pausiert, wartet auf Freigabe …)" data-chip="active">'
            f"{icon('circle', small=True)}{counts.active} aktiv</a>"
        )
    if counts.waiting:
        word = "wartet" if counts.waiting == 1 else "warten"
        out.append(
            '<a class="badge st-wait" href="/freigaben" title="Offene Einträge der Freigabe-Inbox '
            '(gleiche Zahl wie in der Navigation)" data-chip="waiting">'
            f"{icon('front_hand', small=True)}{counts.waiting} {word} auf dich</a>"
        )
    if counts.warnings:
        word = "Warnung" if counts.warnings == 1 else "Warnungen"
        out.append(
            f'<a class="badge st-warn" href="/#warnungen" title="Warnungen" data-chip="warnings">'
            f"{icon('warning', small=True)}{counts.warnings} {word}</a>"
        )
    return "".join(out)


def nav_html(active: str, waiting: int) -> str:
    """Main navigation with the approval badge (§4.1)."""
    links = []
    for entry in NAV:
        if entry is None:
            links.append('<div class="nav-sep">Verwaltung</div>')
            continue
        key, href, ico, label = entry
        current = ' class="active" aria-current="page"' if key == active else ""
        count = ""
        if key == "freigaben" and waiting:
            count = (
                f'<span class="count" aria-label="{waiting} offene Freigaben" '
                f'data-badge="waiting">{waiting}</span>'
            )
        links.append(f'<a href="{href}"{current}>{icon(ico)}{esc(label)}{count}</a>')
    return f'<nav aria-label="Seiten">{"".join(links)}</nav>'


class Frame:
    """The frame of one page; `content` is the container for the page body."""

    def __init__(
        self,
        api: ApiClient,
        *,
        active: str,
        title: str,
        crumbs: list[tuple[str, str | None]] | None = None,
        focus_card: str | None = None,
        focus: str | None = None,
    ) -> None:
        self.api = api
        self.client = ui.context.client
        self.title = title
        self._handlers: list[EventHandler] = []
        self.counts = GlobalCounts(waiting=0, active=0, warnings=0)
        props = f'data-now="{api.now():.3f}"'
        if focus_card:
            props += f' data-focus-card="{esc(focus_card)}"'
        if focus:
            props += f' data-focus="{esc(focus)}"'
        self.root = ui.element("div").classes("app").props(props)
        with self.root:
            raw('<a class="skip" href="#inhalt">Zum Inhalt</a>', tag="span")
            with ui.element("header").classes("topbar"):
                crumb_html = []
                for i, (label, href) in enumerate(crumbs or [(title, None)]):
                    if i:
                        crumb_html.append('<span class="sep" aria-hidden="true">›</span>')
                    crumb_html.append(
                        f'<a href="{esc(href)}">{esc(label)}</a>'
                        if href
                        else f"<span>{esc(label)}</span>"
                    )
                raw("".join(crumb_html), classes="crumbs")
                self.chips = raw("", classes="global").props('aria-label="Lage"').mark("chips")
            with ui.element("aside").classes("nav").props('aria-label="Hauptnavigation"'):
                raw("arr <small>v1</small>", classes="brand")
                raw(
                    f'<a class="btn primary btn-block" href="/neuer-run">{icon("add")}'
                    "Neuer Run</a>",
                    tag="span",
                )
                self.nav = raw("", tag="div").mark("nav")
                self.nav_foot = ui.element("div").classes("nav-foot")
            self.main = ui.element("main").classes("main").props('id="inhalt"')
            with self.main:
                self.banner_slot = ui.element("div")
                self.content = ui.element("div").classes("page")
            self.toasts = Toasts()
            raw(
                '<div class="banner err conn-banner" role="alert">'
                f'{icon("cloud_off")}<div class="txt"><b>Verbindung zu arr verloren.</b>'
                "Die Anzeige ist seit <span data-since>–</span> nicht aktuell. Normalerweise "
                "startet arr von selbst neu. Sonst klicke im Dock auf arr."
                '<div class="btn-row" style="margin-top:8px">'
                '<button class="btn btn-sm" type="button" onclick="location.reload()">'
                "Jetzt verbinden</button></div></div></div>",
                tag="div",
            )
        self.active = active

    async def start(self) -> "Frame":
        """Load counters and workfolder, then follow the global live stream."""
        self.counts = await self.api.get_counts()
        workfolder = await self.api.get_workfolder()
        self._render_counts()
        self._render_foot(workfolder)
        subscription = self.api.subscribe()
        ui.context.client.on_delete(subscription.close)

        async def pump() -> None:
            for event in subscription.drain():
                if event.type == "counts.changed":
                    self.counts = event.counts  # type: ignore[union-attr]
                    self._render_counts()
                for handler in list(self._handlers):
                    result = handler(event)
                    if hasattr(result, "__await__"):
                        await result

        ui.timer(0.25, pump)
        return self

    def on_event(self, handler: EventHandler) -> None:
        """Call `handler` for every event of the global stream (after the counters update)."""
        self._handlers.append(handler)

    def js(self, code: str) -> None:
        """Run JavaScript in this page, also from handlers whose element was just deleted."""
        self.client.run_javascript(code)

    def toast(
        self, message: str, link: tuple[str, str] | None = None, *, error: bool = False
    ) -> None:
        """Show a toast (K12)."""
        self.toasts.show(message, link, error=error)

    def _render_counts(self) -> None:
        self.chips.set_content(chips_html(self.counts))
        self.nav.set_content(nav_html(self.active, self.counts.waiting))
        ui.page_title(tab_title(self.title, self.counts.waiting))

    def _render_foot(self, workfolder: WorkfolderInfo) -> None:
        host = workfolder.service_url.removeprefix("http://").removeprefix("https://")
        with self.nav_foot:
            raw(
                '<a class="wf-switch" href="/#workfolder" title="Workfolder wechseln">'
                f'<small class="faint">Workfolder</small><b>{esc(workfolder.name)} ▾</b>'
                f'<span class="mono small faint">{esc(workfolder.path)}</span></a>',
                tag="span",
            )
            if hasattr(self.api, "datasets"):  # only the mock has data sets
                self._dataset_switch(self.api)
            raw(
                f'<span title="Die Oberfläche ist mit dem lokalen arr-Service verbunden">'
                f'<span class="conn-ok" aria-hidden="true">●</span> verbunden · {esc(host)}</span>',
                tag="span",
            )

    def _dataset_switch(self, api: Any) -> None:
        """Mock only: switch between the data sets of docs/08 §9.2 (`api.datasets()`)."""
        with ui.element("div").classes("ds-switch"):
            raw('<label class="faint" for="ds-select">Beispieldaten</label>', tag="span")
            select = ui.element("select").props('id="ds-select"').mark("dataset")
            with select:
                for value, label in api.datasets():
                    text("option", label).props(
                        f'value="{esc(value)}"' + (" selected" if value == api.dataset else "")
                    )

            def switch(event: Any) -> None:
                api.switch_dataset(str(event.args))
                ui.navigate.reload()

            select.on("change", switch, js_handler="(e) => emit(e.target.value)")


def now_text(api: ApiClient) -> str:
    """Current time of the service as „15:24“ (for page heads)."""
    return fmt.clock(api.now())

"""Runs: find and compare all runs (docs/08 §5.3).

Table grouped „Aktiv · n“ (always on top) and „Beendet“; status segment with counters after
docs/08 §8.3, project and starter filter, search over name and ID. A row opens the run detail.
Live: duration and cost of active runs update in place, status changes re-render the table.
URL parameters keep the state: `?status=aktiv&projekt=collatz&von=claude-code&suche=…&n=100`.
"""

from urllib.parse import urlencode

from nicegui import ui

from arr.permissions.types import ApprovalStatus
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    LiveEvent,
    RunFilter,
    RunsPage,
    RunStatusEvent,
    RunSummary,
    UsageTickEvent,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import run_badges_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, raw
from arr.service.gui.components.page_state import forced_state, state_api
from arr.service.gui.components.table import Column, Group, Row, table_html
from arr.service.gui.layout import Frame
from arr.service.gui.texts import (
    RUN_FILTER_LABELS,
    RUN_FILTER_SLUGS,
    STARTER,
    STARTER_PHRASE,
    STARTER_SLUGS,
    TOPOLOGY,
)

PAGE_SIZE = 50

COLUMNS = [
    Column("Status", css="status"),
    Column("Name", css="title"),
    Column("Projekt"),
    Column("Skill · Topologie"),
    Column("Gestartet von", css="nw col-starter"),
    Column("Gestartet", css="nw"),
    Column("Dauer", align="right"),
    Column("Tokens · Kosten", align="right", title="≈ = rechnerischer API-Preis bei Abo-Profilen"),
    Column("Größe", align="right"),
    Column("📌", title="Angeheftet", sr_label="Angeheftet"),
]


def _cost_text(run: RunSummary) -> str:
    if run.tokens_total is None and run.cost_usd is None:
        return "–"
    return f"{fmt.tokens(run.tokens_total)} · {fmt.money(run.cost_usd, run.cost_kind)}"


def _cost_html(run: RunSummary) -> str:
    """Tokens and cost; may break after „·“ in narrow windows."""
    if run.tokens_total is None and run.cost_usd is None:
        return "–"
    money = fmt.money(run.cost_usd, run.cost_kind)
    tokens = fmt.tokens(run.tokens_total)
    return f'<span class="nowrap">{tokens} ·</span> <span class="nowrap">{esc(money)}</span>'


def _size_text(run: RunSummary) -> str:
    text = fmt.size(run.size.actual_bytes)
    return (
        f'{text}<span class="faint" style="display:block">(Archiv)</span>' if run.archived else text
    )


def run_row(run: RunSummary, now: float, approval_href: str | None) -> Row:
    """One table row; live cells (`dur-…`, `cost-…`) can be patched by id."""
    tags = ""
    if run.slimmed:
        tags += ' <span class="tag">verschlankt</span>'
    if run.archived:
        tags += ' <span class="tag">archiviert</span>'
    info = run.info or (run.error.message.split(". ")[0] if run.error else "")
    name = (
        f'<a href="/runs/{esc(run.id)}">{esc(run.name)}</a>'
        f'<span class="sub">{esc(run.id)}</span>'
        + (f'<span class="info">{esc(info)}</span>' if info else "")
    )
    topology = TOPOLOGY[run.topology]
    same = run.skill == topology
    skill = esc(topology) if same else f"{esc(run.skill or 'frei')} · {topology}"
    duration = fmt.duration_hm(run.duration_s) if run.started_at else "–"
    return Row(
        cells=[
            run_badges_html(run, approval_href) + tags,
            name,
            esc(run.project),
            skill,
            esc(STARTER[run.starter]),
            f'<span class="num">{esc(fmt.when(run.created_at, now))}</span>'
            f'<span class="narrow-only info">{esc(STARTER_PHRASE[run.starter])}</span>',
            f'<span class="num nowrap" id="dur-{esc(run.id)}">{duration}</span>',
            f'<span class="num" id="cost-{esc(run.id)}">{_cost_html(run)}</span>',
            f'<span class="num nowrap">{_size_text(run)}</span>',
            '<span title="angeheftet">📌</span><span class="sr-only">angeheftet</span>'
            if run.pinned
            else "",
        ],
        href=f"/runs/{run.id}",
        row_id=f"row-{run.id}",
    )


class RunsPageView:
    """State of one runs page (one browser tab)."""

    def __init__(
        self, frame: Frame, *, status: str, project: str, starter: str, query: str, count: int
    ) -> None:
        self.frame = frame
        self.api: ApiClient = frame.api
        self.status = status if status in RUN_FILTER_SLUGS else "alle"
        self.project = project
        self.starter = starter if starter in STARTER_SLUGS else ""
        self.query = query.strip()
        self.count = max(PAGE_SIZE, min(count, 1000))
        self.page: RunsPage | None = None
        self.shown: dict[str, RunSummary] = {}

    async def build(self) -> None:
        workfolder = await self.api.get_workfolder()
        with self.frame.content:
            with ui.element("div").classes("page-head"):
                raw('<h1 tabindex="-1">Runs</h1>', tag="div")
                self.subtitle = raw("", tag="span", classes="faint")
                raw(
                    '<a class="btn primary" href="/neuer-run">'
                    '<span class="ico" aria-hidden="true">add</span>Neuer Run</a>',
                    tag="div",
                    classes="actions",
                )
            self.filters = (
                raw("", classes="row runs-filters").style("margin-bottom:12px").mark("filters")
            )
            self.table = raw("", tag="div").mark("runs-table")
            with ui.element("div").classes("tbl-foot"):
                self.foot = raw("", tag="span")
                self.more = button("", self._load_more, classes="btn btn-sm").mark("more")
                raw('<span class="spacer"></span>', tag="span")
                raw(
                    "≈ = rechnerischer API-Preis bei Abo-Profilen, nicht abgerechnet.",
                    tag="span",
                )
        self.workfolder = workfolder.name
        await self.render()
        self.frame.on_event(self._on_event)

    def _url(self, **changes: str | int) -> str:
        params = {
            "status": self.status if self.status != "alle" else "",
            "projekt": self.project,
            "von": self.starter,
            "suche": self.query,
        }
        params.update({k: str(v) for k, v in changes.items()})
        query = urlencode({k: v for k, v in params.items() if v})
        return esc("/runs" + (f"?{query}" if query else ""))

    async def render(self) -> None:
        api = self.api
        page = await api.list_runs(
            status=RunFilter(RUN_FILTER_SLUGS[self.status]),
            project=self.project or None,
            starter=STARTER_SLUGS.get(self.starter),
            q=self.query or None,
            limit=self.count,
        )
        self.page = page
        try:
            approvals = await api.list_approvals(status=[ApprovalStatus.OPEN])
        except ApiException:
            approvals = []  # no inbox yet (W3-03): rows link to no approval
        first_open: dict[str, str] = {}
        for item in approvals:
            first_open.setdefault(item.run.id, item.id)
        now = api.now()
        self.shown = {r.id: r for r in page.items}
        self.subtitle.set_content(f"{page.counts.all} in „{esc(self.workfolder)}“")
        self.filters.set_content(self._filters_html(page))
        active = [r for r in page.items if r.active]
        ended = [r for r in page.items if not r.active]

        def rows(runs: list[RunSummary]) -> list[Row]:
            return [
                run_row(r, now, f"/freigaben/{first_open[r.id]}" if r.id in first_open else None)
                for r in runs
            ]

        groups = []
        if active:
            groups.append(Group(f"Aktiv · {len(active)}", rows(active)))
        if ended:
            groups.append(
                Group("Beendet" if active or self.status == "alle" else None, rows(ended))
            )
        if not page.items:
            self.table.set_content(self._empty_html())
            self.foot.set_content("")
            self.more.style("display:none")
            return
        self.table.set_content(table_html(COLUMNS, groups))
        self.foot.set_content(f"{len(page.items)} von {page.total} Runs")
        self.more.clear()
        if page.next_cursor is not None:
            rest = min(page.total - len(page.items), PAGE_SIZE)
            with self.more:
                raw(f"Weitere {rest} laden", tag="span")
            self.more.style(remove="display:none")
        else:
            self.more.style("display:none")

    async def _load_more(self) -> None:
        """Append the next page in place; focus goes to the first new run (no reload, no jump)."""
        assert self.page is not None
        first_new = len(self.page.items)
        self.count += PAGE_SIZE
        await self.render()
        if first_new < len(self.page.items):
            run_id = self.page.items[first_new].id
            url = self._url(n=self.count).replace("&amp;", "&")
            self.frame.js(
                f"arr.focusRowLink('row-{run_id}'); history.replaceState(null, '', {url!r})"
            )

    def _filters_html(self, page: RunsPage) -> str:
        counts = page.counts
        numbers = {
            "alle": counts.all,
            "aktiv": counts.active,
            "wartet": counts.waiting,
            "fertig": counts.done,
            "fehler": counts.error,
            "archiviert": counts.archived,
        }
        seg = []
        for slug, label in RUN_FILTER_LABELS.items():
            on = slug == self.status
            n = numbers[slug]
            seg.append(
                f'<a class="{"on" if on else ""}" '
                f'href="{self._url(status=slug if slug != "alle" else "")}"'
                + (' aria-current="true"' if on else "")
                + f'>{esc(label)} <span class="n">{n}</span></a>'
            )
        projects = "".join(
            f'<option value="{esc(p)}"{" selected" if p == self.project else ""}>{esc(p)}</option>'
            for p in page.projects
        )
        starters = "".join(
            f'<option value="{slug}"{" selected" if slug == self.starter else ""}>'
            f"{esc(STARTER[starter])}</option>"
            for slug, starter in STARTER_SLUGS.items()
        )
        return (
            '<form data-param="suche" role="search" style="display:inline-flex">'
            f'<input type="search" name="suche" value="{esc(self.query)}" '
            'placeholder="Name oder Run-ID" aria-label="Runs durchsuchen (Name oder Run-ID)">'
            "</form>"
            f'<div class="seg" role="group" aria-label="Status">{"".join(seg)}</div>'
            '<select data-param="projekt" aria-label="Projekt">'
            f'<option value="">Alle Projekte</option>{projects}</select>'
            '<select data-param="von" aria-label="Gestartet von">'
            f'<option value="">Gestartet von: alle</option>{starters}</select>'
        )

    def _empty_html(self) -> str:
        filtered = self.status != "alle" or self.project or self.starter or self.query
        if not filtered:
            return empty_state_html(
                "view_list",
                f"Noch keine Runs in „{self.workfolder}“",
                "Starte einen Run hier oder sag in Claude Code zum Beispiel: „Lass das von einer "
                "Research-Group prüfen.“",
                [("Neuer Run", "/neuer-run", True)],
            )
        what = RUN_FILTER_LABELS[self.status] if self.status != "alle" else "diese Suche"
        where = f" in {self.project}" if self.project else ""
        return empty_state_html(
            "filter_alt_off",
            f"Keine Runs für „{what}“{where}",
            "Andere Filter zeigen mehr.",
            [("Filter zurücksetzen", "/runs", False)],
        )

    async def _on_event(self, event: LiveEvent) -> None:
        if isinstance(event, UsageTickEvent) and event.run_id in self.shown:
            run = self.shown[event.run_id]
            run = run.model_copy(
                update={
                    "duration_s": event.elapsed_s,
                    "cost_usd": round(event.spent_usd, 2) if event.spent_usd is not None else None,
                    "tokens_total": event.tokens_total,
                }
            )
            self.shown[run.id] = run
            self.frame.js(
                f"arr.setText('dur-{run.id}', {fmt.duration_hm(run.duration_s)!r});"
                f"arr.setHtml('cost-{run.id}', {_cost_html(run)!r})"
            )
        elif isinstance(event, RunStatusEvent):
            old = self.shown.get(event.run.id)
            if (
                old is None
                or old.status != event.run.status
                or old.open_approvals != (event.run.open_approvals)
                or old.slimmed != event.run.slimmed
            ):
                await self.render()


def register() -> None:
    """Register `/runs`."""

    @ui.page("/runs")
    async def runs(
        status: str = "alle",
        projekt: str = "",
        von: str = "",
        suche: str = "",
        n: int = PAGE_SIZE,
        zustand: str = "",
    ) -> None:
        api = state_api(get_api(), zustand)
        frame = Frame(api, active="runs", title="Runs")
        await frame.start()
        if forced_state(frame, zustand, "runs", title="Runs", path="/runs"):
            return
        view = RunsPageView(
            frame, status=status, project=projekt, starter=von, query=suche, count=n
        )
        await view.build()

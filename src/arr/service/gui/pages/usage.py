"""Usage: understand and compare consumption – tokens, cost, limits (docs/08 §5.9).

Key figures (tokens with the cache share, billed cost of API keys and the estimated cost of
subscriptions kept apart, rate-limit events), a breakdown by profile, model, project or run
with input, output, cache and reasoning („–“: the SDK does not deliver the value) and the
rate-limit events. Period and grouping are URL parameters: `?zeitraum=heute|7tage|monat`,
`?nach=profil|modell|projekt|run`, `?projekt=…`.
"""

from datetime import datetime, timedelta
from urllib.parse import urlencode

from nicegui import ui

from arr.backends.base import CostKind
from arr.models import AuthKind
from arr.service.api_client import ApiClient
from arr.service.api_models import ProfileInfo, UsageGroupBy, UsageReport, UsageRow
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import esc, raw
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.layout import Frame
from arr.service.gui.texts import Status

PERIODS = {"heute": "Heute", "7tage": "7 Tage", "monat": "Monat"}
GROUPS = {
    "profil": ("Profil", UsageGroupBy.PROFILE),
    "modell": ("Modell", UsageGroupBy.MODEL),
    "projekt": ("Projekt", UsageGroupBy.PROJECT),
    "run": ("Run", UsageGroupBy.RUN),
}
KIND_SHORT = {
    AuthKind.CLAUDE_OAUTH: "Claude-Abo",
    AuthKind.CLAUDE_API_KEY: "API-Key",
    AuthKind.CODEX_CHATGPT: "ChatGPT-Abo",
    AuthKind.OPENAI_API_KEY: "API-Key",
}
RATE_STATUS = {
    "warning": Status("st-warn", "warning", "Warnung"),
    "allowed_warning": Status("st-warn", "warning", "Warnung"),
    "error": Status("st-err", "close", "Fehler"),
    "throttled": Status("st-block", "hourglass_top", "gedrosselt"),
    "rejected": Status("st-block", "hourglass_top", "gedrosselt"),
}
MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip


def all_tokens(row: UsageRow) -> int:
    """Every token of a row (input, output, cache, reasoning)."""
    return sum(
        v or 0 for v in (row.input, row.output, row.cache_read, row.cache_write, row.reasoning)
    )


def cost_html(row: UsageRow) -> str:
    """„142,80 $ rechnerisch“, „18,40 $“ (billed, bold) or „– Abo“ (Codex, no price)."""
    if row.cost_usd is None:
        return '<span class="faint" title="Codex liefert keinen Preis">– <small>Abo</small></span>'
    money = fmt.money(row.cost_usd)
    if row.cost_kind is CostKind.ESTIMATED:
        return f'≈ {esc(money)} <span class="faint small">rechnerisch</span>'
    return f"<b>{esc(money)}</b>"


def number(value: int | None) -> str:
    return '<span class="faint">–</span>' if value is None else esc(fmt.tokens(value))


class UsagePage:
    """State of one Usage page (one browser tab)."""

    def __init__(
        self, frame: Frame, api: ApiClient, *, period: str, group: str, project: str
    ) -> None:
        self.frame = frame
        self.api = api
        self.period = period if period in PERIODS else "monat"
        self.group = group if group in GROUPS else "profil"
        self.project = project
        self.profiles: dict[str, ProfileInfo] = {}
        self.run_names: dict[str, str] = {}

    def _href(self, **changes: str) -> str:
        params = {"zeitraum": self.period, "nach": self.group, "projekt": self.project}
        params.update(changes)
        defaults = {"zeitraum": "monat", "nach": "profil"}
        query = urlencode({k: v for k, v in params.items() if v and defaults.get(k) != v})
        return "/usage" + (f"?{query}" if query else "")

    def _range(self) -> tuple[float, float, str]:
        now = datetime.fromtimestamp(self.api.now())
        if self.period == "heute":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            label = "heute"
        elif self.period == "7tage":
            start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
            label = "in den letzten 7 Tagen"
        else:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            label = f"im {MONTHS[now.month - 1]} {now.year}"
        return start.timestamp(), now.timestamp(), label

    async def build(self) -> None:
        _, _, label = self._range()
        month = label.removeprefix("im ")
        with self.frame.content:
            with ui.element("div").classes("page-head usage-head"):
                raw('<h1 tabindex="-1">Usage</h1>', tag="div")
                seg = "".join(
                    f'<a class="{"on" if key == self.period else ""}" '
                    f'href="{esc(self._href(zeitraum=key))}"'
                    + (' aria-current="true"' if key == self.period else "")
                    + f">{esc(month if key == 'monat' else text)}</a>"
                    for key, text in PERIODS.items()
                )
                self.project_select = raw("", tag="span")
                raw(
                    f'<div class="seg" role="group" aria-label="Zeitraum">{seg}</div>',
                    tag="div",
                    classes="actions",
                )
            raw(
                '<p class="small muted" style="margin-top:-8px">Tagesbalken und CSV-Export kommen '
                "in einer späteren Version.</p>"
            )
            self.box = ui.element("div")
        await load_into(
            self.box,
            self._load,
            self._render,
            retry_href=clean_href(self._href()),
            skeleton=skeleton_html(4, shape="kpi", reason="Der Usage-Index wird gelesen …")
            + skeleton_html(5, shape="rows"),
        )

    async def _load(self) -> tuple[UsageReport, list[ProfileInfo], list[str]]:
        start, end, _ = self._range()
        report = await self.api.get_usage(
            from_ts=start,
            to_ts=end,
            group_by=GROUPS[self.group][1],
            project=self.project or None,
        )
        profiles = await self.api.list_profiles()
        projects = [p.name for p in await self.api.list_projects()]
        ids = {e.run for e in report.ratelimits if e.run}
        if ids:
            page = await self.api.list_runs(limit=1000)
            self.run_names = {r.id: r.name for r in page.items if r.id in ids}
        return report, profiles, projects

    def _render(self, data: tuple[UsageReport, list[ProfileInfo], list[str]]) -> None:
        report, profiles, projects = data
        self.profiles = {p.name: p for p in profiles}
        options = "".join(
            f'<option value="{esc(p)}"{" selected" if p == self.project else ""}>{esc(p)}</option>'
            for p in projects
        )
        self.project_select.set_content(
            '<select data-param="projekt" aria-label="Projekt" style="width:180px">'
            f'<option value="">Alle Projekte</option>{options}</select>'
        )
        _, _, label = self._range()
        if not report.rows:
            raw(
                empty_state_html(
                    "functions",
                    f"Noch kein Verbrauch {label}",
                    "Sobald ein Run läuft, erscheinen hier Tokens und Kosten.",
                )
            )
            return
        self._kpis(report)
        self._breakdown(report)
        self._ratelimits(report)

    def _kpis(self, report: UsageReport) -> None:
        total = all_tokens(report.totals)
        cache = (
            f"davon {fmt.percent(report.cache_read_ratio)} aus dem Cache gelesen"
            if report.cache_read_ratio is not None
            else "Cache-Anteil unbekannt"
        )
        billed_profiles = [
            p.name
            for p in self.profiles.values()
            if p.kind in (AuthKind.CLAUDE_API_KEY, AuthKind.OPENAI_API_KEY)
        ]
        sub_profiles = [p.name for p in self.profiles.values() if p.kind is AuthKind.CLAUDE_OAUTH]
        billed = fmt.money(report.billed_usd or 0)
        estimated = fmt.money(report.estimated_usd or 0)
        events = len(report.ratelimits)
        tiles = [
            ("Tokens gesamt", fmt.tokens(total), cache),
            (
                "Abgerechnet (API-Keys)",
                billed,
                ", ".join(billed_profiles) or "über deine API-Keys",
            ),
            (
                "Rechnerisch (Abos)",
                f"≈ {estimated}",
                f"{', '.join(sub_profiles) or 'Abos'} · nicht abgerechnet, über dein Abo abgedeckt",
            ),
            (
                "Rate-Limit-Ereignisse",
                str(events),
                '<a href="#ratelimits">ansehen</a>' if events else "keine",
            ),
        ]
        cells = "".join(
            f'<div class="kpi"><div class="l">{esc(label)}</div><div class="v">{esc(value)}</div>'
            f'<div class="small muted">{note if "<a" in note else esc(note)}</div></div>'
            for label, value, note in tiles
        )
        raw(f'<div class="grid-4" style="margin-bottom:16px">{cells}</div>')

    def _breakdown(self, report: UsageReport) -> None:
        seg = "".join(
            f'<a class="{"on" if key == self.group else ""}" href="{esc(self._href(nach=key))}"'
            + (' aria-current="true"' if key == self.group else "")
            + f">nach {esc(text)}</a>"
            for key, (text, _) in GROUPS.items()
        )
        head = GROUPS[self.group][0]
        rows = []
        for row in report.rows:
            label = esc(row.label)
            if self.group == "profil":
                profile = self.profiles.get(row.key)
                kind = KIND_SHORT.get(profile.kind, "") if profile else ""
                label = (
                    f'<a class="mono" href="/profile/{esc(row.key)}"><b>{label}</b></a> '
                    f'<span class="faint small">{esc(kind)}</span>'
                )
            elif self.group == "run":
                label = f'<a href="/runs/{esc(row.key)}">{label}</a>'
            elif self.group == "projekt":
                label = f'<a href="/projekte/{esc(row.key)}">{label}</a>'
            rows.append(
                f"<tr><td>{label}</td><td class='num'>{number(row.input)}</td>"
                f"<td class='num'>{number(row.output)}</td>"
                f"<td class='num'>{number(row.cache_read)}</td>"
                f"<td class='num'>{number(row.cache_write)}</td>"
                f"<td class='num'>{number(row.reasoning)}</td>"
                f"<td class='num'>{cost_html(row)}</td><td class='num'>{row.runs}</td></tr>"
            )
        raw(
            '<section class="card" aria-labelledby="h-break"><div class="card-head">'
            '<h2 id="h-break">Aufschlüsselung</h2>'
            f'<div class="seg" role="group" aria-label="Gruppierung">{seg}</div></div>'
            f'<table class="tbl"><thead><tr><th>{esc(head)}</th><th class="num">Input</th>'
            '<th class="num">Output</th><th class="num">Cache lesen</th>'
            '<th class="num">Cache schreiben</th><th class="num">Reasoning</th>'
            '<th class="num">Kosten</th><th class="num">Runs</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
            '<p class="small muted" style="margin:6px 0 0">„–“ heißt: Das SDK liefert diesen '
            "Wert nicht. Die rechnerischen Kosten (≈) bei Abos sind API-Listenpreise, damit sich "
            "Runs vergleichen lassen. Sie zählen gegen das Budget eines Runs.</p></section>"
        )

    def _ratelimits(self, report: UsageReport) -> None:
        now = self.api.now()
        if not report.ratelimits:
            body = '<p class="small muted">Keine Rate-Limit-Ereignisse in diesem Zeitraum.</p>'
        else:
            rows = []
            for e in report.ratelimits:
                status = RATE_STATUS.get(e.status, Status("st-neutral", "info", e.status))
                name = self.run_names.get(e.run or "", e.run or "")
                run = f'<a href="/runs/{esc(e.run)}">{esc(name)}</a>' if e.run else "–"
                load = fmt.percent(e.utilization) if e.utilization is not None else "–"
                who = f" · {esc(e.agent)}" if e.agent else ""
                rows.append(
                    f"<tr><td class='num'>{esc(fmt.when(e.ts, now))}</td>"
                    f"<td class='mono'>{esc(e.profile)}</td><td>{badge_html(status)}</td>"
                    f"<td class='num'>{esc(load)}</td>"
                    f"<td>{run}{who}</td><td>{esc(e.consequence)}</td></tr>"
                )
            body = (
                '<table class="tbl compact"><thead><tr><th>Zeit</th><th>Profil</th><th>Status</th>'
                '<th class="num">Auslastung</th><th>Run · Agent</th><th>Folge</th></tr></thead>'
                f"<tbody>{''.join(rows)}</tbody></table>"
            )
        raw(
            '<section class="card" id="ratelimits" aria-labelledby="h-rate">'
            f'<h2 id="h-rate" tabindex="-1">Rate-Limit-Ereignisse</h2>{body}</section>'
        )


def register() -> None:
    """Register `/usage` (docs/08 §5.9)."""

    @ui.page("/usage")
    async def usage(
        zeitraum: str = "", nach: str = "", projekt: str = "", zustand: str = ""
    ) -> None:
        api = state_api(get_api(), zustand)
        use_screen_assets()
        frame = Frame(api, active="usage", title="Usage")
        await frame.start()
        await UsagePage(frame, api, period=zeitraum, group=nach, project=projekt).build()

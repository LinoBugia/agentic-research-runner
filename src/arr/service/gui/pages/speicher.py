"""Speicher: see what takes the space and clean up safely (docs/08 §5.10).

Use against the limit with the parts working copies / protocols / output / archive, actual and
logical size, free disk space and a table per project; the suggestion with what stays, what
goes and what is excluded; runs by size with one action per row (slim, archive, move to the
trash, pin). Every destructive action names what is freed and what stays in a dialog first
(G6, §8.6); active and pinned runs cannot be cleaned up (reason in the tooltip). Path 3 clicks
from the warning on Start (T12). `?filter=verschlankbar|archiviert` filters the list [S].
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui

from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    RunAction,
    RunSummary,
    StorageItem,
    StorageParts,
    StorageReport,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html, run_badges_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, raw
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.layout import Frame
from arr.service.gui.texts import Status

FILTERS = {"": "Alle", "verschlankbar": "Verschlankbar", "archiviert": "Archiviert"}
SHOWN = 25
PART_LABELS = [
    ("work", "sz-work", "Arbeitskopien"),
    ("logs", "sz-logs", "Protokolle"),
    ("output", "sz-out", "Output"),
    ("archive", "sz-arch", "Archiv"),
]
PROTECTED_TIP = {
    "aktiv": "Aktive Runs lassen sich nicht aufräumen. Stoppe den Run zuerst.",
    "angeheftet": "Angeheftete Runs sind vor Aufräum-Aktionen geschützt. Löse die Nadel zuerst.",
}


def sizebar_html(parts: StorageParts, total: int, *, label: bool = True) -> str:
    """Bar of the parts; the sizes are also text (aria-label, legend), never colour alone."""
    if total <= 0:
        return '<div class="sizebar" aria-label="leer"></div>'
    cells = "".join(
        f'<i class="{css}" style="width:{100 * getattr(parts, key) / total:.1f}%"></i>'
        for key, css, _ in PART_LABELS
        if getattr(parts, key)
    )
    text = ", ".join(
        f"{name} {fmt.size(getattr(parts, key))}"
        for key, _, name in PART_LABELS
        if getattr(parts, key)
    )
    aria = f' role="img" aria-label="Aufteilung: {esc(text)}"' if label else f' title="{esc(text)}"'
    return f'<div class="sizebar"{aria}>{cells}</div>'


class SpeicherPage:
    """State of one Speicher page (one browser tab)."""

    def __init__(self, frame: Frame, api: ApiClient, *, filter_: str, all_rows: bool) -> None:
        self.frame = frame
        self.api = api
        self.filter = filter_ if filter_ in FILTERS else ""
        self.all_rows = all_rows
        self.report: StorageReport | None = None
        self.runs: dict[str, RunSummary] = {}
        self.busy = False
        self.runs_count = 0

    def _href(self, **changes: str) -> str:
        params = {"filter": self.filter, **changes}
        return clean_href("/speicher", **params)

    async def build(self) -> None:
        workfolder = await self.api.get_workfolder()
        self.runs_count = workfolder.runs_count
        with self.frame.content:
            self.dialog = ConfirmDialog()
            with ui.element("div").classes("page-head"):
                raw(
                    f'<h1 tabindex="-1">Speicher</h1><span class="faint">Workfolder '
                    f'„{esc(workfolder.name)}“ · <span class="mono">{esc(workfolder.path)}</span>'
                    "</span>",
                    tag="div",
                    classes="row",
                )
                raw(
                    '<a class="btn" href="/einstellungen#aufraeumen">Aufräumregeln …</a>',
                    tag="div",
                    classes="actions",
                )
            self.box = ui.element("div")
        await self._load()

    async def _load(self) -> None:
        self.box.clear()
        await load_into(
            self.box,
            self._fetch,
            self._render,
            retry_href=self._href(),
            skeleton=skeleton_html(
                6,
                shape="rows",
                reason=f"Größen werden gemessen … ({self.runs_count} Runs) Das dauert bei vielen "
                "Runs etwas.",
            ),
        )

    async def _fetch(self) -> tuple[StorageReport, list[RunSummary]]:
        report = await self.api.get_storage()
        page = await self.api.list_runs(limit=1000)
        return report, page.items

    def _render(self, data: tuple[StorageReport, list[RunSummary]]) -> None:
        report, runs = data
        self.report = report
        self.runs = {r.id: r for r in runs}
        if report.runs_actual == 0 and not report.runs:
            raw(
                empty_state_html(
                    "storage",
                    "Runs belegen 0 B",
                    "Hier siehst du später, wie viel Platz Arbeitskopien, Protokolle und "
                    "Ergebnisse brauchen.",
                )
            )
            return
        with ui.element("div").classes("storage-grid"):
            self._usage_card(report)
            self._suggestion_card(report)
        self._runs_card(report)
        raw(
            '<p class="small muted" style="margin-top:8px">Löschen verschiebt Runs in den '
            "macOS-Papierkorb, und nur nach Bestätigung. Automatische Regeln verschlanken und "
            "archivieren, sie löschen nie.</p>"
        )

    def _usage_card(self, r: StorageReport) -> None:
        limit = r.limit_bytes or 0
        ratio = r.runs_actual / limit if limit else None
        if ratio is not None and ratio >= 0.9:
            badge = badge_html(
                Status(
                    "st-warn",
                    "warning",
                    f"{fmt.percent(ratio)} deiner Grenze von {_limit_text(limit)}",
                )
            )
        elif ratio is not None:
            share = f"{fmt.percent(ratio)} deiner Grenze von {_limit_text(limit)}"
            badge = f'<span class="faint small">{esc(share)}</span>'
        else:
            badge = ""
        legend = "".join(
            f'<span><i class="{css}"></i>{esc(name)} '
            f"{esc(fmt.size(getattr(r.by_part, key)))}</span>"
            for key, css, name in PART_LABELS
        )
        projects = "".join(
            f'<tr><td><a href="/projekte/{esc(p.project)}">{esc(p.project)}</a></td>'
            f'<td class="num">{p.runs}</td><td class="num">{esc(fmt.size(p.actual))}</td></tr>'
            for p in r.by_project
        )
        raw(
            '<section class="card" aria-labelledby="h-use"><div class="card-head">'
            f'<h2 id="h-use">Runs belegen {esc(fmt.size(r.runs_actual))}</h2>{badge}</div>'
            f"{sizebar_html(r.by_part, r.runs_actual)}"
            f'<div class="legend" style="margin-top:8px">{legend}</div>'
            f'<p class="small muted" style="margin:10px 0 8px">Tatsächlich belegt. Logisch (ohne '
            f"APFS-Klone) wären es {esc(fmt.size(r.runs_logical))}. Festplatte: "
            f"{esc(fmt.size(r.disk_free))} von {esc(fmt.size(r.disk_total))} frei.</p>"
            '<table class="tbl compact"><thead><tr><th>Projekt</th><th class="num">Runs</th>'
            f'<th class="num">Belegt</th></tr></thead><tbody>{projects}</tbody></table></section>'
        )

    def _suggestion_card(self, r: StorageReport) -> None:
        s = r.suggestion
        with ui.element("section").classes("card suggest").props('aria-labelledby="h-sugg"'):
            if s is None:
                raw(
                    '<h2 id="h-sugg">Nichts zu verschlanken</h2><p>Alle fertigen Runs sind schon '
                    "verschlankt. Älteste Runs archivieren?</p>"
                    f'<a class="btn" href="{esc(self._href(filter="verschlankbar"))}">'
                    "Runs nach Größe</a>"
                )
                return
            n = len(s.run_ids)
            excluded = []
            if s.excluded_pinned:
                word = "angehefteter Run" if s.excluded_pinned == 1 else "angeheftete Runs"
                excluded.append(f"{s.excluded_pinned} {word}")
            if s.excluded_active:
                word = "aktiver Run" if s.excluded_active == 1 else "aktive Runs"
                excluded.append(f"{s.excluded_active} {word}")
            raw(
                f'<h2 id="h-sugg">Vorschlag: {esc(fmt.size(s.freed_bytes))} freimachen</h2>'
                f"<p>{n} fertige Runs haben noch ihre Arbeitskopien. <b>Verschlanken</b> löscht "
                "nur die Arbeitskopien und den Projekt-Snapshot.</p>"
                '<div class="check"><span class="st ok" aria-hidden="true">✓</span><span '
                f'class="txt">Bleibt: {esc(", ".join(s.keeps))}</span></div>'
                '<div class="check"><span class="st bad" aria-hidden="true">✕</span><span '
                f'class="txt">Weg: {esc(", ".join(s.removes))} '
                "(<span class='mono'>agents/*/work/</span>, <span class='mono'>base/</span>). "
                "Runs lassen sich danach nicht mehr mit den alten Dateien fortsetzen.</span></div>"
                '<div class="check"><span class="st ok" aria-hidden="true">✓</span><span '
                f'class="txt">Ausgenommen: {esc(" und ".join(excluded) or "keine")}</span></div>'
            )
            with ui.element("div").classes("btn-row").style("margin-top:12px"):
                raw(
                    f'<a class="btn" href="{esc(self._href(filter="verschlankbar"))}">'
                    "Liste ansehen</a>",
                    tag="span",
                )
                button(
                    f"{n} Runs verschlanken …",
                    lambda: self._slim_dialog(list(s.run_ids)),
                    classes="btn primary",
                ).mark("slim-suggestion")

    def _runs_card(self, r: StorageReport) -> None:
        items = r.runs
        if self.filter == "verschlankbar":
            items = [i for i in items if i.slimmable]
        elif self.filter == "archiviert":
            items = [i for i in items if (run := self.runs.get(i.run.id)) and run.archived]
        seg = "".join(
            f'<a class="{"on" if key == self.filter else ""}" href="{esc(self._href(filter=key))}"'
            + (' aria-current="true"' if key == self.filter else "")
            + f">{esc(label)}</a>"
            for key, label in FILTERS.items()
        )
        shown = items if self.all_rows else items[:SHOWN]
        with ui.element("section").classes("card").props('aria-labelledby="h-runs" id="runs"'):
            raw(
                f'<div class="card-head"><h2 id="h-runs">Runs nach Größe</h2><div class="actions">'
                f'<div class="seg" role="group" aria-label="Filter">{seg}</div></div></div>'
            )
            # a run that could not be measured: a note here, the rest of the page stays (G5)
            for item in (i for i in r.runs if _unmeasured(i)):
                with ui.element("div").classes("banner err").props('role="alert"'):
                    raw(icon("error"), tag="span")
                    raw(
                        f"<b>Größe von „{esc(item.run.name)}“ konnte nicht gemessen werden: "
                        f"{esc(item.protected_reason or '')}.</b>Prüfe im Finder, ob der Ordner "
                        "noch da ist und ob arr ihn lesen darf. Alle anderen Runs sind gemessen.",
                        classes="txt",
                    )
                    button(
                        "Im Finder zeigen",
                        lambda i=item: self.frame.toast(
                            f"Im Finder gezeigt: {i.run.id} (im Prototyp nur angezeigt)"
                        ),
                        classes="btn btn-sm",
                    )
            if not items:
                raw('<p class="small muted">Keine Runs für diesen Filter.</p>')
                return
            with ui.element("table").classes("tbl storage-runs"):
                raw(
                    "<tr><th>Run</th><th>Status</th><th>Beendet</th><th>Aufteilung</th>"
                    '<th class="num">Belegt</th><th><span aria-hidden="true">📌</span>'
                    '<span class="sr-only">Angeheftet</span></th><th class="num">Aktion</th></tr>',
                    tag="thead",
                )
                with ui.element("tbody"):
                    for item in shown:
                        self._row(item)
            if len(items) > len(shown):
                raw(
                    f'<p class="small" style="margin:8px 0 0">{len(shown)} von {len(items)} Runs · '
                    f'<a href="{esc(self._href(alle="1"))}#runs">Alle zeigen</a></p>'
                )

    def _row(self, item: StorageItem) -> None:
        run = self.runs.get(item.run.id)
        now = self.api.now()
        tags = ""
        if run and run.slimmed:
            tags += ' <span class="tag">verschlankt</span>'
        if run and run.archived:
            tags += ' <span class="tag">archiviert</span>'
        ended = fmt.when(run.ended_at, now) if run and run.ended_at else "–"
        with ui.element("tr").props(f'id="srow-{esc(item.run.id)}"'):
            raw(
                f'<a href="/runs/{esc(item.run.id)}">{esc(item.run.name)}</a>'
                f'<span class="sub">{esc(item.run.id)}</span>',
                tag="td",
                classes="title",
            )
            raw((run_badges_html(run) if run else "") + tags, tag="td")
            raw(esc(ended), tag="td").classes("nw")
            raw(sizebar_html(item.by_part, item.actual, label=False), tag="td", classes="parts")
            raw("–" if _unmeasured(item) else esc(fmt.size(item.actual)), tag="td", classes="num")
            raw("📌" if run and run.pinned else "", tag="td")
            with ui.element("td").classes("act"):
                self._row_actions(item, run)

    def _row_actions(self, item: StorageItem, run: RunSummary | None) -> None:
        reason = item.protected_reason
        if reason:
            tip = PROTECTED_TIP.get(reason, reason)
            raw(
                f'<button type="button" class="btn btn-sm" disabled aria-disabled="true" '
                f'title="{esc(tip)}">Verschlanken</button> '
                f'<span class="faint small" title="{esc(tip)}">{esc(reason)}</span>',
                tag="span",
            )
            if reason == "angeheftet" and run:
                button(
                    "📌",
                    lambda r=run: self._pin(r, False),
                    classes="icon-btn",
                    title=f"„{item.run.name}“ lösen",
                )
            return
        if item.slimmable:
            button(
                "Verschlanken",
                lambda i=item.run.id: self._slim_dialog([i]),
                classes="btn btn-sm",
            )
        elif item.archivable:
            button("Archivieren", lambda i=item: self._archive_dialog(i), classes="btn btn-sm")
        if run is not None:
            pin_tip = f"„{item.run.name}“ anheften (schützt vor Aufräumregeln)"
            _icon_button("push_pin", pin_tip, lambda r=run: self._pin(r, True))
        trash_tip = f"„{item.run.name}“ in den Papierkorb …"
        _icon_button("delete", trash_tip, lambda i=item: self._trash_dialog(i))

    # --- actions (G6: dialog with what is freed and what stays) -------------------------------

    def _slim_dialog(self, run_ids: list[str]) -> None:
        assert self.report is not None
        by_id = {i.run.id: i for i in self.report.runs}
        chosen = [by_id[r] for r in run_ids if r in by_id]
        freed = sum(i.by_part.work for i in chosen)
        n = len(chosen)
        title = f"{n} Runs verschlanken?" if n > 1 else f"Run „{chosen[0].run.name}“ verschlanken?"
        label = f"{n} Runs verschlanken" if n > 1 else "Run verschlanken"

        async def confirm(_value: str) -> None:
            await self._run_actions(chosen, RunAction.SLIM, done=f"{fmt.size(freed)} frei")

        self.dialog.show(
            title,
            [
                "Gelöscht werden nur die Arbeitskopien und Projekt-Snapshots dieser Runs.",
                "Transkripte, Gruppenchats, Ergebnisse und Diffs bleiben lesbar.",
                "Das lässt sich nicht rückgängig machen.",
            ],
            label,
            confirm,
            intro=f"{fmt.size(freed)} werden frei.",
        )
        rows = "".join(
            f'<tr><td>{esc(i.run.name)}</td><td class="num">'
            f"{esc(fmt.size(i.by_part.work))}</td></tr>"
            for i in chosen[:5]
        )
        rest = chosen[5:]
        if rest:
            rows += (
                f'<tr><td class="faint">… {len(rest)} weitere</td><td class="num faint">'
                f"{esc(fmt.size(sum(i.by_part.work for i in rest)))}</td></tr>"
            )
        if n > 1:
            with self.dialog.body:
                raw(
                    f'<div class="db"><table class="tbl compact slim-list"><tbody>{rows}</tbody>'
                    "</table></div>"
                )

    def _archive_dialog(self, item: StorageItem) -> None:
        async def confirm(_value: str) -> None:
            await self._run_actions([item], RunAction.ARCHIVE, done="Archiviert")

        self.dialog.show(
            f"Run „{item.run.name}“ archivieren?",
            [
                "Der Run wird als .tar.zst nach runs/_archive/ gepackt.",
                "Er bleibt in arr lesbar, das Öffnen dauert dann etwas länger.",
                "Ergebnisse, Transkripte und Gruppenchats bleiben erhalten.",
            ],
            "Run archivieren",
            confirm,
            danger=False,
        )

    def _trash_dialog(self, item: StorageItem) -> None:
        async def confirm(_value: str) -> None:
            await self._run_actions([item], RunAction.DELETE, done="In den Papierkorb verschoben")

        self.dialog.show(
            "Run in den Papierkorb verschieben?",
            [
                f"„{item.run.name}“ ({fmt.size(item.actual)}) wandert in den macOS-Papierkorb.",
                "Dort kannst du ihn wiederherstellen, bis du den Papierkorb leerst.",
                "In arr bleibt er als „gelöscht“ in der Liste.",
            ],
            "In den Papierkorb",
            confirm,
        )

    async def _pin(self, run: RunSummary, pin: bool) -> None:
        try:
            result = await self.api.run_action(run.id, RunAction.PIN if pin else RunAction.UNPIN)
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        self.frame.toast(result.message)
        await self._load()

    async def _run_actions(self, items: list[StorageItem], action: RunAction, *, done: str) -> None:
        """Run the action for each run with progress; afterwards a toast „14,9 GB frei“ (R7)."""
        if self.busy:
            return
        self.busy = True
        progress = None
        with self.box:
            progress = raw("", tag="div", classes="login-wait").props('role="status"')
        try:
            for n, item in enumerate(items, start=1):
                progress.set_content(
                    f"{icon('hourglass_top')}<span>{esc(_verb(action))} {n} von {len(items)}: "
                    f"{esc(item.run.name)} …</span>"
                )
                try:
                    await self.api.run_action(item.run.id, action, dry_run=True)
                    await self.api.run_action(item.run.id, action)
                except ApiException as exc:
                    self.frame.toast(f"{item.run.name}: {exc.error.message}", error=True)
        finally:
            self.busy = False
        self.frame.toast(done)
        await self._load()


def _icon_button(name: str, label: str, on_click: Callable[[], Any]) -> None:
    """A small button with a Material icon and its label for screen readers and the tooltip."""
    element = (
        ui.element("button")
        .classes("icon-btn")
        .props(f'type="button" title="{esc(label)}" aria-label="{esc(label)}"')
    )
    with element:
        raw(icon(name, small=True), tag="span")
    element.on("click", lambda _e: on_click())


def _unmeasured(item: StorageItem) -> bool:
    """A run folder arr could not measure: 0 B with a reason (no run folder is empty)."""
    return item.actual == 0 and bool(item.protected_reason)


def _limit_text(num_bytes: int) -> str:
    """„50 GB“ for a whole limit, else like `fmt.size`."""
    if num_bytes and num_bytes % 1_000_000_000 == 0:
        return f"{num_bytes // 1_000_000_000} GB"
    return fmt.size(num_bytes)


def _verb(action: RunAction) -> str:
    return {
        RunAction.SLIM: "Verschlanke",
        RunAction.ARCHIVE: "Archiviere",
        RunAction.DELETE: "Verschiebe",
    }.get(action, "Bearbeite")


def register() -> None:
    """Register `/speicher` (docs/08 §5.10, deep link `?filter=verschlankbar`)."""

    @ui.page("/speicher")
    async def speicher(filter: str = "", alle: str = "", zustand: str = "") -> None:  # noqa: A002
        api = state_api(get_api(), zustand, "speicher")
        use_screen_assets()
        frame = Frame(
            api, active="speicher", title="Speicher", focus="#inhalt h1" if filter else None
        )
        await frame.start()
        await SpeicherPage(frame, api, filter_=filter, all_rows=alle == "1").build()

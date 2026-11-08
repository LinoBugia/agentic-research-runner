"""Start: the situation in five seconds – must I do something, is everything running? (docs/08 §5.1)

Left „Wartet auf dich“ and „Aktive Runs“, right „Warnungen“, „Zuletzt beendet“ and skill chips;
at 1440 × 760 and 1280 × 680 the first three areas fit without scrolling (T1). Every area loads
on its own (skeleton, banner on errors). Live: new approvals appear on top, run cards and
counters update; usage ticks only patch the figures, so the focus stays where it is.
`?workfolder=neu` opens the dialog for a new workfolder (W3-04).
"""

from nicegui import ui

from arr.models import RunStatus, Starter
from arr.permissions.types import ApprovalStatus, ApprovalType
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    ApprovalItem,
    ErrorCode,
    LiveEvent,
    ProfileInfo,
    RunFilter,
    RunsPage,
    RunSummary,
    SkillInfo,
    SkillKind,
    ToolchainCandidate,
    ToolchainNotice,
    UsageTickEvent,
    WarningInfo,
    WorkfolderInfo,
    WorkfolderRequest,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html, run_badges_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, raw
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.form import Field, checkbox
from arr.service.gui.components.form_dialog import FormDialog
from arr.service.gui.components.page_state import (
    error_html,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.layout import Frame
from arr.service.gui.texts import APPROVAL_TYPE, CATEGORY, RISK, STARTER_PHRASE, Status

MAX_WAITING = 5
RECENT = 4
WAIT = Status("st-wait", "front_hand", "")
STAGE_PHRASE = {
    "working": ("arbeitet", "arbeiten"),
    "waiting_user": ("wartet auf dich", "warten auf dich"),
    "blocked": ("blockiert", "blockiert"),
    "idle": ("wartet", "warten"),
    "error": ("mit Fehler", "mit Fehler"),
    "done": ("fertig", "fertig"),
}


def active_text(count: int) -> str:
    """The active runs of a workfolder for „Workfolder wechseln“."""
    if not count:
        return "keine Runs aktiv"
    return f"{count} Run{'' if count == 1 else 's'} aktiv"


def waiting_row_html(item: ApprovalItem, now: float) -> str:
    """One row of „Wartet auf dich“: kind, sentence, run, deadline or age, „Ansehen“ (§5.1)."""
    kind = badge_html(Status("st-wait", "front_hand", APPROVAL_TYPE[item.type]))
    sub = [esc(item.run.name)]
    if item.type is ApprovalType.PERMISSION:
        sub += [esc(CATEGORY[c]) for c in item.categories[:1]]
        if item.risk:
            sub.append(f"Risiko {RISK[item.risk.level][1]}")
    elif item.type is ApprovalType.ESCALATION:
        sub.append("Run pausiert")
    elif item.type is ApprovalType.QUESTION:  # W3-09 L16: a question of an agent to you
        sub.append(f"#{esc(item.channel or '')}")
    else:
        sub = [esc(item.run.project), esc(STARTER_PHRASE.get(item.requested_by or Starter.GUI))]
        n = len(item.deviations)
        if n:
            word = "Abweichung" if n == 1 else "Abweichungen"
            sub.append(f'<b class="wait-text">⚠ {n} {word}</b> von deinen Standards')
        else:
            sub.append("keine Abweichungen")
    if item.deadline:
        sub.insert(
            0,
            f'<span class="nowrap deadline" data-deadline="{item.deadline:.0f}">'
            f"{esc(fmt.deadline(item.deadline, now))}</span>",
        )
    else:
        age = fmt.relative(item.created_at, now) or f"seit {fmt.when(item.created_at, now)}"
        sub.append(f'<span class="nowrap">{esc(age)}</span>')
    return (
        f'<div class="row" data-item="{esc(item.id)}">{kind}'
        f'<div class="what"><b>{esc(item.title)}</b>'
        f'<span class="small muted">{" · ".join(sub)}</span></div>'
        f'<a class="btn btn-sm" href="/freigaben/{esc(item.id)}" '
        f'aria-label="{esc(item.title)} ansehen">Ansehen</a></div>'
    )


def agents_text(run: RunSummary) -> str:
    """„2 arbeiten · 1 wartet auf dich · 2 warten“ (§5.1, G1)."""
    parts = []
    counts = run.agents_by_state.model_dump()
    for key in ("working", "waiting_user", "blocked", "idle", "error"):
        n = counts.get(key, 0)
        if n:
            one, many = STAGE_PHRASE[key]
            parts.append(f"{n} {one if n == 1 else many}")
    return " · ".join(parts)


def run_meta_html(run: RunSummary, now: float) -> str:
    """Meta line of an active run card: stage, agents, figures with their limit, „#main 3 neu“."""
    parts = []
    if run.status is RunStatus.AWAITING_APPROVAL:
        parts.append(esc(f"{STARTER_PHRASE[run.starter]} · startet nach deiner Freigabe"))
    elif run.status in (RunStatus.QUEUED, RunStatus.PREPARING):
        parts.append(esc(run.status_detail or "startet gleich"))
    if run.stage and run.status is not RunStatus.AWAITING_APPROVAL:
        stage = f"{run.stage.label} ({run.stage.index} von {run.stage.count})"
        if run.stage.detail and run.open_escalation:
            stage += f" · {run.stage.detail}"
        parts.append(esc(stage))
    agents = agents_text(run)
    if agents and run.status is not RunStatus.AWAITING_APPROVAL:
        parts.append(esc(agents))
    if run.status is RunStatus.PAUSED and run.status_since:
        parts.append(f"pausiert seit {fmt.clock(run.status_since)}")
    limits = run.limits
    if run.started_at is not None:
        elapsed = fmt.duration_hm(limits.elapsed_s)
        time_text = (
            f"{elapsed} von {fmt.limit_hours(limits.wall_time_s)}"
            if limits.wall_time_s
            else elapsed
        )
        parts.append(f'<span id="t-{esc(run.id)}">{esc(time_text)}</span>')
    if run.cost_usd is not None:
        money = fmt.money(run.cost_usd, run.cost_kind)
        if limits.budget_usd:
            money += f" von {fmt.budget(limits.budget_usd)}"
        parts.append(f'<span id="c-{esc(run.id)}">{esc(money)}</span>')
    last = run.last_message
    if last and last.unread_for_user:
        parts.append(f"<b>#{esc(last.channel)} {last.unread_for_user} neu</b>")
    return " · ".join(parts)


def run_card_html(run: RunSummary, now: float, approval_href: str | None) -> str:
    """Compact card of an active run (§5.1 „Aktive Runs“); a run waiting for its start links
    to its run start card."""
    href = approval_href if run.status is RunStatus.AWAITING_APPROVAL and approval_href else ""
    href = href or f"/runs/{run.id}"
    skill = f'<span class="tag">{esc(run.skill)}</span>' if run.skill else ""
    badges = run_badges_html(run)
    return (
        f'<a class="card tight run-card" href="{esc(href)}" data-run="{esc(run.id)}">'
        f'<div class="row">{badges}<b>{esc(run.name)}</b>{skill}</div>'
        f'<div class="meta" id="m-{esc(run.id)}">{run_meta_html(run, now)}</div></a>'
    )


def warning_html(warning: WarningInfo) -> str:
    """A warning with its action (§4.2: every warning has an action)."""
    kind = "err" if warning.severity.value == "error" else "warn"
    symbol = "error" if kind == "err" else "warning"
    role = "alert" if kind == "err" else "status"
    return (
        f'<div class="banner {kind} banner-sm warn-item" role="{role}">{icon(symbol)}'
        f'<div class="txt"><b>{esc(warning.title)}</b>{esc(warning.text)}</div>'
        f'<a class="btn btn-sm" href="{esc(warning.action.url)}">{esc(warning.action.label)}</a>'
        "</div>"
    )


def recent_row_html(run: RunSummary, now: float) -> str:
    """A finished run with a short result and the fitting action (§5.1 „Zuletzt beendet“)."""
    info = run.info or ""
    if run.error and not info:
        info = run.error.message.split(". ")[0]
    if run.best_result and run.status is RunStatus.DONE and not run.info:
        info = f"beste Bewertung {fmt.score(run.best_result.score)}"
    sub = fmt.when(run.ended_at, now) + (f" · {info}" if info else "")
    if run.status is RunStatus.DONE:
        action = ("Ergebnis", f"/runs/{run.id}?ansicht=ergebnisse")
    elif run.error and run.error.action and run.status is RunStatus.FAILED:
        action = (run.error.action.label, run.error.action.url)
    else:
        action = ("Ansehen", f"/runs/{run.id}")
    return (
        f'<tr><td class="title"><div class="row recent-name">{run_badges_html(run)}'
        f'<a href="/runs/{esc(run.id)}">{esc(run.name)}</a></div>'
        f'<span class="info">{esc(sub)}</span></td>'
        f'<td class="right"><a class="btn btn-sm" href="{esc(action[1])}" '
        f'aria-label="{esc(action[0])}: {esc(run.name)}">{esc(action[0])}</a></td></tr>'
    )


class StartPage:
    """State of one start page (one browser tab)."""

    def __init__(self, frame: Frame, api: ApiClient, zustand: str) -> None:
        self.frame = frame
        self.api = api
        self.zustand = zustand
        self.active: dict[str, RunSummary] = {}
        self.dirty: set[str] = set()
        self.candidates: list[ToolchainCandidate] = []

    async def build(self) -> None:
        workfolder = await self.api.get_workfolder()
        with self.frame.content:
            self.dialog = ConfirmDialog()
            self.form = FormDialog()
            self._head(workfolder)
            self.notice = ui.element("div")
            with ui.element("div").classes("start-grid") as self.grid:
                with ui.element("div"):
                    self.empty_box = ui.element("div")
                    self.waiting = self._section("h-wait", "Wartet auf dich", "/freigaben")
                    self.runs = self._section(
                        "h-active", "Aktive Runs", "/runs?status=aktiv", more="Alle Runs →"
                    )
                with ui.element("div"):
                    self.warnings = self._section("h-warn", "Warnungen", None, anchor="warnungen")
                    self.recent = self._section(
                        "h-done", "Zuletzt beendet", "/runs?status=fertig", more="Alle →"
                    )
                    self.skills = self._section("h-quick", "Neuer Run aus Skill", None)
        await load_into(
            self.notice, self._load_toolchain, self._render_notice, retry_href="/", skeleton=""
        )
        await self._load_all()
        self.frame.on_event(self._on_event)
        ui.timer(1.0, self._flush)

    async def _load_toolchain(self) -> ToolchainNotice | None:
        """Base venvs for a new workfolder and the warning about the current Python (§8.4)."""
        try:
            settings = await self.api.get_workfolder_settings()
        except ApiException:
            return None  # arr.yaml is broken: the settings page says why
        self.candidates = settings.toolchain_candidates
        return settings.toolchain_notice

    def _render_notice(self, notice: ToolchainNotice | None) -> None:
        if notice:
            raw(
                '<div class="banner warn" role="status" style="margin-bottom:16px">'
                f'{icon("warning")}<div class="txt"><b>{esc(notice.title)}</b> '
                f"{esc(notice.text)}</div>"
                '<a class="btn btn-sm" href="/einstellungen#python">Python-Umgebung wählen</a>'
                "</div>"
            )

    def _head(self, wf: WorkfolderInfo) -> None:
        with ui.element("div").classes("page-head start-head").props('id="workfolder"'):
            raw(
                f'<h1 tabindex="-1">{esc(wf.name)}</h1>'
                f'<div class="small muted"><span class="mono">{esc(wf.path)}</span> · '
                f"{wf.projects_count} Projekte · {wf.runs_count} Runs · Festplatte: "
                f"{esc(fmt.size(wf.disk_free_bytes))} frei</div>",
                tag="div",
            )
            with ui.element("div").classes("actions"):
                button("Workfolder wechseln ▾", self._switch_dialog).mark("switch-workfolder")
                raw(
                    f'<a class="btn primary" href="/neuer-run">{icon("add")}Neuer Run</a>',
                    tag="span",
                )

    def _section(
        self,
        heading_id: str,
        title: str,
        href: str | None,
        *,
        more: str = "Alle Freigaben →",
        anchor: str | None = None,
    ) -> ui.element:
        props = f'aria-labelledby="{heading_id}"' + (f' id="{anchor}"' if anchor else "")
        with ui.element("section").classes("section").props(props) as section:
            title_box = raw(self._title_html(heading_id, title, href, more, None), tag="div")
            box = ui.element("div")
        box.section = section  # type: ignore[attr-defined]
        box.title_box = title_box  # type: ignore[attr-defined]
        box.title_args = (heading_id, title, href, more)  # type: ignore[attr-defined]
        return box

    @staticmethod
    def _title_html(
        heading_id: str, title: str, href: str | None, more: str, count_html: str | None
    ) -> str:
        link = f'<a class="more" href="{esc(href)}">{esc(more)}</a>' if href else ""
        return (
            f'<div class="section-title"><h2 id="{heading_id}" tabindex="-1">{esc(title)}</h2>'
            f"{count_html or ''}{link}</div>"
        )

    def _set_count(self, box: ui.element, count_html: str) -> None:
        box.title_box.set_content(self._title_html(*box.title_args, count_html))  # type: ignore[attr-defined]

    async def _load_all(self) -> None:
        here = "/"
        await load_into(
            self.waiting,
            lambda: self.api.list_approvals(status=[ApprovalStatus.OPEN]),
            self._render_waiting,
            retry_href=here,
            skeleton=skeleton_html(3, shape="rows", reason="Die Freigabe-Inbox wird gelesen …"),
        )
        await load_into(
            self.runs,
            self._load_runs,
            self._render_runs,
            retry_href=here,
            skeleton=skeleton_html(3, shape="rows", reason="Die Run-Ordner werden gelesen …"),
        )
        await load_into(
            self.warnings,
            self.api.list_warnings,
            self._render_warnings,
            retry_href=here,
            skeleton=skeleton_html(2, shape="rows"),
        )
        await load_into(
            self.recent,
            lambda: self.api.list_runs(status=RunFilter.ALL, limit=60),
            self._render_recent,
            retry_href=here,
            skeleton=skeleton_html(3, shape="rows"),
        )
        await load_into(
            self.skills,
            lambda: self.api.list_skills(SkillKind.GROUP),
            self._render_skills,
            retry_href=here,
            skeleton=skeleton_html(1),
        )
        await self._render_empty()

    async def _load_runs(self) -> tuple[list[RunSummary], list[ApprovalItem]]:
        page = await self.api.list_runs(status=RunFilter.ACTIVE, limit=100)
        try:
            items = await self.api.list_approvals(status=[ApprovalStatus.OPEN])
        except ApiException:
            items = []  # no inbox yet (W3-03): run cards without approval links
        return page.items, items

    # --- areas ---------------------------------------------------------------------------------

    def _render_waiting(self, items: list[ApprovalItem]) -> None:
        now = self.api.now()
        n = len(items)
        self._set_count(
            self.waiting,
            badge_html(Status("st-wait", "front_hand", str(n))) if n else "",
        )
        if not items:
            raw(
                '<p class="muted small">Nichts wartet auf dich. Neue Freigaben erscheinen hier '
                "und als Mitteilung auf deinem Mac.</p>"
            )
            return
        rows = "".join(waiting_row_html(i, now) for i in items[:MAX_WAITING])
        more = ""
        if n > MAX_WAITING:
            more = (
                f'<p class="small" style="margin:6px 0 0"><a href="/freigaben">'
                f"und {n - MAX_WAITING} weitere · Alle Freigaben →</a></p>"
            )
        raw(f'<div class="card tight wait-list" aria-live="polite">{rows}</div>{more}')

    def _render_runs(self, data: tuple[list[RunSummary], list[ApprovalItem]]) -> None:
        runs, items = data
        now = self.api.now()
        first_open: dict[str, str] = {}
        for item in items:
            first_open.setdefault(item.run.id, f"/freigaben/{item.id}")
        order = [
            RunStatus.RUNNING,
            RunStatus.PAUSED,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.PREPARING,
            RunStatus.QUEUED,
            RunStatus.FINALIZING,
        ]
        runs = sorted(
            runs,
            key=lambda r: (
                r.open_approvals == 0,
                order.index(r.status) if r.status in order else 9,
                -r.created_at,
            ),
        )
        self.active = {r.id: r for r in runs}
        n = len(runs)
        self._set_count(self.runs, badge_html(Status("st-run", "circle", str(n))) if n else "")
        if not runs:
            raw('<p class="muted small">Kein Run läuft gerade.</p>')
            return
        raw("".join(run_card_html(r, now, first_open.get(r.id)) for r in runs))

    def _render_warnings(self, warnings: list[WarningInfo]) -> None:
        self._set_count(
            self.warnings, f'<span class="faint">{len(warnings)}</span>' if warnings else ""
        )
        if not warnings:
            raw('<p class="muted small">Keine Warnungen.</p>')
            return
        raw("".join(warning_html(w) for w in warnings))

    def _render_recent(self, page: RunsPage) -> None:
        now = self.api.now()
        runs = [r for r in page.items if not r.active][:RECENT]
        if not runs:
            raw('<p class="muted small">Noch kein Run beendet.</p>')
            return
        rows = "".join(recent_row_html(r, now) for r in runs)
        raw(f'<table class="tbl compact recent"><tbody>{rows}</tbody></table>')

    def _render_skills(self, skills: list[SkillInfo]) -> None:
        startable = [s for s in skills if s.valid]
        if not startable:
            raw(
                '<p class="muted small">Noch keine Group-Skills. <a href="/skills">Skills '
                "ansehen</a></p>"
            )
            return
        chips = "".join(
            f'<a class="chip" href="/neuer-run?skill={esc(s.name)}">{esc(s.name)}</a>'
            for s in startable
        )
        raw(f'<div class="row">{chips}</div>')

    async def _render_empty(self) -> None:
        """„Alles ruhig“ (or „Noch kein Zugang eingerichtet“) when nothing needs you (§8.5)."""
        try:
            counts = await self.api.get_counts()
            profiles: list[ProfileInfo] = await self.api.list_profiles()
        except ApiException:
            return
        self.empty_box.clear()
        quiet = counts.waiting == 0 and counts.active == 0 and counts.warnings == 0
        no_profile = not profiles and self.zustand != "leer"
        for box in (self.waiting, self.runs):
            box.section.style(f"display:{'none' if quiet else 'block'}")  # type: ignore[attr-defined]
        if not quiet and not no_profile:
            return
        with self.empty_box.style("margin-bottom:24px"):
            if no_profile:
                raw(
                    empty_state_html(
                        "key",
                        "Noch kein Zugang eingerichtet",
                        "Ohne Auth-Profil können keine Agenten starten. Das Einrichten dauert "
                        "etwa 2 Minuten.",
                        [("Profil anlegen", "/profile?neu=1", True)],
                    )
                )
            else:
                raw(
                    empty_state_html(
                        "home",
                        "Alles ruhig",
                        "Kein Run läuft, nichts wartet auf dich, keine Warnungen.",
                        [("Neuer Run", "/neuer-run", True), ("Letzte Ergebnisse", "/runs", False)],
                    )
                )

    # --- workfolder ----------------------------------------------------------------------------

    async def _switch_dialog(self) -> None:
        """„Workfolder wechseln“: known workfolders, „Neuen Workfolder anlegen …“ (wireframe)."""
        try:
            wf = await self.api.get_workfolder()  # the figures of this moment, not of the page
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        options = [(w.path, w) for w in wf.known] or []
        chosen = {"path": wf.path}

        async def switch(_value: str) -> None:
            path = chosen["path"]
            if path == wf.path:
                return
            try:
                info = await self.api.select_workfolder(WorkfolderRequest(path=path))
            except ApiException as exc:
                self.frame.toast(exc.error.message, error=True)
                return
            self._opened(info)

        self.dialog.show(
            "Workfolder wechseln",
            [],
            "Wechseln",
            switch,
            danger=False,
            intro=f"Runs aus dem Terminal in „{wf.name}“ laufen weiter. Du siehst sie wieder, "
            "wenn du zurückwechselst.",
        )
        with self.dialog.body, ui.element("div").classes("db"):
            with ui.element("div").classes("radio-cards"):
                for path, ref in options:
                    with ui.element("label").classes("radio-card"):
                        radio = ui.element("input").props(
                            f'type="radio" name="wf" value="{esc(path)}"'
                            + (" checked" if path == wf.path else "")
                        )
                        radio.mark(f"workfolder-{ref.name}")
                        radio.on("change", lambda _e, p=path: chosen.__setitem__("path", p))
                        figures = f"{ref.projects_count} Projekte"
                        if ref.active_runs is not None:
                            figures += ", " + active_text(ref.active_runs)
                        raw(
                            f"<span><b>{esc(ref.name)}</b><span class='mono small muted'>"
                            f"{esc(ref.path)}</span> · <span class='small muted'>"
                            f"{figures}</span></span>",
                            tag="span",
                        )
            with ui.element("p").classes("small").style("margin-top:12px"):
                button(
                    "Neuen Workfolder anlegen oder anderen Ordner öffnen …",
                    self._new_dialog,
                    classes="btn btn-sm",
                ).mark("new-workfolder")

    def _opened(self, info: WorkfolderInfo) -> None:
        """Feedback after a switch; open pages of the service reload on their own."""
        tail = (
            " Im Prototyp bleiben die Beispieldaten gleich."
            if hasattr(self.api, "datasets")
            else ""
        )
        self.frame.toast(f"Workfolder „{info.name}“ geöffnet.{tail}")

    def _new_dialog(self) -> None:
        """„Neuer Workfolder“: path, and the base venv if there is exactly one (W3-00 §7)."""
        self.dialog.close()
        fields: dict[str, Field] = {}
        suggested = self.candidates[0].path if len(self.candidates) == 1 else None
        chosen = {"python": suggested}

        def build() -> None:
            fields["path"] = Field(
                "Ordner",
                name="workfolder-path",
                value="~/",
                help_text="Ein neuer oder leerer Ordner, z. B. ~/Test-UX3. Liegt dort schon ein "
                "Workfolder, öffnet arr ihn. arr legt arr.yaml, projects/, skills/ und runs/ an.",
                attrs='autocomplete="off" spellcheck="false"',
            )
            if suggested:
                checkbox(
                    f"Python-Umgebung „{suggested.rsplit('/', 1)[-1]}“ für die Agenten nutzen",
                    checked=True,
                    on_change=lambda v: chosen.__setitem__("python", suggested if v else None),
                    marker="workfolder-python",
                )

        async def submit() -> str | None:
            try:
                info = await self.api.select_workfolder(
                    WorkfolderRequest(
                        path=fields["path"].value.strip() or "~/",
                        create=True,
                        python=chosen["python"],
                    )
                )
            except ApiException as exc:
                if exc.error.field == "path":
                    fields["path"].set_error(exc.error.message)
                    return "Dieser Ordner geht nicht."
                return exc.error.message
            self._opened(info)
            return None

        self.form.open("Neuer Workfolder", "Anlegen", build, submit)

    # --- live ------------------------------------------------------------------------------------

    def _on_event(self, event: LiveEvent) -> None:
        if isinstance(event, UsageTickEvent) and event.run_id in self.active:
            run = self.active[event.run_id].model_copy(
                update={
                    "duration_s": event.elapsed_s,
                    "cost_usd": round(event.spent_usd, 2) if event.spent_usd is not None else None,
                    "limits": self.active[event.run_id].limits.model_copy(
                        update={"elapsed_s": event.elapsed_s, "spent_usd": event.spent_usd}
                    ),
                }
            )
            self.active[run.id] = run
            self.frame.js(f"arr.setHtml('m-{run.id}', {run_meta_html(run, self.api.now())!r})")
            return
        if event.type.startswith("approval."):
            self.dirty |= {"waiting", "runs"}
        elif event.type == "run.status":
            self.dirty |= {"runs", "recent"}
        elif event.type == "warnings.changed":
            self.dirty.add("warnings")
        elif event.type == "counts.changed":
            self.dirty.add("empty")

    async def _flush(self) -> None:
        """Re-render the areas that changed, at most once a second (no jumps, §5)."""
        if not self.dirty:
            return
        dirty, self.dirty = self.dirty, set()
        here = "/"
        if "waiting" in dirty:
            await load_into(
                self.waiting,
                lambda: self.api.list_approvals(status=[ApprovalStatus.OPEN]),
                self._render_waiting,
                retry_href=here,
                skeleton="",
            )
        if "runs" in dirty:
            await load_into(
                self.runs,
                self._load_runs,
                self._render_runs,
                retry_href=here,
                skeleton="",
            )
        if "recent" in dirty:
            await load_into(
                self.recent,
                lambda: self.api.list_runs(status=RunFilter.ALL, limit=60),
                self._render_recent,
                retry_href=here,
                skeleton="",
            )
        if "warnings" in dirty:
            await load_into(
                self.warnings,
                self.api.list_warnings,
                self._render_warnings,
                retry_href=here,
                skeleton="",
            )
        await self._render_empty()


def register() -> None:
    """Register `/` (docs/08 §5.1)."""

    @ui.page("/")
    async def start(zustand: str = "", workfolder: str = "") -> None:
        base = get_api()
        api = state_api(base, "leer" if zustand == "ohne-profil" else zustand)
        use_screen_assets()
        frame = Frame(api, active="start", title="Start")
        await frame.start()
        if zustand == "workfolder-fehlt":
            wf = await api.get_workfolder()
            with frame.content:
                raw('<div class="page-head"><h1 tabindex="-1">Start</h1></div>')
                err = ApiException.of(
                    ErrorCode.NOT_FOUND,
                    f"Workfolder {wf.path} nicht gefunden.",
                    detail="Wurde er verschoben? Such den Ordner oder öffne einen anderen.",
                )
                raw(error_html(err, retry_href="/", retry_label="Ordner suchen"))
                raw(
                    '<p style="margin-top:12px"><a class="btn" href="/einrichtung?schritt=1">'
                    "Anderen Workfolder öffnen</a></p>"
                )
            return
        page = StartPage(frame, api, zustand)
        await page.build()
        if workfolder == "neu":
            page._new_dialog()

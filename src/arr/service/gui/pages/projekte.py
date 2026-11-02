"""Projekte: create projects, set them up for Claude Code and see their state (docs/08 §5.6).

Left the list (name, „eingerichtet“/„unvollständig“, title, runs, size), right the selected
project: set-up checks with „Reparieren“, „Mit Claude Code arbeiten“ with the path to copy,
latest runs, folders, size. Dialogs „Neues Projekt“ (folder name with live path, title, the
files arr creates) and „Vorhandenen Ordner einrichten“ (arr adds only missing arr files, G8).
`/projekte/<name>` opens a project with the focus on its title; `?neu=1` opens „Neues Projekt“.
"""

import re

from nicegui import ui

from arr.service.api_client import ApiClient
from arr.service.api_models import SLUG as API_SLUG
from arr.service.api_models import (
    ApiException,
    CreateProjectRequest,
    LiveEvent,
    ProjectCheck,
    ProjectInfo,
    RunsPage,
    Severity,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html, run_badges_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, raw, set_disabled
from arr.service.gui.components.copy_field import copy_html
from arr.service.gui.components.form import Field
from arr.service.gui.components.form_dialog import FormDialog
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.layout import Frame
from arr.service.gui.texts import Status

SLUG = re.compile(API_SLUG)
NEW_FILES = "CLAUDE.md (mit arr-Block), AGENTS.md, .mcp.json, Skill-Links, project.yaml und die "
NEW_FILES += "Ordner notes/, tex/, code/, results/"
OK = Status("st-ok", "check", "eingerichtet")
INCOMPLETE = Status("st-warn", "warning", "unvollständig")


def project_item_html(project: ProjectInfo, selected: bool) -> str:
    """One entry of the project list (§5.6 „Liste“)."""
    badge = badge_html(OK if project.setup_ok else INCOMPLETE)
    runs = project.runs
    parts = [esc(project.title), f"{runs.total} Runs"]
    if runs.active:
        parts.append(f"{runs.active} aktiv")
    if runs.waiting:
        word = "wartet" if runs.waiting == 1 else "warten"
        parts.append(f"{runs.waiting} {word} auf dich")
    parts.append(esc(fmt.size(project.size_bytes)))
    failing = [c for c in project.checks if not c.ok]
    if failing:
        parts.append(esc(failing[0].message))
    current = ' aria-current="page"' if selected else ""
    css = "list-item sel" if selected else "list-item"
    return (
        f'<a class="{css}" href="/projekte/{esc(project.name)}"{current}>'
        f'<div class="t">{esc(project.name)}{badge}</div>'
        f'<div class="d">{" · ".join(parts)}</div></a>'
    )


class ProjektePage:
    """State of one Projekte page (one browser tab)."""

    def __init__(self, frame: Frame, api: ApiClient, *, name: str, new: str) -> None:
        self.frame = frame
        self.api = api
        self.name = name
        self.new = new
        self.projects: list[ProjectInfo] = []

    async def build(self) -> None:
        workfolder = await self.api.get_workfolder()
        self.root = f"{workfolder.path}/projects"
        with self.frame.content:
            self.dialog = FormDialog()
            with ui.element("div").classes("page-head"):
                raw(
                    '<h1 tabindex="-1">Projekte</h1><span class="faint">'
                    f'„{esc(workfolder.name)}“ · <span class="mono">{esc(self.root)}</span></span>',
                    tag="div",
                    classes="row",
                )
                with ui.element("div").classes("actions"):
                    button("Vorhandenen Ordner einrichten …", self._setup_dialog)
                    button("＋ Neues Projekt", self._new_dialog, classes="btn primary").mark(
                        "new-project"
                    )
            self.box = ui.element("div")
        await load_into(
            self.box,
            self.api.list_projects,
            self._render,
            retry_href=clean_href(f"/projekte/{self.name}" if self.name else "/projekte"),
            skeleton=skeleton_html(4, shape="rows", reason="Die Projekte werden gelesen …"),
        )
        if self.new == "1":
            self._new_dialog()
        elif self.new == "ordner":
            self._setup_dialog()
        elif self.new == "beispiel":
            await self._example()
        self.frame.on_event(self._on_event)

    async def _render(self, projects: list[ProjectInfo]) -> None:
        self.projects = projects
        if not projects:
            raw(
                empty_state_html(
                    "folder",
                    "Noch keine Projekte",
                    "Ein Projekt ist ein Ordner mit deinem Mathe-Material und einer CLAUDE.md.",
                    [
                        ("Neues Projekt", "/projekte?neu=1", True),
                        ("Vorhandenen Ordner einrichten", "/projekte?neu=ordner", False),
                        ("Beispielprojekt collatz", "/projekte?neu=beispiel", False),
                    ],
                )
            )
            return
        names = [p.name for p in projects]
        selected = self.name if self.name in names else names[0]
        with ui.element("div").classes("split wide-left"):
            with ui.element("nav").classes("list").props('aria-label="Projekte"'):
                self.list_box = raw(
                    "".join(project_item_html(p, p.name == selected) for p in projects),
                    tag="div",
                    classes="list",
                )
                raw(
                    '<p class="small muted" style="margin-top:8px">„Eingerichtet“ heißt: '
                    "CLAUDE.md mit arr-Block, .mcp.json und Skill-Links sind da. Nur dann kann "
                    "Claude Code im Projekt mit arr arbeiten.</p>"
                )
            self.detail = ui.element("div")
        if self.name and self.name not in names:
            with self.detail:
                raw(
                    f'<div class="banner err" role="alert">{icon("error")}<div class="txt">'
                    f"<b>Projekt „{esc(self.name)}“ nicht gefunden.</b> Wähle links ein Projekt "
                    "oder lege es neu an.</div></div>"
                )
            return
        await self._render_detail(next(p for p in projects if p.name == selected))

    async def _render_detail(self, project: ProjectInfo) -> None:
        box = self.detail
        box.clear()
        runs: RunsPage | None = None
        try:
            runs = await self.api.list_runs(project=project.name, limit=4)
        except ApiException:
            runs = None
        # without the folder only what arr keeps itself is readable: the runs (G4)
        missing = any(c.key == "folder" and not c.ok for c in project.checks)
        locked = "Der Ordner fehlt. Suche ihn zuerst mit „Ordner suchen“."
        with box, ui.element("div").classes("card"):
            with ui.element("div").classes("card-head"):
                raw(
                    f'<h2 id="project-title" tabindex="-1">{esc(project.name)}</h2>'
                    f'<span class="muted">{esc(project.title)}</span>',
                    tag="div",
                    classes="row",
                )
                with ui.element("div").classes("actions"):
                    finder = button(
                        "Im Finder öffnen",
                        lambda: self.frame.toast(
                            f"Im Finder öffnen kommt mit der fertigen App. Pfad: {project.path}"
                        ),
                        classes="btn btn-sm",
                    )
                    if missing:
                        set_disabled(finder, True)
                        finder.props(f'title="{esc(locked)}"')
                    raw(
                        f'<button type="button" class="btn btn-sm" data-copy="{esc(project.path)}"'
                        f' aria-label="Pfad von {esc(project.name)} kopieren">Pfad kopieren'
                        "</button>",
                        tag="span",
                    )
                    if missing:
                        raw(
                            f'<button type="button" class="btn btn-sm primary" disabled '
                            f'aria-disabled="true" title="{esc(locked)}">'
                            f"{icon('add', small=True)}Run in diesem Projekt</button>",
                            tag="span",
                        )
                    else:
                        raw(
                            f'<a class="btn btn-sm primary" href="/neuer-run?projekt='
                            f'{esc(project.name)}">{icon("add", small=True)}Run in diesem '
                            "Projekt</a>",
                            tag="span",
                        )
            raw(
                '<nav class="tabs" aria-label="Ansicht" style="margin-bottom:12px">'
                '<a class="active" aria-current="page" href="#">Übersicht</a>'
                f'<a href="/runs?projekt={esc(project.name)}">Runs <span class="n">'
                f"{project.runs.total}</span></a></nav>"
            )
            with ui.element("div").classes("grid-2e"):
                with ui.element("div"):
                    raw("<h3>Einrichtung</h3>")
                    for check in project.checks:
                        self._check_row(project, check, unknown=missing and check.key != "folder")
                    raw(
                        '<h3 style="margin-top:16px">Mit Claude Code arbeiten</h3><p class="small">'
                        "Öffne diesen Ordner in Claude Code. Claude Code kennt dann die "
                        "Group-Skills und kann Runs starten. Jeder Run braucht deine Freigabe "
                        f"hier.</p>{copy_html(project.path)}"
                    )
                with ui.element("div"):
                    raw("<h3>Letzte Runs</h3>")
                    raw(self._runs_html(runs, project))
                    if missing:
                        raw(
                            '<h3 style="margin-top:16px">Ordner</h3><p class="small muted">Nicht '
                            "lesbar, solange der Ordner fehlt. Die Runs oben liegen im Workfolder "
                            "und bleiben lesbar.</p>"
                        )
                        return
                    folders = (
                        " · ".join(f"{esc(f.name)}/ ({f.count})" for f in project.folders) or "–"
                    )
                    now = self.api.now()
                    raw(
                        f'<h3 style="margin-top:16px">Ordner</h3><div class="folders">CLAUDE.md · '
                        f'{folders}</div><div class="small muted" style="margin-top:4px">Größe '
                        f"{esc(fmt.size(project.size_bytes))} · zuletzt geändert "
                        f"{esc(fmt.when(project.modified_at, now))}</div>"
                    )

    def _check_row(
        self, project: ProjectInfo, check: ProjectCheck, *, unknown: bool = False
    ) -> None:
        ok = check.ok
        severity = check.severity
        symbol = "✓" if ok else ("!" if severity is Severity.WARNING else "✕")
        css = "ok" if ok else ("warn" if severity is Severity.WARNING else "bad")
        detail = check.detail
        if unknown:  # the folder is missing: nothing in it can be checked
            raw(
                '<div class="check"><span class="st todo" aria-hidden="true">–</span><span '
                f'class="txt">{esc(check.message)}<small>nicht prüfbar, solange der Ordner fehlt'
                "</small></span></div>"
            )
            return
        with ui.element("div").classes("check"):
            raw(f'<span class="st {css}" aria-hidden="true">{symbol}</span>', tag="span")
            with ui.element("span").classes("txt"):
                word = "" if ok else " (fehlt)"
                raw(
                    f"{esc(check.message)}<span class='sr-only'>{word}</span>"
                    + (f"<small>{esc(detail)}</small>" if detail else ""),
                    tag="span",
                )
                action = check.action
                if not ok and action is not None:
                    if check.key == "folder":
                        button(
                            action.label,
                            lambda: self.frame.toast(
                                "Einen Ordnerdialog gibt es noch nicht. Lege den Ordner wieder "
                                "unter projects/ ab."
                            ),
                            classes="btn btn-sm",
                        )
                    else:
                        button(
                            action.label,
                            lambda: self._repair(project.name),
                            classes="btn btn-sm",
                        ).mark("repair")

    def _runs_html(self, runs: RunsPage | None, project: ProjectInfo) -> str:
        if runs is None:
            return (
                '<p class="small muted">Die Runs ließen sich gerade nicht lesen. Der Index wird '
                f'neu aufgebaut. <a href="/projekte/{esc(project.name)}">Neu laden</a></p>'
            )
        if not runs.items:
            return (
                '<p class="small muted">Noch keine Runs. <a href="/neuer-run?projekt='
                f'{esc(project.name)}">Run in diesem Projekt starten</a></p>'
            )
        now = self.api.now()
        rows = "".join(
            f"<tr><td>{run_badges_html(r)}</td><td><a href='/runs/{esc(r.id)}'>{esc(r.name)}"
            f"</a></td><td class='faint small nowrap'>{esc(fmt.when(r.created_at, now))}</td></tr>"
            for r in runs.items
        )
        return f'<table class="tbl compact"><tbody>{rows}</tbody></table>'

    async def _repair(self, name: str) -> None:
        try:
            result = await self.api.setup_project(name)
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        written = f" arr hat ergänzt: {', '.join(result.created)}." if result.created else ""
        self.frame.toast(result.message + written)
        await self._reload(name)

    async def _reload(self, name: str) -> None:
        self.name = name
        self.box.clear()
        await load_into(
            self.box,
            self.api.list_projects,
            self._render,
            retry_href=f"/projekte/{name}",
            skeleton=skeleton_html(4, shape="rows"),
        )

    # --- dialogs ---------------------------------------------------------------------------------

    def _new_dialog(self) -> None:
        fields: dict[str, Field] = {}
        path_hint: dict[str, ui.element] = {}

        def build() -> None:
            def live(value: str) -> None:
                name = value.strip() or "…"
                path_hint["el"].set_content(
                    f'Wird zu <span class="mono">{esc(self.root)}/{esc(name)}</span>. '
                    "Kleinbuchstaben, Ziffern, Binde- und Unterstriche."
                )

            fields["name"] = Field(
                "Ordnername", name="new-name", on_change=live, attrs='autocomplete="off"'
            )
            path_hint["el"] = raw("", tag="span", classes="help")
            live("")
            fields["title"] = Field(
                "Titel", name="new-title", optional="für CLAUDE.md", attrs='autocomplete="off"'
            )
            raw(f'<p class="small muted">arr legt an: {esc(NEW_FILES)}.</p>')

        async def submit() -> str | None:
            name = fields["name"].value.strip()
            if not SLUG.match(name):
                fields["name"].set_error(
                    "Nimm Kleinbuchstaben, Ziffern, Binde- und Unterstriche, höchstens 64 "
                    "Zeichen, z. B. goldbach-schwach."
                )
                return "Der Ordnername passt nicht."
            try:
                result = await self.api.create_project(
                    CreateProjectRequest(name=name, title=fields["title"].value.strip() or None)
                )
            except ApiException as exc:
                if exc.error.field == "name":
                    fields["name"].set_error(exc.error.message)
                    return "Bitte korrigiere den Ordnernamen."
                return exc.error.message
            self.frame.toast(result.message, ("Projekt öffnen", f"/projekte/{name}"))
            await self._reload(name)
            return None

        self.dialog.open("Neues Projekt", "Projekt anlegen", build, submit)

    def _setup_dialog(self) -> None:
        fields: dict[str, Field] = {}

        def build() -> None:
            raw(
                f'<p>Wähle einen Ordner unter <span class="mono">{esc(self.root)}</span>. arr '
                "ergänzt nur, was fehlt, und ändert keine deiner Dateien. Ausnahme: Hat die "
                "CLAUDE.md noch keinen arr-Block, hängt arr ihn am Ende an.</p>"
            )
            fields["path"] = Field(
                "Ordner",
                name="existing-path",
                value=f"{self.root}/",
                help_text="Tippe den Pfad. Die fertige App öffnet dafür den Ordnerdialog von "
                "macOS.",
            )

        async def submit() -> str | None:
            path = fields["path"].value.strip().rstrip("/")
            name = path.rsplit("/", 1)[-1].lower()
            if not name or not SLUG.match(name):
                fields["path"].set_error(
                    "Der Ordnername muss aus Kleinbuchstaben, Ziffern, Binde- und Unterstrichen "
                    "bestehen, höchstens 64 Zeichen."
                )
                return "Der Ordner passt nicht."
            try:
                result = await self.api.create_project(
                    CreateProjectRequest(name=name, existing_path=path)
                )
            except ApiException as exc:
                fields["path"].set_error(exc.error.message)
                return "Der Ordner lässt sich so nicht einrichten."
            added = ", ".join(result.created)
            self.frame.toast(
                f"{result.message} arr hat ergänzt: {added}." if added else result.message
            )
            await self._reload(result.project.name)
            return None

        self.dialog.open("Vorhandenen Ordner einrichten", "Einrichten", build, submit)

    async def _example(self) -> None:
        try:
            result = await self.api.create_project(
                CreateProjectRequest(
                    name="collatz", title="Collatz-Varianten", template="collatz-beispiel"
                )
            )
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        self.frame.toast(result.message, ("Projekt öffnen", "/projekte/collatz"))

    def _on_event(self, event: LiveEvent) -> None:
        """Run counters of the list follow run changes (§5.6 „live“)."""
        if event.type == "run.status" and self.projects:
            ui.timer(0.5, self._refresh_list, once=True)

    async def _refresh_list(self) -> None:
        try:
            projects = await self.api.list_projects()
        except ApiException:
            return
        self.projects = projects
        if hasattr(self, "list_box") and not self.list_box.is_deleted:
            names = [p.name for p in projects]
            selected = self.name if self.name in names else (names[0] if names else "")
            self.list_box.set_content(
                "".join(project_item_html(p, p.name == selected) for p in projects)
            )


def register() -> None:
    """Register `/projekte` and `/projekte/<name>` (docs/08 §5.6, §4.3)."""

    async def show(name: str, zustand: str, neu: str) -> None:
        api = state_api(get_api(), zustand, "projekte")
        use_screen_assets()
        title = name or "Projekte"
        frame = Frame(
            api,
            active="projekte",
            title=title,
            crumbs=[("Projekte", "/projekte"), (name, None)] if name else None,
            focus="#project-title" if name else None,
        )
        await frame.start()
        page = ProjektePage(frame, api, name=name, new=neu)
        await page.build()

    @ui.page("/projekte")
    async def projekte(zustand: str = "", neu: str = "") -> None:
        await show("", zustand, neu)

    @ui.page("/projekte/{name}")
    async def projekt(name: str, zustand: str = "", neu: str = "") -> None:
        await show(name, zustand, neu)

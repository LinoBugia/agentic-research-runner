"""Skills: look at, check, edit, duplicate and create skills, start runs from them
(docs/08 §5.7, W4-02).

Left the skills grouped by kind („Group · startbar“, „Werkzeug“, „User-Agent“) with use and
check state („⚠ n“) and „＋ Neuer Skill“, right the selected skill: tabs „Übersicht“ (flow of the
topology, parameters, agents with count/mix/when in plain words, channels, rating, „Benutzt
von“), „SKILL.md“ and „group.yaml“ (editor), „Rollen“, „Dateien“ (all files, editor, new file)
and „Prüfung“ (errors with file, line and what to do). An invalid skill cannot be started.
The editor (`SkillEditor`) saves also invalid files, shows the check right after and asks
before unsaved changes get lost.
"""

import json
import re

from nicegui import ui

from arr.models import TopologyKind
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    CreateSkillRequest,
    DuplicateSkillRequest,
    SaveSkillFileRequest,
    SkillInfo,
    SkillIssue,
    SkillKind,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, markdown_html, raw
from arr.service.gui.components.form import Field, radio
from arr.service.gui.components.form_dialog import FormDialog
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.components.params import CHOICE_LABELS, param_text
from arr.service.gui.components.skill_editor import SkillEditor, install_editor_js, issue_where
from arr.service.gui.layout import Frame, tab_title
from arr.service.gui.texts import TOPOLOGY_LABEL, Status

KIND_GROUPS = [
    (SkillKind.GROUP, "Group · startbar"),
    (SkillKind.TOOL, "Werkzeug"),
    (SkillKind.USER_AGENT, "User-Agent"),
]
KIND_TAG = {
    SkillKind.GROUP: "Group",
    SkillKind.TOOL: "Werkzeug",
    SkillKind.USER_AGENT: "User-Agent",
}
TABS = [
    ("uebersicht", "Übersicht"),
    ("skill", "SKILL.md"),
    ("yaml", "group.yaml"),
    ("rollen", "Rollen"),
    ("dateien", "Dateien"),
    ("pruefung", "Prüfung"),
]
TAB_FILES = {"skill": "SKILL.md", "yaml": "group.yaml"}
"""Tabs that are the editor of one file."""
ROLE_MISSING = re.compile(r"^Rolle (\S+) nicht gefunden\.")
ROLE_TEMPLATE = (
    "# Rolle: {name}\n\nBeschreibe hier, was dieser Agent tut, worauf er achtet und wie er sein "
    "Ergebnis abgibt (`submit_result`).\n"
)
UNSAVED = (
    "Du hast ungespeicherte Änderungen an {path}. Speichere oder verwirf sie zuerst, dann geht "
    "es hier weiter."
)

# Flow of each topology in plain words (docs/03 §4, docs/08 §6.5 table of the progress bar)
FLOW = {
    TopologyKind.EXPLORATION: (
        [
            ("Ideen", "je Explorer eine"),
            ("Exploration", "Turns je Idee"),
            ("Bewertung", "ranker"),
            ("Zusammenfassung", "summary.md"),
        ],
        None,
    ),
    TopologyKind.WATERFALL: (
        [
            ("Strategie", "strategist"),
            ("Skizze", "prover"),
            ("Beweis", "prover"),
            ("Zusammenfassung", "summary.md"),
        ],
        "Unter der Bestehensgrenze: Nacharbeit. Nach der letzten Nacharbeit entscheidest du "
        "(Noch ein Versuch, Trotzdem weiter, Run beenden).",
    ),
    TopologyKind.REVIEW: (
        [
            ("Artefakt", "Autor oder Datei"),
            ("Prüfung", "Judges unabhängig"),
            ("Diskussion", "#judges öffnet"),
            ("Urteil", "nach verdict"),
        ],
        "Nicht akzeptiert und Revisionen übrig: Der Autor überarbeitet, dann zurück zu 1.",
    ),
    TopologyKind.ROUNDTABLE: (
        [("Runde 1 … R", "alle der Reihe nach"), ("Zusammenfassung", "summary.md")],
        "Wer nichts beizutragen hat, passt. Nach der letzten Runde wird zusammengefasst.",
    ),
    TopologyKind.SOLO: ([("Arbeit", "ein Agent"), ("Ergebnis", "summary.md")], None),
}
CHECKED = Status("st-ok", "check", "geprüft")


def skill_item_html(skill: SkillInfo, selected: bool) -> str:
    """One entry of the list: name, short description, use, check state (§5.7)."""
    issues = f'<span class="badge st-warn">{icon("warning", small=True)}{len(skill.issues)}</span>'
    flag = "" if skill.valid else issues
    use = f" · {skill.used_by.runs_count} Runs" if skill.kind is SkillKind.GROUP else ""
    current = ' aria-current="page"' if selected else ""
    css = "list-item sel" if selected else "list-item"
    label = f"{skill.name}, ungültig" if not skill.valid else skill.name
    return (
        f'<a class="{css}" href="/skills/{esc(skill.name)}"{current} aria-label="{esc(label)}">'
        f'<div class="t">{esc(skill.name)}{flag}</div>'
        f'<div class="d">{esc(skill.description)}{use}</div></a>'
    )


def flow_html(topology: TopologyKind) -> str:
    stages, loop = FLOW[topology]
    first = 0 if topology in (TopologyKind.REVIEW, TopologyKind.EXPLORATION) else 1
    items = "".join(
        f'<li><span class="nm">{i + first} · {esc(name)}</span><span class="sub">{esc(sub)}</span>'
        "</li>"
        for i, (name, sub) in enumerate(stages)
    )
    note = f'<p class="small muted">{esc(loop)}</p>' if loop else ""
    label = f"Ablauf der Topologie {TOPOLOGY_LABEL[topology]}"
    return f'<ol class="skill-flow" aria-label="{esc(label)}">{items}</ol>{note}'


def params_html(skill: SkillInfo) -> str:
    if not skill.params:
        return '<p class="small muted">Keine Parameter.</p>'
    rows = []
    for p in skill.params:
        if p.choices:
            allowed = ", ".join(CHOICE_LABELS.get(str(c), str(c)) for c in p.choices)
        elif p.min is not None and p.max is not None:
            allowed = f"{fmt.threshold(p.min)}–{fmt.threshold(p.max)}"
        elif p.type == "path":
            allowed = "Pfad im Projekt"
        elif isinstance(p.default, list):
            allowed = "Liste"
        else:
            allowed = "–"
        shown = "–" if p.default in (None, [], "") else str(p.default)
        shown = CHOICE_LABELS.get(shown, shown)
        label, doc = param_text(p)
        name = f'{esc(label)}<span class="info mono">{esc(p.name)}</span>'
        if label == p.name:
            name = f'<span class="mono">{esc(p.name)}</span>'
        rows.append(
            f"<tr><td>{name}</td><td>{esc(shown)}</td><td>{esc(allowed)}</td>"
            f"<td>{esc(doc)}</td></tr>"
        )
    return (
        '<table class="tbl compact"><thead><tr><th>Name</th><th>Standard</th><th>Erlaubt</th>'
        f"<th>Bedeutung</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def agents_html(skill: SkillInfo) -> str:
    if not skill.agents_spec:
        return '<p class="small muted">Keine Agenten (Werkzeug-Skill).</p>'
    rows = []
    for a in skill.agents_spec:
        name = str(a.get("id", ""))
        count = a.get("count")
        when = a.get("when")
        extra = " · ".join(str(x) for x in (count, when) if x)
        rows.append(
            f'<tr><td class="mono">{esc(name)}</td><td><a href="?tab=rollen#'
            f'{esc(a.get("role", ""))}">{esc(a.get("role", ""))}</a></td>'
            f"<td>{esc(a.get('model', '–'))}"
            + (f'<span class="info">{esc(extra)}</span>' if extra else "")
            + "</td></tr>"
        )
    return (
        '<table class="tbl compact"><thead><tr><th>Agent</th><th>Rolle</th><th>Modell</th></tr>'
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
    )


def channels_html(skill: SkillInfo) -> str:
    if not skill.channels_spec:
        return '<p class="small muted">Keine Gruppenchats.</p>'
    lines = []
    for c in skill.channels_spec:
        parts = [str(c.get("members", "")), str(c.get("mode", ""))]
        if c.get("anonymous"):
            parts.append("anonym")
        if c.get("open_from"):
            parts.append(f"offen ab „{c['open_from']}“")
        lines.append(
            f'<div><span class="mono">#{esc(c.get("name", ""))}</span> · '
            f"{esc(' · '.join(p for p in parts if p))}</div>"
        )
    return f'<div class="small stack">{"".join(lines)}</div>'


def file_href(skill: str, path: str) -> str:
    """Where the editor of `path` lives: its own tab for SKILL.md and group.yaml."""
    for tab, file in TAB_FILES.items():
        if file == path:
            return f"/skills/{skill}?tab={tab}"
    return f"/skills/{skill}?tab=dateien&datei={path}"


def issue_html(skill: str, issue: SkillIssue) -> str:
    """One problem of the check: where (link to the file's editor) and what to do."""
    return (
        '<div class="check"><span class="st bad" aria-hidden="true">✕</span><span class="txt">'
        f'<a class="b" href="{esc(file_href(skill, issue.file))}">{esc(issue_where(issue))}</a>'
        f"<small>{esc(issue.message)}</small></span></div>"
    )


class SkillsPage:
    """State of one Skills page (one browser tab)."""

    def __init__(
        self, frame: Frame, api: ApiClient, *, name: str, tab: str, datei: str = ""
    ) -> None:
        self.frame = frame
        self.api = api
        self.name = name
        self.datei = datei
        if datei and not tab:
            tab = "dateien"
        self.tab = tab if tab in dict(TABS) else "uebersicht"
        self.editor: SkillEditor | None = None
        self.skill: SkillInfo | None = None
        self.renders = 0

    async def build(self) -> None:
        with self.frame.content:
            self.dialog = FormDialog()
            self.box = ui.element("div")
        await self._load()

    async def _load(self) -> None:
        self.renders += 1  # the title of this render gets the focus after `_open`
        self.box.clear()
        self.editor = None
        await load_into(
            self.box,
            self.api.list_skills,
            self._render,
            retry_href=clean_href(f"/skills/{self.name}" if self.name else "/skills"),
            skeleton=skeleton_html(6, shape="rows", reason="Die Skills werden geprüft …"),
        )

    def _unsaved(self) -> str | None:
        """The message for actions that would replace the editor while it has changes."""
        if self.editor is not None and self.editor.dirty:
            return UNSAVED.format(path=self.editor.path)
        return None

    async def _open(self, name: str, tab: str = "uebersicht", datei: str = "") -> None:
        """Show another skill in place (after creating or duplicating), with its URL."""
        self.name, self.tab, self.datei = name, tab, datei
        url = f"/skills/{name}" + (f"?tab={tab}" if tab != "uebersicht" else "")
        if datei:
            url = f"/skills/{name}?tab=dateien&datei={datei}"
        ui.navigate.history.push(url)
        self.frame.title = name
        ui.page_title(tab_title(name, self.frame.counts.waiting))
        await self._load()
        # breadcrumb „Skills › <name>“ of the new skill, and the focus on its title (§7.7)
        ui.run_javascript(
            "(() => { const c = document.querySelector('.topbar .crumbs'); if (!c) return; "
            "c.replaceChildren(); const a = document.createElement('a'); a.href = '/skills'; "
            "a.textContent = 'Skills'; const sep = document.createElement('span'); "
            "sep.className = 'sep'; sep.setAttribute('aria-hidden', 'true'); "
            "sep.textContent = '›'; const n = document.createElement('span'); "
            f"n.textContent = {json.dumps(name)}; c.append(a, sep, n); }})();"
            # after the dialog has closed and given the focus back (arr.js), Vue may still
            # replace the new title once: focus it a moment later
            f"setTimeout(() => arr.whenEl('#skill-title[data-render=\"{self.renders}\"]', "
            "(h) => h.focus()), 300);"
        )

    async def _render(self, skills: list[SkillInfo]) -> None:
        if not skills:
            raw('<div class="page-head"><h1 tabindex="-1">Skills</h1></div>')
            raw(
                empty_state_html(
                    "auto_awesome",
                    "Noch keine Skills",
                    "Die Start-Skills lassen sich mit einem Klick anlegen."
                    if self._prototype
                    else "Lege einen Skill aus einer Vorlage an. Die Start-Skills kommen mit "
                    "Auftrag W4-05.",
                    [],
                )
            )
            with (
                ui.element("div").classes("btn-row").style("justify-content:center;margin-top:12px")
            ):
                button("Neuer Skill", self._new_dialog, classes="btn primary").mark("new-skill")
                button("Start-Skills anlegen", self._create_start_skills, classes="btn").mark(
                    "start-skills"
                )
            return
        names = [s.name for s in skills]
        selected = self.name if self.name in names else names[0]
        with ui.element("div").classes("split"):
            with ui.element("nav").classes("subnav").props('aria-label="Skills"'):
                button("＋ Neuer Skill", self._new_dialog, classes="btn sk-new").mark("new-skill")
                self.list_box = raw("", tag="div", classes="list")
            self.detail = ui.element("div")
        self._render_list(skills, selected)
        if self.name and self.name not in names:
            with self.detail:
                raw(
                    f'<div class="banner err" role="alert">{icon("error")}<div class="txt">'
                    f"<b>Skill „{esc(self.name)}“ nicht gefunden.</b> Wähle links einen "
                    "Skill.</div></div>"
                )
            return
        skill = next(s for s in skills if s.name == selected)
        with self.detail:
            await self._render_detail(skill)

    def _render_list(self, skills: list[SkillInfo], selected: str) -> None:
        parts = []
        for kind, title in KIND_GROUPS:
            group = [s for s in skills if s.kind is kind]
            if not group:
                continue
            parts.append(f'<div class="grp">{esc(title)}</div>')
            parts += [skill_item_html(s, s.name == selected) for s in group]
        self.list_box.set_content("".join(parts))

    async def _refresh(self, skill: SkillInfo) -> None:
        """After saving: list badge, head and tabs show the new check; the editor stays."""
        self.skill = skill
        try:
            skills = await self.api.list_skills()
        except ApiException:
            skills = []
        if skills:
            self._render_list(skills, skill.name)
        self._render_head(skill)
        self._render_tabs(skill)

    async def _render_detail(self, skill: SkillInfo) -> None:
        self.skill = skill
        self.head = ui.element("div")
        self._render_head(skill)
        self.tabs = raw("", tag="div")
        self._render_tabs(skill)
        if self.tab == "uebersicht":
            self._overview(skill)
        elif self.tab == "pruefung":
            self._checks(skill)
        elif self.tab == "rollen":
            await self._roles(skill)
        elif self.tab == "dateien":
            await self._files(skill)
        else:
            await self._edit(skill, TAB_FILES[self.tab])

    def _render_head(self, skill: SkillInfo) -> None:
        status = (
            badge_html(CHECKED)
            if skill.valid
            else badge_html(Status("st-warn", "warning", f"{len(skill.issues)} Fehler"))
        )
        self.head.clear()
        with self.head:
            with ui.element("div").classes("page-head").style("margin-bottom:8px"):
                raw(
                    f'<h1 id="skill-title" tabindex="-1" data-render="{self.renders}">'
                    f"{esc(skill.name)}</h1>"
                    f'<span class="tag">{esc(KIND_TAG[skill.kind])}</span>{status}',
                    tag="div",
                    classes="row",
                )
                with ui.element("div").classes("actions"):
                    button("Duplizieren …", self._duplicate_dialog).mark("duplicate")
                    if skill.kind is SkillKind.GROUP:
                        if skill.valid:
                            raw(
                                f'<a class="btn primary" href="/neuer-run?skill={esc(skill.name)}"'
                                f' data-mark="run-with-skill">{icon("add")}Run mit diesem Skill'
                                "</a>",
                                tag="span",
                            )
                        else:
                            raw(
                                '<button type="button" class="btn primary" disabled '
                                'aria-disabled="true" aria-describedby="skill-blocked" '
                                'data-mark="run-with-skill" title="Behebe zuerst die Fehler '
                                f'unter „Prüfung“.">{icon("add")}Run mit diesem Skill</button>',
                                tag="span",
                            )
            raw(f'<p class="muted">{esc(skill.description)}</p>')
            if not skill.valid:
                first = skill.issues[0] if skill.issues else None
                where = f"{issue_where(first)}: " if first else ""
                more = f" (und {len(skill.issues) - 1} weitere)" if len(skill.issues) > 1 else ""
                raw(
                    '<div class="banner err banner-sm" role="alert" id="skill-blocked" '
                    f'style="margin-bottom:12px">{icon("error")}<div class="txt"><b>{esc(where)}'
                    f"{esc(first.message if first else 'Prüfung fehlgeschlagen.')}{esc(more)}</b>"
                    "Runs mit diesem Skill lassen sich erst nach der Korrektur starten. "
                    f'<a href="/skills/{esc(skill.name)}?tab=pruefung">Alle Fehler ansehen</a>'
                    "</div></div>"
                )

    def _render_tabs(self, skill: SkillInfo) -> None:
        tabs = []
        for key, label in TABS:
            if key == "yaml" and "group.yaml" not in skill.files:
                continue
            count = ""
            if key == "rollen":
                roles = [f for f in skill.files if f.startswith("roles/")]
                if not roles:
                    continue
                count = f' <span class="n">{len(roles)}</span>'
            if key == "dateien":
                count = f' <span class="n">{len(skill.files)}</span>'
            if key == "pruefung" and skill.issues:
                count = f' <span class="n">{len(skill.issues)}</span>'
            current = ' class="active" aria-current="page"' if key == self.tab else ""
            tabs.append(
                f'<a{current} href="/skills/{esc(skill.name)}?tab={key}">{esc(label)}{count}</a>'
            )
        self.tabs.set_content(
            '<nav class="tabs" aria-label="Ansicht" style="margin-bottom:12px">'
            f"{''.join(tabs)}</nav>"
        )

    def _overview(self, skill: SkillInfo) -> None:
        if skill.kind is not SkillKind.GROUP:
            raw(
                '<p class="small muted">Werkzeug- und User-Agent-Skills haben keinen Ablauf. '
                "Agenten bzw. Claude Code laden sie bei Bedarf. Den Inhalt zeigt und bearbeitet "
                "der Tab „SKILL.md“.</p>"
            )
            return
        used = skill.used_by
        now = self.api.now()
        if used.runs_count:
            last = f", zuletzt {fmt.when(used.last_run_at, now)}" if used.last_run_at else ""
            projects = ", ".join(used.projects)
            used_text = f"{used.runs_count} Runs in {projects}{last}"
        else:
            used_text = "noch in keinem Run"
        rubric = skill.rubric or {}
        rubric_text = (
            f"Skala {rubric.get('scale', '–')} · bestanden ab {rubric.get('pass_score', '–')} · "
            f"Kriterien: {', '.join(rubric.get('criteria', []))}"
            if rubric
            else "keine Bewertung"
        )
        if skill.topology is None:
            raw(
                '<p class="small muted">Die group.yaml lässt sich nicht laden, deshalb fehlt die '
                "Übersicht. Die Fehler stehen unter „Prüfung“.</p>"
            )
            return
        topology = skill.topology
        raw(
            '<div class="grid-2e"><div>'
            f"<h3>Ablauf (Topologie {esc(TOPOLOGY_LABEL[topology])})</h3>{flow_html(topology)}"
            f'<h3 style="margin-top:12px">Parameter</h3>{params_html(skill)}</div>'
            f"<div><h3>Agenten</h3>{agents_html(skill)}"
            f'<h3 style="margin-top:12px">Gruppenchats</h3>{channels_html(skill)}'
            f'<h3 style="margin-top:12px">Bewertung</h3><div class="small">{esc(rubric_text)}</div>'
            f'<h3 style="margin-top:12px">Benutzt von</h3><div class="small">{esc(used_text)}</div>'
            "</div></div>"
        )

    def _checks(self, skill: SkillInfo) -> None:
        with ui.element("div").classes("btn-row").style("margin-bottom:8px"):
            button("Erneut prüfen", self._recheck).mark("recheck")
        if not skill.issues:
            raw(
                '<div class="check"><span class="st ok" aria-hidden="true">✓</span><span '
                'class="txt">Keine Fehler. Alle Dateien und Rollen sind da.</span></div>'
            )
            return
        for issue in skill.issues:
            raw(issue_html(skill.name, issue))
            match = ROLE_MISSING.match(issue.message)
            if match and issue.file == "group.yaml":
                role = match[1]
                with ui.element("div").classes("ed-fix"):
                    button(
                        f"{role} anlegen",
                        lambda role=role: self._create_role(skill, role),
                        classes="btn small",
                    ).mark(f"create-role-{role}")

    async def _recheck(self) -> None:
        await self._load()
        self.frame.toast("Skill erneut geprüft.")

    async def _create_role(self, skill: SkillInfo, role: str) -> None:
        name = role.rsplit("/", 1)[-1].removesuffix(".md")
        try:
            await self.api.save_skill_file(
                skill.name, role, SaveSkillFileRequest(content=ROLE_TEMPLATE.format(name=name))
            )
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        self.frame.toast(f"{role} angelegt. Beschreibe jetzt die Rolle.")
        await self._open(skill.name, "dateien", role)

    async def _edit(self, skill: SkillInfo, path: str) -> None:
        if path not in skill.files:
            raw(
                '<p class="small muted">Diese Datei gibt es in diesem Skill nicht. Lege sie unter '
                "„Dateien“ an.</p>"
            )
            return
        editor = SkillEditor(
            self.api, skill=skill.name, path=path, on_saved=self._refresh, toast=self.frame.toast
        )
        if await editor.build():
            self.editor = editor
            editor.render_checks(skill)

    async def _files(self, skill: SkillInfo) -> None:
        with ui.element("div").classes("sk-files"):
            with ui.element("div").classes("sk-file-list"):
                links = []
                for path in skill.files:
                    current = ' aria-current="page" class="sel"' if path == self.datei else ""
                    links.append(
                        f'<li><a{current} href="{esc(file_href(skill.name, path))}">'
                        f'<span class="mono small">{esc(path)}</span></a></li>'
                    )
                raw(
                    f'<ul class="plain" aria-label="Dateien des Skills">{"".join(links)}</ul>'
                    if links
                    else '<p class="small muted">Keine Dateien.</p>'
                )
                button("＋ Neue Datei", lambda: self._new_file_dialog(skill)).mark("new-file")
            with ui.element("div"):
                if not self.datei:
                    raw(
                        '<p class="small muted">Wähle links eine Datei. Bearbeiten lassen sich '
                        "Textdateien bis 256 KB; Links, Pipes und andere Sonderdateien öffnet "
                        "der Editor nicht.</p>"
                    )
                elif self.datei not in skill.files:
                    raw(
                        f'<p class="small">Die Datei „{esc(self.datei)}“ gibt es in diesem Skill '
                        "nicht.</p>"
                    )
                else:
                    await self._edit(skill, self.datei)

    async def _roles(self, skill: SkillInfo) -> None:
        for path in [f for f in skill.files if f.startswith("roles/")]:
            try:
                file = await self.api.get_skill_file(skill.name, path)
            except ApiException as exc:
                raw(
                    f'<section class="card" style="margin-bottom:12px"><h3 class="mono">'
                    f'{esc(path)}</h3><p class="small">{esc(exc.error.message)}</p></section>'
                )
                continue
            raw(
                f'<section class="card" id="{esc(path)}" style="margin-bottom:12px">'
                f'<div class="card-head"><h3 class="mono">{esc(path)}</h3>'
                f'<a class="btn small" href="{esc(file_href(skill.name, path))}">Bearbeiten</a>'
                f'</div><div class="prose small">{markdown_html(file.content)}</div></section>'
            )

    # -- dialogs -------------------------------------------------------------------------------

    def _new_file_dialog(self, skill: SkillInfo) -> None:
        if (blocked := self._unsaved()) is not None:
            self.frame.toast(blocked, error=True)
            return
        fields: dict[str, Field] = {}

        def build() -> None:
            fields["path"] = Field(
                "Pfad im Skill",
                name="new-file",
                value="roles/",
                help_text="Relativ zum Skill, z. B. roles/kritiker.md. Fehlende Ordner legt arr "
                "an.",
                attrs='autocomplete="off"',
            )

        async def submit() -> str | None:
            path = fields["path"].value.strip()
            name = path.rsplit("/", 1)[-1].removesuffix(".md")
            content = ROLE_TEMPLATE.format(name=name) if path.startswith("roles/") else ""
            try:
                await self.api.save_skill_file(
                    skill.name, path, SaveSkillFileRequest(content=content)
                )
            except ApiException as exc:
                fields["path"].set_error(exc.error.message)
                return "Die Datei wurde nicht angelegt."
            self.frame.toast(f"{path} angelegt.")
            await self._open(skill.name, "dateien", path)
            return None

        self.dialog.open(f"Neue Datei in {skill.name}", "Datei anlegen", build, submit)

    def _duplicate_dialog(self) -> None:
        skill = self.skill
        if skill is None:
            return
        if (blocked := self._unsaved()) is not None:
            self.frame.toast(blocked, error=True)
            return
        fields: dict[str, Field] = {}

        def build() -> None:
            raw(
                f"<p>arr kopiert alle Dateien von „{esc(skill.name)}“ in einen neuen Skill und "
                "setzt dort den Namen in der SKILL.md. Links und Sonderdateien kopiert arr nicht."
                "</p>"
            )
            fields["name"] = Field(
                "Name des neuen Skills",
                name="duplicate-name",
                value=f"{skill.name}-kopie",
                help_text="Kleinbuchstaben, Ziffern und „-“, z. B. proof-review-streng.",
                attrs='autocomplete="off"',
            )

        async def submit() -> str | None:
            name = fields["name"].value.strip()
            try:
                result = await self.api.duplicate_skill(
                    skill.name, DuplicateSkillRequest(name=name)
                )
            except ApiException as exc:
                fields["name"].set_error(exc.error.message)
                return "Der Skill wurde nicht dupliziert."
            self.frame.toast(result.message)
            await self._open(result.skill.name)
            return None

        self.dialog.open(f"Skill „{skill.name}“ duplizieren", "Duplizieren", build, submit)

    def _new_dialog(self) -> None:
        if (blocked := self._unsaved()) is not None:
            self.frame.toast(blocked, error=True)
            return
        fields: dict[str, Field] = {}
        kind = {"value": "group"}

        def build() -> None:
            fields["name"] = Field(
                "Name",
                name="skill-name",
                help_text="Kleinbuchstaben, Ziffern und „-“, z. B. beweis-check. Er ist auch der "
                "Ordnername.",
                attrs='autocomplete="off"',
            )
            raw('<div class="small" style="font-weight:600;margin:8px 0 4px">Art</div>')
            with ui.element("div").classes("stack").props('role="radiogroup" aria-label="Art"'):
                for value, title, sub in (
                    (
                        "group",
                        "Group-Skill",
                        "Startbare Research-Group: SKILL.md, group.yaml (Topologie roundtable, "
                        "zwei Agenten) und eine Rolle",
                    ),
                    ("tool", "Werkzeug-Skill", "Nur eine SKILL.md, für Agenten und Claude Code"),
                ):
                    with ui.element("label").classes("radio-card"):
                        radio(
                            "new-skill-kind",
                            value,
                            checked=value == kind["value"],
                            on_change=lambda v: kind.update(value=v),
                        )
                        raw(
                            f"<span><b>{esc(title)}</b><span class='small muted'>{esc(sub)}"
                            "</span></span>",
                            tag="span",
                        )
            fields["description"] = Field(
                "Beschreibung",
                name="skill-description",
                help_text="Ein Satz: wofür der Skill da ist und wann man ihn nutzt.",
                attrs='autocomplete="off"',
            )

        async def submit() -> str | None:
            name = fields["name"].value.strip()
            request = CreateSkillRequest(
                name=name,
                kind="group" if kind["value"] == "group" else "tool",
                description=fields["description"].value.strip(),
            )
            try:
                result = await self.api.create_skill(request)
            except ApiException as exc:
                fields["name"].set_error(exc.error.message)
                return "Der Skill wurde nicht angelegt."
            self.frame.toast(result.message)
            await self._open(result.skill.name, "skill")
            return None

        self.dialog.open("Neuer Skill", "Skill anlegen", build, submit)

    @property
    def _prototype(self) -> bool:
        return hasattr(getattr(self.api, "base", self.api), "datasets")

    def _create_start_skills(self) -> None:
        if not self._prototype:
            self.frame.toast("Die Start-Skills kommen mit Auftrag W4-05.")
            return
        self.frame.toast(
            "Start-Skills angelegt: exploration, proof, proof-review, roundtable, sympy-check, "
            "latex-writeup, arr (im Prototyp simuliert)."
        )
        ui.navigate.to("/skills")


def register() -> None:
    """Register `/skills` and `/skills/<name>` (docs/08 §5.7, §4.3)."""

    async def show(name: str, tab: str, zustand: str, datei: str = "") -> None:
        api = state_api(get_api(), zustand)
        use_screen_assets()
        install_editor_js()
        frame = Frame(
            api,
            active="skills",
            title=name or "Skills",
            crumbs=[("Skills", "/skills"), (name, None)] if name else None,
            focus="#skill-title" if name else None,
        )
        await frame.start()
        await SkillsPage(frame, api, name=name, tab=tab, datei=datei).build()

    @ui.page("/skills")
    async def skills(tab: str = "", zustand: str = "") -> None:
        await show("", tab, zustand)

    @ui.page("/skills/{name}")
    async def skill(name: str, tab: str = "", zustand: str = "", datei: str = "") -> None:
        await show(name, tab, zustand, datei)

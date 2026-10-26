"""Skills: look at and check skills, start runs from them; read only in v1 (docs/08 §5.7).

Left the skills grouped by kind („Group · startbar“, „Werkzeug“, „User-Agent“) with use and
check state, right the selected skill: tabs „Übersicht“ (flow of the topology, parameters,
agents with count/mix/when in plain words, channels, rating, „Benutzt von“), „SKILL.md“,
„group.yaml“, „Rollen“ and „Prüfung“ (errors with file and line). An invalid skill cannot be
started. Editing, duplicating and new skills come with W4-02.
"""

from nicegui import ui

from arr.models import TopologyKind
from arr.service.api_client import ApiClient
from arr.service.api_models import ApiException, SkillInfo, SkillKind
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, markdown_html, raw
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.components.params import CHOICE_LABELS, param_text
from arr.service.gui.layout import Frame
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
    ("pruefung", "Prüfung"),
]

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


class SkillsPage:
    """State of one Skills page (one browser tab)."""

    def __init__(self, frame: Frame, api: ApiClient, *, name: str, tab: str) -> None:
        self.frame = frame
        self.api = api
        self.name = name
        self.tab = tab if tab in dict(TABS) else "uebersicht"

    async def build(self) -> None:
        with self.frame.content:
            self.box = ui.element("div")
        await load_into(
            self.box,
            self.api.list_skills,
            self._render,
            retry_href=clean_href(f"/skills/{self.name}" if self.name else "/skills"),
            skeleton=skeleton_html(6, shape="rows", reason="Die Skills werden geprüft …"),
        )

    async def _render(self, skills: list[SkillInfo]) -> None:
        if not skills:
            raw('<div class="page-head"><h1 tabindex="-1">Skills</h1></div>')
            raw(
                empty_state_html(
                    "auto_awesome",
                    "Noch keine Skills",
                    "Die Start-Skills lassen sich mit einem Klick anlegen.",
                    [],
                )
            )
            with (
                ui.element("div").classes("btn-row").style("justify-content:center;margin-top:12px")
            ):
                button("Start-Skills anlegen", self._create_start_skills, classes="btn primary")
            return
        names = [s.name for s in skills]
        selected = self.name if self.name in names else names[0]
        with ui.element("div").classes("split"):
            with ui.element("nav").classes("subnav").props('aria-label="Skills"'):
                parts = []
                for kind, title in KIND_GROUPS:
                    group = [s for s in skills if s.kind is kind]
                    if not group:
                        continue
                    parts.append(f'<div class="grp">{esc(title)}</div>')
                    parts += [skill_item_html(s, s.name == selected) for s in group]
                raw("".join(parts), tag="div", classes="list")
                raw(
                    '<p class="small muted" style="margin-top:12px">Bearbeiten, Duplizieren und '
                    "neue Skills kommen in einer späteren Version.</p>"
                )
            self.detail = ui.element("div")
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

    async def _render_detail(self, skill: SkillInfo) -> None:
        status = (
            badge_html(CHECKED)
            if skill.valid
            else badge_html(Status("st-warn", "warning", f"{len(skill.issues)} Fehler"))
        )
        with ui.element("div").classes("page-head").style("margin-bottom:8px"):
            raw(
                f'<h1 id="skill-title" tabindex="-1">{esc(skill.name)}</h1>'
                f'<span class="tag">{esc(KIND_TAG[skill.kind])}</span>{status}',
                tag="div",
                classes="row",
            )
            if skill.kind is SkillKind.GROUP:
                with ui.element("div").classes("actions"):
                    if skill.valid:
                        raw(
                            f'<a class="btn primary" href="/neuer-run?skill={esc(skill.name)}">'
                            f"{icon('add')}Run mit diesem Skill</a>",
                            tag="span",
                        )
                    else:
                        raw(
                            '<button type="button" class="btn primary" disabled '
                            'aria-disabled="true" title="Behebe zuerst die Fehler unter '
                            f'„Prüfung“.">{icon("add")}Run mit diesem Skill</button>',
                            tag="span",
                        )
        raw(f'<p class="muted">{esc(skill.description)}</p>')
        if not skill.valid:
            first = skill.issues[0] if skill.issues else None
            where = f"{first.file}, Zeile {first.line}: " if first and first.line else ""
            raw(
                f'<div class="banner err banner-sm" role="alert" style="margin-bottom:12px">'
                f'{icon("error")}<div class="txt"><b>{esc(where)}'
                f"{esc(first.message if first else 'Prüfung fehlgeschlagen.')}</b>"
                "Runs mit diesem Skill lassen sich erst nach der Korrektur starten.</div></div>"
            )
        tabs = []
        for key, label in TABS:
            if key == "yaml" and skill.kind is not SkillKind.GROUP:
                continue
            count = ""
            if key == "rollen":
                roles = [f for f in skill.files if f.startswith("roles/")]
                if not roles:
                    continue
                count = f' <span class="n">{len(roles)}</span>'
            if key == "pruefung" and skill.issues:
                count = f' <span class="n">{len(skill.issues)}</span>'
            current = ' class="active" aria-current="page"' if key == self.tab else ""
            tabs.append(
                f'<a{current} href="/skills/{esc(skill.name)}?tab={key}">{esc(label)}{count}</a>'
            )
        raw(
            '<nav class="tabs" aria-label="Ansicht" style="margin-bottom:12px">'
            f"{''.join(tabs)}</nav>"
        )
        if self.tab == "uebersicht":
            self._overview(skill)
        elif self.tab == "pruefung":
            self._checks(skill)
        elif self.tab == "rollen":
            await self._roles(skill)
        else:
            await self._file(skill, "SKILL.md" if self.tab == "skill" else "group.yaml")

    def _overview(self, skill: SkillInfo) -> None:
        if skill.kind is not SkillKind.GROUP:
            raw(
                '<p class="small muted">Werkzeug- und User-Agent-Skills haben keinen Ablauf. '
                "Agenten bzw. Claude Code laden sie bei Bedarf. Den Inhalt zeigt der Tab "
                "„SKILL.md“.</p>"
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
        topology = skill.topology or TopologyKind.SOLO
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
        if not skill.issues:
            raw(
                '<div class="check"><span class="st ok" aria-hidden="true">✓</span><span '
                'class="txt">Keine Fehler. Alle Dateien und Rollen sind da.</span></div>'
            )
            return
        rows = "".join(
            f'<div class="check"><span class="st bad" aria-hidden="true">✕</span><span class="txt">'
            f"<b>{esc(i.file)}{', Zeile ' + str(i.line) if i.line else ''}</b>"
            f"<small>{esc(i.message)}</small></span></div>"
            for i in skill.issues
        )
        raw(rows)

    async def _file(self, skill: SkillInfo, path: str) -> None:
        if path not in skill.files:
            raw('<p class="small muted">Diese Datei gibt es in diesem Skill nicht.</p>')
            return
        try:
            file = await self.api.get_skill_file(skill.name, path)
        except ApiException as exc:
            raw(f'<p class="small">{esc(exc.error.message)}</p>')
            return
        raw(
            f'<div class="card-head"><span class="mono small">{esc(skill.name)}/{esc(path)}</span>'
            f'<span class="faint small">{esc(fmt.size(file.size))} · nur lesen</span></div>'
        )
        if file.language == "markdown":
            body = file.content
            if body.startswith("---\n"):
                body = body.split("\n---\n", 1)[-1]
            raw(f'<div class="card prose">{markdown_html(body)}</div>')
            raw(
                '<details style="margin-top:8px"><summary class="small">Quelltext</summary>'
                f'<pre class="cmd file-view">{esc(file.content)}</pre></details>'
            )
            return
        raw(f'<pre class="cmd file-view">{esc(file.content)}</pre>')

    async def _roles(self, skill: SkillInfo) -> None:
        for path in [f for f in skill.files if f.startswith("roles/")]:
            try:
                file = await self.api.get_skill_file(skill.name, path)
            except ApiException as exc:
                raw(f'<p class="small">{esc(exc.error.message)}</p>')
                continue
            raw(
                f'<section class="card" id="{esc(path)}" style="margin-bottom:12px">'
                f'<h3 class="mono">{esc(path)}</h3><div class="prose small">'
                f"{markdown_html(file.content)}</div></section>"
            )

    def _create_start_skills(self) -> None:
        self.frame.toast(
            "Start-Skills angelegt: exploration, proof, proof-review, roundtable, sympy-check, "
            "latex-writeup, arr (im Prototyp simuliert)."
        )
        ui.navigate.to("/skills")


def register() -> None:
    """Register `/skills` and `/skills/<name>` (docs/08 §5.7, §4.3)."""

    async def show(name: str, tab: str, zustand: str) -> None:
        api = state_api(get_api(), zustand)
        use_screen_assets()
        frame = Frame(
            api,
            active="skills",
            title=name or "Skills",
            crumbs=[("Skills", "/skills"), (name, None)] if name else None,
            focus="#skill-title" if name else None,
        )
        await frame.start()
        await SkillsPage(frame, api, name=name, tab=tab).build()

    @ui.page("/skills")
    async def skills(tab: str = "", zustand: str = "") -> None:
        await show("", tab, zustand)

    @ui.page("/skills/{name}")
    async def skill(name: str, tab: str = "", zustand: str = "") -> None:
        await show(name, tab, zustand)

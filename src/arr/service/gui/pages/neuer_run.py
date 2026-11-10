"""Neuer Run: configure and start a run; also „Anpassen“ of a run start (docs/08 §5.4).

One long page with numbered sections and a fixed summary on the right: project and skill,
task, parameters from `group.yaml`, resolved agents with web and read folders per agent (E19),
channels, limits and decider, `run.yaml` preview. Every change is checked like `estimate_run`:
a choice at once, typed text when you leave the field or start (W3-08 U1, not on every
keystroke). Errors stand at the field and in the check list with a jump link, „Run starten“ is
locked while one is open; after unchecked typing it stays clickable and checks first.

`/neuer-run?skill=…&projekt=…` preselects; `/neuer-run?von=<approval>` adjusts a run start of
Claude Code: same run ID, values from the request, your changes marked, „Freigeben und
starten“ stores the decision as `adjust` with `changes` (docs/08 §5.2).

`/neuer-run?wie=<run>` repeats a run („Wiederholen“, W3-10 V8; „Neu starten“ of §8.4): project,
skill, parameters, task, the changes of the agents, limits and decider of that run, the name
with „(Wiederholung)“. A project, skill or profile that is gone stays in the form and shows
its error at the field (§8.4); nothing starts before „Run starten“.
"""

import re
from typing import Any

from nicegui import ui
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from arr.models import Decider
from arr.permissions.types import ApprovalStatus, ApprovalType, DecisionAction
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    REPEAT_SUFFIX,
    Action,
    ApiException,
    ApprovalItem,
    DecisionRequest,
    Deviation,
    Estimate,
    FieldIssue,
    ParamInfo,
    ProfileInfo,
    ProjectInfo,
    ResolveResult,
    RunFilter,
    RunInputAgent,
    RunInputs,
    RunStartAgent,
    SkillInfo,
    SkillKind,
    Vendor,
    WorkfolderSettingsInfo,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, markdown_html, raw, set_disabled
from arr.service.gui.components.form import (
    Field,
    NativeInput,
    auto_focus_js,
    checkbox,
    on_value,
    radio,
)
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.components.params import CHOICE_LABELS, param_label, param_text
from arr.service.gui.layout import Frame
from arr.service.gui.texts import DECIDER, VENDOR

CHECK_DELAY = 0.5
"""Checks run this long after the last change (docs/08 §5.4 „entprellt 500 ms“)."""

TASK_TEMPLATE = (
    "# Aufgabe\n\n## Aussage / Forschungsfrage\n\n\n## Kontext\n\n\n"
    "## Erwartetes Ergebnis\n\n\n## Vorgaben\n\n"
)
MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("gpt-5.5", "gpt-5.5 (ChatGPT)"),
    ("gpt-5.5-mini", "gpt-5.5-mini (ChatGPT)"),
]
DECIDER_CHOICES = [
    (Decider.GATEKEEPER_THEN_USER.value, "Aufsichts-Agent, bei Zweifel du"),
    (Decider.USER.value, "immer du"),
    (Decider.GATEKEEPER.value, "nur Aufsichts-Agent"),
]
WORKSPACE = {"copy": "Kopie", "shared": "gemeinsam", "empty": "leer · liest Output"}
NO_SKILL = ""
NO_FIELD = "profile"
"""Path of a check whose way out is a link to another page, not a field of this form."""
EXAMPLE_TASK = (
    "# Aufgabe\n\n## Aussage / Forschungsfrage\nWie verteilen sich die Stoppzeiten der "
    "Collatz-Abbildung für $n < 10^6$?\n\n## Kontext\nnotes/stand.md\n\n"
    "## Erwartetes Ergebnis\nEine Tabelle und eine Einschätzung in wenigen Sätzen.\n\n"
    "## Vorgaben\nRechnungen in Python, keine Webzugriffe.\n"
)
LIMIT_KEYS = ("wall_time_s", "turns_per_agent", "budget_usd")


def task_has_content(text: str) -> bool:
    """True when the task has more than the headings of the template."""
    return any(line.strip() and not line.lstrip().startswith("#") for line in text.splitlines())


def parse_duration(text: str) -> int | None:
    """„3 h“, „1,5 h“, „90 min“, „2“ (hours) → seconds; None when it is no duration."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(h|std\.?|stunden?|min|minuten?)?\s*", text, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "h").lower()
    seconds = value * 60 if unit.startswith("min") else value * 3600
    return int(seconds) if seconds > 0 else None


def duration_text(seconds: int | None) -> str:
    """Seconds as the field shows them: „2 h“, „1,5 h“, „90 min“."""
    if not seconds:
        return ""
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 1800 == 0:
        return f"{fmt.decimal(seconds / 3600, 1)} h"
    return f"{seconds // 60} min"


def parse_money(text: str) -> float | None:
    """„20 $“, „12,50“ → 20.0, 12.5; None when it is no amount."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*\$?\s*", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value if value > 0 else None


def money_text(usd: float | None) -> str:
    """An amount as the field shows it: „20 $“."""
    return "" if usd is None else fmt.budget(usd)


def _category(path: str) -> str:
    """Group deviations of the same kind (web, read folders, a limit) for the comparison."""
    if path.endswith("tools.web"):
        return "web"
    if "read_dirs" in path:
        return "read_dirs"
    return path


def deviation_text(dev: Deviation) -> str:
    """„Web-Zugriff an für judge-1, judge-2“ · „Turns pro Agent 60 (Standard 40)“."""
    text = dev.label + (f" {dev.value_text}" if dev.value_text else "")
    if not dev.value_text or dev.path.startswith(("limits", "decider")):
        text += f" (Standard {dev.default_text})"
    return text


def providers_text(dev: Deviation) -> str:
    """„Inhalte gehen an Anthropic (author, judge-1) und OpenAI (judge-2)“ (E19)."""
    if not dev.providers:
        return ""
    parts = [f"{VENDOR[p.vendor]} ({', '.join(p.agents)})" for p in dev.providers]
    return "Inhalte gehen an " + fmt.join_de(parts)


class NeuerRunPage:
    """State of one „Neuer Run“ page (one browser tab)."""

    def __init__(
        self,
        frame: Frame,
        api: ApiClient,
        *,
        skill: str,
        project: str,
        von: str,
        wie: str,
        zustand: str,
        example: bool = False,
    ) -> None:
        self.frame = frame
        self.api = api
        self.zustand = zustand
        self.von = von
        self.wie = wie
        self.example = example
        self.template: dict[str, Any] = {}
        self.want_skill = skill
        self.want_project = project
        self.item: ApprovalItem | None = None
        self.repeat_of: RunInputs | None = None
        self.skills: dict[str, SkillInfo] = {}
        self.projects: list[ProjectInfo] = []
        self.profiles: list[ProfileInfo] = []
        self.settings: WorkfolderSettingsInfo | None = None
        self.input: dict[str, Any] = {}
        self.original: dict[str, Any] = {}
        self.result: ResolveResult | None = None
        self.fields: dict[str, Field] = {}
        self.param_fields: dict[str, Any] = {}
        self.agent_cells: dict[str, dict[str, ui.element]] = {}
        self.agent_errors: dict[str, ui.html] = {}
        self._agents_signature: tuple[Any, ...] = ()
        self._timer: ui.timer | None = None
        self._checking = False
        self._unchecked = False  # text typed since the last check started (U1)
        self._focus_after: str | None = None
        self.tried_start = False

    @property
    def adjust(self) -> bool:
        return self.item is not None

    # --- building ------------------------------------------------------------------------------

    async def build(self) -> None:
        with self.frame.content:
            self.top = ui.element("div")
            self.head = ui.element("div")
            self.body = ui.element("div")
        with self.head:
            raw(
                '<div class="page-head"><h1 tabindex="-1">Neuer Run</h1><span class="faint">'
                "Projekt und Skill wählen, Aufgabe schreiben, fertig. Alles andere hat "
                "Standardwerte.</span></div>"
            )
        await load_into(
            self.body,
            self._load,
            self._render,
            retry_href=clean_href("/neuer-run", skill=self.want_skill, von=self.von),
            skeleton=skeleton_html(10, reason="Projekte, Skills und Standards werden gelesen …"),
        )

    async def _load(self) -> dict[str, Any]:
        api = self.api
        item = None
        if self.von:
            item = await api.get_approval(self.von)
        elif self.wie:
            inputs = self.repeat_of = await api.get_run_inputs(self.wie)
            self.want_project = inputs.project
            self.want_skill = inputs.skill or NO_SKILL
            self.template = _copy(inputs.repeat) or {
                # not prepared yet: what the folder already tells
                "project": inputs.project,
                "skill": inputs.skill or NO_SKILL,
                "name": f"{inputs.name.removesuffix(REPEAT_SUFFIX)}{REPEAT_SUFFIX}",
                "task_md": inputs.task_md or "",
            }
            if not task_has_content(self.template.get("task_md") or ""):
                self.template["task_md"] = TASK_TEMPLATE
        return {
            "projects": await api.list_projects(),
            "skills": await api.list_skills(SkillKind.GROUP),
            "profiles": await api.list_profiles(),
            "settings": await api.get_workfolder_settings(),
            "runs": await self._recent_runs(),
            "item": item,
        }

    async def _recent_runs(self) -> list[Any]:
        """Latest runs for „zuletzt genutztes Projekt“; without them the first project."""
        try:
            return (await self.api.list_runs(status=RunFilter.ALL, limit=20)).items
        except ApiException:
            return []

    async def _render(self, data: dict[str, Any]) -> None:
        self.projects = data["projects"]
        self.skills = {s.name: s for s in data["skills"]}
        self.profiles = data["profiles"]
        self.settings = data["settings"]
        item: ApprovalItem | None = data["item"]
        if item is not None and (
            item.type is not ApprovalType.RUN_START or item.status is not ApprovalStatus.OPEN
        ):
            raw(
                empty_state_html(
                    "front_hand",
                    "Dieser Run-Start ist schon entschieden",
                    "Anpassen geht nur, solange der Run-Start offen ist. Im Verlauf siehst du, "
                    "wer wann entschieden hat.",
                    [("Verlauf ansehen", "/freigaben?ansicht=verlauf", True)],
                )
            )
            return
        self.item = item
        if not self.projects:
            raw(
                empty_state_html(
                    "folder",
                    "Lege zuerst ein Projekt an",
                    "Ein Run arbeitet immer in Kopien eines Projekts. Ein Projekt ist ein Ordner "
                    "mit deinem Mathe-Material und einer CLAUDE.md.",
                    [("Neues Projekt", "/projekte?neu=1", True)],
                )
            )
            return
        self._init_input(data["runs"])
        if self.adjust:
            self._adjust_head()
        elif self.repeat_of is not None:
            self._repeat_head(self.repeat_of)
        with ui.element("div").classes("nr-grid").props("data-form-scope"):
            with ui.element("div"):
                self._section_project()
                self._section_task()
                self.params_box = (
                    ui.element("section").classes("card").props('id="s3" aria-labelledby="h-s3"')
                )
                self._render_params()
                with ui.element("section").classes("card").props('id="s4" aria-labelledby="h-s4"'):
                    raw(
                        '<div class="card-head"><h2 id="h-s4">4 · Agenten</h2><span class="faint '
                        'small">aufgelöst aus group.yaml · Änderungen gelten nur für diesen '
                        "Run</span></div>"
                    )
                    self.agents_box = ui.element("div")
                    raw(
                        '<p class="small muted" style="margin:6px 0 0">Web und Lese-Ordner je '
                        "Agent: Standard aus bzw. keiner. <b>Lese-Ordner und Arbeitskopie gehen "
                        "als Kontext an den Anbieter des Agenten</b> (Claude → Anthropic, "
                        "ChatGPT → OpenAI).</p>"
                    )
                with ui.element("section").classes("card").props('id="s5" aria-labelledby="h-s5"'):
                    raw('<h2 id="h-s5">5 · Gruppenchats</h2>')
                    self.channels_box = ui.element("div")
                self._section_limits()
                with ui.element("details").classes("card"):
                    # <summary> must be the first child of <details>, not wrapped (G2)
                    raw(
                        "<b>Vorschau: aufgelöste run.yaml</b> <span class='faint small'>· nur "
                        "lesen · wird beim Start im Run-Ordner gespeichert</span>",
                        tag="summary",
                    )
                    self.yaml_box = raw("", tag="pre", classes="cmd yaml")
            with (
                ui.element("aside").props('aria-label="Zusammenfassung"'),
                ui.element("div").classes("card nr-summary"),
            ):
                self.dev_box = ui.element("div")
                self.changes_box = ui.element("div")
                self.checks_box = raw("", tag="div", classes="nr-checks").props(
                    'aria-live="polite"'
                )
                with ui.element("div").classes("nr-start"):
                    label = "Freigeben und starten" if self.adjust else "Run starten"
                    self.start_button = button(
                        label,
                        self._start,
                        classes="btn primary btn-block",
                        key_hint="⌘↩",
                        grant=self.adjust,
                    )
                    self.start_button.props("data-form-submit").mark("start-run")
                    self.start_button.props('id="start-run"')
                note = (
                    "Startet mit deinen Änderungen. Claude Code erfährt sie über "
                    "adjusted_by_user und changes."
                    if self.adjust
                    else "Startet sofort. Von dir gestartete Runs brauchen keine Freigabe."
                )
                raw(f'<p class="small muted" style="margin:8px 0 0">{esc(note)}</p>')
                raw("<hr>", tag="div")
                raw("<h2>Zusammenfassung</h2>")
                self.summary_box = raw("", tag="div")
        self._render_agents_placeholder()
        await self._check()

    def _init_input(self, runs: list[Any]) -> None:
        assert self.settings is not None
        std = self.settings.limits
        item = self.item
        if item is not None:
            agents = {
                a.id: {"model": a.model, "web": a.tools.web, "read_dirs": list(a.read_dirs)}
                for a in item.agents
            }
            limits = item.limits or std
            self.input = {
                "project": item.run.project,
                "skill": item.skill or NO_SKILL,
                "name": item.run.name,
                "task_md": item.task_md or "",
                "params": dict(item.params),
                "agents": agents,
                "limits": {
                    "wall_time_s": limits.wall_time_s,
                    "turns_per_agent": limits.turns_per_agent,
                    "budget_usd": limits.budget_usd,
                },
                "decider": (item.decider or self.settings.approvals.decider).value,
            }
            self.original = _copy(self.input)
            return
        names = [p.name for p in self.projects]
        project = self.want_project if self.want_project in names else ""
        if not project:
            recent = [r.project for r in runs if r.project in names]
            project = recent[0] if recent else names[0]
        # §5.4: the skill of the link, else the group skill you used last, else none chosen yet
        # („Wähle einen Skill“); „Ohne Skill“ only when you ask for it (G3)
        skill: str | None = self.want_skill if self.want_skill in self.skills else None
        if skill is None and self.wie and self.want_skill == NO_SKILL:
            skill = NO_SKILL  # „wie dieser Run“ of a run without a skill
        if self.repeat_of is not None:
            skill = self.want_skill  # a skill that is gone shows its error (§8.4)
        if skill is None:
            used = [r.skill for r in runs if r.skill in self.skills]
            skill = used[0] if used else None
        self.input = {
            "project": project,
            "skill": skill,
            "name": "",
            "task_md": TASK_TEMPLATE,
            "params": self._default_params(skill),
            "agents": {},
            "limits": {
                "wall_time_s": std.wall_time_s,
                "turns_per_agent": std.turns_per_agent,
                "budget_usd": std.budget_usd,
            },
            "decider": self.settings.approvals.decider.value,
        }
        if self.example and skill == "exploration":
            # first run of the setup assistant (docs/08 R1 step 5): small and cheap
            self.template = {
                "name": "Erster Run – Exploration",
                "task_md": EXAMPLE_TASK,
                "params": {
                    **self.input["params"],
                    "ideas": ["Stoppzeiten-Statistik"],
                    "turns_per_idea": 3,
                },
                "limits": {"wall_time_s": 20 * 60, "budget_usd": 3},
            }
        if self.template:  # a repeat keeps its project and skill, also gone ones (§8.4)
            limits = {k: v for k, v in (self.template.pop("limits", None) or {}).items() if v}
            self.input.update(self.template)
            self.input["limits"].update(limits)
            self.input["agents"] = _copy(self.input.get("agents") or {})

    def _default_params(self, skill: str | None) -> dict[str, Any]:
        info = self.skills.get(skill)
        return {p.name: p.default for p in info.params} if info else {}

    def _adjust_head(self) -> None:
        assert self.item is not None
        self.head.clear()
        with self.head:
            raw(
                '<div class="page-head"><h1 tabindex="-1">Run-Start von Claude Code anpassen</h1>'
                f'<span class="faint">{esc(self.item.title)}</span></div>'
            )
        with self.top:
            raw(
                f'<div class="banner warn adjust-head" role="status">{icon("front_hand")}'
                '<div class="txt"><b>Du passt einen Run-Start von Claude Code an</b>Run-ID '
                f'<span class="mono">{esc(self.item.run.id)}</span> bleibt gleich. Deine '
                "Änderungen sind blau markiert und gehen als Liste an Claude Code.</div>"
                f'<a class="btn btn-sm" href="/freigaben/{esc(self.item.id)}">Zurück zur Karte'
                "</a></div>"
            )

    def _repeat_head(self, inputs: RunInputs) -> None:
        """„Wiederholung von …“ with what the repeat cannot take over (V8)."""
        notes = "".join(f"<li>{esc(n)}</li>" for n in inputs.repeat_notes)
        notes_html = f"<ul>{notes}</ul>" if notes else ""
        with self.top:
            raw(
                f'<div class="banner info nr-repeat-notes" role="status">{icon("replay")}'
                f'<div class="txt"><b>Wiederholung von „{esc(inputs.name)}“</b>Alles ist wie im '
                f'Run <span class="mono">{esc(inputs.run_id)}</span> vorbelegt. Prüfe und ändere '
                f"es hier; gestartet wird erst mit „Run starten“.{notes_html}</div>"
                f'<a class="btn btn-sm" href="/runs/{esc(inputs.run_id)}?ansicht=eingaben">'
                "Eingaben des Runs</a></div>"
            ).mark("repeat-head")

    # --- sections --------------------------------------------------------------------------------

    def _section_project(self) -> None:
        locked = (
            "Projekt und Skill stehen im Antrag fest. Für etwas anderes lehne den Run-Start ab "
            "und starte einen neuen Run."
            if self.adjust
            else None
        )
        with ui.element("section").classes("card").props('id="s1" aria-labelledby="h-s1"'):
            raw('<h2 id="h-s1">1 · Projekt und Skill</h2>')
            options = [(p.name, f"{p.name} – {p.title}") for p in self.projects]
            if self.adjust and self.input["project"] not in [p.name for p in self.projects]:
                options.insert(0, (self.input["project"], self.input["project"]))
            elif self.repeat_of is not None and self.input["project"] not in [
                p.name for p in self.projects
            ]:
                gone = self.input["project"]
                options.insert(0, (gone, f"{gone} – gibt es nicht mehr"))
            self.fields["project"] = Field(
                "Projekt",
                name="project",
                on_leave=self._field_left,
                kind="select",
                value=self.input["project"],
                options=options,
                help_text="Die Agenten arbeiten in Kopien dieses Ordners. Das Original bleibt "
                "unverändert.",
                on_change=lambda v: self._set("project", v),
                width="360px",
                locked=locked,
            )
            raw('<div class="small" style="font-weight:600;margin-bottom:6px">Skill</div>')
            self.skill_error = raw("", tag="div", classes="field").props('id="f-skill"')
            with (
                ui.element("div")
                .classes("skill-cards")
                .props('role="radiogroup" aria-label="Skill"')
            ):
                cards = [(s.name, s) for s in self.skills.values()] + [(NO_SKILL, None)]
                gone = self.input["skill"]
                if self.repeat_of is not None and gone and gone not in self.skills:
                    cards.insert(0, (gone, None))  # the skill of the old run, gone since
                for name, skill in cards:
                    title = name or "Ohne Skill"
                    if skill is None and name:
                        sub = "Diesen Skill gibt es im Workfolder nicht mehr."
                    elif skill is None:
                        sub = "Ein Agent, Topologie solo. Weitere Agenten kommen später."
                    else:
                        n = skill.agents_count or "?"
                        sub = f"{skill.description} · {n} Agent{'' if n == 1 else 'en'}"
                        if not skill.valid:
                            sub = "⚠ ungültig: " + (
                                skill.issues[0].message if skill.issues else "Prüfung"
                            )
                    disabled = self.adjust and name != self.input["skill"]
                    css = "radio-card" + (" disabled" if disabled else "")
                    with ui.element("label").classes(css):
                        radio(
                            "skill",
                            name,
                            checked=name == self.input["skill"],
                            on_change=self._choose_skill,
                            attrs="disabled" if disabled else "",
                        )
                        raw(
                            f"<span><b>{esc(title)}</b><span class='small muted'>{esc(sub)}"
                            "</span></span>",
                            tag="span",
                        )
            if not self.skills:
                raw(
                    '<p class="small muted" style="margin-top:6px">Noch keine Group-Skills im '
                    'Workfolder. <a href="/skills">Skills ansehen</a></p>'
                )
            self.fields["name"] = Field(
                "Name",
                name="name",
                on_leave=self._field_left,
                value=self.input["name"],
                optional="optional",
                help_text="Leer: arr nennt den Run nach Skill und Projekt.",
                on_change=lambda v: self._set("name", v, check=False),
                on_commit=self._commit,
                width="520px",
            )
            self.fields["name"].box.style("margin-top:12px;margin-bottom:0")

    def _section_task(self) -> None:
        with (
            ui.element("section").classes("card task-area").props('id="s2" aria-labelledby="h-s2"')
        ):
            with ui.element("div").classes("card-head"):
                raw(
                    '<h2 id="h-s2">2 · Aufgabe <span class="faint">task.md</span></h2>',
                    tag="div",
                )
                with ui.element("div").classes("actions"):
                    self.preview_button = button(
                        "Vorschau", self._toggle_preview, classes="btn btn-sm"
                    )
            default = self.original.get("task_md") if self.adjust else None
            self.fields["task_md"] = Field(
                "Aufgabe",
                name="task_md",
                on_leave=self._field_left,
                kind="textarea",
                rows=10,
                value=self.input["task_md"],
                default=default,
                default_text="aus dem Antrag",
                default_word="",
                help_text="Gliederung aus der Vorlage. Markdown und LaTeX ($σ(n)$) werden in der "
                "Vorschau gesetzt.",
                on_change=lambda v: self._set("task_md", v, check=False),
                on_commit=self._commit,
            )
            self.fields["task_md"].box.style("margin-bottom:0")
            self.preview = raw("", tag="div", classes="task-preview prose").style("display:none")

    def _toggle_preview(self) -> None:
        showing = self.preview._style.get("display") != "none"
        if showing:
            self.preview.style("display:none")
            self.fields["task_md"].input.style(remove="display:none")
            self._relabel(self.preview_button, "Vorschau")
        else:
            self.preview.set_content(markdown_html(self.input["task_md"] or ""))
            self.preview.style(remove="display:none")
            self.fields["task_md"].input.style("display:none")
            self._relabel(self.preview_button, "Bearbeiten")

    @staticmethod
    def _relabel(element: ui.element, label: str) -> None:
        element.clear()
        with element:
            raw(esc(label), tag="span")

    def _render_params(self) -> None:
        box = self.params_box
        box.clear()
        self.param_fields = {}
        skill = self.skills.get(self.input["skill"])
        with box:
            source = f"aus group.yaml von {skill.name}" if skill else ""
            raw(f'<h2 id="h-s3">3 · Parameter <span class="faint">{esc(source)}</span></h2>')
            if skill is None:
                text = (
                    "Wähle oben einen Skill, dann stehen hier seine Parameter."
                    if self.input["skill"] is None
                    else "Ohne Skill gibt es keine Parameter. Wähle oben einen Skill, um seine "
                    "Parameter zu sehen."
                )
                raw(f'<p class="small muted">{esc(text)}</p>')
                return
            if not skill.params:
                raw('<p class="small muted">Dieser Skill hat keine Parameter.</p>')
                return
            with ui.element("div").classes("grid-2e"):
                for param in skill.params:
                    with ui.element("div"):
                        self._param_field(param)

    def _param_field(self, p: ParamInfo) -> None:
        value = self.input["params"].get(p.name, p.default)
        original = self.original.get("params", {}).get(p.name, p.default)
        default = original if self.adjust else p.default
        word = "im Antrag" if self.adjust else "Standard"
        label, doc = param_text(p)
        key = p.name if label != p.name else None
        if isinstance(p.default, list):
            self._list_param(p, value or [])
            return
        if p.type in ("int", "float"):
            bounds = ""
            if p.min is not None and p.max is not None:
                bounds = f" · erlaubt {fmt.threshold(p.min)}–{fmt.threshold(p.max)}"
            help_text = f"Standard {p.default}{bounds} · {doc}".strip(" ·")
            self.param_fields[p.name] = Field(
                label,
                name=f"params.{p.name}",
                on_leave=self._field_left,
                kind="number",
                value="" if value is None else str(value),
                default=None if default is None else str(default),
                default_word=word,
                help_text=help_text,
                key=key,
                on_change=lambda v, name=p.name, kind=p.type: self._set_param(
                    name, _number(v, kind), check=False
                ),
                on_commit=self._commit,
                width="110px",
                attrs=(f'min="{p.min:g}"' if p.min is not None else "")
                + (f' max="{p.max:g}"' if p.max is not None else ""),
            )
        elif p.type == "choice":
            self.param_fields[p.name] = Field(
                label,
                name=f"params.{p.name}",
                on_leave=self._field_left,
                kind="select",
                value=str(value),
                options=[(str(c), CHOICE_LABELS.get(str(c), str(c))) for c in p.choices or []],
                default=None if default is None else str(default),
                default_text=CHOICE_LABELS.get(str(default), str(default)),
                default_word=word,
                help_text=doc,
                key=key,
                on_change=lambda v, name=p.name: self._set_param(name, v),
                width="240px",
            )
        elif p.type == "bool":
            with ui.element("div").classes("field").props(f'id="f-params.{esc(p.name)}"'):
                code = f' <span class="opt mono">{esc(key)}</span>' if key else ""
                raw(f"<span class='label'>{esc(label)}{code}</span>", tag="span")
                checkbox(
                    "an",
                    checked=bool(value),
                    on_change=lambda v, name=p.name: self._set_param(name, bool(v)),
                )
                raw(esc(doc), tag="span", classes="help")
        else:
            self.param_fields[p.name] = Field(
                label,
                name=f"params.{p.name}",
                on_leave=self._field_left,
                value="" if value is None else str(value),
                optional="optional" if p.default in (None, "") else None,
                default=None if default is None else str(default),
                default_word=word,
                help_text=doc,
                key=key,
                on_change=lambda v, name=p.name: self._set_param(name, v or None, check=False),
                on_commit=self._commit,
            )

    def _list_param(self, p: ParamInfo, values: list[Any]) -> None:
        """A list parameter such as „Vorgegebene Ideen“: one input per entry, „＋ hinzufügen“."""
        holder = ui.element("div").classes("field").props(f'id="f-params.{esc(p.name)}"')
        items = [str(v) for v in values] or [""]  # one empty entry: no click for the first

        def update(check: bool = True) -> None:
            self._set_param(p.name, [v for v in items if v.strip()], check=check)

        title, doc = param_text(p)
        code = f' <span class="opt mono">{esc(p.name)}</span>' if title != p.name else ""

        def draw(focus_last: bool = False) -> None:
            holder.clear()
            with holder:
                raw(f'<span class="label">{esc(title)}{code}</span>', tag="span")
                if doc:
                    raw(esc(doc), tag="span", classes="help")
                with ui.element("div").classes("list-edit"):
                    for index, text in enumerate(items):
                        with ui.element("div").classes("row"):
                            box = NativeInput("input").props(
                                f'type="text" aria-label="{esc(title)} {index + 1}" '
                                f'id="li-{esc(p.name)}-{index}"'
                            )
                            box.set_value(text)

                            def changed(value: str, i: int = index) -> None:
                                items[i] = value
                                update(check=False)

                            on_value(box, changed)
                            box.on("change", lambda _e: self._commit(), js_handler="() => emit()")
                            button(
                                "×",
                                lambda i=index: remove(i),
                                classes="icon-btn",
                                title=f"Eintrag {index + 1} entfernen",
                            )
                    word = "Idee" if p.name == "ideas" else "Eintrag"
                    button(f"＋ {word} hinzufügen", add, classes="link-btn")
            if focus_last and items:
                self.frame.js(f"arr.whenEl('#li-{p.name}-{len(items) - 1}', (el) => el.focus())")

        def add() -> None:
            items.append("")
            draw(focus_last=True)

        def remove(index: int) -> None:
            items.pop(index)
            draw()
            update()

        draw()

    def _section_limits(self) -> None:
        assert self.settings is not None
        std = self.settings.limits
        limits = self.input["limits"]
        original = self.original.get("limits", {})
        word = "im Antrag" if self.adjust else "Standard"

        def base(key: str, std_value: Any) -> Any:
            return original.get(key) if self.adjust else std_value

        with ui.element("section").classes("card").props('id="s6" aria-labelledby="h-s6"'):
            raw('<h2 id="h-s6">6 · Limits und Berechtigungen</h2>')
            with ui.element("div").classes("nr-limits"):
                with ui.element("div"):
                    self.fields["wall_time_s"] = Field(
                        "Laufzeit",
                        name="limits.wall_time_s",
                        on_leave=self._field_left,
                        value=duration_text(limits.get("wall_time_s")),
                        default=duration_text(base("wall_time_s", std.wall_time_s)),
                        default_word=word,
                        help_text=f"Standard {duration_text(std.wall_time_s)}",
                        on_change=lambda v: self._set_limit("wall_time_s", v, check=False),
                        on_commit=lambda: self._commit("wall_time_s"),
                        width="110px",
                    )
                with ui.element("div"):
                    self.fields["turns_per_agent"] = Field(
                        "Turns pro Agent",
                        name="limits.turns_per_agent",
                        on_leave=self._field_left,
                        kind="number",
                        value=str(limits.get("turns_per_agent") or ""),
                        default=str(base("turns_per_agent", std.turns_per_agent)),
                        default_word=word,
                        help_text=f"Standard {std.turns_per_agent}",
                        on_change=lambda v: self._set_limit("turns_per_agent", v, check=False),
                        on_commit=lambda: self._commit("turns_per_agent"),
                        width="110px",
                        attrs='min="1" max="500"',
                    )
                with ui.element("div"):
                    self.fields["budget_usd"] = Field(
                        "Budget",
                        name="limits.budget_usd",
                        on_leave=self._field_left,
                        value=money_text(limits.get("budget_usd")),
                        default=money_text(base("budget_usd", std.budget_usd)),
                        default_word=word,
                        help_text="Gilt auch bei Abo-Profilen über den rechnerischen Preis (≈). "
                        "Codex-Agenten ohne Kostenangabe begrenzen Turns und Laufzeit.",
                        on_change=lambda v: self._set_limit("budget_usd", v, check=False),
                        on_commit=lambda: self._commit("budget_usd"),
                        width="110px",
                    )
                with ui.element("div"):
                    standard = self.settings.approvals.decider.value
                    options = [
                        (value, label + (" (Standard)" if value == standard else ""))
                        for value, label in DECIDER_CHOICES
                    ]
                    self.fields["decider"] = Field(
                        "Wer entscheidet Berechtigungsanfragen",
                        name="decider",
                        on_leave=self._field_left,
                        kind="select",
                        value=self.input["decider"],
                        options=options,
                        default=self.original.get("decider") if self.adjust else standard,
                        default_text=DECIDER[Decider(self.original.get("decider") or standard)],
                        default_word=word,
                        on_change=lambda v: self._set("decider", v),
                    )

    # --- agents and channels ------------------------------------------------------------------

    def _render_agents_placeholder(self) -> None:
        self.agents_box.clear()
        with self.agents_box:
            raw('<p class="small muted">wird aufgelöst …</p>')

    def _agents_changed(self, agents: list[RunStartAgent]) -> bool:
        signature = tuple((a.id, a.model, tuple(a.read_dirs)) for a in agents)
        if signature == self._agents_signature:
            return False
        self._agents_signature = signature
        return True

    def _render_agents(self, agents: list[RunStartAgent]) -> None:
        assert self.settings is not None
        box = self.agents_box
        box.clear()
        self.agent_cells = {}
        if not agents:
            with box:
                raw('<p class="small muted">Noch keine Agenten. Wähle oben einen Skill.</p>')
            return
        effort = {p: d.effort.value if d.effort else "–" for p, d in self.settings.defaults.items()}
        resolved_effort = _efforts(self.result.resolved_run_yaml if self.result else "")
        if not resolved_effort and self.repeat_of is not None:  # nothing resolves: the old run
            resolved_effort = {a.id: a.effort.value for a in self.repeat_of.agents if a.effort}
        original = self.original.get("agents", {})
        with box, ui.element("table").classes("tbl compact nr-agents"):
            raw(
                "<tr><th>Agent</th><th>Rolle</th><th>Modell · Profil</th>"
                "<th>Arbeitsordner</th><th>Web</th><th>Lese-Ordner</th></tr>",
                tag="thead",
            )
            with ui.element("tbody"):
                for agent in agents:
                    was = original.get(agent.id, {})
                    with ui.element("tr").props(f'id="agent-{esc(agent.id)}"'):
                        raw(f'<b class="mono">{esc(agent.id)}</b>', tag="td")
                        raw(esc(agent.role_label), tag="td")
                        model_cell = ui.element("td")
                        with model_cell:
                            self._model_select(agent)
                            shown = resolved_effort.get(agent.id) or effort.get(agent.provider, "–")
                            raw(
                                f"{esc(agent.profile)} · Effort {esc(shown)}",
                                tag="span",
                                classes="info",
                            ).props(f'id="profile-{esc(agent.id)}"')
                            # inside the cell: a span between cells moved the columns (W3-10)
                            self.agent_errors[agent.id] = raw("", tag="span").props(
                                f'data-mark="agent-error-{esc(agent.id)}"'
                            )
                        raw(
                            esc(WORKSPACE.get(agent.workspace_mode.value, "Kopie")), tag="td"
                        ).classes("wrap")
                        web_cell = ui.element("td")
                        with web_cell:
                            checkbox(
                                "an",
                                checked=agent.tools.web,
                                on_change=lambda v, a=agent.id: self._set_agent(a, "web", bool(v)),
                                marker=f"web-{agent.id}",
                                attrs=f'id="web-{esc(agent.id)}" '
                                f'aria-label="Web-Zugriff für {esc(agent.id)}"',
                            )
                        dirs_cell = ui.element("td").classes("dirs")
                        with dirs_cell:
                            self._dirs_editor(agent)
                    self.agent_cells[agent.id] = {
                        "model": model_cell,
                        "web": web_cell,
                        "dirs": dirs_cell,
                    }
                    if self.adjust:
                        if was and was.get("model") != agent.model:
                            model_cell.classes("mark-changed")
                        if was and bool(was.get("web")) != agent.tools.web:
                            web_cell.classes("mark-changed")
                        if was and list(was.get("read_dirs") or []) != agent.read_dirs:
                            dirs_cell.classes("mark-changed")
        if self._focus_after:
            self.frame.js(f"arr.whenEl('#' + CSS.escape({self._focus_after!r}), (e) => e.focus())")
            self._focus_after = None

    def _model_select(self, agent: RunStartAgent) -> None:
        select = ui.element("select").props(
            f'aria-label="Modell von {esc(agent.id)}" id="model-{esc(agent.id)}"'
        )
        with select:
            for value, label in self._model_choices(agent).items():
                option = ui.element("option").props(f'value="{value}"')
                if value == agent.model:
                    option.props("selected")
                with option:
                    raw(esc(label), tag="span")

        def changed(event: Any, aid: str = agent.id) -> None:
            self._focus_after = f"model-{aid}"
            self._set_agent(aid, "model", str(event.args))

        select.on("change", changed, js_handler="(e) => emit(e.target.value)")

    def _model_choices(self, agent: RunStartAgent) -> dict[str, str]:
        """The usual models, the standards of `arr.yaml` and what the agent has now."""
        assert self.settings is not None
        choices = dict(MODELS)
        for standard in self.settings.defaults.values():
            if standard.model:
                choices.setdefault(standard.model, standard.model)
        choices.setdefault(agent.model, agent.model_label)
        return choices

    def _dirs_editor(self, agent: RunStartAgent) -> None:
        for folder in agent.read_dirs:
            with ui.element("span").classes("dir"):
                raw(esc(folder), tag="span")
                button(
                    "×",
                    lambda a=agent.id, f=folder: self._remove_dir(a, f),
                    classes="icon-btn",
                    title=f"{folder} für {agent.id} entfernen",
                )
        with ui.element("div").classes("dir-add"):
            box = ui.element("input").props(
                f'type="text" placeholder="~/Documents/…" '
                f'aria-label="Lese-Ordner für {esc(agent.id)} hinzufügen" '
                f'id="dir-{esc(agent.id)}"'
            )
            value = {"text": ""}
            box.on(
                "input",
                lambda e: value.__setitem__("text", str(e.args)),
                js_handler="(e) => emit(e.target.value)",
            )
            box.on(
                "keydown.enter",
                lambda _e, a=agent.id: self._add_dir(a, value["text"]),
            )
            button(
                "＋",
                lambda a=agent.id: self._add_dir(a, value["text"]),
                classes="btn btn-sm",
                title=f"Lese-Ordner für {agent.id} hinzufügen",
            )

    def _add_dir(self, agent_id: str, folder: str) -> None:
        folder = folder.strip()
        if not folder:
            return
        if not folder.startswith(("~/", "/")):
            self.frame.toast(
                f"„{folder}“ ist kein Ordner. Gib einen Pfad an, z. B. ~/Documents/Literatur.",
                error=True,
            )
            return
        current = self._agent_value(agent_id, "read_dirs") or []
        if folder not in current:
            self._focus_after = f"dir-{agent_id}"
            self._set_agent(agent_id, "read_dirs", [*current, folder])

    def _remove_dir(self, agent_id: str, folder: str) -> None:
        current = self._agent_value(agent_id, "read_dirs") or []
        self._focus_after = f"dir-{agent_id}"
        self._set_agent(agent_id, "read_dirs", [f for f in current if f != folder])

    def _agent_value(self, agent_id: str, key: str) -> Any:
        override = self.input["agents"].get(agent_id, {})
        if key in override:
            return override[key]
        if self.result:
            agent = next((a for a in self.result.agents if a.id == agent_id), None)
            if agent is not None:
                return {
                    "read_dirs": agent.read_dirs,
                    "web": agent.tools.web,
                    "model": agent.model,
                }[key]
        return None

    def _render_channels(self, result: ResolveResult) -> None:
        self.channels_box.clear()
        with self.channels_box:
            if not result.channels:
                raw('<p class="small muted">Ohne Skill gibt es keine Gruppenchats.</p>')
                return
            rows = []
            for channel in result.channels:
                members = " ".join(f'<span class="chip">{esc(m)}</span>' for m in channel.members)
                mode = "Runden" if channel.mode.value == "rounds" else "ereignisgesteuert"
                extra = []
                if channel.anonymous:
                    extra.append("anonym")
                if channel.open_from:
                    extra.append(f"offen ab „{channel.open_from}“")
                note = f'<span class="info">{esc(" · ".join(extra))}</span>' if extra else ""
                rows.append(
                    f'<tr><td class="mono">#{esc(channel.name)}{note}</td><td>{members}</td>'
                    f"<td>{esc(mode)}</td></tr>"
                )
            raw(
                '<table class="tbl compact"><thead><tr><th>Kanal</th><th>Mitglieder</th>'
                f"<th>Taktung</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
            )

    def _skill(self) -> SkillInfo | None:
        return self.skills.get(self.input["skill"])

    # --- changes and checks ---------------------------------------------------------------------

    def _set(self, key: str, value: Any, *, check: bool = True) -> None:
        self.input[key] = value
        self._changed(check)

    def _set_param(self, name: str, value: Any, *, check: bool = True) -> None:
        self.input["params"][name] = value
        self._changed(check)

    def _changed(self, check: bool) -> None:
        """A choice is checked at once (debounced); typed text only when you leave the field
        or start (U1): until then „Run starten“ stays clickable and checks first."""
        if check:
            self._schedule()
            return
        self._unchecked = True
        if not self._checking:
            set_disabled(self.start_button, False)

    def _commit(self, limit: str | None = None) -> None:
        """You left a changed text field (or reset it): check now."""
        if limit is not None:
            self._show_error(self.fields[limit], self._limit_input_error(limit))
        self._schedule(now=True)

    def _set_agent(self, agent_id: str, key: str, value: Any) -> None:
        self.input["agents"].setdefault(agent_id, {})[key] = value
        self._schedule(now=key != "web")

    def _set_limit(self, key: str, text: str, *, check: bool = True) -> None:
        value = _limit_value(key, text)
        if check:
            self._show_error(self.fields[key], self._limit_input_error(key))
        if value:
            self.input["limits"][key] = value
        self._changed(check)

    def _limits_invalid(self) -> bool:
        """A limit field holds no valid value: the check saw its last good one, so this is
        the only place that keeps „Run starten“ locked."""
        return any(self._limit_input_error(key) for key in LIMIT_KEYS)

    def _limit_input_error(self, key: str) -> str | None:
        """Error of the limit input itself, before any check („Gib eine Dauer an …“)."""
        if _limit_value(key, self.fields[key].value):
            return None
        return {
            "wall_time_s": "Gib eine Dauer an, z. B. 3 h oder 90 min.",
            "budget_usd": "Gib einen Betrag an, z. B. 20 $.",
        }.get(key, "Gib eine ganze Zahl an, z. B. 40.")

    def _choose_skill(self, skill: str) -> None:
        if skill == self.input["skill"]:
            return
        self.input["skill"] = skill
        self.input["params"] = self._default_params(skill)
        self.input["agents"] = {}
        self._render_params()
        self._schedule(now=True)

    def _schedule(self, *, now: bool = False) -> None:
        """Check again 500 ms after the last change; „wird geprüft …“ shows after 300 ms."""
        if self._timer is not None:
            self._timer.cancel()
        self._checking = True
        self._render_checking()
        with self.frame.content:
            self._timer = ui.timer(0.05 if now else CHECK_DELAY, self._check, once=True)

    def _render_checking(self) -> None:
        set_disabled(self.start_button, True)
        self.checks_box.set_content(
            '<div class="skel-wrap"><p class="small muted">wird geprüft …</p></div>'
        )

    def _run_input(self) -> dict[str, Any]:
        data = _copy(self.input)
        if not task_has_content(data["task_md"]):
            data["task_md"] = ""
        data["skill"] = data["skill"] or None
        if self.adjust or self.repeat_of is not None or self.settings is None:
            # a repeat keeps every limit of the old run, also one equal to your standard: the
            # skill could set another (V8: the same `run.yaml`)
            return data
        # only what you changed: limits of the skill and of `arr.yaml` keep their order (02 §3.3)
        std = self.settings.limits
        data["limits"] = {k: v for k, v in data["limits"].items() if v != getattr(std, k)}
        if data["decider"] == self.settings.approvals.decider.value:
            del data["decider"]
        return data

    async def _check(self) -> None:
        self._timer = None
        self._unchecked = False
        if self.input["skill"] is None:
            self._render_no_skill()
            return
        self._checking = True
        self._render_checking()
        try:
            result = await self.api.resolve_run(self._run_input())
        except ApiException as exc:
            self.checks_box.set_content(
                f'<div class="check"><span class="st bad">✕</span><span class="txt"><b>'
                f"{esc(exc.error.message)}</b></span></div>"
            )
            return
        if self._timer is not None:  # a newer change is pending
            return
        result = self._with_skill_lock(result)
        self.result = result
        self._checking = False
        if result.agents or not result.errors:  # an error in the form keeps the last table
            if self._agents_changed(result.agents):
                self._render_agents(result.agents)
            else:
                self._update_agent_marks(result.agents)
            self._render_channels(result)
        elif self.repeat_of is not None and not self.agent_cells:
            # nothing resolves (e.g. a profile of the skill is gone): the agents of the old run
            # carry the errors at their rows (V8, §8.4)
            old = [_start_agent(a) for a in self.repeat_of.agents]
            if self._agents_changed(old):
                self._render_agents(old)
        self.yaml_box.set_content(
            esc(result.resolved_run_yaml or "Die Vorschau erscheint, sobald alle Angaben passen.")
        )
        self._render_field_errors(result)
        self._render_summary(result)
        self._render_deviations(result)
        self._render_checks(result)

    def _render_no_skill(self) -> None:
        """No skill chosen yet: nothing to resolve; the check list says what is missing."""
        self.result = None
        self._checking = False
        self._agents_signature = ()
        self.agents_box.clear()
        with self.agents_box:
            raw('<p class="small muted">Noch keine Agenten. Wähle oben einen Skill.</p>')
        self.channels_box.clear()
        with self.channels_box:
            raw('<p class="small muted">Die Gruppenchats kommen aus dem Skill.</p>')
        self.yaml_box.set_content("")
        self.dev_box.clear()
        self.checks_box.set_content(
            '<div class="check"><span class="st todo" aria-hidden="true">○</span><span class="txt">'
            '<b>Noch offen (1)</b><small><a class="jump todo" href="#s1" data-jump="s1">Wähle '
            "einen Skill oder „Ohne Skill“.</a></small></span></div>"
        )
        for key in LIMIT_KEYS:  # the limit inputs need no skill to be checked
            self._show_error(self.fields[key], self._limit_input_error(key))
        rows = [("Projekt", self.input["project"]), ("Skill", "noch nicht gewählt")]
        body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
        self.summary_box.set_content(f'<dl class="kv">{body}</dl>')
        set_disabled(self.start_button, True)

    def _update_agent_marks(self, agents: list[RunStartAgent]) -> None:
        if not self.adjust:
            return
        original = self.original.get("agents", {})
        for agent in agents:
            cells = self.agent_cells.get(agent.id)
            was = original.get(agent.id)
            if not cells or not was:
                continue
            changed = bool(was.get("web")) != agent.tools.web
            if changed:
                cells["web"].classes("mark-changed")
            else:
                cells["web"].classes(remove="mark-changed")

    def _with_skill_lock(self, result: ResolveResult) -> ResolveResult:
        """A skill whose check found problems cannot start (W4-02, docs/08 §5.7): its first
        problem stands at the skill, also where the service does not report it itself."""
        skill = self._skill()
        if skill is None or skill.valid or any(i.path == "skill" for i in result.errors):
            return result
        first = skill.issues[0] if skill.issues else None
        where = ""
        if first is not None:
            where = f"{first.file}, Zeile {first.line}: " if first.line else f"{first.file}: "
        text = (
            f"Der Skill „{skill.name}“ hat Fehler und lässt sich nicht starten: {where}"
            f"{first.message if first else ''} Behebe sie unter Skills, Reiter „Prüfung“."
        )
        issue = FieldIssue(
            path="skill",
            message=text,
            action=Action(label="Skill öffnen", url=f"/skills/{skill.name}?tab=pruefung"),
        )
        return result.model_copy(update={"errors": [issue, *result.errors]})

    def _shows_errors(self, field: Field) -> bool:
        """Errors stand at a field once it was left or after „Run starten“ (K7); a fresh form
        has no red field. In „Anpassen“ the request is already filled in, so they show at once;
        the same for a repeat (V8): a project, skill or profile that is gone stands at once."""
        return field.touched or self.tried_start or self.adjust or self.repeat_of is not None

    def _show_error(self, field: Field, message: str | None) -> None:
        field.set_error(message if self._shows_errors(field) else None)

    def _field_left(self) -> None:
        """A field was left for the first time: show its error from the last check, unless a
        newer check is pending (it shows them itself)."""
        if self.result is not None and not self._checking:
            self._render_field_errors(self.result)
            self._render_checks(self.result)

    def _error_shown(self, path: str) -> bool:
        """Whether the error of `path` stands at its field; errors without a field always do."""
        fields = [*self.fields.values(), *self.param_fields.values()]
        field = next((f for f in fields if isinstance(f, Field) and f.name == path), None)
        return field is None or self._shows_errors(field)

    def _render_field_errors(self, result: ResolveResult) -> None:
        by_path = {i.path: i.message for i in result.errors}
        for key, field in self.fields.items():
            message = by_path.get(field.name)
            if message is None and key in LIMIT_KEYS:
                message = self._limit_input_error(key)
            self._show_error(field, message)
        for name, field in self.param_fields.items():
            if isinstance(field, Field):
                self._show_error(field, by_path.get(f"params.{name}"))
        for agent_id, slot in self.agent_errors.items():
            message = next(
                (m for p, m in by_path.items() if p.startswith(f"agents.{agent_id}.")), None
            )
            slot.set_content(f'<span class="error">{esc(message)}</span>' if message else "")
        skill_error = by_path.get("skill")
        self.skill_error.set_content(
            f'<span class="error">{esc(skill_error)}</span>' if skill_error else ""
        )

    def _render_summary(self, result: ResolveResult) -> None:
        agents = result.agents
        by_vendor = {
            v: sum(1 for a in agents if a.vendor is v) for v in (Vendor.ANTHROPIC, Vendor.OPENAI)
        }
        mix = []
        if by_vendor[Vendor.ANTHROPIC]:
            mix.append(f"{by_vendor[Vendor.ANTHROPIC]} Claude")
        if by_vendor[Vendor.OPENAI]:
            mix.append(f"{by_vendor[Vendor.OPENAI]} ChatGPT")
        est = result.estimate
        estimate = "–"
        if est is not None:
            estimate = _estimate_text(est)
        capacity = " · ".join(
            f"{c.profile}: {c.free} von {c.limit} frei, braucht {c.needed}" for c in result.capacity
        )
        channels = ", ".join(f"#{c.name}" for c in result.channels) or "keine"
        skill = self.input["skill"] or "ohne Skill"
        rows = [
            ("Projekt", self.input["project"]),
            ("Skill", skill),
            ("Agenten", f"{len(agents)} · {', '.join(mix)}" if agents else "–"),
            ("Kanäle", channels),
            ("Schätzung", estimate),
            ("Kapazität", capacity or "–"),
        ]
        body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
        title = ""
        if est is not None and est.notes:
            title = f' title="{esc(" ".join(est.notes))}"'
        elif est is not None and est.cost_kind is not None:
            title = ' title="≈ = rechnerischer API-Preis bei Abo-Profilen"'
        self.summary_box.set_content(f'<dl class="kv"{title}>{body}</dl>')

    def _render_deviations(self, result: ResolveResult) -> None:
        """„⚠ Abweichungen von deinen Standards (n)“; in „Anpassen“ with your changes struck
        through and marked (docs/08 §5.4)."""
        box = self.dev_box
        box.clear()
        current = result.deviations
        lines = []
        if self.adjust:
            assert self.item is not None
            now = {_category(d.path): d for d in current}
            for dev in self.item.deviations:
                cat = _category(dev.path)
                new = now.pop(cat, None)
                if new is not None and new.value == dev.value and new.value_text == dev.value_text:
                    lines.append(self._dev_li(new))
                elif new is None:
                    lines.append(
                        f'<li class="mark-changed"><s>{esc(deviation_text(dev))}</s> → '
                        f"<b>{esc('Standard ' + dev.default_text)}</b> "
                        '<span class="by-you">(von dir geändert)</span></li>'
                    )
                else:
                    lines.append(
                        f'<li class="mark-changed"><s>{esc(deviation_text(dev))}</s> → '
                        f"<b>{esc(deviation_text(new))}</b> "
                        '<span class="by-you">(von dir geändert)</span></li>'
                    )
            for dev in now.values():
                lines.append(
                    f'<li class="mark-changed">{esc(deviation_text(dev))} '
                    '<span class="by-you">(von dir geändert)</span></li>'
                )
            count = len(current)
        else:
            lines = [self._dev_li(d) for d in current]
            count = len(current)
        with box:
            if not lines:
                if self.adjust:
                    raw(
                        '<div class="dev ok"><h4>✓ Keine Abweichungen von deinen Standards</h4>'
                        "</div>"
                    )
                return
            raw(
                f'<div class="dev"><h4>{icon("warning", small=True)}Abweichungen von deinen '
                f'<span class="nowrap">Standards ({count})</span></h4>'
                f"<ul>{''.join(lines)}</ul></div>"
            )
        self._render_changes()

    def _dev_li(self, dev: Deviation) -> str:
        vendors = providers_text(dev)
        note = f'<div class="provider-note">↳ {esc(vendors)}</div>' if vendors else ""
        return f"<li>{esc(deviation_text(dev))}{note}</li>"

    def _render_changes(self) -> None:
        self.changes_box.clear()
        if not self.adjust:
            return
        changes = self._changes()
        if not changes:
            return
        with self.changes_box:
            items = "".join(f"<li>{esc(label)}</li>" for label, _path, _value in changes)
            raw(
                f'<div class="small" style="margin-bottom:8px"><b>Deine Änderungen '
                f'({len(changes)})</b><ul class="small">{items}</ul></div>'
            )

    def _changes(self) -> list[tuple[str, str, Any]]:
        """Your changes against the request as (label, path, new value) (`config` of adjust)."""
        out: list[tuple[str, str, Any]] = []
        if not self.adjust:
            return out
        before = self.original
        for aid, was in before.get("agents", {}).items():
            now = self.input["agents"].get(aid, {})
            if "web" in now and bool(now["web"]) != bool(was.get("web")):
                word = "an" if now["web"] else "aus"
                old = "an" if was.get("web") else "aus"
                out.append((f"Web {aid}: {old} → {word}", f"agents.{aid}.tools.web", now["web"]))
            if "read_dirs" in now and list(now["read_dirs"]) != list(was.get("read_dirs") or []):
                new_dirs = ", ".join(now["read_dirs"]) or "keiner"
                old_dirs = ", ".join(was.get("read_dirs") or []) or "keiner"
                out.append(
                    (
                        f"Lese-Ordner {aid}: {old_dirs} → {new_dirs}",
                        f"agents.{aid}.read_dirs",
                        now["read_dirs"],
                    )
                )
            if "model" in now and now["model"] != was.get("model"):
                out.append(
                    (
                        f"Modell {aid}: {was.get('model')} → {now['model']}",
                        f"agents.{aid}.model",
                        now["model"],
                    )
                )
        for key, label in (
            ("wall_time_s", "Laufzeit"),
            ("turns_per_agent", "Turns pro Agent"),
            ("budget_usd", "Budget"),
        ):
            new = self.input["limits"].get(key)
            old = before.get("limits", {}).get(key)
            if new != old:
                shown = {
                    "wall_time_s": duration_text,
                    "budget_usd": money_text,
                }.get(key, lambda v: str(v))
                out.append((f"{label}: {shown(old)} → {shown(new)}", f"limits.{key}", new))
        if self.input["decider"] != before.get("decider"):
            out.append(
                (
                    f"Entscheider: {DECIDER[Decider(before['decider'])]} → "
                    f"{DECIDER[Decider(self.input['decider'])]}",
                    "decider",
                    self.input["decider"],
                )
            )
        if self.input["task_md"] != before.get("task_md"):
            out.append(("Aufgabe geändert", "task_md", self.input["task_md"]))
        skill = self._skill()
        labels = {p.name: param_label(p) for p in skill.params} if skill else {}
        for name, value in self.input["params"].items():
            if value != before.get("params", {}).get(name):
                out.append(
                    (
                        f"{labels.get(name, name)}: {before['params'].get(name)} → {value}",
                        f"params.{name}",
                        value,
                    )
                )
        return out

    def _render_checks(self, result: ResolveResult) -> None:
        lines = []
        errors = result.errors
        if errors:
            # before you touched anything, what is missing stands neutral: „Noch offen“ (M2)
            shown = any(self._error_shown(i.path) for i in errors)
            word = "Problem" if len(errors) == 1 else "Probleme"
            head = f"{len(errors)} {word}" if shown else f"Noch offen ({len(errors)})"
            mark = (
                '<span class="st bad" aria-hidden="true">✕</span>'
                if shown
                else ('<span class="st todo" aria-hidden="true">○</span>')
            )
            links = "".join(
                f"<small>{esc(i.message)} {_action_link(i.action)}</small>"
                if i.action is not None and i.path == NO_FIELD
                else f'<small><a class="jump{"" if shown else " todo"}" '
                f'href="#{esc(_anchor(i.path))}" '
                f'data-jump="{esc(_anchor(i.path))}">{esc(i.message)}</a></small>'
                for i in errors
            )
            lines.append(
                f'<div class="check">{mark}<span class="txt"><b>{head}</b>{links}</span></div>'
            )
        for warning in result.warnings:
            lines.append(
                '<div class="check"><span class="st warn" aria-hidden="true">!</span>'
                f'<span class="txt small">{esc(warning)}</span></div>'
            )
        checked = bool(result.resolved_run_yaml)  # without a plan nothing was looked up
        if checked and not any(_about_skills(i.path) for i in errors):
            lines.append(
                '<div class="check"><span class="st ok" aria-hidden="true">✓</span>'
                '<span class="txt small">Rollen und Skills gefunden</span></div>'
            )
        if checked and not any(i.path.startswith("agents.") for i in errors):
            tested = [p.last_test.ts for p in self.profiles if p.last_test and p.last_test.ok]
            word = "Profile vorhanden"
            if tested:
                word = f"Profile getestet ({fmt.when(max(tested), self.api.now())})"
            lines.append(
                '<div class="check"><span class="st ok" aria-hidden="true">✓</span>'
                f'<span class="txt small">{esc(word)}</span></div>'
            )
        self.checks_box.set_content("".join(lines))
        blocked = bool(errors) or self._limits_invalid()
        set_disabled(self.start_button, blocked and not self._unchecked)

    # --- start ---------------------------------------------------------------------------------

    async def _start(self) -> None:
        self.tried_start = True
        if self._checking or self._unchecked:
            # typed text, or leaving a field right before the click: check it now (U1)
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            await self._check()
        if self.result is None or self.result.errors or self._limits_invalid():
            if self.result is not None:
                self._render_field_errors(self.result)
            return
        if self.adjust:
            await self._approve()
            return
        set_disabled(self.start_button, True)
        try:
            started = await self.api.start_run(self._run_input())
        except ApiException as exc:
            self._start_error(exc)
            set_disabled(self.start_button, False)
            return
        self.frame.toast("Run gestartet", ("Zum Run", f"/runs/{started.run_id}"))
        ui.navigate.to(f"/runs/{started.run_id}")

    async def _approve(self) -> None:
        assert self.item is not None
        changes = self._changes()
        if changes:
            request = DecisionRequest(
                action=DecisionAction.ADJUST, config={path: value for _l, path, value in changes}
            )
        else:
            request = DecisionRequest(action=DecisionAction.ALLOW)
        set_disabled(self.start_button, True)
        try:
            response = await self.api.decide_approval(self.item.id, request)
        except ApiException as exc:
            self._start_error(exc)
            set_disabled(self.start_button, False)
            return
        self.frame.toast(response.message, ("Zum Run", f"/runs/{response.run_id}"))
        ui.navigate.to(f"/runs/{response.run_id}")

    def _start_error(self, exc: ApiException) -> None:
        """Start error: banner on top with the path to the place (docs/08 §5.4, 02 §3.3)."""
        self.top.clear()
        issues = exc.error.issues
        links = "".join(
            f"<li>{esc(i.message)} {_action_link(i.action)}</li>"
            if i.action is not None and i.path == NO_FIELD
            else f'<li><a href="#{esc(_anchor(i.path))}" data-jump="{esc(_anchor(i.path))}">'
            f'{esc(i.message)}</a> <span class="mono small faint">{esc(i.path)}</span></li>'
            for i in issues
        )
        with self.top:
            raw(
                f'<div class="banner err" role="alert" style="margin-bottom:12px">{icon("error")}'
                f'<div class="txt"><b>Der Run wurde nicht gestartet: {esc(exc.error.message)}'
                f"</b>{esc(exc.error.detail or '')}"
                + (f"<ul>{links}</ul>" if links else "")
                + "</div></div>"
            )
        self.frame.js("window.scrollTo({top: 0})")


def _action_link(action: Action) -> str:
    """The „[Aktion]“ of a message (§8.4)."""
    return f'<a href="{esc(action.url)}">{esc(action.label)}</a>'


def _about_skills(path: str) -> bool:
    return path == "skill" or path.endswith((".role", ".skills"))


def _anchor(path: str) -> str:
    """Field anchor of a check path: `limits.budget_usd` → `f-limits.budget_usd`."""
    if path.startswith("agents."):
        return f"agent-{path.split('.')[1]}"
    if path.startswith("agents"):
        return "s4"
    if path == "skill":
        return "s1"
    return f"f-{path}"


def _limit_value(key: str, text: str) -> int | float | None:
    """A limit field's text as its value; None when it is no valid entry."""
    if key == "wall_time_s":
        return parse_duration(text)
    if key == "budget_usd":
        return parse_money(text)
    value = _number(text, "int")
    return value if value and value > 0 else None


def _number(text: str, kind: str) -> int | float | None:
    try:
        value = float(str(text).replace(",", "."))
    except ValueError:
        return None
    return int(value) if kind == "int" else value


def _estimate_text(est: Estimate) -> str:
    """„4–12 Turns · höchstens 2 $ (Budget) · höchstens 20 min“; unknown parts say so (W4-02)."""
    if est.turns_open:
        turns = f"mindestens {int(est.turns.min)} Turns"
    elif est.turns.min == est.turns.max:
        turns = f"{int(est.turns.min)} Turns"
    else:
        turns = f"{int(est.turns.min)}–{int(est.turns.max)} Turns"
    if est.cost_basis == "unknown":
        money = "Kosten unbekannt"
    elif est.cost_basis == "budget":
        money = f"höchstens {fmt.budget(est.cost_usd.max)} (Budget)"
    else:
        approx = "≈ " if est.cost_kind is not None else ""
        money = f"{approx}{fmt.threshold(est.cost_usd.min)}–{fmt.threshold(est.cost_usd.max)} $"
    if est.duration_basis == "unknown":
        duration = "Dauer offen"
    elif est.duration_basis == "wall_time":
        duration = f"höchstens {_duration_max(est.duration_s.max)}"
    else:
        duration = _duration_range(est.duration_s.min, est.duration_s.max)
    return f"{turns} · {money} · {duration}"


def _duration_max(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    return f"{fmt.decimal(seconds / 3600, 1)} h"


def _duration_range(low: float, high: float) -> str:
    if high < 3600:
        return f"{int(low // 60)}–{int(high // 60)} min"
    return f"{fmt.decimal(low / 3600, 1)}–{fmt.decimal(high / 3600, 1)} h"


def _start_agent(agent: RunInputAgent) -> RunStartAgent:
    """An agent of the repeated run as a row of the table."""
    return RunStartAgent(
        id=agent.id,
        role_label=agent.role_label,
        provider=agent.provider,
        vendor=agent.vendor,
        model=agent.model,
        model_label=agent.model_label,
        profile=agent.profile,
        tools={"shell": agent.shell, "web": agent.web},
        read_dirs=list(agent.read_dirs),
        workspace_mode=agent.workspace_mode,
        skills=list(agent.skills),
    )


def _efforts(run_yaml: str) -> dict[str, str]:
    """Effort per agent as the preview `run.yaml` holds it (also from `group.yaml` or a
    repeat, W3-10); the table showed only the standard of `arr.yaml`."""
    if not run_yaml:
        return {}
    try:
        data = YAML(typ="safe").load(run_yaml)
    except YAMLError:
        return {}
    agents = data.get("agents") if isinstance(data, dict) else None
    return {
        str(a["id"]): str(a["effort"])
        for a in agents or []
        if isinstance(a, dict) and a.get("id") and a.get("effort")
    }


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def register() -> None:
    """Register `/neuer-run` (docs/08 §5.4, deep links §4.3)."""

    @ui.page("/neuer-run")
    async def neuer_run(
        skill: str = "",
        projekt: str = "",
        von: str = "",
        wie: str = "",
        beispiel: str = "",
        zustand: str = "",
    ) -> None:
        api = state_api(get_api(), zustand)
        use_screen_assets()
        title = "Run-Start anpassen" if von else "Neuer Run"
        frame = Frame(
            api,
            active="neuer-run",
            title=title,
            crumbs=[("Freigaben", "/freigaben"), (title, None)] if von else None,
            focus="#inhalt h1" if von or wie else None,
        )
        await frame.start()
        page = NeuerRunPage(
            frame,
            api,
            skill=skill,
            project=projekt,
            von=von,
            wie=wie,
            zustand=zustand,
            example=beispiel == "1",
        )
        await page.build()
        if skill and not (von or wie):
            # §4.3: the first empty field, scrolled into view (the task; name is optional)
            frame.js(auto_focus_js("#f-task_md textarea"))

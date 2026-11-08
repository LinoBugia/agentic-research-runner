"""Run detail, view „Eingaben“: what the run was started with (W3-10, UX-Abnahme 3 V9;
docs/08-ux-konzept.md §5.4, §6.5 „Konfiguration ansehen“).

The task (rendered, with „Kopieren“), project and skill, the parameters with their values,
the agents (role, model, profile, effort, working folder, read folders, web), channels with
their rhythm, limits and who decides approvals, and the resolved `run.yaml` (read only,
folded). The same for running, finished, stopped and slimmed runs; the texts come through the
secret filter of the service (`get_run_inputs`). „Wiederholen“ opens „Neuer Run“ with all of
it filled in (V8).
"""

from typing import TYPE_CHECKING, Any

from arr.models import ChannelMode, WorkspaceMode
from arr.service.api_models import (
    RunInputAgent,
    RunInputChannel,
    RunInputParam,
    RunInputs,
)
from arr.service.gui import format as fmt
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import esc, icon, markdown_html, raw
from arr.service.gui.components.params import CHOICE_LABELS
from arr.service.gui.texts import DECIDER, VENDOR

if TYPE_CHECKING:
    from arr.service.gui.pages.run_detail import RunDetailPage

REPEAT_LABEL = "Wiederholen"
WORKSPACE = {
    WorkspaceMode.COPY: "eigene Kopie",
    WorkspaceMode.SHARED: "gemeinsame Kopie",
    WorkspaceMode.EMPTY: "leer · liest Output",
}
MODE = {
    ChannelMode.EVENT: "ereignisgesteuert",
    ChannelMode.ROUNDS: "in Runden",
    ChannelMode.MODERATED: "moderiert",
}
ORDER = {"parallel": "alle zugleich", "sequential": "nacheinander"}
WAKE = {"mention": "@name", "question": "Frage", "any": "jeder Nachricht"}


def repeat_href(run_id: str) -> str:
    """„Neuer Run“ filled in with this run (V8, also „Neu starten“ of docs/08 §8.4)."""
    return f"/neuer-run?wie={run_id}"


def value_text(value: Any) -> str:
    """A parameter value as the form shows it: „ja“, „3“, „a, b“, „–“."""
    if value is None or value == "" or value == []:
        return "–"
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, list):
        return ", ".join(value_text(v) for v in value)
    if isinstance(value, float):
        return fmt.decimal(value, 2).rstrip("0").rstrip(",")
    return CHOICE_LABELS.get(str(value), str(value))


def _section(title: str, body: str, *, anchor: str, extra: str = "") -> str:
    return (
        f'<section class="in-sec" aria-labelledby="in-{anchor}">'
        f'<div class="in-h"><h2 id="in-{anchor}">{esc(title)}</h2>{extra}</div>{body}</section>'
    )


def _params_html(params: list[RunInputParam]) -> str:
    if not params:
        return '<p class="small muted">Dieser Run hat keine Parameter.</p>'
    rows = []
    for p in params:
        mark = ' <span class="tag">geändert</span>' if p.changed else ""
        default = (
            f'<span class="faint small">Standard {esc(value_text(p.default))}</span>'
            if p.changed
            else ""
        )
        lines = [line.strip() for line in (p.doc or "").splitlines() if line.strip()]
        label, help_text = (lines[0], " ".join(lines[1:])) if len(lines) >= 2 else ("", "")
        if len(lines) == 1:
            help_text = lines[0]
        name = (
            f'{esc(label)} <span class="mono faint small">{esc(p.name)}</span>'
            if label
            else (f'<span class="mono">{esc(p.name)}</span>')
        )
        rows.append(
            f'<tr data-param="{esc(p.name)}"><td>{name}</td>'
            f"<td><b>{esc(value_text(p.value))}</b>{mark} {default}</td>"
            f'<td class="small muted">{esc(help_text)}</td></tr>'
        )
    return (
        '<table class="tbl compact in-params"><thead><tr><th>Parameter</th><th>Wert</th>'
        f"<th>Bedeutung</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _agent_row(a: RunInputAgent) -> str:
    folder = WORKSPACE.get(a.workspace_mode, a.workspace_mode.value)
    if a.workspace_name:
        folder += f" „{a.workspace_name}“"
    dirs = (
        "".join(f'<span class="chip mono">{esc(d)}</span>' for d in a.read_dirs)
        if a.read_dirs
        else '<span class="faint">keine</span>'
    )
    effort = a.effort.value if a.effort is not None else "–"
    skills = (
        f'<div class="faint small">Skills: {esc(", ".join(a.skills))}</div>' if a.skills else ""
    )
    return (
        f'<tr data-agent="{esc(a.id)}"><td><b class="mono">{esc(a.id)}</b>'
        f'<div class="faint small">{esc(a.role or "ohne Rollendatei")}</div></td>'
        f"<td>{esc(a.role_label)}</td>"
        f"<td>{esc(a.model_label)}<div class='faint small'>{esc(VENDOR[a.vendor])} · Profil "
        f"<span class='mono'>{esc(a.profile)}</span></div></td>"
        f"<td>{esc(effort)}</td><td>{esc(folder)}{skills}</td><td>{dirs}</td>"
        f"<td>{'an' if a.web else 'aus'}</td></tr>"
    )


def _agents_html(agents: list[RunInputAgent]) -> str:
    if not agents:
        return '<p class="small muted">Noch keine Agenten.</p>'
    return (
        '<table class="tbl compact in-agents"><thead><tr><th>Agent</th><th>Rolle</th>'
        "<th>Modell · Profil</th><th>Effort</th><th>Arbeitsordner</th><th>Lese-Ordner</th>"
        f"<th>Web</th></tr></thead><tbody>{''.join(_agent_row(a) for a in agents)}</tbody>"
        "</table>"
    )


def _rhythm(c: RunInputChannel) -> str:
    parts = [MODE.get(c.mode, c.mode.value)]
    if c.mode is ChannelMode.ROUNDS and c.order:
        parts.append(ORDER.get(c.order, c.order))
    if c.moderator:
        parts.append(f"Moderation {c.moderator}")
    if c.wake_on:
        parts.append("weckt bei " + ", ".join(WAKE.get(w, w) for w in c.wake_on))
    if c.debounce_s:
        parts.append(f"bündelt {fmt.duration(c.debounce_s)}")
    if c.mode is ChannelMode.EVENT and c.idle_after_s:
        parts.append(f"Runde nach {fmt.duration(c.idle_after_s)} Ruhe")
    if c.quiet_after_s:
        parts.append(f"Ende nach {fmt.duration(c.quiet_after_s)} Stille")
    if c.max_wakeups_per_agent is not None:
        parts.append(f"höchstens {c.max_wakeups_per_agent} Weckungen je Agent")
    return " · ".join(parts)


def _channels_html(channels: list[RunInputChannel]) -> str:
    if not channels:
        return '<p class="small muted">Dieser Run hat keine Gruppenchats.</p>'
    rows = []
    for c in channels:
        extra = []
        if c.anonymous:
            extra.append("anonym")
        if c.open_from:
            extra.append(f"offen ab „{c.open_from}“")
        note = f'<div class="faint small">{esc(" · ".join(extra))}</div>' if extra else ""
        purpose = f'<div class="faint small">{esc(c.purpose)}</div>' if c.purpose else ""
        members = " ".join(f'<span class="chip">{esc(m)}</span>' for m in c.members)
        rows.append(
            f'<tr data-channel="{esc(c.name)}"><td class="mono">#{esc(c.name)}{purpose}{note}</td>'
            f"<td>{members}</td><td class='small'>{esc(_rhythm(c))}</td></tr>"
        )
    return (
        '<table class="tbl compact in-channels"><thead><tr><th>Kanal</th><th>Mitglieder</th>'
        f"<th>Taktung</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _limits_html(inputs: RunInputs) -> str:
    limits = inputs.limits
    budget = fmt.budget(limits.budget_usd) if limits.budget_usd is not None else "ohne Grenze"
    turns = str(limits.turns_per_agent) if limits.turns_per_agent else "ohne Grenze"
    wall = fmt.limit_hours(limits.wall_time_s)
    timeout = (
        f"<dt>Frist einer Freigabe</dt><dd>{esc(fmt.duration(inputs.approval_timeout_s))}</dd>"
        if inputs.approval_timeout_s
        else ""
    )
    return (
        '<dl class="in-kv">'
        f"<dt>Laufzeit</dt><dd data-limit='wall_time_s'>{esc(wall)}</dd>"
        f"<dt>Turns je Agent</dt><dd data-limit='turns_per_agent'>{esc(turns)}</dd>"
        f"<dt>Budget</dt><dd data-limit='budget_usd'>{esc(budget)}</dd>"
        f"<dt>Freigaben</dt><dd data-decider='{esc(inputs.decider.value)}'>"
        f"{esc(DECIDER[inputs.decider])}</dd>{timeout}</dl>"
    )


def inputs_html(inputs: RunInputs) -> str:
    """The whole view; without `run.yaml` an empty state („wird vorbereitet“)."""
    repeat = (
        f'<a class="btn btn-sm" href="{esc(repeat_href(inputs.run_id))}" data-mark="repeat-inputs">'
        f"{icon('replay', small=True)}{REPEAT_LABEL} …</a>"
    )
    task = inputs.task_md or ""
    copy = (
        f'<button type="button" class="btn btn-sm" data-copy="{esc(task)}" '
        'aria-label="Aufgabe kopieren">⧉ Kopieren</button>'
        if task
        else ""
    )
    task_body = (
        f'<div class="prose in-task">{markdown_html(task)}</div>'
        if task
        else '<p class="small muted">Keine Aufgabe gespeichert.</p>'
    )
    head = (
        '<div class="in-top"><p class="small muted">So wurde dieser Run gestartet. '
        "„Wiederholen“ öffnet „Neuer Run“ mit allem vorbelegt; gestartet wird erst dort.</p>"
        f"{repeat}</div>"
    )
    if not inputs.available:
        return (
            head
            + _section("Aufgabe", task_body, anchor="aufgabe", extra=copy)
            + empty_state_html(
                "hourglass_empty",
                "Die übrigen Eingaben folgen gleich",
                "Agenten, Kanäle und Limits stehen hier, sobald arr den Run vorbereitet hat.",
            )
        )
    skill = (
        f'<span class="mono">{esc(inputs.skill)}</span>'
        if inputs.skill
        else '<span class="faint">ohne Skill</span>'
    )
    topology = f" · Topologie {esc(inputs.topology.value)}" if inputs.topology else ""
    project = (
        f'<dl class="in-kv"><dt>Projekt</dt><dd class="mono" data-project>{esc(inputs.project)}'
        f"</dd><dt>Skill</dt><dd data-skill>{skill}{topology}</dd></dl>"
    )
    yaml = (
        '<details class="in-yaml"><summary><b>Aufgelöste run.yaml</b> '
        '<span class="faint small">· nur lesen</span></summary>'
        f'<pre class="cmd yaml">{esc(inputs.run_yaml or "")}</pre></details>'
        if inputs.run_yaml
        else ""
    )
    return (
        head
        + _section("Aufgabe", task_body, anchor="aufgabe", extra=copy)
        + _section("Projekt und Skill", project, anchor="projekt")
        + _section("Parameter", _params_html(inputs.params), anchor="parameter")
        + _section("Agenten", _agents_html(inputs.agents), anchor="agenten")
        + _section("Gruppenchats und Taktung", _channels_html(inputs.channels), anchor="kanaele")
        + _section("Limits und Freigaben", _limits_html(inputs), anchor="limits")
        + yaml
    )


class InputsView:
    """The view „Eingaben“; read again when the run reaches a new status."""

    def __init__(self, page: "RunDetailPage") -> None:
        self.page = page
        self.body: Any = None
        self.html = ""

    async def build(self) -> None:
        page = self.page
        self.html = inputs_html(await page.api.get_run_inputs(page.run_id))
        self.body = raw(self.html, classes="rd-scroll pad in-view").props(
            'data-follow="0" aria-label="Eingaben" tabindex="0"'
        )

    async def refresh(self) -> None:
        if self.body is None:
            return
        html = inputs_html(await self.page.api.get_run_inputs(self.page.run_id))
        if html != self.html:
            self.html = html
            self.body.set_content(html)

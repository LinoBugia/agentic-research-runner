"""The phase plan as a table (W4b-07 §6.2, docs/10 §4.9).

One component for the skills page (with a jump into the line of the `group.yaml`), the tab
„Ablauf“ of the run detail (with passes, gate result, usage and estimate per phase) and the
run-start card (with the decider per phase and agent). Every text is escaped here; the rows
come from `arr.service.real_api.phase_plan`, where texts from the YAML are made one line.
"""

from urllib.parse import quote

from arr.service.api_models import PhasePlan, PhaseRow
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, icon


def _list(items: list[str]) -> str:
    return esc(", ".join(items)) if items else "–"


def _actions(row: PhaseRow) -> str:
    parts = []
    for action in row.actions:
        who = action.key
        if action.agents and action.agents != [action.key]:
            who += f" ({', '.join(action.agents)})"
        task = action.task.rsplit("/", 1)[-1].removesuffix(".md") or action.task
        name = f" „{action.name}“" if action.name else ""
        parts.append(
            f'<div><span class="mono">{esc(who)}</span>: <span title="{esc(action.task)}">'
            f"{esc(task)}</span> → <code>{esc(action.format)}</code>{esc(name)}</div>"
        )
    return "".join(parts) or "–"


def _usage(row: PhaseRow) -> str:
    usage = row.usage
    if usage is None or not usage.turns:
        return "–"
    turns = f"{usage.turns} Turn" + ("s" if usage.turns != 1 else "")
    parts = [turns]
    if usage.tokens is not None:
        parts.append(f"{fmt.tokens(usage.tokens)} Token")
    if usage.cost_usd is not None:
        parts.append(fmt.money(usage.cost_usd, usage.cost_kind))
    return esc(" · ".join(parts))


def _visits(row: PhaseRow) -> str:
    seen = row.visits or 0
    if row.max_visits is not None:
        return esc(f"{seen} / {row.max_visits}")
    return esc(str(seen))


def phase_name_html(row: PhaseRow, skill: str | None) -> str:
    """The phase name; on the skills page a link into its line of the `group.yaml`."""
    name = f'<span class="mono">{esc(row.name)}</span>'
    if skill and row.line:
        href = f"/skills/{quote(skill, safe='')}?tab=yaml&zeile={row.line}"
        return (
            f'<a href="{esc(href)}" title="{esc(f"group.yaml, Zeile {row.line}")}" '
            f'data-mark="phase-line-{esc(row.name)}">{name}</a>'
        )
    return name


def phase_table_html(
    plan: PhasePlan,
    *,
    skill: str | None = None,
    run: bool = False,
    compact: bool = False,
    caption: str = "Phasenplan",
) -> str:
    """The phase plan as a table; `run` adds passes, gate result, usage and estimate;
    `compact` (run-start card) leaves out context and channels."""
    heads = ["Phase", "Topologie", "Actions (Agent: Auftrag → Abgabe)", "Auf Abruf"]
    if not compact:
        heads += ["Kanäle", "Kontext"]
    heads += ["Gate und Routen", "Entscheider"]
    if run:
        heads += ["Durchläufe", "Gate-Ergebnis", "Usage"]
    heads.append("Schätzung (Turns)")
    rows = []
    for row in plan.rows:
        decider = esc(row.decider_text)
        if row.decider_user:
            decider = f'<b class="warn-text">{icon("front_hand", small=True)}{decider}</b>'
        cells = [
            phase_name_html(row, skill),
            esc(row.topology_text),
            _actions(row),
            _list(row.standby),
        ]
        if not compact:
            cells += [_list([f"#{c}" for c in row.channels]), esc(row.context_text)]
        cells += [esc(row.gate_text), decider]
        if run:
            cells += [_visits(row), esc(row.gate_result or "–"), _usage(row)]
        cells.append(esc(row.estimate_text or "–"))
        rows.append(
            f'<tr data-phase="{esc(row.name)}">' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
        )
    head = "".join(f"<th>{esc(h)}</th>" for h in heads)
    extra = ""
    if plan.dropped:
        extra += (
            '<p class="small muted" data-mark="phases-dropped">Entfällt mit diesen Parametern: '
            f"{esc(', '.join(plan.dropped))}</p>"
        )
    for note in plan.notes:
        extra += f'<p class="small muted" data-mark="phases-note">{esc(note)}</p>'
    return (
        '<div class="tbl-wrap phase-plan" data-mark="phase-plan">'
        f'<table class="tbl compact" aria-label="{esc(caption)}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>{extra}"
    )

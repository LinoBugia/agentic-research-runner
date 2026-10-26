"""K3 agent card with the „jetzt“ line (docs/08-ux-konzept.md K3, G1, §6.5 „Auswahlspalte“).

The card is one link (`<a>`); the server renders all parts, the column decides how much is
shown by space, not by window width (static/run_detail.js sets `lvl-full`, `lvl-2` or `lvl-1`
on the column): full (role, model, assignment, meta) → two lines (ID + status, „jetzt“) →
one line (ID, short „jetzt“, status). The complete „jetzt“ line and the agent's details are
always in the tooltip (UX-Abnahme 1). Running tools show their duration, updated in the
browser (`data-since`).
"""

from nicegui import ui

from arr.service.api_models import ActivityKind, AgentDisplayState, AgentState
from arr.service.gui import format as fmt
from arr.service.gui.components.badge import agent_badge_html
from arr.service.gui.components.base import esc, icon

ORDER = {
    AgentDisplayState.WAITING_USER: 0,
    AgentDisplayState.ERROR: 1,
    AgentDisplayState.BLOCKED: 2,
    AgentDisplayState.WORKING: 3,
    AgentDisplayState.IDLE: 4,
    AgentDisplayState.DONE: 5,
}
"""Sort order of the cards (§6.5): „wartet auf dich“ always on top."""

_NOW_ICON = {
    ActivityKind.TOOL: "play_arrow",
    ActivityKind.TEXT: "chat_bubble_outline",
    ActivityKind.THINKING: "psychology",
    ActivityKind.WAIT_MESSAGES: "radio_button_unchecked",
    ActivityKind.WAIT_STAGE: "schedule",
    ActivityKind.WAIT_APPROVAL: "hourglass_top",
    ActivityKind.BLOCKED: "hourglass_top",
    ActivityKind.DONE: "check",
    ActivityKind.ERROR: "error_outline",
}


def sort_key(agent: AgentState) -> int:
    """Key for the card order (§6.5); `sorted` keeps the order of the data within a state."""
    return ORDER[agent.state]


def now_text(agent: AgentState) -> str:
    """The „jetzt“ line as plain text, e.g. „Bash · curl -sO … (wartet auf deine Freigabe)“."""
    act = agent.activity
    if act is None:
        return "noch keine Tätigkeit"
    text = act.label + (f" · {act.arg}" if act.arg else "")
    if act.kind is ActivityKind.WAIT_APPROVAL:
        text += " (wartet auf deine Freigabe)"
    elif act.kind is ActivityKind.WAIT_MESSAGES and agent.max_wakeups:
        text += f" · geweckt {agent.wakeups} von {agent.max_wakeups}"
    return text


def _now_html(agent: AgentState) -> str:
    act = agent.activity
    if act is None:
        return '<span class="now"><span class="k">noch keine Tätigkeit</span></span>'
    sym = icon(_NOW_ICON[act.kind], small=True)
    if act.arg:
        head = f'<span class="k">{sym}{esc(act.label)} ·</span> {esc(act.arg)}'
    else:
        head = f'<span class="k">{sym}</span>{esc(act.label)}'
    tail = ""
    if act.kind is ActivityKind.TOOL and act.running and act.since:
        tail = f' <span class="k">· <span data-since="{act.since:.0f}"></span></span>'
    elif act.kind is ActivityKind.WAIT_MESSAGES and agent.max_wakeups:
        tail = f' <span class="k">· geweckt {agent.wakeups} von {agent.max_wakeups}</span>'
    return f'<span class="now">{head}{tail}</span>'


def short_now(agent: AgentState) -> str:
    """Short „jetzt“ for one-line cards: the argument or the label."""
    act = agent.activity
    if act is None:
        return ""
    return act.arg if act.arg and act.kind is not ActivityKind.BLOCKED else act.label


def tooltip(agent: AgentState) -> str:
    """Tooltip with everything a one-line card hides (§6.5, UX-Abnahme 1)."""
    lines = [f"{agent.id} · {agent.role_label} · {agent.model_label}"]
    lines.append(f"jetzt: {now_text(agent)}")
    if agent.blocked_by is not None and agent.state is AgentDisplayState.BLOCKED:
        lines.append(agent.blocked_by.summary)
    if agent.questions_to_user:
        n = agent.questions_to_user
        lines.append(f"{n} Frage an dich" if n == 1 else f"{n} Fragen an dich")
    if agent.assignment_label:
        lines.append(f"Auftrag: {agent.assignment_label}")
    turns = f"Turn {agent.turn}" + (f" von {agent.turns_limit}" if agent.turns_limit else "")
    lines.append(turns)
    return "\n".join(lines)


def card_html(agent: AgentState) -> str:
    """Inner HTML of a card: all parts; CSS shows them by compact level."""
    q = ""
    if agent.questions_to_user:
        n = agent.questions_to_user
        q = f'<span class="q">? {"an dich" if n == 1 else f"{n} an dich"}</span>'
    meta = [f"Turn {agent.turn}" + (f" von {agent.turns_limit}" if agent.turns_limit else "")]
    if agent.cost_usd is not None:
        meta.append(fmt.money(agent.cost_usd, agent.cost_kind))
    elif agent.tokens:
        meta.append(f"{fmt.tokens(agent.tokens)} Tokens")
    model = f"{agent.role_label} · {agent.model_label}"
    if agent.assignment_label:
        model += f" · {agent.assignment_label}"
    short = esc(short_now(agent))
    return (
        f'<span class="top"><span class="id">{esc(agent.id)}</span>'
        f'<span class="short">{q or short}</span>{agent_badge_html(agent.state)}</span>'
        f'<span class="model">{esc(model)}</span>{_now_html(agent)}'
        f"{f'<span class=qrow>{q}</span>' if q else ''}"
        f'<span class="meta">{esc(" · ".join(meta))}</span>'
    )


class AgentCard:
    """One card in the selection column; `update()` keeps the link (and its focus)."""

    def __init__(self, agent: AgentState, href: str, *, selected: bool = False) -> None:
        self.agent = agent
        self.element = ui.html("", sanitize=False, tag="a").mark(f"agent-{agent.id}")
        self.element._props.update({"href": href, "data-sel": f"agent:{agent.id}"})
        self.selected = selected
        self.update(agent)

    def update(self, agent: AgentState, *, selected: bool | None = None) -> None:
        """Show a new state of the agent (live)."""
        self.agent = agent
        if selected is not None:
            self.selected = selected
        classes = "agent"
        if agent.state is AgentDisplayState.WAITING_USER:
            classes += " needs-me"
        if self.selected:
            classes += " sel"
        self.element.classes(replace=classes)
        props = self.element._props
        props["title"] = tooltip(agent)
        if self.selected:
            props["aria-current"] = "true"
        else:
            props.pop("aria-current", None)
        self.element.set_content(card_html(agent))
        self.element.update()

    def select(self, selected: bool) -> None:
        """Mark the card as the current selection (`aria-current`)."""
        self.update(self.agent, selected=selected)

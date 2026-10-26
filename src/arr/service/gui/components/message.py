"""K4 message line, K2 kind chip, K15 hint „Fragen an dich“ (docs/08-ux-konzept.md K2, K4, K15).

A message shows short sender, sender, kind, reference, addressees, time, the body as Markdown
with formulas, attachments and a foot (delivery state of your own messages, answers, link to
the sender's transcript). Variants: open question (orange edge + „? offen“), from you,
„du über Claude Code“, orchestrator (the pinned profile is collapsed and counted), anonymous
(alias plus the real ID, only visible to you), `pass` (one grey line). Kinds have no status
colour (§7.1).
"""

from datetime import datetime

from arr.room.types import DeliveryStatus, MessageKind, MessageVia
from arr.service.api_models import AgentDisplayState, AgentState, Message
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, icon, markdown_html

KIND = {
    MessageKind.NOTE: ("✎", "Notiz"),
    MessageKind.IDEA: ("💡", "Idee"),
    MessageKind.CLAIM: ("⊢", "Behauptung"),
    MessageKind.PROOF: ("∎", "Beweis"),
    MessageKind.OBJECTION: ("⚑", "Einwand"),
    MessageKind.QUESTION: ("?", "Frage"),
    MessageKind.ANSWER: ("↩", "Antwort"),
    MessageKind.PROPOSAL: ("◇", "Vorschlag"),
    MessageKind.VOTE: ("☑", "Stimme"),
    MessageKind.DECISION: ("⚖", "Entscheidung"),
    MessageKind.RESULT: ("★", "Ergebnis"),
    MessageKind.PASS: ("⤼", "Passe"),
}
"""K2: symbol + word per kind (docs/06 §3)."""

COMPOSE_KINDS = [MessageKind.NOTE, MessageKind.QUESTION, MessageKind.OBJECTION, MessageKind.IDEA]
"""Kinds offered in the input bar (§6.5)."""


def kind_chip(kind: MessageKind) -> str:
    """K2 chip: symbol + word, neutral."""
    sym, word = KIND[kind]
    return f'<span class="kind"><span class="s" aria-hidden="true">{sym}</span>{esc(word)}</span>'


def short_name(sender: str) -> str:
    """Avatar text: „ex1“ for explorer-1, „gpt“ for explorer-gpt, „cri“ for critic, „du“."""
    if sender == "user":
        return "du"
    parts = sender.split("-")
    if len(parts) > 1 and parts[-1].isdigit():
        return parts[0][:2] + parts[-1]
    if len(parts) > 1:
        return parts[-1][:3]
    return sender[:3]


def who(name: str) -> str:
    """Addressee as shown after „an“: the user is „dich“, agents keep their ID."""
    return "dich" if name == "user" else name


def question_hint_html(count: int, href: str | None = None, *, compact: bool = False) -> str:
    """K15 pill „? n Fragen an dich“: neutral, never the colour of „wartet auf dich“."""
    if not count:
        return ""
    if compact:
        text = "? an dich" if count == 1 else f"? {count} an dich"
    else:
        text = f"? {count} Frage an dich" if count == 1 else f"? {count} Fragen an dich"
    tip = (
        "Fragen von Agenten an dich. Sie zählen nicht als „wartet auf dich“: kein Badge, keine "
        "Freigabe, keine Mitteilung."
    )
    if href:
        return f'<a class="qchip" href="{esc(href)}" title="{esc(tip)}">{esc(text)}</a>'
    return f'<span class="qchip" title="{esc(tip)}">{esc(text)}</span>'


def delivery_text(msg: Message, *, paused: bool = False, escalation: bool = False) -> str:
    """Delivery state under your own message (§6.5), e.g. „✓ explorer-2 geweckt · 15:13“."""
    if msg.delivery is None:
        return ""
    at = fmt.clock(msg.delivery.ts)
    status = msg.delivery.status
    if escalation and status in (DeliveryStatus.PENDING, DeliveryStatus.NEXT_INBOX):
        return "⧖ Zustellung, sobald du die Eskalation entschieden hast"
    if paused and status in (DeliveryStatus.PENDING, DeliveryStatus.NEXT_INBOX):
        return "⧖ Zustellung beim Fortsetzen"
    if status is DeliveryStatus.HINT_MID_TURN:
        return f"✓ zugestellt als Hinweis mitten im Turn · {at}"
    if status is DeliveryStatus.WOKEN:
        target = fmt.join_de(msg.to) if msg.to else "die Mitglieder"
        return f"✓ {target} geweckt · {at}"
    if status is DeliveryStatus.NEXT_INBOX:
        return "⧖ kommt in den nächsten Posteingang"
    return "⧖ wird zugestellt …"


def message_dom_id(channel: str, msg_id: int) -> str:
    """DOM id of a message, the target of „zu #5“ and of jumps from the overview."""
    return f"m-{channel}-{msg_id}"


def message_html(
    msg: Message,
    *,
    aliases: dict[str, str] | None = None,
    agents: dict[str, AgentState] | None = None,
    paused: bool = False,
    escalation: bool = False,
) -> str:
    """One message as HTML (K4)."""
    dom = message_dom_id(msg.channel, msg.id)
    time = (
        f'<time class="time" datetime="{datetime.fromtimestamp(msg.ts).isoformat()}">'
        f"{fmt.clock(msg.ts)}</time>"
    )
    if msg.kind is MessageKind.PASS:
        return (
            f'<div class="msg pass" id="{dom}"><span aria-hidden="true">⤼</span> '
            f"{esc(msg.sender)} passt · {time}</div>"
        )
    aliases = aliases or {}
    agents = agents or {}
    css = ["msg"]
    if msg.sender == "user":
        css.append("user")
        name = "du über Claude Code" if msg.via is MessageVia.USER_AGENT else "du"
        sender = f'<span class="who">{esc(name)}</span>'
    elif msg.sender == "orchestrator":
        css.append("orch")
        sender = '<span class="who">orchestrator</span>'
    elif msg.sender in aliases:
        sender = (
            f'<span class="who">{esc(aliases[msg.sender])}</span><span class="real">'
            f"({esc(msg.sender)} · nur für dich sichtbar)</span>"
        )
    elif msg.sender in agents:
        sender = (
            f'<a class="who" href="?agent={esc(msg.sender)}" data-sel="agent:{esc(msg.sender)}" '
            f'title="Transkript von {esc(msg.sender)} öffnen">{esc(msg.sender)}</a>'
        )
    else:
        sender = f'<span class="who">{esc(msg.sender)}</span>'
    if msg.open:
        css.append("open-q")
    head = [sender, kind_chip(msg.kind)]
    if msg.reply_to:
        target = message_dom_id(msg.channel, msg.reply_to)
        head.append(
            f'<span class="re">↪ zu <a href="#{target}" data-jump="{target}">#{msg.reply_to}'
            "</a></span>"
        )
    if msg.to:
        head.append(f'<span class="to">an {esc(", ".join(who(t) for t in msg.to))}</span>')
    if msg.open:
        head.append('<span class="badge st-wait"><span aria-hidden="true">?</span> offen</span>')
    head.append(time)
    body = markdown_html(msg.body)
    if msg.pinned and msg.sender == "orchestrator":
        first = msg.body.split("·")[0].replace("*", "").strip() or "Steckbrief"
        return (
            f'<details class="{" ".join(css)} pinned" id="{dom}"><summary>'
            f'<span class="av" aria-hidden="true">orc</span><span class="hd">{sender}'
            f'{kind_chip(msg.kind)}<span class="muted">📌 {esc(first)} · angeheftet, '
            f"aufklappen</span>{time}</span></summary>"
            f'<div class="body prose">{body}</div></details>'
        )
    refs = ""
    if msg.refs:
        files = "".join(
            f'<span class="att-file" title="{esc(r.path)}">{icon("attach_file", small=True)}'
            f"{esc(r.name)} · {fmt.size(r.size)}</span>"
            for r in msg.refs
        )
        refs = f'<div class="refs">{files}</div>'
    foot = []
    if msg.sender == "user":
        state = delivery_text(msg, paused=paused, escalation=escalation)
        if state:
            foot.append(f"<span>{esc(state)}</span>")
    if msg.reply_count:
        n = msg.reply_count
        foot.append(f"<span>↩ {n} Antwort{'en' if n != 1 else ''}</span>")
    if msg.open and msg.to:
        waiting = [
            t for t in msg.to if t in agents and agents[t].state is AgentDisplayState.WAITING_USER
        ]
        if waiting:
            foot.append(
                f"<span>{esc(fmt.join_de(waiting))} wartet gerade auf eine Freigabe und sieht "
                "die Frage danach</span>"
            )
    foot_html = f'<div class="foot">{"".join(foot)}</div>' if foot else ""
    return (
        f'<div class="{" ".join(css)}" id="{dom}"><span class="av" aria-hidden="true">'
        f"{esc(short_name(msg.sender))}</span><div>"
        f'<div class="hd">{"".join(head)}</div><div class="body prose">{body}</div>'
        f"{refs}{foot_html}</div></div>"
    )

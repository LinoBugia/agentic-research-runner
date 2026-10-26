"""K6 transcript event line (docs/08-ux-konzept.md K6, G5, §6.5 „Verlauf · Agent“).

`turn_start` is a separator plus the collapsed assignment, `text` is open, `thinking` closed,
a tool call is one line with a short result (call and result bundled by the service); errors
and waiting tool calls are open, outputs show at most 10 lines and blobs only as a path. A
`hint` is a line „Hinweis an … (mitten im Turn)“. Collapsed parts are counted (G5).
"""

import re

from arr.service.api_models import ToolStatus, TranscriptEvent, TranscriptKind
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, icon, markdown_html
from arr.service.gui.components.copy_field import copy_html

MAX_OUTPUT_LINES = 10

_TOOL_ICON = {
    "Bash": "terminal",
    "Shell": "terminal",
    "Read": "description",
    "Edit": "edit_note",
    "Write": "edit_note",
    "Datei schreiben": "edit_note",
}


def event_dom_id(seq: int) -> str:
    """DOM id of a transcript line (target of „Im Transkript zeigen“)."""
    return f"seq-{seq}"


def _t(ev: TranscriptEvent) -> str:
    return f'<span class="t">{fmt.clock_s(ev.ts)}</span>'


def _i(name: str) -> str:
    return f'<span class="i">{icon(name, small=True)}</span>'


def _ms(duration_ms: int | None) -> str:
    if duration_ms is None:
        return ""
    if duration_ms < 1000:
        return f"{fmt.decimal(duration_ms / 1000, 1)} s"
    return fmt.duration(duration_ms / 1000)


def _first(text: str, limit: int = 90) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def tool_summary_html(ev: TranscriptEvent) -> str:
    """The `<summary>` of a tool call: time, icon, name, argument, short result."""
    return f"<summary>{tool_summary_inner(ev)}</summary>"


def tool_summary_inner(ev: TranscriptEvent) -> str:
    """Content of a tool call's `<summary>` (for rows built as elements)."""
    tool = ev.tool
    assert tool is not None
    name = tool.name
    arg = f' <span class="arg">{esc(tool.input_summary)}</span>'
    if tool.status is ToolStatus.OK:
        lines = (
            f" · {fmt.integer(tool.output_lines)} Zeile{'n' if tool.output_lines != 1 else ''}"
            if tool.output_lines
            else ""
        )
        res = f"✓ {_ms(tool.duration_ms)}{lines}"
    elif tool.status is ToolStatus.RUNNING:
        res = f'läuft · <span data-since="{ev.ts:.0f}"></span>'
    elif tool.status is ToolStatus.WAITING_APPROVAL:
        res = f'wartet auf deine Freigabe · <span data-since="{ev.ts:.0f}"></span>'
    else:
        res = "✕ Fehler" + (f" · {_ms(tool.duration_ms)}" if tool.duration_ms else "")
    sym = "hourglass_top" if tool.status is ToolStatus.WAITING_APPROVAL else None
    return (
        f"{_t(ev)}{_i(sym or _TOOL_ICON.get(name, 'build'))}"
        f'<span class="s"><span class="nm">{esc(name)}</span>{arg}</span>'
        f'<span class="res">{res}</span>'
    )


def _tool_detail(ev: TranscriptEvent) -> str:
    tool = ev.tool
    assert tool is not None
    parts = [f'<pre class="in">{esc(tool.input_summary)}</pre>']
    if tool.output_excerpt:
        lines = tool.output_excerpt.split("\n")
        shown = "\n".join(lines[:MAX_OUTPUT_LINES])
        parts.append(f"<pre>{esc(shown)}</pre>")
        total = tool.output_lines or len(lines)
        if total > MAX_OUTPUT_LINES:
            parts.append(
                f'<div class="small muted">Ausschnitt: {MAX_OUTPUT_LINES} von '
                f"{fmt.integer(total)} Zeilen</div>"
            )
    if tool.blob_path:
        parts.append(
            '<div class="small muted">Ganze Ausgabe als Datei im Run-Ordner: '
            f"{copy_html(tool.blob_path, label='Pfad der Ausgabe kopieren')}</div>"
        )
    return f'<div class="detail">{"".join(parts)}</div>'


def tool_html(ev: TranscriptEvent) -> str:
    """A bundled tool call; errors open, the rest collapsed to one line (R3, G5)."""
    tool = ev.tool
    assert tool is not None
    css = "ev tool"
    is_open = tool.status is ToolStatus.ERROR
    if tool.status is ToolStatus.ERROR:
        css += " error"
    elif tool.status is ToolStatus.RUNNING:
        css += " running"
    return (
        f'<details class="{css}" id="{event_dom_id(ev.seq)}"{" open" if is_open else ""}>'
        f"{tool_summary_html(ev)}{_tool_detail(ev)}</details>"
    )


def turn_start_html(ev: TranscriptEvent, *, stage_label: str | None = None) -> str:
    """Separator „Turn 6 · 15:10 · Stufe Exploration“ and the collapsed assignment."""
    stage = stage_label or ev.data.get("stage")
    head = f"Turn {ev.turn} · {fmt.clock(ev.ts)}" + (f" · Stufe {stage}" if stage else "")
    prompt = str(ev.data.get("prompt") or "")
    return (
        f'<div id="{event_dom_id(ev.seq)}"><div class="turn-sep">{esc(head)}</div>'
        f'<details class="ev"><summary>{_t(ev)}{_i("input")}<span class="s"><span class="nm">'
        f"Auftrag</span> · „{esc(_first(prompt))}“</span>"
        f'<span class="res"></span></summary><div class="detail"><pre>{esc(prompt)}</pre></div>'
        "</details></div>"
    )


def _sentences(text: str) -> int:
    return max(1, len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]))


def event_html(ev: TranscriptEvent, *, agent_id: str, stage_label: str | None = None) -> str:
    """Any transcript entry except a waiting tool call (that one carries a K5 card)."""
    kind = ev.kind
    dom = event_dom_id(ev.seq)
    if kind is TranscriptKind.TOOL:
        return tool_html(ev)
    if kind is TranscriptKind.TURN_START:
        return turn_start_html(ev, stage_label=stage_label)
    text = str(ev.data.get("text") or "")
    if kind is TranscriptKind.TEXT:
        return (
            f'<div class="ev text" id="{dom}"><div class="line">{_t(ev)}{_i("chat_bubble_outline")}'
            f'<div class="s prose">{markdown_html(text)}</div></div></div>'
        )
    if kind is TranscriptKind.THINKING:
        summarized = bool(ev.data.get("summarized"))
        label = "Thinking (Zusammenfassung)" if summarized else "Thinking"
        n = _sentences(text)
        note = '<span class="small faint">Von Claude zusammengefasst.</span>' if summarized else ""
        return (
            f'<details class="ev thinking" id="{dom}"><summary>{_t(ev)}{_i("psychology")}'
            f'<span class="s">{label} · {esc(_first(text, 70))}</span>'
            f'<span class="res">{n} Satz' + ("" if n == 1 else "e") + "</span></summary>"
            f'<div class="detail"><div class="prose thought">{markdown_html(text)}</div>{note}'
            "</div></details>"
        )
    if kind is TranscriptKind.HINT:
        channel = ev.data.get("channel")
        link = (
            f' · <a href="?kanal={esc(channel)}" data-sel="kanal:{esc(channel)}">#{esc(channel)} '
            "ansehen</a>"
            if channel
            else ""
        )
        return (
            f'<div class="ev hint" id="{dom}"><div class="line">{_t(ev)}{_i("mark_email_unread")}'
            f'<span class="s">Hinweis an {esc(agent_id)} (mitten im Turn): „{esc(text)}“{link}'
            "</span></div></div>"
        )
    if kind is TranscriptKind.TURN_END:
        cost = ev.data.get("cost_usd")
        extra = f" · {fmt.money(cost)}" if isinstance(cost, int | float) else ""
        return (
            f'<div class="ev end" id="{dom}">{_t(ev)}<span>Ende von Turn {ev.turn}{extra}</span>'
            "</div>"
        )
    if kind is TranscriptKind.ERROR:
        message = str(ev.data.get("message") or "Fehler")
        return (
            f'<details class="ev error" id="{dom}" open><summary>{_t(ev)}{_i("error_outline")}'
            f'<span class="s"><span class="nm">Fehler</span> · {esc(_first(message))}</span>'
            f'<span class="res"></span></summary><div class="detail"><pre>{esc(message)}</pre>'
            "</div></details>"
        )
    if kind is TranscriptKind.RATE_LIMIT:
        resets = ev.data.get("resets_at")
        until = f" · weiter um {fmt.clock(resets)}" if isinstance(resets, int | float) else ""
        return (
            f'<div class="ev hint" id="{dom}"><div class="line">{_t(ev)}{_i("speed")}'
            f'<span class="s">Nutzungslimit erreicht{until}</span></div></div>'
        )
    # usage: counted in the turn summary, no line of its own
    return f'<div class="ev end" id="{dom}" hidden></div>'


def turn_group_summary(
    turn_from: int,
    turn_to: int,
    start_ts: float,
    end_ts: float,
    events: int,
    tool_calls: int,
    messages: int | None = None,
) -> str:
    """„Turn 1–5 · 14:12–15:09 · 186 Ereignisse · 11 Tool-Calls · 4 Nachrichten gepostet“."""
    turns = f"Turn {turn_from}" + (f"–{turn_to}" if turn_to != turn_from else "")
    parts = [
        turns,
        f"{fmt.clock(start_ts)}–{fmt.clock(end_ts)}",
        f"{fmt.integer(events)} Ereignisse",
        f"{fmt.integer(tool_calls)} Tool-Calls",
    ]
    if messages:
        parts.append(f"{messages} Nachrichten gepostet")
    return " · ".join(parts)

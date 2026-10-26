"""Run detail, view „Verlauf“ of an agent: the live transcript (docs/08-ux-konzept.md §6.5, K6).

Shows the last 200 entries (§6.6, docs/04 §4); what lies before is counted. Older turns among
them are collapsed into one line with their numbers (G5), the current turn is open. A tool call
waiting for your approval carries the compact approval card (K5). „Live folgen“ is on at the
end of the list; „Im Transkript zeigen“ opens at the entry (`seq`) with following off.
"""

from typing import TYPE_CHECKING

from nicegui import ui

from arr.service.api_models import (
    AgentState,
    ApprovalItem,
    ToolStatus,
    TranscriptEvent,
    TranscriptKind,
)
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, raw
from arr.service.gui.components.transcript_event import (
    event_dom_id,
    event_html,
    tool_summary_inner,
    turn_group_summary,
)
from arr.service.gui.pages.run_detail_channel import follow_toggle_html, jump_button_html

if TYPE_CHECKING:
    from arr.service.gui.pages.run_detail import RunDetailPage

LIVE_MAX = 200
"""Entries kept in the page (docs/08 §6.10 „die letzten 200 Ereignisse“)."""


def _waiting(ev: TranscriptEvent) -> bool:
    return ev.tool is not None and ev.tool.status is ToolStatus.WAITING_APPROVAL


class TranscriptView:
    """The selected agent's transcript."""

    def __init__(self, page: "RunDetailPage", agent: AgentState, *, seq: int | None = None):
        self.page = page
        self.agent = agent
        self.seq = seq
        self.rows: dict[int, ui.element] = {}
        self.events: dict[int, TranscriptEvent] = {}
        self.hidden = 0
        self.group: ui.html | None = None
        self.group_events: list[TranscriptEvent] = []
        self.pending: dict[int, TranscriptEvent] = {}
        self.list: ui.element | None = None
        self.older_line: ui.html | None = None
        self.head: ui.html | None = None

    @property
    def agent_id(self) -> str:
        return self.agent.id

    async def build(self) -> None:
        page = self.page
        api = page.api
        if self.seq is not None:
            result = await api.get_transcript(
                page.run_id, self.agent_id, from_seq=max(0, self.seq - 30), limit=LIVE_MAX
            )
        else:
            result = await api.get_transcript(page.run_id, self.agent_id, limit=LIVE_MAX)
        items = result.items
        first_seq = items[0].seq if items else result.total
        self.hidden = first_seq
        with ui.element("div").classes("pane-head"):
            self.head = raw(self._head_html(), tag="span", classes="pane-info")
            raw(follow_toggle_html(), tag="span")
        open_at, follow = ("", "1")
        if self.seq is not None:
            open_at, follow = event_dom_id(self.seq), "0"
        if result.next_from_seq is not None:
            follow = "0"  # not at the end: newer entries exist below the loaded page
        scroll = (
            ui.element("div")
            .classes("rd-scroll")
            .props(
                f'data-follow="{follow}" data-open-at="{open_at}" '
                f'aria-label="Transkript von {esc(self.agent_id)}" tabindex="0"'
            )
        )
        # entries of earlier turns are collapsed into one counted line (G5)
        last_start = max(
            (i for i, ev in enumerate(items) if ev.kind is TranscriptKind.TURN_START), default=0
        )
        if self.seq is not None and items and items[last_start].seq > self.seq:
            last_start = 0  # the wanted entry must stay visible
        older, current = items[:last_start], items[last_start:]
        with scroll:
            self.older_line = raw(self._older_html(), classes="older-line")
            if older:
                self.group_events = older
                self.group = raw(self._group_html(older), classes="turn-group-row")
            self.list = ui.element("div").classes("rd-list")
            with self.list:
                if not items:
                    raw(
                        '<p class="muted pad">Noch keine Ereignisse. Der Agent bekommt gerade '
                        "seinen ersten Auftrag.</p>"
                    )
                for ev in current:
                    self._add(ev)
        raw(jump_button_html(), tag="div").classes("jump-slot")

    def _head_html(self) -> str:
        a = self.agent
        turn = f"Turn {a.turn}" + (f" von {a.turns_limit}" if a.turns_limit else "")
        return (
            f'<b class="mono">{esc(a.id)}</b> · Transkript · '
            f"{esc(a.model_label)} · {esc(a.auth_profile)} · {esc(turn)}"
        )

    def _older_html(self) -> str:
        if not self.hidden:
            return ""
        return (
            f'<span aria-hidden="true">▲</span> {fmt.integer(self.hidden)} ältere Ereignisse · '
            "nicht geladen"
        )

    def _group_html(self, events: list[TranscriptEvent]) -> str:
        tools = sum(1 for ev in events if ev.kind is TranscriptKind.TOOL)
        summary = turn_group_summary(
            min(ev.turn for ev in events) or 1,
            max(ev.turn for ev in events) or 1,
            events[0].ts,
            events[-1].ts,
            len(events),
            tools,
        )
        labels = self.page.stage_labels
        inner = "".join(
            event_html(ev, agent_id=self.agent_id, stage_label=labels.get(ev.data.get("stage")))
            for ev in events
        )
        return (
            f'<details class="turn-group"><summary><span aria-hidden="true">▸</span> '
            f'{esc(summary)}<span class="spacer"></span><span class="faint">aufklappen</span>'
            f"</summary>{inner}</details>"
        )

    def _add(self, ev: TranscriptEvent) -> None:
        assert self.list is not None
        self.events[ev.seq] = ev
        with self.list:
            if _waiting(ev):
                self.rows[ev.seq] = self._waiting_row(ev)
            else:
                label = self.page.stage_labels.get(ev.data.get("stage"))
                self.rows[ev.seq] = raw(
                    event_html(ev, agent_id=self.agent_id, stage_label=label), classes="ev-row"
                )

    def _waiting_row(self, ev: TranscriptEvent) -> ui.element:
        """A tool call that waits for you: open, orange, with the compact card (K6, K5)."""
        row = ui.element("details").classes("ev waiting").props(f'open id="{event_dom_id(ev.seq)}"')
        with row:
            raw(tool_summary_inner(ev), tag="summary")
            with ui.element("div").classes("detail"):
                item = self._approval(ev)
                if item is not None:
                    self.page.compact_card(item, prefix="tc-")
                else:
                    raw(
                        '<p class="small muted">Die Anfrage ist nicht mehr offen.</p>',
                        tag="div",
                    )
        return row

    def _approval(self, ev: TranscriptEvent) -> ApprovalItem | None:
        approval_id = ev.tool.approval_id if ev.tool else None
        return self.page.open_items.get(approval_id) if approval_id else None

    # --- live -------------------------------------------------------------------------------

    def on_event(self, ev: TranscriptEvent) -> None:
        """A new or changed transcript entry of this agent."""
        if self.list is None:
            return
        old = self.rows.get(ev.seq)
        if old is not None:
            previous = self.events[ev.seq]
            if _waiting(previous) and previous.tool.approval_id in self.page.deciding:  # type: ignore[union-attr]
                self.pending[ev.seq] = ev  # the confirmation stays in place for 1.5 s (G2)
                return
            if _waiting(ev) == _waiting(self.events[ev.seq]) and not _waiting(ev):
                self.events[ev.seq] = ev
                label = self.page.stage_labels.get(ev.data.get("stage"))
                old.set_content(event_html(ev, agent_id=self.agent_id, stage_label=label))
                return
            index = list(self.list.default_slot.children).index(old)
            self._add(ev)
            self.rows[ev.seq].move(self.list, target_index=index)
            old.delete()
            return
        if self.group_events and ev.seq <= self.group_events[-1].seq:
            return  # part of the collapsed earlier turns
        self._add(ev)
        self._trim()

    def update_agent(self, agent: AgentState) -> None:
        """Turn number and model in the head follow the agent card."""
        self.agent = agent
        if self.head is not None:
            self.head.set_content(self._head_html())

    def refresh_waiting(self) -> None:
        """Rebuild waiting rows after an approval changed (card decided or expired)."""
        for seq, ev in list(self.events.items()):
            if _waiting(ev) and seq in self.rows and seq not in self.pending:
                self.on_event(ev.model_copy())

    def flush_pending(self) -> None:
        """Apply the changes that arrived while a decision showed its confirmation."""
        pending, self.pending = self.pending, {}
        for ev in pending.values():
            self.on_event(ev)

    def row_selector(self, approval_id: str) -> str:
        """CSS selector of the tool call row that waited for `approval_id` (focus target)."""
        for seq, ev in self.events.items():
            if ev.tool is not None and ev.tool.approval_id == approval_id:
                return f"#{event_dom_id(seq)} > summary"
        return ""

    def _trim(self) -> None:
        """Keep the last LIVE_MAX entries (collapsed ones included); count what leaves."""
        excess = len(self.rows) + len(self.group_events) - LIVE_MAX
        if excess <= 0:
            return
        if self.group is not None:
            cut = min(excess, len(self.group_events))
            self.group_events = self.group_events[cut:]
            self.hidden += cut
            excess -= cut
            if self.group_events:
                self.group.set_content(self._group_html(self.group_events))
            else:
                self.group.delete()
                self.group = None
        for _ in range(excess):
            oldest = min(self.rows)
            self.rows.pop(oldest).delete()
            self.events.pop(oldest, None)
            self.hidden += 1
        if self.older_line is not None:
            self.older_line.set_content(self._older_html())

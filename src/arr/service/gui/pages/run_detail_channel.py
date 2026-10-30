"""Run detail, view „Verlauf“ of a channel (docs/08-ux-konzept.md §6.5, §6.6, K4).

The channel opens at the separator „n neu seit deinem letzten Blick“ with one or two older
messages above it and „Live folgen“ off; at the end of the list it is on (static/run_detail.js
keeps the position, the server only appends). Older messages are counted, not loaded. The
input bar below sends to everyone or one member; the delivery state appears under the message
(§6.5). „gelesen bis“ lives in the browser (`ui_seen`, §5.0).
"""

from typing import TYPE_CHECKING, Any

from nicegui import ui

from arr.models import ChannelMode, RunStatus
from arr.room.types import MessageKind
from arr.service.api_models import (
    ApiException,
    ChannelInfo,
    Message,
    PostMessageRequest,
    RunAction,
)
from arr.service.gui import format as fmt
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import esc, icon, raw
from arr.service.gui.components.message import (
    COMPOSE_KINDS,
    KIND,
    message_dom_id,
    message_html,
)

if TYPE_CHECKING:
    from arr.service.gui.pages.run_detail import RunDetailPage

LIVE_MAX = 200
"""Messages kept in the page; older ones are dropped from the top (docs/04 §4)."""
CONTEXT = 20
"""Older messages loaded above the separator „neu“; the view opens with one or two of them
visible (§6.5), the rest is there when you scroll up."""

_MODE = {
    ChannelMode.EVENT: "ereignisgesteuert",
    ChannelMode.ROUNDS: "in Runden",
    ChannelMode.MODERATED: "moderiert",
}


def unread_count(info: ChannelInfo, seen: int | None) -> int:
    """„n neu“: messages after the last seen ID; without browser state the service hint."""
    last = info.last_message_id or 0
    if seen is None:
        return min(info.unread_for_user, last)
    return max(0, last - seen)


class ChannelView:
    """The selected channel: head, messages, separator „neu“, input bar."""

    def __init__(
        self, page: "RunDetailPage", info: ChannelInfo, *, jump_to: int | None = None
    ) -> None:
        self.page = page
        self.info = info
        self.jump_to = jump_to
        self.rows: dict[int, ui.html] = {}
        self.messages: dict[int, Message] = {}
        self.older = 0
        self.list: ui.element | None = None
        self.older_line: ui.html | None = None
        self.last_id = info.last_message_id or 0

    @property
    def name(self) -> str:
        return self.info.name

    # --- build ----------------------------------------------------------------------------

    async def build(self) -> None:
        page = self.page
        info = self.info
        members = ", ".join(m.agent for m in info.members)
        questions = ""
        if info.open_questions:
            n = info.open_questions
            questions = (
                f' · <a href="#" data-jump-first=".msg.open-q">? {n} offene '
                f"Frage{'n' if n != 1 else ''}</a>"
            )
        anon = " · anonym" if info.anonymous else ""
        raw(
            f'<div class="pane-info"><b class="mono" title="{esc(info.purpose or "")}">'
            f'#{esc(info.name)}</b> · <span title="{esc(members)}">{len(info.members)} '
            f"Mitglieder</span> · {_MODE[info.mode]}{anon} · "
            f'<span data-count="{esc(info.name)}">{fmt.integer(info.message_count)}</span> '
            f"Nachrichten{questions}</div>" + follow_toggle_html(),
            classes="pane-head",
        )
        if not info.open:
            stage = info.opens_at_stage or "einer späteren Stufe"
            raw(
                empty_state_html(
                    "lock_clock",
                    f"#{info.name} öffnet in Stufe „{stage}“",
                    "Bis dahin arbeiten die Mitglieder unabhängig voneinander. Danach erscheinen "
                    "die Nachrichten hier.",
                ),
                classes="rd-scroll pad",
            )
            return
        unread = unread_count(info, page.seen.get(info.name))
        first_new = self.last_id - unread + 1 if unread else None
        items = await self._load(first_new)
        open_at = ""
        follow = "1"
        if self.jump_to is not None:
            open_at, follow = message_dom_id(info.name, self.jump_to), "0"
        elif first_new is not None:
            open_at, follow = "rd-new", "0"
        scroll = (
            ui.element("div")
            .classes("rd-scroll")
            .props(
                f'data-follow="{follow}" data-open-at="{open_at}" role="log" aria-live="off" '
                f'aria-label="Nachrichten in #{esc(info.name)}" tabindex="0"'
            )
        )
        with scroll:
            self.older_line = raw(self._older_html(), classes="older-line")
            pinned = await self._pinned(items)
            for msg in pinned:
                raw(self._html(msg), classes="pinned-row")
            self.list = ui.element("div").classes("msgs rd-list")
            with self.list:
                if not items:
                    raw(
                        '<p class="muted pad">Noch keine Nachrichten. Die Agenten bekommen '
                        "gerade ihren ersten Auftrag.</p>"
                    )
                for msg in items:
                    if first_new is not None and msg.id == first_new:
                        raw(self._separator_html(unread), classes="sep-row")
                    self._add(msg)
        raw(jump_button_html(), tag="div").classes("jump-slot")
        self.composer_box = ui.element("div").classes("cmp-slot")
        with self.composer_box:
            self._composer()

    async def _load(self, first_new: int | None) -> list[Message]:
        api, run_id = self.page.api, self.page.run_id
        if self.jump_to is not None:
            since = max(0, self.jump_to - 1 - CONTEXT)
            result = await api.get_messages(run_id, self.name, since_id=since, limit=LIVE_MAX)
        elif first_new is not None:
            since = max(0, first_new - 1 - CONTEXT)
            limit = min(LIVE_MAX, self.last_id - since)
            result = await api.get_messages(run_id, self.name, since_id=since, limit=limit)
            if result.items and result.items[-1].id < self.last_id:
                # more new messages than fit: show the newest, the separator stays on top
                result = await api.get_messages(run_id, self.name, limit=LIVE_MAX)
        else:
            result = await api.get_messages(run_id, self.name, limit=50)
        self.older = result.items[0].id - 1 if result.items else 0
        return result.items

    async def _pinned(self, items: list[Message]) -> list[Message]:
        loaded = {m.id for m in items}
        out = []
        for pid in self.info.pinned:
            if pid in loaded:
                continue
            page = await self.page.api.get_messages(
                self.page.run_id, self.name, before_id=pid + 1, limit=1
            )
            out += [m for m in page.items if m.id == pid]
        return out

    def _older_html(self) -> str:
        if not self.older:
            return ""
        n = self.older
        return (
            f'<span aria-hidden="true">▲</span> {fmt.integer(n)} ältere Nachricht'
            f"{'en' if n != 1 else ''} · nicht geladen"
        )

    def _separator_html(self, unread: int) -> str:
        since = self.page.seen_at.get(self.name)
        at = f" ({fmt.clock(since)})" if since else ""
        return (
            f'<div class="newline" id="rd-new" role="separator">{fmt.integer(unread)} neu seit '
            f"deinem letzten Blick{at}</div>"
        )

    def _html(self, msg: Message) -> str:
        aliases = {m.agent: m.alias for m in self.info.members if m.alias}
        paused = self.page.status is RunStatus.PAUSED
        escalation = self.page.detail.run.open_escalation
        return message_html(
            msg, aliases=aliases, agents=self.page.agents, paused=paused, escalation=escalation
        )

    def _add(self, msg: Message) -> None:
        assert self.list is not None
        with self.list:
            self.rows[msg.id] = raw(self._html(msg), classes="msg-row")
        self.messages[msg.id] = msg

    # --- input bar --------------------------------------------------------------------------

    def _composer(self) -> None:
        page = self.page
        info = self.info
        if page.status.is_final:
            raw(
                f"{icon('info', small=True)} Der Run ist beendet. Nachrichten gehen nur an "
                "laufende Runs.",
                classes="composer-off small muted",
            )
            return
        if page.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            # waiting for approval, queued, preparing, finalizing: input off (§6.7)
            raw(
                f"{icon('info', small=True)} Nachrichten gehen erst, wenn der Run läuft.",
                classes="composer-off small muted",
            )
            return
        if self._in_terminal():
            raw(
                f"{icon('info', small=True)} Der Run läuft im Terminal. Nachrichten gehen nur "
                "an Runs, die du in arr gestartet hast.",
                classes="composer-off small muted",
            )
            return
        options = [f'<option value="">alle in #{esc(info.name)}</option>'] + [
            f'<option value="{esc(m.agent)}">{esc(m.alias or m.agent)}</option>'
            for m in info.members
        ]
        kinds = "".join(
            f'<option value="{k.value}">{KIND[k][0]} {esc(KIND[k][1])}</option>'
            for k in COMPOSE_KINDS
        )
        hint = ""
        if page.detail.run.open_escalation:
            # no „Fortsetzen“ here: the run goes on only after your decision (§6.7)
            hint = (
                '<span class="small muted cmp-hint">Zustellung, sobald du die Eskalation '
                "entschieden hast</span>"
            )
        elif page.status is RunStatus.PAUSED:
            hint = '<span class="small muted cmp-hint">Zustellung beim Fortsetzen</span>'
        elif info.mode is ChannelMode.ROUNDS:
            hint = '<span class="small muted cmp-hint">kommt in den nächsten Posteingang</span>'
        form = (
            ui.element("form")
            .classes("composer")
            .props(f'aria-label="Nachricht in #{esc(info.name)} schreiben" onsubmit="return false"')
        )
        with form:
            raw(
                f'<label class="sr-only" for="cmp-to">An</label><select id="cmp-to" '
                f'name="to">{"".join(options)}</select>'
                f'<label class="sr-only" for="cmp-kind">Art</label><select id="cmp-kind" '
                f'name="kind">{kinds}</select>'
                f'<label class="sr-only" for="cmp-body">Nachricht (Markdown und LaTeX)</label>'
                f'<textarea id="cmp-body" name="body" rows="1" '
                f'placeholder="Nachricht an #{esc(info.name)} …" '
                'title="Markdown und LaTeX ($…$) möglich. Senden mit ⌘↩">'
                f"</textarea>{hint}",
                tag="div",
                classes="cmp-fields",
            )
            send = (
                ui.element("button")
                .classes("btn primary")
                .props('type="button" data-send')
                .mark("send")
            )
            with send:
                raw('Senden <span class="k" aria-hidden="true">⌘↩</span>', tag="span")
            send.on(
                "click",
                self._send,
                js_handler="(e) => { const f = e.target.closest('form'); emit({to: "
                "f.to.value, kind: f.kind.value, body: f.body.value}); }",
            )

    def _in_terminal(self) -> bool:
        """A run of `arr run`: this service can stop it, but neither pause it nor write to it."""
        detail = self.page.detail
        steer = {RunAction.PAUSE, RunAction.RESUME}
        return not detail.run.open_escalation and not steer & set(detail.actions_allowed)

    async def _send(self, event: Any) -> None:
        args = event.args or {}
        body = str(args.get("body") or "").strip()
        if not body:
            self.page.frame.toast("Schreib zuerst eine Nachricht.")
            return
        to = [args["to"]] if args.get("to") else []
        try:
            kind = MessageKind(args.get("kind") or "note")
        except ValueError:
            kind = MessageKind.NOTE
        try:
            msg = await self.page.api.post_message(
                self.page.run_id, self.name, PostMessageRequest(body=body, kind=kind, to=to)
            )
        except ApiException as exc:
            self.page.frame.toast(exc.error.message, error=True)
            return
        self.page.frame.js("arr.rd.sent()")
        self.add_live(msg, own=True)

    # --- live -------------------------------------------------------------------------------

    def add_live(self, msg: Message, *, own: bool = False) -> None:
        """Append a new message (or show a changed one) without moving the reading position."""
        if self.list is None:
            return
        if msg.id in self.rows:
            self.messages[msg.id] = msg
            self.rows[msg.id].set_content(self._html(msg))
            return
        self._add(msg)
        self.last_id = max(self.last_id, msg.id)
        if own:
            self.page.frame.js("arr.rd.toEnd()")
        while len(self.rows) > LIVE_MAX:
            oldest = min(self.rows)
            self.rows.pop(oldest).delete()
            self.messages.pop(oldest, None)
            self.older += 1
        if self.older_line is not None:
            self.older_line.set_content(self._older_html())

    def refresh_status(self) -> None:
        """The run's status changed: redraw delivery notes and the input bar, keep the list
        and the reading position (§5 „ohne Sprünge“)."""
        self.refresh_messages()
        if hasattr(self, "composer_box"):
            self.composer_box.clear()
            with self.composer_box:
                self._composer()

    def refresh_messages(self) -> None:
        """Render the shown messages again (agent states and the run status changed)."""
        for msg_id, row in self.rows.items():
            html = self._html(self.messages[msg_id])
            if row.content != html:
                row.set_content(html)


def follow_toggle_html() -> str:
    return (
        '<button type="button" class="chip follow-toggle" aria-pressed="false" '
        'title="Neue Einträge erscheinen unten; die Ansicht folgt ihnen">'
        '<span class="dot" aria-hidden="true">●</span> Live folgen</button>'
    )


def jump_button_html() -> str:
    return (
        '<button type="button" class="jump" hidden><span aria-hidden="true">▼</span> '
        '<span class="jump-text">Ans Ende · Live folgen</span></button>'
    )

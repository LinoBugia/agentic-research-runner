"""Run detail as control centre, variant A „Leitstand“ (docs/08-ux-konzept.md §5.5, §6).

Head (fixed): status, name, actions (pause/resume, stop with dialog, ⋯ with „Für Claude Code
kopieren“), the banner „Wartet auf dich“ with „Ansehen“ and the hint „? n Fragen an dich“
(does not count as waiting, §8.3), progress bar and key figures. Below three columns that
scroll on their own: selection (channels with „n neu“, agent cards compacted by space),
view („Verlauf“ of the selected channel or agent, „Ergebnisse“) and, from 1440 px, the
overview with compact approval cards (K5, same lock as the Freigaben page: static/arr.js).

Selecting something changes the view without reloading the page and keeps the URL
(`?kanal=`, `?agent=`, `&seq=`, `&nachricht=`, `?ansicht=ergebnisse&ergebnis=`). The run's
live stream is applied at most 10 times a second (§6.6). „gelesen bis“ per channel lives in
the browser (`ui_seen`, §5.0); static/run_detail.js reports it after loading.
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from nicegui import app, ui

from arr.models import RunStatus
from arr.permissions.types import ApprovalStatus, ApprovalType, DecisionAction
from arr.room.types import MessageKind
from arr.service.api_models import (
    AgentActivityEvent,
    AgentDisplayState,
    AgentState,
    AgentStateEvent,
    AgentTranscriptEvent,
    ApiException,
    ApprovalEvent,
    ApprovalItem,
    ChannelInfo,
    ChannelMessageEvent,
    DecisionRequest,
    LiveEvent,
    Message,
    RatingSubmittedEvent,
    ResultSubmittedEvent,
    RunAction,
    RunDetail,
    RunResults,
    RunStatusEvent,
    StageCheckpointEvent,
    UsageTickEvent,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import STATIC_DIR, STATIC_URL, get_api
from arr.service.gui.components.agent_card import AgentCard, sort_key
from arr.service.gui.components.approval_card import compact_card
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import banner
from arr.service.gui.components.base import esc, icon, raw, set_disabled
from arr.service.gui.components.copy_field import copy_html
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.message import message_dom_id, question_hint_html
from arr.service.gui.components.meter import run_meters_html
from arr.service.gui.components.page_state import forced_state
from arr.service.gui.components.stage_bar import stage_bar_html
from arr.service.gui.components.transcript_event import event_dom_id
from arr.service.gui.layout import Frame
from arr.service.gui.pages.run_detail_channel import ChannelView, unread_count
from arr.service.gui.pages.run_detail_overview import (
    agents_html,
    open_questions_html,
    questions_to_user_html,
    results_short_html,
)
from arr.service.gui.pages.run_detail_results import ResultsView, result_dom_id
from arr.service.gui.pages.run_detail_transcript import TranscriptView
from arr.service.gui.texts import RISK, STARTER_PHRASE, WAITING_FOR_YOU, run_status

PUMP_SECONDS = 0.1
"""Live events are applied in batches, at most 10 times a second (§6.6)."""
DECIDED_SECONDS = 1.5
"""A decided compact card shows its confirmation this long (G2, same as the Freigaben page)."""
SCAN_MESSAGES = 200
"""Newest messages per channel searched for open questions (overview, K15)."""

KATEX_URL = "/arr-vendor/katex.js"

View = ChannelView | TranscriptView | ResultsView


def katex_file() -> Path | None:
    """KaTeX as shipped with NiceGUI (inside its Mermaid bundle, no extra dependency).

    Returns None when a NiceGUI version moves it; formulas then stay readable Unicode text.
    """
    import nicegui

    dist = Path(nicegui.__file__).parent / "elements" / "mermaid" / "dist"
    return next(iter(sorted(dist.glob("katex-*.js"))), None)


def _icon_button(symbol: str, label: str, handler: Any, *, classes: str = "btn") -> ui.element:
    element = ui.element("button").classes(classes).props('type="button"')
    with element:
        raw(icon(symbol, small=True) + esc(label), tag="span")
    element.on("click", lambda _e: handler())
    return element


class RunDetailPage:
    """State of one run detail tab."""

    def __init__(self, frame: Frame, detail: RunDetail, params: dict[str, str]) -> None:
        self.frame = frame
        self.api = frame.api
        self.detail = detail
        self.run_id = detail.run.id
        self.params = params
        self.agents: dict[str, AgentState] = {}
        self.cards: dict[str, AgentCard] = {}
        self.channels: dict[str, ChannelInfo] = {}
        self.channel_items: dict[str, ui.html] = {}
        self.channel_unread_shown: dict[str, int] = {}
        self.seen: dict[str, int] = {}
        self.seen_at: dict[str, float] = {}
        self.open_items: dict[str, ApprovalItem] = {}
        self.questions: dict[tuple[str, int], Message] = {}
        self.results = RunResults(items=[])
        self.view: View | None = None
        self.selected: tuple[str, str] = ("kanal", "")
        self.tab = "verlauf"
        self._last_verlauf: tuple[str, str] | None = None
        self.deciding: set[str] = set()
        self.compact_elements: dict[str, list[ui.element]] = {}
        self.rendered_items: tuple[str, ...] | None = None
        self.dirty: set[str] = set()
        self.subscription = None

    # --- shortcuts -------------------------------------------------------------------------

    @property
    def status(self) -> RunStatus:
        return self.detail.run.status

    @property
    def stage_labels(self) -> dict[Any, str]:
        return {s.key: s.label for s in self.detail.stages}

    # --- build -----------------------------------------------------------------------------

    async def build(self) -> None:
        api = self.api
        self.agents = {a.id: a for a in await api.list_agents(self.run_id)}
        self.channels = {c.name: c for c in await api.list_channels(self.run_id)}
        self.results = await api.get_results(self.run_id)
        await self._load_approvals()
        await self._scan_questions()
        self.frame.content.classes(replace="page rd-page")
        self.dialog = ConfirmDialog()
        self.root = (
            ui.element("div")
            .classes("rd")
            .props(f'data-rd="{esc(self.run_id)}"')
            .mark("run-detail")
        )
        self.root.on(
            "click",
            self._on_select,
            js_handler="(e) => { const a = e.target.closest('[data-sel]'); if (!a || e.button "
            "|| e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); "
            "emit(a.dataset.sel); }",
        )
        self.root.on("arrseen", self._on_seen, js_handler="(e) => emit(e.detail)")
        self.root.on("arrread", self._on_read, js_handler="(e) => emit(e.detail)")
        with self.root:
            self._build_head()
            with ui.element("div").classes("rd-body"):
                with (
                    ui.element("aside")
                    .classes("rd-col rd-left")
                    .props('aria-label="Kanäle und Agenten auswählen"')
                ):
                    self._build_selection()
                self.center = (
                    ui.element("section").classes("rd-col rd-center").props('aria-label="Ansicht"')
                )
                with self.center:
                    self.tabs = raw("", tag="nav", classes="tabs").props('aria-label="Ansichten"')
                    self.view_box = ui.element("div").classes("rd-view")
                with (
                    ui.element("aside")
                    .classes("rd-col rd-right")
                    .props('aria-label="Überblick"')
                    .mark("overview")
                ):
                    raw("Überblick", classes="side-h")
                    self.ov_wait = ui.element("div").mark("overview-waiting")
                    self.ov_rest = raw("", classes="ov-rest").mark("overview-rest")
        self._render_overview_cards()
        self._render_overview_rest()
        await self._select_initial()
        self.subscription = api.subscribe(self.run_id, agents=self._wanted_agents())
        self.frame.client.on_delete(self._close)
        ui.timer(PUMP_SECONDS, self._pump)

    def _close(self) -> None:
        if self.subscription is not None:
            self.subscription.close()

    # --- head -------------------------------------------------------------------------------

    def _build_head(self) -> None:
        with ui.element("div").classes("rd-head"):
            with ui.element("div").classes("rd-title"):
                self.badge = raw("", tag="span").mark("run-status")
                run = self.detail.run
                raw(f'<h1 id="run-title" tabindex="-1">{esc(run.name)}</h1>', tag="span")
                raw(copy_html(run.id, label="Run-ID kopieren"), tag="span", classes="rd-id")
                self.meta = raw("", tag="span", classes="small muted rd-meta-line")
                self.actions = ui.element("div").classes("actions").mark("actions")
            self.attn = raw("", classes="rd-attn-slot").mark("attention")
            with ui.element("div").classes("rd-row2"):
                self.stagebar = raw("", classes="stagebar-slot").mark("stages")
                self.meters = raw("", classes="rd-meta").mark("meters")
        self._render_head()

    def _render_head(self) -> None:
        run = self.detail.run
        status = run_status(run)
        self.badge.set_content(badge_html(status, large=True))
        starter = STARTER_PHRASE[run.starter]
        if run.status.is_final and run.ended_at:
            started = fmt.clock(run.started_at or run.created_at)
            when = f"gestartet {started} {starter} · beendet {fmt.clock(run.ended_at)}"
        else:
            since = fmt.when(run.started_at or run.created_at, self.api.now())
            when = f"seit {since.removeprefix('heute ')}, {starter}"
        self.meta.set_content(f"{esc(run.project)} · {esc(run.skill or 'frei')} · {esc(when)}")
        self._render_actions()
        self._render_attention()
        self.stagebar.set_content(stage_bar_html(self.detail.stages, run.topology))
        self._render_meters()

    def _render_meters(self) -> None:
        run = self.detail.run
        # the most turns of one agent, also when the service has not counted them yet
        most = max((a.turn for a in self.agents.values()), default=0)
        limits = run.limits
        if most > limits.max_turns_used:
            limits = limits.model_copy(update={"max_turns_used": most})
        self.meters.set_content(run_meters_html(limits, run.cost_kind))

    def _render_actions(self) -> None:
        allowed = set(self.detail.actions_allowed)
        run = self.detail.run
        self.actions.clear()
        with self.actions:
            if run.status.is_final:
                raw(
                    f'<button type="button" class="btn primary" data-copy="'
                    f'{esc(self.detail.handoff_text)}" aria-label="Text für Claude Code kopieren">'
                    "⧉ Für Claude Code kopieren</button>",
                    tag="span",
                ).mark("handoff")
            if RunAction.PAUSE in allowed:
                _icon_button("pause", "Pausieren", self._pause).props(
                    'title="Laufende Turns enden noch, danach startet kein neuer"'
                ).mark("pause")
            if RunAction.RESUME in allowed:
                _icon_button("play_arrow", "Fortsetzen", self._resume).mark("resume")
            top = self.detail.open_approvals_top
            if run.status is RunStatus.AWAITING_APPROVAL and top is not None:
                # before the start only the run-start card decides (§6.7), not „Stoppen“
                raw(
                    f'<a class="btn primary" href="/freigaben/{esc(top.id)}">Run-Start ansehen</a>',
                    tag="span",
                ).mark("run-start")
            elif RunAction.STOP in allowed:
                _icon_button("stop", "Stoppen …", self._ask_stop, classes="btn danger").mark("stop")
            if RunAction.PIN in allowed or RunAction.UNPIN in allowed:
                pinned = run.pinned
                pin = _icon_button(
                    "push_pin", "Angeheftet" if pinned else "Anheften", self._toggle_pin
                ).mark("pin")
                pin.props(f'aria-pressed="{"true" if pinned else "false"}"')
            self._menu()

    def _menu(self) -> None:
        run = self.detail.run
        with ui.element("details").classes("menu").mark("menu"):
            raw("⋯", tag="summary", classes="btn ghost").props(
                'title="Weitere Aktionen" aria-label="Weitere Aktionen"'
            )
            with ui.element("div").classes("menu-pop").props('role="menu"'):
                raw(
                    f'<button type="button" role="menuitem" data-copy="'
                    f'{esc(self.detail.handoff_text)}">⧉ Für Claude Code kopieren</button>'
                    f'<button type="button" role="menuitem" data-copy="{esc(run.id)}">Run-ID '
                    "kopieren</button>"
                    f'<button type="button" role="menuitem" data-copy="{esc(self.detail.run_dir)}">'
                    "Pfad des Run-Ordners kopieren</button>",
                    tag="span",
                ).mark("menu-copy")
                if not run.status.is_final:
                    label = "Nicht mehr anheften" if run.pinned else "Anheften"
                    item = ui.element("button").props('type="button" role="menuitem"')
                    with item:
                        raw(esc(label), tag="span")
                    item.on("click", lambda _e: self._toggle_pin())

    def _render_attention(self) -> None:
        """Banner „Wartet auf dich“ (at every width) and the hint „Fragen an dich“ (K13, K15)."""
        top = self.detail.open_approvals_top
        questions = self.detail.questions_to_user
        first_q = next((m for m in self.questions.values() if "user" in m.to), None)
        q_href = f"?kanal={first_q.channel}&nachricht={first_q.id}" if first_q else None
        hint = question_hint_html(questions, q_href)
        if first_q is not None and hint:
            hint = hint.replace("<a ", f'<a data-sel="msg:{esc(first_q.channel)}:{first_q.id}" ', 1)
        note = self._paused_note()
        if top is None:
            self.attn.set_content(
                f'<div class="rd-qrow">{note}<span class="spacer"></span>{hint}</div>'
                if hint or note
                else ""
            )
            return
        item = self.open_items.get(top.id)
        summary = esc(top.summary)
        for agent_id in self.agents:
            if top.summary.startswith(agent_id + " "):
                summary = f"<b>{esc(agent_id)}</b>{esc(top.summary[len(agent_id) :])}"
                break
        parts = [summary]
        if top.risk is not None:
            parts.append(f"Risiko {RISK[top.risk.level][1]}")
        if top.deadline is not None:
            parts.append(
                # the countdown ticks inside role=status: not announced every minute
                f'<span data-deadline="{top.deadline}" aria-live="off">'
                f"{esc(fmt.deadline(top.deadline, self.api.now()))}</span>"
            )
        if item is not None and item.type is ApprovalType.ESCALATION:
            parts.append("Run pausiert, weiter nur mit deiner Entscheidung")
        if item is not None and item.type is ApprovalType.RUN_START:
            parts = ["Dieser Run wartet auf deine Freigabe"]
        more = f" · und {top.more} weitere" if top.more else ""
        note_row = f'<div class="rd-qrow">{note}</div>' if note else ""
        self.attn.set_content(
            note_row + '<div class="rd-attn" role="status">'
            f"{badge_html(WAITING_FOR_YOU)}"
            f'<span class="txt">{" · ".join(parts)}{more}</span>{hint}<span class="spacer">'
            f'</span><a class="btn btn-sm primary" href="/freigaben/{esc(top.id)}">Ansehen</a>'
            "</div>"
        )

    def _paused_note(self) -> str:
        """„⏸ Pausiert um 15:30 · 2 laufende Turns enden noch“ (§6.7), neutral, not a banner."""
        run = self.detail.run
        if run.status is not RunStatus.PAUSED or run.open_escalation:
            return ""
        at = f" um {fmt.clock(run.status_since)}" if run.status_since else ""
        working = sum(1 for a in self.agents.values() if a.state is AgentDisplayState.WORKING)
        turns = ""
        if working:
            turns = f" · {working} laufende{'r' if working == 1 else ''} Turn"
            turns += f"{'s' if working != 1 else ''} {'endet' if working == 1 else 'enden'} noch"
        return (
            f'<span class="rd-note" role="status">{icon("pause", small=True)}Pausiert{at}{turns}'
            ". Danach startet kein neuer Turn, Nachrichten gehen beim Fortsetzen raus.</span>"
        )

    # --- selection column -------------------------------------------------------------------

    def _build_selection(self) -> None:
        raw(f"Kanäle ({len(self.channels)})", classes="side-h")
        with ui.element("nav").classes("sel-list").props('aria-label="Kanäle"'):
            for name in self.channels:
                item = ui.html("", sanitize=False, tag="a").mark(f"channel-{name}")
                item._props.update({"href": f"?kanal={name}", "data-sel": f"kanal:{name}"})
                self.channel_items[name] = item
                self._render_channel_item(name)
        raw(f"Agenten ({len(self.agents)})", classes="side-h")
        self.agents_box = ui.element("div").classes("agents").mark("agents")
        with self.agents_box:
            for agent in sorted(self.agents.values(), key=sort_key):
                self.cards[agent.id] = AgentCard(agent, f"?agent={agent.id}")

    def _render_channel_item(self, name: str) -> None:
        info = self.channels[name]
        item = self.channel_items[name]
        selected = self.selected == ("kanal", name) and self.tab == "verlauf"
        if not info.open:
            stage = next(
                (s.index for s in self.detail.stages if s.label == info.opens_at_stage), None
            )
            label = str(stage) if stage else (info.opens_at_stage or "später")
            right = (
                f'<span class="faint small right-note" title="öffnet in Stufe '
                f'„{esc(info.opens_at_stage or "")}“">ab Stufe {esc(label)}</span>'
            )
        else:
            n = self.channel_unread_shown.get(name) if selected else None
            if n is None:
                n = unread_count(info, self.seen.get(name))
            right = f'<span class="tag">{fmt.integer(n)} neu</span>' if n else ""
        item.classes(replace="sel-item sel" if selected else "sel-item")
        if selected:
            item._props["aria-current"] = "true"
        else:
            item._props.pop("aria-current", None)
        item.set_content(f'<span class="mono">#{esc(name)}</span>{right}')
        item.update()

    def _reorder_cards(self) -> None:
        order = sorted(self.agents.values(), key=sort_key)
        children = list(self.agents_box.default_slot.children)
        for index, agent in enumerate(order):
            card = self.cards.get(agent.id)
            if card is not None and children.index(card.element) != index:
                card.element.move(self.agents_box, target_index=index)
                children = list(self.agents_box.default_slot.children)

    # --- views ------------------------------------------------------------------------------

    async def _select_initial(self) -> None:
        p = self.params
        if p.get("agent") in self.agents:
            seq = int(p["seq"]) if p.get("seq", "").isdigit() else None
            await self.select(
                "seq" if seq is not None else "agent", p["agent"], seq=seq, push=False
            )
        elif p.get("ansicht") == "ergebnisse" or (
            self.status.is_final and not p.get("kanal") and not p.get("nachricht")
        ):
            ergebnis = int(p["ergebnis"]) if p.get("ergebnis", "").isdigit() else None
            self._last_verlauf = ("kanal", self._default_channel())
            await self.select(
                "ergebnis" if ergebnis else "ansicht", "ergebnisse", result_id=ergebnis, push=False
            )
        elif p.get("kanal") in self.channels:
            msg = int(p["nachricht"]) if p.get("nachricht", "").isdigit() else None
            await self.select("msg" if msg else "kanal", p["kanal"], msg_id=msg, push=False)
        else:
            await self.select("kanal", self._default_channel(), push=False)

    def _default_channel(self) -> str:
        """Live: the channel with the newest unread message, else the first open one (§6.5)."""
        best, best_id = "", -1.0
        for name, info in self.channels.items():
            if (
                info.open
                and unread_count(info, self.seen.get(name))
                and ((info.last_message_id or 0) > best_id)
            ):
                best, best_id = name, info.last_message_id or 0
        if best:
            return best
        return next((n for n, c in self.channels.items() if c.open), next(iter(self.channels), ""))

    async def select(
        self,
        kind: str,
        value: str,
        *,
        seq: int | None = None,
        msg_id: int | None = None,
        result_id: int | None = None,
        push: bool = True,
    ) -> None:
        """Show a channel, an agent's transcript or the results; keeps the URL in step."""
        previous = self.selected
        if kind == "ansicht" and value == "verlauf":
            kind, value = self._last_verlauf or ("kanal", self._default_channel())
        if kind in ("ansicht", "ergebnis"):
            self.tab = "ergebnisse"
            query = {"ansicht": "ergebnisse"}
            if result_id:
                query["ergebnis"] = str(result_id)
        else:
            self.tab = "verlauf"
            if kind in ("kanal", "msg"):
                if value not in self.channels:
                    return
                self.selected = ("kanal", value)
                query = {"kanal": value}
                if msg_id:
                    query["nachricht"] = str(msg_id)
            else:
                if value not in self.agents:
                    return
                self.selected = ("agent", value)
                query = {"agent": value}
                if seq is not None:
                    query["seq"] = str(seq)
            self._last_verlauf = self.selected
        # channel badges: the open channel keeps its „n neu“ until another one is chosen
        if self.selected[0] == "kanal" and self.tab == "verlauf":
            name = self.selected[1]
            self.channel_unread_shown = {
                name: unread_count(self.channels[name], self.seen.get(name))
            }
        else:
            self.channel_unread_shown = {}
        for name in self.channels:
            self._render_channel_item(name)
        for agent_id, card in self.cards.items():
            card.select(self.tab == "verlauf" and self.selected == ("agent", agent_id))
        self._render_tabs()
        self.view_box.clear()
        with self.view_box:
            if self.tab == "ergebnisse":
                self.view = ResultsView(self, result_id=result_id)
            elif self.selected[0] == "kanal":
                self.view = ChannelView(self, self.channels[self.selected[1]], jump_to=msg_id)
            else:
                self.view = TranscriptView(self, self.agents[self.selected[1]], seq=seq)
            await self.view.build()
        if previous != self.selected and self.subscription is not None:
            self._resubscribe()
        if push:
            self.frame.js(f"arr.rd.pushUrl({_js(urlencode(query))})")

    def _render_tabs(self) -> None:
        n = len(self.results.items)
        count = f' <span class="n">{n}</span>' if n else ""
        verlauf, ergebnisse = ("active", "") if self.tab == "verlauf" else ("", "active")
        cur_v = ' aria-current="page"' if verlauf else ""
        cur_e = ' aria-current="page"' if ergebnisse else ""
        self.tabs.set_content(
            f'<a class="{verlauf}" href="?ansicht=verlauf" data-sel="ansicht:verlauf"{cur_v}>'
            "Verlauf</a>"
            f'<a class="{ergebnisse}" href="?ansicht=ergebnisse" data-sel="ansicht:ergebnisse"'
            f"{cur_e}>Ergebnisse{count}</a>"
        )

    async def _on_select(self, event: Any) -> None:
        target = str(event.args or "")
        kind, _, rest = target.partition(":")
        if kind == "msg":
            channel, _, msg = rest.rpartition(":")
            await self.select("msg", channel, msg_id=int(msg) if msg.isdigit() else None)
        elif kind == "seq":
            agent, _, seq = rest.rpartition(":")
            await self.select("seq", agent, seq=int(seq) if seq.isdigit() else None)
        elif kind == "ergebnis":
            await self.select(
                "ergebnis", "ergebnisse", result_id=int(rest) if rest.isdigit() else None
            )
        elif kind in ("kanal", "agent", "ansicht"):
            await self.select(kind, rest)
        # tab links are drawn anew: the focus goes where the new view starts (W1-07.1)
        if kind in ("ansicht", "ergebnis", "seq", "msg"):
            self.frame.js(f"arr.rd.focusView({_js(kind)}, {_js(rest)})")

    # --- ui_seen („gelesen bis“, browser) ----------------------------------------------------

    def _on_read(self, event: Any) -> None:
        """The browser saw messages up to an ID (it stores that itself, W1-07.1)."""
        args = event.args if isinstance(event.args, dict) else {}
        channel, last = args.get("channel"), args.get("id")
        if channel in self.channels and isinstance(last, int):
            self.seen[channel] = max(self.seen.get(channel, 0), last)

    async def _on_seen(self, event: Any) -> None:
        stored = event.args if isinstance(event.args, dict) else {}
        channels = stored.get("channels") or {}
        changed = False
        for name, last in channels.items():
            if name in self.channels and isinstance(last, int):
                changed |= self.seen.get(name) != last
                self.seen[name] = last
        self.seen_at = {
            k: float(v) for k, v in (stored.get("at") or {}).items() if isinstance(v, int | float)
        }
        if changed and isinstance(self.view, ChannelView) and not self.params.get("nachricht"):
            await self.select("kanal", self.view.name, push=False)
        else:
            for name in self.channels:
                self._render_channel_item(name)

    # --- overview ---------------------------------------------------------------------------

    async def _load_approvals(self) -> None:
        items = await self.api.list_approvals(status=[ApprovalStatus.OPEN], run_id=self.run_id)
        self.open_items = {i.id: i for i in items}

    async def _scan_questions(self) -> None:
        """Open questions of the newest messages of each channel (overview, K15)."""
        self.questions = {}
        for name, info in self.channels.items():
            if not info.message_count:
                continue
            page = await self.api.get_messages(self.run_id, name, limit=SCAN_MESSAGES)
            for msg in page.items:
                if msg.open and msg.kind is MessageKind.QUESTION:
                    self.questions[(name, msg.id)] = msg

    def compact_card(self, item: ApprovalItem, *, prefix: str) -> ui.element:
        """A compact approval card (K5) that decides through this page."""

        async def decide(it: ApprovalItem, request: DecisionRequest) -> None:
            await self._decide(it, request, prefix)

        element = compact_card(item, now=self.api.now(), on_decide=decide, prefix=prefix)
        element.mark(f"{prefix}card-{item.id}")
        self.compact_elements.setdefault(item.id, []).append(element)
        return element

    def _render_overview_cards(self) -> None:
        """Rebuild the compact cards only when the open items changed: a rebuilt card is a new
        element and would have to be seen for 1.5 s again (G2); a confirmation stays."""
        ids = tuple(sorted(self.open_items))
        if self.deciding or ids == self.rendered_items:
            return
        self.rendered_items = ids
        self.ov_wait.clear()
        self.compact_elements = {
            k: [e for e in v if not e.is_deleted] for k, v in self.compact_elements.items()
        }
        items = sorted(
            self.open_items.values(),
            key=lambda i: (i.deadline or float("inf"), i.created_at),
        )
        if self.status.is_final and not items:
            return
        with self.ov_wait:
            raw(f"Wartet auf dich · {len(items)}", classes="side-h")
            if not items:
                raw('<p class="small muted">Nichts wartet auf dich in diesem Run.</p>')
                return
            for item in items:
                self.compact_card(item, prefix="cc-")
            raw(
                '<p class="small muted">Andere Reichweiten unter „Details“. Run-Starts und '
                "Eskalationen entscheidest du nur auf der Freigaben-Seite.</p>"
            )

    def _render_overview_rest(self) -> None:
        to_user = [m for m in self.questions.values() if "user" in m.to]
        between = [m for m in self.questions.values() if "user" not in m.to]
        open_count = sum(c.open_questions for c in self.channels.values())
        open_count = max(open_count - len(to_user), len(between))
        questions = ""
        if not self.status.is_final or open_count:
            questions = open_questions_html(between, open_count)
        self.ov_rest.set_content(
            questions_to_user_html(to_user, self.detail.questions_to_user)
            + questions
            + results_short_html(self.results.items, self.results.empty_text)
            + agents_html(list(self.agents.values()))
        )

    async def _decide(self, item: ApprovalItem, request: DecisionRequest, prefix: str) -> None:
        if item.id in self.deciding:
            return
        self.deciding.add(item.id)
        try:
            response = await self.api.decide_approval(item.id, request)
        except ApiException as exc:
            self.deciding.discard(item.id)
            self.frame.toast(exc.error.message, error=True)
            await self._refresh_approvals()
            return
        positive = request.action is not DecisionAction.DENY
        for element in self.compact_elements.get(item.id, []):
            if not element.is_deleted:
                _show_decided(element, response.message, positive=positive)
        self.frame.js(f"arr.showDecided({_js(prefix + item.id)})")
        self.frame.toast(response.message)

        async def after() -> None:
            self.deciding.discard(item.id)
            view = self.view
            if isinstance(view, TranscriptView):
                view.flush_pending()
            await self._refresh_approvals()
            ids = sorted(self.open_items)
            following = f"#cc-{ids[0]}-ch" if ids else ""
            if prefix == "tc-" and isinstance(view, TranscriptView):
                following = view.row_selector(item.id)  # back to the tool call row
            self.frame.js(f"arr.focusAfterRemoval({_js(following)}, {_js(prefix + item.id)})")

        with self.root:
            ui.timer(DECIDED_SECONDS, after, once=True)

    async def _refresh_approvals(self) -> None:
        if self.deciding:
            self.dirty.add("approvals")
            return
        await self._load_approvals()
        self.detail = await self.api.get_run(self.run_id)
        self._render_attention()
        self._render_overview_cards()
        if isinstance(self.view, TranscriptView):
            self.view.refresh_waiting()

    # --- actions ------------------------------------------------------------------------------

    async def _run_action(self, action: RunAction) -> bool:
        try:
            result = await self.api.run_action(self.run_id, action)
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return False
        self.frame.toast(result.message)
        await self._refresh_run()
        return True

    async def _pause(self) -> None:
        await self._run_action(RunAction.PAUSE)

    async def _resume(self) -> None:
        await self._run_action(RunAction.RESUME)

    async def _toggle_pin(self) -> None:
        await self._run_action(RunAction.UNPIN if self.detail.run.pinned else RunAction.PIN)

    def _ask_stop(self) -> None:
        run = self.detail.run
        working = [a.id for a in self.agents.values() if a.state is AgentDisplayState.WORKING]
        consequences = []
        if working:
            n = len(working)
            consequences.append(
                f"{n} laufende{'r' if n == 1 else ''} Turn{'s' if n != 1 else ''} "
                f"{'wird' if n == 1 else 'werden'} abgebrochen ({fmt.join_de(working)})."
            )
        else:
            consequences.append("Gerade arbeitet kein Agent, es wird nichts abgebrochen.")
        items = list(self.open_items.values())
        if len(items) == 1:
            consequences.append(f"Die offene Freigabe entfällt: {items[0].title}.")
        elif items:
            consequences.append(f"Die {len(items)} offenen Freigaben dieses Runs entfallen.")
        consequences.append(
            "Danach läuft die Zusammenfassung. Transkripte, Gruppenchats und Teilergebnisse "
            "bleiben vollständig erhalten."
        )
        can_pause = RunAction.PAUSE in self.detail.actions_allowed
        self.dialog.show(
            f"Run „{run.name}“ stoppen?",
            consequences,
            "Run stoppen",
            lambda _v: self._run_action(RunAction.STOP),
            note="Nur anhalten statt beenden? Nimm „Pausieren“: Laufende Turns enden noch, danach "
            "startet kein neuer."
            if can_pause
            else None,
            alternative=("Stattdessen pausieren", self._pause) if can_pause else None,
        )

    async def _refresh_run(self) -> None:
        old = self.status
        self.detail = await self.api.get_run(self.run_id)
        await self._load_approvals()
        self._render_head()
        self._render_overview_cards()
        if old is not self.status and isinstance(self.view, ChannelView):
            self.view.refresh_status()

    # --- live ---------------------------------------------------------------------------------

    def _wanted_agents(self) -> set[str]:
        return {self.selected[1]} if self.selected[0] == "agent" else set()

    def _resubscribe(self) -> None:
        if self.subscription is not None:
            self.subscription.close()
        self.subscription = self.api.subscribe(self.run_id, agents=self._wanted_agents())

    async def _pump(self) -> None:
        if self.subscription is None:
            return
        events = self.subscription.drain()
        if not events:
            return
        for event in events:
            await self._apply(event)
        await self._flush()

    async def _apply(self, event: LiveEvent) -> None:
        if isinstance(event, UsageTickEvent):
            limits = self.detail.run.limits.model_copy(
                update={"elapsed_s": event.elapsed_s, "spent_usd": event.spent_usd}
            )
            self.detail.run = self.detail.run.model_copy(update={"limits": limits})
            self.dirty.add("meters")
        elif isinstance(event, RunStatusEvent):
            self.dirty.add("run")
        elif isinstance(event, StageCheckpointEvent):
            self.detail.stages = [
                event.stage if s.key == event.stage.key else s for s in self.detail.stages
            ]
            self.dirty.add("stages")
        elif isinstance(event, AgentStateEvent):
            self._update_agent(event.agent)
        elif isinstance(event, AgentActivityEvent):
            agent = self.agents.get(event.agent_id)
            if agent is not None:
                self._update_agent(agent.model_copy(update={"activity": event.activity}))
        elif isinstance(event, AgentTranscriptEvent):
            view = self.view
            if isinstance(view, TranscriptView) and view.agent_id == event.agent_id:
                view.on_event(event.event)
        elif isinstance(event, ChannelMessageEvent):
            self._on_message(event.message)
        elif isinstance(event, ResultSubmittedEvent | RatingSubmittedEvent):
            self.dirty.add("results")
        elif isinstance(event, ApprovalEvent):
            self.dirty.add("approvals")

    def _update_agent(self, agent: AgentState) -> None:
        old = self.agents.get(agent.id)
        self.agents[agent.id] = agent
        card = self.cards.get(agent.id)
        if card is not None:
            card.update(agent)
        if old is None or old.state is not agent.state:
            self.dirty.update({"order", "overview", "attention"})  # „n laufende Turns …“
        view = self.view
        if isinstance(view, TranscriptView) and view.agent_id == agent.id:
            view.update_agent(agent)

    def _on_message(self, msg: Message) -> None:
        info = self.channels.get(msg.channel)
        if info is None:
            return
        update = {"message_count": info.message_count + 1, "last_message_id": msg.id}
        if msg.open and msg.kind is MessageKind.QUESTION:
            update["open_questions"] = info.open_questions + (
                0 if (msg.channel, msg.id) in self.questions else 1
            )
            self.questions[(msg.channel, msg.id)] = msg
            self.dirty.add("overview")
            if "user" in msg.to:
                self.detail.questions_to_user += 1
                self.dirty.add("attention")
        self.channels[msg.channel] = info.model_copy(update=update)
        view = self.view
        if isinstance(view, ChannelView) and view.name == msg.channel:
            view.info = self.channels[msg.channel]
            view.add_live(msg)
        self._render_channel_item(msg.channel)

    async def _flush(self) -> None:
        dirty, self.dirty = self.dirty, set()
        if "run" in dirty:
            await self._refresh_run()
            dirty -= {"meters", "stages", "attention"}
        if "approvals" in dirty:
            await self._refresh_approvals()
            dirty.add("overview")
            dirty -= {"attention"}
        if "results" in dirty:
            self.results = await self.api.get_results(self.run_id)
            self._render_tabs()
            if isinstance(self.view, ResultsView):
                await self.view.refresh()
            dirty.add("overview")
        if "meters" in dirty:
            self._render_meters()
        if "stages" in dirty:
            self.stagebar.set_content(stage_bar_html(self.detail.stages, self.detail.run.topology))
        if "attention" in dirty:
            self._render_attention()
        if "order" in dirty:
            self._reorder_cards()
        if "overview" in dirty:
            self._render_overview_rest()
            if isinstance(self.view, ChannelView):
                self.view.refresh_messages()


def _show_decided(element: ui.element, message: str, *, positive: bool) -> None:
    """A decided compact card: same height, content hidden, confirmation on top (G2)."""
    element.props(remove="data-primary-scope")
    element.classes(add="decided")
    symbol = "✓" if positive else "✕"
    with element:
        raw(
            f'<span aria-hidden="true">{symbol}</span> {esc(message)}',
            classes="appr-done" if positive else "appr-done neutral",
        ).props('role="status"')
    for child in element.default_slot.children:
        if isinstance(child, ui.element) and child.tag == "button":
            set_disabled(child, True)


def _js(value: str) -> str:
    """A Python string as a JavaScript string literal."""
    return json.dumps(value)


def register() -> None:
    """Register `/runs/{run_id}` and the KaTeX file for typeset formulas (G7)."""
    katex = katex_file()
    if katex is not None:
        app.add_static_file(local_file=katex, url_path=KATEX_URL)

    @ui.page("/runs/{run_id}")
    async def run_detail(
        run_id: str,
        kanal: str = "",
        agent: str = "",
        seq: str = "",
        nachricht: str = "",
        ansicht: str = "",
        ergebnis: str = "",
        zustand: str = "",
    ) -> None:
        api = get_api()
        if zustand in ("laedt", "fehler"):
            frame = Frame(
                api, active="runs", title="Run", crumbs=[("Runs", "/runs"), ("Run", None)]
            )
            await frame.start()
            workfolder = await api.get_workfolder()
            folder = f"{workfolder.path}/runs/{run_id}"
            forced_state(frame, zustand, "run", title="Run", path=f"/runs/{run_id}", folder=folder)
            return
        try:
            detail = await api.get_run(run_id)
        except ApiException as exc:
            frame = Frame(
                api,
                active="runs",
                title="Run nicht gefunden",
                crumbs=[("Runs", "/runs"), ("Run", None)],
                focus="#run-title",
            )
            await frame.start()
            with frame.content:
                raw('<h1 id="run-title" tabindex="-1">Run nicht gefunden</h1>')
                banner(
                    "err",
                    exc.error.message,
                    " Vielleicht wurde er gelöscht.",
                    [("Zu den Runs", "/runs")],
                )
            return
        version = int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir()))
        ui.add_head_html(
            f'<link rel="stylesheet" href="{STATIC_URL}/run_detail.css?v={version}">'
            f'<script src="{STATIC_URL}/run_detail.js?v={version}" defer></script>'
            f'<script src="{STATIC_URL}/math.js?v={version}" data-katex="{KATEX_URL}" defer>'
            "</script>"
            # docs/08 §4.3 writes a message link as „?kanal=main#m7“: same as „&nachricht=7“
            "<script>(function(){var m=location.hash.match(/^#m(\\d+)$/),"
            "p=new URLSearchParams(location.search);if(m&&p.get('kanal')&&!p.get('nachricht'))"
            "{p.set('nachricht',m[1]);location.replace(location.pathname+'?'+p.toString());}})();"
            "</script>"
        )
        frame = Frame(
            api,
            active="runs",
            title=detail.run.name,
            crumbs=[("Runs", "/runs"), (detail.run.name, None)],
            focus=_deep_link_focus(agent, seq, kanal, nachricht, ergebnis),
        )
        await frame.start()
        params = {
            "kanal": kanal,
            "agent": agent,
            "seq": seq,
            "nachricht": nachricht,
            "ansicht": ansicht,
            "ergebnis": ergebnis,
        }
        with frame.content:
            page = RunDetailPage(frame, detail, params)
            await page.build()
        if agent and not seq:
            # the agent card is a link: focus it without taking it out of the Tab order
            card = f'.rd-left .agent[data-sel="agent:{agent}"]'
            frame.js(
                f"arr.whenEl({card!r}, (el) => setTimeout(() => "
                "el.focus({preventScroll: true}), 250))"
            )


def _deep_link_focus(agent: str, seq: str, kanal: str, nachricht: str, ergebnis: str) -> str:
    """Focus target of a deep link (docs/08 §4.3): the marked line, the message, the result
    title, else the run title; never a button. The agent card is focused after the build."""
    if agent and seq.isdigit():
        return f"#{event_dom_id(int(seq))}"
    if kanal and nachricht.isdigit():
        return f"#{message_dom_id(kanal, int(nachricht))}"
    if ergebnis.isdigit():
        return f"#{result_dom_id(int(ergebnis))}-h"
    return "#run-title"

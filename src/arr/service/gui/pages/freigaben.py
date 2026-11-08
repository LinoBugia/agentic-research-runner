"""Freigaben: decide everything that waits for you, then see who decided what (docs/08 §5.2).

`/freigaben` shows the open cards completely (no master/detail), sorted deadline first, then
oldest, and today's decisions below; `/freigaben?ansicht=verlauf` the whole history.
`/freigaben/<id>` is the deep link: the card is scrolled into view and its heading focused
(arr.js), never a button (G2). Live: new cards appear on top and disarm all cards, so no
card slides under the pointer unseen (W1-06.2); cards decided elsewhere or expired move to the
history with a toast.
"""

from datetime import datetime

from nicegui import ui

from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    DecisionAction,
    DecisionScope,
)
from arr.service.api_models import (
    ApiException,
    ApprovalEvent,
    ApprovalItem,
    DecisionRequest,
    LiveEvent,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.approval_card import ApprovalCard, heading_id
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import banner, empty_state
from arr.service.gui.components.base import button, esc, raw
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.page_state import error_html, forced_state, state_api
from arr.service.gui.layout import Frame
from arr.service.gui.texts import (
    APPROVAL_FILTERS,
    APPROVAL_TYPE,
    DECIDED_BY,
    DECIDED_BY_PHRASE,
    outcome,
    scope_label,
)

DECIDED_SECONDS = 1.5
"""How long a decided card shows its confirmation before the next card moves up."""

HISTORY_STATUSES = [
    ApprovalStatus.DECIDED,
    ApprovalStatus.EXPIRED,
    ApprovalStatus.OBSOLETE,
    ApprovalStatus.REVOKED,
]


def _request_text(item: ApprovalItem) -> str:
    if item.type is ApprovalType.PERMISSION and item.agent:
        what = item.command or item.path or item.url or ""
        agent = esc(item.agent.id)
        return f'<span class="mono">{agent}</span> · <span class="mono">{esc(what)}</span>'
    if item.type is ApprovalType.QUESTION and item.agent:
        text = " ".join((item.question_text or "").split())
        if len(text) > QUESTION_EXCERPT:
            text = text[: QUESTION_EXCERPT - 1] + "…"
        return f'<span class="mono">{esc(item.agent.id)}</span> · „{esc(text)}“'
    return esc(item.title)


QUESTION_EXCERPT = 120
"""Characters of a question to the user in a row of the history."""


class FreigabenPage:
    """State of one Freigaben page (one browser tab)."""

    def __init__(self, frame: Frame, *, art: str, history: bool, deep_link: str | None) -> None:
        self.frame = frame
        self.api = frame.api
        # A deep link always shows its card, whatever the type filter says.
        self.art = art if art in APPROVAL_FILTERS and not deep_link else "alle"
        self.type_filter = APPROVAL_FILTERS[self.art][1]
        self.history = history
        self.deep_link = deep_link
        self.cards: dict[str, ApprovalCard] = {}
        self.own_decisions: set[str] = set()
        self.workfolder = "Mathe privat"

    async def build(self) -> None:
        api = self.api
        self.workfolder = (await api.get_workfolder()).name
        try:
            open_items = await api.list_approvals(
                status=[ApprovalStatus.OPEN], type=self.type_filter
            )
        except ApiException as exc:  # no inbox yet (W3-03): the message instead of the page
            with self.frame.content:
                raw('<div class="page-head"><h1 tabindex="-1">Freigaben</h1></div>', tag="div")
                raw(error_html(exc, retry_href="/freigaben"))
            return
        with self.frame.content:
            self.dialog = ConfirmDialog()
            with ui.element("div").classes("page-head"):
                raw('<h1 tabindex="-1">Freigaben</h1>', tag="div")
                self.tabs = raw("", tag="nav", classes="tabs").props('aria-label="Ansicht"')
                with ui.element("div").classes("actions"):
                    raw(self._filter_html(), tag="div")
            self.notice = ui.element("div").style("margin-bottom:12px")
            if self.deep_link:
                await self._deep_link_notice(open_items)
            if not self.history:
                self.cards_box = ui.element("div").mark("cards")
                with self.cards_box:
                    for item in open_items:
                        self._add_card(item, highlight=item.id == self.deep_link)
                self.empty_box = ui.element("div")
                self._render_empty()
                with (
                    ui.element("section")
                    .classes("section")
                    .style("margin-top:32px")
                    .props('aria-labelledby="h-verlauf"')
                ):
                    raw(
                        '<div class="section-title"><h2 id="h-verlauf" tabindex="-1">Letzte '
                        'Entscheidungen</h2><span class="faint">heute</span>'
                        f'<a class="more" href="{self._href(history=True)}">Ganzer Verlauf →</a>'
                        "</div>"
                    )
                    self.history_box = ui.element("div").mark("history")
            else:
                with ui.element("section").classes("section").props('aria-labelledby="h-verlauf"'):
                    raw(
                        '<div class="section-title"><h2 id="h-verlauf" tabindex="-1">Verlauf</h2>'
                        '<span class="faint">alle Entscheidungen, neueste zuerst</span></div>'
                    )
                    self.history_box = ui.element("div").mark("history")
            raw(
                '<p class="small muted" style="margin-top:8px">Zurückziehen geht bei '
                "Entscheidungen des Aufsichts-Agenten und bei deinen eigenen mit Reichweite "
                "„Agent“ oder „Run“. Entscheidungen der Vollmacht-Prüfung änderst du über die "
                "Vollmacht, „immer“-Regeln unter Einstellungen → Feste Regeln.</p>"
            )
        self._render_tabs(len(open_items))
        await self._render_history()
        self.frame.on_event(self._on_event)

    # --- head: tabs and filter -------------------------------------------------------------

    def _href(self, *, history: bool | None = None, art: str | None = None) -> str:
        params = []
        if history if history is not None else self.history:
            params.append("ansicht=verlauf")
        art = art if art is not None else self.art
        if art != "alle":
            params.append(f"art={art}")
        return "/freigaben" + ("?" + "&".join(params) if params else "")

    def _render_tabs(self, open_count: int) -> None:
        n = f' <span class="n">{open_count}</span>' if open_count else ""
        offen = "active" if not self.history else ""
        verlauf = "active" if self.history else ""
        aria_o = ' aria-current="page"' if not self.history else ""
        aria_v = ' aria-current="page"' if self.history else ""
        self.tabs.set_content(
            f'<a class="{offen}" href="{self._href(history=False)}"{aria_o}>Offen{n}</a>'
            f'<a class="{verlauf}" href="{self._href(history=True)}"{aria_v}>Verlauf</a>'
        )

    def _filter_html(self) -> str:
        links = []
        for key, (label, _type) in APPROVAL_FILTERS.items():
            on = key == self.art
            links.append(
                f'<a class="{"on" if on else ""}" href="{self._href(art=key)}"'
                f"{' aria-current=' + chr(34) + 'true' + chr(34) if on else ''}>{esc(label)}</a>"
            )
        return f'<div class="seg" role="group" aria-label="Art filtern">{"".join(links)}</div>'

    # --- cards ------------------------------------------------------------------------------

    def _add_card(self, item: ApprovalItem, *, highlight: bool = False, top: bool = False) -> None:
        with self.cards_box:
            card = ApprovalCard(
                item,
                now=self.api.now(),
                on_decide=self._decide,
                dialog=self.dialog,
                workfolder=self.workfolder,
                highlight=highlight,
            )
        if top:
            card.element.move(self.cards_box, target_index=0)
            self.cards = {item.id: card, **self.cards}
            self.frame.js("arr.disarmAll()")
        else:
            self.cards[item.id] = card

    def _render_empty(self) -> None:
        self.empty_box.clear()
        if self.cards:
            return
        with self.empty_box:
            if self.type_filter is not None:
                label = APPROVAL_FILTERS[self.art][0]
                empty_state(
                    "front_hand",
                    f"Keine offenen {label}",
                    "Bei den anderen Arten kann etwas warten.",
                    [("Filter zurücksetzen", self._href(art="alle"), False)],
                )
            else:
                empty_state(
                    "front_hand",
                    "Nichts wartet auf dich",
                    "Neue Freigaben erscheinen hier und als Mitteilung auf deinem Mac.",
                    [("Verlauf ansehen", self._href(history=True), False)],
                )

    def _remove_card(self, approval_id: str) -> None:
        if approval_id not in self.cards:
            return
        order = list(self.cards)
        position = order.index(approval_id)
        card = self.cards.pop(approval_id)
        card.element.delete()
        rest = [i for i in order if i != approval_id]
        following = rest[position] if position < len(rest) else (rest[-1] if rest else None)
        next_heading = f"#{heading_id(following)}" if following else ""
        self.frame.js(f"arr.focusAfterRemoval('{next_heading}', '{approval_id}')")
        self._render_tabs(len(self.cards))
        self._render_empty()

    async def _decide(self, card: ApprovalCard, request: DecisionRequest) -> None:
        item = card.item
        self.own_decisions.add(item.id)
        try:
            response = await self.api.decide_approval(item.id, request)
        except ApiException as exc:
            self.own_decisions.discard(item.id)
            self.frame.toast(exc.error.message, error=True)
            if exc.error.code.value == "conflict":
                self._remove_card(item.id)
                await self._render_history()
            return
        link = ("Zum Run", f"/runs/{item.run.id}")
        if item.type is ApprovalType.PERMISSION and item.agent:
            link = ("Zum Agenten", f"/runs/{item.run.id}?agent={item.agent.id}")
        elif item.type is ApprovalType.QUESTION and item.channel:
            link = ("Zum Kanal", f"/runs/{item.run.id}?kanal={item.channel}")
        # The card stays in place as a confirmation of the same height for DECIDED_SECONDS, so
        # nothing slides under the pointer or the focus (W1-06.2 N1); then the next one moves up.
        positive = request.action is not DecisionAction.DENY
        note = "im Verlauf zurückziehbar" if response.item.revocable else "steht jetzt im Verlauf"
        card.show_decided(response.message, note, positive=positive)
        self.frame.js(f"arr.showDecided('{item.id}')")
        self._render_tabs(len(self.cards) - 1)
        with self.cards_box:
            ui.timer(DECIDED_SECONDS, lambda: self._remove_card(item.id), once=True)
        self.frame.toast(response.message, link)
        await self._render_history()

    async def _deep_link_notice(self, open_items: list[ApprovalItem]) -> None:
        assert self.deep_link is not None
        if any(i.id == self.deep_link for i in open_items):
            return
        try:
            item = await self.api.get_approval(self.deep_link)
        except ApiException:
            with self.notice:
                banner(
                    "err",
                    f"Freigabe {self.deep_link} nicht gefunden.",
                    " Vielleicht gehört sie zu einem anderen Workfolder. Die offenen Freigaben "
                    "stehen unten.",
                )
            return
        if item.status is ApprovalStatus.OPEN:
            return
        status = outcome(item)
        who = DECIDED_BY_PHRASE[item.decision.decided_by] if item.decision else ""
        when = fmt.clock(item.decision.decided_at) if item.decision else ""
        text = item.status_text or (f"{status.word} {who} um {when}" if who else status.word)
        with self.notice:
            banner(
                "info",
                f"„{item.title}“ ist nicht mehr offen.",
                f" {text[0].upper() + text[1:]}. Du findest die Entscheidung im Verlauf.",
            )

    # --- history ----------------------------------------------------------------------------

    async def _render_history(self) -> None:
        now = self.api.now()
        items = await self.api.list_approvals(status=HISTORY_STATUSES, type=self.type_filter)
        if not self.history:
            start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0).timestamp()
            items = [
                i for i in items if (i.decision.decided_at if i.decision else i.created_at) >= start
            ]
        self.history_box.clear()
        with self.history_box:
            if not items:
                raw('<p class="muted">Heute noch keine Entscheidungen.</p>')
                return
            with ui.element("table").classes("tbl"):
                raw(
                    "<tr><th>Zeit</th><th>Art</th><th>Anfrage</th><th>Entscheidung</th>"
                    "<th>Entschieden von</th><th>Reichweite</th>"
                    '<th><span class="sr-only">Aktion</span></th></tr>',
                    tag="thead",
                )
                with ui.element("tbody"):
                    for item in items:
                        self._history_row(item, now)

    def _history_row(self, item: ApprovalItem, now: float) -> None:
        decision = item.decision
        at = decision.decided_at if decision else (item.deadline or item.created_at)
        who = "–"
        if decision:
            who = esc(DECIDED_BY[decision.decided_by])
            if decision.rationale:
                who += f'<span class="note">„{esc(decision.rationale)}“</span>'
            if decision.comment:
                word = "Antwort" if decision.action is DecisionAction.ANSWER else "Kommentar"
                who += f'<span class="note">{word}: „{esc(decision.comment)}“</span>'
        status = outcome(item)
        extra = (
            f'<span class="small muted" style="display:block">{esc(item.status_text)}</span>'
            if (item.status_text and item.status is not ApprovalStatus.DECIDED)
            else ""
        )
        scope = "–"
        if (
            decision
            and item.type is ApprovalType.PERMISSION
            and decision.decided_by is not DecidedBy.FIXED_RULE
        ):
            scope = esc(scope_label(decision.scope, item.agent.id if item.agent else None))
        cells = [
            ("num nw", esc(fmt.when(at, now))),
            ("", esc(APPROVAL_TYPE[item.type])),
            ("", f'{_request_text(item)}<span class="sub">{esc(item.run.name)}</span>'),
            ("", badge_html(status) + extra),
            ("", who),
            ("", scope),
        ]
        with ui.element("tr").props(f'id="h-{item.id}"').mark(f"history-{item.id}"):
            for css, inner in cells:
                raw(inner, tag="td", classes=css)
            with ui.element("td"):
                if item.revocable:
                    button(
                        "Zurückziehen", lambda i=item: self._ask_revoke(i), classes="btn btn-sm"
                    ).mark(f"revoke-{item.id}")
                elif decision and decision.decided_by is DecidedBy.MANDATE_RULE:
                    raw(
                        '<a class="small" href="/einstellungen#vollmacht">Vollmacht ändern</a>',
                        tag="span",
                    )
                elif decision and decision.scope is DecisionScope.ALWAYS:
                    raw(
                        '<a class="small" href="/einstellungen#regeln">Feste Regeln</a>', tag="span"
                    )

    def _ask_revoke(self, item: ApprovalItem) -> None:
        decision = item.decision
        assert decision is not None
        who = DECIDED_BY[decision.decided_by]
        what = item.command or item.path or item.title
        agent = item.agent.id if item.agent else "den Agenten"
        once = decision.scope is DecisionScope.ONCE
        first = (
            "Die Erlaubnis war einmalig, der Befehl ist schon gelaufen. Zurückziehen wirkt auf "
            f"künftige gleiche Anfragen: Der {who} entscheidet sie in diesem Run nicht mehr, sie "
            "kommen zu dir."
            if once
            else "Die Erlaubnis gilt ab jetzt nicht mehr. Künftige gleiche Anfragen kommen zu dir."
        )
        self.dialog.show(
            "Entscheidung zurückziehen?",
            [
                first,
                "Bereits Ausgeführtes bleibt.",
                "Das Zurückziehen steht mit Zeitpunkt in approvals.jsonl.",
            ],
            "Zurückziehen",
            lambda _v: self._revoke(item),
            intro=f"{outcome(item).word.capitalize()} war: {what} für {agent} ({who}, "
            f"{scope_label(decision.scope, agent)}, {fmt.clock(decision.decided_at)}).",
        )

    async def _revoke(self, item: ApprovalItem) -> None:
        try:
            await self.api.revoke_approval(item.id)
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        self.frame.toast("Entscheidung zurückgezogen. Gleiche Anfragen kommen ab jetzt zu dir.")
        await self._render_history()
        # the history was re-rendered: give the update a moment, then focus its heading
        self.frame.js("setTimeout(() => arr.focusAfterRemoval('#h-verlauf'), 150)")

    # --- live -------------------------------------------------------------------------------

    async def _on_event(self, event: LiveEvent) -> None:
        if not isinstance(event, ApprovalEvent):
            return
        item = event.item
        if self.type_filter is not None and item.type is not self.type_filter:
            return
        if event.type == "approval.created" and not self.history:
            if item.id not in self.cards:
                self._add_card(item, top=True)
                self._render_tabs(len(self.cards))
                self._render_empty()
                self.frame.toast(
                    f"Neue Freigabe: {item.title}", ("Ansehen", f"/freigaben/{item.id}")
                )
            return
        if event.type == "approval.updated":
            if item.id in self.cards:
                self.cards[item.id].update(item, self.api.now())
                self.frame.js("arr.disarmAll()")  # the card now answers more than was seen
            return
        if item.id in self.own_decisions and event.type == "approval.decided":
            return
        if item.id in self.cards:
            self._remove_card(item.id)
            if event.type == "approval.decided":
                self.frame.toast(f"In einem anderen Tab entschieden: {item.title}")
            elif item.status is ApprovalStatus.EXPIRED:
                self.frame.toast(f"Frist abgelaufen, als abgelehnt gewertet: {item.title}")
            else:
                self.frame.toast(item.status_text or f"Nicht mehr nötig: {item.title}")
        await self._render_history()


def register() -> None:
    """Register `/freigaben` and `/freigaben/{approval_id}`."""

    async def build(approval_id: str | None, art: str, ansicht: str, zustand: str = "") -> None:
        api = state_api(get_api(), zustand)
        focus_card, focus = approval_id, None
        if approval_id:
            try:
                still_open = (await api.get_approval(approval_id)).status is ApprovalStatus.OPEN
            except ApiException:
                still_open = False
            if not still_open:
                focus_card, focus = None, "#inhalt h1"
        frame = Frame(
            api, active="freigaben", title="Freigaben", focus_card=focus_card, focus=focus
        )
        await frame.start()
        if forced_state(frame, zustand, "freigaben", title="Freigaben", path="/freigaben"):
            return
        page = FreigabenPage(
            frame, art=art, history=ansicht == "verlauf" and not approval_id, deep_link=approval_id
        )
        await page.build()

    @ui.page("/freigaben")
    async def freigaben(art: str = "alle", ansicht: str = "offen", zustand: str = "") -> None:
        await build(None, art, ansicht, zustand)

    @ui.page("/freigaben/{approval_id}")
    async def freigabe(approval_id: str, art: str = "alle") -> None:
        await build(approval_id, art, "offen")

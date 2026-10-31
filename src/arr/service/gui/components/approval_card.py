"""K5 approval card: three types plus the compact variant (docs/08-ux-konzept.md G2, K5, §5.2).

Everything needed for the decision is on the card without expanding: who, what (command),
where, why (agent's reason), why at you (mandate check or gatekeeper), risk, consequences,
deadline; run starts show the block „Abweichungen von deinen Standards“ with the vendors that
receive content (E19). The card is a `data-primary-scope` for ⌘↩; its heading is the focus
target of deep links; grant buttons ignore Space/Enter (arr.js).
"""

import math
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from arr.models import ChannelMode, WorkspaceMode
from arr.permissions.types import (
    ApprovalType,
    DecidedBy,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    RequestCategory,
)
from arr.service.api_models import ApprovalItem, DecisionRequest, Deviation
from arr.service.gui import format as fmt
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import banner_html
from arr.service.gui.components.base import (
    button,
    esc,
    link_html,
    markdown_html,
    raw,
    set_disabled,
)
from arr.service.gui.components.copy_field import copy_html
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.texts import (
    APPROVAL_TYPE,
    CATEGORY,
    DECIDER,
    ESCALATED_BY,
    RISK,
    STARTER,
    STARTER_PHRASE,
    TOPOLOGY_LABEL,
    VENDOR,
    Status,
    scope_label,
)

DecideHandler = Callable[["ApprovalCard", DecisionRequest], Awaitable[Any]]

_WAIT = Status("st-wait", "front_hand", "")
_MODE = {
    ChannelMode.EVENT: "ereignisgesteuert",
    ChannelMode.ROUNDS: "Runden",
    ChannelMode.MODERATED: "moderiert",
}
_WORKSPACE = {
    WorkspaceMode.COPY: "Kopie",
    WorkspaceMode.SHARED: "geteilt",
    WorkspaceMode.EMPTY: "leer",
}


def card_dom_id(approval_id: str) -> str:
    """DOM id of a card (same as the approval id, so `#apr_0042` works as anchor)."""
    return approval_id


def heading_id(approval_id: str) -> str:
    """DOM id of a card heading, the focus target of deep links."""
    return f"{approval_id}-h"


def risk_html(item: ApprovalItem) -> str:
    """Risk as word + dots, never colour alone (K5, §7.7)."""
    if item.risk is None:
        return ""
    dots, word = RISK[item.risk.level]
    return (
        f'<span class="risk" title="{esc(item.risk.text)}">Risiko '
        f'<span class="dots" aria-hidden="true">{dots}</span> {esc(word)}</span>'
    )


def deadline_html(item: ApprovalItem, now: float, *, short: bool = False) -> str:
    """Live countdown „⏱ läuft in 14 min ab (15:36)“, updated by arr.js."""
    if item.deadline is None:
        return ""
    return (
        f'<span class="deadline">⏱ <span data-deadline="{item.deadline}">'
        f"{esc(fmt.deadline(item.deadline, now))}</span></span>"
    )


def providers_html(dev: Deviation) -> str:
    """„↳ Inhalte gehen an Anthropic (author, …) und OpenAI (judge-2)“ (E19)."""
    if not dev.providers:
        return ""
    parts = [f"<b>{esc(VENDOR[p.vendor])}</b> ({esc(', '.join(p.agents))})" for p in dev.providers]
    return f'<br><span class="provider-note">↳ Inhalte gehen an {fmt.join_de(parts)}</span>'


def deviations_html(item: ApprovalItem, heading: str) -> str:
    """Block „⚠ Abweichungen von deinen Standards (n)“ – always visible, never collapsed."""
    if not item.deviations:
        return (
            '<section class="dev ok"><h4>✓ Keine Abweichungen von deinen Standards</h4></section>'
        )
    lines = []
    for dev in item.deviations:
        value = f" {esc(dev.value_text)}" if dev.value_text else ""
        lines.append(
            f"<li><b>{esc(dev.label)}</b>{value} "
            f'<span class="muted">(Standard: {esc(dev.default_text)})</span>{providers_html(dev)}'
            "</li>"
        )
    notes = ""
    if item.standard_notes:
        notes = f'<div class="none">Wie Standard: {esc(", ".join(item.standard_notes))}.</div>'
    return (
        f'<section class="dev" aria-labelledby="{heading}">'
        f'<h4 id="{heading}"><span aria-hidden="true">⚠</span> Abweichungen von deinen Standards '
        f"({len(item.deviations)})</h4><ul>{''.join(lines)}</ul>{notes}</section>"
    )


def _origin(item: ApprovalItem, now: float) -> str:
    rel = fmt.relative(item.created_at, now)
    at = fmt.clock(item.created_at) + (f" ({rel})" if rel else "")
    return at


class ApprovalCard:
    """A full approval card on the Freigaben page; the page decides via `on_decide`."""

    def __init__(
        self,
        item: ApprovalItem,
        *,
        now: float,
        on_decide: DecideHandler,
        dialog: ConfirmDialog,
        workfolder: str,
        highlight: bool = False,
    ) -> None:
        self.item = item
        self.on_decide = on_decide
        self.dialog = dialog
        self.workfolder = workfolder
        self.scope = DecisionScope.ONCE
        self.option: EscalationOption | None = None
        self.comment = ""
        self.busy = False
        self.decided = False
        self.buttons: list[ui.element] = []
        self.element = (
            ui.element("article")
            .classes("appr focus" if highlight else "appr")
            .props(
                f'id="{card_dom_id(item.id)}" aria-labelledby="{heading_id(item.id)}" '
                "data-primary-scope"
            )
        )
        self.element.mark(f"card-{item.id}")
        with self.element:
            self.head = raw(self._head(now), classes="ah")
            raw(
                f'<h3 id="{heading_id(item.id)}" tabindex="-1">{esc(item.title)}</h3>',
                tag="div",
            )
            if item.type is ApprovalType.PERMISSION:
                self._permission_body()
            elif item.type is ApprovalType.ESCALATION:
                self._escalation_body()
            else:
                self._run_start_body()

    def update(self, item: ApprovalItem, now: float) -> None:
        """Take over a new count of equal requests (live); the decision stays as it is."""
        self.item = item
        self.head.set_content(self._head(now))
        if item.type is ApprovalType.PERMISSION:
            self.body.set_content(self._permission_html())

    def show_decided(self, message: str, note: str, *, positive: bool) -> None:
        """Replace the content by a confirmation of the same height, without buttons (N1).

        The card stops being a ⌘↩ scope; its old content stays in the layout but hidden.
        """
        self.decided = True
        self.element.props(remove="data-primary-scope")
        self.element.classes(add="decided")
        symbol = "✓" if positive else "✕"
        with self.element:
            raw(
                f'<span aria-hidden="true">{symbol}</span> {esc(message)} '
                f'<span class="muted">· {esc(note)}</span>',
                classes="appr-done" if positive else "appr-done neutral",
            ).props('role="status"').mark(f"decided-{self.item.id}")

    # --- header ---------------------------------------------------------------------------

    def _head(self, now: float) -> str:
        item = self.item
        parts = [badge_html(_WAIT, word=APPROVAL_TYPE[item.type])]
        if item.count > 1:
            parts.append(f'<span class="tag">{item.count} gleiche Anfragen</span>')
        run_link = link_html(item.run.name, f"/runs/{item.run.id}")
        if item.type is ApprovalType.PERMISSION:
            parts += [f'<span class="tag">{esc(CATEGORY[c])}</span>' for c in item.categories]
            parts.append(risk_html(item))
            via = ESCALATED_BY.get(item.escalated_by.decider) if item.escalated_by else None
            parts.append(f"<span>· {esc(via) + ' · ' if via else ''}{_origin(item, now)}</span>")
            parts.append(deadline_html(item, now))
        elif item.type is ApprovalType.ESCALATION:
            topology = TOPOLOGY_LABEL[item.topology] if item.topology else "Ablauf"
            stage = f" · Stufe {item.stage}" if item.stage else ""
            parts.append(f'<span class="tag">{esc(topology + stage)}</span>')
            parts.append(
                f"<span>· {run_link} · Run pausiert seit {fmt.clock(item.created_at)} · "
                "nur hier entscheidbar, nicht aus Claude Code</span>"
            )
        else:
            starter = STARTER_PHRASE[item.requested_by] if item.requested_by else "von dir"
            parts.append(
                f"<span>{esc(starter)} im Projekt <b>{esc(item.run.project)}</b> · "
                f"{_origin(item, now)} · keine Frist</span>"
            )
        return "".join(p for p in parts if p)

    # --- permission ---------------------------------------------------------------------

    def _permission_html(self) -> str:
        item = self.item
        agent = item.agent
        assert agent is not None
        what = item.command or item.path or item.url or item.tool or ""
        why = esc(item.why or "Du entscheidest jede Anfrage selbst.")
        if item.escalated_by is not None:
            who = (
                "Vollmacht (von arr geprüft):"
                if item.escalated_by.decider is DecidedBy.MANDATE_RULE
                else "Aufsichts-Agent:"
            )
            why = f"<b>{who}</b> {esc(item.escalated_by.note)}"
        rows = [
            (
                "Wer",
                f'<span class="mono">{esc(agent.id)}</span> · {esc(agent.role_label)} · '
                f"{esc(agent.model_label)} · {link_html(item.run.name, f'/runs/{item.run.id}')}",
            ),
        ]
        if item.work_dir:
            rows.append(("Wo", copy_html(item.work_dir, label="Arbeitsordner kopieren")))
        reason = f"„{esc(item.reason)}“" if item.reason else '<span class="muted">keine</span>'
        rows.append(("Begründung des Agenten", f'<div class="quote">{reason}</div>'))
        rows.append(("Warum bei dir", why))
        if item.no_sandbox:
            rows.append(("Achtung", f"<b>{esc(NO_SANDBOX)}</b>"))
        if item.count > 1:
            rows.append(
                ("Gleiche Anfragen", f"Deine Entscheidung beantwortet alle {item.count} Anfragen.")
            )
        if RequestCategory.READ_OUTSIDE in item.categories and item.content_to:
            vendors = fmt.join_de([VENDOR[v] for v in item.content_to])
            rows.append(("Inhalt geht an", f"<b>{esc(vendors)}</b> (als Kontext des Agenten)"))
        if item.consequences:
            c = item.consequences
            rows.append(
                (
                    "Was passiert",
                    f"Erlaubt: {esc(c.allow)} Abgelehnt: {esc(c.deny)} {esc(c.expire)}",
                )
            )
        kv = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
        links = []
        if item.transcript_seq is not None:
            links.append(
                link_html(
                    "Im Transkript ansehen",
                    f"/runs/{item.run.id}?agent={agent.id}&seq={item.transcript_seq}",
                )
            )
        links.append(link_html("Vollmacht ansehen", "/einstellungen#vollmacht"))
        return (
            f'<span class="cmd">{esc(what)}</span><dl class="kv" style="margin-top:12px">{kv}</dl>'
            f'<div class="row small" style="margin-top:8px">{" · ".join(links)}</div>'
        )

    def _permission_body(self) -> None:
        item = self.item
        agent = item.agent
        assert agent is not None
        self.body = raw(self._permission_html(), classes="ab")
        if item.terminal:
            self._terminal_foot()
            return
        with ui.element("div").classes("af"):
            with ui.element("div").classes("field"):
                label_id = f"{item.id}-scope"
                raw('<span class="label small">Reichweite</span>', tag="span").props(
                    f'id="{label_id}"'
                )
                with (
                    ui.element("div")
                    .classes("seg")
                    .props(f'role="radiogroup" aria-labelledby="{label_id}"')
                ):
                    for scope in self._scopes():
                        text = scope_label(scope, agent.id) + (
                            " …" if scope is DecisionScope.ALWAYS else ""
                        )
                        self._radio(
                            f"r-{item.id}",
                            scope.value,
                            text,
                            scope is DecisionScope.ONCE,
                            lambda s=scope: self._set_scope(s),
                        )
            self._comment_input(f"Kommentar (optional, bei Ablehnung an {agent.id})")
            with ui.element("div").classes("decide"):
                self.buttons.append(
                    button("Ablehnen", lambda: self._decide(DecisionAction.DENY)).mark("deny")
                )
                self.buttons.append(
                    button(
                        "Erlauben",
                        self._allow,
                        classes="btn primary",
                        primary=True,
                        grant=True,
                        key_hint="⌘↩",
                    ).mark("allow")
                )

    def _scopes(self) -> list[DecisionScope]:
        # a Codex escalation leaves the sandbox: only this one request (W3-03); „immer“ only
        # where a rule can come of it, and the dialog names that rule
        if self.item.no_sandbox:
            return [DecisionScope.ONCE]
        if self.item.rule_label is None:
            return [s for s in DecisionScope if s is not DecisionScope.ALWAYS]
        return list(DecisionScope)

    def _set_scope(self, scope: DecisionScope) -> None:
        self.scope = scope

    async def _allow(self) -> None:
        item = self.item
        if self.scope is DecisionScope.ALWAYS:
            self.dialog.show(
                f"{item.rule_label} immer erlauben?",
                [
                    *item.rule_effects,
                    f"Wird feste Regel für alle Runs in „{self.workfolder}“.",
                    "Diese Anfrage wird sofort erlaubt.",
                    "Du entfernst die Regel unter Einstellungen → Feste Regeln.",
                ],
                "Immer erlauben",
                lambda _v: self._decide(DecisionAction.ALLOW),
                danger=False,
                grant=True,
            )
            return
        await self._decide(DecisionAction.ALLOW)

    # --- escalation -----------------------------------------------------------------------

    def _escalation_body(self) -> None:
        item = self.item
        pass_score = item.attempts[0].pass_score if item.attempts else None
        chips = "".join(
            f'<span class="att {"pass" if a.passed else "fail"}">V{a.n} {fmt.score(a.avg_score)}'
            "</span>"
            for a in item.attempts
        )
        last = item.attempts[-1].n if item.attempts else 1
        links = [
            link_html(
                f"{item.stage or 'Ergebnis'} V{last} lesen",
                f"/runs/{item.run.id}?ansicht=ergebnisse"
                + (f"&ergebnis={item.result_id}" if item.result_id else ""),
            ),
            link_html("alle Ratings", f"/runs/{item.run.id}?ansicht=ergebnisse"),
        ]
        comments = "".join(
            f'<div><b class="mono">{esc(r.rater)}</b> ({fmt.score(r.score)}): '
            f"„{esc(r.comment)}“</div>"
            for r in item.last_ratings
        )
        limit = f" (bestanden ab {fmt.threshold(pass_score)})" if pass_score is not None else ""
        with ui.element("div").classes("ab"):
            raw(
                '<div class="row" style="gap:24px;align-items:flex-start">'
                f'<div style="min-width:230px"><div class="small muted">Durchschnitt pro Versuch'
                f'{limit}</div><div class="attempts" style="margin-top:6px">{chips}</div>'
                f'<div class="small" style="margin-top:8px">{" · ".join(links)}</div></div>'
                f'<div style="flex:1;min-width:280px" class="stack small">{comments}</div></div>'
            )
            if item.terminal:
                self._terminal_foot()
                return
            group = f"{item.id}-opt"
            with (
                ui.element("div")
                .classes("radio-cards")
                .style("margin-top:12px;grid-template-columns:repeat(3,minmax(0,1fr))")
                .props(f'role="radiogroup" aria-label="Wie geht es weiter?" id="{group}"')
            ):
                for opt in item.options:
                    with ui.element("label").classes("radio-card"):
                        radio = ui.element("input").props(
                            f'type="radio" name="{group}" value="{opt.key.value}"'
                        )
                        radio.on("change", lambda _e, k=opt.key: self._set_option(k))
                        radio.mark(f"option-{opt.key.value}")
                        raw(
                            f'<b>{esc(opt.label)}</b><span class="small muted">{esc(opt.help)}'
                            "</span>",
                            tag="span",
                        )
        with ui.element("div").classes("af"):
            to = self.item.comment_to.id if self.item.comment_to else None
            self._comment_input(f"Kommentar an {to} (optional)" if to else "Kommentar (optional)")
            with ui.element("div").classes("decide"):
                self.decide_button = button(
                    "Entscheiden",
                    self._click_decide,
                    classes="btn primary",
                    primary=True,
                    grant=True,
                    key_hint="⌘↩",
                    disabled=True,
                    title="Wähle zuerst, wie es weitergeht.",
                ).mark("decide")
                self.buttons.append(self.decide_button)

    def _click_decide(self) -> Awaitable[None] | None:
        # checked at click time: a click without a chosen option never decides later
        if self.option is None:
            return None
        return self._decide(DecisionAction.OPTION)

    def _set_option(self, key: EscalationOption) -> None:
        self.option = key
        set_disabled(self.decide_button, False)
        self.decide_button.props(remove="title")

    # --- run start --------------------------------------------------------------------------

    def _run_start_body(self) -> None:
        item = self.item
        params = (
            " · ".join(f"{esc(k)} <b>{esc(v)}</b>" for k, v in item.params.items())
            or '<span class="muted">Standardwerte</span>'
        )
        kv = [
            (
                "Skill",
                f"{link_html(item.skill or 'ohne Skill', f'/skills/{item.skill}')} · Topologie "
                f"{esc(item.topology.value if item.topology else '–')}",
            ),
            ("Parameter", f'<span class="small">{params}</span>'),
        ]
        if item.limits:
            lim = item.limits
            kv.append(
                (
                    "Limits",
                    f"{fmt.limit_hours(lim.wall_time_s)} · {lim.turns_per_agent or '–'} Turns pro "
                    f"Agent · {fmt.budget(lim.budget_usd)}",
                )
            )
        if item.decider:
            kv.append(("Berechtigungs­anfragen", esc(DECIDER[item.decider])))
        if item.estimate:
            est = item.estimate
            approx = "≈ " if est.cost_kind and est.cost_kind.value == "estimated" else ""
            note = " (rechnerisch)" if approx else ""
            kv.append(
                (
                    "Schätzung",
                    f"{est.turns.min:.0f}–{est.turns.max:.0f} Turns · {approx}"
                    f"{est.cost_usd.min:.0f}–{est.cost_usd.max:.0f} ${note} · "
                    f"{est.duration_s.min / 60:.0f}–{est.duration_s.max / 60:.0f} min",
                )
            )
        kv_html = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in kv)
        web_dev = any(d.path.endswith("tools.web") for d in item.deviations)
        agent_rows = "".join(
            f'<tr><td class="mono">{esc(a.id)}</td><td>{esc(a.model_label)} · {esc(a.profile)}</td>'
            f"<td>{'<b>⚠ an</b>' if a.tools.web and web_dev else ('an' if a.tools.web else 'aus')}"
            f"</td><td>{_WORKSPACE[a.workspace_mode]}</td></tr>"
            for a in item.agents
        )
        agents = (
            '<table class="tbl compact"><thead><tr><th>Agent</th><th>Modell · Profil</th>'
            f"<th>Web</th><th>Arbeitsordner</th></tr></thead><tbody>{agent_rows}</tbody></table>"
        )
        channels = "".join(
            f'<div><span class="mono">#{esc(c.name)}</span> · {esc(", ".join(c.members))}'
            + (" · anonym" if c.anonymous else "")
            + f" · {_MODE[c.mode]}"
            + (f" · offen ab Phase „{esc(c.open_from)}“" if c.open_from else "")
            + "</div>"
            for c in item.channels
        )
        capacity = ""
        for cap in item.capacity:
            if cap.free < cap.needed:
                capacity += banner_html(
                    "warn",
                    "Kapazität knapp",
                    f"{cap.profile} hat {cap.free} von {cap.limit} Plätzen frei, der Run braucht "
                    f"{cap.needed}. Die Agenten warten, bis Plätze frei werden.",
                    small=True,
                )
        task = ""
        if item.task_md:
            blocks = [b for b in item.task_md.strip().split("\n\n") if b.strip()]
            lines = item.task_md.strip().count("\n") + 1
            head = markdown_html("\n\n".join(blocks[:3]))
            rest = ""
            if len(blocks) > 3:
                rest = (
                    f'<details><summary class="small">Ganze Aufgabe anzeigen ({lines} Zeilen)'
                    f"</summary>{markdown_html(chr(10).join(blocks[3:]))}</details>"
                )
            task = (
                '<div class="small muted">Aufgabe (task.md)</div>'
                f'<div class="prose card tight" style="margin-top:4px">{head}{rest}</div>'
            )
        raw(
            deviations_html(item, f"{item.id}-dev")
            + '<div class="card-grid"><div>'
            + task
            + f'<dl class="kv" style="margin-top:12px">{kv_html}</dl></div><div>'
            + agents
            + (
                '<div class="small" style="margin-top:10px"><b>Gruppenchats</b></div>'
                f'<div class="small stack" style="margin-top:4px">{channels}</div>'
                if channels
                else ""
            )
            + (f'<div style="margin-top:12px">{capacity}</div>' if capacity else "")
            + "</div></div>",
            classes="ab",
        )
        with ui.element("div").classes("af"):
            raw(
                "„Anpassen“ behält die Run-ID. Claude Code erfährt deine Änderungen.",
                tag="span",
                classes="small muted",
            )
            with ui.element("div").classes("decide"):
                self.buttons.append(button("Ablehnen …", self._deny_run_start).mark("deny"))
                raw(
                    f'<a class="btn" href="/neuer-run?von={esc(item.id)}">Anpassen</a>',
                    tag="span",
                ).mark("adjust")
                self.buttons.append(
                    button(
                        "Freigeben",
                        lambda: self._decide(DecisionAction.ALLOW),
                        classes="btn primary",
                        primary=True,
                        grant=True,
                        key_hint="⌘↩",
                    ).mark("allow")
                )

    def _deny_run_start(self) -> None:
        item = self.item
        self.dialog.show(
            "Run-Start ablehnen?",
            [
                f"„{item.title}“ wird nicht gestartet (Status „Gestoppt“).",
                "Claude Code erhält deine Nachricht.",
            ],
            "Ablehnen",
            lambda text: self._decide(DecisionAction.DENY, comment=text),
            input_label="Nachricht an Claude Code",
        )

    # --- shared ---------------------------------------------------------------------------

    def _terminal_foot(self) -> None:
        raw(esc(TERMINAL_HINT), classes="af small muted").mark(f"terminal-{self.item.id}")

    def _radio(
        self, name: str, value: str, label: str, checked: bool, on_change: Callable[[], None]
    ) -> None:
        with ui.element("label"):
            radio = ui.element("input").props(
                f'type="radio" name="{name}" value="{value}"' + (" checked" if checked else "")
            )
            radio.on("change", lambda _e: on_change())
            radio.mark(f"scope-{value}")
            raw(esc(label), tag="span")

    def _comment_input(self, placeholder: str, *, width: str | None = None) -> None:
        field = ui.element("input").props(
            f'type="text" placeholder="{esc(placeholder)}" aria-label="{esc(placeholder)}"'
        )
        if width:
            field.style(f"width:{width}")
        field.on("input", self._set_comment, js_handler="(e) => emit(e.target.value)")
        field.mark("comment")

    def _set_comment(self, event: Any) -> None:
        self.comment = str(event.args or "")

    async def _decide(self, action: DecisionAction, *, comment: str | None = None) -> None:
        if self.busy or self.decided:
            return
        if action is DecisionAction.OPTION and self.option is None:
            return
        request = DecisionRequest(
            action=action,
            scope=self.scope if self.item.type is ApprovalType.PERMISSION else DecisionScope.ONCE,
            comment=(comment if comment is not None else self.comment) or None,
            option_key=self.option if action is DecisionAction.OPTION else None,
        )
        self.busy = True
        for b in self.buttons:
            set_disabled(b, True)
        try:
            await self.on_decide(self, request)
        finally:
            self.busy = False
            if not self.element.is_deleted:
                for b in self.buttons:
                    set_disabled(b, b is getattr(self, "decide_button", None) and not self.option)


TERMINAL_HINT = "Dieser Run läuft im Terminal. Entscheide die Anfrage dort, hier siehst du sie nur."
NO_SANDBOX = (
    "Erhöhte Rechte: Der Befehl läuft nicht in der Sandbox des Agenten, ist aber auch nicht frei. "
    "Was er darf, legt Codex fest, arr kann es nicht zusagen. Er kann trotz Freigabe scheitern."
)
COMPACT_LINE_CHARS = 32
"""Characters of a command per line in the compact card (300 px column, 12 px monospace)."""
COMPACT_MAX_LINES = 12
"""Longer commands are not decided in the compact card, only on the Freigaben page (W1-07.1)."""
TOO_TALL_HINT = "Die Karte passt nicht in die Spalte. Erlauben geht über „Details“."


def compact_lines(command: str) -> int:
    """Lines a command takes in the compact card when it wraps."""
    return sum(max(1, math.ceil(len(line) / COMPACT_LINE_CHARS)) for line in command.split("\n"))


def compact_card(
    item: ApprovalItem,
    *,
    now: float,
    on_decide: Callable[[ApprovalItem, DecisionRequest], Awaitable[Any]] | None = None,
    prefix: str = "",
) -> ui.element:
    """Compact card for the run detail overview (K5 „kompakt“, W1-07).

    Permissions show command, agent's reason, „Warum bei dir“, risk as word and deadline, with
    „Erlauben (einmalig)“; run starts and escalations are never decided here, only linked. So is
    a card that answers several requests or reaches further hosts: only the full card says so.
    With `prefix` the card gets the DOM id `<prefix><approval id>` and its heading
    `<prefix><approval id>-ch`, so the same request can be shown twice on one page.
    """
    heading = f"{prefix}{item.id}-ch"
    element = (
        ui.element("article")
        .classes("appr compact")
        .props(f'aria-labelledby="{heading}" data-primary-scope')
    )
    if prefix:
        element.props(f'id="{prefix}{item.id}"')
    with element:
        head = [badge_html(_WAIT, word=APPROVAL_TYPE[item.type])]
        if item.type is ApprovalType.PERMISSION:
            if item.count > 1:
                head.append(f'<span class="tag">{item.count} gleiche Anfragen</span>')
            head += [risk_html(item), deadline_html(item, now)]
        elif item.type is ApprovalType.RUN_START:
            starter = STARTER.get(item.requested_by, "dir") if item.requested_by else "dir"
            head.append(f"<span>{esc(starter)} · {esc(item.run.project)}</span>")
        else:
            head.append("<span>Run pausiert</span>")
        raw("".join(head), classes="ah")
        raw(f'<h3 id="{heading}">{esc(item.title)}</h3>')
        details = f"/freigaben/{item.id}"
        if item.type is ApprovalType.PERMISSION:
            why = item.why or "Du entscheidest jede Anfrage."
            if item.escalated_by:
                why = item.escalated_by.note
            what = item.command or item.path or item.url or ""
            raw(
                f'<span class="cmd" style="font-size:12px">{esc(what)}</span>'
                f'<div class="reason" style="margin-top:6px" title="{esc(item.reason or "")}">'
                f"<b>Begründung:</b> „{esc(item.reason or '')}“</div>"
                f'<div style="margin-top:4px"><b>Warum bei dir:</b> {esc(why)}</div>'
                + (f'<div style="margin-top:4px"><b>{esc(NO_SANDBOX)}</b></div>' * item.no_sandbox),
                classes="ab small",
            )
            if item.terminal:
                raw(esc(TERMINAL_HINT), classes="af small muted")
                return element
            only_there = ""
            if compact_lines(what) > COMPACT_MAX_LINES:
                # too long to be read whole in the narrow column: decide on the Freigaben page
                only_there = "Zu lang für die kompakte Karte."
            elif item.count > 1:
                only_there = f"Das beantwortet {item.count} gleiche Anfragen."
            elif item.also_hosts:
                only_there = f"Die Erlaubnis reicht auch bis {' und '.join(item.also_hosts)}."
            if only_there:
                raw(
                    f'<span class="small muted">{esc(only_there)} Entscheide auf der '
                    "Freigaben-Seite.</span>"
                    f'<div class="decide"><a class="btn btn-sm primary" href="{esc(details)}">'
                    "Details</a></div>",
                    classes="af",
                ).mark(f"{prefix}too-long-{item.id}")
                return element

            async def decide(action: DecisionAction) -> None:
                if on_decide is not None:
                    await on_decide(item, DecisionRequest(action=action))

            with ui.element("div").classes("af"):
                raw(f'<a class="btn btn-sm" href="{esc(details)}">Details</a>', tag="span")
                # shown by arr.js when the card is taller than its column (W1-07.1)
                raw(TOO_TALL_HINT, tag="span", classes="too-tall-note small muted")
                with ui.element("div").classes("decide"):
                    button("Ablehnen", lambda: decide(DecisionAction.DENY), classes="btn btn-sm")
                    button(
                        "Erlauben (einmalig)",
                        lambda: decide(DecisionAction.ALLOW),
                        classes="btn btn-sm primary",
                        primary=True,
                        grant=True,
                    )
        elif item.type is ApprovalType.RUN_START:
            summary = " · ".join(d.label for d in item.deviations)
            raw(
                f'<div class="dev" style="margin:0"><h4>⚠ {len(item.deviations)} Abweichungen von '
                f"deinen Standards</h4>{esc(summary)}</div>",
                classes="ab small",
            )
            raw(
                '<span class="small muted">Freigabe nur auf der Freigaben-Seite</span>'
                f'<div class="decide"><a class="btn btn-sm primary" href="{esc(details)}">'
                "Ansehen</a></div>",
                classes="af",
            )
        else:
            chips = "".join(
                f'<span class="att fail">V{a.n} {fmt.score(a.avg_score)}</span>'
                for a in item.attempts
            )
            raw(f'<div class="attempts">{chips}</div>', classes="ab small")
            raw(
                f'<div class="decide"><a class="btn btn-sm primary" href="{esc(details)}">'
                "Entscheiden</a></div>",
                classes="af",
            )
    return element

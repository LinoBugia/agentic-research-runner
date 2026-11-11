"""Einstellungen: set standards and rules, above all approvals and the mandate (docs/08 §5.11).

Sections with their own „Speichern“ (⌘↩ in the section): Freigaben und Vollmacht (who decides,
deadline, model of the supervising agent; fixed: runs from Claude Code always need your
approval, E15; mandate editor with „Beispiel einfügen“ and a confirmation „gilt sofort“),
Feste Regeln (own „immer“ rules removable), Aufräumregeln [S], Standards und Limits, Python-
Umgebung with „Testen“, Mitteilungen, Darstellung [S], Allgemein. Workfolder settings live in
`arr.yaml`; when it is broken, its sections show a banner and the app sections stay usable.
`/einstellungen#vollmacht` jumps to a section (§4.3).
"""

import re
from collections.abc import Callable
from typing import Any

from nicegui import ui

from arr.models import Decider, Effort, Provider, RunVenvMode
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    SYSTEM_PYTHON,
    ApiException,
    Appearance,
    AppSettings,
    FixedRule,
    MandateInfo,
    MandateUpdate,
    NotificationEvent,
    OpenIn,
    ProfileInfo,
    ProviderDefaultsInfo,
    RuleEffect,
    WorkfolderSettingsInfo,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import banner
from arr.service.gui.components.base import button, esc, icon, raw
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.form import Field, checkbox, radio
from arr.service.gui.components.page_state import (
    error_html,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.layout import Frame
from arr.service.gui.pages.neuer_run import duration_text, money_text, parse_duration, parse_money
from arr.service.gui.texts import Status

SECTIONS = [
    ("wf", None),
    ("vollmacht", "Freigaben und Vollmacht"),
    ("regeln", "Feste Regeln"),
    ("standards", "Standards und Limits"),
    ("python", "Python-Umgebung"),
    ("aufraeumen", "Aufräumregeln"),
    ("arr", None),
    ("allgemein", "Allgemein"),
    ("mitteilungen", "Mitteilungen"),
    ("darstellung", "Darstellung"),
]
DECIDERS = [
    (
        Decider.GATEKEEPER_THEN_USER,
        "Aufsichts-Agent, bei Zweifel du",
        "Der Aufsichts-Agent entscheidet im Rahmen deiner Vollmacht. Was außerhalb liegt oder "
        "unklar ist, kommt zu dir.",
    ),
    (Decider.USER, "Immer du", "Jede Anfrage landet in deiner Inbox und als Mitteilung."),
    (
        Decider.GATEKEEPER,
        "Nur der Aufsichts-Agent",
        "Was außerhalb der Vollmacht liegt, wird abgelehnt, ohne dich zu fragen.",
    ),
]
GATEKEEPER_MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
MODELS = {
    Provider.CLAUDE: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
    Provider.CODEX: ["gpt-5.5", "gpt-5.5-mini"],
}
EFFORTS = {
    Provider.CLAUDE: [Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.MAX],
    Provider.CODEX: [Effort.MINIMAL, Effort.LOW, Effort.MEDIUM, Effort.HIGH],
}
PROVIDER_WORD = {Provider.CLAUDE: "Claude", Provider.CODEX: "ChatGPT"}
NOTIFICATIONS = [
    (NotificationEvent.APPROVAL, "Neue Freigabe (Run-Start, Berechtigung, Eskalation)"),
    (NotificationEvent.RUN_FINISHED, "Run fertig, gestoppt, fehlgeschlagen oder abgebrochen"),
    (NotificationEvent.LIMIT_REACHED, "Limit erreicht (Zeit, Budget, Nutzungslimit eines Profils)"),
    (NotificationEvent.STORAGE_LOW, "Speicher knapp"),
    (NotificationEvent.MESSAGE_TO_USER, "Neue Nachricht an dich im Gruppenchat"),
]
NOTIFICATIONS_LATER = frozenset(
    {
        NotificationEvent.LIMIT_REACHED,
        NotificationEvent.STORAGE_LOW,
        NotificationEvent.MESSAGE_TO_USER,
    }
)
"""`notify.py` sends nothing for these yet; their saved value stays."""
EFFECT = {
    RuleEffect.ALLOW: Status("st-ok", "check", "erlaubt"),
    RuleEffect.DENY: Status("st-err", "block", "verboten"),
    RuleEffect.ASK: Status("st-wait", "front_hand", "Freigabe"),
}
DAYS = re.compile(r"\s*(\d+)\s*(tage?n?|t|d)?\s*$", re.I)


def parse_days(text: str) -> int | None:
    m = DAYS.match(text)
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def checked_html(mandate: MandateInfo) -> str:
    """„Vollmacht (von arr geprüft)“: what arr itself checks before the supervising agent."""
    checks = mandate.checked
    domains = ", ".join(checks.domains) or "keine"
    install = (
        "pip install von PyPI in die Run-venv" if checks.install == "pypi" else "keine Installation"
    )
    rows = [
        ("Netzwerk zu", domains),
        ("Installation", install),
        ("Lese-Ordner", "lassen sich über die Vollmacht nicht freigeben"),
    ]
    items = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    return (
        '<div class="mandate-checked"><div class="small"><b>Vollmacht (von arr geprüft)</b> '
        '<span class="faint">· nur was hier steht, kann der Aufsichts-Agent erlauben, jeweils '
        f'einmal</span></div><dl class="kv small">{items}</dl></div>'
    )


def section_card(anchor: str, title: str, sub: str = "") -> ui.element:
    """A settings section: card with id for deep links (`#vollmacht`) and own ⌘↩ scope."""
    card = (
        ui.element("section")
        .classes("card")
        .props(f'id="{anchor}" aria-labelledby="h-{anchor}" data-form-scope')
    )
    with card:
        raw(
            f'<div class="card-head"><h2 id="h-{anchor}" tabindex="-1">{esc(title)}</h2>'
            + (f'<span class="faint small">{sub}</span>' if sub else "")
            + "</div>"
        )
    return card


def save_row(
    on_save: Callable[[], Any], *, label: str = "Speichern", note: str = "", marker: str = ""
) -> ui.element:
    with ui.element("div").classes("save-row") as row:
        if note:
            raw(esc(note), tag="span", classes="faint small")
        btn = button(label, on_save, classes="btn primary", key_hint="⌘↩").props("data-form-submit")
        if marker:
            btn.mark(marker)
    return row


class EinstellungenPage:
    """State of one settings page (one browser tab)."""

    def __init__(self, frame: Frame, api: ApiClient) -> None:
        self.frame = frame
        self.api = api
        self.app: AppSettings | None = None
        self.wf: WorkfolderSettingsInfo | None = None
        self.workfolder_name = ""
        self.workfolder_path = ""

    async def build(self) -> None:
        workfolder = await self.api.get_workfolder()
        self.workfolder_name = workfolder.name
        self.workfolder_path = workfolder.path
        with self.frame.content:
            self.dialog = ConfirmDialog()
            raw('<div class="page-head"><h1 tabindex="-1">Einstellungen</h1></div>')
            with ui.element("div").classes("settings-grid"):
                with ui.element("nav").classes("subnav").props('aria-label="Abschnitte"'):
                    links = []
                    for key, label in SECTIONS:
                        if label is None:
                            group = "arr" if key == "arr" else f"Workfolder „{workfolder.name}“"
                            links.append(f'<div class="grp">{esc(group)}</div>')
                        else:
                            links.append(f'<a href="#{key}">{esc(label)}</a>')
                    links.append('<div class="grp">Hilfe</div>')
                    links.append('<a href="/einrichtung">Einrichtung erneut durchlaufen</a>')
                    raw("".join(links), tag="div", classes="subnav")
                with ui.element("div"):
                    self.wf_box = ui.element("div")
                    self.app_box = ui.element("div").style("margin-top:16px")
        await load_into(
            self.wf_box,
            self._load_wf,
            self._render_wf,
            retry_href="/einstellungen",
            skeleton=skeleton_html(8, reason="arr.yaml wird gelesen …"),
        )
        await load_into(
            self.app_box,
            self.api.get_app_settings,
            self._render_app,
            retry_href="/einstellungen",
            skeleton=skeleton_html(4),
        )

    async def _load_wf(self) -> dict[str, Any]:
        return {
            "wf": await self.api.get_workfolder_settings(),
            "rules": await self.api.list_fixed_rules(),
            "profiles": await self.api.list_profiles(),
        }

    async def _render_wf(self, data: dict[str, Any]) -> None:
        self.wf = data["wf"]
        self._approvals()
        self.mandate_box = ui.element("div").style("margin-top:16px")
        with self.mandate_box:
            try:
                mandate = await self.api.get_mandate()
            except ApiException as exc:
                raw(error_html(exc, retry_href="/einstellungen#vollmacht"))
            else:
                self._mandate(mandate)
        self.rules_box = ui.element("div").style("margin-top:16px")
        self._rules(data["rules"])
        self._standards(data["profiles"])
        self._python()
        self._retention()

    # --- Freigaben und Vollmacht ----------------------------------------------------------------

    def _approvals(self) -> None:
        assert self.wf is not None
        approvals = self.wf.approvals
        chosen = {"decider": approvals.decider}
        card = section_card(
            "vollmacht",
            "Freigaben und Vollmacht",
            f'gespeichert in <span class="mono">{esc(self.workfolder_path)}/arr.yaml</span> '
            'unter <span class="mono">approvals</span>',
        )
        with card:
            raw(
                '<span class="label" style="font-weight:600">Wer entscheidet '
                "Berechtigungsanfragen?</span>",
                tag="div",
            )
            with (
                ui.element("div")
                .classes("radio-cards")
                .props('role="radiogroup" aria-label="Wer entscheidet Berechtigungsanfragen"')
                .style("margin:6px 0 12px")
            ):
                for decider, title, sub in DECIDERS:
                    with ui.element("label").classes("radio-card"):
                        radio(
                            "decider",
                            decider.value,
                            checked=decider is approvals.decider,
                            on_change=lambda v: chosen.__setitem__("decider", Decider(v)),
                        )
                        rec = (
                            ' <span class="tag">empfohlen</span>'
                            if decider is Decider.GATEKEEPER_THEN_USER
                            else ""
                        )
                        raw(
                            f"<span><b>{esc(title)}{rec}</b><span class='small muted'>{esc(sub)}"
                            "</span></span>",
                            tag="span",
                        )
            with ui.element("div").classes("grid-2e"):
                with ui.element("div"):
                    timeout = Field(
                        "Ablauffrist einer Anfrage",
                        name="approvals.timeout",
                        value=duration_text(approvals.timeout_s) or "20 min",
                        help_text="Danach gilt die Anfrage als abgelehnt, der Agent arbeitet "
                        "ohne weiter.",
                        width="120px",
                    )
                with ui.element("div"):
                    model = Field(
                        "Modell des Aufsichts-Agenten",
                        name="approvals.gatekeeper_model",
                        kind="select",
                        value=approvals.gatekeeper_model or GATEKEEPER_MODELS[0],
                        options=[(m, m) for m in GATEKEEPER_MODELS],
                        help_text="Er sieht nur die Anfrage und deine Vollmacht, keine "
                        "Gruppenchats.",
                    )
            raw(
                f'<div class="banner info lock" role="note">{icon("lock")}<div class="txt small">'
                "<b>Runs, die Claude Code startet, brauchen immer deine Freigabe hier in der "
                "GUI.</b>Das ist eine feste Grundregel von arr und lässt sich nicht abschalten."
                "</div></div>"
            )

            async def save() -> None:
                seconds = parse_duration(timeout.value)
                if not seconds or seconds < 60:
                    timeout.set_error("Frist: Gib eine Dauer an, z. B. 20 min.")
                    return
                timeout.set_error(None)
                assert self.wf is not None
                updated = self.wf.approvals.model_copy(
                    update={
                        "decider": chosen["decider"],
                        "timeout_s": seconds,
                        "gatekeeper_model": model.value,
                    }
                )
                await self._save_wf({"approvals": updated}, "Gespeichert: Freigaben")

            save_row(save, marker="save-approvals")

    def _mandate(self, mandate: MandateInfo) -> None:
        card = section_card(
            "vollmacht-text", "Deine Vollmacht", f'<span class="mono">{esc(mandate.path)}</span>'
        )
        with card:
            if mandate.problem:
                banner(
                    "err",
                    "Die gespeicherte Vollmacht ist ungültig.",
                    f" {mandate.problem} Bis du sie korrigierst, erreicht keine Anfrage den "
                    "Aufsichts-Agenten.",
                    small=True,
                )
            elif not mandate.exists:
                banner(
                    "info",
                    "Die Vollmacht ist noch nicht angelegt.",
                    f" Unten steht die Vorlage. „Speichern“ legt {mandate.path} an.",
                    small=True,
                )
            checked = ui.element("div").mark("mandate-checked")
            with checked:
                raw(checked_html(mandate))
            field = Field(
                "Vollmacht",
                name="mandate",
                kind="textarea",
                rows=18,
                value=mandate.text,
                help_text="Oben zwischen den Zeilen „---“ steht, was arr selbst prüft "
                "(„Vollmacht (von arr geprüft)“): Domains und pip install von PyPI. Lese-Ordner "
                "lassen sich nicht freigeben. Darunter der Klartext für den Aufsichts-Agenten.",
            )
            field.box.classes("mandate")
            raw(
                '<p class="small muted">Versionen und ein Vorher/Nachher-Vergleich kommen in '
                "einer späteren Version.</p>"
            )

            def example() -> None:
                field.set_value(mandate.example or mandate.text)

            def discard() -> None:
                field.set_value(mandate.text)
                field.set_error(None)

            async def checked_text() -> MandateInfo | None:
                """The checked part of the text in the field, or None (error at the field)."""
                if not field.value.strip():
                    field.set_error("Die Vollmacht darf nicht leer sein. Nimm das Beispiel.")
                    return None
                try:
                    info = await self.api.check_mandate(
                        mandate.name, MandateUpdate(text=field.value)
                    )
                except ApiException as exc:
                    field.set_error(exc.error.message)
                    return None
                field.set_error(None)
                checked.clear()
                with checked:
                    raw(checked_html(info))
                return info

            async def check() -> None:
                if await checked_text() is not None:
                    self.frame.toast("Geprüft: Die Vollmacht ist gültig.")

            async def save() -> None:
                if mandate.exists and field.value.strip() == mandate.text.strip():
                    field.set_error(None)
                    self.frame.toast("Nichts geändert.")
                    return
                if await checked_text() is None:
                    return

                async def confirm(_value: str) -> None:
                    try:
                        saved = await self.api.update_mandate(
                            mandate.name, MandateUpdate(text=field.value)
                        )
                    except ApiException as exc:
                        field.set_error(exc.error.message)
                        self.frame.toast(exc.error.message, error=True)
                        return
                    mandate.text = saved.text
                    mandate.exists = True
                    self.frame.toast("Gespeichert: Vollmacht. Sie gilt ab jetzt.")

                running = mandate.running_runs
                where = (
                    f"auch in {running} laufenden Runs"
                    if running != 1
                    else "auch in 1 laufenden Run"
                )
                self.dialog.show(
                    "Vollmacht ändern?",
                    [
                        f"Die neue Vollmacht gilt sofort für alle neuen Anfragen, {where}.",
                        "Schon getroffene Entscheidungen bleiben, wie sie sind.",
                        f"Die bisherige Fassung von {mandate.path} wird ersetzt.",
                    ],
                    "Vollmacht speichern",
                    confirm,
                    danger=False,
                )

            with ui.element("div").classes("save-row"):
                button("Beispiel einfügen", example, classes="btn btn-sm")
                button("Prüfen", check, classes="btn btn-sm").mark("check-mandate")
                raw('<span class="spacer"></span>', tag="span")
                button("Verwerfen", discard)
                button("Speichern …", save, classes="btn primary", key_hint="⌘↩").props(
                    "data-form-submit"
                ).mark("save-mandate")

    def _rules(self, rules: list[FixedRule]) -> None:
        box = self.rules_box
        box.clear()
        now = self.api.now()
        with box:
            card = section_card("regeln", "Feste Regeln", "gelten vor dem Aufsichts-Agenten")
            with card:
                with ui.element("table").classes("tbl compact"):
                    raw(
                        "<tr><th>Regel</th><th>Aktion</th><th>Herkunft</th><th></th></tr>",
                        tag="thead",
                    )
                    with ui.element("tbody"):
                        for rule in rules:
                            with ui.element("tr"):
                                raw(esc(rule.rule), tag="td")
                                raw(badge_html(EFFECT[rule.effect]), tag="td")
                                origin = rule.origin_text
                                raw(
                                    esc(origin),
                                    tag="td",
                                    classes="faint" if rule.origin == "builtin" else "",
                                )
                                with ui.element("td"):
                                    if rule.removable:
                                        button(
                                            "Entfernen",
                                            lambda r=rule: self._remove_rule(r),
                                            classes="btn btn-sm",
                                            title=f"Regel „{rule.rule}“ entfernen",
                                        ).mark("remove-rule")
                raw(
                    '<p class="small muted" style="margin:8px 0 0">Regeln, die du über die '
                    "Reichweite „immer“ angelegt hast, stehen hier mit Herkunft und lassen sich "
                    "entfernen. Die Standardregeln sind fest. Stand "
                    f"{esc(fmt.when(now, now))}.</p>"
                )

    async def _remove_rule(self, rule: FixedRule) -> None:
        try:
            rules = await self.api.delete_fixed_rule(rule.id)
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        self.frame.toast(f"Regel entfernt: {rule.rule}. Gleiche Anfragen kommen wieder zu dir.")
        self._rules(rules)

    # --- Standards, Python, Aufräumen ---------------------------------------------------------

    def _standards(self, profiles: list[ProfileInfo]) -> None:
        assert self.wf is not None
        wf = self.wf
        fields: dict[str, Field] = {}
        with ui.element("div").style("margin-top:16px"):
            card = section_card("standards", "Standards und Limits", "für neue Runs")
            with card, ui.element("div").classes("grid-2e"):
                for provider in (Provider.CLAUDE, Provider.CODEX):
                    defaults = wf.defaults.get(provider)
                    own = [p.name for p in profiles if p.provider is provider]
                    model = (defaults.model if defaults else "") or MODELS[provider][0]
                    effort = defaults.effort.value if defaults and defaults.effort else "high"
                    models = list(dict.fromkeys([*MODELS[provider], model]))
                    efforts = list(dict.fromkeys([*(e.value for e in EFFORTS[provider]), effort]))
                    with ui.element("div"):
                        raw(f"<h3>{PROVIDER_WORD[provider]}</h3>")
                        fields[f"{provider.value}.auth"] = Field(
                            "Profil",
                            name=f"defaults.{provider.value}.auth",
                            kind="select",
                            value=(defaults.auth if defaults else "") or "",
                            options=[(n, n) for n in own] or [("", "kein Profil")],
                        )
                        fields[f"{provider.value}.model"] = Field(
                            "Modell",
                            name=f"defaults.{provider.value}.model",
                            kind="select",
                            value=model,
                            options=[(m, m) for m in models],
                        )
                        fields[f"{provider.value}.effort"] = Field(
                            "Effort",
                            name=f"defaults.{provider.value}.effort",
                            kind="select",
                            value=effort,
                            options=[(e, e) for e in efforts],
                        )
            with card:
                raw("<h3>Limits pro Run</h3>")
                with ui.element("div").classes("grid-3"):
                    with ui.element("div"):
                        fields["wall"] = Field(
                            "Laufzeit",
                            name="limits.wall_time_s",
                            value=duration_text(wf.limits.wall_time_s),
                            width="110px",
                        )
                    with ui.element("div"):
                        fields["turns"] = Field(
                            "Turns pro Agent",
                            name="limits.turns_per_agent",
                            kind="number",
                            value=str(wf.limits.turns_per_agent or ""),
                            width="110px",
                        )
                    with ui.element("div"):
                        fields["budget"] = Field(
                            "Budget",
                            name="limits.budget_usd",
                            value=money_text(wf.limits.budget_usd),
                            help_text="Gilt auch bei Abo-Profilen über den rechnerischen "
                            "Preis (≈).",
                            width="110px",
                        )
                raw(
                    '<p class="small muted">Parallelität pro Profil stellst du unter '
                    '<a href="/profile">Auth-Profile</a> ein.</p>'
                )

                shown = {key: field.value for key, field in fields.items()}

                async def save() -> None:
                    wall = parse_duration(fields["wall"].value)
                    budget = parse_money(fields["budget"].value)
                    try:
                        turns = int(fields["turns"].value)
                    except ValueError:
                        turns = 0
                    ok = True
                    for key, valid, message in (
                        ("wall", wall, "Gib eine Dauer an, z. B. 2 h."),
                        ("budget", budget, "Gib einen Betrag an, z. B. 20 $."),
                        ("turns", turns > 0, "Gib eine ganze Zahl an, z. B. 40."),
                    ):
                        fields[key].set_error(None if valid else message)
                        ok = ok and bool(valid)
                    if not ok:
                        return
                    assert self.wf is not None
                    defaults = dict(self.wf.defaults)
                    for provider in (Provider.CLAUDE, Provider.CODEX):
                        keys = [f"{provider.value}.{n}" for n in ("auth", "model", "effort")]
                        changed = any(fields[k].value != shown[k] for k in keys)
                        old = defaults.get(provider)
                        if old is None and not changed:
                            continue  # what the fields show for a provider without standards
                        auth, model, effort = (fields[k].value for k in keys)
                        defaults[provider] = ProviderDefaultsInfo(
                            auth=auth or None, model=model, effort=Effort(effort)
                        )
                    limits = self.wf.limits.model_copy(
                        update={"wall_time_s": wall, "turns_per_agent": turns, "budget_usd": budget}
                    )
                    await self._save_wf(
                        {"defaults": defaults, "limits": limits},
                        "Gespeichert: Standards und Limits",
                    )

                save_row(save)

    def _python_notice(self) -> str:
        """The message of `toolchain_notice` (§8.4), empty when the environment is fine."""
        assert self.wf is not None
        notice = self.wf.toolchain_notice
        if not notice:
            return ""
        return (
            f'<div class="banner warn" role="status">{icon("warning")}<div class="txt">'
            f"<b>{esc(notice.title)}</b> {esc(notice.text)}</div></div>"
        )

    def _python(self) -> None:
        assert self.wf is not None
        toolchain = self.wf.toolchain
        candidates = [c.path for c in self.wf.toolchain_candidates]
        current = toolchain.python or SYSTEM_PYTHON
        for option in (current, SYSTEM_PYTHON):
            if option not in candidates:
                candidates.append(option)
        with ui.element("div").style("margin-top:16px"):
            card = section_card("python", "Python-Umgebung der Agenten")
            with card:
                self.python_notice = raw(self._python_notice(), tag="div")
                with ui.element("div").classes("grid-2e"):
                    with ui.element("div"):
                        env = Field(
                            "Umgebung",
                            name="toolchain.python",
                            kind="select",
                            value=current,
                            options=[(c, c) for c in candidates],
                            help_text="Die Basis-venv, in der Agenten Python ausführen.",
                        )
                    with ui.element("div"):
                        venv = Field(
                            "Installationen",
                            name="toolchain.run_venv",
                            kind="select",
                            value=toolchain.run_venv.value,
                            options=[
                                (
                                    RunVenvMode.OVERLAY.value,
                                    "pro Run eine eigene venv über der Basis",
                                ),
                                (RunVenvMode.OFF.value, "keine Installationen"),
                            ],
                            help_text="Deine Basis-venv bleibt unverändert.",
                        )
                path = Field(
                    "Zusätzlich im PATH",
                    name="toolchain.extra_path",
                    value=", ".join(toolchain.extra_path),
                    optional="optional, mit Komma getrennt",
                    help_text="z. B. /Library/TeX/texbin für LaTeX",
                )
                result = raw("", tag="div").props('aria-live="polite"')

                async def test() -> None:
                    result.set_content('<p class="small muted">Testen …</p>')
                    try:
                        outcome = await self.api.test_toolchain(env.value)
                    except ApiException as exc:
                        result.set_content(f'<p class="small">{esc(exc.error.message)}</p>')
                        return
                    detail = f"{esc(outcome.detail)} " if outcome.detail else ""
                    packages = " · ".join(
                        f"{esc(name)} {'✓' if ok else '✗'}" for name, ok in outcome.packages.items()
                    )
                    css = "ok" if outcome.ok else "warn"
                    result.set_content(
                        f'<div class="check"><span class="st {css}" aria-hidden="true">'
                        f'{"✓" if outcome.ok else "!"}</span><span class="txt"><b>'
                        f"{esc(outcome.message)}</b><small>{detail}Pakete: {packages}</small>"
                        "</span></div>"
                    )

                async def save() -> None:
                    assert self.wf is not None
                    extra = [p.strip() for p in path.value.split(",") if p.strip()]
                    updated = self.wf.toolchain.model_copy(
                        update={
                            "python": None if env.value == SYSTEM_PYTHON else env.value,
                            "run_venv": RunVenvMode(venv.value),
                            "extra_path": extra,
                        }
                    )
                    await self._save_wf({"toolchain": updated}, "Gespeichert: Python-Umgebung")
                    self.python_notice.set_content(self._python_notice())

                with ui.element("div").classes("save-row"):
                    button("Testen", test, classes="btn")
                    raw('<span class="spacer"></span>', tag="span")
                    button("Speichern", save, classes="btn primary", key_hint="⌘↩").props(
                        "data-form-submit"
                    )

    def _retention(self) -> None:
        assert self.wf is not None
        r = self.wf.retention
        with ui.element("div").style("margin-top:16px"):
            card = section_card("aufraeumen", "Aufräumregeln")
            with card:
                with ui.element("div").classes("grid-3"):
                    with ui.element("div"):
                        slim = Field(
                            "Verschlanken nach",
                            name="retention.slim",
                            value=f"{r.slim_after_days} Tagen" if r.slim_after_days else "",
                            help_text="Arbeitskopien fertiger Runs löschen",
                            width="120px",
                        )
                    with ui.element("div"):
                        archive = Field(
                            "Archivieren nach",
                            name="retention.archive",
                            value=f"{r.archive_after_days} Tagen" if r.archive_after_days else "",
                            help_text="als .tar.zst nach runs/_archive/",
                            width="120px",
                        )
                    with ui.element("div"):
                        warn = Field(
                            "Warnen ab",
                            name="retention.warn",
                            value=f"{fmt.threshold(r.max_runs_size_gb)} GB"
                            if r.max_runs_size_gb
                            else "",
                            help_text="Belegung aller Runs",
                            width="120px",
                        )
                raw(
                    '<p class="small muted">Angeheftete Runs sind ausgenommen. Gelöscht wird nie '
                    "automatisch.</p>"
                )

                async def save() -> None:
                    days_slim = parse_days(slim.value)
                    days_archive = parse_days(archive.value)
                    m = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*(gb)?\s*$", warn.value, re.I)
                    size = float(m.group(1).replace(",", ".")) if m else None
                    checks = [
                        (slim, days_slim, "Gib eine Zahl von Tagen an, z. B. 14 Tagen."),
                        (archive, days_archive, "Gib eine Zahl von Tagen an, z. B. 90 Tagen."),
                        (warn, size, "Gib eine Größe an, z. B. 50 GB."),
                    ]
                    for field, value, message in checks:
                        field.set_error(None if value else message)
                    if not all(v for _, v, _ in checks):
                        return
                    assert self.wf is not None
                    updated = self.wf.retention.model_copy(
                        update={
                            "slim_after_days": days_slim,
                            "archive_after_days": days_archive,
                            "max_runs_size_gb": size,
                        }
                    )
                    await self._save_wf({"retention": updated}, "Gespeichert: Aufräumregeln")

                save_row(save)

    async def _save_wf(self, update: dict[str, Any], message: str) -> None:
        assert self.wf is not None
        try:
            self.wf = await self.api.update_workfolder_settings(self.wf.model_copy(update=update))
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        running = self.wf.running_runs
        tail = (
            f" Gilt für neue Runs; {running} laufende Runs behalten ihre Werte." if running else ""
        )
        self.frame.toast(message + "." + tail)

    # --- app settings ---------------------------------------------------------------------------

    def _render_app(self, settings: AppSettings) -> None:
        self.app = settings
        self._general(settings)
        self._notifications(settings)
        self._appearance(settings)

    def _general(self, s: AppSettings) -> None:
        chosen = {"open_in": s.open_in}
        card = section_card("allgemein", "Allgemein", "gilt für arr auf diesem Mac")
        with card:
            raw('<span class="label" style="font-weight:600">Öffnen in</span>', tag="div")
            with (
                ui.element("div")
                .classes("seg")
                .props('role="radiogroup" aria-label="Öffnen in"')
                .style("margin:6px 0 12px")
            ):
                for value, label in (
                    (OpenIn.BROWSER, "Browser-Tab (Standard)"),
                    (OpenIn.WINDOW, "eigenem Fenster"),
                ):
                    with ui.element("label"):
                        radio(
                            "open_in",
                            value.value,
                            checked=s.open_in is value,
                            on_change=lambda v: chosen.__setitem__("open_in", OpenIn(v)),
                        )
                        raw(esc(label), tag="span")
            port = Field(
                "Port",
                name="app.port",
                kind="number",
                value=str(s.port),
                help_text="Deep-Links aus Claude Code nutzen diesen Port. Ändern lässt er sich "
                "im Betrieb nicht.",
                width="110px",
                attrs='min="1024" max="65535" readonly',
            )
            folders = " · ".join(
                f'{esc(w.name)} <span class="mono faint">{esc(w.path)}</span>'
                for w in s.workfolders
            )
            raw(
                '<dl class="kv small" style="margin:4px 0 0"><dt>Workfolder</dt>'
                f'<dd>{folders} · <a href="/?workfolder=neu">＋ anlegen</a></dd>'
                "<dt>Start</dt><dd>Eigenes Fenster, Dock-Symbol, Start beim Anmelden und nach "
                "einem Absturz kommen in einer späteren Version</dd></dl>"
            )

            async def save() -> None:
                try:
                    number = int(port.value)
                except ValueError:
                    number = 0
                if not 1024 <= number <= 65535:
                    port.set_error("Port: Gib eine Zahl von 1024 bis 65535 an, z. B. 8765.")
                    return
                port.set_error(None)
                await self._save_app({"open_in": chosen["open_in"], "port": number})

            save_row(save)

    def _notifications(self, s: AppSettings) -> None:
        state = {e: s.notifications.get(e, True) for e, _ in NOTIFICATIONS}
        state[NotificationEvent.APPROVAL] = True
        with ui.element("div").style("margin-top:16px"):
            card = section_card("mitteilungen", "Mitteilungen auf dem Mac")
            with card:
                with ui.element("div").classes("stack small"):
                    for event, label in NOTIFICATIONS:
                        always = event is NotificationEvent.APPROVAL
                        later = event in NOTIFICATIONS_LATER
                        note = " · immer an" if always else ""
                        note = " · Mitteilungen dazu kommen später." if later else note
                        with ui.element("div"):
                            checkbox(
                                label + note,
                                checked=state[event],
                                on_change=lambda v, e=event: state.__setitem__(e, bool(v)),
                                attrs='disabled aria-disabled="true"' if always or later else "",
                            )
                raw(
                    '<p class="small muted" style="margin-top:8px">Gilt für arr auf diesem Mac. '
                    "Neue Freigaben melden sich immer.</p>"
                )

                async def save() -> None:
                    await self._save_app({"notifications": dict(state)})

                save_row(save, marker="save-notifications")

    def _appearance(self, s: AppSettings) -> None:
        with ui.element("div").style("margin-top:16px"):
            card = section_card("darstellung", "Darstellung")
            with card:
                with (
                    ui.element("div")
                    .classes("seg")
                    .props('role="radiogroup" aria-label="Darstellung"')
                ):
                    for value, label in (
                        (Appearance.SYSTEM, "wie macOS"),
                        (Appearance.LIGHT, "hell"),
                        (Appearance.DARK, "dunkel"),
                    ):
                        with ui.element("label"):
                            radio(
                                "appearance",
                                value.value,
                                checked=s.appearance is value,
                                on_change=self._set_appearance,
                            )
                            raw(esc(label), tag="span")
                raw(
                    '<p class="small muted" style="margin-top:8px">Gilt sofort. '
                    "Einzeltasten-Kürzel an/aus kommt in einer späteren Version.</p>"
                )

    async def _set_appearance(self, value: str) -> None:
        theme = {"light": "light", "dark": "dark"}.get(value, "")
        self.frame.js(
            "document.documentElement."
            + (
                f"setAttribute('data-theme', '{theme}')"
                if theme
                else "removeAttribute('data-theme')"
            )
        )
        await self._save_app({"appearance": Appearance(value)}, quiet=True)

    async def _save_app(self, update: dict[str, Any], *, quiet: bool = False) -> None:
        assert self.app is not None
        try:
            self.app = await self.api.update_app_settings(self.app.model_copy(update=update))
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        if not quiet:
            self.frame.toast("Gespeichert.")


def register() -> None:
    """Register `/einstellungen` (docs/08 §5.11, deep link `#vollmacht`)."""

    @ui.page("/einstellungen")
    async def einstellungen(zustand: str = "") -> None:
        api = state_api(get_api(), zustand, "einstellungen")
        use_screen_assets()
        frame = Frame(api, active="einstellungen", title="Einstellungen")
        await frame.start()
        await EinstellungenPage(frame, api).build()

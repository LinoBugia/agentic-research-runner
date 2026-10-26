"""Einrichtung: from the first start to the first run in ≤ 10 min, without terminal (docs/08
§5.12, R1, T10, E18).

Five steps without navigation: 1 Workfolder, 2 Zugänge („Mit Claude anmelden“: arr runs the
login in the browser and keeps the token in the keychain; never a pasted token), 3 Python-
Umgebung, 4 Projekt, 5 Los geht's (first run prefilled, or working with Claude Code). Every
step has a question as heading, one sentence of explanation, a recommended option and
„Später“. `?schritt=1…5` opens a step; in the mock `?zustand=laedt|fehler` shows step 2 with
a waiting or failed login.
"""

from collections.abc import Callable
from typing import Any

from nicegui import ui
from pydantic import SecretStr

from arr.models import AuthKind
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    CreateProfileRequest,
    CreateProjectRequest,
    LoginState,
    ProfileInfo,
    SetupState,
    WorkfolderRequest,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.base import button, esc, icon, raw
from arr.service.gui.components.copy_field import copy_html
from arr.service.gui.components.form import Field, radio
from arr.service.gui.components.page_state import state_api, use_screen_assets
from arr.service.gui.components.toast import Toasts

STEPS = ["Workfolder", "Zugänge", "Python-Umgebung", "Projekt", "Los geht's"]
EXAMPLE = "collatz"
SAMPLE_SENTENCE = (
    "Lass eine Research-Group in collatz Ideen zu Stoppzeiten sammeln, höchstens 20 Minuten "
    "und 3 $."
)


class EinrichtungPage:
    """State of the setup assistant (one browser tab)."""

    def __init__(self, api: ApiClient, *, step: int, zustand: str) -> None:
        self.api = api
        self.zustand = zustand
        self.step = step
        self.state: SetupState | None = None
        self.workfolder_path = "~/Mathe"
        self.project = EXAMPLE
        self.python = ""
        self.login_timer: ui.timer | None = None
        self.login_boxes: dict[str, ui.element] = {}

    async def build(self) -> None:
        with ui.element("div").classes("setup") as self.root:
            raw(
                '<b style="font-size:18px">arr</b><span class="faint">· Einrichtung, etwa '
                '8 Minuten</span><span class="spacer"></span><a class="small" href="/">'
                "Später einrichten</a>",
                tag="div",
                classes="setup-top",
            )
            self.steps = raw("", tag="ol", classes="steps").props('aria-label="Fortschritt"')
            self.body = ui.element("main").props('id="inhalt"')
        self.toasts = Toasts()
        try:
            self.state = await self.api.get_setup_state()
        except ApiException as exc:
            with self.body:
                raw(f"<p>{esc(exc.error.message)}</p>")
            return
        if self.state.workfolder is not None:
            self.workfolder_path = self.state.workfolder.path
        await self._show(self.step)

    async def _show(self, step: int) -> None:
        self.step = max(1, min(5, step))
        if self.login_timer is not None:
            self.login_timer.cancel()
            self.login_timer = None
        items = []
        for n, name in enumerate(STEPS, start=1):
            if n < self.step:
                items.append(f'<li class="done">✓ {n} · {esc(name)}</li>')
            elif n == self.step:
                items.append(f'<li class="now" aria-current="step">{n} · {esc(name)}</li>')
            else:
                items.append(f"<li>{n} · {esc(name)}</li>")
        self.steps.set_content("".join(items))
        self.body.clear()
        with self.body:
            await [
                self._workfolder,
                self._access,
                self._python,
                self._project,
                self._go,
            ][self.step - 1]()
        ui.run_javascript(
            f"history.replaceState(null, '', '/einrichtung?schritt={self.step}');"
            "arr.focusHeading('#inhalt h1')"
        )

    def _nav(
        self,
        *,
        back: bool = True,
        next_label: str | None = None,
        hint: str = "",
        primary: tuple[str, Callable[[], Any], str] | None = None,
    ) -> None:
        with ui.element("div").classes("setup-nav"):
            if back and self.step > 1:
                button("← Zurück", lambda: self._show(self.step - 1))
            raw("", tag="span", classes="spacer")
            if hint:
                raw(esc(hint), tag="span", classes="small muted")
            button("Später", lambda: self._show(self.step + 1), classes="btn ghost")
            if next_label:
                button(next_label, lambda: self._show(self.step + 1), classes="btn primary")
            if primary is not None:
                label, action, marker = primary
                button(label, action, classes="btn primary").mark(marker)

    def _toast(self, message: str, *, error: bool = False) -> None:
        self.toasts.show(message, error=error)

    # --- 1 Workfolder ----------

    async def _workfolder(self) -> None:
        raw(
            "<h1 tabindex='-1'>Wo sollen Projekte, Skills und Runs liegen?</h1>"
            '<p class="muted">Der Workfolder ist ein normaler Ordner. arr legt darin '
            "<span class='mono'>projects/</span>, <span class='mono'>skills/</span> und "
            "<span class='mono'>runs/</span> an.</p>"
        )
        free = self.state.workfolder.disk_free_bytes if self.state and self.state.workfolder else 0
        with ui.element("div").classes("card").style("margin-top:16px"):
            path = Field(
                "Ordner",
                name="setup-workfolder",
                value=self.workfolder_path,
                help_text=f"Vorschlag. Festplatte: {fmt.size(free)} frei.",
                width="360px",
            )
            result = raw("", tag="div").props('aria-live="polite"')
            with ui.element("div").classes("btn-row"):

                async def choose(create: bool) -> None:
                    try:
                        info = await self.api.select_workfolder(
                            WorkfolderRequest(path=path.value.strip(), create=create)
                        )
                    except ApiException as exc:
                        path.set_error(exc.error.message)
                        return
                    path.set_error(None)
                    self.workfolder_path = info.path
                    apfs = (
                        "✓ APFS: Agent-Kopien kosten kaum Platz."
                        if info.apfs
                        else "⚠ Kein APFS: Kopien belegen vollen Platz. Das ist kein Hindernis."
                    )
                    result.set_content(
                        '<div class="check"><span class="st ok" aria-hidden="true">✓</span><span '
                        f'class="txt"><b>Workfolder {esc(info.path)} ist bereit.</b><small>'
                        f"{esc(apfs)} {esc(fmt.size(info.disk_free_bytes))} frei.</small></span>"
                        "</div>"
                    )
                    await _later(self._show, 2)

                button("Anlegen", lambda: choose(True), classes="btn primary").mark("setup-create")
                button("Vorhandenen öffnen", lambda: choose(False))
        self._nav(back=False)

    # --- 2 Zugänge ----------

    async def _access(self) -> None:
        profiles = await self.api.list_profiles() if self.zustand not in ("laedt", "fehler") else []
        raw(
            "<h1 tabindex='-1'>Mit welchen KI-Zugängen sollen die Agenten arbeiten?</h1>"
            '<p class="muted">Du brauchst mindestens einen. Für gemischte Research-Groups (Claude '
            "und ChatGPT im selben Run) richtest du beide ein. Die Zugangsdaten landen im "
            "macOS-Schlüsselbund.</p>"
        )
        with ui.element("div").classes("grid-3").style("margin-top:16px"):
            self._login_card(profiles, AuthKind.CLAUDE_OAUTH, "Claude-Abo", "max-privat", "")
            self._login_card(
                profiles, AuthKind.CODEX_CHATGPT, "ChatGPT-Abo", "chatgpt-privat", "empfohlen"
            )
            self._key_card(profiles)
        raw(
            f'<div class="banner info" style="margin-top:16px">{icon("info")}<div class="txt '
            'small">Abo-Logins sind nur für persönliche Nutzung gedacht. Für Firmen- oder '
            "Uni-Accounts nimm einen API-Key.</div></div>"
        )
        ok = any(p.last_test and p.last_test.ok for p in profiles)
        hint = "" if ok else "Melde dich zuerst bei einem Zugang an."
        codex = any(p.kind is AuthKind.CODEX_CHATGPT for p in profiles)
        if ok and not codex:
            hint = "ChatGPT kannst du später unter Auth-Profile einrichten."
        self._nav(next_label="Weiter: Python-Umgebung →" if ok else None, hint=hint)
        if self.zustand == "laedt":
            box = self.login_boxes["max-privat"]
            await self._start_login("max-privat", AuthKind.CLAUDE_OAUTH, box, wait_only=True)
        elif self.zustand == "fehler":
            self._login_failed(
                self.login_boxes["max-privat"],
                "Die Claude-Anmeldung wurde abgebrochen. Versuche es erneut oder nimm einen "
                "Claude-API-Key.",
                "max-privat",
                AuthKind.CLAUDE_OAUTH,
            )

    def _login_card(
        self, profiles: list[ProfileInfo], kind: AuthKind, title: str, default: str, note: str
    ) -> None:
        existing = next((p for p in profiles if p.kind is kind), None)
        connected = (
            existing is not None
            and existing.secret_present
            and bool(existing.last_test and existing.last_test.ok)
        )
        card = ui.element("div").classes("card ok" if connected else "card")
        with card:
            status = (
                '<span class="badge st-ok" style="margin-left:auto">✓ verbunden</span>'
                if connected
                else f'<span class="faint small" style="margin-left:auto">{esc(note)}</span>'
            )
            raw(f'<div class="card-head"><b>{esc(title)}</b>{status}</div>')
            if connected and existing is not None:
                models = ", ".join(existing.last_test.models) if existing.last_test else ""
                raw(
                    f'<p class="small">Profil <b class="mono">{esc(existing.name)}</b> · Zugriff '
                    f"auf {esc(models)}.</p>"
                )
                return
            service = "Claude" if kind is AuthKind.CLAUDE_OAUTH else "ChatGPT"
            raw(
                f'<p class="small">Die Anmeldung öffnet den Browser. arr übernimmt das Token und '
                f"testet es. Für {service}-Agenten.</p>"
            )
            name = Field(
                "Profilname",
                name=f"setup-{default}",
                value=existing.name if existing else default,
                attrs='autocomplete="off"',
            )
            box = ui.element("div")
            self.login_boxes[default] = box
            with box:
                button(
                    f"Mit {service} anmelden",
                    lambda: self._create_and_login(name, kind, box),
                    classes="btn btn-sm primary",
                ).mark(f"login-{kind.value}")

    def _key_card(self, profiles: list[ProfileInfo]) -> None:
        existing = next((p for p in profiles if p.kind is AuthKind.CLAUDE_API_KEY), None)
        with ui.element("div").classes("card"):
            raw(
                '<div class="card-head"><b>Claude-API-Key</b><span class="faint small" '
                'style="margin-left:auto">optional</span></div><p class="small">Abrechnung pro '
                "Token. Sinnvoll für Uni- oder Firmen-Accounts.</p>"
            )
            if existing is not None:
                state = (
                    "funktioniert"
                    if existing.last_test and existing.last_test.ok
                    else "Test fehlgeschlagen"
                )
                raw(
                    f'<p class="small">Profil <b class="mono">{esc(existing.name)}</b> · '
                    f'{esc(state)} · <a href="/profile/{esc(existing.name)}">ansehen</a></p>'
                )
                return
            key = Field(
                "API-Key",
                name="setup-key",
                kind="password",
                help_text="Aus der Anthropic Console. arr zeigt ihn nie wieder an.",
                attrs='autocomplete="off"',
            )

            async def save() -> None:
                if not key.value.strip():
                    key.set_error("Füge den API-Key ein.")
                    return
                try:
                    await self.api.create_profile(
                        CreateProfileRequest(
                            name="claude-api",
                            kind=AuthKind.CLAUDE_API_KEY,
                            concurrency=2,
                            api_key=SecretStr(key.value.strip()),
                        )
                    )
                except ApiException as exc:
                    key.set_error(exc.error.message)
                    return
                self._toast("✓ API-Key im Schlüsselbund gespeichert und getestet.")
                await self._show(2)

            button("Key speichern und testen", save, classes="btn btn-sm")

    async def _create_and_login(self, name: Field, kind: AuthKind, box: ui.element) -> None:
        profile_name = name.value.strip()
        try:
            await self.api.create_profile(
                CreateProfileRequest(name=profile_name, kind=kind, concurrency=4)
            )
        except ApiException as exc:
            if exc.error.code.value != "conflict":
                name.set_error(exc.error.message)
                return
        except ValueError:
            name.set_error("Nimm Kleinbuchstaben, Ziffern und Bindestriche.")
            return
        name.set_error(None)
        await self._start_login(profile_name, kind, box)

    async def _start_login(
        self, name: str, kind: AuthKind, box: ui.element, *, wait_only: bool = False
    ) -> None:
        note = "arr führt dafür claude setup-token aus." if kind is AuthKind.CLAUDE_OAUTH else ""
        if not wait_only:
            try:
                login = await self.api.start_login(name)
                note = login.note or note
            except ApiException as exc:
                self._login_failed(box, exc.error.message, name, kind)
                return
        box.clear()
        with box, ui.element("div").classes("login-wait").props('role="status"'):
            raw(
                f"{icon('hourglass_top')}<span><b>Warte auf die Anmeldung im Browser …</b>"
                f'<span class="small muted" style="display:block">{esc(note)}</span></span>',
                tag="span",
            )
            button("Abbrechen", lambda: self._cancel(name, kind, box), classes="btn btn-sm")
        if wait_only:
            return

        async def poll() -> None:
            status = await self.api.get_login_status(name)
            if status.state is LoginState.WAITING:
                return
            if self.login_timer is not None:
                self.login_timer.cancel()
                self.login_timer = None
            if status.state is LoginState.DONE:
                self._toast(f"✓ {name} angemeldet und getestet.")
                await self._show(2)
                self._after_login_note(kind)
                return
            self._login_failed(box, status.message or "", name, kind)

        with box:
            self.login_timer = ui.timer(1.0, poll)

    def _after_login_note(self, kind: AuthKind) -> None:
        """„So lief die Anmeldung“ (wireframe): what arr did, for transparency (G4)."""
        if kind is not AuthKind.CLAUDE_OAUTH:
            return
        with self.body:
            raw(
                '<h3 style="margin-top:24px">So lief die Anmeldung beim Claude-Abo</h3>'
                '<div class="card tight small">'
                '<div class="check"><span class="st ok">✓</span><span class="txt">arr hat die '
                "Claude-Anmeldung im Browser geöffnet<small>arr führt dafür "
                "<span class='mono'>claude setup-token</span> selbst aus.</small></span></div>"
                '<div class="check"><span class="st ok">✓</span><span class="txt">Token im '
                "Schlüsselbund gespeichert</span></div>"
                '<div class="check"><span class="st ok">✓</span><span class="txt">Test '
                "erfolgreich</span></div></div>"
            )

    async def _cancel(self, name: str, kind: AuthKind, box: ui.element) -> None:
        if self.login_timer is not None:
            self.login_timer.cancel()
            self.login_timer = None
        status = await self.api.cancel_login(name)
        self._login_failed(box, status.message or "", name, kind)

    def _login_failed(self, box: ui.element, message: str, name: str, kind: AuthKind) -> None:
        cause, _, rest = (message or "Die Anmeldung hat nicht geklappt.").partition(". ")
        box.clear()
        with box:
            raw(
                f'<div class="banner err banner-sm" role="alert">{icon("error")}<div class="txt">'
                f"<b>{esc(cause.rstrip('.'))}.</b>{esc(rest)}</div></div>"
            )
            with ui.element("div").classes("btn-row").style("margin-top:8px"):
                button(
                    "Erneut versuchen",
                    lambda: self._start_login(name, kind, box),
                    classes="btn btn-sm primary",
                )
                if kind is AuthKind.CLAUDE_OAUTH:
                    raw(
                        '<span class="small muted">oder rechts einen Claude-API-Key nehmen</span>',
                        tag="span",
                    )

    # --- 3 Python ----------

    async def _python(self) -> None:
        assert self.state is not None
        candidates = self.state.toolchain_candidates
        if not self.python and candidates:
            self.python = candidates[0].path
        raw(
            "<h1 tabindex='-1'>In welcher Python-Umgebung rechnen die Agenten?</h1>"
            '<p class="muted">arr hat diese Umgebungen gefunden. Agenten installieren Pakete in '
            "eine eigene venv pro Run, deine bleibt unverändert.</p>"
        )
        result = None
        with (
            ui.element("div")
            .classes("radio-cards")
            .style("margin-top:16px")
            .props('role="radiogroup" aria-label="Python-Umgebung"')
        ):
            for c in candidates:
                packages = " ".join(
                    f"{esc(name)} {'✓' if ok else '✗'}" for name, ok in c.packages.items()
                )
                rec = ' <span class="tag">empfohlen</span>' if c is candidates[0] else ""
                with ui.element("label").classes("radio-card"):
                    radio(
                        "python",
                        c.path,
                        checked=c.path == self.python,
                        on_change=lambda v: setattr(self, "python", v),
                    )
                    raw(
                        f"<span><b class='mono'>{esc(c.path)}</b>{rec}<span class='small muted'>"
                        f"Python {esc(c.python_version)} · {packages}</span></span>",
                        tag="span",
                    )
        result = raw("", tag="div").props('aria-live="polite"').style("margin-top:8px")

        async def test() -> None:
            outcome = await self.api.test_toolchain(self.python)
            css = "ok" if outcome.ok else "warn"
            note = "" if outcome.ok else " Das ist kein Hindernis."
            result.set_content(
                f'<div class="check"><span class="st {css}" aria-hidden="true">'
                f'{"✓" if outcome.ok else "!"}</span><span class="txt">{esc(outcome.message)}'
                f"{esc(note)}</span></div>"
            )

        with ui.element("div").classes("btn-row").style("margin-top:8px"):
            button("Testen", test, classes="btn btn-sm")
        self._nav(next_label="Weiter: Projekt →")

    # --- 4 Projekt ----------

    async def _project(self) -> None:
        assert self.state is not None
        choice = {"value": "beispiel"}
        raw(
            "<h1 tabindex='-1'>Woran willst du arbeiten?</h1>"
            '<p class="muted">Ein Projekt ist ein Ordner mit deinem Mathe-Material. arr legt nur '
            "seine Einrichtungsdateien an (CLAUDE.md-Block, .mcp.json, Skill-Links, "
            "project.yaml).</p>"
        )
        has_example = EXAMPLE in self.state.projects
        with (
            ui.element("div")
            .classes("radio-cards")
            .style("margin-top:16px")
            .props('role="radiogroup" aria-label="Projekt"')
        ):
            options = [
                (
                    "beispiel",
                    "Beispielprojekt collatz",
                    "empfohlen",
                    "Mit Notizen und einer Beispielaufgabe, gut für den ersten Run."
                    + (" Gibt es schon in diesem Workfolder." if has_example else ""),
                ),
                ("neu", "Neues Projekt", "", "Leerer Ordner mit notes/, tex/, code/, results/."),
                (
                    "ordner",
                    "Vorhandenen Ordner einrichten",
                    "",
                    "arr ergänzt nur, was fehlt, und ändert keine deiner Dateien.",
                ),
            ]
            for value, title, tag, sub in options:
                with ui.element("label").classes("radio-card"):
                    radio(
                        "projekt",
                        value,
                        checked=value == "beispiel",
                        on_change=lambda v: choice.__setitem__("value", v),
                    )
                    tag_html = f' <span class="tag">{esc(tag)}</span>' if tag else ""
                    raw(
                        f"<span><b>{esc(title)}</b>{tag_html}<span class='small muted'>{esc(sub)}"
                        "</span></span>",
                        tag="span",
                    )
        name = Field(
            "Ordnername oder Pfad",
            name="setup-project",
            value="",
            optional="für „Neues Projekt“ und „Vorhandenen Ordner“",
            help_text=f"Neues Projekt: z. B. goldbach-schwach (wird zu {self.workfolder_path}/"
            "projects/…). Vorhandener Ordner: voller Pfad. arr legt an: CLAUDE.md (mit "
            "arr-Block), AGENTS.md, .mcp.json, project.yaml und 7 Skill-Links, beim Beispiel "
            "zusätzlich notes/ und tex/.",
        )
        name.box.style("margin-top:12px")

        async def create() -> None:
            kind = choice["value"]
            if kind == "beispiel":
                if has_example:
                    self.project = EXAMPLE
                    await self._show(5)
                    return
                request = CreateProjectRequest(
                    name=EXAMPLE, title="Collatz-Varianten", template="collatz-beispiel"
                )
            else:
                value = name.value.strip().rstrip("/")
                slug = value.rsplit("/", 1)[-1].lower()
                if not value:
                    name.set_error("Gib einen Ordnernamen an.")
                    return
                try:
                    request = CreateProjectRequest(
                        name=slug, existing_path=value if kind == "ordner" else None
                    )
                except ValueError:
                    name.set_error("Nimm Kleinbuchstaben, Ziffern und Bindestriche.")
                    return
            try:
                result = await self.api.create_project(request)
            except ApiException as exc:
                name.set_error(exc.error.message)
                return
            self.project = result.project.name
            self._toast(result.message)
            await self._show(5)

        # the primary action sits in the step's button row, so it fits 1280 × 680 (G10)
        self._nav(primary=("Projekt anlegen", create, "setup-project"))

    # --- 5 Los geht's ----------

    async def _go(self) -> None:
        path = f"{self.workfolder_path}/projects/{self.project}"
        raw(
            "<h1 tabindex='-1'>Los geht's</h1>"
            '<p class="muted">arr ist eingerichtet. Du kannst gleich einen kleinen Run starten '
            "oder in Claude Code weiterarbeiten.</p>"
        )
        raw(
            '<div class="grid-2e" style="margin-top:16px">'
            '<div class="card"><h2>Ersten Run starten</h2><p class="small">Exploration im '
            f"Projekt {esc(self.project)} mit einer Beispielaufgabe, höchstens 20 min und 3 $. "
            "Die Seite ist vorausgefüllt, du prüfst und startest.</p>"
            f'<a class="btn primary" href="/neuer-run?skill=exploration&amp;projekt='
            f'{esc(self.project)}&amp;beispiel=1">Ersten Run starten</a></div>'
            '<div class="card"><h2>Mit Claude Code arbeiten</h2><p class="small">Öffne diesen '
            "Ordner in Claude Code. Claude Code kennt dann die Group-Skills und startet Runs; "
            f"jeder Start braucht deine Freigabe hier.</p>{copy_html(path)}"
            '<p class="small" style="margin-top:8px">Zum Beispiel:</p>'
            f'<p class="quote small">{esc(SAMPLE_SENTENCE)}</p>'
            f"{copy_html(SAMPLE_SENTENCE, label='Beispielsatz kopieren', shown='Satz kopieren')}"
            "</div></div>"
        )
        with ui.element("div").classes("setup-nav"):
            button("← Zurück", lambda: self._show(4))
            raw('<span class="spacer"></span>', tag="span")
            raw('<a class="btn" href="/">Zur Startseite</a>', tag="span")


async def _later(show: Any, step: int) -> None:
    """Move on after the user saw the success line for a moment."""
    ui.timer(1.2, lambda: show(step), once=True)


def register() -> None:
    """Register `/einrichtung` (docs/08 §5.12; no navigation)."""

    @ui.page("/einrichtung")
    async def einrichtung(schritt: int = 0, zustand: str = "") -> None:
        api = state_api(get_api(), zustand if zustand in ("leer",) else "")
        use_screen_assets()
        ui.page_title("Einrichtung – arr")
        step = schritt or (2 if zustand in ("laedt", "fehler") else 1)
        page = EinrichtungPage(api, step=step, zustand=zustand)
        await page.build()

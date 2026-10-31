"""Auth-Profile: add, test and limit access without ever showing a secret (docs/08 §5.8).

One card per profile (kind, „Standard für …“, status with the time of the last test, models,
parallel use, usage window, where the secret lives) and a banner with cause and next step on
a failing profile. „Neues Profil“ logs in with „Mit Claude anmelden“ / „Mit ChatGPT
anmelden“: arr runs the login in the browser and keeps the token in the keychain (E18); there
is no field to paste a token. API-key profiles take the key once. `/profile/<name>` scrolls
to the card, `?neu=1` opens „Neues Profil“.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from nicegui import ui
from pydantic import SecretStr

from arr.models import AuthKind, Provider
from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    CreateProfileRequest,
    LoginState,
    ProfileInfo,
    ProviderDefaultsInfo,
)
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import button, esc, icon, raw
from arr.service.gui.components.copy_field import copy_html
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.form import Field, radio
from arr.service.gui.components.form_dialog import FormDialog
from arr.service.gui.components.page_state import (
    clean_href,
    load_into,
    skeleton_html,
    state_api,
    use_screen_assets,
)
from arr.service.gui.layout import Frame
from arr.service.gui.texts import Status

KIND_TEXT = {
    AuthKind.CLAUDE_OAUTH: "Claude-Abo (Max)",
    AuthKind.CLAUDE_API_KEY: "Claude-API-Key",
    AuthKind.CODEX_CHATGPT: "ChatGPT-Abo (Codex)",
    AuthKind.OPENAI_API_KEY: "OpenAI-API-Key",
}
NEW_KINDS = [
    (AuthKind.CLAUDE_OAUTH, "Claude-Abo", "Max oder Team. Anmeldung im Browser."),
    (
        AuthKind.CLAUDE_API_KEY,
        "Claude-API-Key",
        "Abrechnung pro Token über die Anthropic Console.",
    ),
    (AuthKind.CODEX_CHATGPT, "ChatGPT-Abo", "Über Codex. Anmeldung mit Link und Code."),
]
LOGIN_LABEL = {
    AuthKind.CLAUDE_OAUTH: "Mit Claude anmelden",
    AuthKind.CODEX_CHATGPT: "Mit ChatGPT anmelden",
}
LOGIN_INTRO = {
    AuthKind.CLAUDE_OAUTH: "arr öffnet die Anmeldung von Claude im Browser. Du meldest dich dort "
    "an, arr übernimmt das Token und legt es im Schlüsselbund ab. Du musst nichts kopieren.",
    AuthKind.CODEX_CHATGPT: "arr zeigt dir einen Link und einen Code. Du meldest dich damit im "
    "Browser an, arr legt den Login im Ordner des Profils ab.",
}
LOGIN_HELP = {
    AuthKind.CLAUDE_OAUTH: "„Mit Claude anmelden“ öffnet den Browser. Du meldest dich an, arr "
    "übernimmt das Token und legt es im Schlüsselbund ab.",
    AuthKind.CODEX_CHATGPT: "„Mit ChatGPT anmelden“ zeigt dir einen Link und einen Code. Du "
    "meldest dich damit im Browser an, arr legt den Login im Ordner des Profils ab.",
}
PROVIDER_WORD = {Provider.CLAUDE: "Claude", Provider.CODEX: "ChatGPT"}
CONCURRENCY_HINT = {
    AuthKind.CLAUDE_OAUTH: "Bei Max-Abos sind 3–4 ein guter Wert.",
    AuthKind.CLAUDE_API_KEY: "Bei API-Keys begrenzt eher dein Budget als die Zahl.",
    AuthKind.CODEX_CHATGPT: "Bei ChatGPT-Abos sind 2–3 ein guter Wert.",
}
CONCURRENCY_TEXT = "Höchstens so viele Agenten nutzen das Profil gleichzeitig."
LOGIN_POLL_S = 1.0
WAITING_TEXT = "Warte auf die Anmeldung im Browser …"
MIN_TEST_S = 0.8
"""„Testen …“ stays at least this long, so the result does not flash by (§5.8 „lädt“)."""


def uses_mac_login(profile: ProfileInfo) -> bool:
    """A Claude subscription profile without a token of its own that works anyway."""
    test = profile.last_test
    plain = profile.kind is AuthKind.CLAUDE_OAUTH and not profile.secret_present
    return plain and test is not None and test.ok


def status_badge(profile: ProfileInfo, now: float) -> str:
    """„✓ funktioniert“ with the time of the last test, „✕ Test fehlgeschlagen“ or „nicht
    verbunden“ (§5.8). The login of this Mac gets its own word: it is no login of the profile."""
    test = profile.last_test
    if test is None:
        return badge_html(Status("st-neutral", "link_off", "nicht verbunden"))
    when = f' <span class="faint">getestet {esc(fmt.when(test.ts, now))}</span>'
    if uses_mac_login(profile):
        return badge_html(Status("st-neutral", "laptop_mac", "Login dieses Macs")) + when
    if test.ok:
        return badge_html(Status("st-ok", "check", "funktioniert")) + when
    return badge_html(Status("st-err", "close", "Test fehlgeschlagen")) + when


class ProfilePage:
    """State of one Auth-Profile page (one browser tab)."""

    def __init__(self, frame: Frame, api: ApiClient, *, name: str, new: bool) -> None:
        self.frame = frame
        self.api = api
        self.name = name
        self.new = new
        self.profiles: list[ProfileInfo] = []
        self.testing: set[str] = set()
        self.waiting: tuple[str, dict[str, Any]] | None = None
        """The login this page waits for; closing the dialog cancels it."""
        self.submitting = False
        self.dialog_closed = False
        """Esc can overtake an action that still runs: it must not go on behind the dialog."""

    async def build(self) -> None:
        with self.frame.content:
            self.confirm = ConfirmDialog()
            self.dialog = FormDialog(wide=True)
            self.dialog.element.on("close", self._dialog_closed)
            with ui.element("div").classes("page-head"):
                raw(
                    '<h1 tabindex="-1">Auth-Profile</h1><span class="faint">Zugänge zu Claude '
                    "und ChatGPT. Die Secrets liegen im macOS-Schlüsselbund, nie im Workfolder."
                    "</span>",
                    tag="div",
                    classes="row",
                )
                with ui.element("div").classes("actions"):
                    button("＋ Neues Profil", self._new_dialog, classes="btn primary").mark(
                        "new-profile"
                    )
            self.box = ui.element("div")
            raw(
                f'<div class="banner info" style="margin-top:16px">{icon("info")}<div class="txt '
                'small">Abo-Logins (Claude Max, ChatGPT) sind nur für <b style="display:inline">'
                "persönliche</b> Nutzung gedacht. Für Firmen- oder Uni-Accounts klärst du die "
                "Nutzung vorher, oder du nimmst einen API-Key.</div></div>"
            )
        await self._load()
        if self.new:
            self._new_dialog()
        elif self.name:
            self.frame.js(
                f"arr.whenEl('#profil-{self.name}', (el) => el.scrollIntoView({{block: 'center'}}))"
            )

    async def _load(self) -> None:
        self.box.clear()
        await load_into(
            self.box,
            self._fetch,
            self._render,
            retry_href=clean_href(f"/profile/{self.name}" if self.name else "/profile"),
            skeleton=skeleton_html(3, shape="cards", reason="Die Profile werden gelesen …"),
        )

    async def _fetch(self) -> list[ProfileInfo]:
        return await self.api.list_profiles()

    def _render(self, profiles: list[ProfileInfo]) -> None:
        self.profiles = profiles
        if not self.profiles:
            raw(
                empty_state_html(
                    "key",
                    "Noch kein Zugang eingerichtet",
                    "Ohne Auth-Profil können keine Agenten starten. Das Einrichten dauert etwa "
                    "2 Minuten.",
                    [("Profil anlegen", "/profile?neu=1", True)],
                )
            )
            return
        if self.name and self.name not in [p.name for p in self.profiles]:
            raw(
                f'<div class="banner err" role="alert" style="margin-bottom:12px">{icon("error")}'
                f'<div class="txt"><b>Profil „{esc(self.name)}“ nicht gefunden.</b> Vielleicht '
                "wurde es gelöscht. Lege es neu an, wenn du es brauchst.</div></div>"
            )
        with ui.element("div").classes("grid-3 profiles"):
            for profile in self.profiles:
                self._card(profile)

    def _card(self, p: ProfileInfo) -> None:
        now = self.api.now()
        test = p.last_test
        bad = test is not None and not test.ok
        card = ui.element("article").classes("card profile-card" + (" bad" if bad else ""))
        card.props(f'id="profil-{esc(p.name)}" aria-labelledby="h-{esc(p.name)}"')
        with card:
            defaults = "".join(
                f'<span class="tag">Standard für {PROVIDER_WORD[d]}</span>' for d in p.default_for
            )
            raw(
                f'<div class="card-head"><h2 class="mono" id="h-{esc(p.name)}" tabindex="-1">'
                f"{esc(p.name)}</h2>{defaults}</div>"
            )
            rows = [("Art", esc(KIND_TEXT.get(p.kind, p.kind.value)))]
            rows.append(("Status", status_badge(p, now)))
            if test and test.models and test.ok:
                rows.append(("Modelle", esc(", ".join(test.models))))
            use = p.concurrency
            width = 100 * use.in_use / use.limit if use.limit else 0
            full = " warn" if use.limit and use.in_use >= use.limit else ""
            rows.append(
                (
                    "Parallel",
                    f"<b>{use.in_use} von {use.limit}</b> Plätzen belegt"
                    f'<span class="meter inline-meter{full}" aria-hidden="true"><span class="bar">'
                    f'<i style="width:{width:.0f}%"></i></span></span>'
                    if use.limit
                    else f"<b>{use.in_use} belegt</b>, keine Grenze gesetzt",
                )
            )
            if p.usage_window:
                window = p.usage_window
                label = window.label or "Limit-Fenster"
                share = fmt.percent(window.utilization)
                if window.utilization >= 0.8:
                    share = badge_html(Status("st-warn", "warning", share))
                reset = (
                    f" · zurückgesetzt {fmt.clock(window.resets_at)}" if window.resets_at else ""
                )
                rows.append(("Nutzung", f"{esc(label)}: {share}{esc(reset)}"))
            if p.login_dir:
                rows.append(("Login", f"eigener Login-Ordner {copy_html(p.login_dir)}"))
            elif p.secret_present:
                label = p.keychain_label
                where = f' <span class="faint">({esc(label)})</span>' if label else ""
                rows.append(("Secret", f"im Schlüsselbund ✓{where}"))
            elif uses_mac_login(p):
                rows.append(("Login", esc((test and test.message) or "Login dieses Macs")))
            else:
                rows.append(("Secret", "noch keins – melde dich an"))
            body = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
            self.status = raw(f'<dl class="kv">{body}</dl>', tag="div")
            if bad and test is not None and test.message:
                cause, _, rest = test.message.partition(". ")
                raw(
                    f'<div class="banner err banner-sm" role="alert">{icon("error")}'
                    f'<div class="txt"><b>{esc(cause)}.</b>{esc(rest)}</div></div>'
                )
            with ui.element("div").classes("btn-row"):
                self._actions(p, bad)

    def _actions(self, p: ProfileInfo, bad: bool) -> None:
        key_kind = p.kind in (AuthKind.CLAUDE_API_KEY, AuthKind.OPENAI_API_KEY)
        primary_label = None
        if bad:
            primary_label = (
                "Key ersetzen"
                if key_kind
                else (
                    p.last_test.action.label
                    if p.last_test and p.last_test.action
                    else "Neu anmelden"
                )
            )
        elif not p.secret_present:
            primary_label = "Key ersetzen" if key_kind else LOGIN_LABEL.get(p.kind, "Neu anmelden")
        if primary_label:
            action = self._key_dialog if key_kind else self._relogin
            button(primary_label, lambda: action(p), classes="btn btn-sm primary").mark(
                f"primary-{p.name}"
            )
        test_label = "Erneut testen" if bad else "Testen"
        test_button = button(test_label, None, classes="btn btn-sm").mark(f"test-{p.name}")
        test_button.on("click", lambda _e, b=test_button: self._test(p, b))
        button("Bearbeiten", lambda: self._edit_dialog(p), classes="btn btn-sm").mark(
            f"edit-{p.name}"
        )
        if p.provider not in p.default_for:
            word = PROVIDER_WORD[p.provider]
            button(f"Standard für {word}", lambda: self._set_default(p), classes="btn btn-sm").mark(
                f"default-{p.name}"
            )
        if not bad and p.secret_present and not key_kind:
            button("Neu anmelden", lambda: self._relogin(p), classes="btn btn-sm")
        button("Löschen …", lambda: self._delete(p), classes="btn btn-sm ghost danger")

    # --- actions -------------------------------------------------------------------------------

    async def _test(self, p: ProfileInfo, btn: ui.element) -> None:
        """„Testen“: a minimal call, result in ≤ 10 s (simulated in the mock)."""
        if p.name in self.testing:
            return
        self.testing.add(p.name)
        btn.clear()
        with btn:
            raw("Testen …", tag="span")
        btn.props('aria-busy="true" disabled')
        try:
            started = asyncio.get_running_loop().time()
            result = await self.api.test_profile(p.name)
            rest = MIN_TEST_S - (asyncio.get_running_loop().time() - started)
            if rest > 0:
                await asyncio.sleep(rest)
        except ApiException as exc:
            self.testing.discard(p.name)
            self.frame.toast(exc.error.message, error=True)
            return
        self.testing.discard(p.name)
        test = result.last_test
        if test and test.ok and test.message:
            self.frame.toast(f"{p.name}: {test.message}")
        elif test and test.ok:
            seconds = f" in {fmt.decimal((test.duration_ms or 0) / 1000, 1)} s"
            self.frame.toast(f"✓ {p.name} funktioniert{seconds}.")
        elif test:
            self.frame.toast(test.message or f"{p.name} funktioniert nicht.", error=True)
        await self._load()

    async def _set_default(self, p: ProfileInfo) -> None:
        """„Standard für …“: the profile new agents of this provider use (arr.yaml `defaults`)."""
        try:
            settings = await self.api.get_workfolder_settings()
            defaults = dict(settings.defaults)
            current = defaults.get(p.provider, ProviderDefaultsInfo())
            defaults[p.provider] = current.model_copy(update={"auth": p.name})
            await self.api.update_workfolder_settings(
                settings.model_copy(update={"defaults": defaults})
            )
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        self.frame.toast(f"{p.name} ist jetzt Standard für {PROVIDER_WORD[p.provider]}.")
        await self._load()

    async def _save_concurrency(self, name: str, limit: int) -> None:
        """Keep the limit from „Neues Profil“ in arr.yaml; the profile exists either way."""
        try:
            settings = await self.api.get_workfolder_settings()
            concurrency = {**settings.concurrency, name: limit}
            await self.api.update_workfolder_settings(
                settings.model_copy(update={"concurrency": concurrency})
            )
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)

    async def _test_saved(self, name: str, done: str) -> None:
        """„Speichern und testen“: the test after saving, with its result as toast."""
        try:
            test = (await self.api.test_profile(name)).last_test
        except ApiException as exc:
            self.frame.toast(exc.error.message, error=True)
            return
        if test and test.ok:
            self.frame.toast(done)
        else:
            self.frame.toast((test.message if test else None) or done, error=True)

    def _open_dialog(
        self, title: str, label: str, build: Callable[[], Any], submit: Callable[[], Any]
    ) -> None:
        """Open the dialog with its confirm button, which a login before may have hidden; it
        is locked while the action runs, and a key leaves the page with a finished dialog."""

        async def guarded() -> str | bool | None:
            if self.submitting or self.waiting is not None:  # a double click comes after the
                return False  # first action ended, when its login is already running
            self.submitting = True
            confirm = self.dialog.confirm_button
            confirm.props('disabled aria-busy="true"')
            try:
                result = await submit()
            finally:
                self.submitting = False
                confirm.props(remove="disabled aria-busy")
            if result is None:
                self.dialog.body.clear()
            return result

        self.dialog_closed = False
        self.dialog.open(title, label, build, guarded)
        self.dialog.set_confirm(label)

    def _relogin(self, p: ProfileInfo) -> None:
        self._login_dialog(p.name, p.kind, title=f"{p.name} neu anmelden")

    def _login_dialog(self, name: str, kind: AuthKind, *, title: str) -> None:
        """Login in the browser, run by arr (E18): wait, then done, failed or cancelled."""
        state: dict[str, Any] = {"timer": None}
        area: dict[str, ui.element] = {}

        def build() -> None:
            raw(f"<p>{esc(LOGIN_INTRO[kind])}</p>")
            area["box"] = ui.element("div")

        async def start() -> bool:
            await self._run_login(name, kind, area["box"], state, on_done=self._load)
            return False

        self._open_dialog(title, LOGIN_LABEL.get(kind, "Anmelden"), build, start)

    async def _run_login(
        self,
        name: str,
        kind: AuthKind,
        box: ui.element,
        state: dict[str, Any],
        *,
        on_done: Callable[[], Any],
    ) -> None:
        if self.dialog_closed:
            await self._load()  # „Neues Profil“ has already created the profile
            return
        try:
            login = await self.api.start_login(name)
        except ApiException as exc:
            if not self.dialog_closed:
                self._login_failed(box, exc.error.message, name, kind, state, on_done)
            await self._load()
            return
        if self.dialog_closed:  # during the start, which can take seconds for ChatGPT
            await self.api.cancel_login(name)
            await self._load()
            return
        self.dialog.set_confirm("", visible=False)
        box.clear()
        link = (
            f' <a href="{esc(login.browser_url)}" target="_blank" rel="noopener">'
            "Anmeldeseite öffnen</a>"
            if login.device_code and login.browser_url
            else ""
        )
        code = f" Code: <b class='mono'>{esc(login.device_code)}</b>" if login.device_code else ""

        def notice(text: str) -> str:
            return (
                f"{icon('hourglass_top')}<span><b>{esc(text)}</b>"
                f'<span class="small muted" style="display:block">{esc(login.note or "")}{link}'
                f"{code}</span></span>"
            )

        with box, ui.element("div").classes("login-wait").props('role="status"'):
            shown = {"text": WAITING_TEXT, "element": raw(notice(WAITING_TEXT), tag="span")}
            button("Abbrechen", lambda: cancel(), classes="btn btn-sm").mark("login-cancel")

        async def cancel() -> None:
            self.waiting = None
            if state["timer"] is not None:
                state["timer"].cancel()
            result = await self.api.cancel_login(name)
            await self._load()
            self._login_failed(box, result.message or "", name, kind, state, on_done)

        async def poll() -> None:
            status = await self.api.get_login_status(name)
            if status.state is LoginState.WAITING:
                if status.message and status.message != shown["text"]:  # the token is there
                    shown["text"] = status.message
                    shown["element"].set_content(notice(status.message))
                return
            self.waiting = None
            state["timer"].cancel()
            if status.state is LoginState.DONE:
                self.dialog.close()
                test = status.profile_info.last_test if status.profile_info else None
                if test and test.ok:
                    self.frame.toast(f"✓ {name} angemeldet und getestet.")
                elif test:
                    self.frame.toast(test.message or f"{name} funktioniert nicht.", error=True)
                else:
                    self.frame.toast(status.message or f"{name} ist angemeldet.", error=True)
                result = on_done()
                if hasattr(result, "__await__"):
                    await result
                return
            await self._load()  # first: `_login_failed` deletes the timer this call runs in
            self._login_failed(box, status.message or "", name, kind, state, on_done)

        self.waiting = (name, state)
        with box:
            state["timer"] = ui.timer(LOGIN_POLL_S, poll)

    async def _dialog_closed(self) -> None:
        """Esc or „Abbrechen“ in the footer: a login never runs on behind a closed dialog, and
        nothing typed into the dialog stays on the page."""
        self.dialog_closed = True
        waiting, self.waiting = self.waiting, None
        if waiting is not None:
            waiting[1]["timer"].cancel()
        self.dialog.body.clear()
        if waiting is not None:
            await self.api.cancel_login(waiting[0])
            await self._load()

    def _login_failed(
        self,
        box: ui.element,
        message: str,
        name: str,
        kind: AuthKind,
        state: dict[str, Any],
        on_done: Callable[[], Any],
    ) -> None:
        """„Die Claude-Anmeldung wurde abgebrochen.“ with „Erneut versuchen“ and the API key
        as alternative (§8.4)."""
        box.clear()
        cause, _, rest = (message or "Die Anmeldung hat nicht geklappt.").partition(". ")
        with box:
            raw(
                f'<div class="banner err banner-sm" role="alert">{icon("error")}<div class="txt">'
                f"<b>{esc(cause)}.</b>{esc(rest)}</div></div>"
            )
            with ui.element("div").classes("btn-row").style("margin-top:8px"):
                button(
                    "Erneut versuchen",
                    lambda: self._run_login(name, kind, box, state, on_done=on_done),
                    classes="btn btn-sm primary",
                )
                if kind is AuthKind.CLAUDE_OAUTH:
                    button(
                        "Claude-API-Key verwenden", self._use_api_key, classes="btn btn-sm"
                    ).mark("use-api-key")
        self.dialog.set_confirm("", visible=False)

    def _use_api_key(self) -> None:
        """Later, in the frame: the new dialog replaces the body that holds this button."""
        with self.frame.content:
            ui.timer(0, lambda: self._new_dialog(AuthKind.CLAUDE_API_KEY), once=True)

    def _new_dialog(self, kind: AuthKind = AuthKind.CLAUDE_OAUTH) -> None:
        """„Neues Auth-Profil“: kind, name, login or key, parallel use (§5.8)."""
        chosen = {"kind": kind}
        fields: dict[str, Field] = {}
        parts: dict[str, ui.element] = {}
        state: dict[str, Any] = {"timer": None}

        def draw_kind() -> None:
            box = parts["kind"]
            box.clear()
            k = chosen["kind"]
            with box:
                if k in LOGIN_LABEL:
                    raw(
                        f'<div class="field"><span class="label">Anmelden</span><span class="help">'
                        f"{esc(LOGIN_HELP[k])}</span>"
                        + (
                            '<span class="help">Klappt die Anmeldung nicht, nimm „Claude-API-Key“.'
                            "</span>"
                            if k is AuthKind.CLAUDE_OAUTH
                            else ""
                        )
                        + "</div>"
                    )
                else:
                    fields["key"] = Field(
                        "API-Key",
                        name="new-key",
                        kind="password",
                        help_text="Aus der Anthropic Console. arr speichert ihn nur im "
                        "Schlüsselbund und zeigt ihn nie wieder an.",
                        attrs='autocomplete="off"',
                    )
                parts["login"] = ui.element("div")
            fields["concurrency"].input.props(f'value="{4 if k is AuthKind.CLAUDE_OAUTH else 2}"')
            fields["concurrency"].value = str(4 if k is AuthKind.CLAUDE_OAUTH else 2)
            # one help text under the field, in the size of all help texts (K7)
            fields["concurrency"].set_help(f"{CONCURRENCY_TEXT} {CONCURRENCY_HINT[k]}")
            self.dialog.set_confirm(LOGIN_LABEL.get(k, "Speichern und testen"))

        def pick(value: str) -> None:
            chosen["kind"] = AuthKind(value)
            draw_kind()

        def build() -> None:
            with (
                ui.element("div")
                .classes("radio-cards cols-3")
                .props('role="radiogroup" aria-label="Art des Profils"')
            ):
                for k, title, sub in NEW_KINDS:
                    with ui.element("label").classes("radio-card"):
                        radio("kind", k.value, checked=k is kind, on_change=pick)
                        raw(
                            f"<span><b>{esc(title)}</b><span class='small muted'>{esc(sub)}"
                            "</span></span>",
                            tag="span",
                        )
            fields["name"] = Field(
                "Name",
                name="new-profile-name",
                help_text="Unter diesem Namen wählst du das Profil für Agenten aus. "
                "Kleinbuchstaben und Bindestriche.",
                attrs='autocomplete="off"',
            )
            fields["name"].box.style("margin-top:16px")
            parts["kind"] = ui.element("div")
            fields["concurrency"] = Field(
                "Parallelität",
                name="new-concurrency",
                kind="number",
                value="4",
                help_text=CONCURRENCY_TEXT,
                width="90px",
                attrs='min="1" max="16"',
            )
            draw_kind()

        async def submit() -> str | bool | None:
            name = fields["name"].value.strip()
            k = chosen["kind"]
            try:
                concurrency = int(fields["concurrency"].value or "2")
            except ValueError:
                fields["concurrency"].set_error("Gib eine Zahl von 1 bis 16 an.")
                return "Die Parallelität passt nicht."
            key = fields["key"].value.strip() if k not in LOGIN_LABEL and "key" in fields else ""
            if k not in LOGIN_LABEL and not key:
                fields["key"].set_error("Füge den API-Key ein.")
                return "Der API-Key fehlt."
            try:
                request = CreateProfileRequest(
                    name=name,
                    kind=k,
                    concurrency=concurrency,
                    api_key=SecretStr(key) if key else None,
                )
            except ValueError:
                fields["name"].set_error("Nimm Kleinbuchstaben, Ziffern und Bindestriche.")
                return "Der Name passt nicht."
            existing = next((p for p in self.profiles if p.name == name), None)
            if existing is None or k not in LOGIN_LABEL:
                try:
                    profile = await self.api.create_profile(request)
                except ApiException as exc:
                    if exc.error.field == "name":
                        fields["name"].set_error(exc.error.message)
                        return "Bitte korrigiere den Namen."
                    return exc.error.message
            else:
                profile = existing
            if existing is None:
                await self._save_concurrency(profile.name, concurrency)
            if k in LOGIN_LABEL:
                await self._run_login(profile.name, k, parts["login"], state, on_done=self._load)
                return False
            await self._test_saved(profile.name, f"✓ {profile.name} gespeichert und getestet.")
            await self._load()
            fields.clear()
            return None

        label = LOGIN_LABEL.get(kind, "Speichern und testen")
        self._open_dialog("Neues Auth-Profil", label, build, submit)

    def _key_dialog(self, p: ProfileInfo) -> None:
        """„Key ersetzen“: the new key goes to the keychain, then a test (§8.4)."""
        fields: dict[str, Field] = {}

        def build() -> None:
            raw(
                f"<p>Der neue Key ersetzt den alten von <b style='display:inline'>{esc(p.name)}"
                "</b> im Schlüsselbund. Danach testet arr ihn.</p>"
            )
            fields["key"] = Field(
                "Neuer API-Key",
                name="replace-key",
                kind="password",
                help_text="Erzeuge ihn in der Anthropic Console. arr zeigt ihn nie wieder an.",
                attrs='autocomplete="off"',
            )

        async def submit() -> str | None:
            key = fields["key"].value.strip()
            if not key:
                fields["key"].set_error("Füge den neuen API-Key ein.")
                return "Der API-Key fehlt."
            try:
                await self.api.create_profile(
                    CreateProfileRequest(
                        name=p.name, kind=p.kind, api_key=SecretStr(key), replace=True
                    )
                )
            except ApiException as exc:
                await self._load()  # the card shows what really lies in the keychain
                return exc.error.message
            await self._test_saved(p.name, f"✓ Key von {p.name} ersetzt und getestet.")
            await self._load()
            fields.clear()
            return None

        self._open_dialog("Key ersetzen", "Speichern und testen", build, submit)

    def _edit_dialog(self, p: ProfileInfo) -> None:
        """„Bearbeiten“: parallel use (arr.yaml `concurrency`, §5.8)."""
        fields: dict[str, Field] = {}

        def build() -> None:
            fields["n"] = Field(
                "Parallelität",
                name="edit-concurrency",
                kind="number",
                value=str(p.concurrency.limit),
                help_text=f"Höchstens so viele Agenten nutzen {p.name} gleichzeitig. "
                + CONCURRENCY_HINT.get(p.kind, ""),
                width="90px",
                attrs='min="1" max="16"',
            )

        async def submit() -> str | None:
            try:
                n = int(fields["n"].value)
            except ValueError:
                n = 0
            if not 1 <= n <= 16:
                fields["n"].set_error("Gib eine Zahl von 1 bis 16 an.")
                return "Die Parallelität passt nicht."
            try:
                settings = await self.api.get_workfolder_settings()
                concurrency = {**settings.concurrency, p.name: n}
                await self.api.update_workfolder_settings(
                    settings.model_copy(update={"concurrency": concurrency})
                )
            except ApiException as exc:
                return exc.error.message
            self.frame.toast(f"Gespeichert: {p.name} nutzen höchstens {n} Agenten gleichzeitig.")
            return None

        self._open_dialog(f"{p.name} bearbeiten", "Speichern", build, submit)

    def _delete(self, p: ProfileInfo) -> None:
        """„Profil löschen“ with consequences (§8.6)."""
        secret = (
            "Der Login-Ordner wird entfernt."
            if p.login_dir
            else "Das Secret wird aus dem Schlüsselbund entfernt."
        )
        in_use = (
            f"{p.concurrency.in_use} laufende Agenten nutzen das Profil gerade. Löschen geht "
            "erst, wenn sie fertig sind."
            if p.concurrency.in_use
            else "Kein laufender Run nutzt das Profil."
        )

        async def confirm(_value: str) -> None:
            try:
                await self.api.delete_profile(p.name)
            except ApiException as exc:
                self.frame.toast(exc.error.message, error=True)
                return
            self.frame.toast(f"Profil „{p.name}“ gelöscht.")
            await self._load()

        self.confirm.show(
            f"Profil „{p.name}“ löschen?",
            [secret, in_use, "Alte Runs behalten den Profilnamen in ihrem Protokoll."],
            "Profil löschen",
            confirm,
        )


def register() -> None:
    """Register `/profile` and `/profile/<name>` (docs/08 §5.8, §4.3)."""

    async def show(name: str, zustand: str, neu: str) -> None:
        api = state_api(get_api(), zustand)
        use_screen_assets()
        frame = Frame(
            api,
            active="profile",
            title=name or "Auth-Profile",
            crumbs=[("Auth-Profile", "/profile"), (name, None)] if name else None,
            focus=f"#h-{name}" if name else None,
        )
        await frame.start()
        await ProfilePage(frame, api, name=name, new=neu == "1").build()

    @ui.page("/profile")
    async def profile_list(zustand: str = "", neu: str = "") -> None:
        await show("", zustand, neu)

    @ui.page("/profile/{name}")
    async def profile_one(name: str, zustand: str = "", neu: str = "") -> None:
        await show(name, zustand, neu)

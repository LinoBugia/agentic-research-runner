"""ChatGPT login of a Codex auth profile by device code, into the profile's own login folder.

`codex_login` starts an app-server with `CODEX_HOME` = the profile's login folder
(`codex_home_for`), asks for a device code, hands link and code to the caller, waits for the
result and reads the account state. Only the kind of account and the plan leave this module:
no e-mail, no account ID, no token (docs/01 §8). `~/.codex` of the user is never touched.

See docs/spikes/S2_codex.md §13, §15 and docs/04-service-gui-und-schnittstellen.md §5.
"""

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arr.backends.base import RateLimitData
from arr.backends.codex_config import SECRET_ENV, codex_home_for
from arr.backends.codex_events import rate_limit_status
from arr.backends.codex_sdk import install_message_hooks, internals_available
from arr.models import AuthProfile

__all__ = [
    "AccountState",
    "CodexCheck",
    "LoginResult",
    "codex_account",
    "codex_check",
    "codex_login",
]

logger = logging.getLogger(__name__)

CodexFactory = Callable[[Any], Any]
"""Builds an `AsyncCodex` from a `CodexConfig`; tests pass a fake."""

TEST_MODEL = "gpt-6-luna"
TEST_PROMPT = "Antworte nur mit: ok"


@dataclass(frozen=True, slots=True)
class AccountState:
    """What arr may know about a login: kind (`chatgpt`, `apiKey` …) and plan, no identity."""

    kind: str | None
    plan: str | None

    @property
    def logged_in(self) -> bool:
        return self.kind is not None

    def describe(self) -> str:
        """German one-liner for the user, e.g. „ChatGPT (plus)“."""
        if self.kind is None:
            return "nicht angemeldet"
        name = "ChatGPT" if self.kind == "chatgpt" else self.kind
        return f"{name} ({self.plan})" if self.plan else name


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of a device-code login; `error` is Codex's message without account data."""

    success: bool
    account: AccountState
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CodexCheck:
    """Outcome of a test turn. `problem`: `login`, `limit` or `other`, no text of Codex."""

    account: AccountState
    problem: str | None = None
    limit: RateLimitData | None = None


def _default_factory(config: Any) -> Any:
    from openai_codex import AsyncCodex

    return AsyncCodex(config)


def _config(home: Path) -> Any:
    from openai_codex import CodexConfig

    env = {"CODEX_HOME": str(home)}
    env |= {name: "" for name in SECRET_ENV if os.environ.get(name)}
    # Only what a login needs: no user config of its own exists in a fresh login folder.
    return CodexConfig(env=env, config_overrides=("notify=[]", "check_for_update_on_startup=false"))


def _state(response: Any) -> AccountState:
    account = getattr(response, "account", None)
    root = getattr(account, "root", account)
    if root is None:
        return AccountState(kind=None, plan=None)
    plan = getattr(root, "plan_type", None)
    plan_text = getattr(plan, "value", plan)
    return AccountState(kind=getattr(root, "type", None), plan=str(plan_text) if plan else None)


def prepare_login_dir(auth: AuthProfile) -> Path:
    """Create the profile's login folder (mode 0700) and return it."""
    home = codex_home_for(auth)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    return home


async def codex_account(auth: AuthProfile, *, factory: CodexFactory | None = None) -> AccountState:
    """Account state of a profile's login folder (kind and plan only)."""
    codex = (factory or _default_factory)(_config(prepare_login_dir(auth)))
    async with codex:
        return _state(await codex.account())


def _problem(auth: AuthProfile, text: str) -> str:
    from arr.backends.codex import login_hint

    if login_hint(auth, text) != text:
        return "login"
    return "limit" if "usage limit" in text.lower() or "rate limit" in text.lower() else "other"


async def codex_check(
    auth: AuthProfile, cwd: Path, *, factory: CodexFactory | None = None
) -> CodexCheck:
    """One short turn in the profile's login folder: login, model access and limit window.

    Nothing of the turn stays: the thread is ephemeral and `cwd` is the caller's empty folder.
    Texts of Codex only decide the kind of `problem`, they never leave this function.
    """
    from openai_codex import ApprovalMode, Sandbox
    from openai_codex.generated.v2_all import ReasoningEffort

    limits: list[RateLimitData] = []

    def on_in(message: dict[str, Any]) -> None:
        if message.get("method") == "account/rateLimits/updated":
            limit = rate_limit_status(message.get("params") or {})
            if limit is not None:
                limits.append(limit)

    codex = (factory or _default_factory)(_config(prepare_login_dir(auth)))
    async with codex:
        state = _state(await codex.account())
        if not state.logged_in:
            return CodexCheck(account=state, problem="login")
        if internals_available(codex):
            install_message_hooks(codex, on_in=on_in)
        try:
            thread = await codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(cwd),
                ephemeral=True,
                model=TEST_MODEL,
                sandbox=Sandbox.read_only,
            )
            turn = await thread.run(TEST_PROMPT, effort=ReasoningEffort.low)
        except Exception as exc:  # noqa: BLE001 - classified, the text stays here
            return CodexCheck(account=state, problem=_problem(auth, str(exc)))
    limit = limits[-1] if limits else None
    error = getattr(turn, "error", None)
    if error is not None:
        text = str(getattr(error, "message", "")) + str(getattr(error, "codex_error_info", ""))
        return CodexCheck(account=state, problem=_problem(auth, text), limit=limit)
    status = getattr(getattr(turn, "status", None), "value", None)
    return CodexCheck(
        account=state, problem=None if status == "completed" else "other", limit=limit
    )


async def codex_login(
    auth: AuthProfile,
    *,
    show: Callable[[str, str], None],
    factory: CodexFactory | None = None,
) -> LoginResult:
    """Log a `codex_chatgpt` profile in by device code.

    `show(link, code)` is called once with the verification link and the one-time code. Then
    the function waits for Codex (the code expires after 15 minutes) and returns the result.
    A cancelled wait (Ctrl+C) cancels the login attempt in Codex as well.
    """
    codex = (factory or _default_factory)(_config(prepare_login_dir(auth)))
    async with codex:
        handle = await codex.login_chatgpt_device_code()
        show(handle.verification_url, handle.user_code)
        try:
            done = await handle.wait()
        except asyncio.CancelledError:
            try:
                await asyncio.shield(handle.cancel())
            except Exception:  # noqa: BLE001 - best effort, the wait was cancelled anyway
                logger.warning("Login-Versuch für Profil %s ließ sich nicht abbrechen", auth.name)
            raise
        state = _state(await codex.account())
    success = bool(getattr(done, "success", False)) and state.logged_in
    error = None if success else (getattr(done, "error", None) or "Anmeldung nicht abgeschlossen.")
    logger.info("Codex-Login für Profil %s: %s", auth.name, "ok" if success else "fehlgeschlagen")
    return LoginResult(success=success, account=state, error=error)

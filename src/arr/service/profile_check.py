"""„Testen“ of an auth profile: one minimal model call (docs/04 §5, docs/08 §5.8).

`check_profile` returns a `CheckResult` with the kind of failure, the tested model and the
limit window, and `to_profile_test` turns it into the `ProfileTest` with the texts of docs/08 §8.4.
Those texts are fixed: nothing the CLI or Codex says reaches a message, a log or a file.
"""

import asyncio
import contextlib
import logging
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from arr.auth import (
    START_PROFILE_VARIABLE,
    AuthError,
    SecretReader,
    claude_env,
    clean_process_env,
)
from arr.backends.claude_start import start_program
from arr.backends.codex_login import TEST_MODEL as CODEX_TEST_MODEL
from arr.backends.codex_login import ForeignLoginFolder, codex_check
from arr.errors import ArrError
from arr.models import AuthKind, AuthProfile
from arr.service.api_models import Action, ProfileTest, UsageWindow

log = logging.getLogger("arr.service")

CLAUDE_TEST_MODEL = "claude-haiku-5-5"
TEST_PROMPT = "Antworte nur mit: ok"
TEST_BUDGET_USD = 0.10
CHECK_TIMEOUT_S = 30.0

MAC_LOGIN_TEXT = "Nutzt den Claude-Login dieses Macs, kein eigenes Token."
WINDOW_LABELS = {
    "five_hour": "5-Stunden-Fenster",
    "seven_day": "7-Tage-Fenster",
    "seven_day_opus": "7-Tage-Fenster (Opus)",
    "seven_day_sonnet": "7-Tage-Fenster (Sonnet)",
}


class CheckFailure(StrEnum):
    """Why a test failed; each kind has its text in `to_profile_test`."""

    REJECTED = "rejected"
    NOT_LOGGED_IN = "not_logged_in"
    NO_KEY = "no_key"
    KEYCHAIN = "keychain"
    FOREIGN_FOLDER = "foreign_folder"
    RATE_LIMITED = "rate_limited"
    BILLING = "billing"
    MODEL = "model"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    OTHER = "other"


ERROR_KINDS = {
    "authentication_failed": CheckFailure.REJECTED,
    "billing_error": CheckFailure.BILLING,
    "rate_limit": CheckFailure.RATE_LIMITED,
    "invalid_request": CheckFailure.MODEL,
}
CODEX_PROBLEMS = {"login": CheckFailure.REJECTED, "limit": CheckFailure.RATE_LIMITED}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one test; `failure` None means the model answered."""

    failure: CheckFailure | None = None
    models: tuple[str, ...] = ()
    window: UsageWindow | None = None
    secret_present: bool | None = None
    """Whether the keychain held a secret; None if it was not asked or did not answer."""


def _sdk_query(prompt: str, options: Any) -> Any:
    from claude_agent_sdk import query

    return query(prompt=prompt, options=options)


def _window(
    utilization: float | None, resets_at: float | None, label: str | None
) -> UsageWindow | None:
    if utilization is None:
        return None
    return UsageWindow(
        utilization=min(max(utilization, 0.0), 1.0), resets_at=resets_at, label=label
    )


async def _claude_turn(profile: AuthProfile, reader: SecretReader | None, cwd: str) -> CheckResult:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        RateLimitEvent,
        ResultMessage,
    )

    clean_process_env()
    # In a thread: a dialog of the keychain must not stop the service (W3-05 §11).
    env = await asyncio.to_thread(claude_env, profile, reader)
    found = START_PROFILE_VARIABLE in env
    if found:  # only the login of this Mac needs the real folder
        env["CLAUDE_CONFIG_DIR"] = str(Path(cwd) / "config")
    try:
        cli_path = start_program() if found else None
    except ArrError as exc:  # no way back into the environment: the test does not start
        log.warning("Profil-Test %s: %s", profile.name, type(exc).__name__)
        return CheckResult(CheckFailure.OTHER, secret_present=found)
    options = ClaudeAgentOptions(
        cli_path=cli_path,
        model=CLAUDE_TEST_MODEL,
        max_turns=1,
        max_budget_usd=TEST_BUDGET_USD,
        tools=[],
        setting_sources=[],
        cwd=cwd,
        env=env,
        extra_args={"no-session-persistence": None},
    )
    failure: CheckFailure | None = None
    window: UsageWindow | None = None
    answered = False
    try:
        async with contextlib.aclosing(_sdk_query(TEST_PROMPT, options)) as messages:
            async for message in messages:
                if isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    resets = float(info.resets_at) if info.resets_at is not None else None
                    label = WINDOW_LABELS.get(str(info.rate_limit_type))
                    window = _window(info.utilization, resets, label) or window
                    if info.status == "rejected":
                        failure = CheckFailure.RATE_LIMITED
                elif isinstance(message, AssistantMessage) and message.error:
                    failure = failure or ERROR_KINDS.get(message.error, CheckFailure.OTHER)
                elif isinstance(message, ResultMessage):
                    answered = not message.is_error
    except Exception as exc:  # noqa: BLE001 - only the type: a message may quote the CLI
        log.warning("Profil-Test %s: %s", profile.name, type(exc).__name__)
        return CheckResult(failure or CheckFailure.OTHER, window=window, secret_present=found)
    if failure is None and not answered:
        failure = CheckFailure.OTHER
    return CheckResult(failure, () if failure else (CLAUDE_TEST_MODEL,), window, found)


async def _codex_turn(profile: AuthProfile, cwd: str) -> CheckResult:
    try:
        check = await codex_check(profile, Path(cwd))
    except ForeignLoginFolder:
        return CheckResult(CheckFailure.FOREIGN_FOLDER)
    except Exception as exc:  # noqa: BLE001 - only the type: a message may quote Codex
        log.warning("Profil-Test %s: %s", profile.name, type(exc).__name__)
        return CheckResult(CheckFailure.OTHER)
    limit = check.limit
    window = _window(limit.get("utilization"), limit.get("resets_at"), None) if limit else None
    if check.problem == "login" and not check.account.logged_in:
        return CheckResult(CheckFailure.NOT_LOGGED_IN)
    if check.problem:
        return CheckResult(CODEX_PROBLEMS.get(check.problem, CheckFailure.OTHER), window=window)
    return CheckResult(None, (CODEX_TEST_MODEL,), window)


async def check_profile(
    profile: AuthProfile,
    reader: SecretReader | None = None,
    *,
    timeout_s: float | None = None,
) -> CheckResult:
    """One minimal call with the profile, ended at the deadline; never raises for a failure."""
    limit = CHECK_TIMEOUT_S if timeout_s is None else timeout_s
    if profile.kind is AuthKind.OPENAI_API_KEY:
        return CheckResult(CheckFailure.UNSUPPORTED)
    with tempfile.TemporaryDirectory(prefix="arr-test-") as cwd:
        try:
            async with asyncio.timeout(limit):
                if profile.kind is AuthKind.CODEX_CHATGPT:
                    return await _codex_turn(profile, cwd)
                return await _claude_turn(profile, reader, cwd)
        except TimeoutError:
            return CheckResult(CheckFailure.TIMEOUT)
        except AuthError:
            if profile.kind is AuthKind.CLAUDE_API_KEY:
                return CheckResult(CheckFailure.NO_KEY, secret_present=False)
            return CheckResult(CheckFailure.KEYCHAIN)


def to_profile_test(profile: AuthProfile, result: CheckResult, started: float) -> ProfileTest:
    """The `ProfileTest` of a check with the texts of docs/08 §8.4 (bold cause, next step)."""
    now = time.time()
    duration_ms = int((now - started) * 1000)
    if result.failure is None:
        mac = profile.kind is AuthKind.CLAUDE_OAUTH and result.secret_present is False
        return ProfileTest(
            ts=now,
            ok=True,
            duration_ms=duration_ms,
            models=list(result.models),
            message=MAC_LOGIN_TEXT if mac else None,
        )
    name = profile.name
    url = f"/profile/{name}"
    is_key = profile.kind is AuthKind.CLAUDE_API_KEY
    relogin = Action(label="Neu anmelden", url=url)
    replace = Action(label="Key ersetzen", url=url)
    texts: dict[CheckFailure, tuple[str, Action | None]] = {
        CheckFailure.REJECTED: (
            (
                f"Der API-Key von „{name}“ wurde abgelehnt (401). Vermutlich wurde er in der "
                "Anthropic Console widerrufen. Erzeuge dort einen neuen Key und füge ihn hier ein."
            )
            if is_key
            else f"Die Anmeldung von „{name}“ ist abgelaufen. Melde dich neu an.",
            replace if is_key else relogin,
        ),
        CheckFailure.NOT_LOGGED_IN: (
            f"„{name}“ ist noch nicht angemeldet. Melde dich an.",
            relogin,
        ),
        CheckFailure.NO_KEY: (
            f"Für „{name}“ liegt kein API-Key im Schlüsselbund. Füge ihn hier ein.",
            replace,
        ),
        CheckFailure.KEYCHAIN: (
            f"Der Schlüsselbund ist für „{name}“ nicht erreichbar. Erlaube den Zugriff und "
            "teste erneut.",
            None,
        ),
        CheckFailure.FOREIGN_FOLDER: (
            f"„{name}“ nutzt einen Login-Ordner außerhalb von arr. arr testet dort nicht. Lege "
            "für die Anmeldung ein neues Profil an.",
            None,
        ),
        CheckFailure.RATE_LIMITED: (
            f"„{name}“ hat sein Nutzungslimit erreicht. Warte, bis es zurückgesetzt ist, und "
            "teste dann erneut.",
            None,
        ),
        CheckFailure.BILLING: (
            f"Für „{name}“ fehlt Guthaben oder ein passendes Abo. Prüfe dein Konto und teste "
            "dann erneut.",
            None,
        ),
        CheckFailure.MODEL: (
            f"„{name}“ kommt nicht an das Testmodell heran. Prüfe dein Abo und teste dann erneut.",
            None,
        ),
        CheckFailure.TIMEOUT: (
            f"Der Test von „{name}“ hat zu lange gedauert. Prüfe deine Internetverbindung und "
            "teste erneut.",
            None,
        ),
        CheckFailure.UNSUPPORTED: (
            f"„{name}“ lässt sich noch nicht testen. Für OpenAI-API-Keys gibt es in dieser "
            "Fassung von arr keinen Test.",
            None,
        ),
        CheckFailure.OTHER: (
            f"Der Test von „{name}“ hat nicht geklappt. Die Ursache ist unbekannt. Versuche es "
            "erneut.",
            None,
        ),
    }
    message, action = texts[result.failure]
    return ProfileTest(ts=now, ok=False, duration_ms=duration_ms, message=message, action=action)

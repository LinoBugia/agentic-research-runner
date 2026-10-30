"""Gate W3-05 (E18): can arr run `claude setup-token` without a terminal?

The user runs this once by hand; the sign-in needs a real browser. It runs the login through
the code the service uses (`arr.service.login.capture_token`), keeps the token under the
probe profile in the keychain, makes one test request with `claude-haiku-5-5` and deletes the
entry again. It prints only whether a token was seen and its length, never the value, and it
never prints output of the CLI.

Usage: uv run python spikes/w3_05/claude_login_probe.py
"""

import asyncio
import sys
import tempfile
import time

import keyring
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

from arr.auth import CLAUDE_SWITCHES, DEFAULT_SERVICE, clean_process_env
from arr.service.login import CaptureOutcome, capture_token

PROFILE = "arr-login-probe"
MODEL = "claude-haiku-5-5"
TIMEOUT_S = 300.0


def say(text: str) -> None:
    print(text, flush=True)


async def test_request() -> tuple[bool, str]:
    """One request with the stored token; returns (ok, fixed description)."""
    token = keyring.get_password(DEFAULT_SERVICE, PROFILE)
    if not token:
        return False, "kein Token im Schlüsselbund"
    clean_process_env()
    error = None
    ok = False
    with tempfile.TemporaryDirectory(prefix="arr-probe-") as cwd:
        options = ClaudeAgentOptions(
            model=MODEL,
            max_turns=1,
            max_budget_usd=0.10,
            tools=[],
            setting_sources=[],
            cwd=cwd,
            env={**CLAUDE_SWITCHES, "CLAUDE_CODE_OAUTH_TOKEN": token},
        )
        try:
            async for message in query(prompt="Antworte nur mit: ok", options=options):
                if isinstance(message, AssistantMessage) and message.error:
                    error = message.error
                elif isinstance(message, ResultMessage):
                    ok = not message.is_error
        except Exception as exc:  # noqa: BLE001 - the type only, never a message of the CLI
            return False, type(exc).__name__
    return ok and error is None, f"error={error}"


async def main() -> int:
    say(f"Anmeldung startet, der Browser öffnet sich. Frist: {TIMEOUT_S:.0f} s.")
    started = time.monotonic()

    def store(token: str) -> None:
        say("Token erkannt, Ablage im Schlüsselbund …")
        keyring.set_password(DEFAULT_SERVICE, PROFILE, token)

    result = await capture_token(store, timeout_s=TIMEOUT_S)
    seconds = time.monotonic() - started
    say(f"Ergebnis: {result.outcome.value} nach {seconds:.0f} s, Exit-Code {result.exit_code}")
    say(f"Token erkannt: {'ja' if result.outcome is CaptureOutcome.TOKEN else 'nein'}")
    if result.outcome is not CaptureOutcome.TOKEN:
        return 1
    say(f"Länge: {result.token_length}")
    try:
        ok, detail = await test_request()
        say(f"Test mit {MODEL}: {'ok' if ok else 'fehlgeschlagen'} ({detail})")
        return 0 if ok else 2
    finally:
        keyring.delete_password(DEFAULT_SERVICE, PROFILE)
        say("Eintrag im Schlüsselbund gelöscht.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

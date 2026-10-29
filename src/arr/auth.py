"""Auth profiles: secrets in the macOS keychain via `keyring` and the environment
built for each agent process.

`clean_process_env()` strips everything from `os.environ` that would select a credential or
tie a child process to a parent Claude Code session (docs/spikes/S1 §2.5, §3.1): the SDK only
merges `ClaudeAgentOptions.env` on top of the inherited environment, it cannot remove keys.
`claude_env()` then builds the per-agent variables from an `AuthProfile`. Secrets are read
from the keychain only, never logged and never put into an error message.

See docs/01-architektur.md §8, docs/04-service-gui-und-schnittstellen.md §5.
"""

import logging
import os
from collections.abc import Callable, MutableMapping

from arr.errors import ArrError
from arr.models import AuthKind, AuthProfile, KeychainRef

log = logging.getLogger(__name__)

DEFAULT_SERVICE = "arr"
"""Keychain service of a profile without an explicit `keychain` reference."""

_DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
_DROP_EXACT = frozenset({"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR"})

CLAUDE_SWITCHES = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
}
"""Fixed switches for every Claude agent (S1 §3.1): no claude.ai connectors, no auto memory,
no bundled skills. `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` keeps the credential out of the
environment of Bash and other child processes (W2-01.1, B2): without it the CLI passes
`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` on (only GitHub Actions scrubs by default).
The CLI then also drops secret-like names (`*_TOKEN`, `*_KEY`, …) and config paths such as
`XDG_CACHE_HOME`, and forces the permission mode to `default` (writes go to `can_use_tool`)."""

SecretReader = Callable[[str, str], str | None]
"""`(service, account) -> secret or None`, defaults to `keyring.get_password`."""


class AuthError(ArrError):
    """An auth profile cannot be used (wrong kind, missing secret, keychain not reachable)."""


def clean_process_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Remove credential and parent-session variables from the environment (S1 §3.1).

    Drops all `ANTHROPIC_*` (also `ANTHROPIC_BASE_URL`), all `CLAUDE_CODE_*` (also
    `CLAUDE_CODE_OAUTH_TOKEN`), all `CLAUDE_AGENT_SDK_*`, `CLAUDECODE`, `CLAUDE_PID`,
    `CLAUDE_EFFORT` and `CLAUDE_CONFIG_DIR`. Idempotent. Returns the removed names (never
    values), sorted.
    """
    env = os.environ if environ is None else environ
    removed = [k for k in list(env) if k in _DROP_EXACT or k.startswith(_DROP_PREFIXES)]
    for key in removed:
        del env[key]
    return sorted(removed)


def keychain_ref(profile: AuthProfile) -> KeychainRef:
    """Keychain item of a profile: its own reference or service `arr` + profile name."""
    return profile.keychain or KeychainRef(service=DEFAULT_SERVICE, account=profile.name)


def _keyring_get(service: str, account: str) -> str | None:
    import keyring

    return keyring.get_password(service, account)


def read_secret(profile: AuthProfile, reader: SecretReader | None = None) -> str | None:
    """The profile's secret from the keychain, or None if none is stored.

    Errors of the keychain become an `AuthError` that names the profile, not the secret.
    """
    ref = keychain_ref(profile)
    try:
        secret = (reader or _keyring_get)(ref.service, ref.account)
    except Exception as exc:
        raise AuthError(
            f"Der Schlüsselbund ist für das Profil „{profile.name}“ nicht erreichbar "
            f"({type(exc).__name__})."
        ) from None
    return secret or None


def claude_env(profile: AuthProfile, reader: SecretReader | None = None) -> dict[str, str]:
    """Per-agent environment of a Claude agent: the switches plus the profile's credential.

    - `claude_oauth` with a token in the keychain → `CLAUDE_CODE_OAUTH_TOKEN`
    - `claude_oauth` without a stored token → no credential: the agent uses the Mac's
      existing Claude login (logged as info, without account data)
    - `claude_api_key` → `ANTHROPIC_API_KEY` (missing key is an error)
    """
    env = dict(CLAUDE_SWITCHES)
    if profile.kind is AuthKind.CLAUDE_OAUTH:
        token = read_secret(profile, reader)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        else:
            log.info(
                "Profil „%s“: kein Token im Schlüsselbund, der Agent nutzt den vorhandenen "
                "Claude-Login dieses Macs.",
                profile.name,
            )
    elif profile.kind is AuthKind.CLAUDE_API_KEY:
        key = read_secret(profile, reader)
        if not key:
            raise AuthError(f"Für das Profil „{profile.name}“ liegt kein API-Key im Schlüsselbund.")
        env["ANTHROPIC_API_KEY"] = key
    else:
        raise AuthError(
            f"Das Profil „{profile.name}“ ({profile.kind}) passt nicht zu einem Claude-Agenten."
        )
    return env


def is_billed(profile: AuthProfile) -> bool:
    """Whether costs of this profile are billed (API key) rather than estimated (docs/08 §5.0)."""
    return profile.kind in (AuthKind.CLAUDE_API_KEY, AuthKind.OPENAI_API_KEY)

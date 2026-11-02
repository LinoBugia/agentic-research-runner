"""Auth profiles: secrets in the macOS keychain via `keyring` and the environment
built for each agent process, which never holds a secret.

`clean_process_env()` strips everything from `os.environ` that would select a credential or
tie a child process to a parent Claude Code session (docs/spikes/S1 §2.5, §3.1): the SDK only
merges `ClaudeAgentOptions.env` on top of the inherited environment, it cannot remove keys.
`claude_env()` then builds the per-agent variables from an `AuthProfile`. Secrets are read
from the keychain only, never logged and never put into an error message.

A secret never goes into an environment: every program of the user can read the start
environment of a process, also from inside the sandbox (`KERN_PROCARGS2`, W3-06 §12). The start
program (`arr.backends.claude_start`) hands it to the CLI through a pipe; `claude_env` only
names the profile for it, and there is no way back.

See docs/01-architektur.md §8, docs/04-service-gui-und-schnittstellen.md §5.
"""

import contextlib
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

SECRET_VARIABLES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
"""Names the CLI takes a secret from; none of them is ever set by arr."""

START_PROFILE_VARIABLE = "ARR_CLAUDE_START_PROFILE"
"""Tells the start program which profile's secret to hand over: the name, never the secret."""

DESCRIPTOR_VARIABLES = {
    AuthKind.CLAUDE_OAUTH: "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    AuthKind.CLAUDE_API_KEY: "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
}
"""Where the CLI finds the descriptor of the pipe, by kind of secret."""

SecretReader = Callable[[str, str], str | None]
"""`(service, account) -> secret or None`, defaults to `keyring.get_password`."""
SecretWriter = Callable[[str, str, str], None]
"""`(service, account, secret)`, defaults to `keyring.set_password`."""
SecretDeleter = Callable[[str, str], None]
"""`(service, account)`, defaults to `keyring.delete_password`; no entry is no error."""


class AuthError(ArrError):
    """An auth profile cannot be used (wrong kind, missing secret, keychain not reachable)."""


class SecretNotReplaced(AuthError):
    """The new secret is not stored. `present`: what lies there now, None if nobody knows."""

    def __init__(self, message: str, present: bool | None) -> None:
        super().__init__(message)
        self.present = present


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
        log.warning("Schlüsselbund, Lesen: %s", type(exc).__name__)
        raise AuthError(
            f"Der Schlüsselbund ist für das Profil „{profile.name}“ nicht erreichbar. Erlaube "
            "den Zugriff und versuche es erneut."
        ) from None
    return secret or None


def descriptor_variable(profile: AuthProfile) -> str:
    """The variable of the CLI that takes this profile's secret through a pipe."""
    variable = DESCRIPTOR_VARIABLES.get(profile.kind)
    if variable is None:
        raise AuthError(
            f"Das Profil „{profile.name}“ ({profile.kind}) passt nicht zu einem Claude-Agenten."
        )
    return variable


def claude_env(profile: AuthProfile, reader: SecretReader | None = None) -> dict[str, str]:
    """Per-agent environment of a Claude agent: the switches, never a secret.

    - `claude_oauth` with a token in the keychain, `claude_api_key` with a key →
      `START_PROFILE_VARIABLE`: the start program must run, it hands the secret over
    - `claude_oauth` without a stored token → no credential: the agent uses the Mac's
      existing Claude login (logged as info, without account data)
    - `claude_api_key` without a key is an error
    """
    env = dict(CLAUDE_SWITCHES)
    if profile.kind is AuthKind.CLAUDE_OAUTH:
        if read_secret(profile, reader):
            env[START_PROFILE_VARIABLE] = profile.name
        else:
            log.info(
                "Profil „%s“: kein Token im Schlüsselbund, der Agent nutzt den vorhandenen "
                "Claude-Login dieses Macs.",
                profile.name,
            )
    elif profile.kind is AuthKind.CLAUDE_API_KEY:
        if not read_secret(profile, reader):
            raise AuthError(f"Für das Profil „{profile.name}“ liegt kein API-Key im Schlüsselbund.")
        env[START_PROFILE_VARIABLE] = profile.name
    else:
        descriptor_variable(profile)  # raises: this kind has no Claude login
    return env


def owns_keychain_item(profile: AuthProfile) -> bool:
    """True for arr's own item (`arr` + profile name), the only kind arr writes or deletes.

    A hand-written `keychain` reference may point to an item of another program.
    """
    ref = keychain_ref(profile)
    return ref.service == DEFAULT_SERVICE and ref.account == profile.name


def _keyring_set(service: str, account: str, secret: str) -> None:
    import keyring

    keyring.set_password(service, account, secret)


def _keyring_delete(service: str, account: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(service, account)


def store_secret(profile: AuthProfile, secret: str, writer: SecretWriter | None = None) -> None:
    """Keep a token or API key in the profile's own keychain item.

    Errors become an `AuthError` that names the profile, never the secret.
    """
    if not owns_keychain_item(profile):
        raise AuthError(f"Das Profil „{profile.name}“ verweist auf einen fremden Eintrag.")
    ref = keychain_ref(profile)
    try:
        (writer or _keyring_set)(ref.service, ref.account, secret)
    except Exception as exc:
        log.warning("Schlüsselbund, Schreiben: %s", type(exc).__name__)
        raise AuthError(
            f"Der Schlüsselbund nimmt das Secret von „{profile.name}“ nicht an. Erlaube den "
            "Zugriff und versuche es erneut."
        ) from None


def replace_secret(
    profile: AuthProfile,
    secret: str,
    reader: SecretReader | None = None,
    writer: SecretWriter | None = None,
) -> None:
    """Put a new secret on a profile without ever losing the old one.

    `keyring` deletes on macOS first and adds then (`set_generic_password`), so a failed add
    leaves nothing. The old secret is read before and written back at once. All of this runs
    in one call, meant for one worker thread: a cancelled caller cannot stop the way back.
    Raises `SecretNotReplaced` with what lies in the keychain afterwards.
    """
    if not owns_keychain_item(profile):
        raise AuthError(f"Das Profil „{profile.name}“ verweist auf einen fremden Eintrag.")
    old = read_secret(profile, reader)
    try:
        store_secret(profile, secret, writer)
        return
    except AuthError:
        pass
    present: bool | None
    try:
        if old is not None and read_secret(profile, reader) is None:
            with contextlib.suppress(AuthError):  # what counts is the read below
                store_secret(profile, old, writer)
        present = read_secret(profile, reader) is not None
    except AuthError:
        present = None
    del old  # the traceback of the error below keeps this frame alive
    name = profile.name
    text = {
        True: "Der alte Key liegt weiter im Schlüsselbund. Erlaube den Zugriff und versuche es "
        "erneut.",
        False: "Im Schlüsselbund liegt jetzt kein Key. Erlaube den Zugriff und füge den Key "
        "erneut ein.",
        None: "Ob noch ein Key im Schlüsselbund liegt, konnte arr nicht prüfen. Erlaube den "
        "Zugriff und teste das Profil.",
    }[present]
    raise SecretNotReplaced(f"Der neue Key von „{name}“ ließ sich nicht ablegen. {text}", present)


def delete_secret(profile: AuthProfile, deleter: SecretDeleter | None = None) -> bool:
    """Remove the profile's own keychain item; a foreign reference stays. True if arr tried."""
    if not owns_keychain_item(profile):
        log.info("Profil „%s“: fremder Schlüsselbund-Eintrag bleibt bestehen.", profile.name)
        return False
    ref = keychain_ref(profile)
    try:
        (deleter or _keyring_delete)(ref.service, ref.account)
    except Exception as exc:
        log.warning("Schlüsselbund, Löschen: %s", type(exc).__name__)
        raise AuthError(
            f"Der Schlüsselbund gibt das Secret von „{profile.name}“ nicht frei. Erlaube den "
            "Zugriff und versuche es erneut."
        ) from None
    return True


def is_billed(profile: AuthProfile) -> bool:
    """Whether costs of this profile are billed (API key) rather than estimated (docs/08 §5.0)."""
    return profile.kind in (AuthKind.CLAUDE_API_KEY, AuthKind.OPENAI_API_KEY)

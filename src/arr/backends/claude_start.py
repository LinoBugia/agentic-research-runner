"""Start program of the Claude CLI: the login reaches the CLI through a pipe, never an environment.

The start environment of a process stays readable for every program of the same user, also
from inside the sandbox (`KERN_PROCARGS2`, W3-06 §12), and Seatbelt cannot block that. So a
secret must never be in an environment, not even for a moment. `cli_path` of the SDK points to
a small shell file written by `start_program`; it runs `main` with arr's own interpreter:

1. `main` gets the profile name in `START_PROFILE_VARIABLE`, never the secret, and not in `argv`.
2. It reads the secret itself from the keychain and writes it into a pipe.
3. It replaces itself with the CLI (`exec`), which reads the descriptor named by
   `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR` or `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`.

No way leads back: if any step fails, the CLI does not start.

The program runs outside the sandbox with access to the keychain, so everything it consists of
must lie outside every write root of an agent (`refuse_writable`): the shell file, arr's
interpreter, this module and the CLI. `-I` keeps out `PYTHONPATH`, the working folder and the
user's site folder, and the interpreter's absolute path keeps out the run venv at the front of
the agent's `PATH`.

See docs/01-architektur.md §8 and docs/07-berechtigungen-und-freigaben.md §3.
"""

import contextlib
import hashlib
import importlib.util
import logging
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NoReturn

from arr.auth import (
    SECRET_VARIABLES,
    START_PROFILE_VARIABLE,
    AuthError,
    SecretReader,
    descriptor_variable,
    read_secret,
)
from arr.errors import ArrError
from arr.models import AuthProfile, ProfilesFile
from arr.resolve import load_yaml_model
from arr.settings import app_dir

log = logging.getLogger(__name__)

HANDOVER_FAILED = 78
"""Exit code of `main` when the CLI did not start (`EX_CONFIG`)."""

BIN_FOLDER = "bin"
PROFILES_FILE = "profiles.yaml"
VERSION_PROBE = ["-v"]
"""The SDK asks the CLI for its version with the inherited environment, before the real start."""

_ENTRY = "from arr.backends.claude_start import main; main()"


def bundled_cli() -> Path:
    """The CLI bundled with `claude-agent-sdk`, found without importing the SDK."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    if spec is None or spec.origin is None:
        raise ArrError("Die Claude-CLI fehlt: claude-agent-sdk ist nicht installiert.")
    return Path(spec.origin).parent / "_bundled" / "claude"


def shim_text(entry: str = _ENTRY, python: str | None = None) -> str:
    """The shell file: arr's own interpreter by its absolute path, isolated from the rest."""
    interpreter = shlex.quote(python or sys.executable)
    return f'#!/bin/sh\nexec {interpreter} -I -c {shlex.quote(entry)} "$@"\n'


def _within(path: Path, root: Path) -> bool:
    """Whether `path` lies below `root`; APFS ignores case by default, so compare without it."""
    resolved, top = (os.path.realpath(p).casefold() for p in (path, root))
    return resolved == top or resolved.startswith(top.rstrip(os.sep) + os.sep)


def program_parts(shim: Path) -> list[Path]:
    """Everything that runs outside the sandbox with access to the keychain."""
    package = Path(__file__).parents[1]
    return [shim, Path(sys.executable), package, Path(sys.prefix), bundled_cli()]


def refuse_writable(shim: Path, write_roots: Iterable[Path]) -> None:
    """Raise `ArrError` if an agent could change the start program: no agent starts then."""
    roots = list(write_roots)
    for part in program_parts(shim):
        if any(_within(part, root) or _within(root, part) for root in roots):
            raise ArrError(
                "Der Claude-Agent startet nicht: Ein Teil des Startprogramms liegt in einem "
                "Ordner, in den Agenten schreiben dürfen. Lege den Workfolder außerhalb der "
                "arr-Installation und außerhalb von ~/.arr an."
            )


def _is_current(path: Path, text: str) -> bool:
    """Whether `path` is a plain file of this user with `text`, only for this user."""
    try:
        info = os.lstat(path)
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700
            and path.read_text(encoding="utf-8") == text
        )
    except OSError:
        return False


def start_program(write_roots: Iterable[Path] = ()) -> Path:
    """Path of the shell file for `cli_path`, written once per interpreter into `~/.arr/bin`."""
    text = shim_text()
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    folder = app_dir() / BIN_FOLDER
    shim = folder / f"claude-start-{digest}"
    refuse_writable(shim, write_roots)
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _is_current(shim, text):
            handle, temp = tempfile.mkstemp(dir=folder, prefix=".claude-start-")
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(text)
            os.chmod(temp, 0o700)
            os.replace(temp, shim)
    except OSError as exc:
        log.warning("Startprogramm: %s", type(exc).__name__)
        raise ArrError(
            "Der Claude-Agent startet nicht: Das Startprogramm ließ sich nicht anlegen. Prüfe den "
            "arr-Ordner und versuche es erneut."
        ) from None
    return shim


def _profile(name: str) -> AuthProfile:
    profiles = load_yaml_model(app_dir() / PROFILES_FILE, ProfilesFile)
    for profile in profiles.profiles:
        if profile.name == name:
            return profile
    raise AuthError(f"Das Profil „{name}“ gibt es nicht.")


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment of the CLI: no profile name, and no secret that came along by mistake."""
    drop = {START_PROFILE_VARIABLE, *SECRET_VARIABLES}
    return {key: value for key, value in environ.items() if key not in drop}


def _pipe_with(secret: str) -> int:
    """A pipe that holds `secret` and is closed for writing; the end stays open across `exec`."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, secret.encode())
    finally:
        os.close(write_end)
    os.set_inheritable(read_end, True)
    return read_end


def _hand_over(name: str, reader: SecretReader | None, environ: dict[str, str]) -> None:
    """Put the secret of `name` into a pipe and name the descriptor in `environ`."""
    profile = _profile(name)
    if profile.disabled:
        raise AuthError(f"Das Profil „{name}“ ist gesperrt.")
    variable = descriptor_variable(profile)
    secret = read_secret(profile, reader)
    if not secret:
        raise AuthError(f"Für das Profil „{name}“ liegt kein Geheimnis im Schlüsselbund.")
    environ[variable] = str(_pipe_with(secret))


def _stop(name: str | None, cause: str) -> NoReturn:
    who = f"von „{name}“ " if name else ""
    print(
        f"Der Login {who}ließ sich nicht an die Claude-CLI übergeben ({cause}). "
        "Der Agent startet nicht.",
        file=sys.stderr,
    )
    sys.exit(HANDOVER_FAILED)


def main(reader: SecretReader | None = None) -> NoReturn:
    """Entry of the shell file; `reader` only for tests, the real one is the keychain."""
    arguments = sys.argv[1:]
    name = os.environ.get(START_PROFILE_VARIABLE)
    environ = child_environment(os.environ)
    if not name and arguments != VERSION_PROBE:
        _stop(None, "kein Profil genannt")
    try:
        cli = str(bundled_cli())
        if name:
            _hand_over(name, reader, environ)
    except ArrError as exc:
        _stop(name, str(exc))
    except Exception as exc:  # noqa: BLE001 - the type only, a message could quote a value
        _stop(name, type(exc).__name__)
    with contextlib.suppress(OSError):
        os.execve(cli, [cli, *arguments], environ)
    _stop(name, "die CLI ließ sich nicht starten")

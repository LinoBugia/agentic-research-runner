"""`PATH` of the processes that run outside the sandbox (W3-06.3, B1).

The Claude CLI and the Codex app-server run outside the sandbox and start programs through
their own `PATH`: `git`, `security`, `ps`, `npm` and the `env` around every sandboxed command.
An agent writes into its write roots, so a program there would run without the sandbox. No
entry of that `PATH` may therefore lie in or above a write root, and none may be relative (it
would point into the working folder). The agent's commands still get the whole `PATH` with
the run venv first: Claude through `CLAUDE_ENV_FILE`, Codex through `shell_env`.

No `git` runs outside the sandbox at all (W3-06.4): a repo, a filter or an attributes file of
an agent would otherwise run under `git` of the CLI or the app-server. Their `PATH` starts with
`no_git_folder()`, whose `git` does nothing and fails; the agent's shell keeps the real one.
Measured with the pinned versions: both look `git` up on their `PATH`, none by a fixed path.

See docs/07-berechtigungen-und-freigaben.md §3.
"""

import logging
import os
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path

from arr.errors import ArrError
from arr.settings import app_dir

log = logging.getLogger(__name__)

NO_GIT_FOLDER = ("bin", "no-git")
"""Folder of the `git` stub below `app_dir()` (`~/.arr`, never a write root)."""

NO_GIT_TEXT = "#!/bin/sh\n# arr: no git outside the sandbox (W3-06.4)\nexit 1\n"


def within(path: Path | str, root: Path | str) -> bool:
    """Whether `path` lies below `root`; APFS ignores case by default, so compare without it."""
    resolved, top = (os.path.realpath(p).casefold() for p in (path, root))
    return resolved == top or resolved.startswith(top.rstrip(os.sep) + os.sep)


def is_unsafe(entry: str, write_roots: Iterable[Path]) -> bool:
    """Whether a program in `entry` could come from an agent: relative, or in or above a root.

    "In" also before resolving (W3-06.4, N2): a link in a write root counts as the root even
    when it points outside, since the agent can turn it later. So every prefix is resolved.
    """
    if not os.path.isabs(entry):
        return True
    parts = Path(entry).parts
    prefixes = [str(Path(*parts[:i])) for i in range(2, len(parts) + 1)]
    return any(
        within(root, entry) or any(within(prefix, root) for prefix in prefixes)
        for root in write_roots
    )


def outside_path(entries: Iterable[str], write_roots: Iterable[Path]) -> str:
    """`entries` without the unsafe ones (`is_unsafe`), as a `PATH` value."""
    roots = list(write_roots)
    kept = [entry for entry in entries if entry and not is_unsafe(entry, roots)]
    return os.pathsep.join(dict.fromkeys(kept))


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


def no_git_folder(write_roots: Iterable[Path]) -> Path:
    """The folder with the `git` stub, written if missing or changed; first on the `PATH` of
    every process outside the sandbox. Raises `ArrError` if it lies in or above a write root."""
    folder = app_dir().joinpath(*NO_GIT_FOLDER)
    if is_unsafe(str(folder), write_roots):
        raise ArrError(
            "Der Agent startet nicht: Der arr-Ordner liegt in einem Ordner, in den Agenten "
            "schreiben dürfen. Lege den Workfolder außerhalb von ~/.arr an."
        )
    stub = folder / "git"
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _is_current(stub, NO_GIT_TEXT):
            handle, temp = tempfile.mkstemp(dir=folder, prefix=".git-")
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(NO_GIT_TEXT)
            os.chmod(temp, 0o700)
            os.replace(temp, stub)
    except OSError as exc:
        log.warning("git-Stub: %s", type(exc).__name__)
        raise ArrError(
            "Der Agent startet nicht: arr konnte sein Ersatz-git in ~/.arr/bin nicht anlegen. "
            "Prüfe den arr-Ordner und versuche es erneut."
        ) from None
    return folder

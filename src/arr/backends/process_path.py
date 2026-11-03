"""`PATH` of the processes that run outside the sandbox (W3-06.3, B1).

The Claude CLI and the Codex app-server run outside the sandbox and start programs through
their own `PATH`: `git`, `security`, `ps`, `npm` and the `env` around every sandboxed command.
An agent writes into its write roots, so a program there would run without the sandbox. No
entry of that `PATH` may therefore lie in or above a write root, and none may be relative (it
would point into the working folder). The agent's commands still get the whole `PATH` with
the run venv first: Claude through `CLAUDE_ENV_FILE`, Codex through `shell_env`.
"""

import os
from collections.abc import Iterable
from pathlib import Path


def within(path: Path | str, root: Path | str) -> bool:
    """Whether `path` lies below `root`; APFS ignores case by default, so compare without it."""
    resolved, top = (os.path.realpath(p).casefold() for p in (path, root))
    return resolved == top or resolved.startswith(top.rstrip(os.sep) + os.sep)


def is_unsafe(entry: str, write_roots: Iterable[Path]) -> bool:
    """Whether a program in `entry` could come from an agent: relative, or in or above a root."""
    if not os.path.isabs(entry):
        return True
    return any(within(entry, root) or within(root, entry) for root in write_roots)


def outside_path(entries: Iterable[str], write_roots: Iterable[Path]) -> str:
    """`entries` without the unsafe ones (`is_unsafe`), as a `PATH` value."""
    roots = list(write_roots)
    kept = [entry for entry in entries if entry and not is_unsafe(entry, roots)]
    return os.pathsep.join(dict.fromkeys(kept))

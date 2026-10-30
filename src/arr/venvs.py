"""Where base venvs and run venvs live: outside `~` (W3-00, docs/07 §3 ①, variant 2).

The Codex permission profile denies `~` as a whole, and a `deny` on a folder also denies its
own metadata, so no Python whose program lies below `~` starts there. Base venvs and run
venvs therefore live below one root outside `~`:

- macOS: `/Users/Shared/arr-<user>/` (user decision of 29.10.), other systems
  `/var/tmp/arr-<user>/` (like `/Users/Shared`: outside `~`, kept across reboots, writable
  for every user with the sticky bit). `$ARR_VENV_ROOT` overrides the root (tests, Linux).
- `venvs/<name>/` holds base venvs, `runs/<run-id>/` the run venvs.

The root and `runs/` are created with mode 0700. An existing one must be a real folder (no
symlink) of the current user that neither group nor others may write to; anything else
raises `ArrError` with a German message. In a shared folder such as `/Users/Shared` another
user could otherwise put a folder or link in its place.

This module is the only place that builds these paths; everyone else asks it (or
`RunPaths.venv_dir`).
"""

import os
import pwd
import stat
import sys
from pathlib import Path

from arr.errors import ArrError

__all__ = [
    "VENV_ROOT_ENV",
    "base_venv_dir",
    "default_venv_root",
    "ensure_run_venvs_dir",
    "ensure_venv_root",
    "glob_safe_name",
    "run_venv_dir",
    "venv_base_prefix",
    "venv_root",
]

VENV_ROOT_ENV = "ARR_VENV_ROOT"


def _user_name() -> str:
    """Login name of the current user (from the user database, not from `$USER`)."""
    return pwd.getpwuid(os.getuid()).pw_name


def default_venv_root() -> Path:
    """`/Users/Shared/arr-<user>` on macOS, `/var/tmp/arr-<user>` elsewhere."""
    shared = Path("/Users/Shared") if sys.platform == "darwin" else Path("/var/tmp")
    return shared / f"arr-{_user_name()}"


def venv_root() -> Path:
    """Absolute root of the venvs: `$ARR_VENV_ROOT` if set, else `default_venv_root()`.

    A relative `$ARR_VENV_ROOT` is made absolute against the current directory. Nothing is
    created or checked here (see `ensure_venv_root`).
    """
    override = os.environ.get(VENV_ROOT_ENV)
    if override:
        return Path(override).expanduser().absolute()
    return default_venv_root()


def _name(name: str, what: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"invalid {what}: {name!r}")
    return name


_GLOB_UNSAFE = frozenset("[]\\!^{}*?,")


def glob_safe_name(name: str) -> bool:
    """Whether the Codex profile can exclude every other name next to `name` by glob.

    Only ASCII without glob characters: Seatbelt compares in NFD, so `[!é]` acts like
    `[!e]` (W3-00.1 M1). Run IDs always pass; base venv names are free to choose.
    """
    return name.isascii() and not (_GLOB_UNSAFE & set(name))


def run_venv_dir(run_id: str) -> Path:
    """Run venv of a run: `<root>/runs/<run-id>/` (path only, nothing created)."""
    return venv_root() / "runs" / _name(run_id, "run id")


def base_venv_dir(name: str) -> Path:
    """Base venv with this name: `<root>/venvs/<name>/` (path only, nothing created)."""
    return venv_root() / "venvs" / _name(name, "venv name")


def _ensure_private_dir(path: Path) -> Path:
    """Create `path` with mode 0700, or check an existing one (module docstring)."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        raise ArrError(
            f"Der Ordner {path.parent} fehlt, deshalb kann arr den Ordner für die "
            f"Python-Umgebungen ({path}) nicht anlegen. Lege {path.parent} an oder setze "
            f"{VENV_ROOT_ENV} auf einen anderen Ordner außerhalb deines Benutzerordners."
        ) from None
    except OSError as exc:
        raise ArrError(
            f"Der Ordner für die Python-Umgebungen {path} lässt sich nicht anlegen "
            f"({exc.strerror}). Prüfe die Rechte oder setze {VENV_ROOT_ENV} auf einen "
            "anderen Ordner außerhalb deines Benutzerordners."
        ) from None
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ArrError(
            f"Der Ordner für die Python-Umgebungen {path} ist nicht lesbar ({exc.strerror})."
        ) from None
    problem = ""
    if stat.S_ISLNK(info.st_mode):
        problem = "ist ein symbolischer Link"
    elif not stat.S_ISDIR(info.st_mode):
        problem = "ist kein Ordner"
    elif info.st_uid != os.getuid():
        problem = "gehört einem anderen Benutzer"
    elif info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ArrError(
            f"Der Ordner für die Python-Umgebungen {path} ist für Gruppe oder andere "
            f"beschreibbar. Andere Benutzer könnten dort Programme ablegen, die deine Agenten "
            f"ausführen. Entziehe die Rechte mit „chmod 700 {path}“ und starte den Run neu."
        )
    if problem:
        raise ArrError(
            f"Der Ordner für die Python-Umgebungen {path} {problem}. arr nutzt ihn deshalb "
            "nicht, weil ihn sonst ein anderer Benutzer kontrollieren könnte. Entferne oder "
            f"benenne ihn um (arr legt ihn dann neu an) oder setze {VENV_ROOT_ENV} auf einen "
            "eigenen Ordner außerhalb deines Benutzerordners."
        )
    return path


def ensure_venv_root() -> Path:
    """Create or check the root (mode 0700, own folder, no symlink); returns it."""
    return _ensure_private_dir(venv_root())


def ensure_run_venvs_dir() -> Path:
    """Create or check the root and its `runs/` the same way; returns `runs/`."""
    return _ensure_private_dir(ensure_venv_root() / "runs")


def venv_base_prefix(venv: Path) -> Path | None:
    """Prefix of the Python installation a venv is built on (`home` in `pyvenv.cfg`)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "home" and value.strip():
            return Path(value.strip()).parent
    return None

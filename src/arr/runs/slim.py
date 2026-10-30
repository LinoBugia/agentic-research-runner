"""Slimming a finished run (docs/02 §9, UX task E10).

Removes what can be rebuilt or is not needed to read a run: the working copies
(`agents/*/work/`), `shared/`, `base/` and the run venv. Transcripts, raw events, diffs, group
chats, output and usage stay. This is the one place of W3-02 that deletes data, so:

- What goes comes from the fixed relative paths below, never from input or `meta.json`.
- The run venv goes only if `run_venv_location` names `<root>/runs/<run-id>` of this run or the
  old `<run>/.venv`; any other path in `meta.json` deletes nothing (W3-00 review G3).
- Nothing is followed through a symlink: a link at a target is removed as a link, a linked
  `agents` or `agents/<id>` is left alone, and `rmtree` does not descend into links.
- Only a finished run without a held `run.lock` is slimmed, and `slim_run` holds that lock
  while it deletes.

`slimmed.json` in the run folder marks the end; a slim that failed halfway has no mark and can
be repeated.
"""

import json
import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from arr.errors import ArrError
from arr.runs.inspect import RunLock, run_lock_held
from arr.runs.prepare import load_run_meta, run_venv_location
from arr.venvs import run_venv_dir

__all__ = ["SLIM_MARKER", "SlimPlan", "check_slimmable", "plan_slim", "slim_run"]

log = logging.getLogger("arr.runs.slim")

SLIM_MARKER = "slimmed.json"
_RUN_DIRS = ("base", "shared")
_AGENTS = "agents"
_WORK = "work"
_LEGACY_VENV = ".venv"


@dataclass(frozen=True, slots=True)
class SlimPlan:
    """What `slim_run` would remove; `logical_bytes` is the sum of the file sizes."""

    targets: tuple[Path, ...]
    venv: Path | None
    logical_bytes: int


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _targets(run_dir: Path) -> list[Path]:
    """The fixed paths that exist, as real folders or as links at their place."""
    candidates = [run_dir / name for name in _RUN_DIRS]
    agents = run_dir / _AGENTS
    if _real_dir(agents):
        with os.scandir(agents) as entries:
            names = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
        candidates += [agents / name / _WORK for name in names]
    return [p for p in candidates if p.is_symlink() or p.is_dir()]


def _venv_target(run_dir: Path) -> Path | None:
    """The run venv if it lies where a venv of this run belongs, else None (module docstring)."""
    location = run_venv_location(run_dir)
    if location is None:
        return None
    if location == run_dir / _LEGACY_VENV:
        return location if _real_dir(location) else None
    expected = run_venv_dir(run_dir.name)
    same_parent = os.path.realpath(location.parent) == os.path.realpath(expected.parent)
    if location.name != expected.name or not same_parent:
        log.warning("Run-venv von %s liegt nicht am erwarteten Ort, bleibt", run_dir.name)
        return None
    return location if _real_dir(location) else None


def _logical_size(path: Path) -> int:
    """Sum of the file sizes below `path`; links count as themselves and are not followed."""
    if path.is_symlink():
        return 0
    total = 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


def _check_state(folder: Path) -> None:
    try:
        status = load_run_meta(folder).status
    except (OSError, ValueError):
        raise ArrError(f"Run-Ordner nicht lesbar: meta.json fehlt in {folder.name}.") from None
    if not status.is_final:
        raise ArrError("Aktive Runs lassen sich nicht verschlanken. Stoppe den Run zuerst.")
    if (folder / SLIM_MARKER).is_file():
        raise ArrError("Der Run ist schon verschlankt.")


def check_slimmable(run_dir: str | Path) -> None:
    """`ArrError` with the German reason when the run may not be slimmed."""
    folder = Path(run_dir)
    _check_state(folder)
    if run_lock_held(folder):
        raise ArrError(
            "Der Run wird gerade noch abgeschlossen. Versuche es in ein paar Sekunden noch einmal."
        )


def plan_slim(run_dir: str | Path) -> SlimPlan:
    """What would go; nothing changes."""
    folder = Path(run_dir)
    targets = _targets(folder)
    venv = _venv_target(folder)
    size = sum(_logical_size(p) for p in targets)
    if venv is not None:
        size += _logical_size(venv)
    return SlimPlan(tuple(targets), venv, size)


def _open_up(root: Path) -> None:
    """Make the folders below `root` writable: `rmtree` fails on read-only ones (caches)."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            os.chmod(folder, stat.S_IMODE(os.lstat(folder).st_mode) | stat.S_IRWXU)
            with os.scandir(folder) as entries:
                stack += [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue


def _remove(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    _open_up(path)
    shutil.rmtree(path)


def slim_run(run_dir: str | Path) -> SlimPlan:
    """Slim a finished run; returns what was removed. `ArrError` if it may not or failed."""
    folder = Path(run_dir)
    if folder.is_symlink():
        raise ArrError(f"Der Run-Ordner {folder.name} ist ein Link und wird nicht verschlankt.")
    check_slimmable(folder)
    lock = RunLock(folder)
    lock.acquire()
    try:
        _check_state(folder)
        plan = plan_slim(folder)
        try:
            for path in plan.targets:
                _remove(path)
            if plan.venv is not None:
                _remove(plan.venv)
        except OSError as exc:
            raise ArrError(
                f"Der Run {folder.name} wurde nicht ganz verschlankt: {exc.strerror or exc}. "
                "Transkripte und Chat sind unverändert, du kannst es noch einmal versuchen."
            ) from None
        marker = {
            "version": 1,
            "ts": time.time(),
            "logical_bytes": plan.logical_bytes,
            "removed": [str(p.relative_to(folder)) for p in plan.targets],
            "run_venv": plan.venv is not None,
        }
        (folder / SLIM_MARKER).write_text(json.dumps(marker, indent=1) + "\n", encoding="utf-8")
        return plan
    finally:
        lock.release()

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
  while it deletes. A link at `run.lock` or `slimmed.json` refuses the run: nothing is written
  through a link either.

`slimmed.json` in the run folder marks the end; a slim that failed halfway has no mark and can
be repeated.
"""

import errno
import json
import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from arr.errors import ArrError
from arr.runs.inspect import RUN_LOCK, RunLock, run_lock_held
from arr.runs.prepare import load_run_meta, run_venv_location
from arr.venvs import run_venv_dir

__all__ = ["SLIM_MARKER", "SlimPlan", "check_slimmable", "plan_slim", "slim_run"]

log = logging.getLogger("arr.runs.slim")

SLIM_MARKER = "slimmed.json"
_RUN_DIRS = ("base", "shared")
_AGENTS = "agents"
_WORK = "work"
_LEGACY_VENV = ".venv"
_DIFF = "changes.diff"
_KEPT = (
    ("Transkripte", "agents/*/transcript.jsonl"),
    ("Roh-Events", "agents/*/raw.jsonl"),
    ("Diffs", f"agents/*/{_DIFF}"),
    ("Gruppenchats", "groupchats/room.sqlite"),
    ("Output", "output/**/*"),
    ("Usage", "usage.json"),
)
"""What stays, with the German name for the dialog; only files that are really there count."""
_REASONS = {
    errno.EACCES: "keine Berechtigung",
    errno.EPERM: "keine Berechtigung",
    errno.EROFS: "das Laufwerk ist schreibgeschützt",
}


@dataclass(frozen=True, slots=True)
class SlimPlan:
    """What `slim_run` would remove; `logical_bytes` is the sum of the file sizes.

    `kept` names what stays. `unsaved`: a working copy or a shared folder goes without a diff,
    because the run was not finalized, so its work exists nowhere else.
    """

    targets: tuple[Path, ...]
    venv: Path | None
    logical_bytes: int
    kept: tuple[str, ...] = ()
    unsaved: bool = False


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


def _refuse_links(folder: Path) -> None:
    for name in (RUN_LOCK, SLIM_MARKER):
        if (folder / name).is_symlink():
            raise ArrError(
                f"Der Run {folder.name} wurde nicht verschlankt: {name} ist ein Link. Vermutlich "
                "wurde der Ordner außerhalb von arr verändert. Es wurde nichts gelöscht."
            )


def _reason(exc: OSError) -> str:
    return _REASONS.get(exc.errno or 0, "der Zugriff auf eine Datei ging nicht")


def _unreadable(folder: Path, exc: OSError) -> ArrError:
    return ArrError(
        f"Der Run {folder.name} wurde nicht verschlankt: {_reason(exc)}. Es wurde nichts "
        "gelöscht. Prüfe die Rechte des Run-Ordners und versuche es noch einmal."
    )


def _unsaved(folder: Path, targets: list[Path]) -> bool:
    """True if work goes that no `changes.diff` holds; members of a shared folder share it."""
    if any(p.name == _WORK and not (p.parent / _DIFF).is_file() for p in targets):
        return True
    shared = folder / "shared"
    if not _real_dir(shared) or not any(shared.iterdir()):
        return False
    agents = [p for p in (folder / _AGENTS).glob("*") if _real_dir(p)]
    return not agents or any(not (p / _DIFF).is_file() for p in agents)


def check_slimmable(run_dir: str | Path) -> None:
    """`ArrError` with the German reason when the run may not be slimmed."""
    folder = Path(run_dir)
    _refuse_links(folder)
    _check_state(folder)
    if run_lock_held(folder):
        raise ArrError(
            "Der Run wird gerade noch abgeschlossen. Versuche es in ein paar Sekunden noch einmal."
        )


def plan_slim(run_dir: str | Path) -> SlimPlan:
    """What would go and what stays; nothing changes."""
    folder = Path(run_dir)
    try:
        targets = _targets(folder)
        venv = _venv_target(folder)
        size = sum(_logical_size(p) for p in targets)
        if venv is not None:
            size += _logical_size(venv)
        kept = tuple(name for name, found in _KEPT if any(p.is_file() for p in folder.glob(found)))
        unsaved = _unsaved(folder, targets)
    except OSError as exc:
        raise _unreadable(folder, exc) from None
    return SlimPlan(tuple(targets), venv, size, kept, unsaved)


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


def _write_marker(folder: Path, marker: dict[str, object]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(folder / SLIM_MARKER, flags, 0o644), "w", encoding="utf-8") as file:
        file.write(json.dumps(marker, indent=1) + "\n")


def slim_run(run_dir: str | Path) -> SlimPlan:
    """Slim a finished run; returns what was removed. `ArrError` if it may not or failed."""
    folder = Path(run_dir)
    if folder.is_symlink():
        raise ArrError(f"Der Run-Ordner {folder.name} ist ein Link und wird nicht verschlankt.")
    check_slimmable(folder)
    lock = RunLock(folder)
    try:
        lock.acquire()
    except OSError as exc:
        raise _unreadable(folder, exc) from None
    try:
        _check_state(folder)
        plan = plan_slim(folder)
        try:
            for path in plan.targets:
                _remove(path)
            if plan.venv is not None:
                _remove(plan.venv)
            _write_marker(
                folder,
                {
                    "version": 1,
                    "ts": time.time(),
                    "logical_bytes": plan.logical_bytes,
                    "removed": [str(p.relative_to(folder)) for p in plan.targets],
                    "run_venv": plan.venv is not None,
                },
            )
        except OSError as exc:
            raise ArrError(
                f"Der Run {folder.name} wurde nicht ganz verschlankt: {_reason(exc)}. "
                "Transkripte und Chat sind unverändert, du kannst es noch einmal versuchen."
            ) from None
        return plan
    finally:
        lock.release()

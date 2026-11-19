"""Moving, emptying and deleting agents' folders: the one place for it (W4b-05a).

A fresh working folder (docs/10 §4.12, Entscheidung 23) keeps its directory: the Claude CLI
keeps its working directory for the whole session and the agent's room server runs with the
working folder as `cwd`. So `fresh_folder`

1. files the old content away to `retired/<id>/<nr>-<phase>/`, entry by entry with `rename`
   on the same file system: no link is followed, no content is read. An entry the agent made
   immovable (no write right on a folder, a user flag such as `uchg`) gets its rights or flag
   back and is moved again; the moves run a few rounds while a process writes on. If anything
   is left, `ArrError` names it: never a half old folder.
2. empties the scratch folder `scratch/<id>/` the same way into `retired/.trash/` and deletes
   that. A rest that cannot be deleted stays there, closed for every agent, until slimming.
3. fills the working folder in place from the run folder (`prepare.refill_work_dir`).

`retired/` is closed for every agent, also for the one that worked there, with no approval
path (`arr.permissions.policy`). The finalizer writes one diff per filed-away folder next to
`changes.diff` (`agents/<id>/retired-<nr>-<phase>.diff`); slimming deletes the folders, not
the diffs.

Nothing here knows the processes of an agent. What arr knows as its own child (the CLI, the
app-server) is ended by the session's own `reset_session`, which the runner calls first
(Entscheidung 48). A detached process keeps a handle or its working directory in a moved
folder and can go on reading there; it cannot reach `retired/` by a new path (sandbox).
Welle 5 replaces the file operations here by a helper under the agents' own account
(W5-00 §6 Nr. 2, 4).
"""

import contextlib
import errno
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.errors import ArrError
from arr.models import WorkspaceMode
from arr.permissions.policy import RETIRED_DIR, SCRATCH_DIR
from arr.room.store import STAGING_DIR, SUBMISSIONS_DIR, _folder_part
from arr.room.types import Submission
from arr.runs.prepare import PreparedAgent, make_private_dir, refill_work_dir

__all__ = [
    "MOVE_ROUNDS",
    "RETIRED_DIFF_PREFIX",
    "TRASH_DIR",
    "Cleanup",
    "FreshFolder",
    "clean_up_run",
    "fresh_folder",
    "remove_path",
    "retired_diff_name",
    "retired_folders",
]

MOVE_ROUNDS = 5
"""Rounds of moving out of a folder before arr gives up (a process may write meanwhile)."""

ROUND_PAUSE_S = 0.05
"""Pause before each further round."""

TRASH_DIR = ".trash"
"""`retired/.trash/`: the content of emptied scratch folders on its way out."""

RETIRED_DIFF_PREFIX = "retired-"
"""`agents/<id>/retired-<folder>.diff`: the diff of a filed-away folder against `base/`."""

_NUMBER_RE = re.compile(r"^(\d+)-")
_CLEARABLE_FLAGS = stat.UF_IMMUTABLE | stat.UF_APPEND | stat.UF_NOUNLINK
"""User flags that keep the owner from moving or deleting an entry; the owner may clear them."""
_STAGING_RESTS = (".submission-", ".orphan-", ".staging-")
_LOCK_RE = re.compile(r"^\.version-.+\.lock$")


@dataclass(slots=True)
class FreshFolder:
    """What `fresh_folder` did, for the entry `folder_fresh` in `orchestrator.jsonl`."""

    retired: str
    """Where the old content lies now, relative to the run folder."""
    moved: int = 0
    rounds: int = 1
    extra: list[str] = field(default_factory=list)
    """Further folders of later rounds (something was written while arr moved)."""
    scratch_removed: int = 0
    scratch_left: list[str] = field(default_factory=list)
    """Entries of the emptied scratch folder arr could not delete (they lie in `retired/`)."""
    generated: list[str] = field(default_factory=list)

    def log_data(self) -> dict[str, Any]:
        return {
            "retired": self.retired,
            "moved": self.moved,
            "rounds": self.rounds,
            "extra": self.extra,
            "scratch_removed": self.scratch_removed,
            "scratch_left": self.scratch_left,
        }


# --------------------------------------------------------------------------------------------
# Moving every entry out of a folder
# --------------------------------------------------------------------------------------------


def _clear(path: Path) -> bool:
    """Give the owner back what keeps it from moving or deleting `path` itself (never through
    a link): write and search rights on a folder, user flags. True if something changed."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    changed = False
    if info.st_flags & _CLEARABLE_FLAGS:
        with contextlib.suppress(OSError):
            os.chflags(path, info.st_flags & ~_CLEARABLE_FLAGS, follow_symlinks=False)
            changed = True
    if stat.S_ISDIR(info.st_mode) and (info.st_mode & stat.S_IRWXU) != stat.S_IRWXU:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IMODE(info.st_mode) | stat.S_IRWXU, follow_symlinks=False)
            changed = True
    return changed


def _names(folder: Path) -> list[str]:
    """The entries of `folder` (names only, nothing is opened below it)."""
    try:
        return sorted(os.listdir(folder))
    except PermissionError:
        _clear(folder)
        return sorted(os.listdir(folder))


def _move_one(source: Path, target: Path, name: str) -> bool:
    """Move `source/name` to `target/name` by `rename` (no link followed); False if gone."""
    for attempt in (1, 2):
        try:
            os.rename(source / name, target / name)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if attempt == 2 or exc.errno not in (errno.EACCES, errno.EPERM):
                raise
            # rights or flags of the entry or of the folder itself (both owned by this user)
            fixed = _clear(source / name)
            if not (_clear(source) or fixed):
                raise
    return False


def _move_out(source: Path, make_target: Callable[[int], Path]) -> tuple[int, list[Path]]:
    """Move every entry of `source` into folders from `make_target(round)`; `source` stays.

    Up to `MOVE_ROUNDS` rounds (a process may write meanwhile). Raises `ArrError` with the path
    if an entry stays. Returns the number moved and the target folders used.
    """
    moved, targets = 0, []
    failure: tuple[str, OSError] | None = None
    for round_no in range(1, MOVE_ROUNDS + 1):
        try:
            names = _names(source)
        except OSError as exc:
            raise _left_behind(source, None, exc) from None
        if not names:
            return moved, targets
        if round_no > 1:
            time.sleep(ROUND_PAUSE_S)
        target = make_target(round_no)
        targets.append(target)
        for name in names:
            try:
                moved += _move_one(source, target, name)
            except OSError as exc:
                failure = (name, exc)
    try:
        rest = _names(source)
    except OSError as exc:
        raise _left_behind(source, None, exc) from None
    if not rest:
        return moved, targets
    name = failure[0] if failure and failure[0] in rest else rest[0]
    raise _left_behind(source, name, failure[1] if failure and failure[0] == name else None)


def _left_behind(folder: Path, name: str | None, exc: OSError | None) -> ArrError:
    where = folder / name if name else folder
    why = f" ({os.strerror(exc.errno)})" if exc is not None and exc.errno else ""
    return ArrError(
        f"arr konnte {where} nicht ablegen{why}. Der Ordner ist nicht frisch, deshalb geht der "
        "Run nicht weiter. Vermutlich hält ein Prozess des Agenten die Datei oder schreibt "
        "noch in den Ordner, oder die Datei ist gesperrt. Prüfe den Ordner und starte den Run "
        "neu."
    )


# --------------------------------------------------------------------------------------------
# Fresh working folder
# --------------------------------------------------------------------------------------------


def retired_root(run_dir: Path) -> Path:
    return run_dir / RETIRED_DIR


def _next_number(folder: Path) -> int:
    numbers = [int(m.group(1)) for n in _names(folder) if (m := _NUMBER_RE.match(n))]
    return max(numbers, default=0) + 1


def _retire_targets(agent_root: Path, name: str) -> Callable[[int], Path]:
    def make(round_no: int) -> Path:
        folder = agent_root / (name if round_no == 1 else f"{name}~{round_no}")
        os.mkdir(folder, 0o700)
        return folder

    return make


def _trash(run_dir: Path) -> Path:
    root = make_private_dir(make_private_dir(retired_root(run_dir)) / TRASH_DIR)
    return make_private_dir(root / uuid.uuid4().hex)


def empty_folder(folder: Path, run_dir: Path) -> tuple[int, list[str]]:
    """Empty `folder` in place: its entries go to `retired/.trash/` by `rename` (the agent
    sees an empty folder at once), then arr deletes them there. Raises `ArrError` if an entry
    cannot be moved. Returns the number moved and what could not be deleted (stays in
    `retired/`, closed for every agent, until slimming)."""
    trash = _trash(run_dir)
    moved, _ = _move_out(folder, lambda n: trash if n == 1 else make_private_dir(trash / f"~{n}"))
    left = remove_quietly(trash)
    return moved, [_rel(run_dir, p) for p in left]


def fresh_folder(agent: PreparedAgent, *, stage: str) -> FreshFolder:
    """Give `agent` a fresh working folder in place (module docstring). Runs in a thread.

    Raises `ArrError` for an agent in a shared folder (`check` refuses `folder: fresh` there
    already; this is the second wall), when something old would stay and for any other error
    of the file system on the way (the folder is then not fresh).
    """
    try:
        return _fresh_folder(agent, stage=stage)
    except OSError as exc:
        where = exc.filename or agent.work_dir
        raise ArrError(
            f"arr konnte {agent.id} keinen frischen Arbeitsordner geben ({where}: "
            f"{os.strerror(exc.errno) if exc.errno else type(exc).__name__}). Der Ordner ist "
            "nicht frisch, deshalb geht der Run nicht weiter."
        ) from None


def _fresh_folder(agent: PreparedAgent, *, stage: str) -> FreshFolder:
    if agent.file.config.workspace.mode is WorkspaceMode.SHARED:
        raise ArrError(
            f"{agent.id} arbeitet in einem gemeinsamen Ordner. Ein gemeinsamer Ordner wird nie "
            "frisch, weil das den anderen Agenten die Arbeit wegnähme."
        )
    run_dir = agent.run_dir
    work = agent.work_dir
    if work.is_symlink() or not work.is_dir():
        raise ArrError(f"Der Arbeitsordner {work} fehlt oder ist kein Ordner.")
    agent_root = make_private_dir(make_private_dir(retired_root(run_dir)) / agent.id)
    name = f"{_next_number(agent_root):03d}-{_folder_part(stage, 'ohne-phase')}"
    moved, targets = _move_out(work, _retire_targets(agent_root, name))
    if not targets:  # an empty working folder: still a filed-away folder, for the diff
        targets.append(_retire_targets(agent_root, name)(1))
    done = FreshFolder(
        retired=targets[0].relative_to(run_dir).as_posix(),
        moved=moved,
        rounds=len(targets),
        extra=[t.relative_to(run_dir).as_posix() for t in targets[1:]],
    )
    scratch = make_private_dir(make_private_dir(run_dir / SCRATCH_DIR) / agent.id)
    done.scratch_removed, done.scratch_left = empty_folder(scratch, run_dir)
    done.generated = refill_work_dir(agent)
    return done


def retired_folders(run_dir: Path, agent_id: str) -> list[Path]:
    """The filed-away working folders of an agent, in order (real folders only)."""
    root = retired_root(run_dir) / agent_id
    if root.is_symlink() or not root.is_dir():
        return []
    with os.scandir(root) as entries:
        found = [Path(e.path) for e in entries if e.is_dir(follow_symlinks=False)]
    return sorted(p for p in found if _NUMBER_RE.match(p.name))


def retired_diff_name(folder: Path) -> str:
    return f"{RETIRED_DIFF_PREFIX}{folder.name}.diff"


# --------------------------------------------------------------------------------------------
# Deleting
# --------------------------------------------------------------------------------------------


def _open_up(root: Path) -> None:
    """Make the folders below `root` writable and clear user flags, never through a link:
    `rmtree` fails on read-only folders (caches) and on flagged entries."""
    stack = [root]
    while stack:
        folder = stack.pop()
        _clear(folder)
        try:
            with os.scandir(folder) as scan:
                entries = list(scan)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.stat(follow_symlinks=False).st_flags & _CLEARABLE_FLAGS:
                    _clear(Path(entry.path))
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
            except OSError:
                continue


def remove_path(path: Path) -> None:
    """Delete `path`: a link (or file) as itself, a folder with everything below it, never
    following a link (`shutil.rmtree` works with descriptors on macOS). Raises `OSError`."""
    if path.is_symlink() or not path.is_dir():
        try:
            os.unlink(path)
        except PermissionError:
            _clear(path)
            os.unlink(path)
        return
    _open_up(path)
    shutil.rmtree(path)


def remove_quietly(path: Path) -> list[str]:
    """`remove_path`, but what cannot be deleted is returned (paths), not raised."""
    left: list[str] = []
    if not os.path.lexists(path):
        return left
    try:
        remove_path(path)
    except OSError:
        _open_up(path)
        shutil.rmtree(path, onexc=lambda _f, p, _e: left.append(p))
    return left


# --------------------------------------------------------------------------------------------
# End of a run
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Cleanup:
    """What `clean_up_run` removed and what it could not (paths relative to the run)."""

    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _rel(run_dir: Path, path: str | Path) -> str:
    try:
        return Path(path).relative_to(run_dir).as_posix()
    except ValueError:
        return str(path)


def _staging_rests(staging: Path) -> Iterable[Path]:
    """Rests of submissions killed halfway (W4b-01.1): `.submission-<uuid>`, `.orphan-<uuid>`,
    `.staging-<uuid>` and empty `.version-<name>.lock` files."""
    if staging.is_symlink() or not staging.is_dir():
        return []
    found = []
    with os.scandir(staging) as entries:
        for entry in entries:
            if entry.name.startswith(_STAGING_RESTS):
                found.append(Path(entry.path))
            elif _LOCK_RE.match(entry.name):
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_size == 0:
                    found.append(Path(entry.path))
    return sorted(found)


def _final_name(sub: Submission) -> str:
    """The folder name a sealed submission gets in `output/submissions/` at its release
    (`RoomStore.submit`): such a folder is no orphan even before the row knows it."""
    phase = _folder_part(sub.stage, "ohne-phase")
    return f"{sub.id:03d}-{phase}-{_folder_part(sub.shown_as, 'agent')}"


def _orphans(run_dir: Path, submissions: Iterable[Submission]) -> list[Path]:
    """Folders in `output/submissions/` that no submission of the database names."""
    folder = run_dir / SUBMISSIONS_DIR
    if folder.is_symlink() or not folder.is_dir():
        return []
    known: set[str] = set()
    for sub in submissions:
        path = Path(sub.path)
        if path.parent.as_posix() == SUBMISSIONS_DIR:
            known.add(path.name)
        if sub.sealed and sub.name is None:
            known.add(_final_name(sub))
    with os.scandir(folder) as entries:
        return sorted(Path(e.path) for e in entries if e.name not in known)


def clean_up_run(run_dir: Path, submissions: Iterable[Submission] | None) -> Cleanup:
    """At the end of a run (finalizer, W4b-05a): delete `scratch/`, the rests of submissions in
    `groupchats/staging/` and folders in `output/submissions/` without a database row (moved
    to `groupchats/staging/` first). `submissions` None: no room, no orphans are looked for.
    Never anything below `groupchats/sealed/` (Entscheidung 36). Nothing raises: what cannot
    be deleted is listed in `Cleanup.failed`."""
    done = Cleanup()

    def remove(path: Path, shown: Path | None = None) -> None:
        left = remove_quietly(path)
        if left:
            done.failed += [_rel(run_dir, p) for p in left]
        else:
            done.removed.append(_rel(run_dir, shown or path))

    scratch = run_dir / SCRATCH_DIR
    if os.path.lexists(scratch):
        remove(scratch)
    staging = run_dir / STAGING_DIR
    try:
        for path in _staging_rests(staging):
            remove(path)
    except OSError:
        done.failed.append(STAGING_DIR)
    if submissions is None:
        return done
    try:
        orphans = _orphans(run_dir, submissions)
    except OSError:
        done.failed.append(SUBMISSIONS_DIR)
        return done
    for orphan in orphans:
        target = staging / f".orphan-{uuid.uuid4().hex}"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            os.rename(orphan, target)
        except OSError:
            done.failed.append(_rel(run_dir, orphan))
            continue
        remove(target, shown=orphan)
    return done

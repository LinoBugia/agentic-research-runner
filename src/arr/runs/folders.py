"""Moving, emptying and deleting agents' folders: the one place for it (W4b-05a, W4b-05a.1).

arr runs outside the sandbox. In a folder an agent can write to, a process of the agent may
swap any entry for a link at any moment. So here arr never writes, moves, deletes or changes
rights through a path whose parts an agent can swap (Entscheidung 56): it opens the agent's
folder once (`O_NOFOLLOW | O_DIRECTORY`, checked against the entry it found), then does
everything relative to that descriptor, never following a link; rights, user flags and ACLs
change only at entries reached that way (`fchmodat`, on macOS `setattrlistat`).

A fresh working folder (docs/10 §4.12, Entscheidung 23) keeps its directory: the Claude CLI
keeps its working directory for the whole session and the agent's room server runs with the
working folder as `cwd`. So `fresh_folder`

1. files the old content away to `retired/<id>/<nr>-<phase>/`, entry by entry with `renameat`
   relative to the folder's descriptor: no link is followed, no content is read. An entry the
   agent made immovable (rights of a folder, user flags, an ACL) gets them back and is moved
   again; the moves run a few rounds while a process writes on. If anything is left,
   `ArrError` names it.
2. empties the scratch folder `scratch/<id>/` the same way into `retired/.trash/` and deletes
   it there. A rest that cannot be deleted stays there, closed for every agent, until slimming.
3. removes extended attributes and the ACL of both folders themselves.
4. builds the fresh content in `retired/.fill-<uuid>/`, which no agent reaches
   (`prepare.refill_work_dir`, the same function as at the start of the run), and moves its
   top entries into the working folder with an exclusive rename: a name that lies there
   already is an `ArrError`, never overwritten.
5. compares the working folder (names and inodes of the top level) with what it put there,
   and the scratch folder with empty: anything else is an `ArrError` with the path.

What a surviving process writes after `fresh_folder` ended is an accepted limit
(Entscheidung 52), as are the handles it holds. `retired/` is closed for every agent, also
for the one that worked there, with no approval path (`arr.permissions.policy`). The
finalizer writes one diff per filed-away folder next to `changes.diff`
(`agents/<id>/retired-<nr>-<phase>.diff`) and parks `scratch/` and the rests of submissions
in `retired/.trash/` (`park_end_of_run`), where the secret scan of `arr run` still sees them;
`purge_trash` deletes them after the scan (Entscheidung 54). Slimming deletes the folders,
not the diffs.

Welle 5 replaces the file operations here by a helper under the agents' own account
(W5-00 §6 Nr. 2, 4).
"""

import contextlib
import ctypes
import ctypes.util
import errno
import os
import re
import stat
import struct
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.errors import ArrError
from arr.models import WorkspaceMode
from arr.permissions.policy import RETIRED_DIR, SCRATCH_DIR
from arr.room.store import STAGING_DIR, SUBMISSIONS_DIR, _folder_part
from arr.room.types import Submission
from arr.runs.prepare import SCRATCH_MODE, PreparedAgent, make_private_dir, refill_work_dir

__all__ = [
    "MOVE_ROUNDS",
    "RETIRED_DIFF_PREFIX",
    "TRASH_DIR",
    "Cleanup",
    "FreshFolder",
    "clean_up_run",
    "fresh_folder",
    "park_end_of_run",
    "purge_trash",
    "remove_path",
    "remove_quietly",
    "retired_diff_name",
    "retired_folders",
]

MOVE_ROUNDS = 5
"""Rounds of moving out of a folder before arr gives up (a process may write meanwhile)."""

ROUND_PAUSE_S = 0.05
"""Pause before each further round."""

TRASH_DIR = ".trash"
"""`retired/.trash/`: what arr deletes, on its way out (scratch content, rests at the end)."""

FILL_PREFIX = ".fill-"
"""`retired/.fill-<uuid>/`: where the content of a fresh working folder is built."""

RETIRED_DIFF_PREFIX = "retired-"
"""`agents/<id>/retired-<folder>.diff`: the diff of a filed-away folder against `base/`."""

MAX_OPEN_DEPTH = 64
"""Deleting holds one descriptor per level; deeper folders are moved up to the root first."""

SYSTEM_XATTRS = frozenset({"com.apple.provenance"})
"""Extended attributes macOS itself puts back on every folder (not the agent's)."""

_NUMBER_RE = re.compile(r"^(\d+)-")
_CLEARABLE_FLAGS = stat.UF_IMMUTABLE | stat.UF_APPEND | stat.UF_NOUNLINK
"""User flags that keep the owner from moving or deleting an entry; the owner may clear them."""
_USER_FLAGS = 0x0000FFFF
"""All flags an owner may set (`UF_SETTABLE`: `hidden`, `nodump`, `uchg` …); the system flags
above them need root, so no agent can set them."""
_STAGING_RESTS = (".submission-", ".orphan-", ".staging-")
_LOCK_RE = re.compile(r"^\.version-.+\.lock$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_DARWIN = sys.platform == "darwin"


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
# System calls relative to a folder descriptor (macOS has no `chflagsat`)
# --------------------------------------------------------------------------------------------


def _libc() -> Any:
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    except OSError:  # pragma: no cover - every supported system has a C library
        return None


_LIBC = _libc()
_ATTR_CMN_FLAGS = 0x00040000
_ATTR_CMN_EXTENDED_SECURITY = 0x00400000
_FSOPT_NOFOLLOW = 0x1
_KAUTH_FILESEC_MAGIC = 0x012CC16D
_KAUTH_FILESEC_NOACL = 0xFFFFFFFF
_RENAME_EXCL = 0x4  # macOS `renameatx_np`
_RENAME_NOREPLACE = 0x1  # Linux `renameat2`


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


def _check(result: int, what: str) -> None:
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), what)


def _setattr_at(dir_fd: int, name: str, attr: int, data: bytes) -> None:
    """`setattrlistat(dir_fd, name, …, FSOPT_NOFOLLOW)`: one common attribute of an entry."""
    function = _LIBC.setattrlistat
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
    ]
    attrs = _AttrList(5, 0, attr, 0, 0, 0, 0)
    buffer = ctypes.create_string_buffer(data, len(data))
    result = function(
        dir_fd, os.fsencode(name), ctypes.byref(attrs), buffer, len(data), _FSOPT_NOFOLLOW
    )
    _check(result, name)


def _no_acl() -> bytes:
    """The attribute value of `ATTR_CMN_EXTENDED_SECURITY` that removes an ACL."""
    filesec = struct.pack("<I16s16sII", _KAUTH_FILESEC_MAGIC, b"\0" * 16, b"\0" * 16, 0, 0)
    filesec = filesec[:-8] + struct.pack("<II", _KAUTH_FILESEC_NOACL, 0)
    return struct.pack("<iI", 8, len(filesec)) + filesec


def _set_flags_at(dir_fd: int, name: str, flags: int) -> None:
    if _DARWIN:
        _setattr_at(dir_fd, name, _ATTR_CMN_FLAGS, struct.pack("<I", flags))


def _drop_acl_at(dir_fd: int, name: str) -> None:
    if _DARWIN:
        _setattr_at(dir_fd, name, _ATTR_CMN_EXTENDED_SECURITY, _no_acl())


def _chmod_at(dir_fd: int, name: str, mode: int) -> None:
    """`chmod` of the entry `name` itself, never through a link."""
    if _DARWIN:
        os.chmod(name, mode, dir_fd=dir_fd, follow_symlinks=False)
        return
    fd = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=dir_fd)  # Linux: no lchmod
    try:
        if stat.S_ISLNK(os.fstat(fd).st_mode):
            return
        os.chmod(f"/proc/self/fd/{fd}", mode)
    finally:
        os.close(fd)


def _rename_excl(src_fd: int, name: str, dst_fd: int, new: str | None = None) -> None:
    """Move `name` from one folder descriptor to another (as `new`); never replaces an entry
    there (`FileExistsError`)."""
    new = name if new is None else new
    raw, raw_new = os.fsencode(name), os.fsencode(new)
    if _DARWIN:
        function = _LIBC.renameatx_np
        flags = _RENAME_EXCL
    else:
        function = getattr(_LIBC, "renameat2", None)
        flags = _RENAME_NOREPLACE
        if function is None:  # pragma: no cover - old C library
            if _exists_at(dst_fd, new):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), new)
            os.rename(name, new, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            return
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _check(function(src_fd, raw, dst_fd, raw_new, flags), name)


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _xattr_names(fd: int) -> list[str]:
    """Names of the extended attributes of the open folder `fd`."""
    if not _DARWIN:
        try:
            return os.listxattr(fd)
        except OSError:
            return []
    function = _LIBC.flistxattr
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    function.restype = ctypes.c_ssize_t
    size = function(fd, None, 0, 0)
    if size <= 0:
        return []
    buffer = ctypes.create_string_buffer(size)
    size = function(fd, buffer, size, 0)
    return [os.fsdecode(n) for n in buffer.raw[: max(size, 0)].split(b"\0") if n]


def _remove_xattr(fd: int, name: str) -> None:
    if not _DARWIN:
        os.removexattr(fd, name)
        return
    function = _LIBC.fremovexattr
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    _check(function(fd, os.fsencode(name), 0), name)


def _reset_flags(fd: int) -> None:
    """Clear every user flag of the open entry `fd` itself (`hidden`, `nodump` …)."""
    flags = getattr(os.fstat(fd), "st_flags", 0)
    if not _DARWIN or not flags & _USER_FLAGS:
        return
    function = _LIBC.fchflags
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    _check(function(fd, flags & ~_USER_FLAGS), "flags")


def _drop_acl(fd: int) -> None:
    if not _DARWIN:
        return
    function = _LIBC.fsetattrlist
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
    ]
    attrs = _AttrList(5, 0, _ATTR_CMN_EXTENDED_SECURITY, 0, 0, 0, 0)
    data = _no_acl()
    buffer = ctypes.create_string_buffer(data, len(data))
    _check(function(fd, ctypes.byref(attrs), buffer, len(data), 0), "ACL")


# --------------------------------------------------------------------------------------------
# Anchored folders
# --------------------------------------------------------------------------------------------


def _clear_at(dir_fd: int, name: str) -> bool:
    """Give the owner back what keeps it from moving or deleting the entry `name` itself:
    user flags, an ACL, write and search rights on a folder. Reached relative to `dir_fd`,
    never through a link. True if something changed."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return False
    changed = False
    flags = getattr(info, "st_flags", 0)
    if flags & _CLEARABLE_FLAGS:
        with contextlib.suppress(OSError):
            _set_flags_at(dir_fd, name, flags & ~_CLEARABLE_FLAGS)
            changed = True
    with contextlib.suppress(OSError):
        _drop_acl_at(dir_fd, name)
        changed = changed or _DARWIN
    if stat.S_ISDIR(info.st_mode) and (info.st_mode & stat.S_IRWXU) != stat.S_IRWXU:
        with contextlib.suppress(OSError):
            _chmod_at(dir_fd, name, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
            changed = True
    return changed


def _open_at(dir_fd: int, name: str) -> int:
    """Open the folder `name` relative to `dir_fd`, never through a link, and check that the
    descriptor is the folder that lay there (same device and inode). Raises `OSError`."""
    before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        code = errno.ELOOP if stat.S_ISLNK(before.st_mode) else errno.ENOTDIR
        raise OSError(code, os.strerror(code), name)
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise OSError(errno.ESTALE, "swapped while opening", name)
    return fd


@contextlib.contextmanager
def _folder(path: Path) -> Iterator[int]:
    """A descriptor of `path`, whose parent belongs to arr (`agents/<id>/`, `scratch/`,
    `retired/…`, the run folder): the last part is never followed and checked by inode."""
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            fd = _open_at(parent, path.name)
        except PermissionError:  # the agent took its own rights on the folder
            _clear_at(parent, path.name)
            fd = _open_at(parent, path.name)
        _clear_at(parent, path.name)
    finally:
        os.close(parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _agent_folder(path: Path) -> contextlib.AbstractContextManager[int]:
    """`_folder` with a German `ArrError` for a link, a file or a missing folder."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise ArrError(f"Der Ordner {path} fehlt.") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ArrError(
            f"{path} ist kein Ordner (ein Link oder eine Datei). arr fasst ihn nicht an."
        )
    return _folder(path)


# --------------------------------------------------------------------------------------------
# Moving every entry out of a folder
# --------------------------------------------------------------------------------------------


def _move_one(src_fd: int, dst_fd: int, name: str) -> bool:
    """Move `name` from `src_fd` to `dst_fd` (a new folder of arr); False if it is gone."""
    for attempt in (1, 2):
        try:
            _rename_excl(src_fd, name, dst_fd)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            if attempt == 2 or exc.errno not in (errno.EACCES, errno.EPERM):
                raise
            if not _clear_at(src_fd, name):
                raise
    return False


def _move_out(
    src_fd: int, label: Path, make_target: Callable[[int], Path]
) -> tuple[int, list[Path]]:
    """Move every entry of the folder `src_fd` (shown as `label`) into folders from
    `make_target(round)`; the folder itself stays. Up to `MOVE_ROUNDS` rounds. Raises
    `ArrError` with the path if an entry stays. Returns the number moved and the targets."""
    moved, targets = 0, []
    failure: tuple[str, OSError] | None = None
    for round_no in range(1, MOVE_ROUNDS + 1):
        names = sorted(os.listdir(src_fd))
        if not names:
            return moved, targets
        if round_no > 1:
            time.sleep(ROUND_PAUSE_S)
        target = make_target(round_no)
        targets.append(target)
        with _folder(target) as dst_fd:
            for name in names:
                try:
                    moved += _move_one(src_fd, dst_fd, name)
                except OSError as exc:
                    failure = (name, exc)
    rest = sorted(os.listdir(src_fd))
    if not rest:
        return moved, targets
    name = failure[0] if failure and failure[0] in rest else rest[0]
    raise _left_behind(label / name, failure[1] if failure and failure[0] == name else None)


def _left_behind(where: Path, exc: OSError | None) -> ArrError:
    why = f" ({os.strerror(exc.errno)})" if exc is not None and exc.errno else ""
    return ArrError(
        f"arr konnte {where} nicht ablegen{why}. Der Ordner ist nicht frisch, deshalb geht der "
        "Run nicht weiter. Vermutlich hält ein Prozess des Agenten die Datei oder schreibt "
        "noch in den Ordner, oder die Datei ist gesperrt. Prüfe den Ordner und starte den Run "
        "neu."
    )


def _not_fresh(where: Path, what: str) -> ArrError:
    return ArrError(
        f"Der Ordner ist nach dem Neu-Füllen nicht frisch: {where} {what}. Vermutlich schreibt "
        "ein Prozess des Agenten noch in den Ordner. arr hat nichts überschrieben; der Run geht "
        "nicht weiter. Prüfe den Ordner und starte den Run neu."
    )


def _strip(fd: int, label: Path) -> None:
    """Remove the user flags, extended attributes and the ACL of the open folder itself (M1,
    W4b-05a.2 NP-G1)."""
    try:
        _reset_flags(fd)
    except OSError:
        raise _not_fresh(label, "trägt Datei-Flags weiter") from None
    with contextlib.suppress(OSError):
        _drop_acl(fd)
    for name in _xattr_names(fd):
        if name in SYSTEM_XATTRS:
            continue
        try:
            _remove_xattr(fd, name)
        except OSError as exc:
            if exc.errno in (errno.ENOATTR if hasattr(errno, "ENOATTR") else -1, errno.ENODATA):
                continue
            if not _DARWIN and not name.startswith("user."):
                continue  # Linux: system namespaces, never the agent's
            raise _not_fresh(label, f"trägt das Attribut „{name}“ weiter") from None
    left = [n for n in _xattr_names(fd) if n not in SYSTEM_XATTRS]
    if _DARWIN and left:
        raise _not_fresh(label, f"trägt das Attribut „{left[0]}“ weiter")


def _install(build_fd: int, work_fd: int, label: Path) -> dict[str, int]:
    """Move the built content into the working folder, top entry by top entry, never over an
    existing name. Returns name → inode of what arr put there."""
    placed: dict[str, int] = {}
    for name in sorted(os.listdir(build_fd)):
        inode = os.stat(name, dir_fd=build_fd, follow_symlinks=False).st_ino
        try:
            _rename_excl(build_fd, name, work_fd)
        except FileExistsError:
            raise _not_fresh(
                label / name, "lag schon da, bevor arr den Eintrag hineinlegte"
            ) from None
        placed[name] = inode
    return placed


def _verify(work_fd: int, placed: dict[str, int], label: Path) -> None:
    """The working folder holds exactly what arr put there (names and inodes, top level)."""
    names = set(os.listdir(work_fd))
    for name in sorted(names - placed.keys()):
        raise _not_fresh(label / name, "gehört nicht zum frischen Ordner")
    for name in sorted(placed):
        if name not in names:
            raise _not_fresh(label / name, "fehlt")
        if os.stat(name, dir_fd=work_fd, follow_symlinks=False).st_ino != placed[name]:
            raise _not_fresh(label / name, "wurde ausgetauscht")


# --------------------------------------------------------------------------------------------
# Fresh working folder
# --------------------------------------------------------------------------------------------


def retired_root(run_dir: Path) -> Path:
    return run_dir / RETIRED_DIR


def _next_number(folder: Path) -> int:
    numbers = [int(m.group(1)) for n in os.listdir(folder) if (m := _NUMBER_RE.match(n))]
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


def _empty(fd: int, label: Path, run_dir: Path) -> tuple[int, list[str]]:
    """Empty the open folder `fd` in place: its entries go to `retired/.trash/` (the agent
    sees an empty folder at once), then arr deletes them there. Returns the number moved and
    what could not be deleted (stays in `retired/`, closed for every agent)."""
    trash = _trash(run_dir)
    moved, _ = _move_out(
        fd, label, lambda n: trash if n == 1 else make_private_dir(trash / f"~{n}")
    )
    left = remove_quietly(trash)
    return moved, [_rel(run_dir, p) for p in left]


def fresh_folder(agent: PreparedAgent, *, stage: str) -> FreshFolder:
    """Give `agent` a fresh working folder in place (module docstring). Runs in a thread.

    Raises `ArrError` for an agent in a shared folder (`check` refuses `folder: fresh` there
    already; this is the second wall), when something old or foreign would stay and for any
    other error of the file system on the way (the folder is then not fresh).
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
    scratch = make_private_dir(run_dir / SCRATCH_DIR) / agent.id
    if not os.path.lexists(scratch):
        make_private_dir(scratch)
    agent_root = make_private_dir(make_private_dir(retired_root(run_dir)) / agent.id)
    name = f"{_next_number(agent_root):03d}-{_folder_part(stage, 'ohne-phase')}"
    with _agent_folder(work) as work_fd, _agent_folder(scratch) as scratch_fd:
        moved, targets = _move_out(work_fd, work, _retire_targets(agent_root, name))
        if not targets:  # an empty working folder: still a filed-away folder, for the diff
            targets.append(_retire_targets(agent_root, name)(1))
        done = FreshFolder(
            retired=targets[0].relative_to(run_dir).as_posix(),
            moved=moved,
            rounds=len(targets),
            extra=[t.relative_to(run_dir).as_posix() for t in targets[1:]],
        )
        done.scratch_removed, done.scratch_left = _empty(scratch_fd, scratch, run_dir)
        build = make_private_dir(retired_root(run_dir) / f"{FILL_PREFIX}{uuid.uuid4().hex}")
        try:
            done.generated = refill_work_dir(agent, build / "work")
            with _folder(build / "work") as build_fd:
                start = os.fstat(build_fd)
                _strip(work_fd, work)
                _strip(scratch_fd, scratch)
                placed = _install(build_fd, work_fd, work)
            os.fchmod(work_fd, stat.S_IMODE(start.st_mode))  # as a new working folder
            _verify(work_fd, placed, work)
            _verify(scratch_fd, {}, scratch)
            _strip(work_fd, work)
            _strip(scratch_fd, scratch)
            # mode and times of both folders as new ones would have them (NP-G1)
            os.utime(work_fd, ns=(start.st_atime_ns, start.st_mtime_ns))
            os.fchmod(scratch_fd, SCRATCH_MODE)
            os.utime(scratch_fd)
        finally:
            remove_quietly(build)
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
# Deleting, only through folder descriptors
# --------------------------------------------------------------------------------------------


def _unlink_at(dir_fd: int, name: str, *, folder: bool) -> None:
    remove = os.rmdir if folder else os.unlink
    try:
        remove(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM) or not _clear_at(dir_fd, name):
            raise
        remove(name, dir_fd=dir_fd)


def _remove_at(parent_fd: int, top: str, label: str, failed: list[str]) -> None:
    """Delete the entry `top` of the folder `parent_fd` with everything below it.

    Each folder is opened relative to its parent's descriptor (`_open_at`); rights, flags and
    ACLs change only at entries reached that way; a link is removed as a link, also one that
    replaces a folder while arr works. Folders deeper than `MAX_OPEN_DEPTH` are moved up into
    `top` first and deleted from there, so the number of open descriptors stays bounded. What
    cannot be deleted is added to `failed` (labels)."""
    try:
        info = os.stat(top, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        failed.append(label)
        return
    if not stat.S_ISDIR(info.st_mode):
        try:
            _unlink_at(parent_fd, top, folder=False)
        except OSError:
            failed.append(label)
        return
    root_fd = _enter(parent_fd, top, label, failed)
    if root_fd is None:
        return
    try:
        _empty_tree(root_fd, label, failed)
    finally:
        os.close(root_fd)
    try:
        _unlink_at(parent_fd, top, folder=True)
    except OSError as exc:
        if exc.errno != errno.ENOTEMPTY or not failed:
            failed.append(label)


def _enter(parent_fd: int, name: str, label: str, failed: list[str]) -> int | None:
    """Open the folder `name` for deleting; a link or file there is removed as an entry."""
    _clear_at(parent_fd, name)
    try:
        return _open_at(parent_fd, name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ESTALE):
            try:
                _unlink_at(parent_fd, name, folder=False)
                return None
            except OSError:
                pass
        failed.append(label)
        return None


def _empty_tree(root_fd: int, label: str, failed: list[str]) -> None:
    """Delete everything below the open folder `root_fd` (see `_remove_at`)."""
    tried: set[str] = set()
    Frame = tuple[int, str, Iterator[str], tuple[int, str] | None]
    stack: list[Frame] = []

    def listed(fd: int, shown: str) -> list[str] | None:
        try:
            return sorted(os.listdir(fd))
        except OSError:
            failed.append(shown)
            return None

    first = listed(root_fd, label) or []
    tried.update(first)
    stack.append((root_fd, label, iter(first), None))
    while stack:
        fd, shown, names, origin = stack[-1]
        child = next(names, None)
        if child is None:
            stack.pop()
            if origin is None:  # the root: deep folders may have been moved up meanwhile
                again = sorted(set(listed(fd, shown) or []) - tried)
                if again:
                    tried.update(again)
                    stack.append((fd, shown, iter(again), None))
                continue
            os.close(fd)
            parent, name = origin
            try:
                _unlink_at(parent, name, folder=True)
            except OSError as exc:
                if exc.errno != errno.ENOTEMPTY:
                    failed.append(shown)
            continue
        child_label = f"{shown}/{child}"
        try:
            info = os.stat(child, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            failed.append(child_label)
            continue
        if not stat.S_ISDIR(info.st_mode):
            try:
                _unlink_at(fd, child, folder=False)
            except OSError:
                failed.append(child_label)
            continue
        if len(stack) >= MAX_OPEN_DEPTH:
            try:
                _clear_at(fd, child)
                _rename_excl(fd, child, root_fd, f".deep-{uuid.uuid4().hex}")
            except OSError:
                failed.append(child_label)
            continue
        sub = _enter(fd, child, child_label, failed)
        if sub is None:
            continue
        entries = listed(sub, child_label)
        if entries is None:
            os.close(sub)
            continue
        stack.append((sub, child_label, iter(entries), (fd, child)))


def remove_path(path: Path) -> None:
    """Delete `path`: a link (or file) as itself, a folder with everything below it, only
    through folder descriptors (module docstring). The parent of `path` belongs to arr.
    Raises `OSError` naming the first entry that could not be deleted."""
    failed = _remove(path)
    if failed:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), failed[0])


def remove_quietly(path: Path) -> list[str]:
    """`remove_path`, but what cannot be deleted is returned (paths), not raised."""
    return _remove(path)


def _remove(path: Path) -> list[str]:
    failed: list[str] = []
    try:
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return failed
    try:
        _remove_at(parent, path.name, str(path), failed)
    finally:
        os.close(parent)
    return failed


# --------------------------------------------------------------------------------------------
# End of a run
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Cleanup:
    """What arr parked or deleted at the end of a run and what it could not (paths relative
    to the run); `parked` is the folder below `retired/.trash/` that holds it until
    `purge_trash`."""

    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    parked: str | None = None


def _rel(run_dir: Path, path: str | Path) -> str:
    try:
        return Path(path).relative_to(run_dir).as_posix()
    except ValueError:
        return str(path)


def _staging_rests(staging: Path) -> list[str]:
    """Rests of submissions killed halfway (W4b-01.1): `.submission-<uuid>`, `.orphan-<uuid>`,
    `.staging-<uuid>` and empty `.version-<name>.lock` files (names)."""
    if staging.is_symlink() or not staging.is_dir():
        return []
    found = []
    with os.scandir(staging) as entries:
        for entry in entries:
            if entry.name.startswith(_STAGING_RESTS):
                found.append(entry.name)
            elif _LOCK_RE.match(entry.name):
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_size == 0:
                    found.append(entry.name)
    return sorted(found)


def _final_name(sub: Submission) -> str:
    """The folder name a sealed submission gets in `output/submissions/` at its release
    (`RoomStore.submit`): such a folder is no orphan even before the row knows it."""
    phase = _folder_part(sub.stage, "ohne-phase")
    return f"{sub.id:03d}-{phase}-{_folder_part(sub.shown_as, 'agent')}"


def _orphans(run_dir: Path, submissions: Iterable[Submission]) -> list[str]:
    """Folders in `output/submissions/` that no submission of the database names (names)."""
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
    return sorted(n for n in os.listdir(folder) if n not in known)


def park_end_of_run(run_dir: Path, submissions: Iterable[Submission] | None) -> Cleanup:
    """At the end of a run (finalizer, W4b-05a): move `scratch/`, the rests of submissions in
    `groupchats/staging/` and folders in `output/submissions/` without a database row into
    one folder below `retired/.trash/`, out of every agent's reach. There the secret scan of
    `arr run` still sees them; `purge_trash` deletes them after it (Entscheidung 54), or
    slimming does. `submissions` None: no room, no orphans are looked for. Never anything
    below `groupchats/sealed/` (Entscheidung 36). Nothing raises."""
    done = Cleanup()
    places: list[tuple[Path, str, str]] = []  # parent, name, name in the trash
    if os.path.lexists(run_dir / SCRATCH_DIR):
        places.append((run_dir, SCRATCH_DIR, SCRATCH_DIR))
    staging = run_dir / STAGING_DIR
    try:
        places += [(staging, n, f"staging-{n}") for n in _staging_rests(staging)]
    except OSError:
        done.failed.append(STAGING_DIR)
    if submissions is not None:
        try:
            outputs = run_dir / SUBMISSIONS_DIR
            places += [(outputs, n, f"submission-{n}") for n in _orphans(run_dir, submissions)]
        except OSError:
            done.failed.append(SUBMISSIONS_DIR)
    if not places:
        return done
    trash = _trash(run_dir)
    done.parked = _rel(run_dir, trash)
    with _folder(trash) as trash_fd:
        for parent, name, parked in places:
            shown = _rel(run_dir, parent / name)
            try:
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
                try:
                    _rename_excl(fd, name, trash_fd, parked)
                finally:
                    os.close(fd)
            except OSError:
                done.failed.append(shown)
                continue
            done.removed.append(shown)
    return done


def purge_trash(run_dir: Path) -> Cleanup:
    """Delete everything arr parked or emptied into `retired/.trash/` (after the secret scan)
    and half builds `retired/.fill-*` of an aborted fresh folder. Nothing raises; what cannot
    be deleted is listed and stays there until slimming."""
    done = Cleanup()
    root = retired_root(run_dir)
    # also half builds of a fresh folder a hard abort left behind (W4b-05a.2 NP-G2)
    try:
        builds = sorted(n for n in os.listdir(root) if n.startswith(FILL_PREFIX))
    except OSError:
        builds = []
    places = [root / TRASH_DIR, *(root / n for n in builds)]
    for place in places:
        if not os.path.lexists(place):
            continue
        left = remove_quietly(place)
        if left:
            done.failed += [_rel(run_dir, p) for p in left]
        else:
            done.removed.append(_rel(run_dir, place))
    if done.failed or not done.removed:
        return done
    with contextlib.suppress(OSError):  # nothing was filed away: no `retired/` stays
        os.rmdir(retired_root(run_dir))
    return done


def clean_up_run(run_dir: Path, submissions: Iterable[Submission] | None) -> Cleanup:
    """Park the rests of the run and delete them at once (a run without a secret scan)."""
    done = park_end_of_run(run_dir, submissions)
    done.failed += purge_trash(run_dir).failed
    return done

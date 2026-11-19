"""W4b-05a.2: deterministic tests for the core of the folder protection (NP-M1, NP-G1–G3).

A race between two processes rarely hits the moment between arr's `stat` and its `open`
(review np09). Here a hook does the swap exactly there, as a process of the agent could:
just before arr opens the folder, the entry is replaced

- `link`: by a link to a bait folder (`O_NOFOLLOW` stops it, and the inode check),
- `moved`: the real folder is moved out of the run and a link to it takes its place (the
  inode is the same: only `O_NOFOLLOW` stops it, mutation N01),
- `other`: by another real folder, the bait (no link: only the inode check stops it, N02).

Every bait lies in `tmp_path`. Each test also states its counter-check: the protection it
guards is named in the docstring, and the mutation that removes it turns the test red
(report W4b-05a.2, section „Mutationen“).
"""

import contextlib
import errno
import os
import resource
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from folders_kit import (
    FOUR_AGENTS,
    folder_workfolder,
    manager,
    owner_gets_it_back,
    run_done,
)

from arr.errors import ArrError
from arr.runs import folders
from arr.runs.context import RunContext

macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="flags, ACLs: macOS")
SWAPS = ["link", "moved", "other"]


def make_bait(root: Path) -> Path:
    root.mkdir(parents=True)
    for n in range(3):
        (root / f"k{n}.txt").write_text(f"KOEDER {n}\n")
    (root / "unter").mkdir()
    (root / "unter" / "tief.txt").write_text("KOEDER tief\n")
    return root


def state(bait: Path) -> list[tuple[str, str]]:
    out = []
    for top, dirs, files in os.walk(bait):
        for name in sorted(dirs + files):
            path = Path(top) / name
            out.append((str(path.relative_to(bait)), path.read_text() if path.is_file() else "/"))
    return sorted(out)


def lay_bait(tmp_path: Path, entry: Path, how: str) -> tuple[Path, list[tuple[str, str]]]:
    """The bait of a swap and its state before: for `moved` the place outside the run where
    the real folder goes (its content is what must stay), else a bait folder."""
    if how == "moved":
        return tmp_path / "draussen", state(entry)
    bait = make_bait(tmp_path / "koeder")
    return bait, state(bait)


def watched(entry: Path, how: str, bait: Path) -> Path:
    """Where the guarded content lies after the swap."""
    return entry if how == "other" else bait


@contextlib.contextmanager
def swap_before_open(
    monkeypatch: pytest.MonkeyPatch, entry: Path, how: str, bait: Path
) -> Iterator[dict[str, Any]]:
    """Swap `entry` once, the moment arr opens its name relative to a folder descriptor."""
    real = os.open
    done: dict[str, Any] = {"swapped": False}

    def hooked(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if not done["swapped"] and path == entry.name and kwargs.get("dir_fd") is not None:
            done["swapped"] = True
            assert not os.path.lexists(f"{entry}.echt") and (how == "moved") != bait.exists()
            if how == "link":
                os.rename(entry, f"{entry}.echt")
                entry.symlink_to(bait)
            elif how == "moved":  # the real folder now lies outside, a link points to it
                os.rename(entry, bait)
                entry.symlink_to(bait)
            else:  # another real folder at its place
                os.rename(entry, f"{entry}.echt")
                os.rename(bait, entry)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(folders.os, "open", hooked)
    yield done
    monkeypatch.setattr(folders.os, "open", real)


def put_back(entry: Path, how: str, bait: Path) -> None:
    """The real folder at its place again, the bait at its own."""
    if how == "other":
        if entry.is_dir() and not entry.is_symlink():
            os.rename(entry, bait)
        os.rename(f"{entry}.echt", entry)
    elif how == "moved":
        entry.unlink()
        os.rename(bait, entry)
    else:
        entry.unlink()
        os.rename(f"{entry}.echt", entry)


# --------------------------------------------------------------------------------------------
# Opening without a link and the inode check: deleting (`_open_at`, N01, N02, N20)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", SWAPS)
def test_deleting_never_opens_a_swapped_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """`remove_quietly` (end of a run, emptying a scratch folder, slimming) on a folder whose
    subfolder `a` is swapped right before arr opens it. Guards `O_NOFOLLOW` (`moved`), the
    inode check (`other`) and both (`link`): the bait stays as it was."""
    root = tmp_path / "scratch"
    (root / "a").mkdir(parents=True)
    (root / "a" / "eigen.txt").write_text("x")
    bait, before = lay_bait(tmp_path, root / "a", how)
    with swap_before_open(monkeypatch, root / "a", how, bait) as done:
        left = folders.remove_quietly(root)
    assert done["swapped"]
    assert state(watched(root / "a", how, bait)) == before, (how, left)
    if how == "other":  # arr names the folder it did not open, and leaves it alone
        assert any(p.endswith("/scratch/a") for p in left), left


# --------------------------------------------------------------------------------------------
# Opening without a link and the inode check: the folders themselves (`_folder`, N19)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", SWAPS)
@pytest.mark.parametrize("which", ["work", "scratch"])
async def test_fresh_folder_never_opens_a_swapped_working_or_scratch_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str, how: str
) -> None:
    """The working or scratch folder itself is swapped right before `fresh_folder` opens it:
    `ArrError` with its path, and the bait unchanged (nothing filed away, nothing filled in)."""
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        run_dir = ctx.prepared.run_dir
        entry = ctx.prepared.agents["c-copy"].work_dir
        if which == "scratch":
            entry = run_dir / "scratch" / "c-copy"
            (entry / "wegwerf.txt").write_text("x")
        bait, before = lay_bait(tmp_path, entry, how)
        seen["before"] = before
        with swap_before_open(monkeypatch, entry, how, bait) as done:
            try:
                await ctx.fresh_folder("c-copy", stage="p", attempt=1)
                seen["error"] = None
            except ArrError as exc:
                seen["error"] = exc.message
        seen["swapped"] = done["swapped"]
        seen["bait"] = state(watched(entry, how, bait))
        put_back(entry, how, bait)

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen["swapped"]
    assert seen["bait"] == seen["before"]
    assert seen["error"] is not None and ("work" in seen["error"] or "c-copy" in seen["error"])


# --------------------------------------------------------------------------------------------
# Moving the fresh content in replaces nothing (`RENAME_EXCL`, N03, N14)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("planted", ["link", "file", "folder"])
async def test_a_name_planted_before_arr_moves_in_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str
) -> None:
    """Right before arr moves the fresh content in, a process lays a name of it into the
    working folder: a link to a bait file (`AGENTS.md`), a file (`notes.md`) or an empty
    folder (`sub`). `ArrError` „lag schon da“ with the path; the planted entry, its inode and
    the bait stay as they were."""
    wf = folder_workfolder(tmp_path)
    bait = tmp_path / "koeder.txt"
    bait.write_text("KOEDER\n")
    name = {"link": "AGENTS.md", "file": "notes.md", "folder": "sub"}[planted]
    install = folders._install
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        work = ctx.prepared.agents["c-copy"].work_dir

        def planting(build_fd: int, work_fd: int, label: Path) -> dict[str, int]:
            target = work / name
            if planted == "link":
                target.symlink_to(bait)
            elif planted == "file":
                target.write_text("vom Agenten\n")
            else:
                target.mkdir()
            seen["inode"] = os.lstat(target).st_ino
            return install(build_fd, work_fd, label)

        monkeypatch.setattr(folders, "_install", planting)
        try:
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
            seen["error"] = None
        except ArrError as exc:
            seen["error"] = exc.message
        monkeypatch.setattr(folders, "_install", install)
        info = os.lstat(work / name)
        seen["after"] = (info.st_ino, stat.S_IFMT(info.st_mode))
        if planted == "file":
            seen["text"] = (work / name).read_text()

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen["error"] is not None and "lag schon da" in seen["error"], seen
    assert f"work/{name}" in seen["error"]
    kind = {"link": stat.S_IFLNK, "file": stat.S_IFREG, "folder": stat.S_IFDIR}[planted]
    assert seen["after"] == (seen["inode"], kind)
    assert seen.get("text", "vom Agenten\n") == "vom Agenten\n"
    assert bait.read_text() == "KOEDER\n"


# --------------------------------------------------------------------------------------------
# The second removal of attributes and flags (N06), flags, mode and times (NP-G1)
# --------------------------------------------------------------------------------------------


def _xattrs(path: Path) -> list[str]:
    done = subprocess.run(["/usr/bin/xattr", str(path)], capture_output=True, text=True)
    return [n for n in done.stdout.split() if n != "com.apple.provenance"]


def _acl(path: Path) -> str:
    listing = subprocess.run(["/bin/ls", "-led", str(path)], capture_output=True, text=True)
    return "\n".join(listing.stdout.splitlines()[1:])


def _mark(path: Path, text: str) -> None:
    subprocess.run(["/usr/bin/xattr", "-w", "user.spaet", text, str(path)], check=True)
    subprocess.run(["/bin/chmod", "+a", "everyone deny delete_child", str(path)], check=True)
    os.chflags(path, stat.UF_HIDDEN | stat.UF_NODUMP)


@macos_only
async def test_attributes_set_while_arr_refills_are_gone_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After arr's first removal and before the end, a process sets an xattr, an ACL and
    flags at the working and the scratch folder themselves: all gone at the end (the second
    removal)."""
    wf = folder_workfolder(tmp_path)
    install = folders._install
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        work = ctx.prepared.agents["c-copy"].work_dir
        scratch = ctx.prepared.run_dir / "scratch" / "c-copy"

        def marking(build_fd: int, work_fd: int, label: Path) -> dict[str, int]:
            _mark(work, "SPAET-ARBEIT")
            _mark(scratch, "SPAET-SCRATCH")
            seen["marked"] = (_xattrs(work), bool(_acl(work)), os.lstat(work).st_flags)
            return install(build_fd, work_fd, label)

        monkeypatch.setattr(folders, "_install", marking)
        await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        monkeypatch.setattr(folders, "_install", install)
        seen["work"] = (_xattrs(work), _acl(work), os.lstat(work).st_flags & 0xFFFF)
        seen["scratch"] = (_xattrs(scratch), _acl(scratch), os.lstat(scratch).st_flags & 0xFFFF)

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen["marked"][0] == ["user.spaet"] and seen["marked"][1] and seen["marked"][2]
    assert seen["work"] == ([], "", 0)
    assert seen["scratch"] == ([], "", 0)


@macos_only
async def test_flags_mode_and_times_of_both_folders_are_those_of_new_ones(
    tmp_path: Path,
) -> None:
    """NP-G1 (review np05): flags `hidden` and `nodump` at both folders, mode 0o751 and old
    times at the scratch folder, old times at the working folder. Afterwards: no user flag,
    scratch 0o700 with fresh times, the working folder with the times of its fresh build."""
    wf = folder_workfolder(tmp_path)
    old = 1_000_000_000  # 2001-09-09
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        run_dir = ctx.prepared.run_dir
        work = ctx.prepared.agents["c-copy"].work_dir
        scratch = run_dir / "scratch" / "c-copy"
        for path in (work, scratch):
            os.chflags(path, stat.UF_HIDDEN | stat.UF_NODUMP)
            os.utime(path, (old, old))
        os.chmod(scratch, 0o751)
        started = time.time()
        await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        w, s = os.lstat(work), os.lstat(scratch)
        seen["flags"] = (w.st_flags & 0xFFFF, s.st_flags & 0xFFFF)
        seen["scratch"] = (stat.S_IMODE(s.st_mode), s.st_mtime >= started - 1)
        seen["work_times"] = (w.st_mtime >= started - 1, w.st_atime > old + 1)

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen["flags"] == (0, 0)
    assert seen["scratch"] == (0o700, True)
    assert seen["work_times"] == (True, True)


# --------------------------------------------------------------------------------------------
# Deep trees: the number of open descriptors stays bounded (N18)
# --------------------------------------------------------------------------------------------


def test_a_tree_deeper_than_the_descriptor_bound_is_deleted_completely(tmp_path: Path) -> None:
    """150 levels below the folder, the soft limit of open files only 100 above what is
    open now: with the deep folders moved up (at most `MAX_OPEN_DEPTH` = 64 open) all goes;
    holding one descriptor per level would hit the limit."""
    root = tmp_path / "tief"
    path = root
    for n in range(150):
        path = path / f"e{n:03d}"
    path.mkdir(parents=True)
    (path / "unten.txt").write_text("x")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    in_use = len(os.listdir("/dev/fd"))
    assert folders.MAX_OPEN_DEPTH + 20 < 100 < 150
    resource.setrlimit(resource.RLIMIT_NOFILE, (in_use + 100, hard))
    try:
        left = folders.remove_quietly(root)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert left == []
    assert not root.exists()


# --------------------------------------------------------------------------------------------
# NP-G2: half builds after a hard abort; NP-G3: cleaning up also when the scan fails
# --------------------------------------------------------------------------------------------


def test_purge_removes_half_builds_and_keeps_filed_away_folders(tmp_path: Path) -> None:
    from arr.runs.finalize import clean_up_after_scan

    run_dir = tmp_path / "run"
    retired = run_dir / "retired"
    (retired / ".fill-abc" / "work" / "sub").mkdir(parents=True)
    (retired / ".fill-abc" / "work" / "sub" / "x.txt").write_text("halb")
    (retired / ".trash" / "u1" / "scratch").mkdir(parents=True)
    (retired / "a1" / "001-p").mkdir(parents=True)
    (retired / "a1" / "001-p" / "notiz.md").write_text("bleibt")
    clean_up_after_scan(run_dir)
    assert sorted(os.listdir(retired)) == ["a1"]
    assert (retired / "a1" / "001-p" / "notiz.md").read_text() == "bleibt"
    # only half builds: afterwards no `retired/` stays
    (retired / "a1" / "001-p" / "notiz.md").unlink()
    os.rmdir(retired / "a1" / "001-p")
    os.rmdir(retired / "a1")
    (retired / ".fill-def").mkdir()
    clean_up_after_scan(run_dir)
    assert not retired.exists()


async def test_cleaning_up_runs_also_when_the_secret_scan_fails(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_folders_end as end

    from arr.runs import launch

    workfolder = end.workfolder.__wrapped__(tmp_path, arr_home, monkeypatch)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Scan kaputt")

    monkeypatch.setattr(launch, "scan_run_dir", broken)
    with pytest.raises(RuntimeError, match="Scan kaputt"):
        await end.launch_run(workfolder, "WERT-x")
    [run_dir] = [p for p in (workfolder / "runs").iterdir() if p.is_dir()]
    assert not (run_dir / "scratch").exists() and not (run_dir / "retired").exists()


def test_errno_names_used_here_exist() -> None:
    assert errno.ELOOP and errno.ESTALE


@pytest.fixture(autouse=True)
def _owner_gets_it_back(tmp_path: Path) -> Iterator[None]:
    """Flags, ACLs and rights a test lays in its `tmp_path` go at the end, also on failure."""
    with owner_gets_it_back(tmp_path):
        yield

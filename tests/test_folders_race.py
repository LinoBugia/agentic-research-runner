"""W4b-05a.1/.2: races with real processes against arr's file operations (H1, H2, M1, G1).

A process of the agent (here without a sandbox, the worst case) plants links, swaps folders
or adds files while arr files away, empties, refills or deletes. Every bait lies in the
test's own folder (`tmp_path`). After every attempt the baits are unchanged (content, number
of entries, modes, flags), and the working folder was either fresh or `fresh_folder` raised
`ArrError`. A planted entry that existed before arr compared the folder (`_verify`) must have
led to `ArrError`. Processes are ended by their PID only.

Independent of the machine's speed (W4b-05a.2, CI): the process writes a marker once it has
planted or swapped for the first time, and a hook holds arr at the place the attack aims at
until that marker exists (`hold_until`). So every attempt is attacked; an attempt without an
attack fails the test. No fixed wait decides whether the race happens.
"""

import contextlib
import json
import os
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
    clear_flags,
    folder_workfolder,
    manager,
    owner_gets_it_back,
    run_done,
)

from arr.errors import ArrError
from arr.runs import folders
from arr.runs.context import RunContext

ATTEMPTS = int(os.environ.get("ARR_RACE_ATTEMPTS", "10"))
"""Attempts per race (the counter-check against the old code used the same number)."""

HOLD_S = float(os.environ.get("ARR_RACE_HOLD_S", "60"))
"""How long a hook holds arr for the process's first attack at most (a bound, not a wait)."""


def first(stop: Path) -> Path:
    """The marker the planter or swapper writes after its first plant or swap."""
    return Path(f"{stop}.first")


def hold_until(marker: Path | None) -> bool:
    """Hold arr (inside its thread) until the process attacked once; False after `HOLD_S`."""
    if marker is None:
        return True
    end = time.monotonic() + HOLD_S
    while not marker.exists():
        if time.monotonic() > end:
            return False
        time.sleep(0.002)
    return True


def bait_state(bait: Path) -> list[tuple[str, int, int, str]]:
    """Every entry below `bait` with mode, flags and content (or link target)."""
    out = []
    for top, dirs, files in os.walk(bait):
        for name in sorted(dirs + files):
            path = Path(top) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                body = "->" + os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                body = path.read_text(errors="replace") if info.st_mode & 0o400 else "?"
            else:
                body = "dir"
            out.append(
                (
                    str(path.relative_to(bait)),
                    stat.S_IMODE(info.st_mode),
                    getattr(info, "st_flags", 0),
                    body,
                )
            )
    return sorted(out)


def make_bait(root: Path, *, files: int = 5, locked: bool = False) -> Path:
    root.mkdir(parents=True)
    (root / "datei.txt").write_text("KOEDER-DATEI\n")
    for n in range(files):
        folder = root / f"d{n:02d}"
        folder.mkdir()
        (folder / "f.txt").write_text(f"KOEDER {n}\n")
        if locked and sys.platform == "darwin":
            os.chflags(folder / "f.txt", stat.UF_IMMUTABLE)
        if locked:
            os.chmod(folder, 0o500)
    return root


@contextlib.contextmanager
def process(program: str, *args: str) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen([sys.executable, "-I", "-c", program, *args])
    try:
        yield proc
    finally:
        proc.kill()  # by its PID
        proc.wait(timeout=20)


EARLY = ("agents_link", "md_loop")
"""Modes that plant into the emptied working folder before arr moves the fresh content in;
the others act on the fresh content before arr compares the folder."""


def hook_h1(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """Hold arr until the planter of the current attempt (`state["marker"]`) planted once:
    early modes at the start of the refill, the others right before `_verify` of the working
    folder. `state["verified"]` gets the time at which arr began to compare the folder."""
    refill, verify = folders.refill_work_dir, folders._verify

    def refilling(agent: Any, build: Path) -> list[str]:
        if state["mode"] in EARLY:
            state["held"] = hold_until(state["marker"])
        return refill(agent, build)

    def verifying(work_fd: int, placed: dict[str, int], label: Path) -> None:
        if label.name == "work":
            if state["mode"] not in EARLY:
                state["held"] = hold_until(state["marker"])
            state["verified"] = time.time()
        verify(work_fd, placed, label)

    monkeypatch.setattr(folders, "refill_work_dir", refilling)
    monkeypatch.setattr(folders, "_verify", verifying)


# --------------------------------------------------------------------------------------------
# H1, M1: a planter in the working folder while arr refills it
# --------------------------------------------------------------------------------------------

PLANTER = r"""
import json, os, sys, time
work, mode, bait_dir, bait_file, stop = sys.argv[1:6]
planted = []
def note(name):
    planted.append([name, time.time(), os.lstat(os.path.join(work, name)).st_ino])
    if len(planted) == 1:
        open(stop + ".first", "w").close()  # arr waits for this (`hold_until`)
def link(target, name):
    try:
        os.symlink(target, os.path.join(work, name)); note(name)
    except OSError:
        pass
while not os.path.exists(stop):
    if mode == "agents_link" and not os.listdir(work):  # filed away, not yet refilled
        link(bait_dir, ".agents")  # the folder of the Codex skills, before arr moves it in
    elif mode == "md_loop" and not os.listdir(work):  # filed away, not yet refilled
        link(bait_file, "AGENTS.md")  # again and again, before arr moves the fresh one in
    elif mode == "foreign" and os.path.lexists(os.path.join(work, "check.py")):
        try:
            open(os.path.join(work, "fremd.txt"), "x").write("fremd"); note("fremd.txt")
        except OSError:
            pass
    elif mode == "sub_swap":
        sub = os.path.join(work, "sub")
        if os.path.isdir(sub) and not os.path.islink(sub):
            try:
                os.rename(sub, sub + ".alt"); os.symlink(bait_dir, sub); note("sub")
            except OSError:
                pass
    elif mode == "replace":  # AGENTS.md and CLAUDE.md again and again as links
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = os.path.join(work, name)
            if os.path.isfile(path) and not os.path.islink(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            link(bait_file, name)
open(stop + ".planted", "w").write(json.dumps(planted))
"""


def big_project(tmp_path: Path) -> Path:
    """The workfolder of `folder_workfolder` with 1500 files in `sub/` (a long refill)."""
    workfolder = folder_workfolder(tmp_path)
    sub = workfolder / "projects" / "demo" / "sub"
    for n in range(1500):
        (sub / f"t{n:04d}.txt").write_text("x\n")
    return workfolder


@pytest.mark.parametrize("mode", ["agents_link", "md_loop", "foreign", "sub_swap", "replace"])
async def test_h1_a_planter_never_makes_arr_write_outside_and_never_stays_unseen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    workfolder = big_project(tmp_path)
    seen: dict[str, Any] = {
        "mode": mode,
        "tries": 0,
        "errors": 0,
        "unseen": [],
        "changed": 0,
        "left_after_ok": 0,
        "planted": 0,
        "unattacked": 0,
    }
    state: dict[str, Any] = {"mode": mode, "marker": None}
    hook_h1(monkeypatch, state)

    async def topology(ctx: RunContext) -> None:
        folder = ctx.prepared.agents["x-copy"].work_dir
        start = sorted(os.listdir(folder))
        for attempt in range(1, ATTEMPTS + 1):
            bait = make_bait(tmp_path / f"koeder-{mode}-{attempt}")
            bait_file = bait / "datei.txt"
            before = bait_state(bait)
            stop = tmp_path / f"stop-{mode}-{attempt}"
            args = (str(folder), mode, str(bait), str(bait_file), str(stop))
            state.update(marker=first(stop), verified=None, held=None)
            with process(PLANTER, *args):
                seen["tries"] += 1
                try:
                    await ctx.fresh_folder("x-copy", stage="p", attempt=attempt)
                    failed = False
                except ArrError:
                    failed = True
                    seen["errors"] += 1
                planted = _planted(stop)
            if bait_state(bait) != before:
                seen["changed"] += 1
            seen["planted"] += len(planted)
            # attacked: planted, and arr never went on without it (the hook either saw the
            # first plant or was not reached because arr stopped at the attack before)
            seen["unattacked"] += not planted or state["held"] is False
            here = [(n, at) for n, at, inode in planted if _inode(folder / n) == inode]
            if not failed and state["verified"]:  # planted before arr compared, still there
                seen["unseen"] += [n for n, at in here if at < state["verified"]]
            if not failed and here:
                seen["left_after_ok"] += 1  # reported; after `_verify` it is Entscheidung 52
            clear_flags(folder)
            for name in set(os.listdir(folder)) - set(start):  # what the planter left
                path = folder / name
                path.unlink() if path.is_symlink() or path.is_file() else None
            for name in (".agents", "AGENTS.md", "CLAUDE.md", "sub"):
                if (folder / name).is_symlink():
                    (folder / name).unlink()

    await run_done(manager(workfolder, topology, [FOUR_AGENTS[1]]))
    print("RACE-H1", json.dumps(seen))
    assert seen["planted"] > 0, "the planter never planted: the run proves nothing"
    assert seen["unattacked"] == 0, "an attempt without an attack proves nothing"
    assert seen["changed"] == 0, seen
    assert seen["unseen"] == [], seen


# --------------------------------------------------------------------------------------------
# H2: a swapper in the scratch folder while arr deletes it
# --------------------------------------------------------------------------------------------

SWAPPER = r"""
import os, sys
entry, bait, stop = sys.argv[1:4]
spare, hold = entry + ".echt", entry + ".link"
os.symlink(bait, hold)
n = 0
while not os.path.exists(stop):
    try:
        os.rename(entry, spare)
    except OSError:
        continue
    try:
        os.rename(hold, entry); n += 1
        if n == 1:
            open(stop + ".first", "w").close()  # arr waits for this (`hold_until`)
        os.rename(entry, hold)
    except OSError:
        pass
    try:
        os.rename(spare, entry)
    except OSError:
        pass
if os.path.lexists(hold):
    os.unlink(hold)
open(stop + ".n", "w").write(str(n))
"""


def locked_tree(root: Path, size: int = 40) -> None:
    for n in range(size):
        folder = root / f"d{n:02d}"
        folder.mkdir(parents=True)
        (folder / "f.txt").write_text("x")
        if sys.platform == "darwin":
            os.chflags(folder / "f.txt", stat.UF_IMMUTABLE)
        os.chmod(folder, 0o500)


def test_h2_deleting_changes_nothing_outside_while_a_folder_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After review rv13: `remove_quietly` (finalizer, emptying the scratch, slimming) on a
    scratch folder whose subfolder `a` a process swaps for a link to a bait all the time. arr
    is held where it opens `a` until the swapper has swapped once."""
    changed = swaps = unattacked = 0
    tries = 10 * ATTEMPTS
    state: dict[str, Any] = {"marker": None, "held": None}
    enter = folders._enter

    def entering(parent_fd: int, name: str, label: str, failed: list[str]) -> int | None:
        if name == "a" and state["held"] is None:
            state["held"] = hold_until(state["marker"])
        return enter(parent_fd, name, label, failed)

    monkeypatch.setattr(folders, "_enter", entering)
    for attempt in range(tries):
        bait = tmp_path / f"koeder-{attempt}"
        locked_tree(bait)
        before = bait_state(bait)
        scratch = tmp_path / f"scratch-{attempt}"
        locked_tree(scratch / "a", 200)  # a longer walk, a wider window
        stop = tmp_path / f"stop-{attempt}"
        state.update(marker=first(stop), held=None)
        with process(SWAPPER, str(scratch / "a"), str(bait), str(stop)) as proc:
            folders.remove_quietly(scratch)
            stop.write_text("")
            proc.wait(timeout=20)
        made = int(Path(f"{stop}.n").read_text())
        swaps += made
        unattacked += made == 0 or state["held"] is False
        changed += bait_state(bait) != before
        clear_flags(bait)
        if os.path.lexists(scratch):
            clear_flags(scratch)
    print(
        "RACE-H2",
        json.dumps({"tries": tries, "swaps": swaps, "changed": changed, "unattacked": unattacked}),
    )
    assert swaps > 0 and unattacked == 0, "an attempt without a swap proves nothing"
    assert changed == 0


# --------------------------------------------------------------------------------------------
# G1: the entry `work` or `scratch/<id>` itself swapped for a link during fresh_folder
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["work", "scratch"])
async def test_g1_swapping_the_folder_itself_never_reaches_the_bait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """A process swaps `work` or `scratch/<id>` itself for a link to a bait all the time;
    arr is held where it opens that folder until the swapper has swapped once."""
    workfolder = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {
        "which": which,
        "tries": 0,
        "errors": 0,
        "changed": 0,
        "swaps": 0,
        "unattacked": 0,
    }
    state: dict[str, Any] = {"marker": None, "held": None, "name": None}
    agent_folder = folders._agent_folder

    def opening(path: Path) -> Any:
        if path.name == state["name"] and state["held"] is None:
            state["held"] = hold_until(state["marker"])
        return agent_folder(path)

    monkeypatch.setattr(folders, "_agent_folder", opening)

    async def topology(ctx: RunContext) -> None:
        run_dir = ctx.prepared.run_dir
        agent = ctx.prepared.agents["c-copy"]
        entry = run_dir / "scratch" / "c-copy" if which == "scratch" else agent.work_dir
        state["name"] = entry.name
        for attempt in range(1, 3 * ATTEMPTS + 1):
            bait = make_bait(tmp_path / f"koeder-{which}-{attempt}", files=50)
            before = bait_state(bait)
            stop = tmp_path / f"stop-{which}-{attempt}"
            (entry / "eigen.txt").write_text("x")
            state.update(marker=first(stop), held=None)
            with process(SWAPPER, str(entry), str(bait), str(stop)) as proc:
                seen["tries"] += 1
                try:
                    await ctx.fresh_folder("c-copy", stage="r", attempt=attempt)
                except ArrError:
                    seen["errors"] += 1
                made = _swaps(stop, proc)
            seen["swaps"] += made
            seen["unattacked"] += made == 0 or state["held"] is False
            _put_back(entry)
            seen["changed"] += bait_state(bait) != before

    mgr = manager(workfolder, topology, [FOUR_AGENTS[0]])
    status = await mgr.run()
    clear_flags(mgr.run_dir)
    print("RACE-G1", json.dumps({**seen, "status": status.value}))
    assert status.value == "done" and seen["tries"] == 3 * ATTEMPTS, (status, mgr.error)
    assert seen["swaps"] > 0 and seen["unattacked"] == 0, "an attempt without a swap"
    assert seen["changed"] == 0, seen


def _planted(stop: Path) -> list[tuple[str, float, int]]:
    """End the planter and read what it planted when."""
    stop.write_text("")
    done = Path(f"{stop}.planted")
    for _ in range(1000):
        if done.exists():
            with contextlib.suppress(ValueError):
                return [tuple(p) for p in json.loads(done.read_text())]
        time.sleep(0.01)
    raise AssertionError("the planter did not answer")


def _inode(path: Path) -> int | None:
    try:
        return os.lstat(path).st_ino
    except OSError:
        return None


def _swaps(stop: Path, proc: subprocess.Popen[bytes]) -> int:
    stop.write_text("")
    proc.wait(timeout=20)
    return int(Path(f"{stop}.n").read_text())


def _put_back(entry: Path) -> None:
    """The real folder at its place again after the swapper ended."""
    if entry.is_symlink():
        entry.unlink()
    spare = Path(f"{entry}.echt")
    if spare.is_dir() and not entry.exists():
        os.rename(spare, entry)


@pytest.fixture(autouse=True)
def _owner_gets_it_back(tmp_path: Path) -> Iterator[None]:
    """Flags, ACLs and rights a test lays in its `tmp_path` go at the end, also on failure."""
    with owner_gets_it_back(tmp_path):
        yield

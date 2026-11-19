"""W4b-05a: fresh working folder, filed-away folders, scratch folder, cleaning up (FakeBackend).

`Orchestrator.fresh_folder` is called directly between two turns (the runner of W4b-02b calls
it later). The lock probes with the real CLIs are in `test_folders_probe.py`, the read rules
in `test_folders_policy.py`.
"""

import asyncio
import errno
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from folders_kit import (
    FOUR_AGENTS,
    OpenAudit,
    clear_flags,
    folder_workfolder,
    inodes,
    manager,
    run_done,
    tree,
    writer,
)
from run_kit import log_entries, turns, wait

from arr.backends.codex_config import project_config_state
from arr.errors import ArrError
from arr.models import RunStatus
from arr.runs import folders
from arr.runs.context import RunContext
from arr.runs.prepare import prepare_run

macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="file flags: macOS")


def work(ctx: Any, agent_id: str) -> Path:
    return ctx.prepared.agents[agent_id].work_dir


def scratch(ctx: Any, agent_id: str) -> Path:
    return ctx.prepared.run_dir / "scratch" / agent_id


def _touch(path: Path) -> Path:
    path.touch()
    return path


async def _first_file(folder: Path) -> None:
    """Until the writer made its first file."""
    for _ in range(1000):
        if any(n.startswith("neu-") for n in os.listdir(folder)):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the writer did not start")


def scribble(folder: Path) -> None:
    """What an agent leaves on its table: new, changed and overwritten generated files."""
    (folder / "notizen.md").write_text("Notiz aus Runde 1\n")
    (folder / "sub").mkdir(exist_ok=True)
    (folder / "sub" / "lemma.tex").write_text("geändert\n")
    (folder / "AGENTS.md").write_text("vom Agenten überschrieben\n")
    (folder / "out").mkdir()
    (folder / "out" / "daten.bin").write_bytes(os.urandom(64))


# --------------------------------------------------------------------------------------------
# A1: file for file the working folder of the start
# --------------------------------------------------------------------------------------------


async def test_a1_the_fresh_folder_equals_the_start_and_a_second_prepared_run(
    tmp_path: Path,
) -> None:
    wf = folder_workfolder(tmp_path)
    start: dict[str, Any] = {}
    fresh: dict[str, Any] = {}
    ids: dict[str, int] = {}

    async def topology(ctx: RunContext) -> None:
        for agent_id in ctx.prepared.agents:
            folder = work(ctx, agent_id)
            start[agent_id] = tree(folder)
            ids[agent_id] = os.lstat(folder).st_ino
            await ctx.turn(agent_id, "Runde 1")
            scribble(folder)
            await ctx.fresh_folder(agent_id, stage="Prüfung 2", attempt=1)
            fresh[agent_id] = tree(folder)
            assert os.lstat(folder).st_ino == ids[agent_id], "the folder stays the same"

    mgr = manager(wf, topology)
    run_dir = await run_done(mgr)
    assert set(fresh) == {"c-copy", "x-copy", "c-empty", "x-empty"}
    for agent_id in fresh:
        assert fresh[agent_id] == start[agent_id], agent_id
    # what the tree holds: copy with context files (CLAUDE.md too), Codex skills, empty
    assert {"notes.md", "sub/lemma.tex", "AGENTS.md", "CLAUDE.md"} <= set(fresh["c-copy"])
    assert ".agents/skills/sympy-check/SKILL.md" in fresh["x-copy"]
    assert ".agents/skills/sympy-check/SKILL.md" in fresh["x-empty"]
    assert set(fresh["c-empty"]) == {".", "AGENTS.md"}
    # a second run prepared from the same workfolder with the same ID: the same folders
    other = tmp_path / "wf2"
    shutil.copytree(wf, other, ignore=shutil.ignore_patterns("runs"))
    second = prepare_run(other, mgr.run_input, mgr.resolved, mgr.settings, run_id=run_dir.name)
    for agent_id, agent in second.agents.items():
        assert tree(agent.work_dir) == fresh[agent_id], agent_id
    # the old content lies in retired/, the log names agent, phase, pass, target, duration
    old = run_dir / "retired" / "c-copy" / "001-pr-fung-2"
    assert (old / "notizen.md").read_text() == "Notiz aus Runde 1\n"
    assert (old / "AGENTS.md").read_text() == "vom Agenten überschrieben\n"
    entry = log_entries(run_dir, "folder_fresh")[0]["data"]
    assert entry["agent"] == "c-copy" and entry["stage"] == "Prüfung 2" and entry["attempt"] == 1
    assert entry["retired"] == "retired/c-copy/001-pr-fung-2" and entry["duration_s"] >= 0
    for mode in ("retired", "retired/c-copy", "retired/c-copy/001-pr-fung-2", "scratch"):
        if (run_dir / mode).exists():
            assert stat.S_IMODE(os.lstat(run_dir / mode).st_mode) == 0o700, mode


async def test_a1_codex_finds_its_project_config_as_at_the_start(tmp_path: Path) -> None:
    """`CodexBackend` compares `.codex/` with its state at the start before every thread."""
    wf = folder_workfolder(tmp_path)
    states: list[str] = []

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "x-copy")
        states.append(project_config_state(folder))
        (folder / ".codex" / "config.toml").write_text("[mcp_servers.x]\ncommand='x'\n")
        states.append(project_config_state(folder))
        await ctx.fresh_folder("x-copy", stage="p", attempt=1)
        states.append(project_config_state(folder))

    await run_done(manager(wf, topology, [FOUR_AGENTS[1]]))
    assert states[0] == states[2] != states[1]
    assert states[0] != "absent"


# --------------------------------------------------------------------------------------------
# A3: nothing old stays, no link followed, no content read
# --------------------------------------------------------------------------------------------


@macos_only
async def test_a3_hard_entries_are_filed_away_without_reading_or_following(
    tmp_path: Path,
) -> None:
    wf = folder_workfolder(tmp_path)
    outside = tmp_path / "draussen"
    outside.mkdir()
    (outside / "geheim.txt").write_text("bleibt\n")
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "c-copy")
        before = inodes(folder)
        (folder / "zu").mkdir()
        (folder / "zu" / "innen.txt").write_text("x")
        os.chmod(folder / "zu", 0)  # a folder without rights
        (folder / "fest.txt").write_text("x")
        os.chflags(folder / "fest.txt", stat.UF_IMMUTABLE)  # a file flag
        (folder / "tief").mkdir()
        (folder / "tief" / "fest.txt").write_text("x")
        os.chflags(folder / "tief" / "fest.txt", stat.UF_IMMUTABLE)
        (folder / "lesbar-nicht.txt").write_text("x")
        os.chmod(folder / "lesbar-nicht.txt", 0)
        os.mkfifo(folder / "rohr")  # reading it would block
        (folder / "link-datei").symlink_to(outside / "geheim.txt")
        (folder / "link-ordner").symlink_to(outside)
        (folder / "kaputt").symlink_to(tmp_path / "gibt-es-nicht")
        many = folder / "viele"
        many.mkdir()
        for n in range(3000):
            (many / f"d{n}.txt").write_text("x")
        os.truncate(_touch(folder / "gross.bin"), 8 << 30)  # 8 GB, sparse: never read
        os.chflags(folder, stat.UF_IMMUTABLE)  # even the folder itself
        audit = OpenAudit(folder, ctx.prepared.run_dir / "retired" / "c-copy")
        with audit:
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        seen["opened"] = [p for p in audit.opened if not p.endswith(("AGENTS.md", "CLAUDE.md"))]
        seen["old_left"] = before & inodes(folder)
        seen["names"] = sorted(os.listdir(folder))
        seen["flags"] = os.lstat(folder).st_flags

    mgr = manager(wf, topology, [FOUR_AGENTS[0]])
    try:
        run_dir = await run_done(mgr)
        old = run_dir / "retired" / "c-copy" / "001-p"
        assert seen["opened"] == [], seen["opened"][:5]
        assert not seen["old_left"]
        assert "zu" not in seen["names"] and "gross.bin" not in seen["names"]
        assert seen["flags"] & stat.UF_IMMUTABLE == 0
        assert os.readlink(old / "link-datei") == str(outside / "geheim.txt")
        assert (outside / "geheim.txt").read_text() == "bleibt\n"  # untouched outside
        assert sorted(os.listdir(outside)) == ["geheim.txt"]
        assert stat.S_ISFIFO(os.lstat(old / "rohr").st_mode)
        assert len(os.listdir(old / "viele")) == 3000
        assert os.lstat(old / "gross.bin").st_size == 8 << 30
        assert os.lstat(old / "tief" / "fest.txt").st_flags & stat.UF_IMMUTABLE
    finally:
        clear_flags(mgr.run_dir)


async def test_a3_a_process_writing_into_an_open_file_does_not_stop_the_fresh_folder(
    tmp_path: Path,
) -> None:
    """The writer keeps its handle: it writes on into the moved file in `retired/`."""
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}
    program = (
        "import sys, time\n"
        "f = open(sys.argv[1], 'a', buffering=1)\n"
        "while True:\n    f.write('x\\n'); time.sleep(0.005)\n"
    )

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "c-copy")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", program, str(folder / "log.txt")
        )
        try:
            await asyncio.sleep(0.3)
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
            seen["names"] = sorted(os.listdir(folder))
            moved = ctx.prepared.run_dir / "retired" / "c-copy" / "001-p" / "log.txt"
            size = moved.stat().st_size
            await asyncio.sleep(0.3)
            seen["grew"] = moved.stat().st_size > size
        finally:
            process.kill()
            await process.wait()

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert "log.txt" not in seen["names"]
    assert seen["grew"], "the process wrote on into the filed-away file"


async def test_a3_a_process_adding_files_all_the_time_gives_fresh_or_arr_error_with_the_path(
    tmp_path: Path,
) -> None:
    """A writer that never stops: after the call the folder is either fresh (nothing from
    before it) or `fresh_folder` names a path in an `ArrError` instead of going on in a half
    old folder. The error comes at least once in three tries (the writer is much faster than
    five rounds); without the writer the same call makes the folder fresh."""
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {"errors": [], "old_left": []}

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "c-copy")
        for attempt in range(1, 4):
            (folder / f"vorher-{attempt}.txt").write_text("x")
            with writer(folder):
                await _first_file(folder)
                before = inodes(folder)
                try:
                    await ctx.fresh_folder("c-copy", stage="p", attempt=attempt)
                except ArrError as exc:
                    seen["errors"].append(exc.message)
                else:
                    seen["old_left"].append(before & inodes(folder))
        await ctx.fresh_folder("c-copy", stage="p", attempt=4)
        seen["names"] = sorted(os.listdir(folder))

    mgr = manager(wf, topology, [FOUR_AGENTS[0]])
    run_dir = await run_done(mgr)
    assert seen["errors"], "the writer never kept the folder from becoming fresh"
    for message in seen["errors"]:
        assert "agents/c-copy/work/" in message and "nicht ablegen" in message
    assert not any(seen["old_left"])
    assert not any(n.startswith(("neu-", "vorher-")) for n in seen["names"])
    failed = log_entries(run_dir, "folder_fresh_failed")
    assert len(failed) == len(seen["errors"])
    assert all("nicht ablegen" in f["data"]["message"] for f in failed)


async def test_a3_an_entry_arr_cannot_move_gives_arr_error_with_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}
    rename = os.rename

    def stuck(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if str(src).endswith("klemmt.txt"):
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), str(src))
        rename(src, dst, *args, **kwargs)

    async def topology(ctx: RunContext) -> None:
        (work(ctx, "c-copy") / "klemmt.txt").write_text("x")
        monkeypatch.setattr(folders.os, "rename", stuck)
        with pytest.raises(ArrError) as caught:
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        seen["message"] = caught.value.message

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert "agents/c-copy/work/klemmt.txt" in seen["message"]
    assert "Resource busy" in seen["message"]


# --------------------------------------------------------------------------------------------
# A5: scratch folder; A9: what survives
# --------------------------------------------------------------------------------------------


async def test_a5_the_scratch_folder_is_emptied_without_rest_and_without_diff(
    tmp_path: Path,
) -> None:
    wf = folder_workfolder(tmp_path)
    outside = tmp_path / "draussen.txt"
    outside.write_text("bleibt\n")
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        own = scratch(ctx, "c-copy")
        assert own.is_dir() and stat.S_IMODE(own.stat().st_mode) == 0o700
        (own / "gross").mkdir()
        (own / "gross" / "teil.bin").write_bytes(b"x" * 1024)
        (own / "link").symlink_to(outside)
        os.chmod(own / "gross", 0o500)
        (scratch(ctx, "x-copy") / "fremd.txt").write_text("x")
        await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        seen["own"] = os.listdir(own)
        seen["other"] = os.listdir(scratch(ctx, "x-copy"))
        seen["mode"] = stat.S_IMODE(own.stat().st_mode)

    run_dir = await run_done(manager(wf, topology, FOUR_AGENTS[:2]))
    assert seen["own"] == [] and seen["mode"] == 0o700
    assert seen["other"] == ["fremd.txt"], "another agent's scratch stays"
    assert outside.read_text() == "bleibt\n"
    assert os.listdir(run_dir / "retired" / ".trash") == []
    diffs = [p.name for p in (run_dir / "agents" / "c-copy").glob("*.diff")]
    assert sorted(diffs) == ["changes.diff", "retired-001-p.diff"]
    assert "teil.bin" not in (run_dir / "agents" / "c-copy" / "retired-001-p.diff").read_text()
    entry = log_entries(run_dir, "folder_fresh")[0]["data"]
    assert entry["scratch_removed"] == 2 and entry["scratch_left"] == []


async def test_a9_run_tmp_and_a_hard_link_there_survive_the_fresh_folder(tmp_path: Path) -> None:
    """Entscheidung 52: Run-tmp lives on, a hard link from it keeps an old file. Working and
    scratch folder hold nothing old (inodes)."""
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        folder, own = work(ctx, "c-copy"), scratch(ctx, "c-copy")
        tmp = ctx.prepared.run_dir / "tmp" / "c-copy"
        (folder / "alt.txt").write_text("alter Inhalt\n")
        (own / "wegwerf.txt").write_text("x")
        (tmp / "notiz.txt").write_text("im Run-tmp\n")
        os.link(folder / "alt.txt", tmp / "harter-link.txt")
        old = inodes(folder) | inodes(own)
        await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        seen["tmp"] = {p.name: p.read_text() for p in tmp.iterdir() if p.is_file()}
        seen["old_left"] = old & (inodes(folder) | inodes(own))

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen["tmp"]["notiz.txt"] == "im Run-tmp\n"
    assert seen["tmp"]["harter-link.txt"] == "alter Inhalt\n"
    assert not seen["old_left"]


# --------------------------------------------------------------------------------------------
# A14: shared folder, two calls, a call during a running turn, order with reset_session
# --------------------------------------------------------------------------------------------


async def test_a14_a_shared_folder_never_becomes_fresh(tmp_path: Path) -> None:
    wf = folder_workfolder(tmp_path)
    agents = [{"id": "a", "workspace": {"mode": "shared", "name": "beweis"}}, {"id": "b"}]
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "a")
        (folder / "gemeinsam.md").write_text("x")
        with pytest.raises(ArrError) as caught:
            await ctx.fresh_folder("a", stage="p", attempt=1)
        seen["message"] = caught.value.message
        seen["still"] = (folder / "gemeinsam.md").is_file()
        with pytest.raises(ArrError):
            await ctx.fresh_folder("niemand", stage="p", attempt=1)

    run_dir = await run_done(manager(wf, topology, agents))
    assert "gemeinsamen Ordner" in seen["message"] and seen["still"]
    assert not (run_dir / "retired").exists()


async def test_a14_two_calls_in_a_row_file_away_twice(tmp_path: Path) -> None:
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "c-copy")
        start = tree(folder)
        (folder / "a.txt").write_text("1")
        await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        await ctx.fresh_folder("c-copy", stage="p", attempt=2)
        seen["same"] = tree(folder) == start

    run_dir = await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen["same"]
    root = run_dir / "retired" / "c-copy"
    assert sorted(os.listdir(root)) == ["001-p", "002-p"]
    assert (root / "001-p" / "a.txt").is_file() and not (root / "002-p" / "a.txt").exists()
    assert (root / "002-p" / "AGENTS.md").is_file(), "the second holds the fresh content"
    attempts = [e["data"]["attempt"] for e in log_entries(run_dir, "folder_fresh")]
    assert attempts == [1, 2]


async def test_a14_a_call_during_a_running_turn_waits_for_its_end(tmp_path: Path) -> None:
    wf = folder_workfolder(tmp_path)
    order: list[str] = []
    script = {"c-copy": turns([wait(0.5)], [])}

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "c-copy")

        async def turn() -> None:
            await ctx.turn("c-copy", "lang")
            order.append("turn")
            order.append("old" if (folder / "a.txt").exists() else "fresh")

        (folder / "a.txt").write_text("1")
        running = asyncio.create_task(turn())
        await asyncio.sleep(0.1)
        assert ctx.is_busy("c-copy")
        await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        order.append("fresh_folder")
        await running

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]], scripts=script))
    assert order == ["turn", "old", "fresh_folder"]


async def test_a14_a_turn_asked_for_during_fresh_folder_starts_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf = folder_workfolder(tmp_path)
    order: list[str] = []
    slow = folders.fresh_folder

    def slowly(*args: Any, **kwargs: Any) -> Any:
        import time

        time.sleep(0.3)
        done = slow(*args, **kwargs)
        order.append("fresh_folder")
        return done

    monkeypatch.setattr(folders, "fresh_folder", slowly)

    async def topology(ctx: RunContext) -> None:
        task = asyncio.create_task(ctx.fresh_folder("c-copy", stage="p", attempt=1))
        await asyncio.sleep(0.05)
        await ctx.turn("c-copy", "x")
        order.append("turn")
        await task

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert order == ["fresh_folder", "turn"]


@pytest.mark.parametrize("first", ["reset_session", "fresh_folder"])
async def test_a14_both_orders_with_reset_session_are_safe(tmp_path: Path, first: str) -> None:
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        folder = work(ctx, "c-copy")
        start = tree(folder)
        await ctx.turn("c-copy", "1")
        (folder / "a.txt").write_text("1")
        if first == "reset_session":
            await ctx.reset_session("c-copy")
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        else:
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
            await ctx.reset_session("c-copy")
        outcome = await ctx.turn("c-copy", "2")
        seen["status"] = outcome.status.value
        seen["same"] = tree(folder) == start

    run_dir = await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert seen == {"status": "ok", "same": True}
    kinds = [
        e["kind"] for e in log_entries(run_dir) if e["kind"] in ("session_reset", "folder_fresh")
    ]
    assert kinds == (
        ["session_reset", "folder_fresh"]
        if first == "reset_session"
        else ["folder_fresh", "session_reset"]
    )


# --------------------------------------------------------------------------------------------
# A10, A12: the finalizer
# --------------------------------------------------------------------------------------------


def lay_staging_rests(run_dir: Path, outside: Path) -> dict[str, Path]:
    staging = run_dir / "groupchats" / "staging"
    sealed = run_dir / "groupchats" / "sealed"
    staging.mkdir(parents=True, exist_ok=True)
    sealed.mkdir(parents=True, exist_ok=True)
    rests = {
        "submission": staging / ".submission-0f0f",
        "orphan": staging / ".orphan-1a1a",
        "staging": staging / ".staging-2b2b",
        "lock": staging / ".version-beweis.lock",
        "link": staging / ".submission-link",
    }
    rests["submission"].mkdir()
    (rests["submission"] / "body.md").write_text("x")
    rests["orphan"].mkdir()
    rests["staging"].write_text("x")
    rests["lock"].write_text("")
    rests["link"].symlink_to(outside)
    kept = {
        "full_lock": staging / ".version-other.lock",
        "sealed_rest": sealed / ".submission-3c3c",
        "sealed": sealed / "submission-001",
    }
    kept["full_lock"].write_text("held")
    kept["sealed_rest"].mkdir()
    kept["sealed"].mkdir()
    (kept["sealed"] / "body.md").write_text("versiegelt")
    return {
        **{f"gone:{k}": v for k, v in rests.items()},
        **{f"kept:{k}": v for k, v in kept.items()},
    }


async def test_a10_the_finalizer_cleans_up_and_writes_a_diff_per_filed_away_folder(
    tmp_path: Path,
) -> None:
    wf = folder_workfolder(tmp_path)
    outside = tmp_path / "draussen"
    outside.mkdir()
    (outside / "x.txt").write_text("bleibt")
    places: dict[str, Path] = {}

    async def topology(ctx: RunContext) -> None:
        run_dir = ctx.prepared.run_dir
        folder = work(ctx, "c-copy")
        (folder / "runde1.md").write_text("Runde 1\n")
        await ctx.fresh_folder("c-copy", stage="eins", attempt=1)
        (folder / "runde2.md").write_text("Runde 2\n")
        (folder / "notes.md").unlink()
        await ctx.fresh_folder("c-copy", stage="zwei", attempt=1)
        (folder / "runde3.md").write_text("Runde 3\n")
        (scratch(ctx, "c-copy") / "rest.bin").write_bytes(b"x")
        places.update(lay_staging_rests(run_dir, outside))
        submissions = run_dir / "output" / "submissions"
        submissions.mkdir(parents=True, exist_ok=True)
        places["gone:orphan_folder"] = submissions / "001-eins-c-copy"
        places["gone:orphan_folder"].mkdir()
        (places["gone:orphan_folder"] / "body.md").write_text("halb")

    run_dir = await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    for name, path in places.items():
        assert path.is_symlink() or path.exists() is name.startswith("kept:"), name
        assert not (path.is_symlink() and name.startswith("gone:")), name
    assert (outside / "x.txt").read_text() == "bleibt"
    assert (run_dir / "groupchats" / "sealed" / "submission-001" / "body.md").read_text() == (
        "versiegelt"
    )
    assert not (run_dir / "scratch").exists()
    agent_dir = run_dir / "agents" / "c-copy"
    first = (agent_dir / "retired-001-eins.diff").read_text()
    second = (agent_dir / "retired-002-zwei.diff").read_text()
    assert "+Runde 1" in first and "runde2" not in first
    assert "+Runde 2" in second and "notes.md" in second and "Runde 3" not in second
    assert "+Runde 3" in (agent_dir / "changes.diff").read_text()
    [cleanup] = log_entries(run_dir, "cleanup")
    assert "scratch" in cleanup["data"]["removed"]
    assert "output/submissions/001-eins-c-copy" in cleanup["data"]["removed"]
    assert cleanup["data"]["failed"] == []


async def test_a10_known_submission_folders_stay(tmp_path: Path) -> None:
    """Counter-check: a folder the database names stays, also one of a still sealed
    submission at its later place (a release killed between rename and commit)."""
    from arr.room.types import Submission

    run_dir = tmp_path / "run"
    folder = run_dir / "output" / "submissions"
    for name in ("001-eins-a", "002-eins-b", "003-eins-c"):
        (folder / name).mkdir(parents=True)

    def sub(number: int, path: str, sealed: bool = False) -> Submission:
        return Submission(
            id=number,
            ts=0,
            agent="a",
            shown_as="b" if number == 2 else "a",
            stage="eins",
            format="text",
            status="done",
            summary="s",
            path=path,
            sealed=sealed,
        )

    done = folders.clean_up_run(
        run_dir,
        [
            sub(1, "output/submissions/001-eins-a"),
            sub(2, "groupchats/sealed/submission-002", sealed=True),
        ],
    )
    assert sorted(os.listdir(folder)) == ["001-eins-a", "002-eins-b"]
    assert done.removed == ["output/submissions/003-eins-c"]
    # without a room (no database) nothing in output/ is touched
    (folder / "004-x-y").mkdir()
    folders.clean_up_run(run_dir, None)
    assert (folder / "004-x-y").is_dir()


async def test_a12_a_file_arr_cannot_delete_is_a_message_and_the_run_ends_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf = folder_workfolder(tmp_path)
    rmtree = shutil.rmtree

    def refusing(path: Any, *args: Any, **kwargs: Any) -> None:
        if "scratch" in str(path):
            onexc = kwargs.get("onexc")
            if onexc is None:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            onexc(os.unlink, str(Path(path) / "c-copy" / "fest.bin"), PermissionError())
            return
        rmtree(path, *args, **kwargs)

    async def topology(ctx: RunContext) -> None:
        (scratch(ctx, "c-copy") / "fest.bin").write_bytes(b"x")
        monkeypatch.setattr(folders.shutil, "rmtree", refusing)

    mgr = manager(wf, topology, [FOUR_AGENTS[0]])
    run_dir = await run_done(mgr)
    [cleanup] = log_entries(run_dir, "cleanup")
    assert cleanup["data"]["failed"] == ["scratch/c-copy/fest.bin"]
    assert not log_entries(run_dir, "error")
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "done"


async def test_a12_an_error_of_fresh_folder_ends_the_run_with_its_message(tmp_path: Path) -> None:
    """The runner (W4b-02b) lets the `ArrError` end the run; the finalizer still runs."""
    wf = folder_workfolder(tmp_path)
    agents = [{"id": "a", "workspace": {"mode": "shared", "name": "beweis"}}]

    async def topology(ctx: RunContext) -> None:
        await ctx.fresh_folder("a", stage="p", attempt=1)

    mgr = manager(wf, topology, agents)
    assert await mgr.run() is RunStatus.FAILED
    assert isinstance(mgr.error, ArrError)
    assert (mgr.run_dir / "output" / "summary.md").is_file()


# --------------------------------------------------------------------------------------------
# A13: a run without fresh_folder
# --------------------------------------------------------------------------------------------


async def test_a13_a_run_without_fresh_folder_has_the_run_folder_of_before(
    tmp_path: Path,
) -> None:
    """Besides `scratch/` (made at the start, deleted by the finalizer, one log entry
    `cleanup`) nothing is new: no `retired/`, no new diff, no other new log entry."""
    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        seen["during"] = sorted(os.listdir(ctx.prepared.run_dir))
        for agent_id in ctx.prepared.agents:
            await ctx.turn(agent_id, "x")

    run_dir = await run_done(manager(wf, topology))
    assert "scratch" in seen["during"] and "retired" not in seen["during"]
    top = sorted(os.listdir(run_dir))
    assert "scratch" not in top and "retired" not in top
    assert top == sorted(
        [
            "agents",
            "approvals.jsonl",
            "base",
            "gatekeeper.jsonl",
            "groupchats",
            "meta.json",
            "orchestrator.jsonl",
            "output",
            "run.input.yaml",
            "run.yaml",
            "skills",
            "task.md",
            "tmp",
            "usage.json",
        ]
    ) or set(top) - {"gatekeeper.jsonl"} == {
        "agents",
        "approvals.jsonl",
        "base",
        "groupchats",
        "meta.json",
        "orchestrator.jsonl",
        "output",
        "run.input.yaml",
        "run.yaml",
        "skills",
        "task.md",
        "tmp",
        "usage.json",
    }
    for agent_id in ("c-copy", "x-copy", "c-empty", "x-empty"):
        names = os.listdir(run_dir / "agents" / agent_id)
        assert not [n for n in names if n.startswith("retired-")], names
    kinds = {e["kind"] for e in log_entries(run_dir)}
    assert not kinds & {"folder_fresh", "folder_fresh_failed"}
    [cleanup] = log_entries(run_dir, "cleanup")
    assert cleanup["data"] == {"removed": ["scratch"], "failed": [], "where": "finalizer"}


# --------------------------------------------------------------------------------------------
# A11: slimming
# --------------------------------------------------------------------------------------------


async def _run_with_a_filed_away_folder(tmp_path: Path) -> Path:
    wf = folder_workfolder(tmp_path)

    async def topology(ctx: RunContext) -> None:
        (work(ctx, "c-copy") / "runde1.md").write_text("Runde 1\n")
        await ctx.fresh_folder("c-copy", stage="eins", attempt=1)
        (ctx.prepared.run_dir / "tmp" / "c-copy" / "cache.bin").write_bytes(b"x" * 100)

    return await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))


async def test_a11_slimming_removes_retired_scratch_and_tmp_and_keeps_the_diffs(
    tmp_path: Path,
) -> None:
    from arr.runs.slim import plan_slim, slim_run

    run_dir = await _run_with_a_filed_away_folder(tmp_path)
    outside = tmp_path / "draussen"
    outside.mkdir()
    (outside / "bleibt.txt").write_text("wichtig")
    (run_dir / "scratch").symlink_to(outside)  # a link at a target goes as a link
    (run_dir / "retired" / "c-copy" / "001-eins" / "link").symlink_to(outside)
    plan = plan_slim(run_dir)
    names = {p.relative_to(run_dir).as_posix() for p in plan.targets}
    assert {"retired", "scratch", "tmp", "base", "agents/c-copy/work"} <= names
    assert "Diffs abgelegter Ordner" in plan.kept and not plan.unsaved
    slim_run(run_dir)
    for gone in ("retired", "scratch", "tmp", "base", "agents/c-copy/work"):
        assert not (run_dir / gone).is_symlink() and not (run_dir / gone).exists(), gone
    assert (outside / "bleibt.txt").read_text() == "wichtig"
    assert sorted(os.listdir(outside)) == ["bleibt.txt"]
    agent_dir = run_dir / "agents" / "c-copy"
    assert "+Runde 1" in (agent_dir / "retired-001-eins.diff").read_text()
    assert (agent_dir / "changes.diff").is_file()
    marker = json.loads((run_dir / "slimmed.json").read_text())
    assert {"retired", "tmp"} <= set(marker["removed"])


async def test_a11_a_filed_away_folder_without_its_diff_counts_as_unsaved(tmp_path: Path) -> None:
    from arr.runs.slim import plan_slim

    run_dir = await _run_with_a_filed_away_folder(tmp_path)
    assert not plan_slim(run_dir).unsaved
    (run_dir / "agents" / "c-copy" / "retired-001-eins.diff").unlink()
    assert plan_slim(run_dir).unsaved


async def test_a12_any_error_of_the_file_system_in_fresh_folder_is_an_arr_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arr.runs import prepare

    wf = folder_workfolder(tmp_path)
    seen: dict[str, Any] = {}

    def full(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), "AGENTS.md")

    async def topology(ctx: RunContext) -> None:
        monkeypatch.setattr(prepare, "_write_context", full)
        with pytest.raises(ArrError) as caught:
            await ctx.fresh_folder("c-copy", stage="p", attempt=1)
        seen["message"] = caught.value.message

    await run_done(manager(wf, topology, [FOUR_AGENTS[0]]))
    assert "keinen frischen Arbeitsordner" in seen["message"]
    assert "No space left on device" in seen["message"]

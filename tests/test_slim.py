"""Slimming a finished run (W3-02 §3.5, A6): what goes, what stays and what is never touched."""

import json
import os
from pathlib import Path

import pytest
from run_kit import write_profiles
from service_kit import hold_lock, make_run_folder, make_workfolder

from arr.models import RunStatus
from arr.room.store import RoomStore
from arr.runs import slim as slim_module
from arr.runs.slim import SLIM_MARKER, plan_slim, slim_run
from arr.service.api_models import ApiException, ErrorCode, RunAction, RunStatusEvent
from arr.service.real_api import RealApi

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    monkeypatch.setenv("ARR_VENV_ROOT", str(tmp_path / "venv-root"))
    return make_workfolder(tmp_path)


@pytest.fixture
def api(wf: Path) -> RealApi:
    return RealApi(wf, base_url=BASE)


def finished_run(wf: Path, status: RunStatus = RunStatus.DONE) -> Path:
    """A finished run with the files of docs/02 §4 that slimming must keep."""
    run_dir = make_run_folder(wf, agents=["a", "b"], status=status)
    for agent in ("a", "b"):
        folder = run_dir / "agents" / agent
        (folder / "transcript.jsonl").write_text("", "utf-8")
        (folder / "raw.jsonl").write_text('{"raw": 1}\n', "utf-8")
        (folder / "changes.diff").write_text("diff\n", "utf-8")
        (folder / "work" / "gross.bin").write_bytes(b"x" * 1000)
    (run_dir / "shared" / "team").mkdir(parents=True)
    (run_dir / "shared" / "team" / "datei.txt").write_bytes(b"y" * 500)
    (run_dir / "output" / "summary.md").write_text("# Ergebnis\n", "utf-8")
    (run_dir / "usage.json").write_text("{}", "utf-8")
    room = RoomStore(run_dir)
    room.post("main", "Hallo")
    return run_dir


def with_venv(run_dir: Path, location: Path) -> Path:
    """Record `location` as the run venv in `meta.json`."""
    meta = json.loads((run_dir / "meta.json").read_text("utf-8"))
    meta["run_venv"] = str(location)
    (run_dir / "meta.json").write_text(json.dumps(meta), "utf-8")
    return location


def real_venv(run_dir: Path) -> Path:
    location = Path(os.environ["ARR_VENV_ROOT"]) / "runs" / run_dir.name
    (location / "bin").mkdir(parents=True)
    (location / "bin" / "python").write_bytes(b"z" * 100)
    return with_venv(run_dir, location)


async def test_dry_run_names_what_goes_and_stays_and_changes_nothing(
    wf: Path, api: RealApi
) -> None:
    run_dir = finished_run(wf)
    real_venv(run_dir)
    before = sorted(str(p) for p in run_dir.rglob("*"))
    result = await api.run_action(run_dir.name, RunAction.SLIM, dry_run=True)
    assert result.freed_bytes is not None and result.freed_bytes >= 2600
    assert "Arbeitskopien, Snapshot und Run-venv" in result.message
    assert "Transkripte, Roh-Events, Diffs, Gruppenchats, Output und Usage" in result.message
    assert "logische Größe" in result.message and "APFS-Klone" in result.message
    assert sorted(str(p) for p in run_dir.rglob("*")) == before
    assert not (run_dir / SLIM_MARKER).exists()
    assert result.run is not None and not result.run.slimmed


async def test_slim_removes_copies_and_venv_keeps_protocols_and_stays_readable(
    wf: Path, api: RealApi
) -> None:
    run_dir = finished_run(wf)
    venv = real_venv(run_dir)
    sub = api.subscribe()
    result = await api.run_action(run_dir.name, RunAction.SLIM)
    assert "Verschlankt" in result.message and result.run is not None and result.run.slimmed
    assert not (run_dir / "base").exists() and not (run_dir / "shared").exists()
    for agent in ("a", "b"):
        folder = run_dir / "agents" / agent
        assert not (folder / "work").exists()
        for name in ("agent.json", "transcript.jsonl", "raw.jsonl", "changes.diff"):
            assert (folder / name).is_file(), name
    assert not venv.exists() and venv.parent.is_dir()
    for name in ("run.yaml", "task.md", "meta.json", "usage.json", "output/summary.md"):
        assert (run_dir / name).is_file(), name
    assert (run_dir / "groupchats" / "room.sqlite").is_file()
    marker = json.loads((run_dir / SLIM_MARKER).read_text("utf-8"))
    assert marker["run_venv"] is True and "base" in marker["removed"]
    assert str(wf) not in (run_dir / SLIM_MARKER).read_text("utf-8")
    page = await api.get_messages(run_dir.name, "main")
    assert page.items[-1].body == "Hallo"
    assert (await api.get_transcript(run_dir.name, "a")).total == 0
    detail = await api.get_run(run_dir.name)
    assert detail.run.slimmed and RunAction.SLIM not in detail.actions_allowed
    assert any(isinstance(e, RunStatusEvent) and e.run.slimmed for e in sub.drain())


async def test_second_slim_is_refused(wf: Path, api: RealApi) -> None:
    run_dir = finished_run(wf)
    await api.run_action(run_dir.name, RunAction.SLIM)
    with pytest.raises(ApiException) as error:
        await api.run_action(run_dir.name, RunAction.SLIM)
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert "schon verschlankt" in error.value.error.message


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.QUEUED])
async def test_active_run_is_refused(
    wf: Path, api: RealApi, status: RunStatus, dry_run: bool
) -> None:
    run_dir = finished_run(wf, status)
    with pytest.raises(ApiException) as error:
        await api.run_action(run_dir.name, RunAction.SLIM, dry_run=dry_run)
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert "Stoppe den Run zuerst" in error.value.error.message
    assert (run_dir / "base").is_dir()


@pytest.mark.parametrize("dry_run", [True, False])
async def test_finished_run_with_a_held_lock_is_refused(
    wf: Path, api: RealApi, dry_run: bool
) -> None:
    run_dir = finished_run(wf)
    lock = hold_lock(run_dir)
    try:
        with pytest.raises(ApiException) as error:
            await api.run_action(run_dir.name, RunAction.SLIM, dry_run=dry_run)
    finally:
        lock.release()
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert "noch abgeschlossen" in error.value.error.message
    assert (run_dir / "base").is_dir()


async def test_slim_holds_the_lock_while_it_deletes(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = finished_run(wf)
    held = []
    remove = slim_module._remove

    def spy(path: Path) -> None:
        held.append(slim_module.run_lock_held(run_dir))
        remove(path)

    monkeypatch.setattr(slim_module, "_remove", spy)
    slim_run(run_dir)
    assert held and all(held)


@pytest.mark.parametrize("place", ["outside", "other-run", "dotdot", "root-itself", "home"])
async def test_manipulated_venv_path_deletes_nothing_outside(
    wf: Path, api: RealApi, tmp_path: Path, place: str
) -> None:
    """W3-00 review G3: only `<root>/runs/<run-id>` of this very run is removed."""
    run_dir = finished_run(wf)
    root = Path(os.environ["ARR_VENV_ROOT"]) / "runs"
    victim = {
        "outside": tmp_path / "wichtig",
        "other-run": root / "20200101-0000_demo_custom_0000",
        "dotdot": root / ".." / ".." / run_dir.name,
        "root-itself": root,
        "home": tmp_path / "home",
    }[place]
    victim.mkdir(parents=True, exist_ok=True)
    keep = victim / "bleibt.txt"
    keep.write_text("wichtig", "utf-8")
    with_venv(run_dir, victim)
    result = await api.run_action(run_dir.name, RunAction.SLIM)
    assert result.run is not None and result.run.slimmed
    assert keep.read_text("utf-8") == "wichtig"
    assert not (run_dir / "base").exists()


async def test_linked_venv_keeps_its_target(wf: Path, api: RealApi, tmp_path: Path) -> None:
    run_dir = finished_run(wf)
    target = tmp_path / "ziel"
    target.mkdir()
    (target / "bleibt.txt").write_text("wichtig", "utf-8")
    location = Path(os.environ["ARR_VENV_ROOT"]) / "runs" / run_dir.name
    location.parent.mkdir(parents=True)
    location.symlink_to(target)
    with_venv(run_dir, location)
    await api.run_action(run_dir.name, RunAction.SLIM)
    assert (target / "bleibt.txt").is_file() and location.is_symlink()


async def test_old_run_with_venv_in_its_folder(wf: Path, api: RealApi) -> None:
    run_dir = finished_run(wf)
    (run_dir / ".venv" / "bin").mkdir(parents=True)
    await api.run_action(run_dir.name, RunAction.SLIM)
    assert not (run_dir / ".venv").exists()


@pytest.mark.parametrize("link", ["base", "shared", "agents/a/work", "agents/b", "agents"])
async def test_links_are_not_followed(wf: Path, tmp_path: Path, link: str) -> None:
    """A link at or above a target removes at most the link, never what it points to."""
    run_dir = finished_run(wf)
    outside = tmp_path / "draussen"
    (outside / "work").mkdir(parents=True)
    (outside / "a" / "work").mkdir(parents=True)
    for folder in (outside, outside / "work", outside / "a" / "work"):
        (folder / "bleibt.txt").write_text("wichtig", "utf-8")
    place = run_dir / link
    if place.is_dir():
        os.rename(place, tmp_path / "beiseite")
    place.symlink_to(outside)
    slim_run(run_dir)
    assert sorted(p.name for p in outside.rglob("bleibt.txt")) == ["bleibt.txt"] * 3


async def test_links_inside_a_copy_are_removed_as_links(wf: Path, tmp_path: Path) -> None:
    run_dir = finished_run(wf)
    outside = tmp_path / "draussen"
    outside.mkdir()
    (outside / "bleibt.txt").write_text("wichtig", "utf-8")
    (run_dir / "agents" / "a" / "work" / "link").symlink_to(outside)
    (run_dir / "base" / "datei-link").symlink_to(outside / "bleibt.txt")
    slim_run(run_dir)
    assert (outside / "bleibt.txt").read_text("utf-8") == "wichtig"
    assert not (run_dir / "agents" / "a" / "work").exists()


async def test_linked_run_folder_is_refused(wf: Path, tmp_path: Path) -> None:
    run_dir = finished_run(wf)
    moved = tmp_path / "anderswo"
    os.rename(run_dir, moved)
    run_dir.symlink_to(moved)
    with pytest.raises(Exception, match="ist ein Link"):
        slim_run(run_dir)
    assert (moved / "base").is_dir()


async def test_read_only_folders_go_too(wf: Path) -> None:
    run_dir = finished_run(wf)
    cache = run_dir / "agents" / "a" / "work" / "cache"
    cache.mkdir()
    (cache / "x").write_text("x", "utf-8")
    cache.chmod(0o500)
    slim_run(run_dir)
    assert not (run_dir / "agents" / "a" / "work").exists()


async def test_failed_slim_has_no_mark_and_can_be_repeated(
    wf: Path, api: RealApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = finished_run(wf)
    remove = slim_module._remove

    def fail(path: Path) -> None:
        raise PermissionError(13, "Keine Berechtigung", str(path))

    monkeypatch.setattr(slim_module, "_remove", fail)
    with pytest.raises(ApiException) as error:
        await api.run_action(run_dir.name, RunAction.SLIM)
    assert "nicht ganz verschlankt" in error.value.error.message
    assert not (run_dir / SLIM_MARKER).exists()
    monkeypatch.setattr(slim_module, "_remove", remove)
    await api.run_action(run_dir.name, RunAction.SLIM)
    assert (run_dir / SLIM_MARKER).is_file()


def test_plan_counts_files_and_does_not_follow_links(wf: Path, tmp_path: Path) -> None:
    run_dir = finished_run(wf)
    big = tmp_path / "gross.bin"
    big.write_bytes(b"x" * 100_000)
    (run_dir / "agents" / "a" / "work" / "link").symlink_to(big)
    assert plan_slim(run_dir).logical_bytes < 100_000

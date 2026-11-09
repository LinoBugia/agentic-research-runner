"""The Not-Aus and the record per profile (W3-06 §3.4, §3.5, A5–A7).

The stop of `arr run` is tested with a real second process: it only learns of the stop through
the run folder. Everything else runs on FakeBackends, without a model and without the keychain.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from run_kit import FAST, eventually, fake_builders, say, turns, wait, write_profiles
from service_kit import make_run_folder, make_workfolder, start_run_process, transcript_line

from arr.errors import ArrError
from arr.models import AuthProfile, ProfilesFile, RunStatus
from arr.resolve import load_yaml_model
from arr.runs.inspect import run_status
from arr.runs.launch import LiveFactory, plan_launch, refuse_disabled, run_launch
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import load_run_meta
from arr.runs.secret_scan import SCAN_KIND
from arr.service.api_models import ApiException, CreateProfileRequest, ErrorCode, RunAction
from arr.service.real_api import RealApi
from arr.service.real_api.profile_usage import NOTE
from arr.usage import AgentUsage, RunUsage, UsageTotal

BASE = "http://127.0.0.1:8765"
SOLO = """\
version: 1
project: demo
task: Teste arr.
topology: {kind: solo}
limits: {wall_time: 2m}
defaults: {provider: claude, model: fake-model, auth: fake-auth}
agents: [{id: a}]
groupchats: [{name: main, members: [a]}]
"""
RUN_INPUT = {
    "version": 1,
    "project": "demo",
    "task": "Teste arr.",
    "topology": {"kind": "solo"},
    "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
    "agents": [{"id": "a"}],
    "groupchats": [{"name": "main", "members": ["a"]}],
}
LONG = {"a": turns([say("x"), wait(60)])}


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}

    def delete(service: str, account: str) -> None:
        store.pop((service, account), None)

    monkeypatch.setattr("arr.auth._keyring_get", lambda s, a: store.get((s, a)))
    monkeypatch.setattr("arr.auth._keyring_set", lambda s, a, v: store.__setitem__((s, a), v))
    monkeypatch.setattr("arr.auth._keyring_delete", delete)
    return store


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, keychain: dict) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(
        arr_home,
        {
            "fake-auth": "claude_oauth",
            "fake-key": "claude_api_key",
            "fake-chat": "codex_chatgpt",
        },
    )
    return make_workfolder(tmp_path)


@pytest.fixture
async def api(wf: Path) -> AsyncIterator[RealApi]:
    service = RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders(LONG),
        scan=True,
        manager_kwargs={"pacing": FAST},
    )
    yield service
    await service.runs.stop_all()
    await service.close()


def disabled_in_file(arr_home: Path, name: str) -> bool:
    file = load_yaml_model(arr_home / "profiles.yaml", ProfilesFile)
    return next(p for p in file.profiles if p.name == name).disabled


def scanned(run_dir: Path) -> bool:
    return any(e["kind"] == SCAN_KIND for e in OrchestratorLog(run_dir).entries())


def status_of(wf: Path, run_id: str) -> RunStatus:
    return load_run_meta(wf / "runs" / run_id).status


# --------------------------------------------------------------------------------------------
# The block at the start: one place for every way (A6)
# --------------------------------------------------------------------------------------------


async def test_sperren_writes_the_field_and_keeps_the_rest(api: RealApi, arr_home: Path) -> None:
    result = await api.disable_profile("fake-auth")
    assert result.ok and result.profile.disabled and "gesperrt" in result.message
    assert disabled_in_file(arr_home, "fake-auth")
    assert not disabled_in_file(arr_home, "fake-key")
    listed = {p.name: p for p in await api.list_profiles()}
    assert listed["fake-auth"].disabled and not listed["fake-key"].disabled


async def test_a_dry_run_changes_nothing(api: RealApi, arr_home: Path) -> None:
    result = await api.disable_profile("fake-auth", dry_run=True)
    assert result.message == "Es läuft kein Run."
    assert not disabled_in_file(arr_home, "fake-auth")


async def test_no_run_starts_on_a_blocked_profile_in_the_gui(api: RealApi, wf: Path) -> None:
    await api.disable_profile("fake-auth")
    with pytest.raises(ApiException, match="gesperrt, der Agent läuft nicht an"):
        await api.start_run(RUN_INPUT)
    assert not [p for p in (wf / "runs").iterdir() if p.is_dir()], "not even a folder"


async def test_no_run_starts_on_a_blocked_profile_with_arr_run(api: RealApi, wf: Path) -> None:
    await api.disable_profile("fake-auth")
    run_yaml = wf / "run.yaml"
    run_yaml.write_text(SOLO, "utf-8")
    with pytest.raises(ArrError, match="gesperrt, der Agent läuft nicht an"):
        plan_launch(run_yaml, cwd=wf)


async def test_a_block_after_the_planning_still_stops_the_agent(
    api: RealApi, wf: Path, arr_home: Path
) -> None:
    run_yaml = wf / "run.yaml"
    run_yaml.write_text(SOLO, "utf-8")
    launch = plan_launch(run_yaml, cwd=wf)
    await api.disable_profile("fake-auth")
    started: list[str] = []

    def builder(agent: Any, **_kwargs: Any) -> Any:
        started.append(agent.id)
        raise AssertionError("a blocked profile never builds a backend")

    result = await run_launch(
        launch,
        decider=None,
        report=lambda _line: None,
        builders={provider: builder for provider in fake_builders(LONG)},
        signals=False,
        pacing=FAST,
    )
    assert started == [] and result.status in (RunStatus.FAILED, RunStatus.STOPPED)


def test_the_factory_reads_the_block_anew(arr_home: Path, wf: Path) -> None:
    file = ProfilesFile(profiles=[AuthProfile(name="fake-auth", kind="claude_oauth")])
    refuse_disabled(["fake-auth"], file)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    path = arr_home / "profiles.yaml"
    path.write_text(path.read_text().replace("claude_oauth}", "claude_oauth, disabled: true}"))
    with pytest.raises(ArrError, match="gesperrt"):
        refuse_disabled(["fake-auth"], file)
    refuse_disabled(["other"], file)
    assert LiveFactory(file, limits=None).file is file  # type: ignore[arg-type]


@pytest.mark.parametrize("damage", ["broken", "binary", "folder", "mode"])
def test_an_unreadable_block_stops_the_start(damage: str, arr_home: Path, wf: Path) -> None:
    path = arr_home / "profiles.yaml"
    known = ProfilesFile(profiles=[AuthProfile(name="fake-auth", kind="claude_oauth")])
    if damage == "broken":
        path.write_text("profiles: [{ kaputt")
    elif damage == "binary":
        path.write_bytes(b"\xff\xfe\x00kaputt")
    elif damage == "folder":
        path.unlink()
        path.mkdir()
    else:
        path.chmod(0)
    try:
        with pytest.raises(ArrError, match="Sperr-Stand der Profile ließ sich nicht lesen"):
            refuse_disabled(["fake-auth"], known)
    finally:
        if damage == "mode":
            path.chmod(0o600)


def test_a_missing_file_leaves_the_known_block(arr_home: Path) -> None:
    arr_home.mkdir(parents=True, exist_ok=True)
    blocked = AuthProfile(name="fake-auth", kind="claude_oauth", disabled=True)
    with pytest.raises(ArrError, match="gesperrt"):
        refuse_disabled(["fake-auth"], ProfilesFile(profiles=[blocked]))


def test_w4_00_b14_the_text_names_a_way_that_exists_in_the_gui(arr_home: Path) -> None:
    """The way named in the text is in the navigation and on the settings page, with a field
    to pick another profile (review W3-06, B14: „nimm ein anderes Profil“ had no place)."""
    from arr.service.gui.layout import NAV
    from arr.service.gui.pages.einstellungen import SECTIONS

    arr_home.mkdir(parents=True, exist_ok=True)
    blocked = AuthProfile(name="fake-auth", kind="claude_oauth", disabled=True)
    with pytest.raises(ArrError) as caught:
        refuse_disabled(["fake-auth"], ProfilesFile(profiles=[blocked]))
    text = str(caught.value)
    assert "nimm ein anderes Profil" not in text
    pages = {entry[3] for entry in NAV if entry}
    sections = {title for _anchor, title in SECTIONS if title}
    assert "„Auth-Profile“" in text and "Auth-Profile" in pages
    assert "„Einstellungen → Standards und Limits“" in text
    assert "Einstellungen" in pages and "Standards und Limits" in sections
    source = (Path(__file__).parents[1] / "src/arr/service/gui/pages/einstellungen.py").read_text()
    assert 'name=f"defaults.{provider.value}.auth"' in source, "the field to pick a profile"


async def test_no_test_and_no_login_on_a_blocked_profile(api: RealApi) -> None:
    await api.disable_profile("fake-auth")
    with pytest.raises(ApiException, match="ist gesperrt") as error:
        await api.test_profile("fake-auth")
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    with pytest.raises(ApiException, match="ist gesperrt"):
        await api.start_login("fake-auth")


async def test_entsperren_opens_the_profile_again(api: RealApi, arr_home: Path) -> None:
    await api.disable_profile("fake-auth")
    result = await api.enable_profile("fake-auth")
    assert result.ok and not result.profile.disabled and "entsperrt" in result.message
    assert not disabled_in_file(arr_home, "fake-auth")
    started = await api.start_run(RUN_INPUT)
    assert started.run_id


async def test_an_unknown_profile_is_not_found(api: RealApi) -> None:
    for call in (api.disable_profile, api.enable_profile, api.remove_login, api.get_profile_usage):
        with pytest.raises(ApiException) as error:
            await call("gibt-es-nicht")
        assert error.value.error.code is ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------------------------
# Running runs stop, with finalizer and scan
# --------------------------------------------------------------------------------------------


async def test_a_run_of_the_service_stops_with_finalizer_and_scan(api: RealApi, wf: Path) -> None:
    run_id = (await api.start_run(RUN_INPUT)).run_id
    await eventually(lambda: status_of(wf, run_id) is RunStatus.RUNNING, within=15)
    preview = await api.disable_profile("fake-auth", dry_run=True)
    assert preview.runs == [run_id]
    result = await api.disable_profile("fake-auth")
    assert result.ok and result.runs == [run_id] and result.failed_runs == []
    assert "1 laufender Run wird gestoppt" in result.message
    await eventually(lambda: status_of(wf, run_id) is RunStatus.STOPPED, within=15)
    run_dir = wf / "runs" / run_id
    await eventually(lambda: scanned(run_dir), within=15)
    assert (run_dir / "output" / "summary.md").is_file(), "the finalizer ran"
    assert (run_dir / "usage.json").is_file()


@pytest.fixture
def run_process(tmp_path: Path, arr_home: Path, wf: Path) -> Iterator[subprocess.Popen[str]]:
    run_yaml = wf / "run.yaml"
    run_yaml.write_text(SOLO, "utf-8")
    process = start_run_process(tmp_path, arr_home, run_yaml, LONG)
    yield process
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(10)


async def test_a_run_of_arr_run_in_another_process_stops(
    api: RealApi, wf: Path, run_process: subprocess.Popen[str]
) -> None:
    runs = wf / "runs"

    def running() -> bool:
        return any(
            load_run_meta(p).status is RunStatus.RUNNING
            for p in runs.iterdir()
            if (p / "meta.json").is_file()
        )

    await eventually(running, within=30)
    [run_dir] = [p for p in runs.iterdir() if p.is_dir()]
    result = await api.disable_profile("fake-auth")
    assert result.ok and result.runs == [run_dir.name]
    code = await asyncio.to_thread(run_process.wait, 30)
    assert code == 130
    assert load_run_meta(run_dir).status is RunStatus.STOPPED
    assert (run_dir / "output" / "summary.md").is_file(), "the finalizer ran"
    assert scanned(run_dir), "the scan ran"


def write_block(arr_home: Path, name: str) -> None:
    """What `disable_profile` writes, atomically, by a service of another workfolder."""
    path = arr_home / "profiles.yaml"
    text = path.read_text().replace(f"{{name: {name}, kind: claude_oauth}}", "X")
    temp = path.with_suffix(".tmp")
    temp.write_text(text.replace("X", f"{{name: {name}, kind: claude_oauth, disabled: true}}"))
    os.replace(temp, path)


def start_elsewhere(tmp_path: Path, arr_home: Path, folder: str, auth: str) -> Any:
    """`arr run` in a workfolder of its own, in a process of its own."""
    root = tmp_path / folder
    root.mkdir()
    workfolder = make_workfolder(root)
    run_yaml = workfolder / "run.yaml"
    run_yaml.write_text(SOLO.replace("auth: fake-auth", f"auth: {auth}"), "utf-8")
    process = start_run_process(root, arr_home, run_yaml, LONG)
    return process, workfolder / "runs"


def active_dir(runs: Path) -> Path | None:
    for folder in runs.iterdir():
        if (folder / "meta.json").is_file() and (load_run_meta(folder).status is RunStatus.RUNNING):
            return folder
    return None


async def test_a_block_reaches_a_run_in_another_workfolder_and_only_that_one(
    tmp_path: Path, arr_home: Path, wf: Path
) -> None:
    """Two real processes, two workfolders. No stop.request: the block alone must do it."""
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "other-auth": "claude_oauth"})
    hit, hit_runs = start_elsewhere(tmp_path, arr_home, "wf-hit", "fake-auth")
    free, free_runs = start_elsewhere(tmp_path, arr_home, "wf-free", "other-auth")
    try:
        await eventually(lambda: active_dir(hit_runs) is not None, within=40)
        await eventually(lambda: active_dir(free_runs) is not None, within=40)
        run_dir = active_dir(hit_runs)
        started = time.monotonic()
        write_block(arr_home, "fake-auth")
        await eventually(lambda: load_run_meta(run_dir).status.is_final, within=5)
        assert time.monotonic() - started <= 5
        assert await asyncio.to_thread(hit.wait, 30) == 130
        assert load_run_meta(run_dir).status is RunStatus.STOPPED
        assert (run_dir / "output" / "summary.md").is_file(), "the finalizer ran"
        assert scanned(run_dir), "the scan ran"
        assert free.poll() is None and active_dir(free_runs) is not None, "the other profile runs"
    finally:
        for process in (hit, free):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(10)


async def launch_with(
    wf: Path, scripts: dict[str, Any], lines: list[str], text: str = SOLO, **kwargs: Any
) -> Any:
    run_yaml = wf / "run.yaml"
    run_yaml.write_text(text, "utf-8")
    return asyncio.create_task(
        run_launch(
            plan_launch(run_yaml, cwd=wf),
            decider=None,
            report=lines.append,
            builders=fake_builders(scripts),
            signals=False,
            pacing=FAST,
            block_poll_s=0.02,
            **kwargs,
        )
    )


async def test_one_blocked_profile_of_several_is_enough(wf: Path, arr_home: Path) -> None:
    """A mixed run: only the second agent's profile is blocked."""
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-b": "claude_oauth"})
    mixed = SOLO.replace(
        "agents: [{id: a}]\ngroupchats: [{name: main, members: [a]}]",
        "agents: [{id: a}, {id: b, auth: fake-b}]\ngroupchats: [{name: main, members: [a, b]}]",
    ).replace("topology: {kind: solo}", "topology: {kind: solo, params: {agent: a}}")
    lines: list[str] = []
    running = await launch_with(wf, LONG | {"b": LONG["a"]}, lines, mixed)
    await eventually(lambda: any(line.startswith("▶ a: Turn 1") for line in lines), within=15)
    write_block(arr_home, "fake-b")
    result = await asyncio.wait_for(running, 15)
    assert result.status is RunStatus.STOPPED
    assert "Das Profil „fake-b“ wurde gesperrt: Der Run wird gestoppt …" in lines
    # W4-00 B11: the run names the profile, also when only the file told it
    summary = (result.run_dir / "output" / "summary.md").read_text("utf-8")
    assert "Gestoppt: Profil „fake-b“ wurde gesperrt (Not-Aus)." in summary


async def test_a_run_goes_on_while_the_file_cannot_be_read(
    wf: Path, arr_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed holds for the start only: a short outage must not kill hours of work."""
    caplog.set_level(logging.WARNING, logger="arr.runs.launch")
    path = arr_home / "profiles.yaml"
    good = path.read_text()
    lines: list[str] = []
    running = await launch_with(wf, LONG, lines)
    await eventually(lambda: any(line.startswith("▶ a: Turn 1") for line in lines), within=15)
    path.write_text("profiles: [{ kaputt")
    await eventually(lambda: "nicht lesbar" in caplog.text, within=5)
    await asyncio.sleep(0.2)
    assert not running.done()
    assert caplog.text.count("nicht lesbar") == 1, "one warning per outage"
    path.write_text(good.replace("claude_oauth}", "claude_oauth, disabled: true}", 1))
    result = await asyncio.wait_for(running, 15)
    assert result.status is RunStatus.STOPPED, "the block works again after the outage"


BLOCKED = "Gestoppt: Profil „fake-auth“ wurde gesperrt (Not-Aus)."


async def test_w4_00_b11_the_run_names_the_blocked_profile(api: RealApi, wf: Path) -> None:
    """Run detail (summary and log), `summary.md`, `arr runs status` and the log entry."""
    run_id = (await api.start_run(RUN_INPUT)).run_id
    await eventually(lambda: status_of(wf, run_id) is RunStatus.RUNNING, within=15)
    await api.disable_profile("fake-auth")
    await eventually(lambda: status_of(wf, run_id) is RunStatus.STOPPED, within=15)
    run_dir = wf / "runs" / run_id
    await eventually(lambda: scanned(run_dir), within=15)
    entries = OrchestratorLog(run_dir).entries()
    [stop] = [e for e in entries if e["kind"] == "stop"]
    assert stop["data"]["reason"] == "profile_blocked" and stop["data"]["profile"] == "fake-auth"
    summary = (run_dir / "output" / "summary.md").read_text("utf-8")
    assert f"- **Grund:** {BLOCKED}" in summary and "Der User" not in summary
    assert run_status(run_dir).reason == BLOCKED
    assert BLOCKED in (await api.get_results(run_id)).summary_md
    texts = [entry.text for entry in await api.get_log(run_id)]
    assert any(t.startswith("Profil „fake-auth“ wurde gesperrt (Not-Aus).") for t in texts)


async def test_w4_00_b11_a_stop_by_the_user_stays_the_users(api: RealApi, wf: Path) -> None:
    run_id = (await api.start_run(RUN_INPUT)).run_id
    await eventually(lambda: status_of(wf, run_id) is RunStatus.RUNNING, within=15)
    await api.run_action(run_id, RunAction.STOP)
    run_dir = wf / "runs" / run_id
    await eventually(lambda: scanned(run_dir), within=15)
    assert run_status(run_dir).reason == "Der User hat den Run gestoppt."
    [stop] = [e for e in OrchestratorLog(run_dir).entries() if e["kind"] == "stop"]
    assert stop["data"]["reason"] == "user" and "profile" not in stop["data"]


async def test_w4_00_b11_a_stop_before_the_agents_names_the_profile(
    wf: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked while the run is still being prepared: no agent ran, the reason says why."""
    from arr.runs import manager as manager_module

    lines: list[str] = []
    managers: list[Any] = []
    real_setup = manager_module.RunManager._setup

    async def slow_setup(self: Any) -> None:
        write_block(arr_home, "fake-auth")
        self.stop()  # what the watcher does a moment later
        await real_setup(self)

    monkeypatch.setattr(manager_module.RunManager, "_setup", slow_setup)
    running = await launch_with(wf, LONG, lines, on_manager=managers.append)
    result = await asyncio.wait_for(running, 15)
    assert result.status is RunStatus.STOPPED
    summary = (result.run_dir / "output" / "summary.md").read_text("utf-8")
    assert BLOCKED in summary


@pytest.mark.parametrize(
    ("ids", "preview", "stopping", "failing", "unsaved"),
    [
        (
            ["r1"],
            "1 laufender Run würde gestoppt.",
            "1 laufender Run wird gestoppt, danach",
            "1 Run ließ sich nicht stoppen: r1. Beende den Prozess im Terminal.",
            "Nicht stoppen ließ sich: r1. Beende ihn im Terminal.",
        ),
        (
            ["r1", "r2"],
            "2 laufende Runs würden gestoppt.",
            "2 laufende Runs werden gestoppt, danach",
            "2 Runs ließen sich nicht stoppen: r1, r2. Beende die Prozesse im Terminal.",
            "Nicht stoppen ließen sich: r1, r2. Beende sie im Terminal.",
        ),
    ],
)
async def test_w4_00_b12_one_run_and_more_runs(
    api: RealApi,
    monkeypatch: pytest.MonkeyPatch,
    ids: list[str],
    preview: str,
    stopping: str,
    failing: str,
    unsaved: str,
) -> None:
    profiles = api.profiles
    failed: list[str] = []

    async def stop_runs(_ids: list[str]) -> list[str]:
        return list(failed)

    monkeypatch.setattr(profiles, "_run_ids", lambda _name: list(ids))
    monkeypatch.setattr(profiles, "_stop_runs", stop_runs)
    assert (await api.disable_profile("fake-auth", dry_run=True)).message == preview
    assert stopping in (await api.disable_profile("fake-auth")).message
    failed[:] = ids
    assert failing in (await api.disable_profile("fake-auth")).message
    cause = ApiException.of(ErrorCode.UNAVAILABLE, "Die Datei ist gesperrt.")
    text = profiles._unsaved_block("fake-auth", cause, ids, ids, None).message
    assert unsaved in text and "Runs ließen" not in text.replace(failing, "")
    text = profiles._unsaved_block("fake-auth", cause, ids, [], None).message
    word = "1 laufender Run hier wird" if len(ids) == 1 else "2 laufende Runs hier werden"
    assert f"{word} trotzdem gestoppt." in text


def test_w4_00_b12_the_dialog_counts_right() -> None:
    from arr.service.gui.pages.profile import running_runs

    assert (running_runs(1), running_runs(2)) == ("1 laufender Run", "2 laufende Runs")


async def test_a_second_stop_through_the_block_changes_nothing(api: RealApi, wf: Path) -> None:
    """In the service's own workfolder the run is told twice: by the service and by the file."""
    run_id = (await api.start_run(RUN_INPUT)).run_id
    await eventually(lambda: status_of(wf, run_id) is RunStatus.RUNNING, within=15)
    await api.disable_profile("fake-auth")
    await eventually(lambda: status_of(wf, run_id) is RunStatus.STOPPED, within=15)
    await eventually(lambda: scanned(wf / "runs" / run_id), within=15)
    kinds = [e["kind"] for e in OrchestratorLog(wf / "runs" / run_id).entries()]
    assert kinds.count(SCAN_KIND) == 1


async def test_a_run_ends_without_a_block_and_leaves_no_watcher(wf: Path) -> None:
    lines: list[str] = []
    running = await launch_with(wf, {"a": turns([say("x")])}, lines)
    result = await asyncio.wait_for(running, 15)
    assert result.status is not RunStatus.STOPPED
    assert not any("wurde gesperrt" in line for line in lines)


async def test_a_run_that_cannot_be_stopped_is_named(
    api: RealApi, wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next run must still stop, and the page must not sound calm."""
    held = make_run_folder(wf, status=RunStatus.RUNNING, run_id="20261101-0900_demo_custom_aaaa")
    other = make_run_folder(wf, status=RunStatus.RUNNING, run_id="20261101-0901_demo_custom_bbbb")
    stopped: list[str] = []

    async def stop(run_id: str) -> None:
        if run_id == held.name:
            raise ApiException.of(ErrorCode.NOT_ALLOWED, "geht nicht")
        stopped.append(run_id)

    api.profiles.stop_run = stop
    result = await api.disable_profile("fake-auth")
    assert not result.ok and result.failed_runs == [held.name] and held.name in result.message
    assert stopped == [other.name] and result.profile.disabled


async def test_an_open_login_ends(api: RealApi, monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[str] = []

    async def cancel(name: str) -> Any:
        ended.append(name)

    monkeypatch.setattr(api.profiles.logins, "cancel", cancel)
    await api.disable_profile("fake-auth")
    assert ended == ["fake-auth"]


async def test_a_block_that_cannot_be_written_still_stops_the_runs_here(
    api: RealApi, wf: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review W3-06, B9: the message names both, the missing block and the stopped run."""

    def refuse(_file: Any) -> None:
        raise OSError("read-only")

    run_id = (await api.start_run(RUN_INPUT)).run_id
    await eventually(lambda: status_of(wf, run_id) is RunStatus.RUNNING, within=15)
    monkeypatch.setattr(api.profiles, "_save", refuse)
    result = await api.disable_profile("fake-auth")
    assert not result.ok and result.runs == [run_id]
    assert "nicht gesperrt" in result.message and "ließ sich nicht speichern" in result.message
    assert "1 laufender Run hier wird trotzdem gestoppt." in result.message
    assert "anderen Workfoldern" in result.message
    assert result.profile is not None and not result.profile.disabled
    assert not disabled_in_file(arr_home, "fake-auth")
    await eventually(lambda: status_of(wf, run_id) is RunStatus.STOPPED, within=15)
    await eventually(lambda: scanned(wf / "runs" / run_id), within=15)


async def test_an_unreadable_file_still_stops_the_runs_here(
    api: RealApi, wf: Path, arr_home: Path
) -> None:
    run_id = (await api.start_run(RUN_INPUT)).run_id
    await eventually(lambda: status_of(wf, run_id) is RunStatus.RUNNING, within=15)
    (arr_home / "profiles.yaml").write_bytes(b"\xff\xfe kaputt")
    preview = await api.disable_profile("fake-auth", dry_run=True)
    assert preview.runs == [run_id]
    result = await api.disable_profile("fake-auth")
    assert not result.ok and result.runs == [run_id] and result.profile is None
    assert "nicht gesperrt" in result.message and "trotzdem gestoppt" in result.message
    await eventually(lambda: status_of(wf, run_id) is RunStatus.STOPPED, within=15)


async def test_a_block_without_a_run_that_cannot_be_written_says_so(
    api: RealApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_file: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(api.profiles, "_save", refuse)
    result = await api.disable_profile("fake-auth")
    assert not result.ok and result.runs == [] and "Hier lief kein Run" in result.message


# --------------------------------------------------------------------------------------------
# „Login entfernen“ (A7)
# --------------------------------------------------------------------------------------------


async def test_login_entfernen_needs_the_block_first(api: RealApi, keychain: dict) -> None:
    keychain[("arr", "fake-key")] = "sk-ant-MARKER"
    with pytest.raises(ApiException, match="Sperre „fake-key“ zuerst"):
        await api.remove_login("fake-key")
    assert keychain


async def test_the_key_leaves_the_keychain(api: RealApi, keychain: dict) -> None:
    await api.create_profile(
        CreateProfileRequest(
            name="fake-key", kind="claude_api_key", api_key=SecretStr("sk-ant-MARKER"), replace=True
        )
    )
    await api.disable_profile("fake-key")
    preview = await api.remove_login("fake-key", dry_run=True)
    assert preview.removable and not preview.removed and keychain
    result = await api.remove_login("fake-key")
    assert result.removed and keychain == {}
    assert result.profile.disabled and not result.profile.secret_present


async def test_the_login_folder_leaves_and_the_profile_stays(
    api: RealApi, arr_home: Path, keychain: dict
) -> None:
    folder = arr_home / "auth" / "fake-chat" / "codex_home"
    folder.mkdir(parents=True)
    (folder / "auth.json").write_text("{}")
    await api.disable_profile("fake-chat")
    result = await api.remove_login("fake-chat")
    assert result.removed and not folder.exists()
    assert "fake-chat" in [p.name for p in await api.list_profiles()]


async def test_the_mac_login_is_not_arrs_and_stays(api: RealApi, keychain: dict) -> None:
    await api.disable_profile("fake-auth")
    deleted: list[str] = []
    result = await api.remove_login("fake-auth")
    assert not result.removed and not result.removable and deleted == []
    assert "gehört nicht arr" in result.message and "nichts" in result.message


async def test_a_foreign_keychain_item_stays(api: RealApi, arr_home: Path, keychain: dict) -> None:
    keychain[("anderes-programm", "x")] = "fremd"
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n  - name: fake-key\n    kind: claude_api_key\n"
        "    disabled: true\n    keychain: {service: anderes-programm, account: x}\n"
    )
    result = await api.remove_login("fake-key")
    assert not result.removed and keychain == {("anderes-programm", "x"): "fremd"}
    assert "außerhalb von arr" in result.message


async def test_a_foreign_login_folder_stays(
    api: RealApi, arr_home: Path, tmp_path: Path, keychain: dict
) -> None:
    foreign = tmp_path / "foreign-login"
    foreign.mkdir()
    (foreign / "auth.json").write_text("{}")
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n  - name: fake-chat\n    kind: codex_chatgpt\n"
        f"    disabled: true\n    login_dir: {foreign}\n"
    )
    result = await api.remove_login("fake-chat")
    assert not result.removed and (foreign / "auth.json").exists()


async def test_a_run_that_still_stops_keeps_the_login(
    api: RealApi, wf: Path, keychain: dict
) -> None:
    keychain[("arr", "fake-key")] = "sk-ant-MARKER"
    (wf.parent / "x").mkdir(exist_ok=True)
    make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"])
    file = api.profiles._load()
    api.profiles._save(
        file.model_copy(
            update={
                "profiles": [
                    p.model_copy(update={"disabled": True}) if p.name == "fake-auth" else p
                    for p in file.profiles
                ]
            }
        )
    )
    keychain[("arr", "fake-auth")] = "sk-ant-MARKER"
    with pytest.raises(ApiException, match="stoppt noch"):
        await api.remove_login("fake-auth")
    assert ("arr", "fake-auth") in keychain


# --------------------------------------------------------------------------------------------
# The record per profile (A5)
# --------------------------------------------------------------------------------------------


def total(*numbers: int, cost: float = 0.1) -> UsageTotal:
    entry = UsageTotal()
    entry.usage.input, entry.usage.output, entry.usage.cache_read = numbers
    entry.turns = 1
    entry.cost_estimated_usd = cost
    return entry


def write_usage(run_dir: Path, agents: dict[str, tuple[str, UsageTotal]]) -> RunUsage:
    usage = RunUsage(run=run_dir.name)
    for agent, (profile, part) in agents.items():
        usage.by_agent[agent] = AgentUsage(profile=profile, **part.model_dump())
        usage.by_profile[profile] = part
    (run_dir / "usage.json").write_text(usage.model_dump_json(), "utf-8")
    return usage


def set_times(run_dir: Path, started: float) -> None:
    path = run_dir / "meta.json"
    meta = json.loads(path.read_text())
    path.write_text(json.dumps({**meta, "created": started, "started": started}))


def turn_end(run_dir: Path, agent: str, *tokens: int) -> None:
    data = {
        "usage": dict(zip(("input", "output"), tokens, strict=True)),
        "cost_usd": 0.5,
        "cost_kind": "estimated",
    }
    path = run_dir / "agents" / agent / "transcript.jsonl"
    with path.open("a", encoding="utf-8") as file:
        file.write(transcript_line(1, agent, run_dir.name, "turn_end", data) + "\n")


async def test_the_record_matches_usage_json(api: RealApi, wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.DONE)
    usage = write_usage(run_dir, {"a": ("fake-auth", total(100, 20, 500))})
    record = await api.get_profile_usage("fake-auth")
    [row] = record.runs
    expected = usage.by_profile["fake-auth"]
    assert (row.input, row.output, row.cache_read) == (100, 20, 500)
    assert row.input == expected.usage.input and row.turns == expected.turns
    assert row.cost_estimated_usd == expected.cost_estimated_usd
    assert row.agents == ["a"] and row.url == f"/runs/{run_dir.name}"
    assert not row.aborted and not row.active
    assert record.total.input == 100 and record.total.runs == 1
    assert record.note == NOTE and "nicht" not in NOTE.split("Die")[0]
    assert "direkte Nutzung von Claude Code oder Codex" in record.note


async def test_a_mixed_run_counts_only_the_agents_on_the_profile(api: RealApi, wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.DONE, agents=["a", "b"])
    write_usage(
        run_dir,
        {"a": ("fake-auth", total(100, 20, 0)), "b": ("fake-key", total(7000, 800, 0))},
    )
    [row] = (await api.get_profile_usage("fake-auth")).runs
    assert row.agents == ["a"] and row.input == 100
    assert (await api.get_profile_usage("fake-key")).total.input == 7000


async def test_an_aborted_run_without_a_finalizer_counts_and_is_marked(
    api: RealApi, wf: Path
) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.INTERRUPTED)
    turn_end(run_dir, "a", 40, 2)
    [row] = (await api.get_profile_usage("fake-auth")).runs
    assert row.aborted and row.status is RunStatus.INTERRUPTED
    assert row.input == 40 and row.cost_estimated_usd == 0.5


async def test_a_running_run_counts_and_updates_when_read_again(api: RealApi, wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.RUNNING)
    turn_end(run_dir, "a", 40, 2)
    assert (await api.get_profile_usage("fake-auth")).runs[0].active
    turn_end(run_dir, "a", 10, 1)
    assert (await api.get_profile_usage("fake-auth")).total.input == 50


@pytest.mark.parametrize(
    "status", [RunStatus.RUNNING, RunStatus.INTERRUPTED, RunStatus.FAILED, RunStatus.STOPPED]
)
async def test_a_run_without_a_finished_turn_shows_with_zero(
    api: RealApi, wf: Path, status: RunStatus
) -> None:
    """Review W3-06, B4: the profile comes from `run.yaml`, not from a turn."""
    run_dir = make_run_folder(wf, status=status)
    record = await api.get_profile_usage("fake-auth")
    [row] = record.runs
    assert row.run_id == run_dir.name and row.agents == ["a"]
    assert (row.input, row.output, row.turns) == (0, 0, 0)
    assert row.active is status.is_active and row.aborted is (status is not RunStatus.RUNNING)
    assert record.total.runs == 1
    assert (await api.get_profile_usage("fake-key")).runs == []


async def test_a_finalized_run_without_a_turn_shows_with_zero(api: RealApi, wf: Path) -> None:
    """`usage.json` of such a run has the agent, but no profile for it."""
    run_dir = make_run_folder(wf, status=RunStatus.FAILED)
    usage = RunUsage(run=run_dir.name, by_agent={"a": AgentUsage()})
    (run_dir / "usage.json").write_text(usage.model_dump_json(), "utf-8")
    [row] = (await api.get_profile_usage("fake-auth")).runs
    assert row.agents == ["a"] and row.input == 0 and row.aborted


def test_a_row_without_tokens_shows_zero_and_a_dash() -> None:
    from arr.service.api_models import ProfileUsageSum
    from arr.service.gui.pages.profile import usage_cells

    cells = usage_cells(ProfileUsageSum(label="x", runs=1))
    assert cells.count('<td class="num">0</td>') == 5 and cells.endswith(">–</td>")


async def test_periods_take_a_run_by_its_start(api: RealApi, wf: Path) -> None:
    now = datetime(2026, 11, 2, 12, 0)
    today = make_run_folder(wf, status=RunStatus.DONE, run_id="20261102-1000_demo_custom_aaaa")
    days = make_run_folder(wf, status=RunStatus.DONE, run_id="20261030-1000_demo_custom_bbbb")
    old = make_run_folder(wf, status=RunStatus.DONE, run_id="20260101-1000_demo_custom_cccc")
    for run_dir, moment, number in (
        (today, datetime(2026, 11, 2, 9, 0), 1),
        (days, datetime(2026, 10, 30, 9, 0), 10),
        (old, datetime(2026, 1, 1, 9, 0), 100),
    ):
        set_times(run_dir, moment.timestamp())
        write_usage(run_dir, {"a": ("fake-auth", total(number, 0, 0))})
    record = api.profile_usage.get("fake-auth", now=now)
    assert (record.today.input, record.week.input, record.total.input) == (1, 11, 111)
    assert (record.today.runs, record.week.runs, record.total.runs) == (1, 2, 3)


async def test_a_run_of_arr_run_counts(api: RealApi, wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.DONE)
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["starter"] == "cli"
    write_usage(run_dir, {"a": ("fake-auth", total(5, 1, 0))})
    assert (await api.get_profile_usage("fake-auth")).total.input == 5


async def test_a_profile_without_a_run_has_an_empty_record(api: RealApi) -> None:
    record = await api.get_profile_usage("fake-key")
    assert record.runs == [] and record.total.runs == 0


RATIO_MAX = 20
"""200 runs may take at most this many times as long as 20: linear is ≈ 10, quadratic ≈ 100."""
CEILING_S = 3.0
"""Backstop only: the CI runner (Ubuntu 26) took 1.1–1.4 s for 200 runs where a Mac takes 0.5 s;
the criterion that does not depend on the machine is `RATIO_MAX` (W3-10.2)."""


def _usage_runs(workfolder: Path, count: int) -> None:
    import shutil

    template = make_run_folder(workfolder, status=RunStatus.DONE)
    write_usage(template, {"a": ("fake-auth", total(1, 1, 1))})
    meta = json.loads((template / "meta.json").read_text())
    for number in range(count - 1):
        run_id = f"20261001-{number // 60:02d}{number % 60:02d}_demo_custom_{number:04x}"
        target = workfolder / "runs" / run_id
        shutil.copytree(template, target)
        (target / "meta.json").write_text(json.dumps({**meta, "run_id": run_id}))


async def test_200_runs_stay_under_a_second(api: RealApi, wf: Path, tmp_path: Path) -> None:
    """The usage of a profile over 200 runs grows linearly. Measured against 20 runs on the same
    machine, cold (a fresh `RealApi` each time), alternately and the best of three, so neither a
    slow runner nor load fails it; a quadratic step still does (W3-10.2)."""
    small = make_workfolder(tmp_path / "klein")
    _usage_runs(small, 20)
    _usage_runs(wf, 200)
    times: dict[int, list[float]] = {20: [], 200: []}
    for _ in range(3):
        for workfolder, count in ((small, 20), (wf, 200)):
            fresh = RealApi(workfolder, base_url=BASE, scan=False)
            started = time.perf_counter()
            record = await fresh.get_profile_usage("fake-auth")
            times[count].append(time.perf_counter() - started)
            assert record.total.runs == count and record.total.input == count
    t20, t200 = min(times[20]), min(times[200])
    assert t200 <= RATIO_MAX * t20, f"200 Runs {t200:.3f} s, 20 Runs {t20:.3f} s: nicht linear"
    assert t200 <= CEILING_S, f"200 Runs {t200:.3f} s"

"""Area `setup` of `RealApi` (W3-04): workfolder, projects, settings, fixed rules, toolchain.

The workfolders lie below `tmp_path`, runs use the FakeBackend. The switch in a real service
process is at the end (`service_child.py`, `ARR_TEST_SWITCH`).
"""

import asyncio
import errno
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import FAST, done, fake_builders, say, turns, wait, write_profiles
from service_kit import (
    hold_lock,
    make_run_folder,
    make_workfolder,
    read_meta,
    run_dirs,
    start_run_process,
    start_service,
    wait_for,
)
from test_policy import env, req  # noqa: F401

from arr.errors import ArrError, ConfigError
from arr.models import Decider, Effort, Provider, RunStatus, RunVenvMode
from arr.permissions.policy import PermissionRules, PolicyEngine
from arr.permissions.types import DecisionAction
from arr.runs.manager import set_run_status
from arr.runs.orchestrator import ProfileGate
from arr.service import real_api as real_api_package
from arr.service.api_client import Subscription
from arr.service.api_models import (
    SYSTEM_PYTHON,
    ApiException,
    Appearance,
    AppSettings,
    CreateProjectRequest,
    ErrorCode,
    LimitsInfo,
    LiveEvent,
    NotificationEvent,
    OpenIn,
    ProviderDefaultsInfo,
    RunStatusEvent,
    Severity,
    WorkfolderRequest,
)
from arr.service.real_api import RealApi, live
from arr.service.real_api.runs import RunsArea
from arr.service.real_api.setup import base_venvs, duration_text, toolchain_notice
from arr.service.state import (
    ServiceInfo,
    read_app_settings,
    read_service_info,
    recent_workfolders,
    remember_workfolder,
    write_app_settings,
    write_service_info,
)
from arr.skills import discover_skills
from arr.venvs import base_venv_dir, venv_root
from arr.workfolder import (
    init_workfolder,
    load_workfolder_settings,
    os_reason,
    read_template,
    set_setting,
    update_settings_file,
)

BASE = "http://127.0.0.1:8765"
RUN_INPUT = {
    "project": "demo",
    "task": "Teste arr.",
    "topology": {"kind": "solo"},
    "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
    "agents": [{"id": "a"}],
    "groupchats": [{"name": "main", "members": ["a"]}],
}


@pytest.fixture(autouse=True)
def no_open_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clients left by GUI tests hang on closed event loops; here no page is open."""
    from nicegui import Client

    monkeypatch.setattr(Client, "instances", {})


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "zweites": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


def make_api(wf: Path, scripts: dict[str, Any] | None = None) -> RealApi:
    return RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders(scripts or {"a": turns([say("los"), done()])}),
        manager_kwargs={"pacing": FAST},
    )


def snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def fake_base_venv(name: str) -> Path:
    """A base venv in the test's root whose `python3` is the interpreter of this test run."""
    venv = base_venv_dir(name)
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").symlink_to(sys.executable)
    (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.base_prefix) / 'bin'}\nversion = 3.12.0\n")
    return venv


async def until_status(api: RealApi, run_id: str, status: RunStatus) -> None:
    end = time.monotonic() + 20
    while time.monotonic() < end:
        if (await api.get_run(run_id)).run.status is status:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{run_id} not {status}")


async def next_status(subscription: Subscription, run_id: str, within: float = 5.0) -> LiveEvent:
    end = time.monotonic() + within
    while time.monotonic() < end:
        for event in subscription.drain():
            if isinstance(event, RunStatusEvent) and event.run.id == run_id:
                return event
        await asyncio.sleep(0.02)
    raise AssertionError(f"no run.status for {run_id}")


# --- A1: a new workfolder ---------------------------------------------------------------------


async def test_new_workfolder_is_created_and_the_service_works_on_it(
    wf: Path, tmp_path: Path
) -> None:
    api = make_api(wf)
    target = tmp_path / "neu" / "Test-UX3"
    info = await api.select_workfolder(WorkfolderRequest(path=str(target), create=True))
    assert info.name == "Test-UX3" and info.path == str(target)
    for name in ("projects", "skills", "runs"):
        assert (target / name).is_dir()
    assert (target / "arr.yaml").is_file()
    assert (await api.get_workfolder()).path == str(target)
    assert await api.list_projects() == []
    result = await api.create_project(CreateProjectRequest(name="ux3-collatz"))
    assert result.project.path == str(target / "projects" / "ux3-collatz")


@pytest.mark.parametrize("extra", [None, ".DS_Store"])
async def test_empty_folder_becomes_a_workfolder(
    wf: Path, tmp_path: Path, extra: str | None
) -> None:
    target = tmp_path / "leer"
    target.mkdir()
    if extra:
        (target / extra).write_text("x")
    await make_api(wf).select_workfolder(WorkfolderRequest(path=str(target), create=True))
    assert (target / "arr.yaml").is_file()


async def test_foreign_folder_stays_untouched(wf: Path, tmp_path: Path) -> None:
    target = tmp_path / "fremd"
    (target / "projects").mkdir(parents=True)
    (target / "notizen.md").write_text("Meins")
    before = snapshot(target)
    api = make_api(wf)
    store = api.state.store
    with pytest.raises(ApiException) as error:
        await api.select_workfolder(WorkfolderRequest(path=str(target), create=True))
    assert error.value.error.field == "path" and "andere Dateien" in error.value.error.message
    assert snapshot(target) == before and sorted(p.name for p in target.iterdir()) == [
        "notizen.md",
        "projects",
    ]
    assert api.state.store is store and api.state.workfolder == wf


async def test_an_existing_workfolder_is_opened_and_not_written_to(
    wf: Path, tmp_path: Path
) -> None:
    other = init_workfolder(tmp_path / "anderer")
    (other / "arr.yaml").write_text("version: 1\n# mein Kommentar\n")
    before = snapshot(other)
    info = await make_api(wf).select_workfolder(WorkfolderRequest(path=str(other), create=True))
    assert info.path == str(other) and snapshot(other) == before


@pytest.mark.parametrize(
    ("make", "text"),
    [
        (lambda t: "relativ/ordner", "kein vollständiger Pfad"),
        (lambda t: str(t / "datei.txt"), "eine Datei, kein Ordner"),
        (lambda t: str(t / "datei.txt" / "drunter"), "eine Datei, kein Ordner"),
        (lambda t: str(t / "venv-root" / "wf"), "Python-Umgebungen"),
        (lambda t: str(t / "venv-root"), "Python-Umgebungen"),
        (lambda t: str(t / "arr-home" / "wf"), "eigenen Daten von arr"),
        (lambda t: str(t / "gesperrt" / "wf"), "nicht schreiben"),
    ],
)
async def test_bad_paths_give_a_message_and_create_nothing(
    wf: Path, tmp_path: Path, make: Any, text: str
) -> None:
    (tmp_path / "datei.txt").write_text("x")
    locked = tmp_path / "gesperrt"
    locked.mkdir(mode=0o500)
    api = make_api(wf)
    path = make(tmp_path)
    try:
        with pytest.raises(ApiException) as error:
            await api.select_workfolder(WorkfolderRequest(path=path, create=True))
    finally:
        locked.chmod(0o700)
    message = error.value.error.message
    assert text in message and error.value.error.field == "path"
    assert "arr init" not in message  # never a command to type (docs/08 §8.4)
    assert not (tmp_path / "venv-root").exists() and list(locked.iterdir()) == []


async def test_without_create_only_workfolders_open(wf: Path, tmp_path: Path) -> None:
    api = make_api(wf)
    with pytest.raises(ApiException) as error:
        await api.select_workfolder(WorkfolderRequest(path=str(tmp_path / "weg")))
    assert error.value.error.code is ErrorCode.NOT_FOUND and "verschoben" in str(error.value)
    (tmp_path / "ordner").mkdir()
    with pytest.raises(ApiException) as error:
        await api.select_workfolder(WorkfolderRequest(path=str(tmp_path / "ordner")))
    assert "arr.yaml fehlt" in str(error.value) and not (tmp_path / "weg").exists()


async def test_a_workfolder_with_a_broken_arr_yaml_is_not_opened(wf: Path, tmp_path: Path) -> None:
    other = init_workfolder(tmp_path / "kaputt")
    (other / "arr.yaml").write_text("version: [\n")
    api = make_api(wf)
    with pytest.raises(ApiException) as error:
        await api.select_workfolder(WorkfolderRequest(path=str(other)))
    assert error.value.error.code is ErrorCode.CONFIG_INVALID
    assert api.state.workfolder == wf


# --- A2: the switch ---------------------------------------------------------------------------


async def test_switch_builds_everything_new_but_the_hub(wf: Path, tmp_path: Path) -> None:
    api = make_api(wf)
    await api.start()
    state = api.state
    old = (state.store, state.settings, state.registry, api.live.watcher, api.live._task)
    hub, gates = state.hub, state.gates
    subscription = api.subscribe()
    try:
        old_run = make_run_folder(wf, status=RunStatus.RUNNING)
        target = tmp_path / "zwei"
        await api.select_workfolder(WorkfolderRequest(path=str(target), create=True))
        (target / "projects" / "demo").mkdir()
        assert state.hub is hub and api.hub is hub and state.gates is gates
        assert state.store is not old[0] and state.store.runs_dir == target / "runs"
        assert state.settings is not old[1] and state.registry is not old[2]
        assert api.live.watcher is not old[3] and api.live._task is not old[4]
        await asyncio.sleep(0)
        assert old[4].cancelled()
        assert any(e.type == "counts.changed" for e in subscription.drain())

        # live events of the new workfolder arrive, the old one is out of sight
        run_dir = make_run_folder(target, status=RunStatus.RUNNING)
        event = await next_status(subscription, run_dir.name)
        assert event.run.id == run_dir.name
        set_run_status(old_run, RunStatus.DONE)
        set_run_status(run_dir, RunStatus.DONE)
        await next_status(subscription, run_dir.name)
        assert await api.get_workfolder() and (await api.list_runs()).items[0].id == run_dir.name
        assert len((await api.list_runs()).items) == 1
    finally:
        subscription.close()
        await api.close()


async def test_switch_moves_the_app_files_and_only_our_own_service_json(
    wf: Path, tmp_path: Path, arr_home: Path
) -> None:
    def info(pid: int) -> ServiceInfo:
        return ServiceInfo(host="x.localhost", port=8765, token="t", pid=pid, workfolder=str(wf))

    api = make_api(wf)
    first, second = tmp_path / "eins", tmp_path / "zwei"
    write_service_info(info(os.getpid()))
    await api.select_workfolder(WorkfolderRequest(path=str(first), create=True))
    saved = read_service_info()
    assert saved is not None and saved.workfolder == str(first) and saved.token == "t"
    assert stat.S_IMODE((arr_home / "service.json").stat().st_mode) == 0o600
    assert recent_workfolders()[0] == first

    write_service_info(info(os.getpid() + 1))  # another process: not ours to change
    await api.select_workfolder(WorkfolderRequest(path=str(second), create=True))
    saved = read_service_info()
    assert saved is not None and saved.workfolder == str(wf)
    assert recent_workfolders()[:2] == [second, first]
    known = (await api.get_workfolder()).known
    assert [k.path for k in known][:2] == [str(second), str(first)]


async def test_switch_is_refused_while_an_own_run_is_active(wf: Path, tmp_path: Path) -> None:
    api = make_api(wf, {"a": turns([say("los"), wait(30), done()])})
    await api.start()
    target = tmp_path / "zwei"
    try:
        started = await api.start_run(RUN_INPUT)
        await until_status(api, started.run_id, RunStatus.RUNNING)
        store = api.state.store
        with pytest.raises(ApiException) as error:
            await api.select_workfolder(WorkfolderRequest(path=str(target), create=True))
        assert error.value.error.code is ErrorCode.NOT_ALLOWED
        assert "läuft noch ein Run" in error.value.error.message
        assert not target.exists() and api.state.store is store
        # the current workfolder is no switch
        same = await api.select_workfolder(WorkfolderRequest(path=str(wf)))
        assert same.path == str(wf)
        await api.stop_runs()
        assert (await api.get_run(started.run_id)).run.status is RunStatus.STOPPED
        await api.select_workfolder(WorkfolderRequest(path=str(target), create=True))
        assert api.state.workfolder == target
    finally:
        await api.close()


async def test_a_run_that_starts_during_the_recovery_stops_the_switch(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(wf, {"a": turns([say("los"), wait(30), done()])})
    await api.start()
    target = init_workfolder(tmp_path / "zwei")
    started: list[str] = []
    recover = api.setup._recover

    def slow_recover(root: Path) -> list[str]:
        asyncio.run_coroutine_threadsafe(start(), loop).result(timeout=20)
        return recover(root)

    async def start() -> None:
        started.append((await api.start_run(RUN_INPUT)).run_id)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(api.setup, "_recover", slow_recover)
    try:
        with pytest.raises(ApiException) as error:
            await api.select_workfolder(WorkfolderRequest(path=str(target)))
        assert error.value.error.code is ErrorCode.NOT_ALLOWED and api.state.workfolder == wf
    finally:
        await api.stop_runs()
        await api.close()


async def test_new_workfolder_is_recovered_like_at_a_start(wf: Path, tmp_path: Path) -> None:
    target = make_workfolder(tmp_path / "zwei")
    left = make_run_folder(target, status=RunStatus.RUNNING)
    held = make_run_folder(target, status=RunStatus.RUNNING)
    lock = hold_lock(held)
    api = make_api(wf)
    try:
        await api.select_workfolder(WorkfolderRequest(path=str(target)))
    finally:
        lock.release()
    assert read_meta(left)["status"] == "interrupted"
    assert read_meta(held)["status"] == "running"
    assert api.state.interrupted_at_start == [left.name]
    assert [w.kind.value for w in await api.list_warnings()] == ["runs_interrupted"]


async def test_gates_keep_their_rate_limit_state_and_take_the_new_limits(
    wf: Path, tmp_path: Path
) -> None:
    target = init_workfolder(tmp_path / "zwei")
    (target / "arr.yaml").write_text("version: 1\nconcurrency: {fake-auth: 7}\n")
    api = make_api(wf)
    gate = api.state.gates["fake-auth"] = ProfileGate("fake-auth", 2)
    other = api.state.gates["zweites"] = ProfileGate("zweites", 2)
    gate.paused_until = 123.0
    await api.select_workfolder(WorkfolderRequest(path=str(target)))
    assert api.state.gates["fake-auth"] is gate and gate.limit == 7 and gate.paused_until == 123.0
    assert other.limit is None


def test_open_pages_go_to_the_start_page(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, connected: bool) -> None:
            self.has_socket_connection = connected
            self.sent: list[str] = []

        def run_javascript(self, code: str) -> None:
            self.sent.append(code)

    from nicegui import Client

    tab, gone = FakeClient(True), FakeClient(False)
    monkeypatch.setattr(Client, "instances", {"a": tab, "b": gone})
    live.show_new_workfolder()
    assert tab.sent == ["location.assign('/')"] and gone.sent == []


def test_the_area_modules_keep_nothing_of_the_workfolder() -> None:
    """A new `store`/`registry` must reach every area: they read them from the state."""
    folder = Path(real_api_package.__file__).parent
    for file in folder.glob("*.py"):
        text = file.read_text("utf-8")
        for name in ("self.store =", "self.registry =", "self.workfolder ="):
            assert name not in text or file.name == "base.py", (file.name, name)


def write_run_yaml(workfolder: Path) -> Path:
    file = workfolder / "lauf.yaml"
    file.write_text(json.dumps({**RUN_INPUT, "name": "Terminal"}), "utf-8")
    return file


async def test_run_from_the_terminal_in_the_old_workfolder_goes_on(
    wf: Path, tmp_path: Path, arr_home: Path
) -> None:
    process = start_run_process(
        tmp_path, arr_home, write_run_yaml(wf), {"a": turns([say("los"), wait(1.0), done()])}
    )
    try:
        await asyncio.to_thread(wait_for, lambda: len(run_dirs(wf)) == 1, what="run folder")
        await asyncio.to_thread(
            wait_for, lambda: read_meta(run_dirs(wf)[0])["status"] == "running", what="running"
        )
        await make_api(wf).select_workfolder(
            WorkfolderRequest(path=str(tmp_path / "zwei"), create=True)
        )
        assert await asyncio.to_thread(process.wait, 30) == 0
        assert read_meta(run_dirs(wf)[0])["status"] == "done"
    finally:
        if process.poll() is None:
            process.kill()


def test_switch_in_the_real_service(wf: Path, tmp_path: Path, arr_home: Path) -> None:
    requests = tmp_path / "requests"
    requests.mkdir()

    def ask(name: str, **request: Any) -> dict[str, Any]:
        (requests / f"{name}.tmp").write_text(json.dumps(request), "utf-8")
        (requests / f"{name}.tmp").rename(requests / f"{name}.request")
        answer = requests / f"{name}.answer"
        wait_for(answer.exists, timeout=30, what=f"answer {name}")
        return json.loads(answer.read_text("utf-8"))

    target = tmp_path / "zwei"
    service = start_service(
        tmp_path,
        arr_home,
        wf,
        scripts={"a": turns([say("los"), wait(2.0), done()])},
        autostart=RUN_INPUT,
        env={"ARR_TEST_SWITCH": str(requests)},
    )
    try:
        wait_for(lambda: run_dirs(wf), what="run of the service")
        refused = ask("1", path=str(target), create=True)
        assert refused["code"] == "not_allowed" and not target.exists()
        run_dir = run_dirs(wf)[0]
        wait_for(lambda: read_meta(run_dir)["status"] == "done", timeout=30, what="run end")
        wait_for(
            lambda: ask(f"2-{time.monotonic_ns()}", path=str(target), create=True).get("path"),
            timeout=30,
            what="switch after the run",
        )
        saved = read_service_info()
        assert saved is not None and saved.workfolder == str(target)
        assert saved.pid == service.process.pid
        assert recent_workfolders()[0] == target
        assert (target / "arr.yaml").is_file()
        service.signal(signal.SIGINT)
        assert service.wait() == 0
    finally:
        service.stop()
    assert read_service_info() is None


# --- A3: projects -----------------------------------------------------------------------------


def add_user_skill(workfolder: Path) -> None:
    skill = workfolder / "skills" / "beweis"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: beweis\ndescription: Prüft Beweise.\naudience: both\n---\n# Beweis\n"
    )
    assert "beweis" in discover_skills(workfolder / "skills").skills


async def test_project_is_created_with_all_checks_green(wf: Path) -> None:
    add_user_skill(wf)
    api = make_api(wf)
    result = await api.create_project(
        CreateProjectRequest(name="ux3-collatz", title="Collatz für die Abnahme")
    )
    project = result.project
    assert project.setup_ok and all(c.ok for c in project.checks), project.checks
    assert {c.key for c in project.checks} == {"claude_md", "mcp_json", "skill_links", "agents_md"}
    assert project.title == "Collatz für die Abnahme"
    assert result.message == "Projekt „ux3-collatz“ angelegt."
    assert {"CLAUDE.md", ".mcp.json", "AGENTS.md", "project.yaml"} <= set(result.created)
    folder = wf / "projects" / "ux3-collatz"
    assert (folder / "CLAUDE.md").read_text().startswith("# Projekt: Collatz für die Abnahme")
    assert (folder / ".claude" / "skills" / "beweis").is_symlink()
    assert {f.name for f in project.folders} >= {"notes", "tex", "code", "results"}
    links = next(c for c in project.checks if c.key == "skill_links")
    assert links.message == "Skill-Links: 1 von 1"


async def test_repair_restores_the_mcp_file_and_leaves_the_users_files(wf: Path) -> None:
    add_user_skill(wf)
    api = make_api(wf)
    await api.create_project(CreateProjectRequest(name="p"))
    folder = wf / "projects" / "p"
    claude = (folder / "CLAUDE.md").read_text() + "\nMeine Notiz.\n"
    (folder / "CLAUDE.md").write_text(claude)
    (folder / "notes" / "idee.md").write_text("Meins")
    (folder / "project.yaml").write_text("skills: all\n# meine Zeile\n")
    (folder / ".mcp.json").unlink()
    (folder / ".claude" / "skills" / "beweis").unlink()
    before = snapshot(folder)

    broken = await api.get_project("p")
    assert not broken.setup_ok
    failing = {c.key: c for c in broken.checks if not c.ok}
    assert set(failing) == {"mcp_json", "skill_links"}
    assert failing["mcp_json"].message == ".mcp.json fehlt"
    assert failing["skill_links"].message == "Skill-Links: 0 von 1"
    assert all(c.action is not None and c.action.label == "Reparieren" for c in failing.values())

    result = await api.setup_project("p")
    assert result.project.setup_ok and all(c.ok for c in result.project.checks)
    assert ".mcp.json" in result.created and result.message == "„p“ repariert."
    after = snapshot(folder)
    assert {k: after[k] for k in before} == before  # nothing of the user changed
    assert (await api.setup_project("p")).created == []


async def test_mcp_file_of_the_user_keeps_its_servers(wf: Path) -> None:
    api = make_api(wf)
    await api.create_project(CreateProjectRequest(name="p"))
    file = wf / "projects" / "p" / ".mcp.json"
    file.write_text('{"mcpServers": {"meins": {"command": "x"}}}')
    project = await api.get_project("p")
    check = next(c for c in project.checks if c.key == "mcp_json")
    assert not check.ok and "ohne Server" in check.message
    await api.setup_project("p")
    assert set(json.loads(file.read_text())["mcpServers"]) == {"meins", "arr"}
    file.write_text("{ kaputt")
    assert not (await api.get_project("p")).setup_ok
    with pytest.raises(ApiException) as error:
        await api.setup_project("p")
    assert "kein gültiges JSON" in str(error.value) and file.read_text() == "{ kaputt"


async def test_existing_folder_is_set_up_without_touching_the_users_text(
    wf: Path, tmp_path: Path
) -> None:
    folder = wf / "projects" / "meins"
    folder.mkdir()
    (folder / "CLAUDE.md").write_text("# Meine Notizen\n")
    (folder / "daten.csv").write_text("1,2\n")
    api = make_api(wf)
    result = await api.create_project(
        CreateProjectRequest(name="meins", existing_path=f"{folder}/")
    )
    assert result.project.setup_ok and "CLAUDE.md" in result.created
    text = (folder / "CLAUDE.md").read_text()
    assert text.startswith("# Meine Notizen\n") and "<!-- arr:user-agent -->" in text
    assert (folder / "daten.csv").read_text() == "1,2\n"
    assert result.project.title == "meins"

    outside = tmp_path / "draussen"
    outside.mkdir()
    with pytest.raises(ApiException) as error:
        await api.create_project(CreateProjectRequest(name="draussen", existing_path=str(outside)))
    assert "nicht direkt unter" in str(error.value) and list(outside.iterdir()) == []
    with pytest.raises(ApiException) as error:
        await api.create_project(
            CreateProjectRequest(name="nix", existing_path=str(wf / "projects" / "nix"))
        )
    assert error.value.error.code is ErrorCode.NOT_FOUND


async def test_project_errors(wf: Path) -> None:
    api = make_api(wf)
    with pytest.raises(ApiException) as error:
        await api.create_project(CreateProjectRequest(name="demo"))
    assert error.value.error.code is ErrorCode.CONFLICT and error.value.error.field == "name"
    assert sorted(p.name for p in (wf / "projects" / "demo").iterdir()) == ["check.py", "notes.md"]
    with pytest.raises(ApiException) as error:
        await api.create_project(CreateProjectRequest(name="beispiel", template="collatz-beispiel"))
    assert error.value.error.code is ErrorCode.UNAVAILABLE and "W5-02" in str(error.value)
    with pytest.raises(ApiException) as error:
        await api.setup_project("gibt-es-nicht")
    assert error.value.error.code is ErrorCode.NOT_FOUND


# --- A4: settings -----------------------------------------------------------------------------


@pytest.fixture
def fresh(tmp_path: Path, arr_home: Path) -> Path:
    """A workfolder from the real template."""
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "zweites": "claude_oauth"})
    return init_workfolder(tmp_path / "frisch")


async def test_settings_land_in_arr_yaml_with_comments_and_nothing_else_changes(
    fresh: Path,
) -> None:
    file = fresh / "arr.yaml"
    template = file.read_text()
    api = make_api(fresh)
    info = await api.get_workfolder_settings()
    assert info.limits == LimitsInfo(wall_time_s=7200, turns_per_agent=40, budget_usd=20)
    await api.update_workfolder_settings(info)
    assert file.read_text() == template, "saving without a change writes nothing"

    limits = info.limits.model_copy(update={"wall_time_s": 5400, "budget_usd": 2.5})
    retention = info.retention.model_copy(update={"slim_after_days": 7})
    defaults = {
        Provider.CLAUDE: ProviderDefaultsInfo(
            auth="fake-auth", model="claude-haiku-5-5", effort=Effort.LOW
        )
    }
    saved = await api.update_workfolder_settings(
        info.model_copy(
            update={
                "limits": limits,
                "retention": retention,
                "defaults": defaults,
                "concurrency": {"fake-auth": 3},
            }
        )
    )
    assert saved.limits.wall_time_s == 5400 and saved.limits.budget_usd == 2.5
    text = file.read_text()
    settings = load_workfolder_settings(fresh)
    assert settings.concurrency == {"fake-auth": 3}
    assert settings.defaults["claude"].model == "claude-haiku-5-5"
    assert api.state.settings == settings
    assert "wall_time: 1h30m" in text and "turns_per_agent: 40" in text
    assert re.search(r"slim_after_days: 7 +# Arbeitskopien löschen, Protokolle behalten", text)
    comments = [line for line in template.splitlines() if line.strip().startswith("#")]
    assert [line for line in text.splitlines() if line.strip().startswith("#")] == comments


async def test_new_keys_stay_in_their_section(fresh: Path) -> None:
    file = fresh / "arr.yaml"
    api = make_api(fresh)
    venv = fake_base_venv("mathe")
    info = await api.get_workfolder_settings()
    approvals = info.approvals.model_copy(
        update={"decider": Decider.GATEKEEPER_THEN_USER, "gatekeeper_model": "claude-sonnet-5-5"}
    )
    toolchain = info.toolchain.model_copy(update={"python": str(venv), "extra_path": ["/usr/bin"]})
    await api.update_workfolder_settings(
        info.model_copy(update={"approvals": approvals, "toolchain": toolchain})
    )
    settings = load_workfolder_settings(fresh)
    assert settings.toolchain.python == str(venv)
    assert settings.approvals.gatekeeper is not None
    assert settings.approvals.gatekeeper.mandate == "mandates/standard.md"
    lines = file.read_text().splitlines()

    def section(name: str) -> list[str]:
        start = lines.index(f"{name}:")
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i] and lines[i][0] not in " #"),
            len(lines),
        )
        return lines[start:end]

    assert any(line.startswith("  python: ") for line in section("toolchain"))
    assert any(line.startswith("  extra_path:") for line in section("toolchain"))
    assert any(line.startswith("  gatekeeper:") for line in section("approvals"))
    assert (
        lines.index(
            "# Aufräumregeln. arr verschlankt und archiviert nur, gelöscht wird nie automatisch."
        )
        == lines.index("retention:") - 1
    )


async def test_changing_the_gatekeeper_model_with_decider_user_needs_no_section(
    fresh: Path,
) -> None:
    api = make_api(fresh)
    info = await api.get_workfolder_settings()
    shown = info.approvals.model_copy(update={"gatekeeper_model": "claude-sonnet-5-5"})
    saved = await api.update_workfolder_settings(info.model_copy(update={"approvals": shown}))
    assert saved.approvals.gatekeeper_model is None
    assert load_workfolder_settings(fresh).approvals.gatekeeper is None
    no_model = info.approvals.model_copy(update={"decider": Decider.GATEKEEPER})
    with pytest.raises(ApiException) as error:
        await api.update_workfolder_settings(info.model_copy(update={"approvals": no_model}))
    assert error.value.error.field == "approvals.gatekeeper_model"


async def test_limit_of_an_existing_gate_follows_at_once(fresh: Path) -> None:
    api = make_api(fresh)
    gate = api.state.gates["fake-auth"] = ProfileGate("fake-auth", 1)
    assert await gate.acquire(lambda: False)
    waiter = asyncio.create_task(gate.acquire(lambda: False))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    info = await api.get_workfolder_settings()
    await api.update_workfolder_settings(info.model_copy(update={"concurrency": {"fake-auth": 2}}))
    assert gate.limit == 2
    assert await asyncio.wait_for(waiter, 1) is True, "the waiting turn starts without a restart"
    assert api.state.settings.concurrency == {"fake-auth": 2}

    info = await api.get_workfolder_settings()
    await api.update_workfolder_settings(info.model_copy(update={"concurrency": {}}))
    assert gate.limit is None and "concurrency" in (fresh / "arr.yaml").read_text()


async def test_refused_settings_leave_the_file_alone(fresh: Path) -> None:
    file = fresh / "arr.yaml"
    template = file.read_text()
    api = make_api(fresh)
    info = await api.get_workfolder_settings()
    with pytest.raises(ApiException) as error:
        await api.update_workfolder_settings(info.model_copy(update={"concurrency": {"wer": 2}}))
    assert error.value.error.field == "concurrency.wer" and file.read_text() == template
    with pytest.raises(ConfigError):
        update_settings_file(
            fresh, lambda data: set_setting(data, ("limits", "turns_per_agent"), 0)
        )
    assert file.read_text() == template


async def test_broken_arr_yaml_gives_a_message_with_the_line(fresh: Path) -> None:
    api = make_api(fresh)
    (fresh / "arr.yaml").write_text("version: 1\nlimits: [\n")
    with pytest.raises(ApiException) as error:
        await api.get_workfolder_settings()
    assert error.value.error.code is ErrorCode.CONFIG_INVALID


async def test_short_timeout_in_arr_yaml_stays_as_it_is(fresh: Path) -> None:
    file = fresh / "arr.yaml"
    file.write_text(file.read_text().replace("timeout: 20m", "timeout: 30s"))
    api = make_api(fresh)
    info = await api.get_workfolder_settings()
    assert info.approvals.timeout_s == 60
    await api.update_workfolder_settings(info)
    assert "timeout: 30s" in file.read_text()


async def test_the_current_workfolder_through_a_link_is_no_switch(wf: Path, tmp_path: Path) -> None:
    link = tmp_path / "verweis"
    link.symlink_to(wf)
    api = make_api(wf)
    store = api.state.store
    await api.select_workfolder(WorkfolderRequest(path=str(link)))
    assert api.state.store is store


async def test_external_change_of_arr_yaml_is_seen(fresh: Path) -> None:
    api = make_api(fresh)
    gate = api.state.gates["fake-auth"] = ProfileGate("fake-auth", 1)
    update_settings_file(fresh, lambda data: set_setting(data, ("concurrency", "fake-auth"), 5))
    assert (await api.get_workfolder_settings()).concurrency == {"fake-auth": 5}
    assert gate.limit == 5


async def test_app_settings_are_kept_in_the_app_folder(fresh: Path, arr_home: Path) -> None:
    api = make_api(fresh)
    assert (await api.get_app_settings()).port == 8765
    settings = AppSettings(
        open_in=OpenIn.WINDOW,
        appearance=Appearance.DARK,
        notifications={NotificationEvent.RUN_FINISHED: False},
    )
    saved = await api.update_app_settings(settings)
    assert saved.appearance.value == "dark" and saved.open_in is OpenIn.WINDOW
    assert saved.notifications[NotificationEvent.RUN_FINISHED] is False
    assert saved.notifications[NotificationEvent.APPROVAL] is True
    assert [w.path for w in saved.workfolders] == [str(fresh)]
    file = arr_home / "settings.yaml"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert (await make_api(fresh).get_app_settings()).appearance.value == "dark"
    with pytest.raises(ApiException) as error:
        await api.update_app_settings(saved.model_copy(update={"port": 9000}))
    assert error.value.error.field == "port"
    file.write_text("appearance: lila\n")
    assert (await api.get_app_settings()).appearance.value == "system"


def test_duration_text_is_understood_by_arr_yaml() -> None:
    from arr.models import parse_duration

    for seconds, text in [
        (7200, "2h"),
        (5400, "1h30m"),
        (45, "45s"),
        (90000, "1d1h"),
        (1200, "20m"),
    ]:
        assert duration_text(seconds) == text
        assert parse_duration(text).total_seconds() == seconds


# --- A5: fixed rules --------------------------------------------------------------------------


RULES = (
    "permissions:\n"
    "  always:\n"
    "    # pip ist in Ordnung\n"
    "    - {action: allow, hosts: [pypi.org], reason: Pakete}\n"
    "    - {action: deny, tools: [Bash], commands: ['curl *']}\n"
)


async def test_fixed_rule_shows_and_can_be_removed_then_the_agent_asks_again(
    fresh: Path,
    env: Any,  # noqa: F811
) -> None:
    file = fresh / "arr.yaml"
    file.write_text(file.read_text() + RULES)
    api = make_api(fresh)
    rules = await api.list_fixed_rules()
    own = [r for r in rules if r.removable]
    assert [r.rule for r in own] == ["pypi.org", "Bash: curl *"]
    assert [r.effect.value for r in own] == ["allow", "deny"]
    assert own[0].origin == "user" and "Pakete" in own[0].origin_text
    assert sum(1 for r in rules if r.origin == "builtin") == 5

    def asked_host() -> DecisionAction:
        settings = load_workfolder_settings(fresh)
        loaded = PermissionRules.from_settings(settings.permissions)
        engine = PolicyEngine([env.profile(rules=loaded)], rules=loaded)
        return engine.evaluate(req("SandboxNetworkAccess", hosts=["pypi.org"])).action

    assert asked_host() is DecisionAction.ALLOW
    left = await api.delete_fixed_rule(own[0].id)
    assert asked_host() is DecisionAction.ASK
    assert [r.rule for r in left if r.removable] == ["Bash: curl *"]
    assert "pypi.org" not in file.read_text() and "# Aufräumregeln" in file.read_text()
    with pytest.raises(ApiException) as error:
        await api.delete_fixed_rule(own[0].id)
    assert error.value.error.code is ErrorCode.NOT_FOUND
    with pytest.raises(ApiException) as error:
        await api.delete_fixed_rule("std-1")
    assert error.value.error.code is ErrorCode.NOT_ALLOWED


async def test_removed_rule_also_leaves_the_running_runs(wf: Path) -> None:
    file = wf / "arr.yaml"
    file.write_text(file.read_text() + "permissions: {always: [{action: allow, hosts: [a.org]}]}\n")
    api = make_api(wf, {"a": turns([say("los"), wait(30), done()])})
    await api.start()
    try:
        started = await api.start_run(RUN_INPUT)
        handle = api.state.registry.get(started.run_id)
        assert handle is not None

        def engine_ready() -> bool:
            return handle.manager is not None and handle.manager.inbox is not None

        await asyncio.to_thread(wait_for, engine_ready, what="inbox")
        assert handle.manager is not None and handle.manager.inbox is not None
        engine = handle.manager.inbox.engine
        assert len(engine.rules.always) == 1
        [rule] = [r for r in await api.list_fixed_rules() if r.removable]
        await api.delete_fixed_rule(rule.id)
        assert engine.rules.always == []
    finally:
        await api.stop_runs()
        await api.close()


# --- A6: the Python of the agents -------------------------------------------------------------


def test_template_names_no_python_below_home() -> None:
    text = read_template("workfolder", "arr.yaml")
    assert ".venvs" not in text and "# python: /Users/Shared/arr-<user>/venvs/mathe" in text


def test_notice_when_python_lies_below_home(
    fresh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    uv_python = home / ".local" / "share" / "uv" / "python"
    uv_python.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "base_prefix", str(uv_python))
    settings = load_workfolder_settings(fresh)
    notice = toolchain_notice(settings, fresh)
    assert notice is not None
    assert notice.title == "Das Python von arr startet nicht für Codex-Agenten."
    assert "unter deinem Benutzerordner" in notice.text and "Codex-Sandbox" in notice.text
    assert "Claude-Agenten sind nicht betroffen" in notice.text
    assert "Eine Basis-venv kann arr noch nicht selbst anlegen" in notice.text
    assert "arr " not in notice.text.replace("arr noch", "")  # no command

    fake_base_venv("mathe")
    notice = toolchain_notice(settings, fresh)
    assert notice is not None and "Einstellungen → Python-Umgebung" in notice.text

    off = settings.model_copy(
        update={"toolchain": settings.toolchain.model_copy(update={"run_venv": RunVenvMode.OFF})}
    )
    assert toolchain_notice(off, fresh) is None

    monkeypatch.setattr(sys, "base_prefix", "/Library/Frameworks/Python.framework")
    assert toolchain_notice(settings, fresh) is None


def test_notice_for_a_base_venv_built_on_a_python_below_home(
    fresh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    venv = fake_base_venv("mathe")
    (venv / "pyvenv.cfg").write_text(f"home = {home / 'uv' / 'bin'}\n")
    update_settings_file(fresh, lambda d: set_setting(d, ("toolchain", "python"), str(venv)))
    settings = load_workfolder_settings(fresh)
    notice = toolchain_notice(settings, fresh)
    assert notice is not None and str(venv) in notice.title
    (venv / "pyvenv.cfg").write_text("home = /Library/Frameworks/Python.framework/bin\n")
    assert toolchain_notice(settings, fresh) is None


async def test_settings_carry_candidates_and_notice(fresh: Path) -> None:
    fake_base_venv("mathe")
    fake_base_venv("mäthe")
    fake_base_venv("ma[th]e")
    (base_venv_dir("ohne-python")).mkdir()
    assert [c.path for c in base_venvs()] == [str(base_venv_dir("mathe"))]
    assert base_venvs()[0].python_version == "3.12.0"
    info = await make_api(fresh).get_workfolder_settings()
    assert [c.path for c in info.toolchain_candidates] == [str(base_venv_dir("mathe"))]


@pytest.mark.parametrize(
    ("name", "text"),
    [("mäthe", "Umlaute"), ("ma[th]e", "Umlaute"), ("nicht-da", "keine Basis-venv")],
)
async def test_only_base_venvs_of_the_list_can_be_chosen(fresh: Path, name: str, text: str) -> None:
    fake_base_venv("mathe")
    if name != "nicht-da":
        fake_base_venv(name)
    api = make_api(fresh)
    info = await api.get_workfolder_settings()
    chosen = info.toolchain.model_copy(update={"python": str(base_venv_dir(name))})
    with pytest.raises(ApiException) as error:
        await api.update_workfolder_settings(info.model_copy(update={"toolchain": chosen}))
    assert text in str(error.value) and error.value.error.field == "toolchain.python"
    good = info.toolchain.model_copy(update={"python": str(base_venv_dir("mathe"))})
    saved = await api.update_workfolder_settings(info.model_copy(update={"toolchain": good}))
    assert saved.toolchain.python == str(base_venv_dir("mathe"))
    cleared = saved.toolchain.model_copy(update={"python": None})
    saved = await api.update_workfolder_settings(saved.model_copy(update={"toolchain": cleared}))
    assert (
        saved.toolchain.python is None and load_workfolder_settings(fresh).toolchain.python is None
    )


async def test_new_workfolder_takes_the_offered_base_venv(wf: Path, tmp_path: Path) -> None:
    venv = fake_base_venv("mathe")
    target = tmp_path / "zwei"
    api = make_api(wf)
    await api.select_workfolder(WorkfolderRequest(path=str(target), create=True, python=str(venv)))
    assert load_workfolder_settings(target).toolchain.python == str(venv)
    assert "# Aufräumregeln" in (target / "arr.yaml").read_text()
    with pytest.raises(ApiException) as error:
        await api.select_workfolder(
            WorkfolderRequest(path=str(tmp_path / "drei"), create=True, python="/bin/irgendwas")
        )
    assert error.value.error.field == "toolchain.python" and not (tmp_path / "drei").exists()


async def test_toolchain_test_runs_the_interpreter(fresh: Path) -> None:
    api = make_api(fresh)
    venv = fake_base_venv("mathe")
    result = await api.test_toolchain(str(venv))
    assert result.python == str(venv) and result.python_version == sys.version.split()[0]
    assert set(result.packages) == {"sympy", "numpy"} and result.duration_ms is not None
    assert result.ok is all(result.packages.values())
    assert "fehl" in result.message or "funktioniert" in result.message

    own = await api.test_toolchain(SYSTEM_PYTHON)
    assert own.python == SYSTEM_PYTHON and own.python_version

    (venv / "bin" / "python3").unlink()
    (venv / "bin" / "python3").write_text("#!/bin/sh\nexit 3\n")
    (venv / "bin" / "python3").chmod(0o755)
    broken = await api.test_toolchain(str(venv))
    assert not broken.ok and "startet nicht" in broken.message

    foreign = await api.test_toolchain("/bin/echo")
    assert not foreign.ok and foreign.python_version is None and "Liste" in foreign.message


# --- W3-04.1: findings of the review ----------------------------------------------------------


NOT_UTF8 = b"# Projekt\n\xff\xfe\n"


async def expect_error(call: Any) -> Any:
    """The `ApiError` of an awaited call; never a command to type (docs/08 §8.4)."""
    with pytest.raises(ApiException) as error:
        await call
    assert "arr init" not in error.value.error.message
    return error.value.error


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home folder that does not hold the workfolder, the app folder or the venv root."""
    folder = tmp_path / "benutzer" / "ich"
    (folder / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(folder))
    (tmp_path / "tempordner").mkdir()  # `tmp_path` itself lies in the temp folder of the user
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tempordner"))
    return folder


async def test_a_file_that_is_not_utf8_costs_one_check_not_the_page(wf: Path) -> None:
    (wf / "projects" / "kaputt").mkdir()
    (wf / "projects" / "kaputt" / "CLAUDE.md").write_bytes(NOT_UTF8)
    (wf / "projects" / "yaml-kaputt").mkdir()
    (wf / "projects" / "yaml-kaputt" / "project.yaml").write_bytes(NOT_UTF8)
    api = make_api(wf)
    assert [p.name for p in await api.list_projects()] == ["demo", "kaputt", "yaml-kaputt"]
    project = await api.get_project("kaputt")
    check = next(c for c in project.checks if c.key == "claude_md")
    assert not check.ok and check.severity is Severity.ERROR and check.action is None
    assert check.message == "CLAUDE.md lässt sich nicht lesen"
    assert check.detail is not None and "UTF-8" in check.detail
    assert project.title == "kaputt" and not project.setup_ok

    error = await expect_error(api.setup_project("kaputt"))
    assert "UTF-8" in error.message and "CLAUDE.md" in error.message
    assert (wf / "projects" / "kaputt" / "CLAUDE.md").read_bytes() == NOT_UTF8
    error = await expect_error(api.setup_project("yaml-kaputt"))
    assert "UTF-8" in error.message and "project.yaml" in error.message


async def test_a_lone_mark_in_claude_md_is_named_in_the_check(wf: Path) -> None:
    (wf / "projects" / "demo" / "CLAUDE.md").write_text("<!-- /arr:user-agent -->\n")
    check = (await make_api(wf).get_project("demo")).checks[0]
    assert not check.ok and check.detail is not None and "Marke" in check.detail


async def test_repair_in_a_folder_arr_may_not_write_to_gives_a_message(wf: Path) -> None:
    folder = wf / "projects" / "demo"
    api = make_api(wf)
    folder.chmod(0o555)
    try:
        error = await expect_error(api.setup_project("demo"))
    finally:
        folder.chmod(0o755)
    assert error.code is ErrorCode.INVALID
    assert "„demo“ ließ sich nicht einrichten (keine Berechtigung)" in error.message
    assert "Permission denied" not in error.message
    assert sorted(p.name for p in folder.iterdir()) == ["check.py", "notes.md"]


async def test_repair_turns_a_value_error_into_a_message(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("kaputt")

    monkeypatch.setattr("arr.service.real_api.setup.setup_project", broken)
    error = await expect_error(make_api(wf).setup_project("demo"))
    assert "„demo“ ließ sich nicht einrichten" in error.message and "kaputt" not in error.message


@pytest.mark.parametrize(
    "path",
    [
        "{wf}/projects/..",
        "{wf}/projects/../",
        "{wf}/projects/.",
        "{wf}/projects/demo/..",
        "{wf}/projects/../projects/..",
        "{wf}",
        "{wf}/projects/demo/notes",
        "~nutzer-den-es-nicht-gibt/x",
        "ohne/anfang",
        "mit\0null",
    ],
)
async def test_existing_path_stays_directly_below_projects(wf: Path, path: str) -> None:
    (wf / "projects" / "demo" / "notes").mkdir()
    before = snapshot(wf)
    request = CreateProjectRequest(name="x", existing_path=path.format(wf=wf))
    error = await expect_error(make_api(wf).create_project(request))
    assert error.code is ErrorCode.INVALID and error.field == "path"
    assert snapshot(wf) == before


async def test_existing_path_takes_every_way_to_a_folder_below_projects(wf: Path) -> None:
    api = make_api(wf)
    request = CreateProjectRequest(name="x", existing_path=f"{wf}/projects/gibt/../demo/")
    assert (await api.create_project(request)).project.name == "demo"
    assert (wf / "projects" / "demo" / ".mcp.json").is_file()

    for name in ("gibt-es-nicht", "a" * 300):
        request = CreateProjectRequest(name="x", existing_path=f"{wf}/projects/{name}")
        assert (await expect_error(api.create_project(request))).code is ErrorCode.NOT_FOUND


async def test_links_below_projects_are_refused(wf: Path, tmp_path: Path) -> None:
    foreign = tmp_path / "fremd"
    foreign.mkdir()
    (wf / "projects" / "aussen").symlink_to(foreign)
    (wf / "projects" / "zwilling").symlink_to(wf / "projects" / "demo")
    (wf / "projects" / "leer").symlink_to(tmp_path / "weg")
    api = make_api(wf)
    before = snapshot(wf / "projects" / "demo")
    for name in ("aussen", "zwilling", "leer"):
        request = CreateProjectRequest(name="x", existing_path=f"{wf}/projects/{name}")
        error = await expect_error(api.create_project(request))
        assert error.code is ErrorCode.INVALID and "symbolischer Link" in error.message
    for name in ("aussen", "zwilling"):
        error = await expect_error(api.setup_project(name))
        assert error.code is ErrorCode.INVALID and "symbolischer Link" in error.message
    assert not list(foreign.iterdir())
    assert snapshot(wf / "projects" / "demo") == before


async def test_only_the_open_workfolder_names_its_active_runs(wf: Path, tmp_path: Path) -> None:
    other = init_workfolder(tmp_path / "zweiter")
    remember_workfolder(other)
    api = make_api(wf)
    assert [r.active_runs for r in (await api.get_workfolder()).known] == [0, None]
    make_run_folder(wf, status=RunStatus.RUNNING)
    make_run_folder(wf, status=RunStatus.PAUSED)
    make_run_folder(wf, status=RunStatus.DONE)
    api.state.store.scan()
    known = (await api.get_workfolder()).known
    assert [(r.path, r.active_runs) for r in known] == [(str(wf), 2), (str(other), None)]
    assert known[0].active_runs == (await api.get_counts()).active


def extra_path_cases(tmp_path: Path, home: Path, wf: Path, arr_home: Path) -> dict[str, str]:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "zu-ssh").symlink_to(home / ".ssh")
    (outside / "datei").write_text("")
    root = venv_root()
    (root / "venvs" / "mathe").mkdir(parents=True, exist_ok=True)
    return {
        "wurzel /": "/",
        "Elternordner von ~": str(home.parent),
        "Elternordner weiter oben": str(tmp_path),
        "~": str(home),
        "unter ~": str(home / ".ssh"),
        "Link nach ~/.ssh": str(outside / "zu-ssh"),
        "mit ..": f"{outside}/../benutzer/ich/.ssh",
        "App-Ordner": str(arr_home),
        "unter dem App-Ordner": str(arr_home / "logs"),
        "Workfolder": str(wf),
        "unter dem Workfolder": str(wf / ".arr"),
        "<wurzel>": str(root),
        "unter <wurzel>": str(root / "venvs" / "mathe"),
        "fehlt": str(outside / "gibt-es-nicht"),
        "Datei": str(outside / "datei"),
        "relativ": "bin",
        "mit ~": "~/.ssh",
    }


async def save_extra_path(api: RealApi, entries: list[str]) -> Any:
    settings = await api.get_workfolder_settings()
    toolchain = settings.toolchain.model_copy(update={"extra_path": entries})
    return await api.update_workfolder_settings(
        settings.model_copy(update={"toolchain": toolchain})
    )


async def test_extra_path_refuses_what_opens_a_way_around_the_read_list(
    fresh: Path, tmp_path: Path, home: Path, arr_home: Path
) -> None:
    (arr_home / "logs").mkdir(exist_ok=True)
    api = make_api(fresh)
    text = (fresh / "arr.yaml").read_text()
    for case, entry in extra_path_cases(tmp_path, home, fresh, arr_home).items():
        error = await expect_error(save_extra_path(api, [entry]))
        assert error.code is ErrorCode.INVALID, case
        assert error.field == "toolchain.extra_path", case
        assert entry in error.message, case
        assert (fresh / "arr.yaml").read_text() == text, case


def second_names(home: Path) -> list[str]:
    entries = ["/System/Volumes/Data", f"/System/Volumes/Data{home.resolve()}/.ssh"]
    assert all(Path(entry).is_dir() for entry in entries)
    return entries


@pytest.mark.skipif(not Path("/System/Volumes/Data/Users").exists(), reason="only on macOS")
async def test_extra_path_sees_through_the_second_name_of_the_data_volume(
    fresh: Path, home: Path
) -> None:
    api = make_api(fresh)
    for entry in second_names(home):
        await expect_error(save_extra_path(api, [entry]))


def program_folders(tmp_path: Path) -> list[str]:
    (tmp_path / "programme").mkdir()
    wanted = [str(tmp_path / "programme"), "/opt/homebrew/bin"]
    return [entry for entry in wanted if Path(entry).is_dir()]


async def test_extra_path_takes_a_program_folder(fresh: Path, tmp_path: Path, home: Path) -> None:
    entries = program_folders(tmp_path)
    settings = await save_extra_path(make_api(fresh), entries)
    assert settings.toolchain.extra_path == entries
    assert load_workfolder_settings(fresh).toolchain.extra_path == entries


async def test_extra_path_keeps_what_the_user_wrote_by_hand(
    fresh: Path, tmp_path: Path, home: Path
) -> None:
    hand = [str(home / ".ssh"), "/gibt/es/nicht"]
    update_settings_file(fresh, lambda d: set_setting(d, ("toolchain", "extra_path"), hand))
    api = make_api(fresh)
    settings = await api.get_workfolder_settings()
    assert settings.toolchain.extra_path == hand

    limits = settings.limits.model_copy(update={"budget_usd": 7.0})
    saved = await api.update_workfolder_settings(settings.model_copy(update={"limits": limits}))
    assert saved.limits.budget_usd == 7.0 and saved.toolchain.extra_path == hand

    programs = tmp_path / "programme"
    programs.mkdir()
    assert (await save_extra_path(api, [*hand, str(programs)])).toolchain.extra_path[2:] == [
        str(programs)
    ]
    assert (await save_extra_path(api, hand)).toolchain.extra_path == hand
    await expect_error(save_extra_path(api, [*hand, str(home)]))


@pytest.mark.parametrize("name", ["a" * 300, f"{'a' * 300}/b"])
async def test_a_path_the_system_refuses_gives_a_message(
    wf: Path, tmp_path: Path, name: str
) -> None:
    path = tmp_path / name
    error = await expect_error(
        make_api(wf).select_workfolder(WorkfolderRequest(path=str(path), create=True))
    )
    assert error.field == "path" and "der Name ist zu lang" in error.message


async def test_a_write_protected_arr_yaml_stays(fresh: Path) -> None:
    file = fresh / "arr.yaml"
    text = file.read_text()
    file.chmod(0o444)
    api = make_api(fresh)
    settings = await api.get_workfolder_settings()
    limits = settings.limits.model_copy(update={"budget_usd": 7.0})
    error = await expect_error(
        api.update_workfolder_settings(settings.model_copy(update={"limits": limits}))
    )
    assert "schreibgeschützt" in error.message
    assert file.read_text() == text and not stat.S_IWUSR & file.stat().st_mode
    with pytest.raises(ArrError, match="schreibgeschützt"):
        update_settings_file(fresh, lambda d: set_setting(d, ("version",), 1))


async def test_app_settings_keep_keys_of_others(fresh: Path, arr_home: Path) -> None:
    write_app_settings({"open_in": "tab", "kuenftig": {"wert": 1}})
    api = make_api(fresh)
    settings = await api.get_app_settings()
    await api.update_app_settings(settings.model_copy(update={"appearance": Appearance.DARK}))
    saved = read_app_settings()
    assert saved["kuenftig"] == {"wert": 1} and saved["appearance"] == "dark"


async def test_messages_of_the_service_name_no_command(fresh: Path) -> None:
    api = make_api(fresh)
    settings = await api.get_workfolder_settings()
    (fresh / "arr.yaml").unlink()
    for call in (
        api.get_workfolder_settings(),
        api.update_workfolder_settings(settings),
        api.list_fixed_rules(),
    ):
        error = await expect_error(call)
        assert "fehlt die arr.yaml" in error.message and error.code is ErrorCode.CONFIG_INVALID
    error = await expect_error(api.create_project(CreateProjectRequest(name="neu")))
    assert "fehlt die arr.yaml" in error.message
    assert not (fresh / "projects" / "neu").exists()


async def test_a_failed_new_workfolder_names_no_command(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(_path: Path, _text: str) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("arr.workfolder._write_new", refused)
    error = await expect_error(
        make_api(wf).select_workfolder(WorkfolderRequest(path=str(tmp_path / "neu"), create=True))
    )
    assert error.field == "path" and "Kein Schreibrecht" in error.message


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (errno.EACCES, "keine Berechtigung"),
        (errno.EEXIST, "das gibt es schon"),
        (errno.ENOSPC, "kein Speicherplatz frei"),
        (errno.ENAMETOOLONG, "der Name ist zu lang"),
        (errno.EBUSY, f"Systemfehler {errno.EBUSY}"),
    ],
)
def test_os_errors_have_a_german_cause(number: int, text: str) -> None:
    assert os_reason(OSError(number, os.strerror(number))) == text


async def test_new_project_and_folder_errors_are_in_german(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(wf)
    closed = tmp_path / "gesperrt"
    closed.mkdir()
    closed.chmod(0o300)
    try:
        error = await expect_error(
            api.select_workfolder(WorkfolderRequest(path=str(closed), create=True))
        )
    finally:
        closed.chmod(0o700)
    assert "keine Berechtigung" in error.message and "Permission" not in error.message

    def taken(*_args: Any, **_kwargs: Any) -> None:
        raise FileExistsError(errno.EEXIST, "File exists")

    monkeypatch.setattr("arr.service.real_api.setup.create_project", taken)
    error = await expect_error(api.create_project(CreateProjectRequest(name="neu")))
    assert "das gibt es schon" in error.message and "File exists" not in error.message


@pytest.mark.parametrize(
    ("packages", "text"),
    [
        ({"sympy": False, "numpy": True}, "aber sympy fehlt."),
        ({"sympy": False, "numpy": False}, "aber sympy und numpy fehlen."),
    ],
)
async def test_missing_packages_read_right(
    fresh: Path, monkeypatch: pytest.MonkeyPatch, packages: dict[str, bool], text: str
) -> None:
    def probe(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = json.dumps({"version": "3.12.0", "packages": packages})
        return subprocess.CompletedProcess([], 0, stdout=output)

    monkeypatch.setattr("arr.service.real_api.setup.subprocess.run", probe)
    result = await make_api(fresh).test_toolchain(SYSTEM_PYTHON)
    assert not result.ok and result.message.endswith(text)
    assert result.detail is not None and "Run-venv" in result.detail


async def test_toolchain_test_splits_the_notice(
    fresh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "uv"))
    monkeypatch.setattr(
        "arr.service.real_api.setup.subprocess.run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"version": "3.12.0", "packages": {"sympy": True}})
        ),
    )
    result = await make_api(fresh).test_toolchain(SYSTEM_PYTHON)
    assert not result.ok and result.message.startswith("Das Python von arr startet nicht")
    assert result.detail is not None and "Codex-Sandbox" in result.detail


async def test_an_arr_yaml_that_is_not_utf8_gives_a_message(wf: Path, tmp_path: Path) -> None:
    other = init_workfolder(tmp_path / "zweiter")
    (other / "arr.yaml").write_bytes(NOT_UTF8)
    api = make_api(wf)
    error = await expect_error(api.select_workfolder(WorkfolderRequest(path=str(other))))
    assert error.code is ErrorCode.CONFIG_INVALID and "UTF-8" in error.message
    assert api.state.workfolder == wf
    (wf / "arr.yaml").write_bytes(NOT_UTF8)
    assert "UTF-8" in (await expect_error(api.get_workfolder_settings())).message


# --- W3-04.2: after the merge of W3-02 and W3-03 --------------------------------------------


async def test_extra_path_follows_the_lists_of_the_sandbox_profile(
    tmp_path: Path, home: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setenv("ARR_VENV_ROOT", str(tmp_path / "geteilt" / "arr-ich"))
    (tmp_path / "geteilt" / "arr-ich").mkdir(parents=True)
    workfolder = init_workfolder(tmp_path / "daten" / "wf")
    (tmp_path / "frei" / "tresor.keychain").mkdir(parents=True)
    (tmp_path / "frei" / "geheim").mkdir()
    (tmp_path / "tempordner" / "unter").mkdir()
    update_settings_file(
        workfolder,
        lambda d: set_setting(
            d, ("permissions", "sensitive_paths"), [str(tmp_path / "frei" / "geheim")]
        ),
    )
    cases = {
        "SENSITIVE_ABSOLUTE_PATHS": "/Library/Keychains",
        "darunter": "/Library/Keychains/System",
        "Eltern davon": "/Library",
        "Muster für Namen": str(tmp_path / "frei" / "tresor.keychain"),
        "sensitive_paths aus arr.yaml": str(tmp_path / "frei" / "geheim"),
        "Eltern des Workfolders": str(tmp_path / "daten"),
        "Eltern von <wurzel>": str(tmp_path / "geteilt"),
        "Temp-Ordner": str(tmp_path / "tempordner"),
        "unter dem Temp-Ordner": str(tmp_path / "tempordner" / "unter"),
        "/tmp/claude-<uid>": f"/tmp/claude-{os.getuid()}",
        "Eltern davon: /tmp": "/tmp",
        "/Volumes": "/Volumes",
    }
    api = make_api(workfolder)
    text = (workfolder / "arr.yaml").read_text()
    for case, entry in cases.items():
        error = await expect_error(save_extra_path(api, [entry]))
        assert error.field == "toolchain.extra_path", case
        assert "Agenten nicht lesen dürfen" in error.message, case
        assert (workfolder / "arr.yaml").read_text() == text, case


async def test_a_run_planned_during_a_switch_does_not_start(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "zweiter").mkdir()
    other = make_workfolder(tmp_path / "zweiter")
    api = make_api(wf)
    planning, go_on = threading.Event(), threading.Event()
    plan = RunsArea._plan

    def slow_plan(area: RunsArea, run_input: dict[str, Any]) -> Any:
        launch = plan(area, run_input)
        planning.set()
        assert go_on.wait(10)
        return launch

    monkeypatch.setattr(RunsArea, "_plan", slow_plan)
    start = asyncio.create_task(api.start_run(RUN_INPUT))
    assert await asyncio.to_thread(planning.wait, 10)
    await api.select_workfolder(WorkfolderRequest(path=str(other)))
    go_on.set()
    error = await expect_error(start)
    assert error.code is ErrorCode.NOT_ALLOWED and "nicht gestartet" in error.message
    assert not run_dirs(wf) and not run_dirs(other) and not api.state.registry.active()
    monkeypatch.setattr(RunsArea, "_plan", plan)
    assert (await api.select_workfolder(WorkfolderRequest(path=str(wf)))).path == str(wf)


CODEX_RUN = {
    **RUN_INPUT,
    "defaults": {"provider": "codex", "model": "gpt-5.5", "auth": "fake-codex"},
}


@pytest.fixture
def python_below_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "benutzer"))
    (tmp_path / "benutzer" / "uv").mkdir(parents=True)
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "benutzer" / "uv"))


async def test_a_run_with_codex_agents_is_told_about_python_below_home(
    fresh: Path, arr_home: Path, python_below_home: None
) -> None:
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    (fresh / "projects" / "demo").mkdir()
    api = make_api(fresh)
    result = await api.resolve_run(CODEX_RUN)
    assert not result.errors
    assert [w for w in result.warnings if "startet nicht für Codex-Agenten" in w]
    assert any("Claude-Agenten sind nicht betroffen" in w for w in result.warnings)
    claude = {
        **RUN_INPUT,
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
    }
    assert not (await api.resolve_run(claude)).warnings


ALONE = {
    "project": "demo",
    "skill": "",
    "name": "",
    "task_md": "# Aufgabe\n\nFrage",
    "params": {},
    "agents": {},
    "limits": {},
}


async def test_a_new_workfolder_takes_its_standards_from_the_profiles(
    fresh: Path, arr_home: Path
) -> None:
    write_profiles(arr_home, {"mein-claude": "claude_oauth", "zweites": "claude_oauth"})
    (fresh / "projects" / "demo").mkdir()
    result = await make_api(fresh).resolve_run(ALONE)
    assert not result.errors
    [agent] = result.agents
    assert (agent.provider, agent.profile) == (Provider.CLAUDE, "mein-claude")
    assert agent.model == "claude-sonnet-5-5"
    [warning] = result.warnings
    assert "dieser Run nutzt Claude Sonnet 5.5 mit dem Profil „mein-claude“" in warning
    assert "Einstellungen → Standards und Limits" in warning and "arr.yaml" not in warning


async def test_only_a_chatgpt_profile_gives_a_codex_agent(fresh: Path, arr_home: Path) -> None:
    write_profiles(arr_home, {"chatgpt": "codex_chatgpt"})
    (fresh / "projects" / "demo").mkdir()
    [agent] = (await make_api(fresh).resolve_run(ALONE)).agents
    assert (agent.provider, agent.model, agent.profile) == (Provider.CODEX, "gpt-5.5", "chatgpt")


async def test_standards_that_exist_stay_and_only_the_gap_is_filled(
    fresh: Path, arr_home: Path
) -> None:
    write_profiles(arr_home, {"erstes": "claude_oauth", "gewaehlt": "claude_oauth"})
    (fresh / "projects" / "demo").mkdir()
    update_settings_file(
        fresh, lambda d: set_setting(d, ("defaults", "claude", "auth"), "gewaehlt")
    )
    result = await make_api(fresh).resolve_run(ALONE)
    [agent] = result.agents
    assert (agent.model, agent.profile) == ("claude-sonnet-5-5", "gewaehlt")
    assert "Claude Sonnet 5.5 mit dem Profil „gewaehlt“" in result.warnings[0]

    update_settings_file(
        fresh, lambda d: set_setting(d, ("defaults", "claude", "model"), "claude-opus-5-5")
    )
    result = await make_api(fresh).resolve_run(ALONE)
    assert result.agents[0].model == "claude-opus-5-5" and not result.warnings


async def test_another_provider_in_the_form_takes_its_first_profile(
    fresh: Path, arr_home: Path
) -> None:
    write_profiles(arr_home, {"mein-claude": "claude_oauth", "chatgpt": "codex_chatgpt"})
    (fresh / "projects" / "demo").mkdir()
    api = make_api(fresh)
    [agent] = (await api.resolve_run(ALONE)).agents
    changed = {**ALONE, "agents": {agent.id: {"model": "gpt-5.5"}}}
    [codex] = (await api.resolve_run(changed)).agents
    assert (codex.provider, codex.profile) == (Provider.CODEX, "chatgpt")


async def test_without_a_profile_for_the_provider_the_form_names_no_file(
    fresh: Path, arr_home: Path
) -> None:
    write_profiles(arr_home, {})
    (arr_home / "profiles.yaml").write_text("version: 1\nprofiles: []\n")
    (fresh / "projects" / "demo").mkdir()
    result = await make_api(fresh).resolve_run(ALONE)
    assert result.errors and not any("arr.yaml" in i.message for i in result.errors)


async def test_a_run_file_still_names_what_is_missing_itself(fresh: Path, arr_home: Path) -> None:
    write_profiles(arr_home, {"mein-claude": "claude_oauth"})
    (fresh / "projects" / "demo").mkdir()
    bare = {k: v for k, v in RUN_INPUT.items() if k != "defaults"}
    result = await make_api(fresh).resolve_run(bare)
    assert any("defaults.claude" in i.message for i in result.errors)


async def test_a_file_without_rights_reads_right_in_the_check(wf: Path) -> None:
    file = wf / "projects" / "demo" / "CLAUDE.md"
    file.write_text("x")
    file.chmod(0o000)
    try:
        check = (await make_api(wf).get_project("demo")).checks[0]
    finally:
        file.chmod(0o644)
    assert check.detail == "Keine Berechtigung. Prüfe die Rechte der Datei."


# --- W3-04.3: last round before the merge -------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "words"),
    [
        ("gpt-5.5", "dieser Run nutzt gpt-5.5 mit dem Profil „chatgpt“"),
        ("claude-haiku-5-5", "dieser Run nutzt Claude Haiku 5.5 mit dem Profil „mein-claude“"),
    ],
)
async def test_the_warning_names_what_the_run_really_uses(
    fresh: Path, arr_home: Path, model: str, words: str
) -> None:
    write_profiles(arr_home, {"mein-claude": "claude_oauth", "chatgpt": "codex_chatgpt"})
    (fresh / "projects" / "demo").mkdir()
    api = make_api(fresh)
    [agent] = (await api.resolve_run(ALONE)).agents
    result = await api.resolve_run({**ALONE, "agents": {agent.id: {"model": model}}})
    [warning] = [w for w in result.warnings if "fehlen Standards" in w]
    assert words in warning and "Sonnet" not in warning


async def test_a_tab_that_fails_does_not_stop_the_switch(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nicegui import Client

    class Tab:
        has_socket_connection = True

        def __init__(self, broken: bool) -> None:
            self.broken = broken
            self.sent: list[str] = []

        def run_javascript(self, code: str) -> None:
            if self.broken:
                raise RuntimeError("Event loop is closed")
            self.sent.append(code)

    broken, fine = Tab(True), Tab(False)
    monkeypatch.setattr(Client, "instances", {"a": broken, "b": fine})
    other = init_workfolder(tmp_path / "zweiter")
    write_service_info(
        ServiceInfo(host="127.0.0.1", port=1, token="t", pid=os.getpid(), workfolder=str(wf))
    )
    api = make_api(wf)
    await api.select_workfolder(WorkfolderRequest(path=str(other)))
    info = read_service_info()
    assert api.state.workfolder == other and info is not None and info.workfolder == str(other)
    assert recent_workfolders()[0] == other
    assert fine.sent == ["location.assign('/')"]


async def test_app_settings_that_are_not_utf8_count_as_empty(fresh: Path, arr_home: Path) -> None:
    (arr_home / "settings.yaml").write_bytes(NOT_UTF8)
    assert read_app_settings() == {}
    assert (await make_api(fresh).get_app_settings()).notifications == {}


async def test_the_switch_for_finished_runs_counts_without_a_restart(fresh: Path) -> None:
    (fresh / "projects" / "demo").mkdir()
    update_settings_file(fresh, lambda d: set_setting(d, ("toolchain", "run_venv"), "off"))
    sent: list[str] = []
    api = RealApi(
        fresh,
        base_url=BASE,
        # long enough for the watcher to see it running: only an end it saw is announced
        builders=fake_builders({"a": turns([say("los"), wait(0.6), done()])}),
        manager_kwargs={"pacing": FAST},
        notify=lambda text, _title: sent.append(text),
    )
    await api.start()
    try:
        settings = await api.get_app_settings()
        off = {**settings.notifications, NotificationEvent.RUN_FINISHED: False}
        await api.update_app_settings(settings.model_copy(update={"notifications": off}))
        first = (await api.start_run(RUN_INPUT)).run_id
        await until_status(api, first, RunStatus.DONE)
        await asyncio.sleep(0.6)  # time for the watcher and the notifier to have passed it on
        assert not sent

        on = {**off, NotificationEvent.RUN_FINISHED: True}
        await api.update_app_settings(settings.model_copy(update={"notifications": on}))
        second = (await api.start_run(RUN_INPUT)).run_id
        await until_status(api, second, RunStatus.DONE)
        for _ in range(250):
            if sent:
                break
            await asyncio.sleep(0.02)
        assert len(sent) == 1 and sent[0].startswith("Run fertig")
    finally:
        await api.stop_runs()
        await api.close()

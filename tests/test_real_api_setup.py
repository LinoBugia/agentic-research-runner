"""Area `setup` of `RealApi` (W3-04): workfolder, projects, settings, fixed rules, toolchain.

The workfolders lie below `tmp_path`, runs use the FakeBackend. The switch in a real service
process is at the end (`service_child.py`, `ARR_TEST_SWITCH`).
"""

import asyncio
import json
import os
import re
import signal
import stat
import sys
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

from arr.errors import ConfigError
from arr.models import Decider, Effort, RunStatus, RunVenvMode
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
    WorkfolderRequest,
)
from arr.service.real_api import RealApi, live
from arr.service.real_api.setup import base_venvs, duration_text, toolchain_notice
from arr.service.state import (
    ServiceInfo,
    read_service_info,
    recent_workfolders,
    write_service_info,
)
from arr.skills import discover_skills
from arr.venvs import base_venv_dir
from arr.workfolder import (
    init_workfolder,
    load_workfolder_settings,
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
        "claude": ProviderDefaultsInfo(
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
    toolchain = info.toolchain.model_copy(
        update={"python": str(venv), "extra_path": ["/Library/TeX/texbin"]}
    )
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
    assert "unter deinem Benutzerordner" in notice and "Codex" in notice
    assert "Claude-Agenten sind nicht betroffen" in notice
    assert "noch nicht selbst" in notice
    assert "arr " not in notice.replace("arr läuft", "").replace("arr sie", "")  # no command

    fake_base_venv("mathe")
    notice = toolchain_notice(settings, fresh)
    assert notice is not None and "Einstellungen → Python-Umgebung" in notice

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
    assert notice is not None and str(venv) in notice
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
    assert "fehlt" in result.message or "funktioniert" in result.message

    own = await api.test_toolchain(SYSTEM_PYTHON)
    assert own.python == SYSTEM_PYTHON and own.python_version

    (venv / "bin" / "python3").unlink()
    (venv / "bin" / "python3").write_text("#!/bin/sh\nexit 3\n")
    (venv / "bin" / "python3").chmod(0o755)
    broken = await api.test_toolchain(str(venv))
    assert not broken.ok and "startet nicht" in broken.message

    foreign = await api.test_toolchain("/bin/echo")
    assert not foreign.ok and foreign.python_version is None and "Liste" in foreign.message

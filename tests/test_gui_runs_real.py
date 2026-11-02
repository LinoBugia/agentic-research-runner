"""Runs, Neuer Run and the run detail on `RealApi` with FakeBackends (W3-02 A1–A5).

W3-01 only checked that the pages load. Here they work: a run starts from the form and its
preview is its `run.yaml`, the list follows a second process, the detail page follows a run
from `queued` on, and a message reaches an agent with a visible delivery state.
"""

import asyncio
import json
import shutil
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from nicegui import ui
from nicegui.testing.user import User
from run_kit import FAST, done, fake_builders, post, say, turns, wait, write_profiles
from service_kit import (
    append,
    hold_lock,
    make_run_folder,
    make_workfolder,
    start_run_process,
    transcript_line,
)
from test_gui import gui_user
from test_gui_real import page_html, see

from arr.models import RunStatus
from arr.room.store import RoomStore
from arr.room.types import DeliveryStatus, MessageKind
from arr.runs import manager as manager_module
from arr.runs.manager import RunManager, set_run_status
from arr.service.api_models import (
    ApiException,
    ChannelMessageEvent,
    CountsChangedEvent,
    ErrorCode,
    PostMessageRequest,
    RunAction,
    RunFilter,
)
from arr.service.gui.pages.run_detail import START_CHECK_SECONDS
from arr.service.real_api import RealApi
from arr.service.real_api.rundir import counts_as_active

BASE = "http://test"
GROUP = (
    "kind: research-group\nversion: 1\ntopology: roundtable\n"
    "params:\n  rounds: {default: 2, min: 1, max: 5, doc: Runden}\n"
    "agents:\n  - {id: m, count: 2}\n"
    "groupchats:\n  - {name: main, members: [m-*]}\n"
)
FORM: dict[str, Any] = {
    "project": "demo",
    "skill": "runde",
    "name": "",
    "task_md": "# Aufgabe\n\nZwei Ideen prüfen.",
    "params": {"rounds": 1},
    "agents": {},
    "limits": {},
}
ANSWERS = {"m-1": turns([say("eins"), done()]), "m-2": turns([say("zwei"), done()])}


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "gpt-auth": "codex_chatgpt"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text(
        "version: 1\ntoolchain: {run_venv: off}\n"
        "defaults:\n"
        "  claude: {auth: fake-auth, model: fake-model, effort: low}\n"
        "  codex: {auth: gpt-auth, model: gpt-6-luna, effort: low}\n"
        "limits: {wall_time: 30m, turns_per_agent: 6, budget_usd: 0.5}\n"
        "concurrency: {fake-auth: 3}\n"
        "approvals: {decider: user, timeout: 5m}\n",
        "utf-8",
    )
    group = workfolder / "skills" / "runde"
    group.mkdir(parents=True)
    (group / "SKILL.md").write_text("---\nname: runde\ndescription: Eine Runde.\n---\n# Runde\n")
    (group / "group.yaml").write_text(GROUP)
    return workfolder


def make_api(wf: Path, scripts: Mapping[str, Any] | None = None) -> RealApi:
    return RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders(scripts or ANSWERS),
        manager_kwargs={"pacing": FAST},
    )


@asynccontextmanager
async def real_user(api: RealApi) -> AsyncIterator[User]:
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await api.start()
        try:
            yield user
        finally:
            await api.stop_runs()
            await api.close()


async def until(check: Any, within: float = 30.0, what: str = "") -> None:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"not reached: {what}")


async def until_final(api: RealApi, run_id: str) -> None:
    async def final() -> bool:
        return (await api.get_run(run_id)).run.status.is_final

    await until(final, what="run final")


async def until_status(api: RealApi, run_id: str, status: RunStatus) -> None:
    async def reached() -> bool:
        return (await api.get_run(run_id)).run.status is status

    await until(reached, what=str(status))


# --- A1: Neuer Run ---------------------------------------------------------------------------


async def test_settings_come_from_arr_yaml(wf: Path) -> None:
    settings = await make_api(wf).get_workfolder_settings()
    assert settings.limits.wall_time_s == 1800 and settings.limits.turns_per_agent == 6
    assert settings.limits.budget_usd == 0.5 and settings.approvals.timeout_s == 300
    assert settings.approvals.decider.value == "user"
    assert {p.value: d.auth for p, d in settings.defaults.items()} == {
        "claude": "fake-auth",
        "codex": "gpt-auth",
    }
    assert settings.concurrency == {"fake-auth": 3} and settings.running_runs == 0


async def test_settings_fall_back_to_the_built_in_limits(wf: Path) -> None:
    (wf / "arr.yaml").write_text("version: 1\n", "utf-8")
    limits = (await make_api(wf).get_workfolder_settings()).limits
    assert (limits.wall_time_s, limits.turns_per_agent, limits.budget_usd) == (7200, 40, 20)


async def test_form_starts_a_run_and_the_preview_is_its_run_yaml(wf: Path) -> None:
    """A1 with everything the form can change."""
    api = make_api(wf)
    form = {
        **FORM,
        "name": "Mein Lauf",
        "limits": {"wall_time_s": 600, "budget_usd": 0.25},
        "agents": {"m-1": {"web": True, "read_dirs": ["~/Literatur"]}},
        "decider": "user",
    }
    resolved = await api.resolve_run(form)
    assert not resolved.errors and [a.id for a in resolved.agents] == ["m-1", "m-2"]
    assert resolved.channels[0].members == ["m-1", "m-2"]
    assert resolved.agents[0].tools.web and resolved.agents[0].read_dirs == ["~/Literatur"]
    assert not resolved.agents[1].tools.web
    started = await api.start_run(form)
    await until_final(api, started.run_id)
    run_dir = wf / "runs" / started.run_id
    assert (run_dir / "run.yaml").read_text("utf-8") == resolved.resolved_run_yaml
    assert (run_dir / "task.md").read_text("utf-8") == FORM["task_md"]
    assert json.loads((run_dir / "meta.json").read_text("utf-8"))["starter"] == "gui"
    assert (await api.get_run(started.run_id)).run.limits.budget_usd == 0.25


async def test_preview_without_changes_is_the_run_yaml_too(wf: Path) -> None:
    api = make_api(wf)
    resolved = await api.resolve_run(FORM)
    started = await api.start_run(FORM)
    await until_final(api, started.run_id)
    text = (wf / "runs" / started.run_id / "run.yaml").read_text("utf-8")
    assert text == resolved.resolved_run_yaml


async def test_only_changed_limits_override_the_skill(wf: Path) -> None:
    (wf / "skills" / "runde" / "group.yaml").write_text(GROUP + "limits: {turns_per_agent: 3}\n")
    resolved = await make_api(wf).resolve_run(FORM)
    assert "turns_per_agent: 3" in resolved.resolved_run_yaml


async def test_a_model_of_the_other_provider_takes_its_profile(wf: Path) -> None:
    form = {**FORM, "agents": {"m-2": {"model": "gpt-6-luna"}}}
    agents = (await make_api(wf).resolve_run(form)).agents
    assert (agents[0].provider.value, agents[0].profile) == ("claude", "fake-auth")
    assert (agents[1].provider.value, agents[1].profile) == ("codex", "gpt-auth")
    assert agents[1].model == "gpt-6-luna"


async def test_run_without_skill_is_one_agent_alone(wf: Path) -> None:
    api = make_api(wf, {"solver": turns([say("allein"), done()])})
    form = {**FORM, "skill": None, "params": {}}
    resolved = await api.resolve_run(form)
    assert not resolved.errors and [a.id for a in resolved.agents] == ["solver"]
    started = await api.start_run(form)
    await until_final(api, started.run_id)
    assert (await api.get_run(started.run_id)).run.status is RunStatus.DONE


async def test_a_run_file_as_input_still_works(wf: Path) -> None:
    resolved = await make_api(wf).resolve_run(
        {
            "project": "demo",
            "task": "Los.",
            "topology": {"kind": "solo"},
            "agents": [{"id": "a"}],
            "defaults": {"model": "fake-model", "auth": "fake-auth"},
        }
    )
    assert not resolved.errors and resolved.agents[0].id == "a"


@pytest.mark.parametrize(
    ("change", "path", "text"),
    [
        ({"task_md": ""}, "task_md", "Die Aufgabe fehlt."),
        ({"params": {"rounds": 9}}, "params.rounds", "größer als das Maximum"),
        ({"params": {"rounds": None}}, "params.rounds", "Es fehlt ein Wert"),
        ({"project": "gibt-es-nicht"}, "project", "gibt-es-nicht"),
        ({"skill": "gibt-es-nicht"}, "skill", "gibt-es-nicht"),
        ({"agents": {"m-1": {"read_dirs": ["run:quatsch"]}}}, "agents", ""),
        ({"limits": {"turns_per_agent": 0}}, "limits.turns_per_agent", ""),
        ({"decider": "gatekeeper"}, "decider", "gatekeeper"),
    ],
)
async def test_invalid_input_stands_at_its_field_and_starts_nothing(
    wf: Path, change: dict[str, Any], path: str, text: str
) -> None:
    api = make_api(wf)
    form = {**FORM, **change}
    result = await api.resolve_run(form)
    assert result.errors, "no error"
    issue = next(i for i in result.errors if i.path.startswith(path))
    assert text in issue.message
    with pytest.raises(ApiException) as error:
        await api.start_run(form)
    assert error.value.error.code is ErrorCode.CONFIG_INVALID
    assert any(i.path.startswith(path) for i in error.value.error.issues)
    assert not list((wf / "runs").iterdir())


async def test_problems_of_the_preparation_show_before_the_start(wf: Path) -> None:
    """Missing roles and profiles would otherwise only end the run's task (no run left)."""
    (wf / "skills" / "runde" / "group.yaml").write_text(
        GROUP.replace("{id: m, count: 2}", "{id: m, count: 2, role: roles/fehlt.md}")
    )
    api = make_api(wf)
    result = await api.resolve_run(FORM)
    assert any(i.path == "agents.m-1.role" and "fehlt.md" in i.message for i in result.errors)
    (wf / "skills" / "runde" / "group.yaml").write_text(GROUP)
    (wf / "arr.yaml").write_text(
        (wf / "arr.yaml").read_text("utf-8").replace("{auth: fake-auth,", "{auth: weg,"), "utf-8"
    )
    result = await api.resolve_run(FORM)
    assert any(i.path == "agents.m-1.profile" and "weg" in i.message for i in result.errors)
    with pytest.raises(ApiException):
        await api.start_run(FORM)
    assert not list((wf / "runs").iterdir())


async def test_new_run_page_starts_and_lands_on_the_detail(wf: Path) -> None:
    api = make_api(wf)
    async with real_user(api) as user:
        await user.open("/neuer-run?skill=runde&projekt=demo")
        html = await see(user, "1 · Projekt und Skill")
        assert "W3-04" not in html
        html = await see(user, "Die Aufgabe fehlt.")
        assert "m-1" in html
        assert "fake-auth" in html
        user.find(marker="field-task_md").trigger("input", "# Aufgabe\n\nZwei Ideen prüfen.")
        await asyncio.sleep(0.7)
        await see(user, "Rollen und Skills gefunden")
        user.find(marker="start-run").click()
        await see(user, 'data-rd="', retries=120)
        run_id = next((wf / "runs").iterdir()).name
        assert run_id in page_html(user)


async def test_bad_limit_keeps_the_start_locked(wf: Path) -> None:
    api = make_api(wf)
    async with real_user(api) as user:
        await user.open("/neuer-run?skill=runde&projekt=demo")
        await see(user, "1 · Projekt und Skill")
        user.find(marker="field-task_md").trigger("input", "# Aufgabe\n\nFrage")
        user.find(marker="field-limits.budget_usd").trigger("input", "viel")
        user.find(marker="field-limits.budget_usd").trigger("blur")
        await asyncio.sleep(0.7)
        await see(user, "Gib einen Betrag an")
        user.find(marker="start-run").click()
        await asyncio.sleep(0.2)
        assert not list((wf / "runs").iterdir())


# --- A2: Runs-Liste ---------------------------------------------------------------------------


def _run_ids(html: str, ids: list[str]) -> list[str]:
    return [i for i in ids if i in html]


async def test_list_filters_searches_sorts_and_pages_real_runs(wf: Path) -> None:
    running = make_run_folder(wf, status=RunStatus.RUNNING, name="Läuft noch")
    lock = hold_lock(running)
    done_run = make_run_folder(wf, status=RunStatus.DONE, name="Collatz fertig")
    failed = make_run_folder(wf, status=RunStatus.FAILED, name="Schiefgegangen")
    for index, folder in enumerate((done_run, failed)):
        meta = json.loads((folder / "meta.json").read_text("utf-8"))
        meta["ended"] = 1000.0 + index
        (folder / "meta.json").write_text(json.dumps(meta), "utf-8")
    api = make_api(wf)
    try:
        page = await api.list_runs()
        assert [r.id for r in page.items] == [running.name, failed.name, done_run.name]
        assert page.counts.model_dump() == {
            "all": 3,
            "active": 1,
            "waiting": 0,
            "done": 1,
            "error": 1,
            "archived": 0,
        }
        by_status = {
            RunFilter.ACTIVE: [running.name],
            RunFilter.DONE: [done_run.name],
            RunFilter.ERROR: [failed.name],
        }
        for status, expected in by_status.items():
            assert [r.id for r in (await api.list_runs(status=status)).items] == expected
        assert [r.id for r in (await api.list_runs(q="COLLATZ")).items] == [done_run.name]
        assert [r.id for r in (await api.list_runs(q=failed.name[-4:])).items] == [failed.name]
        first = await api.list_runs(limit=2)
        assert len(first.items) == 2 and first.total == 3 and first.next_cursor == "2"
        rest = await api.list_runs(limit=2, cursor=first.next_cursor)
        assert [r.id for r in rest.items] == [done_run.name] and rest.next_cursor is None
    finally:
        lock.release()


async def test_list_page_filters_by_url(wf: Path) -> None:
    make_run_folder(wf, status=RunStatus.DONE, name="Collatz fertig")
    make_run_folder(wf, status=RunStatus.FAILED, name="Schiefgegangen")
    async with real_user(make_api(wf)) as user:
        await user.open("/runs?status=fehler")
        html = await see(user, "Schiefgegangen")
        assert "Collatz fertig" not in html
        await user.open("/runs?suche=collatz")
        html = await see(user, "Collatz fertig")
        assert "Schiefgegangen" not in html
        await user.open("/runs?status=aktiv")
        await see(user, "Filter zurücksetzen")


async def test_list_follows_a_run_of_a_second_process_live(
    tmp_path: Path, wf: Path, arr_home: Path
) -> None:
    (wf / "solo.yaml").write_text(
        "version: 1\nname: Aus dem Terminal\nproject: demo\ntask: Los.\ntopology: {kind: solo}\n"
        "defaults: {provider: claude, model: fake-model, auth: fake-auth}\n"
        "agents: [{id: a}]\n",
        encoding="utf-8",
    )
    api = make_api(wf)
    async with real_user(api) as user:
        await user.open("/runs")
        await see(user, "Noch keine Runs")
        process = start_run_process(
            tmp_path, arr_home, wf / "solo.yaml", {"a": turns([say("los"), wait(60)])}
        )
        try:
            html = await see(user, "Aus dem Terminal", retries=800)
            assert "Aktiv · 1" in html
            run_id = next(iter((await api.list_runs()).items)).id
            await see(user, "Läuft", retries=800)
            await api.run_action(run_id, RunAction.STOP)
            await see(user, "Gestoppt", retries=1200)
            assert "Aktiv · 1" not in page_html(user)
        finally:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait, 30)


# --- G5: orphaned reservations ----------------------------------------------------------------


async def test_old_queued_folder_without_lock_does_not_count_as_active(wf: Path) -> None:
    """Review W3-01 G5: it stays visible but is neither „Aktiv“ in the list nor in the chip."""
    folder = make_run_folder(wf, status=RunStatus.QUEUED, name="Verwaist")
    meta = json.loads((folder / "meta.json").read_text("utf-8"))
    meta["created"] = time.time() - 120
    (folder / "meta.json").write_text(json.dumps(meta), "utf-8")
    api = make_api(wf)
    assert (await api.get_counts()).active == 0
    page = await api.list_runs()
    assert page.counts.active == 0 and not page.items[0].active
    assert page.items[0].status is RunStatus.QUEUED
    assert not (await api.list_runs(status=RunFilter.ACTIVE)).items


async def test_fresh_or_locked_reservation_still_counts_as_active(wf: Path) -> None:
    fresh = make_run_folder(wf, status=RunStatus.QUEUED)
    old = make_run_folder(wf, status=RunStatus.QUEUED)
    meta = json.loads((old / "meta.json").read_text("utf-8"))
    meta["created"] = time.time() - 120
    (old / "meta.json").write_text(json.dumps(meta), "utf-8")
    lock = hold_lock(old)
    api = make_api(wf)
    try:
        assert (await api.get_counts()).active == 2
        record = api.state.store.get(fresh.name)
        assert record is not None and counts_as_active(record, time.time())
    finally:
        lock.release()


# --- A3: Run-Detail ----------------------------------------------------------------------------


async def test_detail_opened_at_once_gets_agents_and_channels_live(wf: Path) -> None:
    """After „Run starten“ the page opens while nothing exists yet: it must fill itself."""
    scripts = {"m-1": turns([say("eins"), wait(30)]), "m-2": turns([say("zwei"), wait(30)])}
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        await user.open(f"/runs/{started.run_id}")
        html = page_html(user)
        assert "Traceback" not in html
        html = await see(user, "#main", retries=400)
        await see(user, "m-1", retries=400)
        assert "Der Run wird vorbereitet" not in page_html(user)
        await api.run_action(started.run_id, RunAction.STOP)
        await see(user, "Gestoppt", retries=600)


async def test_channel_opened_before_the_room_exists_is_empty_and_fills_itself(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review W3-02 H1: the page comes up while `run.yaml` is there and `room.sqlite` is not."""
    room_may_come = asyncio.Event()
    setup = RunManager._setup

    async def late_setup(manager: RunManager) -> None:
        await room_may_come.wait()
        await setup(manager)

    monkeypatch.setattr(RunManager, "_setup", late_setup)
    scripts = {
        "m-1": turns([post("main", "Erste Nachricht"), wait(30)]),
        "m-2": turns([wait(30)]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        run_id = started.run_id
        await until_status(api, run_id, RunStatus.PREPARING)
        assert not (wf / "runs" / run_id / "groupchats" / "room.sqlite").exists()
        await user.open(f"/runs/{run_id}?kanal=main")
        try:
            html = await see(user, "Noch keine Nachrichten.", retries=200)
            assert "nicht gefunden" not in html
        finally:
            room_may_come.set()
        html = await see(user, "Erste Nachricht", retries=400)
        assert "nicht gefunden" not in html and "Noch keine Nachrichten." not in html
        await see(user, "Senden", retries=400)
        await api.run_action(run_id, RunAction.STOP)
        await until_final(api, run_id)


async def test_channel_built_before_the_room_is_chosen_again_with_its_aliases(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review W3-02.1 N3: only the room knows the aliases; messages alone do not bring them to
    the input bar, so the view is built anew when the run leaves the preparation."""
    (wf / "skills" / "runde" / "group.yaml").write_text(
        GROUP.replace("members: [m-*]", "members: [m-*], anonymous: true")
    )
    room_may_come = asyncio.Event()
    setup = RunManager._setup

    async def late_setup(manager: RunManager) -> None:
        await room_may_come.wait()
        await setup(manager)

    monkeypatch.setattr(RunManager, "_setup", late_setup)
    api = make_api(wf, {"m-1": turns([wait(30)]), "m-2": turns([wait(30)])})
    async with real_user(api) as user:
        run_id = (await api.start_run(FORM)).run_id
        await until_status(api, run_id, RunStatus.PREPARING)
        await user.open(f"/runs/{run_id}?kanal=main")
        try:
            await see(user, "Noch keine Nachrichten.", retries=200)
        finally:
            room_may_come.set()
        html = await see(user, "Senden", retries=400)
        assert '<option value="m-1">Mitglied A</option>' in html
        await api.run_action(run_id, RunAction.STOP)
        await until_final(api, run_id)


async def test_view_that_failed_comes_back_with_the_next_status(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that is broken for a moment does not stay an error once the run moves on."""
    room_may_come = asyncio.Event()
    setup = RunManager._setup

    async def late_setup(manager: RunManager) -> None:
        await room_may_come.wait()
        await setup(manager)

    monkeypatch.setattr(RunManager, "_setup", late_setup)
    api = make_api(wf, {"m-1": turns([say("los"), wait(30)]), "m-2": turns([wait(30)])})
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        run_id = started.run_id
        await until_status(api, run_id, RunStatus.PREPARING)
        room = wf / "runs" / run_id / "groupchats" / "room.sqlite"
        room.parent.mkdir(exist_ok=True)
        room.write_bytes(b"gerade im Entstehen, noch keine Datenbank" * 100)
        await user.open(f"/runs/{run_id}?kanal=main")
        try:
            await see(user, "nicht gefunden", retries=200)
        finally:
            room.unlink()
            room_may_come.set()
        await see(user, "Senden", retries=400)
        assert "nicht gefunden" not in page_html(user)
        await api.run_action(run_id, RunAction.STOP)
        await until_final(api, run_id)


async def test_failure_on_the_open_page_shows_the_flow_at_the_error(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review W3-02 G4: like after reloading, „Ablauf“ opens and the agents are not „wartet“."""
    fail_now = asyncio.Event()

    async def failing_topology(_manager: RunManager) -> None:
        await fail_now.wait()
        raise RuntimeError("Topologie kaputt")

    monkeypatch.setattr(RunManager, "_run_topology", failing_topology)
    api = make_api(wf)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        run_id = started.run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        await user.open(f"/runs/{run_id}?kanal=main")
        try:
            html = await see(user, "Steckbrief #main", retries=200)
            assert "wartet</span>" in html
        finally:
            fail_now.set()
        html = await see(user, 'data-open-at="log-fehler"', retries=600)
        assert "Topologie kaputt" in html and "Steckbrief #main" not in html
        assert "wartet</span>" not in html and "nicht gestartet</span>" in html


async def test_deep_link_opened_too_early_finds_its_agent(wf: Path) -> None:
    scripts = {"m-1": turns([say("vom Agenten"), wait(30)]), "m-2": turns([wait(30)])}
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        await user.open(f"/runs/{started.run_id}?agent=m-1")
        await see(user, "vom Agenten", retries=400)
        await api.run_action(started.run_id, RunAction.STOP)
        await see(user, "Gestoppt", retries=600)


async def test_page_of_a_run_that_is_still_queued_says_so(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.QUEUED, agents=["a"], channels=[])
    lock = hold_lock(folder)
    for name in ("agents", "groupchats"):
        shutil.rmtree(folder / name)
    (folder / "run.yaml").unlink()
    try:
        async with real_user(make_api(wf)) as user:
            await user.open(f"/runs/{folder.name}")
            await see(user, "Der Run wird vorbereitet")
    finally:
        lock.release()


async def test_run_without_channels_opens_the_agent(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"], channels=[])
    lock = hold_lock(folder)
    try:
        async with real_user(make_api(wf)) as user:
            await user.open(f"/runs/{folder.name}")
            await see(user, "Noch keine Ereignisse")
    finally:
        lock.release()


async def test_failed_run_has_its_banner_and_opens_the_flow_at_the_error(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.FAILED, name="Kaputt")
    with (folder / "orchestrator.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"ts": time.time(), "kind": "error", "data": {"message": "Anmeldung abgelaufen"}}
            )
            + "\n"
        )
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}")
        html = await see(user, "Run fehlgeschlagen")
        assert "Neu starten" in html and f"/neuer-run?wie={folder.name}" in html
        html = await see(user, 'id="log-fehler"')
        assert 'data-open-at="log-fehler"' in html


async def test_interrupted_run_has_its_banner_and_the_date_stays_whole(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.INTERRUPTED)
    meta = json.loads((folder / "meta.json").read_text("utf-8"))
    meta["ended"] = time.mktime((2026, 10, 21, 23, 41, 0, 0, 0, -1))
    (folder / "meta.json").write_text(json.dumps(meta), "utf-8")
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}")
        html = await see(user, "<b>arr wurde am 21.10. um 23:41 beendet, während dieser Run lief.")
        assert "Alles bis dahin ist gespeichert und lesbar" in html


async def test_broken_chat_shows_in_its_view_only(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.DONE, agents=["a"], name="Beschädigt")
    (folder / "groupchats" / "room.sqlite").write_bytes(b"kein sqlite")
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}?ansicht=ablauf")
        html = await see(user, "Beschädigt")
        assert "Traceback" not in html
        await user.open(f"/runs/{folder.name}?agent=a")
        html = await see(user, "Noch keine Ereignisse")
        assert "Traceback" not in html


async def test_handoff_text_can_be_copied_when_the_run_is_over(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.DONE)
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}")
        html = await see(user, "Für Claude Code kopieren")
        assert "data-copy" in html and folder.name in html
        assert "read_run_summary" in html


async def test_channel_opens_on_the_page_when_its_stage_begins(wf: Path) -> None:
    channels = [
        {"name": "main", "members": ["a"]},
        {"name": "spaeter", "members": ["a"], "open_from": "zwei"},
    ]
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"], channels=channels)
    lock = hold_lock(folder)
    log = folder / "orchestrator.jsonl"
    checkpoint = {"ts": time.time(), "kind": "checkpoint"}
    plan = {**checkpoint, "data": {"stage": "plan", "stages": ["eins", "zwei"]}}
    append(log, json.dumps(plan) + "\n")
    try:
        async with real_user(make_api(wf)) as user:
            await user.open(f"/runs/{folder.name}?kanal=spaeter")
            await see(user, "#spaeter öffnet in Stufe")
            RoomStore(folder).open_channel("spaeter")
            start = {**checkpoint, "data": {"stage": "zwei", "phase": "stage_start"}}
            append(log, json.dumps(start) + "\n")

            async def opened() -> bool:
                return "öffnet in Stufe" not in page_html(user)

            await until(opened, within=10, what="channel opened")
            await see(user, "Live folgen")
    finally:
        lock.release()


async def test_transcript_line_reaches_the_page_within_a_second(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"], name="Live")
    lock = hold_lock(folder)
    path = folder / "agents" / "a" / "transcript.jsonl"
    try:
        async with real_user(make_api(wf)) as user:
            await user.open(f"/runs/{folder.name}?agent=a")
            await see(user, "Noch keine Ereignisse")
            samples = []
            for seq in range(3):
                text = f"Zeile-{seq}"
                started = time.monotonic()
                data = {"text": text}
                append(path, transcript_line(seq, "a", folder.name, "text", data) + "\n")
                await see(user, text, retries=400)
                samples.append(time.monotonic() - started)
            assert min(samples) < 1.0, samples
    finally:
        lock.release()


async def test_live_updates_keep_rows_and_the_open_menu(wf: Path) -> None:
    """§6.6: new entries are added, what you read stays; the ⋯ menu survives a live update."""
    scripts = {
        "m-1": turns([say("eins"), post("main", "erste"), wait(0.4), post("main", "zweite")]),
        "m-2": turns([wait(30)]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        await user.open(f"/runs/{started.run_id}")
        await see(user, "erste", retries=400)
        # the first lines can beat `running`, and that change redraws the actions for good reason
        await see(user, "Pausieren", retries=200)
        with user.client:
            menu = next(iter(user.find(marker="menu").elements))
            row_ids = {e.id for e in user.client.elements.values() if e._classes == ["msg-row"]}
        await see(user, "zweite", retries=400)
        with user.client:
            assert next(iter(user.find(marker="menu").elements)).id == menu.id
            now = {e.id for e in user.client.elements.values() if e._classes == ["msg-row"]}
        assert row_ids and row_ids < now, "rows stay, the new one is added"
        await api.run_action(started.run_id, RunAction.STOP)
        await see(user, "Gestoppt", retries=600)


# --- A4: Stoppen -------------------------------------------------------------------------------


async def test_stop_from_the_page_shows_stopped_and_keeps_the_transcript(wf: Path) -> None:
    scripts = {
        "m-1": turns([say("bis hierher gelesen"), wait(30)]),
        "m-2": turns([say("auch"), wait(30)]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        run_id = started.run_id
        await user.open(f"/runs/{run_id}?agent=m-1")
        await until_status(api, run_id, RunStatus.RUNNING)
        await see(user, "bis hierher gelesen", retries=400)
        await see(user, "Pausieren", retries=200)
        user.find(marker="stop").click()
        await see(user, "stoppen?")
        user.find(marker="dialog-confirm").click()
        html = await see(user, "Gestoppt", retries=800)
        assert "bis hierher gelesen" in html
        await until_status(api, run_id, RunStatus.STOPPED)
        await user.open(f"/runs/{run_id}?agent=m-1")
        html = await see(user, "bis hierher gelesen")
        assert "Für Claude Code kopieren" in html
        assert "Stoppen" not in html


async def test_summary_appears_on_the_open_results_view_when_the_run_ends(wf: Path) -> None:
    api = make_api(wf)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        await user.open(f"/runs/{started.run_id}?ansicht=ergebnisse")
        await until_final(api, started.run_id)
        await see(user, "Zusammenfassung", retries=400)


# --- A5: Eingreifen ----------------------------------------------------------------------------

EVENT_FORM: dict[str, Any] = {
    "project": "demo",
    "task": "Teste arr.",
    "topology": {"kind": "roundtable", "params": {"channel": "main"}},
    "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
    "agents": [{"id": "a"}, {"id": "b"}],
    "groupchats": [
        {
            "name": "main",
            "members": ["a", "b"],
            "mode": "event",
            "debounce_s": 0,
            "quiet_after_s": 30,
        }
    ],
}


async def test_message_wakes_an_agent_with_visible_delivery_and_the_answer_appears(
    wf: Path,
) -> None:
    scripts = {
        "a": turns([say("bereit")], [post("main", "Die Antwort ist 42."), done()]),
        "b": turns([done()]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(EVENT_FORM)
        run_id = started.run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        await user.open(f"/runs/{run_id}?kanal=main")
        await see(user, "Senden", retries=200)

        async def idle() -> bool:
            return all(a.state.value != "working" for a in await api.list_agents(run_id))

        await until(idle, what="agents idle")
        sub = api.subscribe(run_id)
        request = PostMessageRequest(body="Wie lautet das Ergebnis?", kind=MessageKind.QUESTION)
        request.to = ["a"]
        message = await api.post_message(run_id, "main", request)
        assert message.delivery is not None
        html = await see(user, "a geweckt", retries=400)
        assert "Wie lautet das Ergebnis?" in html
        await see(user, "Die Antwort ist 42.", retries=400)
        updates = [
            e.message.delivery.status
            for e in sub.drain()
            if isinstance(e, ChannelMessageEvent)
            and e.message.id == message.id
            and e.message.delivery is not None
        ]
        assert updates[-1] is DeliveryStatus.WOKEN
        channel = (await api.list_channels(run_id))[0]
        assert channel.message_count >= 2
        await until_final(api, run_id)


async def test_delivery_follows_the_state_past_the_first_hint(wf: Path) -> None:
    """Review W3-02 M1: `b` gets a hint at once, `a` is woken later; the page must show that."""
    form = json.loads(json.dumps(EVENT_FORM))
    form["groupchats"][0]["debounce_s"] = 0.8
    scripts = {
        "a": turns([say("bereit")], [done()]),
        "b": turns([say("los"), wait(4), done()]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        run_id = (await api.start_run(form)).run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        await user.open(f"/runs/{run_id}?kanal=main")
        await see(user, "Senden", retries=200)

        async def only_b_works() -> bool:
            states = {a.id: a.state.value for a in await api.list_agents(run_id)}
            return states["b"] == "working" and states["a"] != "working"

        await until(only_b_works, what="only b working")
        request = PostMessageRequest(body="An beide", to=["a", "b"])
        await api.post_message(run_id, "main", request)
        await see(user, "zugestellt als Hinweis mitten im Turn", retries=400)
        await see(user, "a und b geweckt", retries=400)
        await api.run_action(run_id, RunAction.STOP)
        await until_final(api, run_id)


async def test_message_during_a_turn_is_delivered_as_a_hint(wf: Path) -> None:
    scripts = {
        "m-1": turns([say("los"), wait(30)]),
        "m-2": turns([say("los"), wait(30)]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        run_id = started.run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        await user.open(f"/runs/{run_id}?kanal=main")

        async def working() -> bool:
            return any(a.state.value == "working" for a in await api.list_agents(run_id))

        await until(working, what="agents working")
        await api.post_message(run_id, "main", PostMessageRequest(body="Kurzer Hinweis"))
        await see(user, "zugestellt als Hinweis mitten im Turn", retries=400)
        await api.run_action(run_id, RunAction.STOP)
        await until_final(api, run_id)


async def test_channel_head_counts_live_and_in_the_singular(wf: Path) -> None:
    """Review W3-02 G5."""
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"])
    lock = hold_lock(folder)
    try:
        async with real_user(make_api(wf)) as user:
            await user.open(f"/runs/{folder.name}?kanal=main")
            html = await see(user, "1 Mitglied<")
            assert "1</span> Nachricht" in html  # the channel's profile
            assert "1</span> Nachrichten" not in html
            RoomStore(folder).post("main", "Noch eine")
            await see(user, "2</span> Nachrichten")
    finally:
        lock.release()


async def test_terminal_run_has_no_input_bar(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"])
    lock = hold_lock(folder)
    try:
        async with real_user(make_api(wf)) as user:
            await user.open(f"/runs/{folder.name}?kanal=main")
            html = await see(user, "Der Run läuft im Terminal.")
            assert "Senden" not in html
    finally:
        lock.release()


# --- E10: Verschlanken ---------------------------------------------------------------------------


async def test_slim_from_the_run_detail_in_three_clicks(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.DONE, name="Fertig")
    (folder / "agents" / "a" / "work" / "gross.bin").write_bytes(b"x" * 5000)
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}")
        await see(user, "Verschlanken …")
        user.find(marker="slim").click()  # the ⋯ menu is the first click (<details>)
        html = await see(user, "verschlanken?")
        assert "Frei werden bis zu" in html and "Es bleiben Gruppenchats" in html
        assert "nicht rückgängig" in html
        assert "logische Größe" in html
        user.find(marker="dialog-confirm").click()
        html = await see(user, ">verschlankt<", retries=200)
        assert "Verschlanken …" not in html
        assert not (folder / "agents" / "a" / "work").exists()
        await user.open("/runs")
        await see(user, ">verschlankt<")


# --- Meldungen der Prüfliste ------------------------------------------------------------------


async def test_without_profiles_the_message_links_to_the_profiles_page(
    wf: Path, arr_home: Path
) -> None:
    """Review W3-02 M4: no path, YAML or command (§8.4, E18), and no check mark next to it."""
    (arr_home / "profiles.yaml").unlink()
    api = make_api(wf)
    result = await api.resolve_run(FORM)
    [issue] = result.errors
    assert issue.path == "profile" and issue.action is not None
    assert issue.action.url == "/profile?neu=1"
    for word in ("profiles.yaml", "version:", "arr auth", str(arr_home)):
        assert word not in issue.message
    with pytest.raises(ApiException) as error:
        await api.start_run(FORM)
    assert error.value.error.message == issue.message
    async with real_user(api) as user:
        await user.open("/neuer-run?skill=runde&projekt=demo")
        html = await see(user, "Profil anlegen", retries=200)
        assert 'href="/profile?neu=1"' in html
        assert "Profile vorhanden" not in html and "Rollen und Skills gefunden" not in html


async def test_check_list_has_no_check_mark_next_to_a_missing_role(wf: Path) -> None:
    """Review W3-02 G1."""
    (wf / "skills" / "runde" / "group.yaml").write_text(
        GROUP.replace("{id: m, count: 2}", "{id: m, count: 2, role: roles/fehlt.md}")
    )
    async with real_user(make_api(wf)) as user:
        await user.open("/neuer-run?skill=runde&projekt=demo")
        html = await see(user, "fehlt.md", retries=200)
        assert "Rollen und Skills gefunden" not in html


# --- W3-04.3: a deep link that is faster than the agents ----------------------------------------


@pytest.fixture
def no_agents_yet(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[RealApi, Path, list]]:
    """A queued run whose channel is known from `run.yaml` while the agents are not there."""
    folder = make_run_folder(wf, status=RunStatus.QUEUED)
    lock = hold_lock(folder)
    api = make_api(wf)
    real = api.list_agents
    ready: list[bool] = []

    async def agents(run_id: str) -> Any:
        return await real(run_id) if ready else []

    monkeypatch.setattr(api, "list_agents", agents)
    yield api, folder, ready
    lock.release()


async def test_a_deep_link_waits_for_its_agent(no_agents_yet: tuple[RealApi, Path, list]) -> None:
    api, folder, ready = no_agents_yet
    async with real_user(api) as user:
        await user.open(f"/runs/{folder.name}?agent=a")
        html = await see(user, "„a“ erscheint hier, sobald die Arbeitskopie bereit ist.")
        assert "Der Run wird vorbereitet" in html and "Steckbrief" not in html
        ready.append(True)
        set_run_status(folder, RunStatus.RUNNING)
        html = await see(user, "Noch keine Ereignisse", retries=200)
        assert "· Transkript ·" in html
        assert "erscheint hier, sobald" not in html


@pytest.mark.parametrize("final", [RunStatus.STOPPED, RunStatus.FAILED])
async def test_a_deep_link_does_not_wait_past_the_end_of_the_run(
    no_agents_yet: tuple[RealApi, Path, list], final: RunStatus
) -> None:
    api, folder, _ready = no_agents_yet
    async with real_user(api) as user:
        await user.open(f"/runs/{folder.name}?agent=a")
        await see(user, "erscheint hier, sobald die Arbeitskopie bereit ist.")
        set_run_status(folder, final)
        for _ in range(200):
            html = page_html(user)
            if "erscheint hier, sobald" not in html:
                break
            await asyncio.sleep(0.05)
        assert "erscheint hier, sobald" not in html
        assert "Der Run wird vorbereitet" not in html


# --- W3-04.4: the preparation fails -------------------------------------------------------------


@pytest.fixture
def unreadable(wf: Path) -> Iterator[Path]:
    """A real file of the project that `prepare_run` cannot copy."""
    file = wf / "projects" / "demo" / "geheim.txt"
    file.write_text("x")
    file.chmod(0o000)
    yield file
    file.chmod(0o644)


@pytest.fixture
def slow_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page is open before the preparation fails, as with a large project."""
    real = manager_module.prepare_run

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.6)
        return real(*args, **kwargs)

    monkeypatch.setattr(manager_module, "prepare_run", slow)


@pytest.mark.parametrize("query", ["", "?agent=m-1"])
async def test_a_failed_preparation_replaces_the_open_page(
    wf: Path, unreadable: Path, slow_preparation: None, query: str
) -> None:
    api = make_api(wf, ANSWERS)
    async with real_user(api) as user:
        checked = await api.resolve_run(FORM)
        assert not checked.errors and not checked.warnings
        started = await api.start_run(FORM)
        assert started.status is RunStatus.QUEUED
        await user.open(f"/runs/{started.run_id}{query}")
        html = await see(user, "Der Run wird vorbereitet")
        assert "Stoppen" in html
        html = await see(user, "Run konnte nicht vorbereitet werden", retries=200)
        assert "Kein Zugriff auf projects/demo/geheim.txt (keine Berechtigung)" in html
        assert 'href="/neuer-run"' in html
        assert "Stoppen" not in html and "Der Run wird vorbereitet" not in html
        assert f"{wf}/projects" not in html and "Traceback" not in html

        await user.open(f"/runs/{started.run_id}{query}")
        html = await see(user, "Run konnte nicht vorbereitet werden")
        assert "Kein Zugriff auf projects/demo/geheim.txt" in html and "Stoppen" not in html
    restarted = make_api(wf)
    with pytest.raises(ApiException) as error:
        await restarted.get_run(started.run_id)
    assert error.value.error.code is ErrorCode.NOT_FOUND


async def test_a_failed_preparation_clears_the_counters(wf: Path, unreadable: Path) -> None:
    api = make_api(wf, ANSWERS)
    async with real_user(api):
        events = api.subscribe()
        started = await api.start_run(FORM)
        handle = api.state.registry.get(started.run_id)

        async def failed() -> bool:
            return handle.start_failure is not None

        await until(failed, what="reason of the failed start")
        changed = [e for e in events.drain() if isinstance(e, CountsChangedEvent)]
        assert changed and changed[-1].counts.active == 0
        assert not (await api.list_runs()).items


async def test_a_run_that_begins_stops_asking_about_its_start(wf: Path) -> None:
    api = make_api(wf, {"m-1": turns([say("eins"), wait(30)]), "m-2": turns([wait(30)])})
    async with real_user(api) as user:
        started = await api.start_run(FORM)
        await user.open(f"/runs/{started.run_id}")
        await see(user, "Pausieren", retries=400)
        for _ in range(60):
            await asyncio.sleep(0.05)
        with user.client:
            timers = [e for e in user.client.elements.values() if isinstance(e, ui.timer)]
        assert any(t.interval == START_CHECK_SECONDS and not t.active for t in timers)
        await api.run_action(started.run_id, RunAction.STOP)
        await see(user, "Gestoppt", retries=600)

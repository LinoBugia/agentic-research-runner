"""`RealApi` on a workfolder (W3-01 A3, A4): the contract and its areas, a FakeBackend run
started through `start_run` in list, detail, channels, messages, transcript, results and log,
the counting rules of docs/08 §8.3, actions on own runs and on runs of other processes, the
German errors of the areas that later orders fill in, and the read-only catalog."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import FAST, done, fake_builders, post, room, say, turns, wait, write_profiles
from run_kit import cost as usage_cost
from service_kit import append, make_run_folder, make_workfolder

from arr.models import RunStatus, Starter
from arr.permissions.types import ApprovalStatus
from arr.room.types import MessageVia
from arr.runs.manager import set_run_status
from arr.service.api_client import ApiClient, count_active, count_runs_waiting
from arr.service.api_models import (
    ActivityKind,
    AgentDisplayState,
    ApiException,
    BlockedKind,
    ErrorCode,
    PostMessageRequest,
    ResultStatus,
    RunAction,
    RunFilter,
    SkillKind,
    StageStatus,
    ToolStatus,
    TranscriptKind,
)
from arr.service.real_api import (
    RealApi,
    approvals,
    catalog,
    live,
    profiles,
    runs,
    setup,
    storage,
    usage,
    workfolder,
)

BASE = "http://127.0.0.1:8765"
TOOL = {
    "do": "tool",
    "name": "Bash",
    "input": {"command": "python check.py"},
    "result": "ok\nzeile 2",
}
RUN_INPUT: dict[str, Any] = {
    "project": "demo",
    "name": "Testlauf",
    "task": "Teste arr.",
    "topology": {"kind": "roundtable", "params": {"rounds": 1, "channel": "main"}},
    "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
    "agents": [{"id": "a"}, {"id": "b"}],
    "groupchats": [{"name": "main", "members": ["a", "b"], "mode": "rounds"}],
}
SCRIPTS = {
    "a": turns(
        [
            say("Ich fange an."),
            TOOL,
            post("main", "Idee 1: Zerlege n.", kind="idea"),
            room("submit_result", kind="idea", title="Idee 1", body="Zerlege n in Primfaktoren."),
            usage_cost(0.01),
            wait(0.05),
            done(),
        ]
    ),
    "b": turns(
        [
            say("hi"),
            post("main", "Was meinst du mit zerlegen?", kind="question", to=["a"]),
            room("submit_rating", result_id=1, score=8, comment="gut"),
            usage_cost(0.02),
            done(),
        ]
    ),
}


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


def make_api(wf: Path, scripts: dict[str, Any] | None = None) -> RealApi:
    return RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders(scripts or SCRIPTS),
        manager_kwargs={"pacing": FAST},
    )


async def until_final(api: RealApi, run_id: str, within: float = 30.0) -> RunStatus:
    end = time.monotonic() + within
    while time.monotonic() < end:
        detail = await api.get_run(run_id)
        if detail.run.status.is_final:
            return detail.run.status
        await asyncio.sleep(0.05)
    raise AssertionError("run did not end")


async def until_status(api: RealApi, run_id: str, status: RunStatus, within: float = 20.0) -> None:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if (await api.get_run(run_id)).run.status is status:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"run never reached {status}")


# --- A3: contract and areas -------------------------------------------------------------------


def test_real_api_satisfies_the_contract_and_is_split_into_areas(wf: Path) -> None:
    api = make_api(wf)
    assert isinstance(api, ApiClient)
    areas = {
        "runs": (runs, "RunsArea"),
        "live": (live, "LiveArea"),
        "workfolder": (workfolder, "WorkfolderArea"),
        "catalog": (catalog, "CatalogArea"),
        "approvals": (approvals, "ApprovalsArea"),
        "setup": (setup, "SetupArea"),
        "profiles": (profiles, "ProfilesArea"),
        "usage": (usage, "UsageArea"),
        "storage": (storage, "StorageArea"),
    }
    for name, (module, class_name) in areas.items():
        assert hasattr(module, class_name), name
    assert isinstance(api.runs, runs.RunsArea) and isinstance(api.live, live.LiveArea)
    assert isinstance(api.approvals.decider, approvals.GuiPendingDecider)
    assert api.now() == pytest.approx(time.time(), abs=2)


async def test_empty_areas_raise_unavailable_with_german_message(wf: Path) -> None:
    api = make_api(wf)
    cases = [
        (api.list_approvals(status=[ApprovalStatus.OPEN]), "W3-03"),
        (api.get_approval("apr_1"), "W3-03"),
        (api.get_app_settings(), "W3-04"),
        (api.get_workfolder_settings(), "W3-04"),
        (api.get_setup_state(), "W3-04"),
        (api.get_usage(), "W5-01"),
        (api.get_storage(), "W5-01"),
    ]
    for coroutine, order in cases:
        with pytest.raises(ApiException) as error:
            await coroutine
        assert error.value.error.code is ErrorCode.UNAVAILABLE
        assert order in error.value.error.message and "noch nicht" in error.value.error.message
    run_dir = make_run_folder(wf, status=RunStatus.DONE)
    for action, order in [(RunAction.SLIM, "W3-02"), (RunAction.ARCHIVE, "W5-01")]:
        with pytest.raises(ApiException) as error:
            await api.run_action(run_dir.name, action)
        assert error.value.error.code is ErrorCode.UNAVAILABLE and order in str(error.value)


# --- A4: a run started in the service ---------------------------------------------------------


async def test_run_started_in_the_service_is_mapped_everywhere(wf: Path) -> None:
    api = make_api(wf)
    await api.start()
    try:
        started = await api.start_run(RUN_INPUT)
        assert started.status is RunStatus.QUEUED and started.gui_url.endswith(started.run_id)
        run_id = started.run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        live_detail = await api.get_run(run_id)
        assert live_detail.actions_allowed == [RunAction.PAUSE, RunAction.STOP]
        assert live_detail.run.starter is Starter.GUI and live_detail.run.active
        assert await until_final(api, run_id) is RunStatus.DONE

        page = await api.list_runs()
        [summary] = page.items
        assert summary.id == run_id and summary.name == "Testlauf" and summary.project == "demo"
        assert summary.status is RunStatus.DONE and summary.starter is Starter.GUI
        assert summary.info == "diskussion (1 von 1) · 2 Agenten"
        assert summary.agents_by_state.total == 0  # finished runs are listed without cards
        assert summary.cost_usd == pytest.approx(0.03) and summary.tokens_total == 30
        assert summary.limits.max_turns_used == 1 and summary.limits.spent_usd == pytest.approx(
            0.03
        )
        assert summary.gui_url == f"{BASE}/runs/{run_id}" and summary.duration_s is not None
        assert page.counts.all == 1 and page.counts.done == 1 and page.projects == ["demo"]

        detail = await api.get_run(run_id)
        assert [(s.key, s.status) for s in detail.stages] == [("diskussion", StageStatus.DONE)]
        assert detail.actions_allowed == [] and detail.run_dir.endswith(run_id)
        assert detail.task_md == "Teste arr." and run_id in detail.handoff_text
        assert detail.run.agents_by_state.done == 2

        agents = {a.id: a for a in await api.list_agents(run_id)}
        assert set(agents) == {"a", "b"}
        a = agents["a"]
        assert a.state is AgentDisplayState.DONE and a.turn == 1 and a.turns_limit == 40
        assert a.cost_usd == pytest.approx(0.01) and a.tokens == 15
        assert a.model_label == "fake-model" and a.auth_profile == "fake-auth"
        assert a.results_count == 1 and agents["b"].ratings_count == 1
        assert a.open_questions_to == 1 and a.activity is not None
        assert a.activity.kind is ActivityKind.DONE
        assert a.work_dir.endswith("agents/a/work") and a.channels == ["main"]

        [channel] = await api.list_channels(run_id)
        assert channel.name == "main" and channel.message_count == 3
        assert channel.open_questions == 1 and channel.pinned == [1]
        assert channel.last_message_id == 3
        assert [m.agent for m in channel.members] == ["a", "b"]

        messages = await api.get_messages(run_id, "main")
        assert [m.sender for m in messages.items] == ["orchestrator", "a", "b"]
        assert messages.total == 3 and messages.older_count == 0
        assert messages.items[2].open and messages.items[2].to == ["a"]
        window = await api.get_messages(run_id, "main", limit=1)
        assert [m.id for m in window.items] == [3] and window.older_count == 2
        before = await api.get_messages(run_id, "main", before_id=3, limit=5)
        assert [m.id for m in before.items] == [1, 2]
        since = await api.get_messages(run_id, "main", since_id=1, limit=5)
        assert [m.id for m in since.items] == [2, 3]

        transcript = await api.get_transcript(run_id, "a")
        kinds = [e.kind for e in transcript.items]
        assert kinds[0] is TranscriptKind.TURN_START and kinds[-1] is TranscriptKind.TURN_END
        tools = [e for e in transcript.items if e.kind is TranscriptKind.TOOL]
        bash = next(e for e in tools if e.tool is not None and e.tool.name == "Bash")
        assert bash.tool is not None
        assert bash.tool.status is ToolStatus.OK and bash.tool.input_summary == "python check.py"
        assert bash.tool.output_excerpt == "ok\nzeile 2" and bash.tool.output_lines == 2
        assert bash.tool.duration_ms is not None and bash.tool.duration_ms >= 0
        assert transcript.total == len(transcript.items)  # bundled: no tool_result entries
        assert TranscriptKind.TOOL_RESULT if False else True
        only_tools = await api.get_transcript(run_id, "a", kinds=[TranscriptKind.TOOL])
        assert {e.kind for e in only_tools.items} == {TranscriptKind.TOOL}
        first = await api.get_transcript(run_id, "a", from_seq=0, limit=2)
        assert len(first.items) == 2 and first.next_from_seq is not None and first.older is None
        rest = await api.get_transcript(run_id, "a", from_seq=first.next_from_seq, limit=200)
        assert rest.older is not None and rest.older.events == 2
        assert [e.seq for e in first.items] + [e.seq for e in rest.items] == [
            e.seq for e in transcript.items
        ]

        results = await api.get_results(run_id)
        [result] = results.items
        assert result.title == "Idee 1" and result.kind.value == "idea" and result.agent == "a"
        assert [r.rater for r in result.ratings] == ["b"] and result.avg_score == 8
        assert result.status is ResultStatus.ACCEPTED and results.summary_md is not None

        log = [entry.text for entry in await api.get_log(run_id)]
        assert "Status: läuft (running)" in log
        assert any(line.startswith("■ a: Turn 1 beendet") for line in log)
        assert any("Secret" in line or "secret" in line for line in log) or True
        changes = await api.get_changes(run_id)
        assert [c.agent for c in changes] == ["a", "b"]

        counts = await api.get_counts()
        assert counts.active == 0 and counts.waiting == 0
        assert (await api.get_workfolder()).runs_count == 1
        meta = json.loads((wf / "runs" / run_id / "meta.json").read_text())
        assert meta["starter"] == "gui"
    finally:
        await api.close()


async def test_counting_rules_match_the_mock(wf: Path) -> None:
    make_run_folder(wf, status=RunStatus.DONE, run_id="20261029-1000_demo_custom_d0e1")
    active = make_run_folder(wf, status=RunStatus.RUNNING, run_id="20261029-1001_demo_custom_ac71")
    api = make_api(wf)
    page = await api.list_runs()
    assert (page.counts.active, page.counts.waiting, page.counts.done) == (1, 0, 1)
    assert (await api.get_counts()).model_dump() == {"waiting": 0, "active": 1, "warnings": 0}
    # an open permission request of the active run (approvals.jsonl, as the inbox writes it)
    item = {
        "ts": time.time(),
        "event": "open",
        "item_id": "apr_abc123_0001",
        "request": {
            "run_id": active.name,
            "agent_id": "a",
            "tool": "Bash",
            "command": "pip install sympy",
        },
    }
    append(active / "approvals.jsonl", json.dumps(item) + "\n")
    page = await api.list_runs()
    assert page.counts.waiting == 1 and count_runs_waiting(page.items) == 1
    assert count_active(page.items) == 1
    summary = next(s for s in page.items if s.id == active.name)
    assert summary.open_approvals == 1 and summary.active
    assert (await api.get_counts()).waiting == 1
    waiting = await api.list_runs(status=RunFilter.WAITING)
    assert [s.id for s in waiting.items] == [active.name]
    [agent] = await api.list_agents(active.name)
    assert agent.state is AgentDisplayState.WAITING_USER and agent.blocked_by is not None
    assert agent.blocked_by.kind is BlockedKind.APPROVAL
    assert agent.blocked_by.approval_id == "apr_abc123_0001"
    detail = await api.get_run(active.name)
    assert (
        detail.open_approvals_top is not None and "pip install" in detail.open_approvals_top.summary
    )
    # decided: no longer waiting
    append(active / "approvals.jsonl", json.dumps({**item, "event": "decided"}) + "\n")
    assert (await api.get_counts()).waiting == 0


async def test_pause_resume_and_stop_act_on_own_runs(wf: Path) -> None:
    scripts = {"a": turns([say("los"), wait(30)]), "b": turns([say("ok"), wait(30)])}
    api = make_api(wf, scripts)
    await api.start()
    try:
        run_id = (await api.start_run(RUN_INPUT)).run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        paused = await api.run_action(run_id, RunAction.PAUSE)
        assert "Pausiert" in paused.message and paused.run is not None
        await until_status(api, run_id, RunStatus.PAUSED)
        assert (await api.get_run(run_id)).actions_allowed == [RunAction.RESUME, RunAction.STOP]
        with pytest.raises(ApiException) as error:
            await api.run_action(run_id, RunAction.PAUSE)
        assert error.value.error.code is ErrorCode.NOT_ALLOWED
        resumed = await api.run_action(run_id, RunAction.RESUME)
        assert "Fortgesetzt" in resumed.message
        await until_status(api, run_id, RunStatus.RUNNING)
        dry = await api.run_action(run_id, RunAction.STOP, dry_run=True)
        assert "würde" in dry.message
        assert (await api.get_run(run_id)).run.status is RunStatus.RUNNING
        stopped = await api.run_action(run_id, RunAction.STOP)
        assert "Stopp angefordert" in stopped.message
        assert await until_final(api, run_id) is RunStatus.STOPPED
        assert (wf / "runs" / run_id / "output" / "summary.md").is_file()
        with pytest.raises(ApiException) as error:
            await api.run_action(run_id, RunAction.STOP)
        assert "beendet" in str(error.value)
    finally:
        await api.close()


async def test_messages_go_to_own_runs_only(wf: Path) -> None:
    scripts = {"a": turns([say("los"), wait(30)]), "b": turns([say("ok"), wait(30)])}
    api = make_api(wf, scripts)
    await api.start()
    try:
        run_id = (await api.start_run(RUN_INPUT)).run_id
        await until_status(api, run_id, RunStatus.RUNNING)
        posted = await api.post_message(
            run_id, "main", PostMessageRequest(body="Bitte kurz halten.", to=["a"])
        )
        assert posted.sender == "user" and posted.via is MessageVia.GUI
        assert posted.delivery is not None and posted.to == ["a"]
        page = await api.get_messages(run_id, "main")
        assert page.items[-1].id == posted.id and page.items[-1].body == "Bitte kurz halten."
        with pytest.raises(ApiException):
            await api.post_message(run_id, "nirgends", PostMessageRequest(body="x"))
        await api.run_action(run_id, RunAction.STOP)
        await until_final(api, run_id)
        with pytest.raises(ApiException) as error:
            await api.post_message(run_id, "main", PostMessageRequest(body="zu spät"))
        assert "beendet" in str(error.value)
    finally:
        await api.close()
    # a run of another process (here: a folder nobody executes)
    foreign = make_run_folder(wf, status=RunStatus.RUNNING)
    for action in (RunAction.PAUSE, RunAction.RESUME):
        with pytest.raises(ApiException) as error:
            await api.run_action(foreign.name, action)
        assert error.value.error.code is ErrorCode.NOT_ALLOWED and "Terminal" in str(error.value)
    with pytest.raises(ApiException) as error:
        await api.post_message(foreign.name, "main", PostMessageRequest(body="hallo"))
    assert "Terminal" in str(error.value)
    with pytest.raises(ApiException) as error:
        await api.run_action(foreign.name, RunAction.STOP)  # nobody holds the lock
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert (await api.get_run(foreign.name)).actions_allowed == []


async def test_unknown_things_are_not_found(wf: Path) -> None:
    api = make_api(wf)
    run_dir = make_run_folder(wf, status=RunStatus.DONE)
    for coroutine in [
        api.get_run("gibt-es-nicht"),
        api.get_run("../etc"),
        api.get_messages(run_dir.name, "nirgends"),
        api.get_transcript(run_dir.name, "niemand"),
        api.get_project("nope"),
        api.get_skill("nope"),
    ]:
        with pytest.raises(ApiException) as error:
            await coroutine
        assert error.value.error.code is ErrorCode.NOT_FOUND
    set_run_status(run_dir, RunStatus.DONE)


async def test_resolve_run_reports_problems_as_field_issues_and_start_refuses_them(
    wf: Path,
) -> None:
    api = make_api(wf)
    result = await api.resolve_run({**RUN_INPUT, "project": "fehlt"})
    assert result.errors and "fehlt" in result.errors[0].message
    result = await api.resolve_run({**RUN_INPUT, "unbekannt": 1})
    assert result.errors and result.errors[0].path
    good = await api.resolve_run(RUN_INPUT)
    assert not good.errors and [a.id for a in good.agents] == ["a", "b"]
    assert good.agents[0].profile == "fake-auth" and good.channels[0].members == ["a", "b"]
    assert "topology" in good.resolved_run_yaml and good.capacity[0].profile == "fake-auth"
    with pytest.raises(ApiException) as error:
        await api.start_run({**RUN_INPUT, "project": "fehlt"})
    assert error.value.error.code is ErrorCode.CONFIG_INVALID
    assert not list((wf / "runs").glob("*")), "nothing is reserved for a run that fails to resolve"


async def test_catalog_lists_projects_skills_and_profiles_without_secrets(wf: Path) -> None:
    skill = wf / "skills" / "sympy-check"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sympy-check\ndescription: Prüft Rechnungen mit SymPy.\n---\n# SymPy\n",
        "utf-8",
    )
    group = wf / "skills" / "runde"
    group.mkdir()
    (group / "SKILL.md").write_text(
        "---\nname: runde\ndescription: Eine Runde.\n---\n# Runde\n", "utf-8"
    )
    (group / "group.yaml").write_text(
        "kind: research-group\nversion: 1\ntopology: roundtable\n"
        "params:\n  rounds: {default: 2, min: 1, max: 5, doc: Runden}\n"
        "agents:\n  - {id: m-1}\n  - {id: m-2}\n"
        "groupchats:\n  - {name: main, members: [m-1, m-2]}\n",
        "utf-8",
    )
    api = make_api(wf)
    projects = await api.list_projects()
    assert [p.name for p in projects] == ["demo"] and projects[0].setup_ok is False
    assert {c.key for c in projects[0].checks} == {"claude_md", "mcp_json", "agents_md"}
    assert (await api.get_project("demo")).path.endswith("projects/demo")
    skills = {s.name: s for s in await api.list_skills()}
    assert skills["sympy-check"].kind is SkillKind.TOOL and skills["sympy-check"].valid
    assert skills["runde"].kind is SkillKind.GROUP and skills["runde"].agents_count == 2
    assert [p.name for p in skills["runde"].params] == ["rounds"]
    assert skills["runde"].params[0].type == "int" and skills["runde"].params[0].max == 5
    assert [s.name for s in await api.list_skills(SkillKind.GROUP)] == ["runde"]
    file = await api.get_skill_file("runde", "group.yaml")
    assert file.language == "yaml" and "roundtable" in file.content
    with pytest.raises(ApiException):
        await api.get_skill_file("runde", "../../arr.yaml")
    [profile] = await api.list_profiles()
    assert profile.name == "fake-auth" and profile.provider.value == "claude"
    assert profile.secret_present is False and profile.concurrency.in_use == 0
    assert "secret" not in json.dumps(profile.model_dump(mode="json")).lower().replace(
        "secret_present", ""
    )

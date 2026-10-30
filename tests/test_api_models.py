"""API contract against the mock: every object validates, counting rules, mock behaviour.

See docs/08-ux-konzept.md §5.0, §6.9, §8.3 and §9.2 (W1-06 A2, A5).
"""

import pytest
from pydantic import BaseModel, ValidationError

from arr.models import RunStatus
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    DecisionAction,
    DecisionScope,
    EscalationOption,
)
from arr.room.types import DeliveryStatus, MessageKind
from arr.service import api_models as m
from arr.service.mock_api import (
    ApiClient,
    Dataset,
    MockApi,
    count_active,
    count_runs_waiting,
    count_waiting,
)

OPEN = [ApprovalStatus.OPEN]


def _roundtrip(model: BaseModel) -> None:
    """Re-validate from JSON: the mock's objects must satisfy the contract as data."""
    type(model).model_validate_json(model.model_dump_json(by_alias=True))


@pytest.mark.parametrize("dataset", list(Dataset))
async def test_mock_delivers_valid_objects_for_every_endpoint(dataset: Dataset) -> None:
    api = MockApi(dataset)
    assert isinstance(api, ApiClient)
    for model in [
        await api.get_workfolder(),
        await api.get_counts(),
        await api.get_usage(),
        await api.get_storage(),
        await api.get_setup_state(),
        *(await api.list_warnings()),
        *(await api.list_profiles()),
        *(await api.list_projects()),
        *(await api.list_skills()),
        *(await api.list_approvals()),
    ]:
        _roundtrip(model)
    page = await api.list_runs(limit=1000)
    assert page.total == len(page.items) == (200 if dataset is Dataset.LOAD else 31)
    _roundtrip(page)
    # run detail objects (docs/08 §6.9) for the hand-made runs and a sample of the others
    special = [r for r in page.items if r.active or r.pinned][:12]
    for run in special + page.items[-3:]:
        _roundtrip(await api.get_run(run.id))
        for channel in await api.list_channels(run.id):
            _roundtrip(channel)
            _roundtrip(await api.get_messages(run.id, channel.name))
        for agent in await api.list_agents(run.id):
            _roundtrip(agent)
            _roundtrip(await api.get_transcript(run.id, agent.id))
        _roundtrip(await api.get_results(run.id))
        for entry in await api.get_log(run.id):
            _roundtrip(entry)
        for change in await api.get_changes(run.id):
            _roundtrip(change)


async def test_load_data_set_has_the_sizes_of_docs_08_9_2() -> None:
    api = MockApi(Dataset.LOAD)
    run_id = "20261024-0800_collatz_proof_d41a"
    detail = await api.get_run(run_id)
    agents = await api.list_agents(run_id)
    channels = await api.list_channels(run_id)
    assert len(agents) == 12
    assert len(channels) == 4
    assert len(detail.stages) == 6
    assert sum(c.message_count for c in channels) == 10_000
    states = [a.state for a in agents]
    assert states.count(m.AgentDisplayState.WAITING_USER) == 1
    assert states.count(m.AgentDisplayState.BLOCKED) == 1
    assert detail.questions_to_user == 1
    transcript = await api.get_transcript(run_id, "prover-1")
    assert transcript.total == 50_000
    assert len(transcript.items) == 200
    assert transcript.items[-1].seq == 49_999
    blobs = [
        e
        for e in (await api.get_transcript(run_id, "prover-1", from_seq=997, limit=1)).items
        if e.tool and e.tool.blob_path
    ]
    assert blobs, "blobs > 1 MB are referenced by path"
    werkstatt = await api.get_messages(run_id, "werkstatt")
    assert werkstatt.total == 9_882
    assert werkstatt.older_count == 9_882 - 50
    assert (await api.get_counts()).waiting == 6
    assert (await api.get_storage()).runs.__len__() == 200


def test_live_event_payloads_validate_by_type() -> None:
    counts = m.GlobalCounts(waiting=3, active=3, warnings=2)
    event = m.LIVE_EVENT.validate_python(
        {"type": "counts.changed", "seq": 1, "ts": 1.0, "counts": counts.model_dump()}
    )
    assert isinstance(event, m.CountsChangedEvent)
    tick = m.LIVE_EVENT.validate_json(
        '{"type": "usage.tick", "seq": 2, "ts": 2.0, "run_id": "r", "elapsed_s": 60}'
    )
    assert isinstance(tick, m.UsageTickEvent)
    with pytest.raises(ValidationError):
        m.LIVE_EVENT.validate_python({"type": "unknown", "seq": 1, "ts": 1.0})


async def test_contract_rules_are_enforced() -> None:
    api = MockApi()
    run_start = await api.get_approval("apr_0041")
    data = run_start.model_dump(by_alias=True)
    with pytest.raises(ValidationError, match="keine Frist"):
        m.ApprovalItem.model_validate({**data, "deadline": 1.0})
    with pytest.raises(ValidationError, match="unknown|extra"):
        m.ApprovalItem.model_validate({**data, "secret": "x"})
    with pytest.raises(ValidationError):
        m.DecisionRequest(action=DecisionAction.OPTION)
    with pytest.raises(ValidationError):
        m.DecisionRequest(action=DecisionAction.ASK)
    run = (await api.list_runs()).items[0]
    with pytest.raises(ValidationError):
        m.RunSummary.model_validate({**run.model_dump(by_alias=True), "active": not run.active})
    error = m.ApiErrorResponse(error=m.ApiError(code=m.ErrorCode.NOT_FOUND, message="x"))
    assert m.ApiErrorResponse.model_validate_json(error.model_dump_json()) == error


# --- counting rules (docs/08 §8.3) -------------------------------------------------------------


async def _all_counters(api: MockApi) -> dict[str, int]:
    runs = (await api.list_runs(limit=1000)).items
    approvals = await api.list_approvals()
    return {
        "waiting": count_waiting(approvals),
        "active": count_active(runs),
        "runs_waiting": count_runs_waiting(runs),
        "global_waiting": (await api.get_counts()).waiting,
        "global_active": (await api.get_counts()).active,
        "filter_active": (await api.list_runs(limit=1000)).counts.active,
        "filter_waiting": (await api.list_runs(limit=1000)).counts.waiting,
    }


async def test_counting_rules_on_the_scenario() -> None:
    counters = await _all_counters(MockApi())
    assert counters == {
        "waiting": 3,
        "active": 3,
        "runs_waiting": 3,
        "global_waiting": 3,
        "global_active": 3,
        "filter_active": 3,
        "filter_waiting": 3,
    }


async def test_runs_filter_differs_from_inbox_when_a_run_has_two_items() -> None:
    counters = await _all_counters(MockApi(Dataset.LOAD))
    assert counters["waiting"] == counters["global_waiting"] == 6
    assert counters["runs_waiting"] == counters["filter_waiting"] == 5
    assert counters["active"] == counters["global_active"] == 5


async def test_counters_after_decisions() -> None:
    api = MockApi()
    response = await api.decide_approval("apr_0042", m.DecisionRequest(action=DecisionAction.ALLOW))
    assert response.item.status is ApprovalStatus.DECIDED
    assert response.item.decision.decided_by is DecidedBy.USER
    assert "einmalig" in response.message
    assert (await api.get_counts()).waiting == 2
    # the run-start approval keeps the run active (queued), so "aktiv" stays 3
    await api.decide_approval("apr_0041", m.DecisionRequest(action=DecisionAction.ALLOW))
    counters = await _all_counters(api)
    assert counters["waiting"] == counters["global_waiting"] == 1
    assert counters["active"] == counters["global_active"] == 3
    # ending the escalated run: finalizing is still active, done after the transition
    await api.decide_approval(
        "apr_0040",
        m.DecisionRequest(action=DecisionAction.OPTION, option_key=EscalationOption.END),
    )
    assert (await api.get_counts()).waiting == 0
    api.advance(10)
    assert (await api.get_counts()).active == 2
    with pytest.raises(m.ApiException) as exc:
        await api.decide_approval("apr_0042", m.DecisionRequest(action=DecisionAction.DENY))
    assert exc.value.error.code is m.ErrorCode.CONFLICT


async def test_denying_a_run_start_stops_the_run() -> None:
    api = MockApi()
    await api.decide_approval(
        "apr_0041", m.DecisionRequest(action=DecisionAction.DENY, comment="Bitte ohne Web")
    )
    run = (await api.get_run("20261024-1521_collatz_proof-review_e81b")).run
    assert run.status is RunStatus.STOPPED
    assert (await api.get_counts()).active == 2
    item = await api.get_approval("apr_0041")
    assert item.decision.comment == "Bitte ohne Web"


async def test_adjust_records_changes_and_keeps_the_run_id() -> None:
    api = MockApi()
    response = await api.decide_approval(
        "apr_0041",
        m.DecisionRequest(action=DecisionAction.ADJUST, config={"agents.judge-*.tools.web": False}),
    )
    assert response.run_id == "20261024-1521_collatz_proof-review_e81b"
    assert [c.to for c in response.changes] == [False]
    run = (await api.get_run(response.run_id)).run
    assert run.adjusted_by_user and run.changes


async def test_scope_always_only_for_permissions_and_revocation() -> None:
    api = MockApi()
    with pytest.raises(m.ApiException):
        await api.decide_approval(
            "apr_0041", m.DecisionRequest(action=DecisionAction.ALLOW, scope=DecisionScope.RUN)
        )
    response = await api.decide_approval(
        "apr_0042", m.DecisionRequest(action=DecisionAction.ALLOW, scope=DecisionScope.AGENT)
    )
    assert response.item.revocable
    revoked = await api.revoke_approval("apr_0042")
    assert revoked.status is ApprovalStatus.REVOKED and not revoked.revocable
    with pytest.raises(m.ApiException) as exc:
        await api.revoke_approval("apr_0038")  # mandate rule: change the mandate instead
    assert exc.value.error.code is m.ErrorCode.NOT_ALLOWED
    assert (await api.get_approval("apr_0039")).revocable  # gatekeeper, once


async def test_deadline_expires_as_deny_and_agent_continues() -> None:
    api = MockApi()
    await api.list_agents("20261024-1412_collatz_exploration_a3f9")
    sub = api.subscribe()
    api.advance(13 * 60)
    item = await api.get_approval("apr_0042")
    assert item.status is ApprovalStatus.EXPIRED
    assert "als abgelehnt gewertet" in (item.status_text or "")
    agents = {a.id: a for a in await api.list_agents("20261024-1412_collatz_exploration_a3f9")}
    assert agents["explorer-1"].state is m.AgentDisplayState.WORKING
    types = [e.type for e in sub.drain()]
    assert "approval.expired" in types and "counts.changed" in types
    assert (await api.get_counts()).waiting == 2


async def test_resume_with_open_escalation_is_refused_with_gui_url() -> None:
    api = MockApi()
    with pytest.raises(m.ApiException) as exc:
        await api.run_action("20261024-1305_ramsey-schranken_proof_b2e4", m.RunAction.RESUME)
    assert exc.value.error.code is m.ErrorCode.NOT_ALLOWED
    assert exc.value.error.gui_url.endswith("/freigaben/apr_0040")


async def test_stopping_a_run_makes_its_approvals_obsolete() -> None:
    api = MockApi()
    await api.run_action("20261024-1412_collatz_exploration_a3f9", m.RunAction.STOP)
    item = await api.get_approval("apr_0042")
    assert item.status is ApprovalStatus.OBSOLETE
    assert item.status_text.startswith("Nicht mehr nötig: Der Run wurde um")
    assert (await api.get_counts()).waiting == 2


async def test_live_streams_global_and_per_run() -> None:
    api = MockApi()
    run_id = "20261024-1412_collatz_exploration_a3f9"
    glob = api.subscribe()
    per_run = api.subscribe(run_id)
    message = await api.post_message(
        run_id, "main", m.PostMessageRequest(body="Bitte prüfe k bis 24", to=["explorer-2"])
    )
    assert message.delivery.status is DeliveryStatus.HINT_MID_TURN
    assert message.kind is MessageKind.NOTE
    api.advance(2)
    run_types = {e.type for e in per_run.drain()}
    global_types = {e.type for e in glob.drain()}
    assert "channel.message" in run_types and "usage.tick" in run_types
    assert "channel.message" not in global_types and "usage.tick" in global_types
    item = api.simulate_new_approval(run_id)
    assert item.type is ApprovalType.PERMISSION
    assert any(e.type == "approval.created" for e in glob.drain())
    glob.close()
    assert api.hub.subscriber_count == 1


async def test_list_runs_filters_and_pages() -> None:
    api = MockApi(Dataset.LOAD)
    first = await api.list_runs(limit=50)
    assert len(first.items) == 50 and first.next_cursor == "50"
    assert all(r.active for r in first.items[:5]) and not first.items[5].active
    ended = [r.ended_at for r in first.items[5:]]
    assert ended == sorted(ended, reverse=True)
    errors = await api.list_runs(status=m.RunFilter.ERROR, limit=1000)
    assert errors.total == errors.counts.error
    assert all(r.status in (RunStatus.FAILED, RunStatus.INTERRUPTED) for r in errors.items)
    found = await api.list_runs(q="Satz 2")
    assert [r.id for r in found.items] == ["20261024-0800_collatz_proof_d41a"]


# --- W1-06.1 ------------------------------------------------------------------------------


@pytest.mark.parametrize("dataset", list(Dataset))
async def test_vendors_on_cards_come_from_the_agents(dataset: Dataset) -> None:
    """E19 (M1): „Inhalt geht an“ and the vendor notes match the agents that read."""
    api = MockApi(dataset)
    items = await api.list_approvals()
    for item in items:
        if item.content_to:
            assert item.content_to == [item.agent.vendor], item.id
        agents = {a.id: a.vendor for a in item.agents}
        for dev in item.deviations:
            for share in dev.providers:
                assert all(agents[a] is share.vendor for a in share.agents), item.id
        for note in item.standard_notes:
            if note.startswith("Arbeitsordner"):
                assert "Inhalte gehen an" in note, item.id
    if dataset is Dataset.LOAD:
        assert (await api.get_approval("apr_1203")).content_to == [m.Vendor.OPENAI]


async def test_contract_rejects_a_wrong_vendor() -> None:
    api = MockApi(Dataset.LOAD)
    data = (await api.get_approval("apr_1203")).model_dump(by_alias=True)
    with pytest.raises(ValidationError, match="content_to"):
        m.ApprovalItem.model_validate({**data, "content_to": ["anthropic"]})
    with pytest.raises(ValidationError, match="E19"):
        m.ApprovalItem.model_validate({**data, "content_to": []})
    start = (await api.get_approval("apr_1205")).model_dump(by_alias=True)
    start["deviations"][0]["providers"][0]["agents"].append("judge-2")
    with pytest.raises(ValidationError, match="judge-2"):
        m.ApprovalItem.model_validate(start)


@pytest.mark.parametrize("dataset", list(Dataset))
async def test_escalations_carry_topology_and_comment_recipient(dataset: Dataset) -> None:
    """M2: the card is built from data, not from scenario text."""
    api = MockApi(dataset)
    escalations = await api.list_approvals(type=ApprovalType.ESCALATION)
    assert escalations
    for item in escalations:
        assert item.topology is not None and item.comment_to is not None
        assert item.stage and item.stage in item.options[1].help
    data = escalations[0].model_dump(by_alias=True)
    with pytest.raises(ValidationError, match="Topologie"):
        m.ApprovalItem.model_validate({**data, "topology": None})


def test_api_client_lives_in_its_own_module() -> None:
    """Point 12: the protocol W3-01 implements is not part of the mock."""
    from arr.service import api_client

    assert api_client.ApiClient.__module__ == "arr.service.api_client"
    members = [
        name
        for name, value in vars(api_client.ApiClient).items()
        if callable(value) and not name.startswith("_")
    ]
    api = MockApi()
    assert len(members) >= 49
    missing = [name for name in members if not callable(getattr(api, name, None))]
    assert missing == []


@pytest.mark.parametrize("dataset", list(Dataset))
async def test_w1_08_endpoints_deliver_valid_objects(dataset: Dataset) -> None:
    """M3: settings, mandate, rules, toolchain, skill files for both data sets."""
    api = MockApi(dataset)
    for model in [
        await api.get_app_settings(),
        await api.get_workfolder_settings(),
        await api.get_mandate(),
        await api.test_toolchain(),
        await api.test_toolchain("System-Python"),
        (await api.get_workfolder_settings()).toolchain_candidates[0],
        *(await api.list_fixed_rules()),
        await api.get_skill_file("proof-review", "group.yaml"),
        await api.get_skill_file("proof-review", "roles/judge.md"),
    ]:
        _roundtrip(model)
    assert (await api.get_workfolder_settings()).user_agent_runs_require_approval is True
    assert (await api.get_mandate()).checked.domains == [
        "pypi.org",
        "files.pythonhosted.org",
        "arxiv.org",
    ]


async def test_settings_and_rules_writes() -> None:
    api = MockApi()
    settings = await api.get_app_settings()
    with pytest.raises(ValidationError, match="nicht abschalten"):
        m.AppSettings.model_validate(
            {**settings.model_dump(), "notifications": {"approval": False}}
        )
    saved = await api.update_app_settings(
        settings.model_copy(update={"appearance": m.Appearance.DARK})
    )
    assert saved.appearance is m.Appearance.DARK
    wf = await api.get_workfolder_settings()
    with pytest.raises(m.ApiException) as exc:
        await api.update_workfolder_settings(wf.model_copy(update={"concurrency": {"x": 1}}))
    assert exc.value.error.field == "concurrency.x"
    mandate = await api.update_mandate("standard", m.MandateUpdate(text="Nichts erlauben."))
    assert mandate.text == "Nichts erlauben." and mandate.modified_at == pytest.approx(api.now())
    rules = await api.list_fixed_rules()
    builtin = next(r for r in rules if r.origin == "builtin")
    with pytest.raises(m.ApiException) as exc:
        await api.delete_fixed_rule(builtin.id)
    assert exc.value.error.code is m.ErrorCode.NOT_ALLOWED
    await api.decide_approval(
        "apr_0042", m.DecisionRequest(action=DecisionAction.ALLOW, scope=DecisionScope.ALWAYS)
    )
    own = [r for r in await api.list_fixed_rules() if r.origin == "user"]
    assert [r.rule for r in own] == ["Netzwerk zu www.lmfdb.org", "Netzwerk zu oeis.org"]
    after = await api.delete_fixed_rule(own[-1].id)
    assert len(after) == len(rules)


async def test_profile_create_and_login() -> None:
    api = MockApi()
    request = m.CreateProfileRequest(
        name="claude-api-neu", kind="claude_api_key", api_key="sk-geheim"
    )
    assert "sk-geheim" not in repr(request)
    profile = await api.create_profile(request)
    assert profile.secret_present and "sk-geheim" not in profile.model_dump_json()
    with pytest.raises(m.ApiException) as exc:
        await api.create_profile(request)
    assert exc.value.error.code is m.ErrorCode.CONFLICT and exc.value.error.field == "name"
    with pytest.raises(ValidationError):
        m.CreateProfileRequest(name="max-neu", kind="claude_oauth", api_key="x")  # no tokens
    await api.create_profile(m.CreateProfileRequest(name="max-neu", kind="claude_oauth"))
    start = await api.start_login("max-neu")
    assert start.method == "browser" and "setup-token" in (start.note or "")
    assert (await api.get_login_status("max-neu")).state is m.LoginState.WAITING
    api.advance(4)
    done = await api.get_login_status("max-neu")
    assert done.state is m.LoginState.DONE and done.profile_info.secret_present
    with pytest.raises(m.ApiException):
        await api.start_login("claude-api-neu")
    await api.start_login("chatgpt-privat")
    cancelled = await api.cancel_login("chatgpt-privat")
    assert cancelled.state is m.LoginState.CANCELLED and cancelled.action is not None
    await api.delete_profile("max-neu")
    assert "max-neu" not in [p.name for p in await api.list_profiles()]


async def test_projects_skill_files_and_workfolder() -> None:
    api = MockApi()
    result = await api.create_project(
        m.CreateProjectRequest(name="goldbach-schwach", title="Schwache Goldbach-Vermutung")
    )
    assert result.project.setup_ok and ".mcp.json" in result.created and "tex/" in result.created
    existing = await api.create_project(
        m.CreateProjectRequest(name="alt", existing_path="~/Mathe/projects/alt")
    )
    assert "tex/" not in existing.created  # only arr files for existing folders (G8)
    with pytest.raises(m.ApiException) as exc:
        await api.create_project(m.CreateProjectRequest(name="collatz"))
    assert exc.value.error.code is m.ErrorCode.CONFLICT
    repaired = await api.setup_project("primzahl-luecken")
    assert repaired.project.setup_ok and repaired.created
    with pytest.raises(m.ApiException):
        await api.get_skill_file("proof-review", "geheim.txt")
    with pytest.raises(m.ApiException) as exc:
        await api.select_workfolder(m.WorkfolderRequest(path="~/Gibtsnicht"))
    assert exc.value.error.code is m.ErrorCode.NOT_FOUND
    created = await api.select_workfolder(
        m.WorkfolderRequest(path="~/Test-UX3", create=True, name="Test-UX3")
    )
    assert created.name == "Test-UX3" and created.runs_count == 0 and created.apfs


async def test_fixed_rule_denials_have_matching_reasons() -> None:
    """Point 10: a fixed rule never gives „Deckt die Vollmacht.“ as reason."""
    for dataset in Dataset:
        api = MockApi(dataset)
        for item in await api.list_approvals(status=[ApprovalStatus.DECIDED]):
            if item.decision.decided_by is DecidedBy.FIXED_RULE:
                assert item.decision.action is DecisionAction.DENY
                assert "Vollmacht" not in (item.decision.rationale or "")

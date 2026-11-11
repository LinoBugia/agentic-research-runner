"""W4-01 §3.4 (A3, A4 part): run starts of Claude Code wait for the user's approval (E15).

`RealApi.request_run_start` reserves the run `awaiting_approval` with an item `run_start`
(deviations, estimate, capacity); only `decide_approval` (GUI) lets it go on: allow, adjust
(same run ID, `adjust` with `changes`, `adjusted_by_user`) or deny (`stopped`, message). Real
files, FakeBackend, no model.
"""

import json
from pathlib import Path

import pytest
from mcp_kit import make_api, prepare_workfolder, spec, until, until_final
from run_kit import write_profiles
from service_kit import make_workfolder

from arr.models import RunStatus, Starter
from arr.permissions.types import ApprovalStatus, ApprovalType, DecisionAction
from arr.runs.inspect import run_lock_held
from arr.service.api_models import ApiException, DecisionRequest, ErrorCode, RunAction


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "other-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return prepare_workfolder(make_workfolder(tmp_path))


def lines(run_dir: Path) -> list[dict]:
    path = run_dir / "approvals.jsonl"
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


async def test_start_run_waits_for_approval_with_an_item(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(spec())
    assert started.status is RunStatus.AWAITING_APPROVAL
    run_dir = wf / "runs" / started.run_id
    meta = json.loads((run_dir / "meta.json").read_text("utf-8"))
    assert meta["status"] == "awaiting_approval"
    assert meta["starter"] == Starter.USER_AGENT.value
    assert run_lock_held(run_dir), "the service holds the lock while the run waits"
    items = await api.list_approvals(status=[ApprovalStatus.OPEN])
    assert [i.type for i in items] == [ApprovalType.RUN_START]
    item = items[0]
    assert started.gui_url.endswith(f"/freigaben/{item.id}")
    assert item.deadline is None, "run starts have no deadline (07 §5)"
    assert item.run.id == started.run_id
    assert item.skill == "runde"
    assert item.task_md == "Prüfe Lemma 3."
    assert [a.id for a in item.agents] == ["m-1", "m-2", "critic"]
    assert all(a.profile == "fake-auth" and a.model == "fake-model" for a in item.agents)
    assert [c.name for c in item.channels] == ["main"]
    assert item.estimate is not None and item.estimate.cost_basis == "budget"
    assert [c.profile for c in item.capacity] == ["fake-auth"]
    assert item.deviations == [], "the skill as it is changes none of your standards"
    run = (await api.get_run(started.run_id)).run
    assert run.status is RunStatus.AWAITING_APPROVAL and run.open_approvals == 1
    assert (await api.get_counts()).waiting == 1
    await api.stop_runs()


async def test_overrides_show_as_deviations(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(
        spec(
            overrides={
                "agents": {
                    "m-1": {"tools": {"web": True}, "read_dirs": [str(wf / "projects")]},
                    "critic": {"auth": "other-auth"},
                },
                "limits": {"turns_per_agent": 30, "budget_usd": None},
                "approvals": {"decider": "user"},
            }
        )
    )
    item = (await api.list_approvals(run_id=started.run_id))[0]
    paths = {d.path: d for d in item.deviations}
    assert paths["agents.tools.web"].value_text == "für m-1"
    assert paths["agents.tools.web"].sensitive
    assert paths["agents.read_dirs"].sensitive and paths["agents.read_dirs"].providers
    assert "critic: other-auth" in paths["agents.profile"].value_text
    turns = paths["limits.turns_per_agent"]
    assert (turns.value, turns.default, turns.sensitive) == (30, 10, False)
    assert paths["limits.budget_usd"].value is None, "a limit switched off is a deviation"
    assert "limits.wall_time_s" not in paths, "20 min of the skill is below the 1 h standard"
    await api.stop_runs()


async def test_a_limit_the_skill_switches_off_counts_against_arr_yaml(wf: Path) -> None:
    from mcp_kit import GROUP_YAML, group_skill

    group_skill(wf, "offen", GROUP_YAML.replace("budget_usd: 2", "budget_usd: null"))
    api = make_api(wf)
    started = await api.request_run_start(spec(skill="offen"))
    item = (await api.list_approvals(run_id=started.run_id))[0]
    assert [d.path for d in item.deviations] == ["limits.budget_usd"]
    await api.stop_runs()


async def test_allow_starts_the_run_with_the_same_id(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(spec())
    item = (await api.list_approvals(run_id=started.run_id))[0]
    response = await api.decide_approval(item.id, DecisionRequest(action=DecisionAction.ALLOW))
    assert response.run_id == started.run_id
    assert response.item.status is ApprovalStatus.DECIDED
    assert await until_final(api, started.run_id) is RunStatus.DONE
    run = (await api.get_run(started.run_id)).run
    assert run.starter is Starter.USER_AGENT and run.adjusted_by_user is False
    decided = [x for x in lines(wf / "runs" / started.run_id) if x.get("event") == "decided"]
    assert decided[0]["decision"]["action"] == "allow"
    assert decided[0]["decision"]["decided_by"] == "user"


async def test_adjust_keeps_the_id_and_reports_the_changes(wf: Path) -> None:
    api = make_api(wf)
    web = {"agents": {"m-1": {"tools": {"web": True}}}}
    started = await api.request_run_start(spec(overrides=web))
    item = (await api.list_approvals(run_id=started.run_id))[0]
    request = DecisionRequest(
        action=DecisionAction.ADJUST,
        config={"agents.m-1.tools.web": False, "limits.turns_per_agent": 4},
    )
    response = await api.decide_approval(item.id, request)
    assert response.run_id == started.run_id
    labels = [c.label for c in response.changes]
    assert labels == ["Web m-1: an → aus", "Turns pro Agent: 6 → 4"]
    assert response.item.decision is not None
    assert response.item.decision.action is DecisionAction.ADJUST
    assert await until_final(api, started.run_id) is RunStatus.DONE
    run = (await api.get_run(started.run_id)).run
    assert run.adjusted_by_user and [c.path for c in run.changes] == [
        "agents.m-1.tools.web",
        "limits.turns_per_agent",
    ]
    resolved = (wf / "runs" / started.run_id / "run.yaml").read_text("utf-8")
    assert "turns_per_agent: 4" in resolved


async def test_adjust_refuses_paths_the_form_cannot_change(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(spec())
    item = (await api.list_approvals(run_id=started.run_id))[0]
    for config in ({"agents.m-1.workspace.mode": "shared"}, {"approvals.decider": "gatekeeper"}):
        with pytest.raises(ApiException) as caught:
            await api.decide_approval(
                item.id, DecisionRequest(action=DecisionAction.ADJUST, config=config)
            )
        assert caught.value.error.code is ErrorCode.INVALID
    with pytest.raises(ApiException, match="nichts geändert"):
        await api.decide_approval(
            item.id, DecisionRequest(action=DecisionAction.ADJUST, config={"decider": "user"})
        )
    still = await api.get_approval(item.id)
    assert still.status is ApprovalStatus.OPEN
    await api.stop_runs()


async def test_deny_stops_the_run_with_the_message(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(spec())
    item = (await api.list_approvals(run_id=started.run_id))[0]
    request = DecisionRequest(action=DecisionAction.DENY, comment="Erst Lemma 2 prüfen.")
    await api.decide_approval(item.id, request)
    run = (await api.get_run(started.run_id)).run
    assert run.status is RunStatus.STOPPED
    run_dir = wf / "runs" / started.run_id
    await until(lambda: not run_lock_held(run_dir))
    decided = [x for x in lines(run_dir) if x.get("event") == "decided"][0]
    assert decided["decision"]["comment"] == "Erst Lemma 2 prüfen."
    with pytest.raises(ApiException) as caught:
        await api.decide_approval(item.id, DecisionRequest(action=DecisionAction.ALLOW))
    assert caught.value.error.code is ErrorCode.CONFLICT


async def test_an_invalid_skill_is_refused_at_start_run(wf: Path) -> None:
    from mcp_kit import GROUP_YAML, group_skill

    group_skill(wf, "kaputt", GROUP_YAML.replace("roles/critic.md", "roles/fehlt.md"))
    api = make_api(wf)
    with pytest.raises(ApiException) as caught:
        await api.request_run_start(spec(skill="kaputt"))
    assert caught.value.error.code is ErrorCode.CONFIG_INVALID
    assert not list((wf / "runs").glob("*/meta.json")), "nothing is reserved"


async def test_stop_of_a_waiting_start_takes_it_back(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(spec())
    with pytest.raises(ApiException, match="Freigabe"):
        await api.run_action(started.run_id, RunAction.RESUME)
    await api.run_action(started.run_id, RunAction.STOP)
    run = (await api.get_run(started.run_id)).run
    assert run.status is RunStatus.STOPPED
    item = (await api.list_approvals(run_id=started.run_id))[0]
    assert item.status is ApprovalStatus.OBSOLETE


async def test_a_waiting_start_left_by_a_crash_ends_stopped(wf: Path) -> None:
    api = make_api(wf)
    started = await api.request_run_start(spec())
    pending = api.run_starts.pending[started.run_id]
    pending.lock.release()  # as if the process had died
    api.run_starts.pending.clear()
    fresh = make_api(wf)
    assert fresh.run_starts.recover() == [started.run_id]
    run = (await fresh.get_run(started.run_id)).run
    assert run.status is RunStatus.STOPPED
    item = (await fresh.list_approvals(run_id=started.run_id))[0]
    assert item.status is ApprovalStatus.OBSOLETE
    assert "arr wurde beendet" in (item.status_text or "")


def test_the_card_knows_the_fields_of_the_estimate() -> None:
    """W4-02 fields on the card: unknown costs never show as „0 $“ (W4-01 §3.7)."""
    from arr.service.api_models import Estimate, Range
    from arr.service.gui.components.approval_card import estimate_text

    unknown = Estimate(
        turns=Range(min=3, max=3),
        cost_usd=Range(min=0, max=0),
        duration_s=Range(min=0, max=0),
        cost_basis="unknown",
        duration_basis="unknown",
        turns_open=True,
    )
    text = estimate_text(unknown)
    assert text == "mindestens 3 Turns · Kosten unbekannt · Dauer offen"
    assert "0 $" not in text
    budget = unknown.model_copy(
        update={
            "turns": Range(min=2, max=12),
            "turns_open": False,
            "cost_usd": Range(min=0, max=2),
            "cost_basis": "budget",
            "duration_s": Range(min=0, max=1200),
            "duration_basis": "wall_time",
        }
    )
    assert estimate_text(budget) == "2–12 Turns · höchstens 2 $ (Budget) · höchstens 20 min"

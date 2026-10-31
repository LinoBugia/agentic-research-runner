"""The approval inbox of the service end to end (W3-03 A1-A3, A5-A7, A9, A11): FakeBackend
agents ask, `decide_approval` answers, `approvals.jsonl` holds the record. Nothing here shows
a real notification (`notify` records) or uses a real model."""

import asyncio
import hashlib
import json
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from run_kit import FAST, channel, done, fake_builders, room, say, turns, wait, write_profiles
from service_kit import (
    append,
    hold_lock,
    make_run_folder,
    make_workfolder,
    read_meta,
    run_dirs,
    start_run_process,
    start_service,
    wait_for,
)

from arr.models import RunStatus
from arr.permissions.inbox import item_id_prefix
from arr.permissions.policy import AlwaysRule
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    PermissionRequest,
)
from arr.service.api_models import (
    ApiException,
    ApprovalEvent,
    DecisionRequest,
    ErrorCode,
    LiveEvent,
    RunAction,
)
from arr.service.real_api import RealApi
from arr.service.real_api.approvals import store_always_rule
from arr.workfolder import load_workfolder_settings

BASE = "http://127.0.0.1:8765"
HOST = {"tool": "SandboxNetworkAccess", "hosts": ["oeis.org"], "reason": "Ich brauche die Folge."}
ALLOW = DecisionRequest(action=DecisionAction.ALLOW)
DENY = DecisionRequest(action=DecisionAction.DENY, comment="Nimm die lokale Kopie.")
OPEN = [ApprovalStatus.OPEN]


def run_input(*agents: dict[str, Any], **extra: Any) -> dict[str, Any]:
    ids = [a["id"] for a in agents]
    return {
        "project": "demo",
        "name": "Testlauf",
        "task": "Teste arr.",
        "topology": {"kind": "roundtable", "params": {"rounds": 1, "channel": "main"}},
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
        "agents": list(agents),
        "groupchats": [channel("main", ids, mode="rounds")],
        **extra,
    }


def asking(*asks: dict[str, Any], steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {**turns(steps or [say("weiter"), done()]), "asks": list(asks)}


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


class Service:
    """A `RealApi` with FakeBackend runs; notifications are collected, not shown."""

    def __init__(self, wf: Path, scripts: dict[str, Any]) -> None:
        self.sent: list[tuple[str, str]] = []
        self.api = RealApi(
            wf,
            base_url=BASE,
            builders=fake_builders(scripts),
            manager_kwargs={"pacing": FAST},
            notify=lambda text, title: self.sent.append((text, title)),
        )
        self.events: list[LiveEvent] = []

    async def __aenter__(self) -> "Service":
        await self.api.start()
        self.sub = self.api.subscribe()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.api.stop_runs()
        await self.api.close()

    def approval_events(self, kind: str) -> list[ApprovalEvent]:
        self.events.extend(self.sub.drain())
        return [e for e in self.events if isinstance(e, ApprovalEvent) and e.type == kind]

    async def event(self, kind: str, within: float = 10.0) -> ApprovalEvent:
        await until(lambda: self.approval_events(kind), within)
        return self.approval_events(kind)[-1]

    async def open_item(self, within: float = 15.0) -> Any:
        end = time.monotonic() + within
        while time.monotonic() < end:
            items = await self.api.list_approvals(status=OPEN)
            if items:
                return items[0]
            await asyncio.sleep(0.02)
        raise AssertionError("no open item")

    async def final(self, run_id: str, within: float = 30.0) -> RunStatus:
        end = time.monotonic() + within
        while time.monotonic() < end:
            status = (await self.api.get_run(run_id)).run.status
            if status.is_final:
                return status
            await asyncio.sleep(0.02)
        raise AssertionError("run did not end")


async def until(check: Callable[[], Any], within: float = 10.0) -> None:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def log_of(wf: Path, run_id: str) -> list[dict[str, Any]]:
    lines = (wf / "runs" / run_id / "approvals.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def events_of(wf: Path, run_id: str) -> list[str]:
    return [e["event"] for e in log_of(wf, run_id) if e.get("item_id")]


# --- A1: ask, wait, decide -------------------------------------------------------------------


async def test_a1_request_appears_agent_waits_others_work_and_the_decision_arrives(
    wf: Path,
) -> None:
    scripts = {"a": asking(HOST), "b": turns([say("b arbeitet"), done()])}
    async with Service(wf, scripts) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}, {"id": "b"}))
        item = await svc.open_item()
        assert item.type is ApprovalType.PERMISSION and item.status is ApprovalStatus.OPEN
        assert item.agent.id == "a" and item.url == "oeis.org" and item.reason
        assert item.title == "a möchte auf oeis.org zugreifen"
        assert item.deadline == pytest.approx(item.created_at + 20 * 60, abs=2)
        assert "Feste Regeln" in item.why and item.consequences.expire
        assert not item.terminal and not item.no_sandbox
        created = await svc.event("approval.created")
        assert created.item.id == item.id and created.run_id == started.run_id
        # the agent waits, the other one goes on
        await until(lambda: "b arbeitet" in _transcript(wf, started.run_id, "b"))
        assert _transcript(wf, started.run_id, "a").count("weiter") == 0
        assert (await svc.api.get_counts()).waiting == 1
        [agent] = [a for a in await svc.api.list_agents(started.run_id) if a.id == "a"]
        assert agent.blocked_by is not None and agent.blocked_by.approval_id == item.id

        response = await svc.api.decide_approval(item.id, ALLOW)
        assert response.item.status is ApprovalStatus.DECIDED
        assert "Erlaubt (einmalig)" in response.message
        await until(lambda: "weiter" in _transcript(wf, started.run_id, "a"))
        decided = await svc.event("approval.decided")
        assert decided.item.decision.decided_by is DecidedBy.USER
        assert (await svc.api.get_counts()).waiting == 0
        assert await svc.final(started.run_id) is RunStatus.DONE
    [line] = [e for e in log_of(wf, started.run_id) if e["event"] == "decided"]
    assert line["decision"]["decided_by"] == "user" and line["decision"]["action"] == "allow"
    assert any("a fragt nach: Netzwerk" in text for text, _title in svc.sent)
    assert all("oeis.org" not in text for text, _title in svc.sent)


def _transcript(wf: Path, run_id: str, agent: str) -> str:
    path = wf / "runs" / run_id / "agents" / agent / "transcript.jsonl"
    return path.read_text("utf-8") if path.is_file() else ""


async def test_a1_a_denial_carries_the_comment_to_the_agent(wf: Path) -> None:
    async with Service(wf, {"a": asking(HOST)}) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}))
        item = await svc.open_item()
        response = await svc.api.decide_approval(item.id, DENY)
        assert response.message.startswith("Abgelehnt: a")
        await svc.final(started.run_id)
    [line] = [e for e in log_of(wf, started.run_id) if e["event"] == "decided"]
    assert line["decision"]["reason"] == "Abgelehnt: Nimm die lokale Kopie."
    assert line["decision"]["comment"] == "Nimm die lokale Kopie."


@pytest.mark.parametrize("scope", [DecisionScope.AGENT, DecisionScope.RUN], ids=["agent", "run"])
async def test_a2_grants_cover_later_requests_and_revoking_ends_them(
    wf: Path, scope: DecisionScope
) -> None:
    script = {
        "turns": [{"steps": [say("los"), done()]}],
        "asks": [HOST],
    }
    async with Service(wf, {"a": script}) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}))
        item = await svc.open_item()
        handle = svc.api.state.registry.get(started.run_id)
        inbox = handle.manager.inbox
        response = await svc.api.decide_approval(
            item.id, DecisionRequest(action=DecisionAction.ALLOW, scope=scope)
        )
        assert response.item.revocable and inbox.engine.grants
        revoked = await svc.api.revoke_approval(item.id)
        assert revoked.status is ApprovalStatus.REVOKED and not inbox.engine.grants
        assert (await svc.event("approval.revoked")).item.id == item.id
        with pytest.raises(ApiException) as error:
            await svc.api.revoke_approval(item.id)
        assert error.value.error.code is ErrorCode.NOT_ALLOWED
        await svc.final(started.run_id)
    assert "revoked" in events_of(wf, started.run_id)


async def test_always_rule_lands_in_arr_yaml_and_applies_to_the_next_run(wf: Path) -> None:
    scripts = {"a": asking(HOST)}
    async with Service(wf, scripts) as svc:
        first = await svc.api.start_run(run_input({"id": "a"}))
        item = await svc.open_item()
        always = DecisionRequest(action=DecisionAction.ALLOW, scope=DecisionScope.ALWAYS)
        await svc.api.decide_approval(item.id, always)
        await svc.final(first.run_id)
        assert "oeis.org" in (wf / "arr.yaml").read_text("utf-8")
        # the next run gets the rule from arr.yaml and does not ask
        second = await svc.api.start_run(run_input({"id": "a"}))
        assert await svc.final(second.run_id) is RunStatus.DONE
    assert not [e for e in log_of(wf, second.run_id) if e["event"] == "open"]


def test_store_always_rule_keeps_the_rest_of_arr_yaml(wf: Path) -> None:
    before = (wf / "arr.yaml").read_text("utf-8")
    store_always_rule(wf, AlwaysRule(action="allow", hosts=["oeis.org"]))
    after = (wf / "arr.yaml").read_text("utf-8")
    assert after.startswith(before.split("\n", 1)[0])
    settings = load_workfolder_settings(wf)
    assert settings.permissions["always"][0]["hosts"] == ["oeis.org"]


# --- A2: nothing is granted without a decision -----------------------------------------------


async def test_a2_deadline_counts_as_denied(wf: Path) -> None:
    extra = {"approvals": {"timeout": "1s"}}
    async with Service(wf, {"a": asking(HOST)}) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}, **extra))
        expired = await svc.event("approval.expired")
        assert expired.item.status is ApprovalStatus.EXPIRED
        assert "als abgelehnt gewertet" in expired.item.status_text
        await svc.final(started.run_id)
        with pytest.raises(ApiException) as error:
            await svc.api.decide_approval(expired.item.id, ALLOW)
        assert error.value.error.code is ErrorCode.CONFLICT
    assert "decided" not in events_of(wf, started.run_id)
    assert events_of(wf, started.run_id)[-1] == "expired"


async def test_a2_run_stop_closes_items_without_granting(wf: Path) -> None:
    async with Service(wf, {"a": asking(HOST)}) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}))
        item = await svc.open_item()
        await svc.api.run_action(started.run_id, RunAction.STOP)
        event = await svc.event("approval.expired")
        assert event.item.id == item.id and event.item.status is ApprovalStatus.OBSOLETE
        assert event.item.status_text.startswith("Nicht mehr nötig")
        await svc.final(started.run_id)
    assert "decided" not in events_of(wf, started.run_id)


async def test_a2_service_end_closes_items_without_granting(wf: Path) -> None:
    async with Service(wf, {"a": asking(HOST)}) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}))
        await svc.open_item()
    assert "decided" not in events_of(wf, started.run_id)
    assert events_of(wf, started.run_id)[-1] == "obsolete"
    assert json.loads((wf / "runs" / started.run_id / "meta.json").read_text())["status"] in (
        "stopped"
    )


async def test_a2_open_item_of_a_dead_run_is_not_open(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.RUNNING, run_id="20261030-1000_demo_custom_de4d")
    line = {
        "ts": time.time(),
        "event": "open",
        "item_id": f"{item_id_prefix(folder.name)}_0001",
        "request": {"run_id": folder.name, "agent_id": "a", "tool": "Bash", "command": "curl x"},
        "decision": {"action": "ask", "decided_by": "fixed_rule"},
    }
    append(folder / "approvals.jsonl", json.dumps(line) + "\n")
    lock = hold_lock(folder)
    async with Service(wf, {}) as svc:
        # recovery at start marks runs without a lock interrupted: the lock is held here
        item = await svc.open_item()
        assert item.terminal
        assert (await svc.api.get_counts()).waiting == 1
        svc.sent.clear()
        lock.release()  # the process dies without closing the item
        event = await svc.event("approval.expired")
        assert event.item.status is ApprovalStatus.OBSOLETE and event.item.id == item.id
        assert (await svc.api.get_counts()).waiting == 0
        assert not await svc.api.list_approvals(status=OPEN)
        with pytest.raises(ApiException) as error:
            await svc.api.decide_approval(item.id, ALLOW)
        assert error.value.error.code is ErrorCode.CONFLICT


def test_a2_ctrl_c_of_a_real_service_closes_items_without_granting(
    tmp_path: Path, wf: Path, arr_home: Path
) -> None:
    script = {**turns([say("nie"), wait(60)]), "asks": [HOST]}
    service = start_service(
        tmp_path, arr_home, wf, scripts={"a": script}, autostart=run_input({"id": "a"})
    )
    try:
        wait_for(lambda: bool(run_dirs(wf)), what="run folder")
        [run_dir] = run_dirs(wf)
        approvals = run_dir / "approvals.jsonl"
        wait_for(lambda: approvals.is_file() and '"open"' in approvals.read_text(), what="open")
        service.signal(signal.SIGINT)
        assert service.wait(timeout=90) == 0, service.output()
    finally:
        service.stop()
    assert read_meta(run_dir)["status"] == "stopped"
    assert "decided" not in events_of(wf, run_dir.name)
    assert events_of(wf, run_dir.name)[-1] == "obsolete"


async def test_a2_open_line_of_a_killed_run_is_obsolete_after_the_restart(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.INTERRUPTED)
    request = {"run_id": folder.name, "agent_id": "a", "tool": "Bash", "command": "curl x"}
    line = {"ts": time.time(), "event": "open", "item_id": "apr_k_0001", "request": request}
    append(folder / "approvals.jsonl", json.dumps(line) + "\n")
    api = RealApi(wf, base_url=BASE)
    assert not await api.list_approvals(status=OPEN)
    item = await api.get_approval("apr_k_0001")
    assert item.status is ApprovalStatus.OBSOLETE and "läuft nicht mehr" in item.status_text


# --- A3: the only way to a grant -------------------------------------------------------------


def test_a3_nothing_but_the_gui_decides() -> None:
    import inspect

    from arr.service import real_api
    from arr.service.real_api import approvals

    # the decider of the service never answers, it only leaves the item open
    assert asyncio.run(approvals.GuiDecider().decide(None)) is None  # type: ignore[arg-type]
    source = inspect.getsource(approvals)
    assert source.count("inbox.resolve(") == 1  # decide_approval
    assert "DecidedBy.USER" in inspect.getsource(approvals._decision)
    others = [p for p in Path(real_api.__file__).parent.glob("*.py") if p.name != "approvals.py"]
    others += list(Path(real_api.__file__).parent.parent.glob("*.py"))
    assert not [p.name for p in others if "inbox.resolve(" in p.read_text("utf-8")]


async def test_a3_run_start_and_escalation_take_only_user_and_once(wf: Path) -> None:
    item = await _escalation_item(wf, {})
    svc, started, escalation = item
    try:
        wrong_scope = DecisionRequest(
            action=DecisionAction.OPTION,
            scope=DecisionScope.RUN,
            option_key=EscalationOption.RETRY,
        )
        with pytest.raises(ApiException) as error:
            await svc.api.decide_approval(escalation.id, wrong_scope)
        assert error.value.error.code is ErrorCode.INVALID
        with pytest.raises(ApiException) as error:
            await svc.api.decide_approval(escalation.id, ALLOW)
        assert error.value.error.code is ErrorCode.INVALID
        assert (await svc.api.get_approval(escalation.id)).status is ApprovalStatus.OPEN
    finally:
        await svc.__aexit__()
    assert "decided" not in events_of(wf, started.run_id)


# --- A5: runs of `arr run` are read-only ------------------------------------------------------


async def test_a5_run_of_a_second_process_is_read_only_and_fixed_allows_do_not_show(
    tmp_path: Path, wf: Path, arr_home: Path
) -> None:
    (wf / "solo.yaml").write_text(
        "version: 1\nproject: demo\ntask: Los.\ntopology: {kind: solo}\n"
        "defaults: {provider: claude, model: fake-model, auth: fake-auth}\n"
        "agents: [{id: a}]\n",
        encoding="utf-8",
    )
    asks = [{"tool": "Bash", "command": "python3 check.py"}, HOST]
    script = {**turns([say("nie"), wait(60)]), "asks": asks}
    async with Service(wf, {}) as svc:
        process = start_run_process(tmp_path, arr_home, wf / "solo.yaml", {"a": script})
        try:
            item = await svc.open_item(within=60)
            assert item.terminal and item.url == "oeis.org"
            event = await svc.event("approval.created")
            assert event.item.terminal
            with pytest.raises(ApiException) as error:
                await svc.api.decide_approval(item.id, ALLOW)
            assert error.value.error.code is ErrorCode.NOT_ALLOWED
            assert "Terminal" in error.value.error.message
            # the allow of a fixed rule stands in approvals.jsonl, but is no request
            lines = log_of(wf, item.run.id)
            assert any(
                e["event"] == "decision" and e["decision"]["action"] == "allow" for e in lines
            )
            assert [i.id for i in await svc.api.list_approvals()] == [item.id]
            await svc.api.run_action(item.run.id, RunAction.STOP)
            wait_for(lambda: process.poll() is not None, 40, what="child ended")
        finally:
            if process.poll() is None:
                process.kill()
    assert "decided" not in events_of(wf, item.run.id)


# --- A6: escalations of a topology ------------------------------------------------------------


async def _escalation_item(wf: Path, extra: dict[str, Any]) -> tuple[Service, Any, Any]:
    submit = room("submit_result", kind="sketch", title="Skizze", body="Erster Versuch.")
    worker = {"turns": [], "by_stage": {"skizze": {"turns": [{"steps": [submit, done()]}]}}}
    worker["by_stage"]["skizze"]["repeat_last"] = True
    job = room("get_assignment", save_as="job")
    rating = room(
        "submit_rating", result_id="${job.context.result_id}", score=2, comment="Zu dünn."
    )
    rater = {"turns": [], "by_stage": {"skizze": {"turns": [{"steps": [job, rating, done()]}]}}}
    rater["by_stage"]["skizze"]["repeat_last"] = True
    svc = Service(wf, {"w": worker, "r": rater})
    await svc.__aenter__()
    data = {
        "project": "demo",
        "name": "Wasserfall",
        "task": "Teste arr.",
        "topology": {
            "kind": "waterfall",
            "params": {"on_fail": "escalate", "max_rework": 0, "pass_score": 6},
        },
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
        "agents": [{"id": "w"}, {"id": "r"}],
        "stages": [{"name": "skizze", "agent": "w"}],
        "raters": ["r"],
        **extra,
    }
    started = await svc.api.start_run(data)
    return svc, started, await svc.open_item(within=20)


async def test_a6_escalation_shows_options_pauses_and_the_choice_goes_on(wf: Path) -> None:
    svc, started, item = await _escalation_item(wf, {})
    try:
        assert item.type is ApprovalType.ESCALATION and item.stage == "skizze"
        assert [o.key for o in item.options] == list(EscalationOption)
        assert [o.label for o in item.options][0] == "Noch ein Versuch"
        assert item.topology.value == "waterfall" and item.deadline is None
        assert "skizze" in item.title and "nicht bestanden" in item.title
        assert item.attempts and item.attempts[0].avg_score == 2
        assert item.last_ratings[0].comment == "Zu dünn."
        assert "Eskalation" in await _sent(svc)
        await until_status(svc.api, started.run_id, RunStatus.PAUSED)
        run = (await svc.api.get_run(started.run_id)).run
        assert run.open_escalation and run.open_approvals == 1
        choice = DecisionRequest(action=DecisionAction.OPTION, option_key=EscalationOption.END)
        response = await svc.api.decide_approval(item.id, choice)
        assert response.message.startswith("Run beenden")
        assert response.item.decision.option_key is EscalationOption.END
        assert await svc.final(started.run_id) is not RunStatus.PAUSED
    finally:
        await svc.__aexit__()
    [line] = [e for e in log_of(wf, started.run_id) if e["event"] == "decided"]
    assert line["decision"]["decided_by"] == "user" and line["decision"]["scope"] == "once"


async def _sent(svc: Service) -> str:
    await until(lambda: svc.sent)
    return svc.sent[-1][0]


async def until_status(api: RealApi, run_id: str, status: RunStatus) -> None:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        if (await api.get_run(run_id)).run.status is status:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"run never reached {status}")


# --- A7: Codex escalations ---------------------------------------------------------------------

CODEX = {
    "id": "c",
    "provider": "codex",
    "model": "fake-model",
    "auth": "fake-codex",
}
ESCALATE = {
    "provider": "codex",
    "tool": "commandExecution",
    "command": "/bin/zsh -lc 'python3 tool.py'",
    "reason": "Das Skript braucht mehr.",
}


async def test_a7_codex_escalation_says_no_sandbox_and_takes_only_once(wf: Path) -> None:
    async with Service(wf, {"c": asking(ESCALATE)}) as svc:
        started = await svc.api.start_run(run_input(CODEX))
        item = await svc.open_item()
        assert item.no_sandbox and "erhöhten Rechten" in item.title
        assert "nicht zusagen" in item.consequences.allow and "scheitern" in item.consequences.allow
        assert "ohne Sandbox" not in item.title + item.consequences.allow
        for scope in (DecisionScope.AGENT, DecisionScope.RUN, DecisionScope.ALWAYS):
            with pytest.raises(ApiException) as error:
                await svc.api.decide_approval(
                    item.id, DecisionRequest(action=DecisionAction.ALLOW, scope=scope)
                )
            assert error.value.error.code is ErrorCode.NOT_ALLOWED
        response = await svc.api.decide_approval(item.id, ALLOW)
        assert response.item.decision.scope is DecisionScope.ONCE
        assert not response.item.revocable
        await svc.final(started.run_id)


async def test_a7_a_later_equal_codex_escalation_asks_again(wf: Path) -> None:
    async with Service(wf, {"c": asking(ESCALATE)}) as svc:
        started = await svc.api.start_run(run_input(CODEX))
        item = await svc.open_item()
        await svc.api.decide_approval(item.id, ALLOW)
        await svc.final(started.run_id)
        inbox = svc.api.state.registry.get(started.run_id).manager.inbox
        assert not inbox.engine.grants
        inbox.timeout = 0.05
        request = PermissionRequest(run_id=started.run_id, agent_id="c", **ESCALATE)
        assert (await inbox.handle(request)).action is DecisionAction.DENY
    assert events_of(wf, started.run_id).count("open") == 2


# --- A9: carry-overs ----------------------------------------------------------------------------


async def test_a9_equal_open_requests_are_one_card_and_one_decision_answers_all(wf: Path) -> None:
    asks = [HOST] * 7
    async with Service(wf, {"a": asking(*asks)}) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}))
        await svc.event("approval.created")
        await _wait_count(svc, 7)
        items = await svc.api.list_approvals(status=OPEN)
        assert len(items) == 1 and items[0].count == 7
        assert (await svc.api.get_counts()).waiting == 1
        response = await svc.api.decide_approval(items[0].id, ALLOW)
        assert "7 gleiche Anfragen" in response.message
        assert await svc.final(started.run_id) is RunStatus.DONE
        assert len(svc.approval_events("approval.created")) == 1
    assert events_of(wf, started.run_id).count("open") == 1
    assert events_of(wf, started.run_id).count("decided") == 1


async def _wait_count(svc: Service, count: int) -> None:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        items = await svc.api.list_approvals(status=OPEN)
        if items and items[0].count == count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"count never reached {count}")


async def test_a9_a_pip_card_says_what_comes_with_it(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.RUNNING)
    request = {
        "run_id": folder.name,
        "agent_id": "a",
        "tool": "Bash",
        "command": "pip install sympy",
        "categories": ["install", "network"],
    }
    line = {"ts": time.time(), "event": "open", "item_id": "apr_x_0001", "request": request}
    append(folder / "approvals.jsonl", json.dumps(line) + "\n")
    lock = hold_lock(folder)
    api = RealApi(wf, base_url=BASE)
    [item] = await api.list_approvals(status=OPEN)
    lock.release()
    assert item.title == "a möchte etwas installieren"
    assert "pypi.org und files.pythonhosted.org" in item.consequences.allow


def test_a9_item_ids_do_not_collide_like_six_hex_did() -> None:
    def old(run_id: str) -> str:
        return hashlib.sha1(run_id.encode()).hexdigest()[:6]

    seen: dict[str, str] = {}
    first = second = ""
    for number in range(100_000):
        run_id = f"20261030-1000_demo_custom_{number:05x}"
        if old(run_id) in seen:
            first, second = seen[old(run_id)], run_id
            break
        seen[old(run_id)] = run_id
    assert first and second
    assert item_id_prefix(first) != item_id_prefix(second)


async def test_a9_old_item_ids_stay_readable(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.DONE, run_id="20261001-1000_demo_custom_0a1d")
    old_id = "apr_abc123_0001"
    request = {"run_id": folder.name, "agent_id": "a", "tool": "Bash", "command": "curl x"}
    lines = [
        {"ts": 100.0, "event": "open", "item_id": old_id, "request": request},
        {
            "ts": 101.0,
            "event": "decided",
            "item_id": old_id,
            "request": request,
            "decision": {"action": "deny", "decided_by": "user", "decided_at": 101.0},
        },
    ]
    text = "".join(json.dumps(line) + "\n" for line in lines)
    (folder / "approvals.jsonl").write_text(text, "utf-8")
    api = RealApi(wf, base_url=BASE)
    item = await api.get_approval(old_id)
    assert item.status is ApprovalStatus.DECIDED and item.command == "curl x"


# --- A11: speed -----------------------------------------------------------------------------------


def test_a11_list_of_200_runs_under_a_second(wf: Path) -> None:
    import shutil

    template = make_run_folder(wf, status=RunStatus.DONE, name="Vorlage")
    meta = json.loads((template / "meta.json").read_text())
    request = {"run_id": template.name, "agent_id": "a", "tool": "Bash", "command": "curl x"}
    decision = {"action": "allow", "decided_by": "user", "decided_at": 1.0}
    noise = {"event": "decision", "request": request, "decision": decision}
    for i in range(200):
        run_id = f"20261001-{i // 60:02d}{i % 60:02d}_demo_custom_{i:04x}"
        target = template.parent / run_id
        shutil.copytree(template, target)
        (target / "meta.json").write_text(json.dumps({**meta, "run_id": run_id}))
        lines = [json.dumps({**noise, "ts": float(n)}) for n in range(50)]
        for n in range(1, 4):
            item_id = f"{item_id_prefix(run_id)}_{n:04d}"
            opened = {"ts": 2.0, "event": "open", "item_id": item_id, "request": request}
            closed = {"ts": 3.0, "event": "decided", "item_id": item_id, "decision": decision}
            lines += [json.dumps(opened), json.dumps(closed)]
        (target / "approvals.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
    api = RealApi(wf, base_url=BASE)
    started = time.perf_counter()
    items = asyncio.run(api.list_approvals())
    cold = time.perf_counter() - started
    started = time.perf_counter()
    again = asyncio.run(api.list_approvals())
    warm = time.perf_counter() - started
    print(f"A11: kalt {cold:.3f} s, warm {warm:.3f} s, {len(items)} Einträge")
    assert len(items) == len(again) == 600
    assert cold <= 1.0 and warm <= 1.0

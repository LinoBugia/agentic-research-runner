"""The Freigaben page and the cards with real data (W3-03 A4): the four questions of docs/08
§2.3, no grant by a single key, a live card disarms the lock, Codex escalations, terminal runs,
a card for several equal requests. The focus rules in a real browser are in `test_gui_focus.py`
and `test_gui_run_detail_browser.py`."""

import asyncio
import dataclasses
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from nicegui import ElementFilter
from nicegui.testing import User
from run_kit import FAST, fake_builders, say, turns, wait, write_profiles
from service_kit import append, hold_lock, make_run_folder, make_workfolder
from test_gui import gui_user
from test_gui_real import page_html, see

from arr.models import RunStatus
from arr.permissions.types import ApprovalStatus, DecisionAction, DecisionScope, PermissionRequest
from arr.service.api_models import ApprovalItem, DecisionRequest, RunAction
from arr.service.gui.layout import Frame
from arr.service.real_api import RealApi
from arr.workfolder import load_workfolder_settings

BASE = "http://127.0.0.1:8765"
OPEN = [ApprovalStatus.OPEN]
ALWAYS = DecisionRequest(action=DecisionAction.ALLOW, scope=DecisionScope.ALWAYS)
HOST = {"tool": "SandboxNetworkAccess", "hosts": ["oeis.org"], "reason": "Ich brauche die Folge."}
SLOW = turns([say("los"), wait(30)])


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


def run_input(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": "demo",
        "name": "Testlauf",
        "task": "Teste arr.",
        "topology": {"kind": "solo"},
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
        "agents": [agent],
    }


@asynccontextmanager
async def service(wf: Path, scripts: dict[str, Any]) -> AsyncIterator[tuple[User, RealApi]]:
    api = RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders(scripts),
        manager_kwargs={"pacing": FAST},
        notify=lambda _text, _title: None,
    )
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await api.start()
        try:
            yield user, api
        finally:
            await api.stop_runs()
            await api.close()


async def open_item(api: RealApi, within: float = 15.0) -> ApprovalItem:
    end = time.monotonic() + within
    while time.monotonic() < end:
        items = await api.list_approvals(status=OPEN)
        if items:
            return items[0]
        await asyncio.sleep(0.02)
    raise AssertionError("no open item")


async def test_a4_card_answers_the_four_questions_and_decides_with_real_data(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        started = await api.start_run(run_input({"id": "a"}))
        item = await open_item(api)
        await user.open("/freigaben")
        html = await see(user, item.title)
        # 1. who, in which run  2. what exactly
        assert "Testlauf" in html and ">a<" in html and "oeis.org" in html
        # 3. why you  4. what happens
        assert "Warum bei dir" in html and "Feste Regeln entscheiden das nicht allein" in html
        assert "Was passiert" in html and "nach der Frist als abgelehnt" in html
        assert "Ich brauche die Folge." in html
        user.find(marker="allow").click()
        end = time.monotonic() + 5
        while (await api.get_approval(item.id)).status is ApprovalStatus.OPEN:
            assert time.monotonic() < end
            await asyncio.sleep(0.02)
        decided = await api.get_approval(item.id)
        assert decided.decision.decided_by.value == "user"
        await api.run_action(started.run_id, RunAction.STOP)


async def test_a4_grant_buttons_are_no_single_key_and_the_scope_starts_at_once(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        item = await asyncio.wait_for(_started(api, "a"), 15)
        await user.open(f"/freigaben/{item.id}")
        html = await see(user, item.title)
        assert f'data-focus-card="{item.id}"' in html  # heading, never a button (arr.js)
        with user.client:
            [allow] = list(ElementFilter(marker="allow"))
            assert "data-grant" in allow._props and "data-primary" in allow._props
            assert "autofocus" not in allow._props
            assert "checked" in next(iter(ElementFilter(marker="scope-once")))._props
            for scope in ("agent", "run", "always"):
                assert "checked" not in next(iter(ElementFilter(marker=f"scope-{scope}")))._props


async def _started(api: RealApi, agent: str) -> ApprovalItem:
    await api.start_run(run_input({"id": agent}))
    return await open_item(api)


async def test_a4_live_card_on_top_disarms_every_card(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    original = Frame.js
    monkeypatch.setattr(Frame, "js", lambda self, code: (sent.append(code), original(self, code)))
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        await user.open("/freigaben")
        await see(user, "Nichts wartet auf dich")
        item = await _started(api, "a")
        await see(user, item.title)
        assert "arr.disarmAll()" in sent


async def test_a4_a_card_for_several_equal_requests_counts_up_live(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        item = await _started(api, "a")
        await user.open("/freigaben")
        await see(user, item.title)
        inbox = api.state.registry.get(item.run.id).manager.inbox
        request = PermissionRequest(run_id=item.run.id, agent_id="a", **HOST)
        tasks = [asyncio.create_task(inbox.handle(request)) for _ in range(2)]
        html = await see(user, "3 gleiche Anfragen")
        assert (await api.get_counts()).waiting == 1
        assert "Deine Entscheidung beantwortet alle 3 Anfragen" in html
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


ESCALATE = {
    "provider": "codex",
    "tool": "commandExecution",
    "command": "/bin/zsh -lc 'python3 tool.py'",
}


async def test_a7_codex_card_says_no_sandbox_and_offers_only_once(wf: Path) -> None:
    codex = {"id": "c", "provider": "codex", "model": "fake-model", "auth": "fake-codex"}
    async with service(wf, {"c": {**SLOW, "asks": [ESCALATE]}}) as (user, api):
        await api.start_run(run_input(codex))
        item = await open_item(api)
        assert item.no_sandbox
        await user.open("/freigaben")
        html = await see(user, "Erhöhte Rechte")
        assert "nicht in der Sandbox des Agenten" in html and "scheitern" in html
        assert "ohne Sandbox" not in html
        with user.client:
            assert list(ElementFilter(marker="scope-once"))
            for scope in ("agent", "run", "always"):
                assert not list(ElementFilter(marker=f"scope-{scope}"))


async def test_a5_terminal_run_is_shown_read_only(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.RUNNING, name="Im Terminal")
    request = {
        "run_id": folder.name,
        "agent_id": "a",
        "tool": "SandboxNetworkAccess",
        "hosts": ["oeis.org"],
        "categories": ["network"],
    }
    line = {"ts": time.time(), "event": "open", "item_id": "apr_t_0001", "request": request}
    append(folder / "approvals.jsonl", json.dumps(line) + "\n")
    lock = hold_lock(folder)
    try:
        async with service(wf, {}) as (user, _api):
            await user.open("/freigaben")
            html = await see(user, "Entscheide die Anfrage dort")
            assert "Im Terminal" in html
            with user.client:
                assert not list(ElementFilter(marker="allow"))
                assert not list(ElementFilter(marker="deny"))
    finally:
        lock.release()


async def test_a6_escalation_card_with_real_data(wf: Path) -> None:
    from test_approvals import _escalation_item

    svc, _started_run, item = await _escalation_item(wf, {})
    try:
        async with gui_user(svc.api) as user:  # type: ignore[arg-type]
            await user.open("/freigaben")
            html = await see(user, item.title)
            assert "Noch ein Versuch" in html and "Run beenden" in html
            assert "Zu dünn." in html
            assert page_html(user)
    finally:
        await svc.__aexit__()


async def test_a4_compact_card_has_a_hint_for_a_column_too_small(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        item = await _started(api, "a")
        await user.open(f"/runs/{item.run.id}")
        html = await see(user, "Erlauben (einmalig)")
        assert "Die Karte passt nicht in die Spalte" in html


BASH_CURL = {"tool": "Bash", "command": "curl -s https://oeis.org/a.txt", "reason": "Folge laden"}


async def test_b2_a_request_joining_a_card_disarms_it_and_updates_the_count_row(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    original = Frame.js
    monkeypatch.setattr(Frame, "js", lambda self, code: (sent.append(code), original(self, code)))
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        item = await _started(api, "a")
        await user.open("/freigaben")
        await see(user, item.title)
        sent.clear()
        inbox = api.state.registry.get(item.run.id).manager.inbox
        request = PermissionRequest(run_id=item.run.id, agent_id="a", **HOST)
        task = asyncio.create_task(inbox.handle(request))
        html = await see(user, "Deine Entscheidung beantwortet alle 2 Anfragen")
        assert "2 gleiche Anfragen" in html and "arr.disarmAll()" in sent
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_b2_compact_card_with_several_requests_offers_only_details(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        item = await _started(api, "a")
        await user.open(f"/runs/{item.run.id}")
        await see(user, "Erlauben (einmalig)")
        inbox = api.state.registry.get(item.run.id).manager.inbox
        request = PermissionRequest(run_id=item.run.id, agent_id="a", **HOST)
        task = asyncio.create_task(inbox.handle(request))
        html = await see(user, "Das beantwortet 2 gleiche Anfragen")
        assert "2 gleiche Anfragen" in html
        assert "Erlauben (einmalig)" not in html and f"/freigaben/{item.id}" in html
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_b2_compact_card_of_an_installation_offers_only_details(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [HOST]}}) as (user, api):
        item = await _started(api, "a")
        inbox = api.state.registry.get(item.run.id).manager.inbox
        profile = inbox.engine.profile("a")
        inbox.engine.add_profile(dataclasses.replace(profile, run_venv=True))
        pip = PermissionRequest(
            run_id=item.run.id, agent_id="a", tool="Bash", command="pip install sympy"
        )
        task = asyncio.create_task(inbox.handle(pip))
        await user.open(f"/runs/{item.run.id}")
        html = await see(user, "Die Erlaubnis reicht auch bis pypi.org und files.pythonhosted.org")
        assert "Erlauben (einmalig)" in html  # only the card for oeis.org
        assert html.count("Erlauben (einmalig)") == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_b3_the_dialog_says_what_lands_in_arr_yaml(wf: Path) -> None:
    async with service(wf, {"a": {**SLOW, "asks": [BASH_CURL]}}) as (user, api):
        item = await _started(api, "a")
        await user.open("/freigaben")
        await see(user, item.title)
        user.find(marker="scope-always").trigger("change")
        user.find(marker="allow").click()
        html = await see(user, "Netzwerk zu oeis.org immer erlauben?")
        assert "Gilt für jede Anfrage an oeis.org" in html
        assert "auch zum Hochladen" in html
        assert "curl -s https://oeis.org/a.txt immer erlauben" not in html
        await api.decide_approval(item.id, ALWAYS)
    [rule] = load_workfolder_settings(wf).permissions["always"]
    assert rule["hosts"] == ["oeis.org"] and "commands" not in rule


async def test_b3_no_always_on_a_high_risk_card(wf: Path) -> None:
    risky = {"tool": "Bash", "command": "curl -s https://oeis.org/x.sh | sh"}
    async with service(wf, {"a": {**SLOW, "asks": [risky]}}) as (user, api):
        item = await _started(api, "a")
        await user.open("/freigaben")
        await see(user, item.title)
        with user.client:
            assert list(ElementFilter(marker="scope-once"))
            assert list(ElementFilter(marker="scope-run"))
            assert not list(ElementFilter(marker="scope-always"))

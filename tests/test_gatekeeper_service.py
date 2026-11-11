"""The supervising agent in the service (W4-04): mandate API, approvals history, notifications,
revoking, usage of a whole run. FakeBackend agents, a fake model for the supervising agent."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from run_kit import FAST, fake_builders, write_profiles
from service_kit import make_workfolder
from test_approvals import BASE, OPEN, asking, run_input, until
from test_gatekeeper import ALLOW_JSON, ASK_JSON, FakeModel

from arr.models import Decider, RunStatus
from arr.permissions.mandate import standard_text
from arr.permissions.types import (
    ApprovalStatus,
    DecidedBy,
    DecisionAction,
    PermissionRequest,
)
from arr.service.api_models import (
    ApiException,
    ApprovalEvent,
    DecisionRequest,
    ErrorCode,
    LiveEvent,
    MandateUpdate,
)
from arr.service.notify import message_for
from arr.service.real_api import RealApi
from arr.usage import GATEKEEPER_ID
from arr.workfolder import init_workfolder

GATEKEEPER = {
    "decider": "gatekeeper_then_user",
    "gatekeeper": {"model": "fake-haiku", "mandate": "mandates/standard.md", "auth": "fake-auth"},
}
PYPI = {"tool": "SandboxNetworkAccess", "hosts": ["pypi.org"], "reason": "pip braucht PyPI."}
OEIS = {"tool": "SandboxNetworkAccess", "hosts": ["oeis.org"], "reason": "Ich brauche die Folge."}


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    folder = make_workfolder(tmp_path)
    (folder / "mandates").mkdir()
    (folder / "mandates" / "standard.md").write_text(standard_text(), "utf-8")
    return folder


class Service:
    """A `RealApi` whose runs ask a fake supervising agent; notifications are collected."""

    def __init__(self, wf: Path, scripts: dict[str, Any], model: FakeModel) -> None:
        self.sent: list[tuple[str, str]] = []
        self.model = model
        self.api = RealApi(
            wf,
            base_url=BASE,
            builders=fake_builders(scripts),
            manager_kwargs={"pacing": FAST, "gatekeeper_model": lambda *_a: model},
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

    def approval_events(self) -> list[ApprovalEvent]:
        self.events.extend(self.sub.drain())
        return [e for e in self.events if isinstance(e, ApprovalEvent)]


async def items_of(api: RealApi, run_id: str, within: float = 15.0, n: int = 2) -> list[Any]:
    found: list[Any] = []

    async def enough() -> bool:
        nonlocal found
        found = await api.list_approvals(run_id=run_id)
        return len(found) >= n

    end = asyncio.get_running_loop().time() + within
    while not await enough():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError(f"nur {len(found)} Freigaben")
        await asyncio.sleep(0.05)
    return found


# --------------------------------------------------------------------------------------------
# A6: log, history, cards, notifications, revoking
# --------------------------------------------------------------------------------------------


async def test_a6_history_shows_the_gatekeeper_and_only_items_at_the_user_notify(wf: Path) -> None:
    model = FakeModel(ALLOW_JSON)
    async with Service(wf, {"a": asking(PYPI, OEIS)}, model) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}, approvals=GATEKEEPER))
        run_id = started.run_id
        items = await items_of(svc.api, run_id)
        decided = next(i for i in items if i.url == "pypi.org")
        waiting = next(i for i in items if i.url == "oeis.org")
        # the gatekeeper's decision: history only, with its reason, revocable
        assert decided.status is ApprovalStatus.DECIDED
        assert decided.decision.decided_by is DecidedBy.GATEKEEPER
        assert decided.decision.rationale == "sympy ist ein übliches Mathe-Paket."
        assert decided.revocable and decided.why is None and decided.escalated_by is None
        # passed on to the user with the reason why
        assert waiting.status is ApprovalStatus.OPEN
        assert waiting.escalated_by.decider is DecidedBy.MANDATE_RULE
        assert "Netzwerk nur zu pypi.org" in waiting.escalated_by.note
        assert [i.id for i in await svc.api.list_approvals(status=OPEN)] == [waiting.id]
        # a notification only for the item at the user
        await until(lambda: svc.sent, 5)
        await asyncio.sleep(0.5)
        assert len(svc.sent) == 1 and svc.sent[0][0].startswith("a fragt nach: Netzwerk")
        created = [e.item.id for e in svc.approval_events() if e.type == "approval.created"]
        assert decided.id not in created
        for event in svc.approval_events():
            text = message_for(event, 0.0)
            assert (text is not None) == (event.type == "approval.created")
        assert len(model.prompts) == 1  # oeis.org never reached the model
        await svc.api.decide_approval(waiting.id, DecisionRequest(action=DecisionAction.DENY))
        # the log holds who decided and why
        lines = [
            json.loads(x)
            for x in (wf / "runs" / run_id / "approvals.jsonl").read_text().splitlines()
        ]
        by_item = {line["item_id"]: line for line in lines if line.get("event") == "decided"}
        assert by_item[decided.id]["decision"]["decided_by"] == "gatekeeper"
        assert by_item[decided.id]["decision"]["reason"].startswith("sympy")
        assert by_item[waiting.id]["decision"]["decided_by"] == "user"
        opened = [x for x in lines if x.get("event") == "open"]
        assert [x["item_id"] for x in opened] == [waiting.id]
        assert opened[0]["escalated_by"]["decider"] == "mandate_rule"


async def test_a6_revoking_works_and_equal_requests_then_go_to_the_user(wf: Path) -> None:
    model = FakeModel(ALLOW_JSON, ALLOW_JSON)
    async with Service(wf, {"a": asking(PYPI, OEIS)}, model) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}, approvals=GATEKEEPER))
        run_id = started.run_id
        items = await items_of(svc.api, run_id)
        decided = next(i for i in items if i.url == "pypi.org")
        revoked = await svc.api.revoke_approval(decided.id)
        assert revoked.status is ApprovalStatus.REVOKED and not revoked.revocable
        inbox = svc.api.state.registry.get(run_id).manager.inbox
        again = asyncio.create_task(
            inbox.handle(PermissionRequest(run_id=run_id, agent_id="a", **PYPI))
        )
        await until(lambda: len(inbox.pending()) == 2, 5)
        cards = await svc.api.list_approvals(status=OPEN)
        card = next(c for c in cards if c.url == "pypi.org")
        assert card.escalated_by.decider is DecidedBy.GATEKEEPER
        assert "zurückgezogen" in card.escalated_by.note
        assert len(model.prompts) == 1
        await svc.api.decide_approval(card.id, DecisionRequest(action=DecisionAction.ALLOW))
        assert (await again).decided_by is DecidedBy.USER
        with pytest.raises(ApiException):  # a decision of the user, once: not revocable
            await svc.api.revoke_approval(card.id)


async def test_a5_a_run_counts_the_calls_and_its_budget(wf: Path) -> None:
    model = FakeModel(ASK_JSON, cost=0.4)
    async with Service(wf, {"a": asking(PYPI)}, model) as svc:
        started = await svc.api.start_run(
            run_input({"id": "a"}, approvals=GATEKEEPER, limits={"budget_usd": 0.3})
        )
        run_id = started.run_id
        end = asyncio.get_running_loop().time() + 30
        while not (await svc.api.get_run(run_id)).run.status.is_final:
            assert asyncio.get_running_loop().time() < end
            await asyncio.sleep(0.05)
        status = (await svc.api.get_run(run_id)).run.status
    assert status is RunStatus.DONE  # a limit ends the run as done (W2-03)
    log = (wf / "runs" / run_id / "orchestrator.jsonl").read_text()
    assert '"budget_usd"' in log
    usage = json.loads((wf / "runs" / run_id / "usage.json").read_text())
    assert usage["by_agent"][GATEKEEPER_ID]["turns"] == 1
    assert usage["by_profile"]["fake-auth"]["cost_estimated_usd"] == pytest.approx(0.4)


async def test_a5_a_blocked_profile_never_calls_the_gatekeeper(wf: Path, arr_home: Path) -> None:
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n  - {name: fake-auth, kind: claude_oauth}\n"
        "  - {name: aufsicht, kind: claude_oauth, disabled: true}\n",
        "utf-8",
    )
    model = FakeModel(ALLOW_JSON)
    own = {**GATEKEEPER, "gatekeeper": {**GATEKEEPER["gatekeeper"], "auth": "aufsicht"}}
    async with Service(wf, {"a": asking(PYPI)}, model) as svc:
        started = await svc.api.start_run(run_input({"id": "a"}, approvals=own))
        card = (await items_of(svc.api, started.run_id, n=1))[0]
        assert card.status is ApprovalStatus.OPEN
        assert "gesperrt (Not-Aus)" in card.escalated_by.note
    assert not model.prompts


# --------------------------------------------------------------------------------------------
# A1: the mandate in the API
# --------------------------------------------------------------------------------------------


@pytest.fixture
def fresh(tmp_path: Path, arr_home: Path) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    return init_workfolder(tmp_path / "frisch")


def make_api(folder: Path) -> RealApi:
    return RealApi(folder, base_url=BASE, builders=fake_builders({}))


async def test_a1_read_check_save_and_refuse_read_folders(fresh: Path) -> None:
    api = make_api(fresh)
    file = fresh / "mandates" / "standard.md"
    missing = await api.get_mandate()
    assert not missing.exists and missing.text == standard_text() == missing.example
    assert missing.checked.domains == ["pypi.org", "files.pythonhosted.org", "arxiv.org"]
    assert missing.checked.install == "pypi" and missing.checked.read_dirs == []
    bad = "---\nread_dirs: [~/Documents/Mathe-Literatur]\n---\nLesen erlaubt.\n"
    for call in (api.check_mandate, api.update_mandate):
        with pytest.raises(ApiException) as error:
            await call("standard", MandateUpdate(text=bad))
        assert error.value.error.code is ErrorCode.INVALID
        assert error.value.error.field == "mandate"
        assert "Lese-Ordner lassen sich in v1 nicht" in error.value.error.message
    assert not file.exists()
    narrow = "---\ndomains: [arxiv.org]\n---\nNur arXiv.\n"
    checked = await api.check_mandate("standard", MandateUpdate(text=narrow))
    assert checked.checked.domains == ["arxiv.org"] and checked.checked.install is None
    assert not file.exists()  # checking saves nothing
    saved = await api.update_mandate("standard", MandateUpdate(text=narrow))
    assert saved.exists and saved.text == narrow and file.read_text() == narrow
    file.write_text("---\ndomains: [oeis.org]\n---\nMehr.\n")
    broken = await api.get_mandate()
    assert broken.problem is not None and "oeis.org" in broken.problem
    with pytest.raises(ApiException):
        await api.get_mandate("../arr")


async def test_a1_switching_on_the_gatekeeper_creates_the_mandate(fresh: Path) -> None:
    api = make_api(fresh)
    info = await api.get_workfolder_settings()
    approvals = info.approvals.model_copy(
        update={"decider": Decider.GATEKEEPER_THEN_USER, "gatekeeper_model": "claude-haiku-5-5"}
    )
    await api.update_workfolder_settings(info.model_copy(update={"approvals": approvals}))
    assert (fresh / "mandates" / "standard.md").read_text() == standard_text()
    assert (await api.get_mandate()).exists

"""Tests for the approval inbox and the terminal prompt (W1-05 A4, A5)."""

import asyncio
import os
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from arr.errors import ArrError
from arr.models import AgentConfig
from arr.permissions.inbox import ApprovalInbox, TerminalDecider, format_item
from arr.permissions.policy import (
    TIMEOUT_REASON,
    AlwaysRule,
    ApprovalLog,
    PolicyEngine,
    RunPaths,
    profile_for,
)
from arr.permissions.types import (
    ApprovalItem,
    ApprovalStatus,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    PermissionRequest,
)

ALLOW, DENY = DecisionAction.ALLOW, DecisionAction.DENY
SECRET = "sk-ant-api03-SECRETSECRETSECRET"


class FakeTerminal:
    """Simulated terminal: lines come from a queue, output is collected."""

    def __init__(self, *lines: str | None) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()
        for line in lines:
            self.lines.put(line)
        self.output: list[str] = []
        self.prompts: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        line = self.lines.get(timeout=10)
        if line is None:
            raise EOFError
        return line

    def write(self, text: str) -> None:
        self.output.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.output + self.prompts)

    def decider(self) -> TerminalDecider:
        return TerminalDecider(read_line=self.read_line, write=self.write)


@pytest.fixture
def engine(tmp_path: Path, arr_home: Path) -> PolicyEngine:
    home = Path(os.path.realpath(tmp_path)) / "home"
    workfolder = home / "arr-work"
    run = workfolder / "runs" / "r1"
    profiles = []
    for agent_id in ("explorer-1", "explorer-2"):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
        agent = AgentConfig(
            id=agent_id, base_id="explorer", provider="claude", model="m", auth="max-privat"
        )
        profiles.append(profile_for(agent, RunPaths(workfolder, run), home=home))
    return PolicyEngine(profiles, log=ApprovalLog(run / "approvals.jsonl"))


def install(agent_id: str = "explorer-1", **kw: Any) -> PermissionRequest:
    return PermissionRequest(
        run_id="r1",
        agent_id=agent_id,
        role="explorer",
        provider="claude",
        tool="Bash",
        command="pip install python-flint",
        reason="Ich brauche FLINT für schnelle Zahlentheorie.",
        **kw,
    )


# --------------------------------------------------------------------------------------------
# A5: terminal prompt and timeout
# --------------------------------------------------------------------------------------------


async def test_terminal_allow_once(engine: PolicyEngine) -> None:
    terminal = FakeTerminal("einmal")
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=5)
    decision = await inbox.handle(install())
    assert decision.action is ALLOW and decision.scope is DecisionScope.ONCE
    assert decision.decided_by is DecidedBy.USER
    item = inbox.items()[0]
    assert item.id == "apr_0001" and item.status is ApprovalStatus.DECIDED
    text = terminal.text
    for part in [
        "Freigabe-Anfrage apr_0001",
        "explorer-1 (Rolle explorer, claude)",
        "Befehl:         pip install python-flint",
        "Begründung des Agenten: Ich brauche FLINT",
        "Risiko:         mittel – ",
        "Kategorien:     Installation, Netzwerk",
        "Braucht eine Freigabe",
        "einmal  – nur diese Anfrage erlauben",
    ]:
        assert part in text, part
    # once: the next identical request asks again
    terminal.lines.put("nein")
    terminal.lines.put("")
    second = await inbox.handle(install())
    assert second.action is DENY and second.decided_by is DecidedBy.USER
    assert "Im Terminal abgelehnt" in second.reason


async def test_terminal_scope_agent_is_remembered(engine: PolicyEngine) -> None:
    terminal = FakeTerminal("agent")
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=5)
    assert (await inbox.handle(install())).scope is DecisionScope.AGENT
    prompts = len(terminal.prompts)
    again = await inbox.handle(install())
    assert again.action is ALLOW and len(terminal.prompts) == prompts  # no new prompt
    assert len(inbox.items()) == 1
    terminal.lines.put("run")
    other = await inbox.handle(install("explorer-2"))
    assert other.scope is DecisionScope.RUN


async def test_terminal_rejects_single_keys_and_empty_lines(engine: PolicyEngine) -> None:
    terminal = FakeTerminal("", "j", "y", "ja", "nein", "Bitte ohne FLINT.")
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=5)
    decision = await inbox.handle(install())
    assert decision.action is DENY
    assert decision.reason == "Im Terminal abgelehnt: Bitte ohne FLINT."
    assert terminal.text.count("verstehe ich nicht") == 4


async def test_terminal_always_needs_confirmation(engine: PolicyEngine) -> None:
    rules: list[AlwaysRule] = []
    terminal = FakeTerminal("immer", "ja", "immer", "ja immer")
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=5, on_rule=rules.append)
    decision = await inbox.handle(install())
    assert decision.scope is DecisionScope.ALWAYS and decision.decided_by is DecidedBy.USER
    assert "Nicht bestätigt" in terminal.text
    assert rules and rules[0].commands == ["pip install python-flint"]
    assert engine.evaluate(install("explorer-2")).action is ALLOW


async def test_terminal_closed_denies(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, decider=FakeTerminal(None).decider(), timeout=5)
    decision = await inbox.handle(install())
    assert decision.action is DENY and decision.decided_by is DecidedBy.FIXED_RULE
    assert TIMEOUT_REASON in decision.reason


async def test_timeout_denies_with_hint(engine: PolicyEngine) -> None:
    terminal = FakeTerminal()  # nobody answers
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=0.2)
    decision = await inbox.handle(install())
    assert decision.action is DENY
    assert decision.decided_by is DecidedBy.FIXED_RULE
    assert decision.reason == TIMEOUT_REASON
    assert "frage im Chat nach" in decision.reason
    item = inbox.items()[0]
    assert item.status is ApprovalStatus.EXPIRED and item.decision == decision
    assert item.deadline is not None and item.deadline > item.created_at
    assert "abgelaufen" in terminal.text
    events = [e["event"] for e in engine.log.entries()] if engine.log else []
    assert events[-2:] == ["open", "expired"]
    terminal.lines.put(None)  # release the reader thread


async def test_line_for_expired_request_is_not_applied_to_the_next(
    engine: PolicyEngine,
) -> None:
    terminal = FakeTerminal()
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=0.3)
    first = await inbox.handle(install())
    assert first.reason == TIMEOUT_REASON
    inbox.timeout = 5
    second = asyncio.create_task(inbox.handle(install("explorer-2")))
    await asyncio.sleep(0.05)
    terminal.lines.put("einmal")  # typed late, belongs to apr_0001 → dropped
    terminal.lines.put("nein")
    terminal.lines.put("")
    decision = await second
    assert decision.action is DENY
    assert "abgelaufenen Anfrage" in terminal.text


async def test_parallel_requests_are_asked_one_after_the_other(engine: PolicyEngine) -> None:
    terminal = FakeTerminal("einmal", "nein", "")
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=5)
    first, second = await asyncio.gather(
        inbox.handle(install()), inbox.handle(install("explorer-2"))
    )
    assert {first.action, second.action} == {ALLOW, DENY}
    assert terminal.text.count("Freigabe-Anfrage apr_") == 2


async def test_timeout_from_settings_duration(engine: PolicyEngine) -> None:
    from datetime import timedelta

    inbox = ApprovalInbox(engine, timeout=timedelta(minutes=20))
    assert inbox.timeout == 1200


# --------------------------------------------------------------------------------------------
# Inbox without terminal: GUI-like resolve, cancel, revoke, broken deciders
# --------------------------------------------------------------------------------------------


async def test_resolve_from_outside_like_the_gui(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    assert item.request is not None and item.risk is not None and item.summary
    decision = Decision(action=ALLOW, scope=DecisionScope.RUN, decided_by=DecidedBy.USER)
    inbox.resolve(item.id, decision)
    assert (await waiting).action is ALLOW
    with pytest.raises(ArrError, match="nicht mehr offen"):
        inbox.resolve(item.id, decision)
    # The run-wide grant covers the other agent, then is revoked.
    assert (await inbox.handle(install("explorer-2"))).action is ALLOW
    revoked = inbox.revoke(item.id)
    assert revoked.status is ApprovalStatus.REVOKED
    waiting = asyncio.create_task(inbox.handle(install("explorer-2")))
    await asyncio.sleep(0.01)
    assert len(inbox.pending()) == 1
    inbox.cancel_all()
    assert (await waiting).action is DENY


async def test_resolve_rejects_wrong_actions(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    with pytest.raises(ArrError):
        inbox.resolve(item.id, Decision(action=DecisionAction.ASK, decided_by=DecidedBy.USER))
    inbox.cancel(item.id)
    decision = await waiting
    assert decision.action is DENY and "hinfällig" in decision.reason
    assert inbox.get(item.id).status is ApprovalStatus.OBSOLETE
    with pytest.raises(ArrError):
        inbox.get("apr_9999")


async def test_revoke_only_revocable(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    inbox.resolve(item.id, Decision(action=ALLOW, decided_by=DecidedBy.USER))  # once
    await waiting
    with pytest.raises(ArrError, match="nicht zurückziehen"):
        inbox.revoke(item.id)


async def test_cancelled_wait_marks_item_obsolete(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert inbox.items()[0].status is ApprovalStatus.OBSOLETE


class BrokenDecider:
    async def decide(self, item: ApprovalItem) -> Decision | None:
        raise RuntimeError("kaputt")


class GatekeeperStub:
    async def decide(self, item: ApprovalItem) -> Decision | None:
        return Decision(action=DENY, decided_by=DecidedBy.GATEKEEPER, reason="Vollmacht: nein")


async def test_broken_decider_never_raises_and_times_out(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, decider=BrokenDecider(), timeout=0.1)
    decision = await inbox.handle(install())
    assert decision.reason == TIMEOUT_REASON


async def test_other_deciders_use_the_same_interface(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, decider=GatekeeperStub(), timeout=5)
    decision = await inbox.handle(install())
    assert decision.decided_by is DecidedBy.GATEKEEPER and decision.action is DENY


async def test_allowed_requests_skip_the_inbox(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, decider=FakeTerminal().decider(), timeout=5)
    request = PermissionRequest(run_id="r1", agent_id="explorer-1", tool="Bash", command="ls")
    assert (await inbox.handle(request)).action is ALLOW
    assert not inbox.items()


# --------------------------------------------------------------------------------------------
# A4: approvals.jsonl with decided_by, scope and reason, without secrets
# --------------------------------------------------------------------------------------------


async def test_log_records_the_whole_flow_without_secrets(engine: PolicyEngine) -> None:
    assert engine.log is not None
    terminal = FakeTerminal("agent")
    inbox = ApprovalInbox(engine, decider=terminal.decider(), timeout=5)
    request = PermissionRequest(
        run_id="r1",
        agent_id="explorer-1",
        tool="Bash",
        command=f"curl -H 'x-api-key: {SECRET}' https://api.example/v1?token={SECRET}",
        reason=f"Test mit ANTHROPIC_API_KEY={SECRET}",
    )
    await inbox.handle(request)
    inbox.revoke("apr_0001")
    entries = engine.log.entries()
    assert [e["event"] for e in entries] == ["decision", "open", "decided", "revoked"]
    for entry in entries:
        decision = entry["decision"]
        assert {"decided_by", "scope", "reason", "decided_at"} <= decision.keys()
        assert entry["request"]["agent_id"] == "explorer-1"
    assert entries[2]["decision"]["decided_by"] == "user"
    assert entries[2]["decision"]["scope"] == "agent"
    assert SECRET not in engine.log.path.read_text()
    assert SECRET not in terminal.text
    # A new inbox on the same log continues the numbering.
    assert ApprovalInbox(engine)._counter == 1


def test_format_item_redacts_and_is_german(engine: PolicyEngine) -> None:
    assessment = engine.assess(install())
    item = ApprovalItem(
        id="apr_0007",
        type="permission",
        run_id="r1",
        request=assessment.request.model_copy(update={"reason": f"Key {SECRET}"}),
        risk=assessment.decision.risk,
        summary=assessment.decision.reason,
    )
    text = format_item(item)
    assert SECRET not in text and "Freigabe-Anfrage apr_0007" in text
    assert "Antworte mit einem ganzen Wort" in text


def test_reader_thread_is_daemon(engine: PolicyEngine) -> None:
    # The terminal reader must never keep the process alive after a timeout.
    started = threading.Event()

    def read_line(prompt: str) -> str:
        started.set()
        threading.Event().wait(0.5)
        return "einmal"

    decider = TerminalDecider(read_line=read_line, write=lambda _: None)

    async def run() -> None:
        item = ApprovalItem(id="apr_0001", type="permission", run_id="r1", request=install())
        task = asyncio.create_task(decider.decide(item))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    threads = [t for t in threading.enumerate() if t.name == "arr-approval-input"]
    assert all(t.daemon for t in threads)

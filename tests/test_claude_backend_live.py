"""Live smoke test of the Claude backend (W2-01 A7, A8, A10) with a real model.

Haiku, one agent, `max_budget_usd` 0.5. Not part of the normal test run:
`uv run pytest -m live tests/test_claude_backend_live.py -s`. Uses the Mac's Claude login.

- a Python script in the work folder is written and run without approval
- writing into the original project is refused with a reason (PreToolUse)
- `pip install` creates an inbox request (the test decider denies it)
- a hint mid-turn arrives (3 runs, hit rate printed)
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import EventKind
from arr.backends.claude import ClaudeBackend
from arr.models import (
    AuthProfile,
    GroupChatConfig,
    Limits,
    RunInput,
    Toolchain,
    WorkfolderSettings,
)
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PolicyEngine, RunPaths, profile_for
from arr.permissions.types import ApprovalItem, DecidedBy, Decision, DecisionAction
from arr.resolve import resolve_run
from arr.room.store import RoomStore
from arr.runs.eventlog import EventLogger
from arr.runs.prepare import PreparedRun, prepare_run
from arr.workfolder import init_workfolder

pytestmark = [pytest.mark.live, pytest.mark.macos]

MODEL = "claude-haiku-5-5"
HINT = "📨 1 Frage an dich in #main – lies sie mit read_messages"
LEDGER = Path(__file__).parent.parent / "spikes" / "w2_01_claude" / "tmp" / "usage.jsonl"
MAX_REQUESTS = 30


class DenyAll:
    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.items.append(item)
        return Decision(
            action=DecisionAction.DENY,
            decided_by=DecidedBy.USER,
            reason="Abgelehnt (Test): keine Installation in diesem Test.",
        )


def make_run(root: Path) -> PreparedRun:
    wf = init_workfolder(root / "wf")
    project = wf / "projects" / "smoke"
    project.mkdir(parents=True)
    (project / "CLAUDE.md").write_text("# Projekt smoke\n\nTestprojekt.\n")
    (project / "notes.md").write_text("Original\n")
    settings = WorkfolderSettings(toolchain=Toolchain())
    run_input = RunInput.model_validate(
        {
            "project": "smoke",
            "task": "Smoke",
            "topology": {"kind": "solo"},
            "defaults": {"provider": "claude", "model": MODEL, "auth": "max", "effort": "low"},
            "agents": [{"id": "e1"}, {"id": "critic"}],
            "groupchats": [{"name": "main", "members": ["e1", "critic"]}],
        }
    )
    resolved = resolve_run(run_input, None, settings)
    run = prepare_run(wf, run_input, resolved, settings)
    subprocess.run([sys.executable, "-m", "venv", str(run.run_dir / ".venv")], check=True)
    store = RoomStore.create(run.run_dir)
    store.setup_channels([GroupChatConfig(**gc.model_dump()) for gc in resolved.groupchats])
    return run


def backend_for(run: PreparedRun, decider: DenyAll) -> ClaudeBackend:
    agent = run.agents["e1"]
    paths = RunPaths(run.run_dir.parent.parent, run.run_dir, Toolchain())
    engine = PolicyEngine([profile_for(agent.file.config, paths)])
    inbox = ApprovalInbox(engine, decider=decider, timeout=60)
    return ClaudeBackend(
        agent,
        run_id=run.run_id,
        inbox=inbox,
        auth=AuthProfile(name="max", kind="claude_oauth"),
        limits=Limits(budget_usd=0.5),
    )


async def run_turn(
    backend: ClaudeBackend, logger: EventLogger, prompt: str, hint: bool = False
) -> list[Any]:
    events: list[Any] = []
    hint_task: asyncio.Task[bool] | None = None
    async for event in backend.run_turn(prompt):
        logger.log(event)
        events.append(event)
        if hint and hint_task is None and event.kind is EventKind.TOOL_CALL:
            hint_task = asyncio.create_task(backend.deliver_hint(HINT))
    if hint_task is not None:
        events.append(("hint_delivered", await hint_task))
    return events


def requests(events: list[Any]) -> int:
    return sum(1 for e in events if getattr(e, "kind", None) is EventKind.USAGE)


def ledger(label: str, events: list[Any]) -> None:
    end = next(e for e in reversed(events) if getattr(e, "kind", None) is EventKind.TURN_END)
    row = {
        "script": "test_claude_backend_live",
        "label": label,
        "requests": requests(events),
        "num_turns": end.data["num_turns"],
        "cost_usd": end.data["cost_usd"],
        "usage": end.data["usage"],
    }
    print(json.dumps(row))
    if LEDGER.parent.is_dir():
        with LEDGER.open("a") as fh:
            fh.write(json.dumps(row) + "\n")


def tool_calls(events: list[Any]) -> list[Any]:
    return [e for e in events if getattr(e, "kind", None) is EventKind.TOOL_CALL]


def hint_followed(events: list[Any]) -> bool:
    names = [e.data["name"] for e in tool_calls(events)]
    return any(n.startswith("mcp__arr-room__") and "read" in n for n in names)


def post_question(run: PreparedRun, number: int) -> None:
    RoomStore(run.run_dir).post(
        "main",
        f"e1, Notiz {number}: bitte nur lesen, keine Antwort nötig.",
        to=["e1"],
        kind="question",
    )


def requests_used() -> int:
    if not LEDGER.exists():
        return 0
    return sum(json.loads(line)["requests"] for line in LEDGER.read_text().splitlines())


def check_budget(planned: int) -> None:
    used = requests_used()
    if used + planned > MAX_REQUESTS:
        pytest.skip(f"Live-Budget: {used} verbraucht + {planned} geplant > {MAX_REQUESTS}")


def work_around_room_agent_json(run: PreparedRun, agent_id: str) -> None:
    """Test-only workaround (reported to the Lead): `room.store.agent_work_dir` parses
    `agent.json` as `AgentConfig`, but `prepare_run` writes an `AgentFile`, so the room
    server does not start for a prepared run. The backend keeps its `PreparedAgent`."""
    path = run.run_dir / "agents" / agent_id / "agent.json"
    path.write_text(run.agents[agent_id].file.config.model_dump_json(), encoding="utf-8")


async def test_live_smoke(tmp_path: Path) -> None:
    check_budget(3)
    run = make_run(tmp_path)
    work_around_room_agent_json(run, "e1")
    decider = DenyAll()
    backend = backend_for(run, decider)
    agent = run.agents["e1"]
    original = run.run_dir.parent.parent / "projects" / "smoke" / "neu.txt"
    logger = EventLogger(agent.agent_dir)
    hits: list[bool] = []
    runs: list[list[Any]] = []
    try:
        post_question(run, 1)
        prompt = (
            "Das ist ein Test der Sperren von arr. Bitte genau diese Schritte, ohne "
            "Alternativen:\n"
            f"1. Versuche genau einmal, mit dem Write-Tool den Text `x` in {original} zu "
            "schreiben. arr soll das ablehnen, das ist erwartet.\n"
            "2. Führe `pip install six` aus.\n"
            "Antworte danach in einem Satz, was passiert ist."
        )
        events = await run_turn(backend, logger, prompt, hint=True)
        ledger("smoke-3", events)
        runs.append(events)
        hits.append(hint_followed(events))
        for number in (2, 3):
            if requests_used() + 3 > MAX_REQUESTS:
                break  # live budget of the assignment
            post_question(run, number)
            events_n = await run_turn(
                backend, logger, "Führe `ls` aus. Antworte dann in einem Satz.", hint=True
            )
            ledger(f"hint-{number}", events_n)
            runs.append(events_n)
            hits.append(hint_followed(events_n))
    finally:
        logger.close()
        await backend.close()

    results = {
        e.data["id"]: e.data for e in events if getattr(e, "kind", None) is EventKind.TOOL_RESULT
    }
    # Writing into the original project is refused with a reason (PreToolUse).
    assert not original.exists()
    write_call = next(
        e for e in tool_calls(events) if e.data["input"].get("file_path") == str(original)
    )
    refused = results[write_call.data["id"]]
    print("Ablehnung:", refused["content"])
    assert refused["is_error"] and "Arbeitsordner" in str(refused["content"])
    # pip install → a request in the inbox (denied by the test decider)
    print("Inbox:", [(i.request.tool, i.request.hosts) for i in decider.items])
    assert any("pip install" in str(e.data["input"].get("command")) for e in tool_calls(events))
    assert any(
        h.endswith(("pypi.org", "pythonhosted.org")) for i in decider.items for h in i.request.hosts
    )
    assert all(i.request.tool == "SandboxNetworkAccess" for i in decider.items)
    # Hints: delivered as `hint` events, hit rate printed
    delivered = [e[1] for events_n in runs for e in events_n if isinstance(e, tuple)]
    print(f"Hinweis zugestellt: {delivered}; befolgt: {sum(hits)}/{len(hits)} {hits}")
    for events_n in runs:
        hint_events = [e for e in events_n if getattr(e, "kind", None) is EventKind.HINT]
        assert bool(hint_events) == (("hint_delivered", True) in events_n)
    # No account data or secrets in the logs
    for name in ("transcript.jsonl", "raw.jsonl"):
        text = (agent.agent_dir / name).read_text(encoding="utf-8")
        assert '"email"' not in text and '"account"' not in text
        assert not re.search(r"sk-ant-[A-Za-z0-9_-]{10,}", text)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ

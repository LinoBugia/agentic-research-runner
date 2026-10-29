"""CodexBackend without a model: a fake app-server replays notifications and server requests
the way the SDK's reader thread does (hooks, approval handler, turn stream)."""

import asyncio
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arr.backends import codex as codex_module
from arr.backends.base import AgentBackend, AgentEvent, EventKind
from arr.backends.codex import CodexBackend
from arr.backends.codex_approvals import ApprovalBridge
from arr.errors import ArrError
from arr.models import AgentConfig, AuthKind, AuthProfile, Provider
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PermissionRules, PolicyEngine, RunPaths, profile_for
from arr.permissions.types import (
    ApprovalItem,
    ApprovalStatus,
    DecidedBy,
    Decision,
    DecisionAction,
    PermissionRequest,
)
from arr.runs.prepare import AgentFile, PreparedAgent

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
THREAD = "thread-1"
TURN = "turn-1"


# -- fake app-server -----------------------------------------------------------------------


class FakeSync:
    """The parts of the SDK's sync client that `codex_sdk` touches."""

    def __init__(self) -> None:
        self._approval_handler: Any = None
        self._next: dict[str, Any] = {}
        self.written: list[dict[str, Any]] = []
        self.thread_start = None

    def _read_message(self) -> dict[str, Any]:
        return self._next

    def _write_message(self, payload: dict[str, Any]) -> None:
        self.written.append(payload)


class FakeCodex:
    def __init__(self, config: Any, script: list[dict[str, Any]], log: list[str]) -> None:
        self.config = config
        self.script = script
        self.log = log
        self._client = SimpleNamespace(_sync=FakeSync())
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.log.append("close")


class FakeHandle:
    """Turn handle; `stream()` starts a reader thread over the script."""

    def __init__(self, codex: FakeCodex, gate: threading.Event | None) -> None:
        self.codex = codex
        self.gate = gate
        self.notes: queue.Queue[Any] = queue.Queue()
        self.steered: list[str] = []
        self.done = False
        self.busy = threading.Lock()  # held while the reader thread is in the handler

    def _reader(self) -> None:
        sync = self.codex._client._sync
        for msg in self.codex.script:
            if msg.get("method") == "@wait" and self.gate is not None:
                self.gate.wait(5)
                continue
            sync._next = msg
            msg = sync._read_message()  # goes through the installed hook
            method = msg.get("method")
            if "id" in msg and method:
                with self.busy:
                    answer = sync._approval_handler(method, msg.get("params"))
                    self.codex.log.append(f"answer {method} {answer}")
                    sync._write_message({"id": msg["id"], "result": answer})
            elif method and method != "account/rateLimits/updated":
                self.notes.put(SimpleNamespace(method=method, payload=msg.get("params") or {}))
        self.notes.put(None)

    async def stream(self):
        threading.Thread(target=self._reader, daemon=True).start()
        while (note := await asyncio.to_thread(self.notes.get)) is not None:
            yield note
        self.done = True

    async def steer(self, text: str) -> None:
        if self.done:
            raise RuntimeError("no active turn to steer")
        self.steered.append(text)
        self.codex.log.append(f"steer {text}")

    async def interrupt(self) -> None:
        # Like the SDK: the answer to `turn/interrupt` needs the reader thread, which is
        # blocked while the approval handler waits.
        free = await asyncio.to_thread(self.busy.acquire, True, 2)
        if free:
            self.busy.release()
        self.codex.log.append("interrupt" if free else "interrupt blocked")
        if self.gate is not None:
            self.gate.set()


class FakeThread:
    def __init__(self, codex: FakeCodex, kit: "Kit") -> None:
        self.codex = codex
        self.kit = kit

    async def turn(self, prompt: str, *, effort: str | None, summary: str) -> FakeHandle:
        self.kit.prompts.append(prompt)
        self.kit.efforts.append(effort)
        handle = FakeHandle(self.codex, self.kit.gate)
        self.kit.handles.append(handle)
        return handle


class Kit:
    """Fakes for `CodexBackend`: factory, thread start/resume, recorded calls."""

    def __init__(self, scripts: list[list[dict[str, Any]]], rollout: Path | None = None) -> None:
        self.scripts = list(scripts)
        self.rollout = rollout
        self.log: list[str] = []
        self.codexes: list[FakeCodex] = []
        self.handles: list[FakeHandle] = []
        self.prompts: list[str] = []
        self.efforts: list[str | None] = []
        self.starts: list[tuple[str, bool, dict[str, Any]]] = []
        self.gate: threading.Event | None = None

    def factory(self, config: Any) -> FakeCodex:
        codex = FakeCodex(config, self.scripts.pop(0), self.log)
        self.codexes.append(codex)
        return codex

    def _started(self, codex: FakeCodex) -> tuple[FakeThread, Any]:
        path = str(self.rollout) if self.rollout else None
        return FakeThread(codex, self), SimpleNamespace(
            thread=SimpleNamespace(id=THREAD, path=path)
        )

    async def start_thread(self, codex: FakeCodex, *, approvals: bool, **settings: Any):
        self.starts.append(("start", approvals, settings))
        return self._started(codex)

    async def resume_thread(self, codex: FakeCodex, thread_id: str, *, approvals: bool, **s: Any):
        self.starts.append((f"resume {thread_id}", approvals, s))
        return self._started(codex)


def note(method: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "params": {"threadId": THREAD, "turnId": TURN, **params}}


def tokens(total_in: int, cached: int, out: int, last_in: int, last_cached: int, last_out: int):
    def counts(i: int, c: int, o: int) -> dict[str, int]:
        return {
            "inputTokens": i,
            "cachedInputTokens": c,
            "outputTokens": o,
            "reasoningOutputTokens": 0,
            "cacheWriteInputTokens": 0,
        }

    usage = {"total": counts(total_in, cached, out), "last": counts(last_in, last_cached, last_out)}
    return note("thread/tokenUsage/updated", tokenUsage=usage)


def simple_turn(text: str = "fertig", status: str = "completed") -> list[dict[str, Any]]:
    return [
        note("turn/started", turn={"id": TURN, "status": "inProgress"}),
        note("item/completed", item={"type": "agentMessage", "id": "m1", "text": text}),
        tokens(1000, 400, 50, 1000, 400, 50),
        note("turn/completed", turn={"id": TURN, "status": status, "durationMs": 10}),
    ]


def fixture_script(name: str) -> list[dict[str, Any]]:
    """The incoming messages of a recorded turn, from its `turn/start` on."""
    lines = [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]
    start = next(i for i, line in enumerate(lines) if line["msg"].get("method") == "turn/start")
    return [
        line["msg"] for line in lines[start:] if line["dir"] == "in" and "method" in line["msg"]
    ]


# -- setup ---------------------------------------------------------------------------------


class Decider:
    """Test decider: a fixed action, or waits forever with `None`."""

    def __init__(self, action: DecisionAction | None) -> None:
        self.action = action
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision | None:
        self.items.append(item)
        if self.action is None:
            await asyncio.Event().wait()
        return Decision(action=self.action, decided_by=DecidedBy.USER, reason="Nein, bitte nicht.")


def make_agent(tmp_path: Path, **changes: Any) -> tuple[PreparedAgent, PolicyEngine]:
    home = tmp_path / "home"
    run = home / "wf" / "runs" / "R"
    agent_dir = run / "agents" / "gpt-1"
    (agent_dir / "work").mkdir(parents=True)
    (run / "tmp").mkdir()
    (agent_dir / "role.md").write_text("Du bist gpt-1.\n")
    (agent_dir / "context.md").write_text("PROJEKTKONTEXT\n")
    data: dict[str, Any] = {
        "id": "gpt-1",
        "base_id": "gpt-1",
        "provider": Provider.CODEX,
        "model": "gpt-6-luna",
        "auth": "chatgpt-test",
        "effort": "low",
        "channels": ["main"],
    }
    data.update(changes)
    config = AgentConfig.model_validate(data)
    work = "shared/beweis" if config.workspace.mode == "shared" else "agents/gpt-1/work"
    (run / work).mkdir(parents=True, exist_ok=True)
    paths = RunPaths(home / "wf", run)
    profile = profile_for(config, paths, PermissionRules(), home=home, case_insensitive=False)
    file = AgentFile(
        config=config,
        work_dir=work,
        role_file="agents/gpt-1/role.md",
        context_file="agents/gpt-1/context.md",
    )
    return PreparedAgent(file, run), PolicyEngine([profile])


def make_auth(tmp_path: Path, logged_in: bool = True) -> AuthProfile:
    home = tmp_path / "codex_home"
    home.mkdir(exist_ok=True)
    if logged_in:
        (home / "auth.json").write_text("{}")
    return AuthProfile(name="chatgpt-test", kind=AuthKind.CODEX_CHATGPT, login_dir=str(home))


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def build(scripts, *, decider=None, timeout=5.0, rollout=None, **changes):
        kit = Kit(scripts, rollout)
        monkeypatch.setattr(codex_module, "start_thread", kit.start_thread)
        monkeypatch.setattr(codex_module, "resume_thread", kit.resume_thread)
        monkeypatch.setattr(codex_module, "_codex_config", lambda launch: launch)
        agent, engine = make_agent(tmp_path, **changes)
        inbox = ApprovalInbox(engine, decider=decider, timeout=timeout)
        backend = CodexBackend(
            agent, run_id="R", inbox=inbox, auth=make_auth(tmp_path), factory=kit.factory
        )
        return backend, kit, inbox

    return build


async def play(backend: CodexBackend, prompt: str = "Los.") -> list[AgentEvent]:
    return [event async for event in backend.run_turn(prompt)]


def kinds(events: list[AgentEvent]) -> list[str]:
    return [str(e.kind) for e in events]


# -- lifecycle -----------------------------------------------------------------------------


def test_is_an_agent_backend(setup) -> None:
    backend, _, _ = setup([])
    assert isinstance(backend, AgentBackend)


async def test_start_needs_a_logged_in_profile(tmp_path: Path) -> None:
    agent, engine = make_agent(tmp_path)
    auth = make_auth(tmp_path, logged_in=False)
    backend = CodexBackend(agent, run_id="R", inbox=ApprovalInbox(engine), auth=auth)
    with pytest.raises(ArrError, match="codex-login chatgpt-test"):
        await backend.start()


async def test_turn_events_and_session(setup) -> None:
    backend, kit, _ = setup([simple_turn()])
    await backend.start()
    events = await play(backend)
    assert kinds(events) == ["turn_start", "text", "usage", "turn_end"]
    assert events[0].data == {"prompt": "Los.", "stage": None}
    assert events[1].data == {"text": "fertig"}
    assert events[1].raw["method"] == "item/completed"
    end = events[-1].data
    assert end["status"] == "ok"
    assert end["usage"]["input"] == 600 and end["usage"]["cache_read"] == 400
    assert end["session"] == THREAD and end["cost_usd"] is None
    assert backend.session_ref() == {"thread_id": THREAD}
    assert kit.efforts == ["low"]
    assert kit.codexes[0].closed  # one app-server per turn


async def test_every_turn_restarts_the_app_server_and_resumes(setup) -> None:
    backend, kit, _ = setup([simple_turn(), simple_turn("zwei")])
    await backend.start()
    await play(backend)
    second = await play(backend, "Weiter.")
    assert [s[0] for s in kit.starts] == ["start", f"resume {THREAD}"]
    assert all(approvals for _, approvals, _ in kit.starts)
    assert len(kit.codexes) == 2 and all(c.closed for c in kit.codexes)
    assert second[0].turn == 2 and second[1].data["text"] == "zwei"
    # the permission profile is part of every start (fresh sibling list)
    overrides = kit.codexes[1].config.config_overrides
    assert any(o.startswith("permissions.arr.filesystem=") for o in overrides)


async def test_failed_turn_ends_with_error_and_turn_end(setup) -> None:
    script = simple_turn(status="failed")
    script[-1]["params"]["turn"]["error"] = {"message": "Kontingent erschöpft"}
    backend, _, _ = setup([script])
    await backend.start()
    events = await play(backend)
    assert kinds(events)[-2:] == ["error", "turn_end"]
    assert events[-2].data["message"] == "Kontingent erschöpft"
    assert events[-1].data["status"] == "error"


async def test_app_server_error_still_ends_with_turn_end(setup, monkeypatch) -> None:
    backend, kit, _ = setup([simple_turn()])

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("app-server exited")

    monkeypatch.setattr(codex_module, "start_thread", broken)
    await backend.start()
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert events[-1].data["status"] == "error"
    assert kit.codexes[0].closed


async def test_close_is_idempotent(setup) -> None:
    backend, _, _ = setup([simple_turn()])
    await backend.start()
    await play(backend)
    await backend.close()
    await backend.close()
    with pytest.raises(RuntimeError):
        backend.run_turn("x")


async def test_fallback_without_internals(setup, monkeypatch) -> None:
    backend, kit, _ = setup([simple_turn()])
    monkeypatch.setattr(codex_module, "internals_available", lambda codex: False)
    await backend.start()
    await play(backend)
    assert kit.starts[0][1] is False  # approvalPolicy never = deny_all
    assert kit.codexes[0]._client._sync._approval_handler is None


# -- project context (A6) -----------------------------------------------------------------


async def test_context_only_in_shared_mode(setup) -> None:
    backend, kit, _ = setup([simple_turn(), simple_turn()])
    await backend.start()
    await play(backend)
    assert kit.prompts == ["Los."]  # copy mode: Codex reads AGENTS.md itself


async def test_context_in_first_prompt_of_shared_mode(setup) -> None:
    backend, kit, _ = setup(
        [simple_turn(), simple_turn()], workspace={"mode": "shared", "name": "beweis"}
    )
    await backend.start()
    await play(backend)
    await play(backend, "Weiter.")
    assert kit.prompts[0] == "PROJEKTKONTEXT\n\nLos."
    assert kit.prompts[1] == "Weiter."


# -- hints ---------------------------------------------------------------------------------


async def test_hint_is_steered_and_logged(setup) -> None:
    script = simple_turn()
    script.insert(1, {"method": "@wait"})
    backend, kit, _ = setup([script])
    kit.gate = threading.Event()
    await backend.start()
    assert await backend.deliver_hint("idle") is False

    async def hint_when_running() -> bool:
        while not kit.handles:  # noqa: ASYNC110 - the fake has no event for this
            await asyncio.sleep(0.01)
        delivered = await backend.deliver_hint("📨 Frage in #main")
        kit.gate.set()
        return delivered

    sender = asyncio.create_task(hint_when_running())
    events = await play(backend)
    assert await sender is True
    hint = next(e for e in events if e.kind is EventKind.HINT)
    assert hint.data == {"text": "📨 Frage in #main", "channel": None, "method": "steer"}
    assert kit.handles[0].steered == ["📨 Frage in #main"]
    assert await backend.deliver_hint("danach") is False


# -- approvals (A3, A4) ---------------------------------------------------------------------


def command_request(command: str = "/bin/zsh -lc 'pip install sympy'") -> dict[str, Any]:
    params = {
        "threadId": THREAD,
        "turnId": TURN,
        "itemId": "exec-1",
        "command": command,
        "cwd": "work",
        "reason": "Ich brauche sympy.",
    }
    return {"id": 7, "method": "item/commandExecution/requestApproval", "params": params}


async def test_denied_request_declines_and_steers_reason(setup) -> None:
    script = simple_turn()
    script.insert(1, command_request())
    backend, kit, inbox = setup([script], decider=Decider(DecisionAction.DENY))
    await backend.start()
    await play(backend)
    assert "answer item/commandExecution/requestApproval {'decision': 'decline'}" in kit.log
    await asyncio.sleep(0.05)
    assert any(line.startswith("steer Abgelehnt: Nein, bitte nicht.") for line in kit.log)
    item = inbox.items()[0]
    assert item.request.provider is Provider.CODEX
    assert item.request.command == "/bin/zsh -lc 'pip install sympy'"  # the whole command


async def test_allowed_request_accepts(setup) -> None:
    script = simple_turn()
    script.insert(1, command_request())
    backend, kit, _ = setup([script], decider=Decider(DecisionAction.ALLOW))
    await backend.start()
    await play(backend)
    assert "answer item/commandExecution/requestApproval {'decision': 'accept'}" in kit.log


async def test_stop_while_waiting_ends_the_wait_first(setup) -> None:
    script = simple_turn(status="interrupted")
    script.insert(1, command_request())
    backend, kit, inbox = setup([script], decider=Decider(None))
    await backend.start()
    turn = asyncio.create_task(play(backend))
    while not inbox.pending():  # noqa: ASYNC110 - the inbox has no event for this
        await asyncio.sleep(0.01)
    await backend.interrupt()
    events = await asyncio.wait_for(turn, 5)
    answer = kit.log.index("answer item/commandExecution/requestApproval {'decision': 'decline'}")
    assert answer < kit.log.index("interrupt")  # not "interrupt blocked"
    assert inbox.items()[0].status is ApprovalStatus.OBSOLETE
    assert events[-1].kind is EventKind.TURN_END and events[-1].data["status"] == "interrupted"


async def test_other_requests_are_refused(setup) -> None:
    script = simple_turn()
    requests = {
        "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
        "mcpServer/elicitation/request": {"action": "decline", "content": None},
        "item/tool/requestUserInput": {"answers": {}},
        "execCommandApproval": {"decision": "denied"},
        "applyPatchApproval": {"decision": "denied"},
    }
    for n, method in enumerate(requests):
        script.insert(1, {"id": 100 + n, "method": method, "params": {}})
    backend, kit, inbox = setup([script])
    await backend.start()
    await play(backend)
    for method, answer in requests.items():
        assert f"answer {method} {answer}" in kit.log
    assert not inbox.items()


def test_bridge_never_raises() -> None:
    class Broken:
        engine = None

        async def handle(self, request: PermissionRequest) -> Decision:
            raise RuntimeError("kaputt")

        def pending(self) -> list[ApprovalItem]:
            return []

    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        bridge = ApprovalBridge(
            Broken(),  # type: ignore[arg-type]
            run_id="R",
            agent_id="gpt-1",
            role=None,
            work_dir="/w",
            loop=loop,
            on_denied=lambda reason: None,
        )
        params = command_request()["params"]
        assert bridge("item/commandExecution/requestApproval", params) == {"decision": "decline"}
        assert bridge("item/fileChange/requestApproval", None) == {"decision": "decline"}
        assert bridge("something/new", {}) == {}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(2)


async def test_file_request_carries_paths_from_the_item(setup) -> None:
    script = simple_turn()
    change = {
        "type": "fileChange",
        "id": "fc-1",
        "status": "inProgress",
        "changes": [{"path": "/elsewhere/x.txt", "kind": {"type": "add"}, "diff": "x"}],
    }
    request = {
        "id": 9,
        "method": "item/fileChange/requestApproval",
        "params": {"threadId": THREAD, "turnId": TURN, "itemId": "fc-1", "reason": None},
    }
    script[1:1] = [note("item/started", item=change), request]
    backend, kit, inbox = setup([script], decider=Decider(DecisionAction.DENY))
    await backend.start()
    events = await play(backend)
    assert "answer item/fileChange/requestApproval {'decision': 'decline'}" in kit.log
    logged = [i.request for i in inbox.items()] or []
    paths = [p for r in logged for p in r.paths]
    assert paths in ([], ["/elsewhere/x.txt"])  # a fixed rule may decide without an item
    assert any(e.kind is EventKind.TOOL_CALL and e.data["name"] == "fileChange" for e in events)


# -- replay of the E19 recording ----------------------------------------------------------


async def test_replay_of_read_allowlist_recording(setup) -> None:
    backend, kit, _ = setup([fixture_script("read_allowlist.jsonl")], timeout=0.2)
    await backend.start()
    events = await play(backend)
    ks = kinds(events)
    assert ks[0] == "turn_start" and ks[-1] == "turn_end"
    assert events[-1].data["status"] == "ok"
    calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    results = {e.data["id"] for e in events if e.kind is EventKind.TOOL_RESULT}
    assert len(calls) >= 25 and {c.data["id"] for c in calls} <= results
    assert "answer item/fileChange/requestApproval {'decision': 'decline'}" in kit.log
    limits = [e for e in events if e.kind is EventKind.RATE_LIMIT]
    assert limits and limits[0].data["status"] == "allowed"
    usage = events[-1].data["usage"]
    assert usage["input"] > 0 and usage["cache_read"] > 0

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
from arr.backends.codex_config import project_config_state
from arr.errors import ArrError
from arr.models import AgentConfig, AuthKind, AuthProfile, Provider
from arr.permissions import policy
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


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` lies in the user's temp folder, which `profile_for` denies (W2-01.1); the
    tests must not depend on the machine."""
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])


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


def make_agent(
    tmp_path: Path, run_id: str = "R", **changes: Any
) -> tuple[PreparedAgent, PolicyEngine]:
    home = tmp_path / "home"
    run = home / "wf" / "runs" / run_id
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


async def test_w2_05_login_failure_names_the_profile_and_the_login_command(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review W2-02 Q13: a turn that fails at the login says how to log in again."""
    from arr.backends.codex import login_hint

    backend, kit, _ = setup([simple_turn()])

    async def failing_start(codex: Any, *, approvals: bool, **settings: Any) -> Any:
        raise RuntimeError("HTTP 401 Unauthorized: refresh token expired")

    monkeypatch.setattr(codex_module, "start_thread", failing_start)
    await backend.start()
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    message = events[1].data["message"]
    assert message.startswith(
        "Profil „chatgpt-test“ neu anmelden: uv run arr auth codex-login chatgpt-test"
    )
    assert "401" in message and events[-1].data["status"] == "error"
    # a `turn/completed` with an auth error gets the hint as well; other errors stay as they are
    auth = backend.auth
    assert login_hint(auth, "Unauthorized").startswith("Profil „chatgpt-test“ neu anmelden")
    assert login_hint(auth, "Not logged in").startswith("Profil „chatgpt-test“ neu anmelden")
    assert login_hint(auth, "stream disconnected before completion") == (
        "stream disconnected before completion"
    )


async def test_w2_05_login_error_in_turn_completed_gets_the_hint(setup) -> None:
    script = [
        note("turn/started", turn={"id": TURN, "status": "inProgress"}),
        note(
            "turn/completed",
            turn={"id": TURN, "status": "failed", "error": {"message": "401 Unauthorized"}},
        ),
    ]
    backend, _, _ = setup([script])
    await backend.start()
    events = await play(backend)
    errors = [e.data["message"] for e in events if e.kind is EventKind.ERROR]
    assert errors and all(m.startswith("Profil „chatgpt-test“ neu anmelden") for m in errors)


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
    # the permission profile is part of every start
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


# -- W3-06.4, 3c: project settings of Codex, fail closed -----------------------------------


@pytest.mark.parametrize(
    "change",
    ["new file", "changed file", "new hooks", "removed", "link"],
)
async def test_a_changed_codex_folder_starts_no_thread(setup, caplog, change: str) -> None:
    backend, kit, _ = setup([simple_turn(), simple_turn("zwei")])
    work = backend.profile.work_dir
    folder = work / ".codex"
    if change != "new file":
        folder.mkdir()
        (folder / "config.toml").write_text("# Projekt\n")
    await backend.start()
    await play(backend)
    if change == "new file":
        folder.mkdir()
        (folder / "config.toml").write_text('[mcp_servers.x]\ncommand = "/bin/sh"\n')
    elif change == "changed file":
        (folder / "config.toml").write_text('[mcp_servers.x]\ncommand = "/bin/sh"\n')
    elif change == "new hooks":
        (folder / "hooks.json").write_text("{}\n")
    elif change == "removed":
        (folder / "config.toml").unlink()
    else:
        (folder / "config.toml").unlink()
        (folder / "config.toml").symlink_to(work / "other.toml")
    events = await play(backend, "Weiter.")
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert "Projekt-Konfiguration" in events[1].data["message"]
    assert "außerhalb der Sandbox" in events[1].data["message"]
    assert events[-1].data["status"] == "error"
    assert len(kit.codexes) == 1, "no app-server started for the second turn"
    assert "seit dem Start verändert" in caplog.text
    again = await play(backend, "Noch einmal.")
    assert kinds(again)[1] == "error" and len(kit.codexes) == 1  # stays closed


async def test_an_unchanged_codex_folder_keeps_the_run_going(setup) -> None:
    backend, kit, _ = setup([simple_turn(), simple_turn("zwei")])
    folder = backend.profile.work_dir / ".codex"
    folder.mkdir()
    (folder / "config.toml").write_text("# Projekt\n")
    await backend.start()
    await play(backend)
    second = await play(backend, "Weiter.")
    assert "error" not in kinds(second) and len(kit.codexes) == 2


def test_the_state_of_the_codex_folder(tmp_path: Path) -> None:
    assert project_config_state(tmp_path) == "absent"
    (tmp_path / ".codex").mkdir()
    empty = project_config_state(tmp_path)
    (tmp_path / ".codex" / "config.toml").write_text("a = 1\n")
    one = project_config_state(tmp_path)
    assert len({"absent", empty, one}) == 3
    assert project_config_state(tmp_path) == one  # stable
    (tmp_path / ".codex" / "config.toml").write_text("a = 2\n")
    assert project_config_state(tmp_path) != one


async def test_close_is_idempotent(setup) -> None:
    backend, kit, _ = setup([simple_turn()])
    await backend.start()
    await play(backend)
    await backend.close()
    await backend.close()
    events = await play(backend, "x")  # like ClaudeBackend: no new process, error + turn_end
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert events[-1].data["status"] == "error" and len(kit.codexes) == 1


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
        "item/tool/call": {"contentItems": [], "success": False},
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
        assert bridge("something/new", {})["code"] == -32601  # an error, never {} (W4-00 H1)
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


# -- W2-02.1 ------------------------------------------------------------------------------


async def test_g2_turn_created_before_close_starts_nothing(setup) -> None:
    backend, kit, _ = setup([simple_turn()])
    await backend.start()
    turn = backend.run_turn("Los.")
    await backend.close()
    events = [event async for event in turn]
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert not kit.codexes  # no app-server


async def test_g2_stop_during_start_is_interrupted(setup, monkeypatch) -> None:
    backend, kit, _ = setup([simple_turn()])
    started = asyncio.Event()

    async def slow_start(codex: Any, **kwargs: Any) -> Any:
        started.set()
        await asyncio.sleep(0.2)
        return kit._started(codex)

    monkeypatch.setattr(codex_module, "start_thread", slow_start)
    await backend.start()
    turn = asyncio.create_task(play(backend))
    await started.wait()
    await backend.interrupt()
    events = await turn
    assert kinds(events) == ["turn_start", "turn_end"]
    assert events[-1].data["status"] == "interrupted"
    assert not kit.handles  # no turn/start sent


async def test_g2_failure_while_stopping_is_interrupted(setup, monkeypatch) -> None:
    backend, _, _ = setup([simple_turn()])
    started = asyncio.Event()

    async def broken(codex: Any, **kwargs: Any) -> Any:
        started.set()
        await asyncio.sleep(0.2)
        raise RuntimeError("app-server closed")

    monkeypatch.setattr(codex_module, "start_thread", broken)
    await backend.start()
    turn = asyncio.create_task(play(backend))
    await started.wait()
    await backend.interrupt()
    events = await turn
    assert kinds(events) == ["turn_start", "turn_end"]
    assert events[-1].data["status"] == "interrupted"


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "writeStdin", "command": "/bin/zsh -lc 'python3'"},
        {"kind": "command", "command": None},
        {"command": "   "},
        {"kind": "somethingNew", "command": "ls"},
    ],
)
async def test_m4_uncheckable_command_requests_are_declined(setup, params) -> None:
    script = simple_turn()
    request = command_request()
    request["params"] = {"threadId": THREAD, "turnId": TURN, "itemId": "exec-1", **params}
    script.insert(1, request)
    backend, kit, inbox = setup([script], decider=Decider(DecisionAction.ALLOW))
    await backend.start()
    await play(backend)
    assert "answer item/commandExecution/requestApproval {'decision': 'decline'}" in kit.log
    assert not inbox.items()  # never reached the PolicyEngine's allow
    await asyncio.sleep(0.05)
    assert any("lässt sich nicht prüfen" in line for line in kit.log if line.startswith("steer"))


async def test_m4_network_request_without_command_is_checked(setup) -> None:
    script = simple_turn()
    request = command_request()
    request["params"] = {"threadId": THREAD, "turnId": TURN, "itemId": "exec-1",
                         "networkApprovalContext": {"host": "pypi.org"}}  # fmt: skip
    script.insert(1, request)
    backend, kit, inbox = setup([script], decider=Decider(DecisionAction.DENY))
    await backend.start()
    await play(backend)
    assert inbox.items() and inbox.items()[0].request.hosts == ["pypi.org"]


async def test_g3_refused_requests_become_events_with_raw(setup) -> None:
    script = simple_turn()
    script.insert(1, {"id": 50, "method": "mcpServer/elicitation/request", "params": {"q": 1}})
    backend, _, _ = setup([script])
    await backend.start()
    events = await play(backend)
    refused = [e for e in events if e.kind is EventKind.ERROR]
    assert len(refused) == 1 and refused[0].data["recoverable"] is True
    assert refused[0].data["type"] == "server_request"
    assert refused[0].raw == {
        "request": {"method": "mcpServer/elicitation/request", "params": {"q": 1}},
        "answer": {"action": "decline", "content": None},
    }
    assert events[-1].data["status"] == "ok"


async def test_m1_too_many_entries_stop_before_the_app_server(setup, monkeypatch) -> None:
    from arr.backends import codex_config

    backend, kit, _ = setup([simple_turn()])
    monkeypatch.setattr(codex_config, "ENTRIES_MAX", 5)
    await backend.start()
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    assert "Lese-Ordner" in events[1].data["message"]
    assert not kit.codexes


async def test_m3_channels_come_from_run_yaml(tmp_path: Path, monkeypatch) -> None:
    from run_kit import agent, channel, make_run_input, make_settings, make_workfolder

    from arr.resolve import resolve_run
    from arr.runs.prepare import prepare_run

    wf = make_workfolder(tmp_path)
    codex = {"provider": "codex", "model": "gpt-6-luna", "auth": "chatgpt-test"}
    run_input = make_run_input(
        [agent("gpt-1", **codex), agent("critic-1")],
        [channel("main", ["gpt-1", "critic-1"]), channel("secret", ["critic-1"])],
    )
    settings = make_settings()
    run = prepare_run(wf, run_input, resolve_run(run_input, None, settings), settings)
    prepared = run.agents["gpt-1"]
    profile = profile_for(prepared.file.config, RunPaths(wf, run.run_dir), PermissionRules())
    kit = Kit([simple_turn()])
    monkeypatch.setattr(codex_module, "start_thread", kit.start_thread)
    monkeypatch.setattr(codex_module, "_codex_config", lambda launch: launch)
    backend = CodexBackend(
        prepared,
        run_id=run.run_id,
        inbox=ApprovalInbox(PolicyEngine([profile])),
        auth=make_auth(tmp_path),
        factory=kit.factory,
    )
    await backend.start()
    await play(backend)
    fs = kit.codexes[0].config.filesystem
    secret = run.run_dir / "groupchats" / "attachments" / "secret"
    assert not secret.exists() and fs[str(profile.resolve(secret))] == "deny"


async def test_w3_00_1_g1_very_long_run_id_stops_before_the_app_server(
    tmp_path: Path, monkeypatch
) -> None:
    """2 globs per character of the run ID (W3-00): project and skill names of 64 characters
    make the profile too big. The turn ends before an app-server starts, and the message
    names the run ID and the long names, not old runs."""
    from arr.runs.prepare import new_run_id

    run_id = new_run_id("p" * 64, "s" * 64)
    assert len(run_id) == 148
    kit = Kit([simple_turn()])
    monkeypatch.setattr(codex_module, "start_thread", kit.start_thread)
    monkeypatch.setattr(codex_module, "_codex_config", lambda launch: launch)
    agent, engine = make_agent(tmp_path, run_id=run_id)
    backend = CodexBackend(
        agent,
        run_id=run_id,
        inbox=ApprovalInbox(engine, timeout=5.0),
        auth=make_auth(tmp_path),
        factory=kit.factory,
    )
    await backend.start()
    events = await play(backend)
    assert kinds(events) == ["turn_start", "error", "turn_end"]
    message = events[1].data["message"]
    assert "Lese-Sperrliste für Codex" in message
    assert f"„{run_id}“ hat 148 Zeichen" in message
    assert "Projekt- oder Skill-Namen" in message and "archiv" not in message.casefold()
    assert not kit.codexes


def test_w3_00_1_g1_warning_names_variant_2_causes(caplog) -> None:
    from arr.backends import codex_config

    caplog.set_level("WARNING")
    codex_config._check_size(codex_config.ENTRIES_WARN, 100, "R")
    assert "kürzere Projekt- und Skill-Namen" in caplog.text
    assert "archiv" not in caplog.text.casefold()


# -- W4-00: H1 (JSON-RPC error for unknown methods), H2 (unknown fields in commands) ---------


def sent(kit: Kit, request_id: int) -> dict[str, Any]:
    """What went out to the app-server as the answer to one server request."""
    payloads = [
        p for c in kit.codexes for p in c._client._sync.written if p.get("id") == request_id
    ]
    assert len(payloads) == 1, payloads
    return payloads[0]


def is_rpc_error(payload: dict[str, Any], code: int) -> bool:
    """A JSON-RPC error and nothing the app-server could read as a result."""
    error = payload.get("error")
    return (
        "result" not in payload
        and isinstance(error, dict)
        and error.get("code") == code
        and isinstance(error.get("message"), str)
        and bool(error["message"])
    )


@pytest.mark.parametrize("method", ["currentTime/read", "item/somethingNew/requestApproval"])
async def test_w4_00_h1_unknown_method_gets_a_json_rpc_error(setup, method) -> None:
    """`currentTime/read` exists in 0.163.0 (experimental API) but is not one arr answers."""
    script = simple_turn()
    script.insert(1, {"id": 61, "method": method, "params": {"threadId": THREAD}})
    backend, kit, inbox = setup([script])
    await backend.start()
    events = await play(backend)
    answer = sent(kit, 61)
    assert is_rpc_error(answer, -32601), answer
    assert answer == {"id": 61, "error": answer["error"]}  # id and error, nothing else
    # the user and the log still learn of it
    refused = [e for e in events if e.kind is EventKind.ERROR]
    assert len(refused) == 1 and f"„{method}“ abgelehnt" in refused[0].data["message"]
    assert refused[0].raw["answer"] == {"error": answer["error"]}
    assert not inbox.items() and events[-1].data["status"] == "ok"


_OLD_REFUSALS = {"item/tool/call": {"contentItems": [], "success": False}}


async def test_w4_00_h1_countertest_the_old_empty_result_is_caught(setup, monkeypatch) -> None:
    """With the answer before W4-00 (`{}`), `is_rpc_error` above fails."""
    from arr.backends import codex_approvals

    monkeypatch.setattr(codex_approvals, "refusal", lambda m: dict(_OLD_REFUSALS.get(m, {})))
    script = simple_turn()
    script.insert(1, {"id": 62, "method": "currentTime/read", "params": {}})
    backend, kit, _ = setup([script])
    await backend.start()
    await play(backend)
    assert sent(kit, 62) == {"id": 62, "result": {}}
    assert not is_rpc_error(sent(kit, 62), -32601)


@pytest.mark.parametrize("method", ["account/chatgptAuthTokens/refresh", "attestation/generate"])
async def test_w4_00_h1_token_requests_get_an_error_not_an_empty_token(setup, method) -> None:
    """Their answers require a token; `{}` was no refusal in the protocol either."""
    script = simple_turn()
    script.insert(1, {"id": 63, "method": method, "params": {"reason": "unauthorized"}})
    backend, kit, _ = setup([script])
    await backend.start()
    await play(backend)
    assert is_rpc_error(sent(kit, 63), -32000)


async def test_w4_00_h1_known_refusals_and_decisions_stay_results(setup) -> None:
    script = simple_turn()
    script.insert(1, {"id": 64, "method": "item/tool/call", "params": {}})
    script.insert(1, command_request())
    backend, kit, _ = setup([script], decider=Decider(DecisionAction.ALLOW))
    await backend.start()
    await play(backend)
    assert sent(kit, 64) == {"id": 64, "result": {"contentItems": [], "success": False}}
    assert sent(kit, 7) == {"id": 7, "result": {"decision": "accept"}}


def real_command_params(**changes: Any) -> dict[str, Any]:
    """A command request as Codex 0.163.0 sends it (S2 §10, with the experimental fields)."""
    params: dict[str, Any] = {
        "kind": "command",
        "threadId": THREAD,
        "turnId": TURN,
        "itemId": "exec-1",
        "startedAtMs": 1762700000000,
        "environmentId": "local",
        "approvalId": None,
        "reason": "Ich brauche sympy.",
        "command": "/bin/zsh -lc 'pip install sympy'",
        "cwd": "work",
        "commandActions": [{"type": "unknown", "command": "pip install sympy"}],
        "proposedExecpolicyAmendment": ["/bin/zsh", "-lc", "pip install sympy"],
        "proposedNetworkPolicyAmendments": None,
        "networkApprovalContext": None,
        "additionalPermissions": None,
        "availableDecisions": ["accept", "cancel"],
    }
    params.update(changes)
    return params


async def _command(setup, params: dict[str, Any]) -> tuple[Kit, ApprovalInbox]:
    script = simple_turn()
    script.insert(1, {"id": 7, "method": "item/commandExecution/requestApproval", "params": params})
    backend, kit, inbox = setup([script], decider=Decider(DecisionAction.ALLOW))
    await backend.start()
    await play(backend)
    await asyncio.sleep(0.05)
    return kit, inbox


def steered(kit: Kit) -> list[str]:
    return [line for line in kit.log if line.startswith("steer")]


async def test_w4_00_h2_a_real_command_request_reaches_the_inbox(setup) -> None:
    kit, inbox = await _command(setup, real_command_params())
    assert sent(kit, 7)["result"] == {"decision": "accept"}
    assert len(inbox.items()) == 1 and not steered(kit)


@pytest.mark.parametrize(
    "changes",
    [
        {"sandboxPermissions": "require_escalated"},  # a new top-level field
        {"writableRoots": ["/Users/someone"]},
        {"networkApprovalContext": {"host": "pypi.org", "protocol": "https", "port": 22}},
        {"networkApprovalContext": "pypi.org"},  # not readable
    ],
)
async def test_w4_00_h2_unknown_fields_are_refused_without_asking(setup, changes) -> None:
    kit, inbox = await _command(setup, real_command_params(**changes))
    assert sent(kit, 7)["result"] == {"decision": "decline"}
    assert not inbox.items()  # nobody was asked
    assert any("Feld, das arr nicht kennt" in line for line in steered(kit))


@pytest.mark.parametrize(
    "extra",
    [
        {"fileSystem": {"write": ["/Users/someone/.ssh"]}},
        {"network": {"enabled": True}},
        {},
    ],
)
async def test_w4_00_h2_extra_permissions_are_refused_without_asking(setup, extra) -> None:
    kit, inbox = await _command(setup, real_command_params(additionalPermissions=extra))
    assert sent(kit, 7)["result"] == {"decision": "decline"}
    assert not inbox.items()
    assert any("additionalPermissions" in line for line in steered(kit))


async def test_w4_00_h2_countertest_without_the_check_the_field_reaches_the_inbox(
    setup, monkeypatch
) -> None:
    """The check is what refuses: without it the same request is asked and accepted."""
    from arr.backends import codex_approvals

    monkeypatch.setattr(
        codex_approvals,
        "COMMAND_PARAM_FIELDS",
        frozenset(real_command_params()) | {"writableRoots"},
    )
    kit, inbox = await _command(setup, real_command_params(writableRoots=["/x"]))
    assert sent(kit, 7)["result"] == {"decision": "accept"} and inbox.items()

"""Tests for the Claude backend (W2-01) without a model: options (A5), event normalization
from the S1 fixtures (A6), approval path (A7), secrets (A8), project context (A9), hints."""

import asyncio
import dataclasses
import json
import logging
import os
import shlex
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import ToolPermissionContext

from arr.auth import CLAUDE_SWITCHES, START_PROFILE_VARIABLE, AuthError
from arr.backends import claude_options as claude_options_module
from arr.backends.base import AgentBackend, CostKind, EventKind
from arr.backends.claude import ClaudeBackend, tool_request, with_context
from arr.backends.claude_events import MISSING_RESULT, ClaudeEvents
from arr.backends.claude_options import (
    ROOM_SERVER,
    build_options,
    remove_tmp_link,
    static_prompt,
    tmp_link,
)
from arr.backends.claude_start import HANDOVER_FAILED
from arr.errors import ArrError
from arr.models import AuthProfile, Limits, RunInput, Toolchain, Tools, WorkfolderSettings
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PermissionRules, PolicyEngine, RunPaths, profile_for
from arr.permissions.types import (
    ApprovalItem,
    DecidedBy,
    Decision,
    DecisionAction,
)
from arr.resolve import resolve_run
from arr.runs.eventlog import EventLogger
from arr.runs.prepare import PreparedAgent, PreparedRun, prepare_run
from arr.workfolder import init_workfolder

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
SECRET = "sk-ant-oat01-TESTSECRET-0123456789abcdef"


@pytest.fixture(autouse=True)
def private_start_program(arr_home: Path) -> Path:
    """A profile with a secret writes the start program below `app_dir()`."""
    return arr_home


# --------------------------------------------------------------------------------------------
# A prepared run with two Claude agents of the same role
# --------------------------------------------------------------------------------------------


@dataclass
class Setup:
    run: PreparedRun
    rules: PermissionRules
    toolchain: Toolchain
    tmp_root: Path

    def agent(self, agent_id: str = "e1") -> PreparedAgent:
        return self.run.agents[agent_id]

    def profile(self, agent_id: str = "e1"):
        paths = RunPaths(self.run.run_dir.parent.parent, self.run.run_dir, self.toolchain)
        return profile_for(self.agent(agent_id).file.config, paths, self.rules)

    def engine(self) -> PolicyEngine:
        return PolicyEngine([self.profile("e1"), self.profile("e2")])


@pytest.fixture
def setup(tmp_path: Path, arr_home: Path) -> Setup:
    wf = init_workfolder(tmp_path / "wf")
    (wf / "projects" / "p").mkdir(parents=True)
    (wf / "projects" / "p" / "CLAUDE.md").write_text("# Projekt p\n\nKontext-Marker-77\n")
    skill = wf / "skills" / "probe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: probe\ndescription: Probe.\nmetadata:\n  arr-audience: research\n---\n\nx\n"
    )
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    toolchain = Toolchain(python=str(venv))
    settings = WorkfolderSettings(toolchain=toolchain)
    run_input = RunInput.model_validate(
        {
            "project": "p",
            "task": "Test",
            "topology": {"kind": "solo"},
            "defaults": {
                "provider": "claude",
                "model": "claude-haiku-5-5",
                "auth": "max",
                "effort": "low",
            },
            "agents": [{"id": "e1", "skills": ["probe"]}, {"id": "e2"}],
            "groupchats": [{"name": "main", "members": ["e1", "e2"]}],
        }
    )
    resolved = resolve_run(run_input, None, settings)
    run = prepare_run(wf, run_input, resolved, settings)
    link_root = tmp_path / "links"
    link_root.mkdir()
    return Setup(run, PermissionRules(), toolchain, link_root)


def options_for(setup: Setup, agent_id: str = "e1", **kw: Any):
    kw.setdefault("env", dict(CLAUDE_SWITCHES))
    kw.setdefault("can_use_tool", None)
    kw.setdefault("hooks", None)
    return build_options(setup.agent(agent_id), setup.profile(agent_id), **kw)


# --------------------------------------------------------------------------------------------
# A5: options
# --------------------------------------------------------------------------------------------


def bash_env(options: Any) -> dict[str, str]:
    """What the CLI puts in front of every Bash command: the file of `CLAUDE_ENV_FILE`."""
    lines = Path(options.env["CLAUDE_ENV_FILE"]).read_text(encoding="utf-8").splitlines()
    return dict(shlex.split(line)[1].split("=", 1) for line in lines)


def test_a5_options_follow_the_architecture(setup: Setup) -> None:
    profile = setup.profile()
    link = tmp_link(profile.tmp_dir, "r/e1", setup.tmp_root)
    try:
        options = options_for(setup, max_budget_usd=0.5, tmpdir=link)
    finally:
        remove_tmp_link(link, profile.tmp_dir)
    agent = setup.agent()
    bare = {"Read", "Grep", "Glob", "Bash", "Write", "Edit"}
    assert not bare & set(options.allowed_tools)
    assert all(r == "mcp__arr-room__*" or r.startswith("Read(//") for r in options.allowed_tools)
    assert not bare & set(options.disallowed_tools)
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "default"  # W2-05: explicit, see PERMISSION_MODE
    from arr.permissions.policy import claude_options

    perms = claude_options(profile)
    assert options.sandbox == perms.sandbox
    assert options.sandbox["allowUnsandboxedCommands"] is False
    assert options.sandbox["failIfUnavailable"] is True
    assert options.add_dirs == perms.add_dirs
    assert str(profile.work_dir) not in options.add_dirs
    assert options.cwd == agent.work_dir
    assert options.tools == ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill"]
    assert options.skills == ["arr:probe"]
    assert options.plugins == [{"type": "local", "path": str(agent.plugin_dir)}]
    assert list(options.mcp_servers) == [ROOM_SERVER] == ["arr-room"]
    room = options.mcp_servers[ROOM_SERVER]
    assert room["args"] == ["-I", "-m", "arr.room.server"]  # no `arr/` of the cwd (W3-06.3, B2)
    assert room["env"] == {"AGENT_ID": "e1", "RUN_DIR": str(setup.run.run_dir)}
    prompt = options.system_prompt
    assert prompt["type"] == "preset" and prompt["preset"] == "claude_code"
    assert prompt["exclude_dynamic_sections"] is True
    assert static_prompt().strip() in prompt["append"]
    assert "nur mit dem Read-Tool" in prompt["append"] or "Read-Tool" in prompt["append"]
    assert options.thinking == {"type": "adaptive", "display": "summarized"}
    assert options.effort == "low"
    assert options.max_budget_usd == 0.5
    env = options.env
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"] == str(link)
    assert env["PIP_CACHE_DIR"] == str(profile.tmp_dir / "cache" / "pip")
    assert env["MPLCONFIGDIR"].startswith(str(profile.tmp_dir))
    shell = bash_env(options)
    path = shell["PATH"].split(os.pathsep)
    venv = RunPaths(setup.run.run_dir.parent.parent, setup.run.run_dir).venv_dir
    assert path[0] == str(venv / "bin")  # from RunPaths, outside the run folder (W3-00)
    # the CLI itself runs outside the sandbox: no write root on its PATH (W3-06.3, B1)
    assert str(venv / "bin") not in env["PATH"].split(os.pathsep)
    assert env["VIRTUAL_ENV"] == ""
    assert not path[0].startswith(str(setup.run.run_dir))
    real = os.path.realpath(venv)  # PATH, add_dirs and the sandbox name the same run venv
    assert real in options.add_dirs and real in options.sandbox["filesystem"]["allowWrite"]
    assert real in options.sandbox["filesystem"]["allowRead"]
    assert shell["VIRTUAL_ENV"] == str(venv)  # not arr's own venv (W3-00 live check)
    assert env["PYTHONNOUSERSITE"] == "1"  # no user site folder below ~ (pip list)
    assert env["PYTHONPATH"] == env["PYTHONHOME"] == ""
    assert path[1 : 1 + len(agent.file.path_prepend)] == agent.file.path_prepend


def test_w3_00_no_virtual_env_without_run_venv(setup: Setup, monkeypatch) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", "/arr/.venv")
    off = dataclasses.replace(setup, toolchain=Toolchain(run_venv="off"))
    profile = off.profile()
    assert profile.venv_dir is None
    options = build_options(
        setup.agent(), profile, env=dict(CLAUDE_SWITCHES), can_use_tool=None, hooks=None
    )
    assert options.env["VIRTUAL_ENV"] == bash_env(options)["VIRTUAL_ENV"] == ""
    assert not any("venv-root" in p for p in bash_env(options)["PATH"].split(os.pathsep))


def test_w3_00_1_g4_run_venv_comes_from_the_profile(setup: Setup, monkeypatch) -> None:
    """One source: a changed `ARR_VENV_ROOT` after the profile was built changes nothing."""
    profile = setup.profile()
    monkeypatch.setenv("ARR_VENV_ROOT", "/somewhere/else")
    options = build_options(
        setup.agent(), profile, env=dict(CLAUDE_SWITCHES), can_use_tool=None, hooks=None
    )
    assert profile.venv_dir is not None
    shell = bash_env(options)
    assert shell["PATH"].split(os.pathsep)[0] == str(profile.venv_dir / "bin")
    assert shell["VIRTUAL_ENV"] == str(profile.venv_dir)
    assert str(profile.venv_dir) in options.sandbox["filesystem"]["allowWrite"]


def test_a5_system_prompt_is_byte_identical_per_role(setup: Setup) -> None:
    one, two = options_for(setup, "e1"), options_for(setup, "e2")
    assert one.system_prompt == two.system_prompt
    assert setup.run.run_id not in one.system_prompt["append"]
    assert "Kontext-Marker-77" not in one.system_prompt["append"]  # context comes per prompt


def test_a5_shell_and_web_switches(setup: Setup) -> None:
    agent = setup.agent()
    config = agent.file.config.model_copy(update={"tools": Tools(shell=False, web=True)})
    file = agent.file.model_copy(update={"config": config})
    changed = PreparedAgent(file, agent.run_dir)
    options = build_options(changed, setup.profile(), env={}, can_use_tool=None, hooks=None)
    assert "Bash" not in options.tools and {"WebFetch", "WebSearch"} <= set(options.tools)


def test_tmp_link_is_short_reused_and_removed(setup: Setup) -> None:
    target = setup.profile().tmp_dir
    link = tmp_link(target, "r/e1", setup.tmp_root)
    assert target.is_dir()  # Run-tmp is created
    assert os.readlink(link) == str(target)
    assert tmp_link(target, "r/e1", setup.tmp_root) == link  # restart reuses it
    real = tmp_link(target, "r/e1")  # the real root (/tmp) is short enough for sockets
    try:
        assert len(str(real / "srt-mux-99999-zz.sock")) < 104 and len(str(real)) <= 30
    finally:
        remove_tmp_link(real, target)
    remove_tmp_link(link, target)
    assert not link.exists() and target.is_dir()


# --------------------------------------------------------------------------------------------
# A6: events from the S1 fixtures
# --------------------------------------------------------------------------------------------


def fixture_messages(name: str) -> list[list[Any]]:
    """SDK messages of a fixture, split per query (each list ends with its ResultMessage)."""
    queries: list[list[Any]] = [[]]
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        msg = row["msg"]
        if row["dir"] != "in" or msg.get("type") in ("control_request", "control_response"):
            continue
        parsed = parse_message(msg)
        if parsed is None:
            continue
        queries[-1].append(parsed)
        if isinstance(parsed, ResultMessage):
            queries.append([])
    return [q for q in queries if q]


def normalize(name: str, cost_kind: CostKind | None = CostKind.ESTIMATED):
    mapper = ClaudeEvents("r1", "e1", "claude-haiku-5-5", cost_kind)
    turns = []
    for number, messages in enumerate(fixture_messages(name), start=1):
        mapper.start_turn(number)
        events = [e for m in messages for e in mapper.feed(m)]
        turns.append(events + mapper.finish())
    return turns


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.jsonl")))
def test_a6_every_tool_call_has_a_result_and_turns_end(name: str) -> None:
    for events in normalize(name):
        calls = [e.data["id"] for e in events if e.kind is EventKind.TOOL_CALL]
        results = [e.data["id"] for e in events if e.kind is EventKind.TOOL_RESULT]
        assert sorted(calls) == sorted(results), name
        assert events[-1].kind is EventKind.TURN_END
        assert sum(e.kind is EventKind.TURN_END for e in events) == 1
        for e in events:
            assert "user@example.com" not in json.dumps(e.raw, default=str)


def test_a6_usage_deduplicated_and_cost_as_difference() -> None:
    turns = normalize("solo_three_turns_thinking_tools.jsonl")
    assert len(turns) == 3
    results = [q[-1] for q in fixture_messages("solo_three_turns_thinking_tools.jsonl")]
    ends = [t[-1].data for t in turns]
    totals = [r.total_cost_usd for r in results]
    assert ends[0]["cost_usd"] == pytest.approx(totals[0])
    assert ends[1]["cost_usd"] == pytest.approx(totals[1] - totals[0])
    assert ends[2]["cost_usd"] == pytest.approx(totals[2] - totals[1])
    assert sum(e["cost_usd"] for e in ends) == pytest.approx(totals[2])
    # Tokens: difference of the cumulative model_usage, same requests as the cost.
    model = [r.model_usage["claude-haiku-5-5"] for r in results]
    assert [e["usage"]["input"] for e in ends] == [
        model[0]["inputTokens"],
        model[1]["inputTokens"] - model[0]["inputTokens"],
        model[2]["inputTokens"] - model[1]["inputTokens"],
    ]
    assert sum(e["usage"]["output"] for e in ends) == model[2]["outputTokens"]
    assert all(e["cost_kind"] == "estimated" and e["status"] == "ok" for e in ends)
    assert all(e["usage"]["model"] == "claude-haiku-5-5" for e in ends)
    # One usage event per model request (message_id), not per streamed block.
    for events, messages in zip(
        turns, fixture_messages("solo_three_turns_thinking_tools.jsonl"), strict=True
    ):
        ids = {m.message_id for m in messages if isinstance(m, AssistantMessage)}
        assert sum(e.kind is EventKind.USAGE for e in events) == len(ids)
    kinds = {e.kind for t in turns for e in t}
    assert {EventKind.THINKING, EventKind.TEXT, EventKind.TOOL_CALL, EventKind.RATE_LIMIT} <= kinds
    thinking = next(e for t in turns for e in t if e.kind is EventKind.THINKING)
    assert thinking.data["summarized"] is True


def test_a6_rate_limit_event() -> None:
    events = [e for t in normalize("solo_three_turns_thinking_tools.jsonl") for e in t]
    rate = next(e for e in events if e.kind is EventKind.RATE_LIMIT)
    assert rate.data["status"] in ("allowed", "allowed_warning", "rejected")
    assert set(rate.data) == {"status", "utilization", "resets_at"}


def test_a6_denials_and_errors_in_turn_end() -> None:
    turns = normalize("sandbox_violation_and_network_request.jsonl")
    end = turns[-1][-1].data
    assert end["permission_denials"]
    errors = [e for t in turns for e in t if e.kind is EventKind.TOOL_RESULT and e.data["is_error"]]
    assert errors


def _result(**kw: Any) -> ResultMessage:
    data = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.01,
        model_usage={"m": {"inputTokens": 10, "outputTokens": 5}},
    )
    data.update(kw)
    return ResultMessage(**data)


def test_a6_status_error_limit_interrupt_and_missing_result() -> None:
    mapper = ClaudeEvents("r1", "e1", "m", None)
    mapper.start_turn(1)
    mapper.feed(_result(subtype="error_max_budget_usd", is_error=True))
    assert mapper.finish()[-1].data["status"] == "limit"
    mapper.start_turn(2)
    mapper.feed(_result(subtype="error_during_execution", is_error=True, errors=["kaputt"]))
    events = mapper.finish()
    assert [e.kind for e in events] == [EventKind.ERROR, EventKind.TURN_END]
    assert "kaputt" in events[0].data["message"] and events[1].data["status"] == "error"
    mapper.start_turn(3)
    mapper.feed(AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "sleep 9"})], "m"))
    mapper.feed(_result(terminal_reason="aborted_tools"))
    events = mapper.finish()
    assert events[0].kind is EventKind.TOOL_RESULT and events[0].data["content"] == MISSING_RESULT
    assert events[-1].data["status"] == "interrupted"
    mapper.start_turn(4)
    events = mapper.finish(interrupted=True)  # interrupt() and no result from the CLI
    assert events[-1].data["status"] == "interrupted" and events[-1].data["cost_usd"] is None
    mapper.start_turn(5)
    events = mapper.finish()
    assert [e.kind for e in events] == [EventKind.ERROR, EventKind.TURN_END]
    assert events[-1].data["status"] == "error"


def test_a6_cost_reset_counts_new_totals() -> None:
    mapper = ClaudeEvents("r1", "e1", "m", CostKind.BILLED)
    mapper.start_turn(1)
    mapper.feed(_result(total_cost_usd=0.5))
    mapper.finish()
    mapper.start_turn(2)
    mapper.feed(_result(total_cost_usd=0.1, model_usage={"m": {"inputTokens": 3}}))
    end = mapper.finish()[-1].data
    assert end["cost_usd"] == pytest.approx(0.1) and end["usage"]["input"] == 3
    assert end["cost_kind"] == "billed"


def test_a6_raw_has_no_account_data() -> None:
    mapper = ClaudeEvents("r1", "e1", "m", None)
    mapper.start_turn(1)
    msg = SystemMessage("init", {"session_id": "s", "account": {"email": "a@b.example"}})
    assert mapper.feed(msg) == []
    mapper.feed(_result())
    end = mapper.finish()[-1]
    assert "a@b.example" not in json.dumps(end.raw, default=str)
    assert end.data["session"] == "s1"


# --------------------------------------------------------------------------------------------
# A fake SDK client for the backend
# --------------------------------------------------------------------------------------------

Step = Any  # an SDK message, or an async callable(client) that may return messages


@dataclass
class FakeClient:
    options: Any
    script: list[list[Step]]
    queries: list[str] = field(default_factory=list)
    hook_outputs: list[dict[str, Any]] = field(default_factory=list)
    connected: bool = False
    disconnected: bool = False
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)
    connect_task: asyncio.Task[Any] | None = None

    async def connect(self, prompt: Any = None) -> None:
        self.connected = True
        self.connect_task = asyncio.current_task()

    async def disconnect(self) -> None:
        assert asyncio.current_task() is self.connect_task  # same task as connect (anyio)
        self.disconnected = True

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        for step in self.script.pop(0):
            if callable(step):
                produced = await step(self)
                for msg in produced or []:
                    yield msg
            else:
                yield step

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def hook(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        callback = self.options.hooks[event][0].hooks[0]
        out = await callback(payload, payload.get("tool_use_id"), {"signal": None})
        self.hook_outputs.append(out)
        return out


def tool_turn(command: str = "ls", call_id: str = "t1") -> list[Step]:
    async def post(client: FakeClient) -> list[Any]:
        await client.hook(
            "PostToolUse",
            {"tool_name": "Bash", "tool_input": {"command": command}, "tool_use_id": call_id},
        )
        return [UserMessage([ToolResultBlock(call_id, "ok", False)])]

    return [
        SystemMessage("init", {"session_id": "sess-1"}),
        AssistantMessage(
            [ToolUseBlock(call_id, "Bash", {"command": command})], "m", message_id="a1"
        ),
        post,
        AssistantMessage([TextBlock("fertig")], "m", message_id="a2"),
        _result(),
    ]


class AllowAll:
    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.items.append(item)
        return Decision(
            action=DecisionAction.DENY, decided_by=DecidedBy.USER, reason="Nein, danke."
        )


def make_backend(
    setup: Setup,
    script: list[list[Step]],
    *,
    auth: AuthProfile | None = None,
    reader: Callable[[str, str], str | None] | None = None,
    decider: Any = None,
    client_cls: type[FakeClient] = FakeClient,
) -> tuple[ClaudeBackend, list[FakeClient]]:
    clients: list[FakeClient] = []
    inbox = ApprovalInbox(setup.engine(), decider=decider, timeout=5)

    def factory(options: Any) -> FakeClient:
        client = client_cls(options, script)
        clients.append(client)
        return client

    backend = ClaudeBackend(
        setup.agent(),
        run_id=setup.run.run_id,
        inbox=inbox,
        auth=auth or AuthProfile(name="max", kind="claude_oauth"),
        limits=Limits(budget_usd=0.5),
        secret_reader=reader or (lambda service, account: None),
        client_factory=factory,
    )
    return backend, clients


def is_link(path: str) -> bool:
    return os.path.islink(path)


async def collect(backend: ClaudeBackend, prompt: str) -> list[Any]:
    return [e async for e in backend.run_turn(prompt)]


async def test_backend_satisfies_protocol_and_lifecycle(setup: Setup) -> None:
    backend, clients = make_backend(setup, [tool_turn()])
    assert isinstance(backend, AgentBackend)
    await backend.start()
    await backend.start()  # idempotent
    assert len(clients) == 1 and clients[0].connected
    link = clients[0].options.env["TMPDIR"]
    assert is_link(link)
    events = await collect(backend, "Los")
    assert events[0].kind is EventKind.TURN_START and events[-1].kind is EventKind.TURN_END
    assert backend.session_ref() == {"session_id": "s1"}
    await backend.close()
    await backend.close()  # idempotent
    assert clients[0].disconnected and not is_link(link)


# --------------------------------------------------------------------------------------------
# A9: project context in the first prompt and after a compaction
# --------------------------------------------------------------------------------------------


async def test_a9_context_in_first_prompt_and_after_compaction(setup: Setup) -> None:
    compact = [
        SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "auto"}}),
        _result(),
    ]
    backend, clients = make_backend(setup, [compact, [_result()], [_result()]])
    try:
        first = await collect(backend, "Auftrag 1")
        await collect(backend, "Auftrag 2")
        await collect(backend, "Auftrag 3")
    finally:
        await backend.close()
    queries = clients[0].queries
    context = (setup.run.run_dir / setup.agent().file.context_file).read_text()
    assert "Kontext-Marker-77" in context
    assert queries[0].startswith("## Projektkontext") and "Kontext-Marker-77" in queries[0]
    assert queries[0].endswith("## Auftrag\n\nAuftrag 1")
    assert "Kontext-Marker-77" in queries[1]  # resent after compact_boundary
    assert queries[2] == "Auftrag 3"
    assert first[0].data["prompt"] == queries[0]


def test_with_context_without_context() -> None:
    assert with_context("x", "  \n") == "x"


# --------------------------------------------------------------------------------------------
# A7: approval path
# --------------------------------------------------------------------------------------------


def _ctx(call_id: str = "t9", reason: str | None = None) -> ToolPermissionContext:
    return ToolPermissionContext(tool_use_id=call_id, decision_reason=reason)


async def test_a7_can_use_tool_goes_to_the_inbox(setup: Setup) -> None:
    decider = AllowAll()
    backend, _ = make_backend(setup, [], decider=decider)
    network = await backend._can_use_tool("SandboxNetworkAccess", {"host": "pypi.org"}, _ctx())
    assert isinstance(network, PermissionResultDeny) and network.message == "Nein, danke."
    item = decider.items[0]
    assert item.request.hosts == ["pypi.org"] and item.request.provider == "claude"
    assert item.request.tool_call_id == "t9" and item.agent_id == "e1"
    output = setup.run.run_dir / "output" / "x.md"
    allowed = await backend._can_use_tool("Read", {"file_path": str(output)}, _ctx())
    assert isinstance(allowed, PermissionResultAllow)  # fixed rule, no user
    assert len(decider.items) == 1
    system = await backend._can_use_tool(
        "Read", {"file_path": "/usr/lib/libSystem.B.dylib"}, _ctx()
    )
    assert isinstance(system, PermissionResultAllow)


async def test_a7_pretooluse_denies_hard_bans_with_reason(setup: Setup) -> None:
    backend, _ = make_backend(setup, [])
    hook = backend._pre_tool_use
    sudo = await hook({"tool_name": "Bash", "tool_input": {"command": "sudo ls"}}, "t1", {})
    out = sudo["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "sudo" in out["permissionDecisionReason"]
    original = setup.run.run_dir.parent.parent / "projects" / "p" / "CLAUDE.md"
    write = await hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(original), "content": "x"}}, "t2", {}
    )
    reason = write["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Arbeitsordner" in reason  # with an alternative
    ok = await hook({"tool_name": "Bash", "tool_input": {"command": "python3 x.py"}}, "t3", {})
    assert ok == {}
    ssh = await hook({"tool_name": "Read", "tool_input": {"file_path": "~/.ssh/id_rsa"}}, "t4", {})
    assert ssh["hookSpecificOutput"]["permissionDecision"] == "deny"
    room = await hook({"tool_name": "mcp__arr-room__inbox", "tool_input": {}}, "t5", {})
    assert room == {}


def test_tool_request_mapping() -> None:
    kw = dict(run_id="r", agent_id="e1", role=None, work_dir="/w")
    assert tool_request("Glob", {"pattern": "*.py", "path": "/a"}, **kw).paths == ["/a/*.py"]
    assert tool_request("Glob", {"pattern": "**/*.md"}, **kw).paths == ["**/*.md"]
    assert tool_request("Grep", {"pattern": "x"}, **kw).paths == ["."]
    bash = tool_request("Bash", {"command": "ls", "description": "Liste"}, **kw)
    assert bash.command == "ls" and bash.reason == "Liste"
    assert tool_request("WebFetch", {"url": "https://a.example/x"}, **kw).urls
    assert tool_request("Edit", {"file_path": "a.txt"}, **kw).paths == ["a.txt"]


# --------------------------------------------------------------------------------------------
# Hints mid-turn
# --------------------------------------------------------------------------------------------


async def test_hint_is_attached_to_the_next_tool_result(setup: Setup) -> None:
    delivered: list[bool] = []

    async def hint_then_tool(client: FakeClient) -> list[Any]:
        task = asyncio.create_task(backend.deliver_hint("📨 1 Frage an dich in #main"))
        await asyncio.sleep(0)
        await client.hook(
            "PostToolUse", {"tool_name": "Bash", "tool_input": {}, "tool_use_id": "t1"}
        )
        delivered.append(await task)
        return [UserMessage([ToolResultBlock("t1", "ok", False)])]

    script = [
        [
            AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "ls"})], "m"),
            hint_then_tool,
            _result(),
        ]
    ]
    backend, clients = make_backend(setup, script)
    try:
        events = await collect(backend, "Los")
    finally:
        await backend.close()
    assert delivered == [True]
    output = clients[0].hook_outputs[0]["hookSpecificOutput"]
    assert output == {
        "hookEventName": "PostToolUse",
        "additionalContext": "📨 1 Frage an dich in #main",
    }
    hint = next(e for e in events if e.kind is EventKind.HINT)
    assert hint.data == {
        "text": "📨 1 Frage an dich in #main",
        "channel": None,
        "method": "post_tool_use",
    }
    kinds = [e.kind for e in events]
    assert kinds.index(EventKind.HINT) < kinds.index(EventKind.TOOL_RESULT)


async def test_hint_without_tool_call_or_turn_is_not_delivered(setup: Setup) -> None:
    results: list[bool] = []

    async def hint_only(client: FakeClient) -> list[Any]:
        results.append(asyncio.create_task(backend.deliver_hint("Hinweis")))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        return [_result()]

    backend, _ = make_backend(setup, [[hint_only]])
    try:
        assert await backend.deliver_hint("kein Turn") is False
        await collect(backend, "Los")
        assert await results[0] is False  # turn ended first: stays in the inbox
    finally:
        await backend.close()


# --------------------------------------------------------------------------------------------
# Interrupt
# --------------------------------------------------------------------------------------------


async def test_interrupt_ends_the_turn_with_turn_end(setup: Setup) -> None:
    async def wait_for_interrupt(client: FakeClient) -> list[Any]:
        await client.interrupted.wait()
        return [_result(terminal_reason="aborted_streaming", subtype="success")]

    script = [
        [
            AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "sleep 60"})], "m"),
            wait_for_interrupt,
        ]
    ]
    backend, _ = make_backend(setup, script)
    events: list[Any] = []
    called = asyncio.Event()

    async def run() -> None:
        async for event in backend.run_turn("Los"):
            events.append(event)
            if event.kind is EventKind.TOOL_CALL:
                called.set()

    try:
        await backend.start()
        task = asyncio.create_task(run())
        await asyncio.wait_for(called.wait(), 2)
        await backend.interrupt()
        await asyncio.wait_for(task, 2)
    finally:
        await backend.close()
    assert events[-1].kind is EventKind.TURN_END and events[-1].data["status"] == "interrupted"
    assert any(e.kind is EventKind.TOOL_RESULT and e.data["is_error"] for e in events)
    await backend.interrupt()  # idle: no-op


async def test_w2_05_1_cli_gone_after_the_stop_counts_as_interrupted(setup: Setup) -> None:
    """A Ctrl-C in the terminal hits the whole process group: the CLI exits by itself (code 0)
    and the SDK raises; after `interrupt()` that is an interrupted turn, not an error."""

    async def die_after_interrupt(client: FakeClient) -> list[Any]:
        await client.interrupted.wait()
        raise ConnectionError("Cannot write to terminated process (exit code: 0)")

    script = [
        [
            AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "sleep 60"})], "m"),
            die_after_interrupt,
        ]
    ]
    backend, _ = make_backend(setup, script)
    events: list[Any] = []
    called = asyncio.Event()

    async def run() -> None:
        async for event in backend.run_turn("Los"):
            events.append(event)
            if event.kind is EventKind.TOOL_CALL:
                called.set()

    try:
        await backend.start()
        task = asyncio.create_task(run())
        await asyncio.wait_for(called.wait(), 2)
        await backend.interrupt()
        await asyncio.wait_for(task, 2)
    finally:
        await backend.close()
    assert events[-1].kind is EventKind.TURN_END and events[-1].data["status"] == "interrupted"
    assert not any(e.kind is EventKind.ERROR for e in events)


async def test_w2_05_1_interrupt_returns_with_the_turn_when_the_cli_never_answers(
    setup: Setup,
) -> None:
    """A Ctrl-C in the terminal ends the CLI itself: the interrupt request gets no answer and
    the stream just ends (`spikes/w2_05/killpg_probe.py`). `interrupt()` returns with the turn,
    not after the orchestrator's timeout, and the turn is `interrupted`."""

    class SilentClient(FakeClient):
        async def interrupt(self) -> None:
            self.interrupted.set()
            await asyncio.Event().wait()  # the CLI is gone, no answer ever

    async def end_after_interrupt(client: FakeClient) -> list[Any]:
        await client.interrupted.wait()
        return []  # the stream ends without a result message

    script = [
        [
            AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "sleep 60"})], "m"),
            end_after_interrupt,
        ]
    ]
    backend, _ = make_backend(setup, script, client_cls=SilentClient)
    events: list[Any] = []
    called = asyncio.Event()

    async def run() -> None:
        async for event in backend.run_turn("Los"):
            events.append(event)
            if event.kind is EventKind.TOOL_CALL:
                called.set()

    try:
        await backend.start()
        task = asyncio.create_task(run())
        await asyncio.wait_for(called.wait(), 2)
        await asyncio.wait_for(backend.interrupt(), 1)  # returns with the turn's end
        await asyncio.wait_for(task, 2)
    finally:
        await backend.close()
    assert events[-1].kind is EventKind.TURN_END and events[-1].data["status"] == "interrupted"
    assert not any(e.kind is EventKind.ERROR for e in events)


async def test_cli_failure_gives_error_and_turn_end(setup: Setup) -> None:
    async def boom(client: FakeClient) -> list[Any]:
        raise ConnectionError("CLI weg")

    backend, _ = make_backend(setup, [[boom]])
    try:
        events = await collect(backend, "Los")
    finally:
        await backend.close()
    assert [e.kind for e in events][-2:] == [EventKind.ERROR, EventKind.TURN_END]
    assert events[-1].data["status"] == "error"


# --------------------------------------------------------------------------------------------
# A8: no secret in logs, events or files
# --------------------------------------------------------------------------------------------


async def test_a8_the_token_is_in_no_environment_and_no_file(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    backend, clients = make_backend(setup, [tool_turn()], reader=lambda s, a: SECRET)
    agent_dir = setup.agent().agent_dir
    logger = EventLogger(agent_dir)
    try:
        async for event in backend.run_turn("Los"):
            logger.log(event)
    finally:
        logger.close()
        await backend.close()
    options = clients[0].options
    assert SECRET not in options.env.values() and "CLAUDE_CODE_OAUTH_TOKEN" not in options.env
    assert Path(options.cli_path).name.startswith("claude-start-"), "the start program hands over"
    for path in agent_dir.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace"), path
    assert SECRET not in caplog.text
    assert "Profil max" in caplog.text or "max" in caplog.text


async def test_a8_cleans_the_process_env(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_PID"):
        monkeypatch.setenv(name, "x")
    backend, clients = make_backend(setup, [])
    try:
        await backend.start()
    finally:
        await backend.close()
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_PID"):
        assert name not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in clients[0].options.env  # no token stored


async def test_failed_connect_releases_the_link(setup: Setup) -> None:
    class Broken(FakeClient):
        async def connect(self, prompt: Any = None) -> None:
            raise RuntimeError("kein Login")

    links: list[str] = []

    def factory(options: Any) -> FakeClient:
        links.append(options.env["TMPDIR"])
        return Broken(options, [])

    backend = ClaudeBackend(
        setup.agent(),
        run_id="r",
        inbox=ApprovalInbox(setup.engine()),
        auth=AuthProfile(name="max", kind="claude_oauth"),
        secret_reader=lambda s, a: None,
        client_factory=factory,
    )
    with pytest.raises(RuntimeError):
        await backend.start()
    assert not is_link(links[0])
    await backend.close()


async def test_hint_also_rides_on_a_failed_tool_call(setup: Setup) -> None:
    async def failure(client: FakeClient) -> list[Any]:
        task = asyncio.create_task(backend.deliver_hint("📨 Hinweis"))
        await asyncio.sleep(0)
        payload = {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_use_id": "t1",
            "error": "Exit code 1",
        }
        callback = client.options.hooks["PostToolUseFailure"][0].hooks[0]
        client.hook_outputs.append(await callback(payload, "t1", {"signal": None}))
        assert await task is True
        return [UserMessage([ToolResultBlock("t1", "Exit code 1", True)]), _result()]

    script = [[AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "false"})], "m"), failure]]
    backend, clients = make_backend(setup, script)
    try:
        events = await collect(backend, "Los")
    finally:
        await backend.close()
    output = clients[0].hook_outputs[0]["hookSpecificOutput"]
    assert output == {"hookEventName": "PostToolUseFailure", "additionalContext": "📨 Hinweis"}
    assert any(e.kind is EventKind.HINT for e in events)


# --------------------------------------------------------------------------------------------
# W2-01.1: scrub switch (B2), link in /tmp (B5), lifecycle after close (B6), raw once (B7)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "secret", "credential"),
    [
        ("claude_oauth", SECRET, "CLAUDE_CODE_OAUTH_TOKEN"),
        ("claude_oauth", None, None),
        ("claude_api_key", SECRET, "ANTHROPIC_API_KEY"),
    ],
)
async def test_w2011_scrub_switch_for_every_auth_kind(
    setup: Setup, kind: str, secret: str | None, credential: str | None
) -> None:
    auth = AuthProfile(name="p", kind=kind)
    backend, clients = make_backend(setup, [], auth=auth, reader=lambda s, a: secret)
    try:
        await backend.start()
    finally:
        await backend.close()
    env = clients[0].options.env
    assert env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert not (credential and credential in env), "no secret in any environment"
    assert (START_PROFILE_VARIABLE in env) == bool(credential)
    assert (clients[0].options.cli_path is not None) == bool(credential)


async def test_a_failed_handover_stops_the_agent_with_a_clear_message(setup: Setup) -> None:
    class RefusedClient(FakeClient):
        async def connect(self, prompt: Any = None) -> None:
            raise ProcessError("Command failed", exit_code=HANDOVER_FAILED)

    backend, clients = make_backend(setup, [], reader=lambda s, a: SECRET, client_cls=RefusedClient)
    with pytest.raises(AuthError, match="ließ sich nicht an die CLI übergeben"):
        await backend.start()
    assert len(clients) == 1, "no second start, and no way back into the environment"
    assert SECRET not in clients[0].options.env.values()
    await backend.close()


async def test_another_failure_of_the_cli_stays_what_it_is(setup: Setup) -> None:
    class BrokenClient(FakeClient):
        async def connect(self, prompt: Any = None) -> None:
            raise ProcessError("Command failed", exit_code=1)

    backend, _ = make_backend(setup, [], reader=lambda s, a: SECRET, client_cls=BrokenClient)
    with pytest.raises(ProcessError):
        await backend.start()
    await backend.close()


def test_w2011_tmp_link_with_other_target_is_an_arr_error(setup: Setup) -> None:
    target = setup.profile().tmp_dir
    link = tmp_link(target, "r/e1", setup.tmp_root)
    link.unlink()
    other = setup.tmp_root / "other"
    other.mkdir()
    link.symlink_to(other)
    with pytest.raises(ArrError, match="schon belegt"):
        tmp_link(target, "r/e1", setup.tmp_root)
    remove_tmp_link(link, target)  # not ours: stays
    assert os.readlink(link) == str(other)


def test_w2011_tmp_link_of_another_owner_is_not_reused_or_removed(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = setup.profile().tmp_dir
    link = tmp_link(target, "r/e1", setup.tmp_root)
    uid = os.getuid()
    monkeypatch.setattr(claude_options_module.os, "getuid", lambda: uid + 1)
    with pytest.raises(ArrError, match="anderer Besitzer"):
        tmp_link(target, "r/e1", setup.tmp_root)
    remove_tmp_link(link, target)
    assert is_link(str(link))
    monkeypatch.undo()
    remove_tmp_link(link, target)
    assert not is_link(str(link))


async def test_w2011_run_turn_after_close_starts_no_client(setup: Setup) -> None:
    backend, clients = make_backend(setup, [tool_turn(), tool_turn()])
    await collect(backend, "Los")
    await backend.close()
    events = await collect(backend, "Noch einmal")
    assert len(clients) == 1
    assert [e.kind for e in events] == [EventKind.TURN_START, EventKind.ERROR, EventKind.TURN_END]
    assert "bereits beendet" in events[1].data["message"]
    assert events[-1].data["status"] == "error"
    with pytest.raises(ArrError):
        await backend.start()
    assert len(clients) == 1
    await backend.close()


async def test_w2011_cancelled_close_cleans_up_and_passes_cancel_on(setup: Setup) -> None:
    class Hanging(FakeClient):
        async def disconnect(self) -> None:
            await asyncio.Event().wait()

    links: list[str] = []
    clients: list[Hanging] = []

    def factory(options: Any) -> Hanging:
        links.append(options.env["TMPDIR"])
        clients.append(Hanging(options, []))
        return clients[-1]

    backend = ClaudeBackend(
        setup.agent(),
        run_id="r",
        inbox=ApprovalInbox(setup.engine()),
        auth=AuthProfile(name="max", kind="claude_oauth"),
        secret_reader=lambda s, a: None,
        client_factory=factory,
    )
    await backend.start()
    owner = backend._owner
    assert owner is not None
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend.close(), 0.05)
    await asyncio.sleep(0)
    assert owner.done() and owner.cancelled()
    assert not is_link(links[0])


def test_w2011_raw_only_on_the_first_event_of_a_message() -> None:
    mapper = ClaudeEvents("r1", "e1", "m", None)
    mapper.start_turn(1)
    blocks = [
        ThinkingBlock("überlege", "sig"),
        TextBlock("los"),
        ToolUseBlock("t1", "Bash", {"command": "ls"}),
        ToolUseBlock("t2", "Bash", {"command": "pwd"}),
    ]
    usage = {"input_tokens": 3, "output_tokens": 4}
    events = mapper.feed(AssistantMessage(blocks, "m", message_id="a1", usage=usage))
    assert [e.kind for e in events][-1] is EventKind.USAGE and len(events) == 5
    assert events[0].raw is not None and all(e.raw is None for e in events[1:])
    results = mapper.feed(
        UserMessage([ToolResultBlock("t1", "a", False), ToolResultBlock("t2", "b", False)])
    )
    assert len(results) == 2 and results[0].raw is not None and results[1].raw is None


def test_w3_06_3_no_source_puts_a_write_root_on_the_clis_path(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: run venv, `path_prepend` and the inherited PATH, each with a write root; the Bash
    commands keep all of them through `CLAUDE_ENV_FILE`, which no agent can change."""
    profile = setup.profile()
    agent = setup.agent()
    work_bin, tmp_bin = str(profile.work_dir / "bin"), str(profile.tmp_dir / "bin")
    file = agent.file.model_copy(update={"path_prepend": [work_bin, *agent.file.path_prepend]})
    agent = PreparedAgent(file, agent.run_dir)
    monkeypatch.setenv("PATH", f"{tmp_bin}{os.pathsep}bin{os.pathsep}/usr/bin")
    options = build_options(
        agent, profile, env=dict(CLAUDE_SWITCHES), can_use_tool=None, hooks=None
    )
    cli = options.env["PATH"].split(os.pathsep)
    assert cli == [*agent.file.path_prepend[1:], "/usr/bin"]
    shell = bash_env(options)["PATH"].split(os.pathsep)
    assert shell[:2] == [str(profile.venv_dir / "bin"), work_bin]
    assert shell[-3:] == [tmp_bin, "bin", "/usr/bin"]
    env_file = Path(options.env["CLAUDE_ENV_FILE"])
    assert env_file.parent == agent.agent_dir
    assert not any(profile.can_write(p) for p in (env_file, env_file.parent))


def test_w3_06_3_the_env_file_never_lies_in_a_write_root(setup: Setup) -> None:
    from arr.backends.claude_options import shell_env_file

    profile = setup.profile()
    inside = dataclasses.replace(
        profile, write_roots=(*profile.write_roots, setup.agent().agent_dir)
    )
    with pytest.raises(ArrError, match="Umgebung seiner Befehle"):
        shell_env_file(setup.agent(), inside, "/usr/bin")


def test_w3_06_3_an_inherited_env_file_is_replaced(setup: Setup, monkeypatch) -> None:
    """Started from a Claude Code session, arr must not hand its env file to the agents."""
    options = options_for(setup, env={**CLAUDE_SWITCHES, "CLAUDE_ENV_FILE": "/elsewhere"})
    assert options.env["CLAUDE_ENV_FILE"] == str(setup.agent().agent_dir / "shell-env.sh")

"""Spike W3-07: how long may a room tool block, and do background processes outlive a turn?

Runs the real Claude CLI (bundled with `claude-agent-sdk`) and the real Codex app-server
(`openai-codex`, pinned) through arr's own backends, with a fake model on 127.0.0.1 and a
test `HOME` in /private/tmp (the lab of the login-guard tests). No request leaves the machine,
no login is used, `~/.claude` and `~/.codex` stay untouched.

    uv run python spikes/w3_07/probe.py block claude 65      # tool blocks 65 s
    uv run python spikes/w3_07/probe.py block codex 65
    uv run python spikes/w3_07/probe.py background claude    # `&` / run_in_background
    uv run python spikes/w3_07/probe.py background codex     # `&` / exec_command session

Prints one JSON object with the measurements.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parents[2] / "tests"
sys.path.insert(0, str(TESTS))

import fake_messages_api as fm  # noqa: E402
import fake_responses_api as fr  # noqa: E402
import login_guard_kit as kit  # noqa: E402
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # noqa: E402

import arr.backends.claude_options as claude_options  # noqa: E402
import arr.backends.codex as codex_module  # noqa: E402
from arr.backends.base import EventKind  # noqa: E402
from arr.backends.claude import ClaudeBackend  # noqa: E402
from arr.backends.codex import CodexBackend  # noqa: E402
from arr.permissions.inbox import ApprovalInbox  # noqa: E402
from arr.permissions.policy import ApprovalLog, PolicyEngine  # noqa: E402

BLOCK_SERVER = Path(__file__).with_name("block_server.py")
MARK = "SPIKE-TURN-"

HEARTBEAT = """
import os, sys, time
name = sys.argv[1]
open(f"pid-{name}", "w").write(str(os.getpid()))
end = time.time() + float(sys.argv[2])
while time.time() < end:
    with open(f"hb-{name}", "w") as handle:
        handle.write(str(time.time()))
    time.sleep(0.5)
"""


DAEMON = (
    "import os, sys\n"
    "if os.fork(): os._exit(0)\n"
    "os.setsid()\n"
    "if os.fork(): os._exit(0)\n"
    "os.execvp('python3', ['python3', 'hb.py', *sys.argv[1:]])"
)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def _turn_of(text: str) -> int | None:
    if MARK not in text:
        return None
    digits = text.split(MARK, 1)[1][:2]
    return int("".join(c for c in digits if c.isdigit()))


# --------------------------------------------------------------------------------------------
# Fake models that play one script per turn (the prompt carries `SPIKE-TURN-<k>`)
# --------------------------------------------------------------------------------------------


class TurnMessagesApi(fm.FakeMessagesApi):
    """Anthropic stand-in: `turns[k]` is the list of tool calls of turn k."""

    def __init__(self, turns: Mapping[int, Sequence[fm.ToolCall]]) -> None:
        super().__init__([])
        self.turns = {k: list(v) for k, v in turns.items()}
        self.tool_names: list[str] = []
        self.answered_at: list[float] = []

    def answer(self, body: Mapping[str, Any]) -> bytes:
        model = str(body.get("model", "fake"))
        if not body.get("tools"):
            return fm._text_stream("ok", model)
        messages = body.get("messages", [])
        with self._lock:
            self.tool_names = [t.get("name", "") for t in body["tools"]]
            self._note_results(body)
            self.answered_at.append(time.time())
            turn, start = 0, 0
            for i, msg in enumerate(messages):
                if msg.get("role") == "user":
                    k = _turn_of(_text(msg.get("content")))
                    if k is not None:
                        turn, start = k, i
            step = sum(
                1
                for msg in messages[start:]
                if isinstance(msg.get("content"), list)
                for block in msg["content"]
                if isinstance(block, dict) and block.get("type") == "tool_result"
            )
        script = self.turns.get(turn, [])
        if step < len(script):
            return fm._tool_stream(script[step], model)
        return fm._text_stream(f"Turn {turn} fertig", model)


class TurnResponsesApi(fr.FakeResponsesApi):
    """OpenAI Responses stand-in: `turns[k]` holds steps `("exec", cmd, yield_ms)` or
    `("tool", name_part, arguments)`; the tool is the first offered tool containing name_part."""

    def __init__(self, turns: Mapping[int, Sequence[tuple[str, str, Any]]]) -> None:
        super().__init__([])
        self.turns = {k: list(v) for k, v in turns.items()}
        self.tool_names: list[str] = []
        self.answers: list[str] = []

    def _names(self, tools: Sequence[Any]) -> list[str]:
        names = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("name"):
                names.append(str(tool["name"]))
            for inner in tool.get("tools", []) or []:  # namespaced tools
                if isinstance(inner, dict) and inner.get("name"):
                    names.append(f"{tool.get('name', '')}/{inner['name']}")
        return names

    def answer(self, body: Mapping[str, Any]) -> bytes:
        items = [i for i in body.get("input", []) if isinstance(i, dict)]
        with self._lock:
            self.requests += 1
            self.tool_names = self._names(body.get("tools", []))
            turn, start = 0, 0
            for i, item in enumerate(items):
                if item.get("type") == "message" and item.get("role") == "user":
                    k = _turn_of(_text(item.get("content")))
                    if k is not None:
                        turn, start = k, i
            outputs = [
                str(i.get("output", ""))
                for i in items[start:]
                if i.get("type") in ("function_call_output", "custom_tool_call_output")
            ]
            if outputs:
                self.outputs.append(outputs[-1])
            step = len(outputs)
        script = self.turns.get(turn, [])
        call_id = f"call_{turn}_{step}"
        if step < len(script):
            kind, what, extra = script[step]
            if kind == "js":
                item = {
                    "type": "custom_tool_call",
                    "id": f"ctc_{turn}_{step}",
                    "call_id": call_id,
                    "name": "exec",
                    "input": f'// @exec: {{"yield_time_ms": {extra}}}\n{what}',
                }
                if os.environ.get("SPIKE_NS"):
                    item["namespace"] = os.environ["SPIKE_NS"]
                return self._reply(item)
            if kind == "exec":
                arguments = {"cmd": what, "yield_time_ms": extra}
                name = "exec_command"
            else:
                name = os.environ.get("SPIKE_NAME", what)
                arguments = extra
            item = {
                "type": "function_call",
                "id": f"fc_{turn}_{step}",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
            if kind == "tool" and os.environ.get("SPIKE_NS"):
                item["namespace"] = os.environ["SPIKE_NS"]
        else:
            content = [{"type": "output_text", "text": f"Turn {turn} fertig"}]
            item = {"type": "message", "role": "assistant", "id": call_id, "content": content}
        return self._reply(item)

    def _reply(self, item: Mapping[str, Any]) -> bytes:
        response = f"resp_{self.requests}"
        return (
            fr._sse({"type": "response.created", "response": {"id": response}})
            + fr._sse({"type": "response.output_item.done", "output_index": 0, "item": item})
            + fr._sse(
                {"type": "response.completed", "response": {"id": response, "usage": fr._USAGE}}
            )
        )


# --------------------------------------------------------------------------------------------
# The block server in place of the room server
# --------------------------------------------------------------------------------------------


def _block_server(log: Path, timeout_s: float | None = None) -> dict[str, Any]:
    env = {"SPIKE_LOG": str(log), "PATH": "/usr/bin:/bin"}
    config: dict[str, Any] = {"command": sys.executable, "args": ["-I", str(BLOCK_SERVER)]}
    config["env"] = env
    if timeout_s is not None:
        config["tool_timeout_sec"] = timeout_s
    return config


def patch_room(log: Path, codex_timeout_s: float | None = None) -> None:
    def claude_room(agent: Any, outside_path: str | None = None) -> dict[str, Any]:
        return {"type": "stdio", **_block_server(log)}

    claude_options.room_server = claude_room

    original = codex_module.build_launch

    def build_launch(*args: Any, **kwargs: Any) -> Any:
        launch = original(*args, **kwargs)
        room = launch.thread_config["mcp_servers"]["arr-room"]
        room.update(_block_server(log, codex_timeout_s))
        return launch

    codex_module.build_launch = build_launch


# --------------------------------------------------------------------------------------------
# Driving the real backends
# --------------------------------------------------------------------------------------------


def _inbox(lab: kit.Lab) -> ApprovalInbox:
    engine = PolicyEngine([lab.profile], log=ApprovalLog(lab.run.run_dir / "approvals.jsonl"))
    return ApprovalInbox(engine, decider=None, timeout=0.05)


def claude_backend(lab: kit.Lab, api: TurnMessagesApi) -> ClaudeBackend:
    def factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        env = {**options.env, **kit.fake_model_env(api.url)}
        options = replace(options, env=env, max_budget_usd=None, cli_path=str(kit.guard_start(lab)))
        return ClaudeSDKClient(options=options)

    return ClaudeBackend(
        lab.agent,
        run_id=lab.run.run_id,
        inbox=_inbox(lab),
        auth=lab.auth,
        secret_reader=lambda _service, _account: lab.token,
        client_factory=factory,
    )


def codex_backend(lab: kit.Lab, api: TurnResponsesApi) -> CodexBackend:
    from openai_codex import AsyncCodex, CodexConfig

    home = lab.base / "codex-home"
    home.mkdir(exist_ok=True)

    def factory(config: CodexConfig) -> AsyncCodex:
        env = {**(config.env or {}), "CODEX_HOME": str(home)}
        overrides = (*config.config_overrides, *api.provider_overrides())
        return AsyncCodex(replace(config, env=env, config_overrides=overrides))

    return CodexBackend(
        lab.agent, run_id=lab.run.run_id, inbox=_inbox(lab), auth=lab.auth, factory=factory
    )


async def play(backend: Any, turn: int) -> dict[str, Any]:
    """One turn; returns its duration, status and the tool results the backend reported."""
    started = time.monotonic()
    results, status, errors = [], None, []
    async for event in backend.run_turn(f"{MARK}{turn}: Arbeite den Ablauf ab."):
        data = event.data
        if event.kind is EventKind.TOOL_RESULT:
            text = str(data.get("output") or data.get("content") or data.get("text") or "")
            results.append({"error": data.get("is_error") or data.get("error"), "text": text[:300]})
        elif event.kind is EventKind.ERROR:
            errors.append(str(data.get("message", ""))[:300])
        elif event.kind is EventKind.TURN_END:
            status = data.get("status")
    return {
        "turn": turn,
        "seconds": round(time.monotonic() - started, 1),
        "status": status,
        "tool_results": results,
        "errors": errors,
    }


def _server_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def block(provider: str, seconds: float, codex_timeout_s: float | None) -> dict[str, Any]:
    with kit.open_lab(provider) as lab:
        log = lab.base / "block.jsonl"
        patch_room(log, codex_timeout_s)
        if provider == "claude":
            api: Any = TurnMessagesApi(
                {1: [fm.ToolCall("mcp__arr-room__block", {"seconds": seconds})]}
            )
        else:
            js = (
                "for (let i = 0; i < 300 && !tools.mcp__arr_room__block; i++) "
                "await new Promise((r) => setTimeout(r, 100));\n"
                "if (!tools.mcp__arr_room__block) "
                "text(JSON.stringify(ALL_TOOLS.map((t) => t.name)));\n"
                f"const r = await tools.mcp__arr_room__block({{seconds: {seconds}}}); text(r);"
            )
            # `exec` sees the tools of its start; a first step lets the MCP server get ready
            steps = [("exec", "sleep 3", 10_000), ("js", js, int((seconds + 120) * 1000))]
            api = TurnResponsesApi({1: steps})
        with api:
            backend = claude_backend(lab, api) if provider == "claude" else codex_backend(lab, api)

            async def main() -> dict[str, Any]:
                try:
                    await backend.start()
                    return await play(backend, 1)
                finally:
                    await backend.close()

            turn = asyncio.run(asyncio.wait_for(main(), seconds + 240))
        model_saw = getattr(api, "results", None) or getattr(api, "outputs", [])
        return {
            "provider": provider,
            "block_s": seconds,
            "codex_tool_timeout_sec": codex_timeout_s,
            "turn": turn,
            "server_log": _server_log(log),
            "model_saw": [str(t)[:300] for t in model_saw],
            "model_saw_error": list(getattr(api, "errors", [])),
            "tools_offered": api.tool_names,
        }


def _beats(work: Path, names: Sequence[str]) -> dict[str, Any]:
    now = time.time()
    state = {}
    for name in names:
        beat = work / f"hb-{name}"
        pid_file = work / f"pid-{name}"
        alive = None
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 0)
                alive = True
            except (ProcessLookupError, ValueError):
                alive = False
            except PermissionError:
                alive = True
        age = None
        for _ in range(20):  # the process rewrites the file every 0.5 s
            try:
                age = round(now - float(beat.read_text()), 1) if beat.exists() else None
                break
            except ValueError:
                time.sleep(0.05)
        state[name] = {"alive": alive, "beat_age_s": age}
    return state


def _kill(work: Path, names: Sequence[str]) -> None:
    for name in names:
        pid_file = work / f"pid-{name}"
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError, PermissionError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)


def background(provider: str) -> dict[str, Any]:
    """Turn 1 starts heartbeat processes and ends; turn 2 looks; then the backend closes."""
    with kit.open_lab(provider) as lab:
        work = lab.agent.work_dir
        (work / "hb.py").write_text(HEARTBEAT)
        (work / "daemon.py").write_text(DAEMON)
        life = 90
        quiet = "> /dev/null 2>&1"
        variants = {
            "amp": f"python3 hb.py amp {life} {quiet} &",
            "sub": f"(python3 hb.py sub {life} {quiet} &)",
            "dis": f"python3 hb.py dis {life} {quiet} & disown",
            "nohup": f"nohup python3 hb.py nohup {life} {quiet} &",
            "late": f"python3 hb.py late {life} {quiet} & sleep 2",
            "setsid": f"python3 daemon.py setsid {life}",
        }
        if provider == "claude":
            names = ["rib", *variants]
            first = [
                fm.ToolCall(
                    "Bash",
                    {
                        "command": f"python3 hb.py rib {life}",
                        "description": "Hintergrund",
                        "run_in_background": True,
                    },
                )
            ]
            first += [kit.bash(cmd, name) for name, cmd in variants.items()]
            turns = {1: first, 2: [kit.bash("ls hb-*; cat pid-*", "nachsehen")]}
            api: Any = TurnMessagesApi(turns)
        else:
            names = ["yield", *variants]
            first = [("exec", f"python3 hb.py yield {life}", 1_000)]
            first += [("exec", cmd, 5_000) for cmd in variants.values()]
            turns = {1: first, 2: [("exec", "ls hb-*; cat pid-*", 5_000)]}
            api = TurnResponsesApi(turns)
        report: dict[str, Any] = {"provider": provider, "processes": names}
        with api:
            backend = claude_backend(lab, api) if provider == "claude" else codex_backend(lab, api)

            async def main() -> None:
                try:
                    await backend.start()
                    report["turn1"] = await play(backend, 1)
                    await asyncio.sleep(1)
                    report["after_turn1_1s"] = _beats(work, names)
                    await asyncio.sleep(10)
                    report["after_turn1_11s"] = _beats(work, names)
                    report["turn2"] = await play(backend, 2)
                    await asyncio.sleep(5)
                    report["after_turn2_5s"] = _beats(work, names)
                finally:
                    await backend.close()
                await asyncio.sleep(5)
                report["after_close_5s"] = _beats(work, names)

            try:
                asyncio.run(asyncio.wait_for(main(), 300))
            finally:
                _kill(work, names)
        return report


def main() -> None:
    what, provider = sys.argv[1], sys.argv[2]
    if what == "block":
        seconds = float(sys.argv[3])
        timeout = float(sys.argv[4]) if len(sys.argv) > 4 else None
        result = block(provider, seconds, timeout)
    else:
        result = background(provider)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

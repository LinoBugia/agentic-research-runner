"""Shared helpers for the S1 spike scripts (throwaway code, never imported by arr).

- clean_env(): drop auth and parent-session variables from os.environ, because
  ClaudeAgentOptions.env is only merged on top of the inherited environment.
- Recorder: tees the raw JSON lines between SDK and CLI into a JSONL file.
- Ledger: appends usage and cost of every ResultMessage to tmp/usage.jsonl and
  enforces the turn limit of the assignment.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

SPIKE_DIR = Path(__file__).resolve().parent
TMP = SPIKE_DIR / "tmp"
RUNS = TMP / "runs"
LEDGER = TMP / "usage.jsonl"

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"

MAX_TOTAL_TURNS = 60
MAX_BUDGET_USD = 1.0

# Variables that select a credential or tie the child to a parent Claude Code session.
_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
_DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
_DROP_EXACT = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR")


# Passed via ClaudeAgentOptions.env: without it Claude Code fetches the account's
# claude.ai connectors even with setting_sources=[] (q01: ~512k cache tokens per session).
AGENT_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def clean_env() -> list[str]:
    """Remove auth and parent-session variables; return the removed names (never values)."""
    removed = []
    for key in list(os.environ):
        if key in _AUTH_VARS or key in _DROP_EXACT or key.startswith(_DROP_PREFIXES):
            del os.environ[key]
            removed.append(key)
    return sorted(removed)


def workdir(name: str, fresh: bool = True) -> Path:
    """Return an empty working folder under tmp/work/<name>."""
    path = TMP / "work" / name
    if fresh and path.exists():
        import shutil

        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


class Recorder:
    """Monkeypatches the subprocess transport to log raw messages in both directions."""

    def __init__(self, name: str) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        self.path = RUNS / f"{name}.jsonl"
        self.path.write_text("")
        self._install()

    def _log(self, direction: str, payload: Any) -> None:
        with self.path.open("a") as fh:
            fh.write(json.dumps({"dir": direction, "t": round(time.time(), 3), "msg": payload}))
            fh.write("\n")

    def _install(self) -> None:
        recorder = self
        original_read = SubprocessCLITransport.read_messages
        original_write = SubprocessCLITransport.write

        async def read_messages(self):  # type: ignore[no-untyped-def]
            async for data in original_read(self):
                recorder._log("in", data)
                yield data

        async def write(self, data: str) -> None:  # type: ignore[no-untyped-def]
            for line in data.splitlines():
                try:
                    recorder._log("out", json.loads(line))
                except json.JSONDecodeError:
                    recorder._log("out", line)
            await original_write(self, data)

        SubprocessCLITransport.read_messages = read_messages  # type: ignore[method-assign]
        SubprocessCLITransport.write = write  # type: ignore[method-assign]


def turns_used() -> int:
    if not LEDGER.exists():
        return 0
    return sum(json.loads(line).get("num_turns") or 0 for line in LEDGER.read_text().splitlines())


def cost_used() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(
        json.loads(line).get("cost_delta_usd") or 0.0 for line in LEDGER.read_text().splitlines()
    )


def check_turn_budget(planned: int) -> None:
    used = turns_used()
    if used + planned > MAX_TOTAL_TURNS:
        raise SystemExit(
            f"Turn-Limit: {used} verbraucht + {planned} geplant > {MAX_TOTAL_TURNS}. Abbruch."
        )


def record_result(
    script: str, label: str, model: str, msg: ResultMessage, prev_cost: float
) -> dict[str, Any]:
    # usage and num_turns are per query; total_cost_usd and model_usage are cumulative
    # per session, so the cost of this query is the delta.
    row = {
        "script": script,
        "label": label,
        "model": model,
        "subtype": msg.subtype,
        "num_turns": msg.num_turns,
        "duration_ms": msg.duration_ms,
        "total_cost_usd": msg.total_cost_usd,
        "cost_delta_usd": (msg.total_cost_usd or 0.0) - prev_cost,
        "usage": msg.usage,
        "model_usage": getattr(msg, "model_usage", None),
    }
    TMP.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace(str(Path.home()), "~")
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit})"


async def run_turn(
    client: ClaudeSDKClient, prompt: str, *, script: str, label: str, model: str
) -> dict[str, Any]:
    """Send one prompt, print a condensed event log, return a summary of the turn."""
    print(f"\n=== {label} ===\n> {short(prompt, 200)}")
    await client.query(prompt)
    summary: dict[str, Any] = {
        "thinking": [],
        "text": [],
        "tool_calls": [],
        "tool_results": [],
        "per_message_usage": [],
        "result": None,
    }
    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage):
            print(f"[system/{msg.subtype}]")
            if msg.subtype == "init":
                summary["init"] = msg.data
        elif isinstance(msg, AssistantMessage):
            if msg.usage:
                summary["per_message_usage"].append(msg.usage)
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    summary["thinking"].append(block.thinking)
                    print(f"[thinking {len(block.thinking)} chars] {short(block.thinking, 160)}")
                elif isinstance(block, TextBlock):
                    summary["text"].append(block.text)
                    print(f"[text] {short(block.text, 400)}")
                elif isinstance(block, ToolUseBlock):
                    summary["tool_calls"].append({"name": block.name, "input": block.input})
                    print(f"[tool_call {block.name}] {short(block.input, 300)}")
        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        summary["tool_results"].append(
                            {"is_error": block.is_error, "content": block.content}
                        )
                        print(f"[tool_result err={block.is_error}] {short(block.content, 300)}")
        elif isinstance(msg, ResultMessage):
            prev = getattr(client, "_spike_cost", 0.0)
            row = record_result(script, label, model, msg, prev)
            client._spike_cost = msg.total_cost_usd or prev  # type: ignore[attr-defined]
            summary["result"] = row
            print(
                f"[result {msg.subtype}] turns={msg.num_turns} cost=${msg.total_cost_usd} "
                f"usage={short(msg.usage, 400)}"
            )
        else:
            print(f"[{type(msg).__name__}] {short(dataclasses_repr(msg), 200)}")
    return summary


def dataclasses_repr(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    return repr(obj)


def init_facts(init: dict[str, Any] | None) -> dict[str, Any]:
    """Pick the fields of the init message that matter for the spike questions."""
    if not init:
        return {}
    keys = ("model", "apiKeySource", "permissionMode", "tools", "mcp_servers", "skills", "plugins")
    return {k: init.get(k) for k in keys if k in init}

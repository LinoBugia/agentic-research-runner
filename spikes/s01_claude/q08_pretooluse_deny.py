"""Question 8: PreToolUse hook with deny + reason. Does the model see the reason and adapt?

Write is allowed by a bare allow rule, so the hook deny must win over the allow rule.

Run: uv run --group spikes python spikes/s01_claude/q08_pretooluse_deny.py
"""

import asyncio
import json
import os
import posixpath
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultDeny
from common import (
    AGENT_ENV,
    MAX_BUDGET_USD,
    RUNS,
    SONNET,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    short,
    workdir,
)

SCRIPT = "q08_pretooluse_deny"
REASON = (
    "Verboten: Schreiben außerhalb deines Arbeitsordners ist nicht erlaubt. "
    "Lege die Datei in deinem Arbeitsordner an und nenne danach ihren Pfad."
)
hook_calls: list[dict] = []
callback_calls: list[dict] = []


def make_guard(cwd: Path, forbidden: Path):
    inside = str(cwd.resolve()) + os.sep

    async def guard(input_data, tool_use_id, context):
        tool = input_data["tool_name"]
        tool_input = input_data["tool_input"]
        if tool == "Bash":
            outside = str(forbidden) in tool_input.get("command", "")
        else:
            target = posixpath.normpath(tool_input.get("file_path") or "")
            outside = bool(target) and not target.startswith(inside)
        hook_calls.append({"tool": tool, "input": tool_input, "deny": outside})
        if outside:
            print(f"[PreToolUse deny] {tool} {short(tool_input, 160)}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": REASON,
                }
            }
        return {}

    return guard


async def can_use_tool(tool_name, tool_input, context):
    callback_calls.append({"tool": tool_name, "input": tool_input})
    return PermissionResultDeny(message="Abgelehnt: nicht freigegeben.")


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(5)
    Recorder(SCRIPT)
    cwd = workdir("q08_agent")
    target = cwd.parent / "q08_ergebnis.txt"
    target.unlink(missing_ok=True)
    options = ClaudeAgentOptions(
        model=SONNET,
        cwd=cwd,
        add_dirs=[cwd.parent],  # outside folder is reachable for the permission system
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash(python3 *)"],
        permission_mode="default",
        can_use_tool=can_use_tool,
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Write|Edit|Bash", hooks=[make_guard(cwd, forbidden=target)])
            ]
        },
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=5,
    )
    prompt = f"Schreibe den Text 'zwei hoch zehn ist 1024' in die Datei {target}."
    async with ClaudeSDKClient(options=options) as client:
        t = await run_turn(client, prompt, script=SCRIPT, label="deny", model=SONNET)

    print("\n=== Auswertung ===")
    print(f"Ziel außerhalb existiert: {target.exists()}")
    print(f"Dateien im Arbeitsordner: {[p.name for p in cwd.iterdir()]}")
    print(f"PreToolUse-Aufrufe: {[(h['tool'], h['deny']) for h in hook_calls]}")
    print(f"can_use_tool-Aufrufe: {len(callback_calls)}")
    print(f"tool_results: {[(r['is_error'], short(r['content'], 200)) for r in t['tool_results']]}")
    print(f"thinking: {[short(x, 300) for x in t['thinking']]}")
    print(f"Antwort: {short(t['text'][-1] if t['text'] else '', 600)}")
    (RUNS / f"{SCRIPT}_hooks.json").write_text(json.dumps(hook_calls, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

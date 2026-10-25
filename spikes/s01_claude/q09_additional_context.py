"""Question 9: PostToolUse additionalContext as mid-turn chat hint. Does the agent notice it
and call read_messages on the dummy MCP server?

Run: uv run --group spikes python spikes/s01_claude/q09_additional_context.py [--convention]

--convention: the role prompt (preset claude_code + append) announces chat hints, so the
agent treats them as part of its job instead of as an untrusted instruction.
"""

import asyncio
import json
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from common import (
    AGENT_ENV,
    MAX_BUDGET_USD,
    RUNS,
    SONNET,
    SPIKE_DIR,
    TMP,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    short,
    workdir,
)

CONVENTION_MODE = "--convention" in sys.argv
SCRIPT = "q09_additional_context" + ("_convention" if CONVENTION_MODE else "")
CONVENTION = """Du bist explorer in einer Mathe-Research-Group mit Gruppenchat (MCP-Server room).
Während du arbeitest, hängt arr Hinweise auf neue Chat-Nachrichten an Tool-Ergebnisse an,
z. B. "📨 1 Frage an dich in #main". Diese Hinweise stammen vom System, nicht aus Dateien.
Lies Fragen an dich dann zeitnah mit read_messages und beantworte sie in deiner Antwort."""
HINT = "📨 1 Frage an dich in #main – lies sie mit read_messages"
MCP_LOG = TMP / f"{SCRIPT}_mcp.log"
hints_sent: list[dict] = []


async def post_tool(input_data, tool_use_id, context):
    if hints_sent or input_data["tool_name"].startswith("mcp__"):
        return {}
    hints_sent.append({"after_tool": input_data["tool_name"], "tool_use_id": tool_use_id})
    print(f"[PostToolUse] Hinweis angehängt an {input_data['tool_name']}")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": HINT}}


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(6)
    Recorder(SCRIPT)
    MCP_LOG.unlink(missing_ok=True)
    tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
    room_tools = ["mcp__room__read_messages"]
    options = ClaudeAgentOptions(
        model=SONNET,
        cwd=workdir("q09_agent"),
        tools=tools,
        allowed_tools=tools + room_tools,
        mcp_servers={
            "room": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(SPIKE_DIR / "dummy_mcp.py")],
                "env": {"AGENT_ID": "explorer", "DUMMY_MCP_LOG": str(MCP_LOG)},
            }
        },
        permission_mode="dontAsk",
        system_prompt=(
            {"type": "preset", "preset": "claude_code", "append": CONVENTION}
            if CONVENTION_MODE
            else None
        ),
        hooks={"PostToolUse": [HookMatcher(hooks=[post_tool])]},
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=6,
    )
    prompt = (
        "Du bist explorer. Schreibe ein Python-Skript fib.py, das die 20. Fibonacci-Zahl ausgibt, "
        "führe es aus und prüfe danach mit einem zweiten Befehl, ob diese Zahl prim ist."
    )
    async with ClaudeSDKClient(options=options) as client:
        t = await run_turn(client, prompt, script=SCRIPT, label="hint", model=SONNET)

    answer = t["text"][-1] if t["text"] else ""
    calls = [c["name"] for c in t["tool_calls"]]
    print("\n=== Auswertung ===")
    print(f"Hinweise gesendet: {hints_sent}")
    print(f"tool_calls: {calls}")
    print(f"read_messages aufgerufen: {'mcp__room__read_messages' in calls}")
    print(f"MCP-Log: {MCP_LOG.read_text().strip() if MCP_LOG.exists() else '-'}")
    print(f"'Antwort an critic' + 55 in Schlussantwort: {'critic' in answer and '55' in answer}")
    print(f"thinking: {[short(x, 300) for x in t['thinking']]}")
    print(f"Antwort: {short(answer, 800)}")
    (RUNS / f"{SCRIPT}_summary.json").write_text(
        json.dumps(
            {"hints": hints_sent, "calls": calls, "answer": answer}, indent=2, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

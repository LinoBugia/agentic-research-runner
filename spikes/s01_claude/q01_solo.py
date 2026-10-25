"""Questions 1, 2, 5a: solo agent, 3 turns, thinking, caching, tool results, auth.

Run: uv run --group spikes python spikes/s01_claude/q01_solo.py
"""

import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from common import (
    AGENT_ENV,
    HAIKU,
    MAX_BUDGET_USD,
    RUNS,
    Recorder,
    check_turn_budget,
    clean_env,
    init_facts,
    run_turn,
    workdir,
)

SCRIPT = "q01_solo"
TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

PROMPTS = [
    "Ist 2^13 - 1 eine Primzahl? Begründe in zwei Sätzen, ohne Tools.",
    "Schreibe ein Python-Skript primes.py, das alle Primzahlen unter 50 ausgibt, "
    "und führe es mit python3 aus. Antworte danach nur mit der Anzahl.",
    "Wie viele Primzahlzwillings-Paare sind darunter? Antworte in einem Satz, ohne Tools.",
]

post_tool_inputs: list[dict] = []


async def on_post_tool(input_data, tool_use_id, context):
    post_tool_inputs.append(dict(input_data))
    return {}


async def main() -> None:
    removed = clean_env()
    print(f"clean_env entfernt: {removed}")
    check_turn_budget(8)
    Recorder(SCRIPT)
    cwd = workdir(SCRIPT)

    options = ClaudeAgentOptions(
        model=HAIKU,
        cwd=cwd,
        tools=TOOLS,
        allowed_tools=TOOLS,
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=6,
        hooks={"PostToolUse": [HookMatcher(hooks=[on_post_tool])]},
    )
    turns = []
    async with ClaudeSDKClient(options=options) as client:
        for i, prompt in enumerate(PROMPTS, start=1):
            turns.append(
                await run_turn(client, prompt, script=SCRIPT, label=f"turn{i}", model=HAIKU)
            )

    print("\n=== Auswertung ===")
    print("init:", json.dumps(init_facts(turns[0].get("init")), ensure_ascii=False))
    for i, t in enumerate(turns, start=1):
        filled = [len(x) for x in t["thinking"]]
        usage = t["result"]["usage"] if t["result"] else {}
        print(
            f"turn{i}: thinking_blocks={filled} "
            f"input={usage.get('input_tokens')} "
            f"cache_creation={usage.get('cache_creation_input_tokens')} "
            f"cache_read={usage.get('cache_read_input_tokens')} "
            f"output={usage.get('output_tokens')}"
        )
        for j, u in enumerate(t["per_message_usage"]):
            print(
                f"   msg{j}: cache_creation={u.get('cache_creation_input_tokens')} "
                f"cache_read={u.get('cache_read_input_tokens')}"
            )

    stream_results = turns[1]["tool_results"]
    print(f"\nToolResultBlocks im Stream (turn2): {len(stream_results)}")
    print(f"PostToolUse-Aufrufe: {len(post_tool_inputs)}")
    for p in post_tool_inputs:
        print(f"  {p['tool_name']}: keys={sorted(p)}")
        print(f"    tool_response={json.dumps(p.get('tool_response'), default=str)[:400]}")
    (RUNS / f"{SCRIPT}_posttooluse.json").write_text(
        json.dumps(post_tool_inputs, indent=2, default=str, ensure_ascii=False)
    )


if __name__ == "__main__":
    asyncio.run(main())

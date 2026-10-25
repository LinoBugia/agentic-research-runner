"""Question 3: system prompt preset claude_code + append vs. role file only.

Agent A: preset + append (exclude_dynamic_sections=True), agent B1 and B2: role file.
B2 starts fresh after B1 to see whether agents with the same role share the cache.

Run: uv run --group spikes python spikes/s01_claude/q03_system_prompt.py
"""

import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from common import (
    AGENT_ENV,
    MAX_BUDGET_USD,
    SONNET,
    SPIKE_DIR,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    workdir,
)

SCRIPT = "q03_system_prompt"
TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
ROLE_FILE = SPIKE_DIR / "role_explorer.md"

TASK = (
    "Wie groß ist die Summe der ersten 1000 Primzahlen? "
    "Prüfe außerdem, ob diese Summe selbst eine Primzahl ist."
)
FOLLOW_UP = "In welcher Datei steht dein Skript? Antworte in einem Satz, ohne Tools."


def options(name: str, system_prompt) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=SONNET,
        cwd=workdir(name),
        tools=TOOLS,
        allowed_tools=TOOLS,
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        system_prompt=system_prompt,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD / 3,
        max_turns=6,
    )


async def run_agent(name: str, system_prompt, prompts: list[str]) -> list[dict]:
    out = []
    async with ClaudeSDKClient(options=options(name, system_prompt)) as client:
        for i, prompt in enumerate(prompts, start=1):
            out.append(
                await run_turn(client, prompt, script=SCRIPT, label=f"{name}/q{i}", model=SONNET)
            )
    return out


def report(name: str, turns: list[dict]) -> None:
    for i, t in enumerate(turns, start=1):
        u = t["result"]["usage"]
        first = t["per_message_usage"][0] if t["per_message_usage"] else {}
        tools = [c["name"] for c in t["tool_calls"]]
        print(
            f"{name}/q{i}: tools={tools} turns={t['result']['num_turns']} "
            f"first_msg cache_creation={first.get('cache_creation_input_tokens')} "
            f"cache_read={first.get('cache_read_input_tokens')} | query total "
            f"cache_creation={u.get('cache_creation_input_tokens')} "
            f"cache_read={u.get('cache_read_input_tokens')} "
            f"cost=${t['result']['cost_delta_usd']:.4f}"
        )
        print(
            f"   answer: {json.dumps(t['text'][-1] if t['text'] else '', ensure_ascii=False)[:300]}"
        )


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(12)
    Recorder(SCRIPT)
    role = ROLE_FILE.read_text()

    preset = {
        "type": "preset",
        "preset": "claude_code",
        "append": role,
        "exclude_dynamic_sections": True,
    }
    role_file = {"type": "file", "path": str(ROLE_FILE)}

    a = await run_agent("A_preset", preset, [TASK, FOLLOW_UP])
    b1 = await run_agent("B1_file", role_file, [TASK, FOLLOW_UP])
    b2 = await run_agent("B2_file", role_file, [FOLLOW_UP])

    print("\n=== Auswertung ===")
    report("A_preset", a)
    report("B1_file", b1)
    report("B2_file", b2)


if __name__ == "__main__":
    asyncio.run(main())

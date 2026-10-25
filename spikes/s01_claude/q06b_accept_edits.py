"""Question 6/7 (addendum): permission_mode="acceptEdits" + sandbox. Do edits inside the
work folder run without approval, and do edits outside still reach can_use_tool?

Run: uv run --group spikes python spikes/s01_claude/q06b_accept_edits.py
"""

import asyncio

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny
from common import (
    AGENT_ENV,
    HAIKU,
    MAX_BUDGET_USD,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    short,
    workdir,
)

SCRIPT = "q06b_accept_edits"
callback_calls: list[dict] = []


async def can_use_tool(tool_name, tool_input, context):
    callback_calls.append({"tool": tool_name, "reason": context.decision_reason})
    print(f"[can_use_tool] {tool_name} {short(tool_input, 160)} reason={context.decision_reason}")
    return PermissionResultDeny(message="Abgelehnt: Schreiben außerhalb des Arbeitsordners.")


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(4)
    Recorder(SCRIPT)
    cwd = workdir("q06b_agent")
    outside = cwd.parent / "q06b_outside.txt"
    outside.unlink(missing_ok=True)
    options = ClaudeAgentOptions(
        model=HAIKU,
        cwd=cwd,
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="acceptEdits",
        can_use_tool=can_use_tool,
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "network": {"allowedDomains": []},
        },
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=4,
    )
    prompt = (
        "Lege mit dem Write-Tool zwei Dateien mit Inhalt 'x' an, beide in einer einzigen "
        f"Antwort (parallel): a.txt in deinem Arbeitsordner und {outside}. "
        "Berichte danach in einem Satz, was geklappt hat."
    )
    async with ClaudeSDKClient(options=options) as client:
        t = await run_turn(client, prompt, script=SCRIPT, label="accept_edits", model=HAIKU)

    print("\n=== Auswertung ===")
    print(f"a.txt im Arbeitsordner: {(cwd / 'a.txt').exists()}")
    print(f"Datei außerhalb: {outside.exists()}")
    print(f"can_use_tool-Aufrufe: {callback_calls}")
    print(f"tool_results: {[(r['is_error'], short(r['content'], 120)) for r in t['tool_results']]}")


if __name__ == "__main__":
    asyncio.run(main())

"""Question 6: SandboxSettings. Writes outside the work folder via Bash, extra writable
folder, network, and what reaches can_use_tool.

Run: uv run --group spikes python spikes/s01_claude/q06_sandbox.py
"""

import asyncio
import json

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, PermissionResultDeny
from common import (
    AGENT_ENV,
    HAIKU,
    MAX_BUDGET_USD,
    RUNS,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    short,
    workdir,
)

SCRIPT = "q06_sandbox"
callback_calls: list[dict] = []


async def can_use_tool(tool_name, tool_input, context):
    entry = {
        "tool": tool_name,
        "input": tool_input,
        "decision_reason": context.decision_reason,
        "blocked_path": context.blocked_path,
        "title": context.title,
        "suggestions": [str(s) for s in context.suggestions],
    }
    callback_calls.append(entry)
    print(f"[can_use_tool] {tool_name} {short(tool_input, 200)} reason={context.decision_reason}")
    return PermissionResultDeny(message="Abgelehnt (Spike-Test): keine Freigabe.")


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(6)
    Recorder(SCRIPT)
    cwd = workdir("q06_agent")
    extra = workdir("q06_extra_writable")
    outside = cwd.parent / "q06_outside.txt"
    outside_write = cwd.parent / "q06_outside_write_tool.txt"
    for p in (outside, outside_write):
        p.unlink(missing_ok=True)

    sandbox = {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
        "network": {"allowedDomains": []},
        # Not declared in the SandboxSettings TypedDict, but forwarded to Claude Code.
        "filesystem": {"allowWrite": [str(extra)]},
    }
    options = ClaudeAgentOptions(
        model=HAIKU,
        cwd=cwd,
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="default",
        can_use_tool=can_use_tool,
        sandbox=sandbox,
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=6,
    )
    command = (
        "python3 -c \"open('inside.txt','w').write('ok')\" && echo INSIDE_OK; "
        f"echo x > {outside} && echo OUTSIDE_OK; "
        f"echo x > {extra}/extra.txt && echo EXTRA_OK"
    )
    prompt = f"""Das ist ein Test der Sandbox. Führe genau diese Schritte aus, ohne Alternativen
zu probieren, und berichte danach für jeden Schritt kurz, ob er geklappt hat:
1. Ein einziger Bash-Aufruf mit genau diesem Befehl:
   {command}
2. Ein zweiter Bash-Aufruf: curl -sS -m 5 -o /dev/null -w '%{{http_code}}' https://example.com
3. Mit dem Write-Tool (nicht Bash) die Datei {outside_write} mit Inhalt "x" anlegen."""

    async with ClaudeSDKClient(options=options) as client:
        t = await run_turn(client, prompt, script=SCRIPT, label="sandbox", model=HAIKU)

    print("\n=== Auswertung ===")
    print(f"inside.txt existiert: {(cwd / 'inside.txt').exists()}")
    print(f"outside (Bash) existiert: {outside.exists()}")
    print(f"extra (allowWrite) existiert: {(extra / 'extra.txt').exists()}")
    print(f"outside (Write-Tool) existiert: {outside_write.exists()}")
    print(f"can_use_tool-Aufrufe: {len(callback_calls)}")
    for c in callback_calls:
        print(
            f"  {c['tool']}: reason={c['decision_reason']} blocked_path={short(c['blocked_path'])}"
        )
    for r in t["tool_results"]:
        print(f"  tool_result err={r['is_error']}: {short(r['content'], 400)}")
    (RUNS / f"{SCRIPT}_callbacks.json").write_text(
        json.dumps(callback_calls, indent=2, default=str, ensure_ascii=False)
    )


if __name__ == "__main__":
    asyncio.run(main())

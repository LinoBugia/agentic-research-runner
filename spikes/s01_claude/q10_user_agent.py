"""Question 10 (S7): project folder with .mcp.json (dummy stdio server, tool ping) and a
symlinked skill in .claude/skills. Agent with setting_sources=["project"], like Claude Code
for the user. Run q04_skills.py first (it creates the skill source).

Run: uv run --group spikes python spikes/s01_claude/q10_user_agent.py
"""

import asyncio
import json
import os
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from common import (
    AGENT_ENV,
    HAIKU,
    MAX_BUDGET_USD,
    SPIKE_DIR,
    TMP,
    Recorder,
    check_turn_budget,
    clean_env,
    init_facts,
    run_turn,
    short,
    workdir,
)

SCRIPT = "q10_user_agent"
SKILL = "arr-probe"
SKILLS_ROOT = TMP / "skill_source" / "skills"
MCP_LOG = TMP / "q10_mcp.log"


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    if not SKILLS_ROOT.exists():
        raise SystemExit("Erst q04_skills.py ausführen.")
    check_turn_budget(5)
    Recorder(SCRIPT)
    MCP_LOG.unlink(missing_ok=True)

    project = workdir("q10_project")
    (project / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "arr": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(SPIKE_DIR / "dummy_mcp.py")],
                        "env": {"AGENT_ID": "user-agent", "DUMMY_MCP_LOG": str(MCP_LOG)},
                    }
                }
            },
            indent=2,
        )
    )
    (project / ".claude" / "skills").mkdir(parents=True)
    os.symlink(SKILLS_ROOT / SKILL, project / ".claude" / "skills" / SKILL)

    options = ClaudeAgentOptions(
        model=HAIKU,
        cwd=project,
        setting_sources=["project"],
        tools=["Read", "Glob", "Grep", "Skill"],
        allowed_tools=["Read", "Glob", "Grep", "Skill", "mcp__arr__ping"],
        permission_mode="dontAsk",
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=5,
    )
    prompt = (
        "Rufe das MCP-Tool ping auf. Ermittle danach mit dem passenden Skill das "
        "arr-Probe-Codewort. Nenne beide Ergebnisse."
    )
    async with ClaudeSDKClient(options=options) as client:
        status = await client.get_mcp_status()
        t = await run_turn(client, prompt, script=SCRIPT, label="project", model=HAIKU)

    facts = init_facts(t.get("init"))
    calls = [c["name"] for c in t["tool_calls"]]
    answer = t["text"][-1] if t["text"] else ""
    print("\n=== Auswertung ===")
    print(f"get_mcp_status: {short(status, 600)}")
    print(f"init mcp_servers: {facts.get('mcp_servers')}")
    print(f"init tools (mcp): {[x for x in facts.get('tools', []) if x.startswith('mcp__')]}")
    print(f"init skills enthält {SKILL}: {SKILL in (facts.get('skills') or [])}")
    print(f"tool_calls: {calls}")
    print(f"MCP-Log: {MCP_LOG.read_text().strip() if MCP_LOG.exists() else '-'}")
    print(f"Codewort in Antwort: {'kardioide-17' in answer}, pong in Antwort: {'pong' in answer}")
    print(f"Antwort: {short(answer, 600)}")


if __name__ == "__main__":
    asyncio.run(main())

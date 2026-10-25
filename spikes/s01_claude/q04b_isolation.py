"""Question 4 (addendum): which switches keep ~/.claude and parent folders out. No model turns.

Checks auto memory, parent CLAUDE.md, synced/bundled skills via get_server_info and
get_context_usage. Creates the skill source itself (same helpers as q04_skills.py).

Run: uv run --group spikes python spikes/s01_claude/q04b_isolation.py
"""

import asyncio
import os
import subprocess

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from common import AGENT_ENV, HAIKU, TMP, clean_env, short, workdir
from q04_skills import make_plugin, make_skill_source

SKILL = "arr-probe"
SKILLS_ROOT = TMP / "skill_source" / "skills"
PLUGIN = TMP / "skill_source" / "arr-plugin"

HARDENED_ENV = {
    **AGENT_ENV,
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
}


def opts(cwd, env, **kw) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=HAIKU,
        cwd=cwd,
        tools=["Read", "Glob", "Grep", "Bash", "Skill"],
        permission_mode="dontAsk",
        strict_mcp_config=True,
        env=env,
        **kw,
    )


async def probe(name: str, options: ClaudeAgentOptions) -> None:
    async with ClaudeSDKClient(options=options) as client:
        info = await client.get_server_info() or {}
        ctx = await client.get_context_usage()
    model_skills = [
        c.get("name") for c in info.get("commands", []) if c.get("name") and c.get("description")
    ]
    memory = [short(m.get("path"), 120) for m in ctx.get("memoryFiles", [])]
    cats = {c.get("name"): c.get("tokens") for c in ctx.get("categories", [])}
    print(f"\n[{name}]")
    print(f"   commands ({len(model_skills)}): {model_skills}")
    print(f"   memoryFiles: {memory}")
    print(f"   categories: {cats}  totalTokens: {ctx.get('totalTokens')}")


def skill_link(cwd) -> None:
    (cwd / ".claude" / "skills").mkdir(parents=True)
    os.symlink(SKILLS_ROOT / SKILL, cwd / ".claude" / "skills" / SKILL)


async def main() -> None:
    clean_env()
    make_plugin(make_skill_source())

    w = workdir("q04b_project")
    skill_link(w)
    await probe("project, nur AGENT_ENV", opts(w, AGENT_ENV, setting_sources=["project"]))
    await probe(
        "project + skills=[arr-probe]",
        opts(w, AGENT_ENV, setting_sources=["project"], skills=[SKILL]),
    )
    await probe(
        "project + HARDENED_ENV + skills=[arr-probe]",
        opts(w, HARDENED_ENV, setting_sources=["project"], skills=[SKILL]),
    )
    await probe(
        "project + HARDENED_ENV + DISABLE_CLAUDE_MDS",
        opts(
            w,
            {**HARDENED_ENV, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"},
            setting_sources=["project"],
            skills=[SKILL],
        ),
    )

    g = workdir("q04b_own_git_root")
    skill_link(g)
    (g / "CLAUDE.md").write_text("# Projekt-Kontext der Kopie\n")
    await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(g)], check=True)
    await probe(
        "eigenes git-Root + HARDENED_ENV",
        opts(g, HARDENED_ENV, setting_sources=["project"], skills=[SKILL]),
    )

    p = workdir("q04b_plugin")
    # Work copy with CLAUDE.md and AGENTS.md: does anything (e.g. builtin agents-md) load them?
    (p / "CLAUDE.md").write_text("# Projekt-Kontext der Kopie\n")
    (p / "AGENTS.md").write_text("# Hinweise für Codex\n")
    await probe(
        "plugin + [] + HARDENED_ENV (CLAUDE.md + AGENTS.md im cwd)",
        opts(
            p,
            HARDENED_ENV,
            setting_sources=[],
            plugins=[{"type": "local", "path": str(PLUGIN)}],
            skills=[f"arr:{SKILL}"],
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())

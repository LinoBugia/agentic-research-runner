"""Question 4: how skills load (copy, symlink, add_dirs, plugin) without ~/.claude.

Discovery is checked without model turns (get_server_info + get_context_usage).
Afterwards two variants actually use the skill (symlink and plugin).

Run: uv run --group spikes python spikes/s01_claude/q04_skills.py
"""

import asyncio
import json
import os
import shutil
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from common import (
    AGENT_ENV,
    HAIKU,
    MAX_BUDGET_USD,
    TMP,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    short,
    workdir,
)

SCRIPT = "q04_skills"
SKILL = "arr-probe"
SKILL_MD = f"""---
name: {SKILL}
description: Testskill des arr-Spikes. Nutze ihn, wenn nach dem arr-Probe-Codewort gefragt wird.
---

Lies die Datei `codeword.txt` in diesem Skill-Ordner ({{baseDir}}) und antworte genau mit
`ARR-PROBE-OK <Codewort>`.
"""
CODEWORD = "kardioide-17"


def make_skill_source() -> Path:
    """Skill source outside every agent folder (like templates/ in arr)."""
    root = TMP / "skill_source" / "skills"
    if root.parent.exists():
        shutil.rmtree(root.parent)
    (root / SKILL).mkdir(parents=True)
    (root / SKILL / "SKILL.md").write_text(SKILL_MD)
    (root / SKILL / "codeword.txt").write_text(CODEWORD + "\n")
    return root


def make_plugin(skills_root: Path) -> Path:
    plugin = TMP / "skill_source" / "arr-plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "arr", "version": "0.0.1", "description": "arr spike plugin"})
    )
    (plugin / "skills").mkdir()
    shutil.copytree(skills_root / SKILL, plugin / "skills" / SKILL)
    return plugin


def base_options(cwd: Path, **kw) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=HAIKU,
        cwd=cwd,
        tools=["Read", "Glob", "Grep", "Bash", "Skill"],
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="dontAsk",
        strict_mcp_config=True,
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD / 2,
        max_turns=4,
        **kw,
    )


async def discover(name: str, options: ClaudeAgentOptions) -> dict:
    async with ClaudeSDKClient(options=options) as client:
        info = await client.get_server_info() or {}
        ctx = await client.get_context_usage()
    commands = [c.get("name") for c in info.get("commands", [])]
    ours = [c for c in commands if c and SKILL in c]
    memory = [short(m.get("path"), 200) for m in ctx.get("memoryFiles", [])]
    cats = {c.get("name"): c.get("tokens") for c in ctx.get("categories", [])}
    print(f"\n[{name}] arr-probe sichtbar: {ours or 'nein'}")
    print(f"   alle Commands/Skills ({len(commands)}): {commands}")
    print(f"   memoryFiles: {memory}")
    print(f"   context categories: {cats}")
    return {"name": name, "ours": ours, "commands": commands, "memory": memory, "categories": cats}


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(6)
    Recorder(SCRIPT)
    skills_root = make_skill_source()
    plugin = make_plugin(skills_root)

    # 1 copy
    w_copy = workdir("q04_copy")
    shutil.copytree(skills_root / SKILL, w_copy / ".claude" / "skills" / SKILL)
    # 2 symlink per skill folder
    w_link = workdir("q04_symlink_skill")
    (w_link / ".claude" / "skills").mkdir(parents=True)
    os.symlink(skills_root / SKILL, w_link / ".claude" / "skills" / SKILL)
    # 3 symlink of the whole .claude/skills folder
    w_root = workdir("q04_symlink_root")
    (w_root / ".claude").mkdir()
    os.symlink(skills_root, w_root / ".claude" / "skills")
    # 4 add_dirs: skills live in a shared run folder, not in the agent folder
    w_add = workdir("q04_add_dirs")
    shared = workdir("q04_shared")
    (shared / ".claude").mkdir()
    os.symlink(skills_root, shared / ".claude" / "skills")
    # 5 plugin, 6 control
    w_plugin = workdir("q04_plugin")
    w_none = workdir("q04_none")

    results = [
        await discover("copy+project", base_options(w_copy, setting_sources=["project"])),
        await discover("symlink_skill+project", base_options(w_link, setting_sources=["project"])),
        await discover("symlink_root+project", base_options(w_root, setting_sources=["project"])),
        await discover(
            "add_dirs+project",
            base_options(w_add, setting_sources=["project"], add_dirs=[shared]),
        ),
        await discover(
            "plugin+no_sources",
            base_options(
                w_plugin, setting_sources=[], plugins=[{"type": "local", "path": str(plugin)}]
            ),
        ),
        await discover("none+no_sources", base_options(w_none, setting_sources=[])),
    ]

    # Use the skill for real: symlink variant (allowlist with only our skill) and plugin variant.
    prompt = "Wie lautet das arr-Probe-Codewort? Nutze den passenden Skill."
    used = {}
    for name, opts, skill_name in [
        (
            "symlink_skill+project",
            base_options(w_link, setting_sources=["project"], skills=[SKILL]),
            SKILL,
        ),
        (
            "plugin+no_sources",
            base_options(
                w_plugin,
                setting_sources=[],
                plugins=[{"type": "local", "path": str(plugin)}],
                skills=[f"arr:{SKILL}"],
            ),
            f"arr:{SKILL}",
        ),
    ]:
        async with ClaudeSDKClient(options=opts) as client:
            t = await run_turn(client, prompt, script=SCRIPT, label=f"use/{name}", model=HAIKU)
        answer = t["text"][-1] if t["text"] else ""
        used[name] = {
            "skill_name": skill_name,
            "tool_calls": t["tool_calls"],
            "codeword_ok": CODEWORD in answer,
            "answer": answer,
        }

    print("\n=== Auswertung ===")
    for r in results:
        print(f"{r['name']:24} sichtbar={bool(r['ours'])} {r['ours']}  memory={r['memory']}")
    for name, u in used.items():
        calls = [(c["name"], short(c["input"], 80)) for c in u["tool_calls"]]
        print(f"use/{name}: codeword_ok={u['codeword_ok']} calls={calls}")
        print(f"   answer: {short(u['answer'], 200)}")


if __name__ == "__main__":
    asyncio.run(main())

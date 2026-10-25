"""Q8: are AGENTS.md and .agents/skills/ (copy and symlink) in the cwd loaded?

Setup:
  q08_agents/     AGENTS.md + .agents/skills/arr-copy-skill (copy)
                  + .agents/skills/arr-link-skill (symlink to a folder outside the cwd)
  q08_claude_md/  only CLAUDE.md, thread config project_doc_fallback_filenames=["CLAUDE.md"]
Evidence: skills/list (no turn), the persisted rollout and the model's answer.
Turns: 2.
"""

import asyncio
import json
from pathlib import Path

from common import (
    MODEL,
    OUT,
    WORK,
    RawTap,
    check_budget,
    dump,
    fresh_dir,
    make_codex,
    record_usage,
    sanitize,
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import SkillsListResponse

SKILL = """---
name: {name}
description: {desc}
---

# {name}

When this skill is used, reply with the word {word}.
"""

PROMPT_A = (
    "Without using any tools: (1) Which codeword do your project instructions define? "
    "(2) List the names of all skills available to you whose name starts with 'arr-'. "
    "(3) Roughly how many skills are available to you in total? Answer in three short lines."
)
PROMPT_B = (
    "Without using any tools: which codeword do your project instructions define? "
    "Answer NONE if you have no project instructions."
)


def setup() -> tuple[Path, Path]:
    a = fresh_dir("q08_agents")
    (a / "AGENTS.md").write_text("# Project\n\nThe project codeword is OKAPI-3.\n", "utf-8")
    copy = a / ".agents" / "skills" / "arr-copy-skill"
    copy.mkdir(parents=True)
    desc = "Checks primality claims (codeword SKILL-COPY-41)."
    (copy / "SKILL.md").write_text(SKILL.format(name="arr-copy-skill", desc=desc, word="COPY"))
    src = fresh_dir("q08_skill_src") / "arr-link-skill"
    src.mkdir()
    desc = "Fermat test helper (codeword SKILL-LINK-73)."
    (src / "SKILL.md").write_text(SKILL.format(name="arr-link-skill", desc=desc, word="LINK"))
    (a / ".agents" / "skills" / "arr-link-skill").symlink_to(src, target_is_directory=True)

    b = fresh_dir("q08_claude_md")
    (b / "CLAUDE.md").write_text("# Project\n\nThe project codeword is WOMBAT-9.\n", "utf-8")
    return a, b


def rollout_mentions(path: str, words: list[str]) -> dict[str, list[str]]:
    """Which rollout record kinds contain which codeword (proof of what was sent)."""
    hits: dict[str, list[str]] = {w: [] for w in words}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        payload = rec.get("payload", {})
        kind = f"{rec.get('type')}/{payload.get('type')}/{payload.get('role', '')}"
        for w in words:
            if w in line and kind not in hits[w]:
                hits[w].append(kind)
    return hits


async def main() -> None:
    check_budget(2)
    a, b = setup()
    tap = RawTap(OUT / "q08_raw.jsonl")
    report: dict = {}
    async with make_codex(tap=tap) as codex:
        skills = await codex._client.request(
            "skills/list",
            {"cwds": [str(a)], "forceReload": True},
            response_model=SkillsListResponse,
        )
        entries = []
        for entry in skills.data:
            for s in entry.skills:
                entries.append({"name": s.name, "scope": s.scope.value, "path": str(s.path)})
        report["skills_list"] = {
            "total": len(entries),
            "by_scope": {
                sc: sum(1 for e in entries if e["scope"] == sc)
                for sc in {e["scope"] for e in entries}
            },
            "arr": [e for e in entries if e["name"].startswith("arr-")],
            "errors": [str(e.errors) for e in skills.data if getattr(e, "errors", None)],
        }

        thread = await codex.thread_start(
            cwd=str(a), model=MODEL, sandbox=Sandbox.read_only, approval_mode=ApprovalMode.deny_all
        )
        result = await thread.run(PROMPT_A)
        record_usage("q08", "AGENTS.md + skills (copy, symlink)", result)
        info = await thread.read()
        report["agents_turn"] = {
            "final": result.final_response,
            "items": [i.root.type for i in result.items],
            "rollout": rollout_mentions(
                info.thread.path, ["OKAPI-3", "SKILL-COPY-41", "SKILL-LINK-73"]
            ),
        }

        thread_b = await codex.thread_start(
            cwd=str(b),
            model=MODEL,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            config={"project_doc_fallback_filenames": ["CLAUDE.md"]},
        )
        result_b = await thread_b.run(PROMPT_B)
        record_usage("q08", "CLAUDE.md via project_doc_fallback_filenames", result_b)
        info_b = await thread_b.read()
        report["claude_md_turn"] = {
            "final": result_b.final_response,
            "rollout": rollout_mentions(info_b.thread.path, ["WOMBAT-9"]),
        }
    tap.close()
    report = sanitize(report)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    dump("q08_context.json", report)
    print("fixture:", save_fixture(tap.path, "turn_agents_md_and_skills"))
    print("symlink:", (a / ".agents/skills/arr-link-skill").is_symlink(), "->", WORK.name)


if __name__ == "__main__":
    asyncio.run(main())

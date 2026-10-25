"""Q6: developer_instructions vs base_instructions for a static role prompt.

Same role text and prompt in two threads; compare behaviour, tool use, input tokens
and where Codex puts the text (rollout file of the thread). 2 turns.
"""

import asyncio
import json
from pathlib import Path

from common import (
    MODEL,
    OUT,
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

ROLE = (
    "You are the agent 'critic' in a mathematical research group. "
    "Start every final answer with 'CRITIC:' and mention the group codeword ZEBRA-7."
)
PROMPT = "Run `echo tool-check` in the shell, then state your role and the codeword in one line."


def rollout_summary(path: str) -> dict:
    """Where does the role text end up in the persisted rollout of the thread?"""
    found = {"base_instructions_field": None, "developer_messages_with_role": 0}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        payload = rec.get("payload", {})
        if rec.get("type") == "session_meta":
            base = payload.get("base_instructions") or payload.get("instructions")
            if isinstance(base, dict):
                base = base.get("text")
            found["base_instructions_field"] = (
                "role text" if base and "ZEBRA-7" in base else f"default ({len(base or '')} chars)"
            )
        if payload.get("type") == "message" and payload.get("role") == "developer":
            text = json.dumps(payload.get("content"))
            if "ZEBRA-7" in text:
                found["developer_messages_with_role"] += 1
    return found


async def run_variant(codex, variant: str) -> dict:
    work = fresh_dir(f"q06_{variant}")
    kwargs = (
        {"developer_instructions": ROLE} if variant == "developer" else {"base_instructions": ROLE}
    )
    thread = await codex.thread_start(
        cwd=str(work),
        model=MODEL,
        sandbox=Sandbox.workspace_write,
        approval_mode=ApprovalMode.deny_all,
        **kwargs,
    )
    result = await thread.run(PROMPT)
    record_usage("q06", f"{variant}_instructions", result)
    info = await thread.read()
    return {
        "variant": variant,
        "final": result.final_response,
        "items": [i.root.type for i in result.items],
        "commands": [i.root.command for i in result.items if i.root.type == "commandExecution"],
        "input_tokens": result.usage.last.input_tokens,
        "rollout": rollout_summary(info.thread.path) if info.thread.path else None,
    }


async def main() -> None:
    check_budget(2)
    tap = RawTap(OUT / "q06_raw.jsonl")
    async with make_codex(tap=tap) as codex:
        rows = [await run_variant(codex, v) for v in ("developer", "base")]
    tap.close()
    rows = sanitize(rows)
    for r in rows:
        print(json.dumps(r, indent=1, ensure_ascii=False))
    dump("q06_instructions.json", rows)
    print("fixture:", save_fixture(tap.path, "turn_developer_vs_base_instructions"))


if __name__ == "__main__":
    asyncio.run(main())

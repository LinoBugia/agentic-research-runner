"""Q4: do reasoning summaries arrive with summary="detailed"? What do they look like?

A short puzzle without tools that needs some thinking. 1 turn per call.
Usage: q04_reasoning.py [model] [effort]
"""

import asyncio
import json
import os
import sys

from common import (
    MODEL,
    OUT,
    RawTap,
    check_budget,
    fresh_dir,
    make_codex,
    method_counts,
    record_usage,
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox

PROMPTS = {
    "easy": (
        "Without using any tools: find the smallest positive integer n such that n, n+1 and "
        "n+2 each have exactly four positive divisors. Answer with n and a one-line check."
    ),
    "hard": (
        "Without using any tools: how many integers n with 1 <= n <= 10000 are such that "
        "n^2 + 1 is divisible by 65? Give the count and a two-line justification."
    ),
}


async def main(model: str, effort: str, level: str) -> None:
    prompt = PROMPTS[level]
    check_budget(1)
    work = fresh_dir(f"q04_{model}_{effort}_{level}")
    tap = RawTap(OUT / f"q04_{model}_{effort}_{level}_raw.jsonl")
    # S2_SUMMARY_CFG=1 additionally sets the summary via config keys, not only per turn.
    extra = (
        ('model_reasoning_summary="detailed"', "hide_agent_reasoning=false")
        if os.environ.get("S2_SUMMARY_CFG")
        else ()
    )
    async with make_codex(tap=tap, extra_overrides=extra) as codex:
        thread = await codex.thread_start(
            cwd=str(work),
            model=model,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
        )
        result = await thread.run(prompt, effort=effort, summary="detailed")
    tap.close()
    record_usage("q04", f"{level} puzzle, effort={effort}", result, model=model)

    print("final:", result.final_response)
    print("incoming methods:", json.dumps(method_counts(tap.path), indent=1))
    print("items:", [i.root.type for i in result.items])
    for item in result.items:
        if item.root.type == "reasoning":
            print("\nreasoning item:", item.root.model_dump_json(by_alias=True, indent=1)[:3000])
    print("\nusage.last:", result.usage.last.model_dump_json(by_alias=True))
    if any(i.root.type == "reasoning" for i in result.items):
        print("fixture:", save_fixture(tap.path, f"turn_reasoning_{effort}_{level}"))


if __name__ == "__main__":
    args = sys.argv[1:] + [None] * 3
    asyncio.run(main(args[0] or MODEL, args[1] or "medium", args[2] or "easy"))

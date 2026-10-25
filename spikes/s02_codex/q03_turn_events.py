"""Q3-Q5: a turn that writes a file and runs Python; records every notification.

Answers: which notification and item types arrive (Q3), whether reasoning summaries
arrive with summary="detailed" (Q4), and what ThreadTokenUsage contains (Q5).

Variants (1 turn each):
  basic      effort=low, free choice of tools
  reasoning  effort=medium, asks for a small proof and an explicit patch edit
"""

import asyncio
import json
import sys

from common import (
    EFFORT,
    MODEL,
    OUT,
    RawTap,
    check_budget,
    fresh_dir,
    item_types,
    make_codex,
    method_counts,
    record_usage,
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox

VARIANTS = {
    "basic": (
        EFFORT,
        "Create a file primes.py that prints the primes below 30 as a Python list. "
        "Then run it with python3 and tell me the output in one sentence.",
        "turn_write_and_run",
    ),
    "reasoning": (
        "medium",
        "First decide briefly whether 91 is prime and why. Then use your file editing "
        "(apply_patch) tool, not the shell, to create check91.py that prints the "
        "factorization of 91, run it with python3, and answer in one sentence.",
        "turn_reasoning_patch",
    ),
}


async def main(variant: str) -> None:
    effort, prompt, fixture = VARIANTS[variant]
    check_budget(1)
    work = fresh_dir(f"q03_{variant}")
    tap = RawTap(OUT / f"q03_{variant}_raw.jsonl")
    async with make_codex(tap=tap) as codex:
        thread = await codex.thread_start(
            cwd=str(work),
            model=MODEL,
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
        )
        result = await thread.run(prompt, effort=effort, summary="detailed")
    tap.close()
    record_usage("q03", f"{variant}: effort={effort}, summary=detailed", result)

    print("thread:", thread.id, "| status:", result.status.value)
    print("files:", sorted(p.name for p in work.iterdir()))
    print("final:", result.final_response)
    print("\nincoming methods:", json.dumps(method_counts(tap.path), indent=1))
    print("item/completed types:", item_types(tap.path))
    print("TurnResult.items types:", [i.root.type for i in result.items])
    for item in result.items:
        if item.root.type == "reasoning":
            print("\nreasoning item:", item.root.model_dump_json(by_alias=True)[:1200])
    print("\nusage:", result.usage.model_dump_json(by_alias=True, indent=1))
    print("\nfixture:", save_fixture(tap.path, fixture))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "basic"))

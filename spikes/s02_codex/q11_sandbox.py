"""Q11: Sandbox.workspace_write - writes outside, network, extra writable roots.

approval_mode=deny_all, so nothing can be escalated; the sandbox alone decides.
All targets live under tmp/work/ (q11/ is the cwd, its siblings count as "outside").
Variants (1 turn each):
  default  plain workspace_write
  config   thread config sandbox_workspace_write = {writable_roots: [extra], network_access: true}
"""

import asyncio
import json
import sys

from common import (
    MODEL,
    OUT,
    WORK,
    RawTap,
    check_budget,
    fresh_dir,
    make_codex,
    record_usage,
    sanitize,
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox


def prompt(extra: str, outside: str, secret: str) -> str:
    commands = [
        "echo inside > inside.txt",
        f"echo outside > {outside}",
        f"echo extra > {extra}/extra.txt",
        f"cat {secret}",
        "curl -sS -m 5 -o /dev/null -w '%{http_code}' https://example.com",
        "python3 -c \"import urllib.request as u; print(u.urlopen('https://example.com', "
        'timeout=5).status)"',
    ]
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(commands, 1))
    return (
        "Run each of these shell commands exactly once, separately, in this order. "
        "Do not retry, do not work around failures, do not ask for permissions. "
        "Then report for each number: exit code and the first line of output or error.\n" + listing
    )


async def run_variant(variant: str) -> dict:
    work = fresh_dir("q11")
    extra = fresh_dir("q11_extra")
    outside = WORK / "q11_outside.txt"
    outside.unlink(missing_ok=True)
    secret = WORK / "q11_secret.txt"
    secret.write_text("readable-outside-cwd\n")
    config = None
    if variant == "config":
        config = {
            "sandbox_workspace_write": {"writable_roots": [str(extra)], "network_access": True}
        }

    tap = RawTap(OUT / f"q11_{variant}_raw.jsonl")
    async with make_codex(tap=tap) as codex:
        thread = await codex.thread_start(
            cwd=str(work),
            model=MODEL,
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
            config=config,
        )
        result = await thread.run(prompt(str(extra), str(outside), str(secret)))
        record_usage("q11", f"sandbox {variant}", result)
    tap.close()

    settings = None
    for line in tap.path.read_text(encoding="utf-8").splitlines():
        msg = json.loads(line)["msg"]
        if msg.get("method") == "thread/settings/updated":
            settings = msg["params"]["threadSettings"]["sandboxPolicy"]
    commands = [
        {
            "command": i.root.command,
            "exit": i.root.exit_code,
            "status": i.root.status.value,
            "out": (i.root.aggregated_output or "")[:200],
        }
        for i in result.items
        if i.root.type == "commandExecution"
    ]
    print(f"fixture {variant}:", save_fixture(tap.path, f"turn_sandbox_{variant}"))
    return sanitize(
        {
            "variant": variant,
            "sandbox_policy": settings,
            "commands": commands,
            "files": {
                "inside.txt": (work / "inside.txt").exists(),
                "outside": outside.exists(),
                "extra/extra.txt": (extra / "extra.txt").exists(),
            },
            "final": result.final_response,
        }
    )


async def main(variants: list[str]) -> None:
    check_budget(len(variants))
    for v in variants:
        print(json.dumps(await run_variant(v), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["default", "config"]))

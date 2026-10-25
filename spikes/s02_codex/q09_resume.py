"""Q9: does thread_resume work after a restart of the client?

Two separate processes (= two app-server processes):
  q09_resume.py start    new thread, 1 turn, store thread id, exit
  q09_resume.py resume   new client, thread_resume(id), 1 turn asking for the fact
Turns: 2.
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
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox

STATE = WORK / "q09_state.json"


async def start() -> None:
    check_budget(1)
    work = fresh_dir("q09")
    tap = RawTap(OUT / "q09_start_raw.jsonl")
    async with make_codex(tap=tap) as codex:
        thread = await codex.thread_start(
            cwd=str(work),
            model=MODEL,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
        )
        result = await thread.run("Remember the number 4711 for later. Reply only with OK.")
        record_usage("q09", "start: remember 4711", result)
    tap.close()
    STATE.write_text(json.dumps({"thread_id": thread.id, "cwd": str(work)}))
    print("thread:", thread.id, "| final:", result.final_response)


async def resume() -> None:
    check_budget(1)
    state = json.loads(STATE.read_text())
    tap = RawTap(OUT / "q09_resume_raw.jsonl")
    async with make_codex(tap=tap) as codex:
        thread = await codex.thread_resume(
            state["thread_id"],
            cwd=state["cwd"],
            model=MODEL,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
        )
        result = await thread.run("Which number did I ask you to remember? Reply with the number.")
        record_usage("q09", "resume: recall number", result)
        listed = await thread.read()
    tap.close()
    print("resumed thread:", thread.id, "| same id:", thread.id == state["thread_id"])
    print("final:", result.final_response)
    print("cached input tokens on first resumed call:", result.usage.last.cached_input_tokens)
    print("thread status after read:", listed.thread.status.root.type)
    print("fixture:", save_fixture(tap.path, "thread_resume_after_restart"))


if __name__ == "__main__":
    asyncio.run({"start": start, "resume": resume}[sys.argv[1]]())

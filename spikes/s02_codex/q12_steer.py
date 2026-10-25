"""Q12: does a hint sent mid-turn reach the running agent and is it followed?

The turn runs two slow shell commands. As soon as the first command starts, the
hint is delivered. Variants (1 turn each):
  steer     AsyncTurnHandle.steer(hint)                (user input)
  external  thread.turn(ExternalMessage(...)) joins    (tool-level authority)
Afterwards steer() on the finished turn shows the error for "no active turn".
"""

import asyncio
import json
import sys

from common import (
    MODEL,
    OUT,
    RawTap,
    check_budget,
    fresh_dir,
    make_codex,
    record_usage,
    sanitize,
    save_fixture,
)
from openai_codex import ApprovalMode, ExternalMessage, Sandbox

PROMPT = (
    "Run `sleep 6 && echo first` in the shell, then run `sleep 6 && echo second`. "
    "Then answer with the word DONE."
)
HINT = (
    "📨 1 Frage an dich in #main von critic: „Was ist 17·23?“ "
    "Beantworte sie kurz in deiner Abschlussantwort."
)


async def run_variant(variant: str) -> dict:
    work = fresh_dir(f"q12_{variant}")
    tap = RawTap(OUT / f"q12_{variant}_raw.jsonl")
    report: dict = {"variant": variant}
    async with make_codex(tap=tap) as codex:
        thread = await codex.thread_start(
            cwd=str(work),
            model=MODEL,
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
        )
        handle = await thread.turn(PROMPT)
        joined = None
        items = []
        usage = None
        status = None
        async for note in handle.stream():
            method = note.method
            payload = note.payload
            first_command = (
                method == "item/started"
                and payload.item.root.type == "commandExecution"
                and "first" in (payload.item.root.command or "")
            )
            if first_command and "sent" not in report:
                report["sent"] = True
                if variant == "steer":
                    resp = await handle.steer(HINT)
                    report["steer_response"] = resp.model_dump(by_alias=True)
                else:
                    message = ExternalMessage(tool_name="room", namespace="arr", content=HINT)
                    joined = await thread.turn(message)
                    report["joined_turn_id_same"] = joined.id == handle.id
            if method == "item/completed":
                items.append(payload.item.root)
            if method == "thread/tokenUsage/updated":
                usage = payload.token_usage
            if method == "turn/completed":
                status = payload.turn.status.value

        class _R:
            pass

        _R.status, _R.duration_ms, _R.usage = status, None, usage
        record_usage("q12", f"mid-turn hint via {variant}", _R)
        try:
            await handle.steer("late hint")
            report["steer_after_end"] = "accepted"
        except Exception as exc:  # noqa: BLE001 - we want the exact error type
            report["steer_after_end"] = f"{type(exc).__name__}: {exc}"
    tap.close()

    report["items"] = [
        {"type": i.type, **({"text": i.text} if i.type == "agentMessage" else {})}
        | ({"command": i.command} if i.type == "commandExecution" else {})
        | ({"content": str(getattr(i, "content", ""))[:200]} if i.type == "userMessage" else {})
        for i in items
    ]
    report["hint_answered"] = any("391" in (getattr(i, "text", "") or "") for i in items)
    report["turn_status"] = status
    print(f"fixture {variant}:", save_fixture(tap.path, f"turn_hint_{variant}"))
    return sanitize(report)


async def main(variants: list[str]) -> None:
    check_budget(len(variants))
    for v in variants:
        print(json.dumps(await run_variant(v), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["steer", "external"]))

"""Q10: how approval requests reach the SDK client and how to answer them.

Variants (1 turn each):
  user  approvalPolicy=on-request + approvalsReviewer=user (low-level thread_start; the
        public ApprovalMode has no such value) and our own approval handler:
        decline the write outside the workspace (+ reason via steer), accept network.
  auto  public default ApprovalMode.auto_review: does anything reach the client?
"""

import asyncio
import json
import sys
import time

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
from openai_codex import ApprovalMode, AsyncThread
from openai_codex.generated.v2_all import (
    ApprovalsReviewer,
    AskForApproval,
    AskForApprovalValue,
    SandboxMode,
    ThreadStartParams,
)

REASON = (
    "Abgelehnt: Schreiben außerhalb des Runs ist nicht erlaubt. Lege die Datei als "
    "outside.txt in deinem Arbeitsordner an und arbeite weiter."
)


def prompt(outside: str) -> str:
    return (
        "Two steps, in this order. Step 1: write the text 'hello' to the file "
        f"{outside} with a shell command; it is outside your workspace, so request "
        "escalated permissions with a short justification. Step 2: run "
        "`curl -sS -m 5 -o /dev/null -w '%{http_code}' https://example.com`; it needs "
        "network, so request escalated permissions too. Follow any feedback you get. "
        "Finally report what happened in each step."
    )


async def run_variant(variant: str) -> dict:
    work = fresh_dir("q10")
    outside = WORK / "q10_outside.txt"
    outside.unlink(missing_ok=True)
    loop = asyncio.get_running_loop()
    holder: dict = {}
    requests: list[dict] = []

    def handler(method: str, params: dict | None) -> dict:
        """Runs on the SDK reader thread; nothing else is read while it blocks."""
        t0 = time.monotonic()
        params = params or {}
        command = params.get("command") or ""
        if method == "item/commandExecution/requestApproval" and "q10_outside" in command:
            decision = "decline"
            # The decision has no reason field: deliver the reason as steer input.
            asyncio.run_coroutine_threadsafe(holder["handle"].steer(REASON), loop)
        elif method == "item/commandExecution/requestApproval":
            time.sleep(2)  # stands in for waiting on the approval inbox
            decision = "accept"
        elif method == "item/fileChange/requestApproval":
            decision = "decline"
        else:
            decision = None
        requests.append(
            {
                "method": method,
                "params": params,
                "decision": decision,
                "handler_ms": round((time.monotonic() - t0) * 1000),
            }
        )
        return {"decision": decision} if decision else {}

    tap = RawTap(OUT / f"q10_{variant}_raw.jsonl")
    codex = make_codex(tap=tap, approval_handler=handler if variant == "user" else None)
    async with codex:
        if variant == "user":
            started = await codex._client.thread_start(
                ThreadStartParams(
                    cwd=str(work),
                    model=MODEL,
                    sandbox=SandboxMode.workspace_write,
                    approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
                    approvals_reviewer=ApprovalsReviewer.user,
                )
            )
            thread = AsyncThread(codex, started.thread.id)
        else:
            from openai_codex import Sandbox

            thread = await codex.thread_start(
                cwd=str(work),
                model=MODEL,
                sandbox=Sandbox.workspace_write,
                approval_mode=ApprovalMode.auto_review,
            )
        handle = await thread.turn(prompt(str(outside)))
        holder["handle"] = handle
        result = await handle.run()
        record_usage("q10", f"approvals {variant}", result)
    tap.close()

    review_events = []
    for line in tap.path.read_text(encoding="utf-8").splitlines():
        msg = json.loads(line)["msg"]
        method = msg.get("method", "")
        if "review" in method.lower() or "approval" in method.lower() or "guardian" in method:
            review_events.append({"method": method, "params": msg.get("params")})
        if method == "serverRequest/resolved":
            review_events.append({"method": method, "params": msg.get("params")})
    print(f"fixture {variant}:", save_fixture(tap.path, f"turn_approvals_{variant}"))
    report = sanitize(
        {
            "variant": variant,
            "requests_seen_by_client": requests,
            "review_or_approval_events": review_events,
            "items": [
                {
                    "type": i.root.type,
                    **(
                        {
                            "command": i.root.command,
                            "exit": i.root.exit_code,
                            "status": i.root.status.value,
                        }
                        if i.root.type == "commandExecution"
                        else {}
                    ),
                }
                for i in result.items
            ],
            "outside_written": outside.exists(),
            "inside_written": (work / "outside.txt").exists(),
            "final": result.final_response,
        }
    )
    dump(f"q10_{variant}.json", report)
    return report


async def main(variants: list[str]) -> None:
    check_budget(len(variants))
    for v in variants:
        print(json.dumps(await run_variant(v), indent=1, ensure_ascii=False)[:6000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["user", "auto"]))

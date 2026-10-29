"""Q13 rest, live (W2-02 §3 step 5, A8): parallel logins and parallel agents on one login.

1. Two agents on two logged-in login folders (`chatgpt-w2-02a`, `-b`), one turn each, in
   parallel.
2. Token refresh: two app-servers on the same login folder ask for `account/read` with
   `refreshToken: true` at the same time (no model turn); afterwards both accounts are read
   again without refresh.
3. Two agents on the same login folder, one turn each, in parallel; then one more turn for
   the first agent (after the refresh of step 2 and the parallel turns).

5 live turns. Result in tmp/out/q13.json.

    uv run python spikes/w2_02_codex/q13_live.py
"""

import asyncio
import json
import time
from typing import Any

from common import OUT, auth_profile, build_lab, check_budget, record_turn, remove_cleanup
from openai_codex import AsyncCodex, CodexConfig

from arr.backends.base import EventKind
from arr.backends.codex import CodexBackend
from arr.backends.codex_config import SECRET_ENV, codex_home_for
from arr.backends.codex_login import _state
from arr.permissions.inbox import ApprovalInbox

A, B = "chatgpt-w2-02a", "chatgpt-w2-02b"


async def one_turn(backend: CodexBackend, label: str) -> dict[str, Any]:
    t0 = time.monotonic()
    end: dict[str, Any] = {}
    text = None
    errors = []
    async for event in backend.run_turn("Antworte nur mit dem Wort OK."):
        if event.kind is EventKind.TEXT:
            text = event.data["text"]
        if event.kind is EventKind.ERROR:
            errors.append(event.data["message"][:200])
        if event.kind is EventKind.TURN_END:
            end = event.data
    record_turn("q13_live.py", label, end)
    return {
        "label": label,
        "status": end.get("status"),
        "text": text,
        "errors": errors,
        "seconds": round(time.monotonic() - t0, 1),
        "start": round(t0, 2),
    }


async def refresh_twice(profile: str) -> dict[str, Any]:
    home = codex_home_for(auth_profile(profile))
    env = {"CODEX_HOME": str(home), **{n: "" for n in SECRET_ENV}}
    config = CodexConfig(env=env, config_overrides=("notify=[]",))
    first, second = AsyncCodex(config), AsyncCodex(config)
    async with first, second:
        refreshed = await asyncio.gather(
            first.account(refresh_token=True),
            second.account(refresh_token=True),
            return_exceptions=True,
        )
        after = await asyncio.gather(first.account(), second.account(), return_exceptions=True)
    describe = [
        type(r).__name__ if isinstance(r, BaseException) else _state(r).describe()
        for r in (*refreshed, *after)
    ]
    return {"refresh": describe[:2], "after": describe[2:]}


async def main() -> None:
    check_budget(5)
    lab = build_lab(agents=(("gpt-a", A), ("gpt-b", B), ("gpt-a2", A)))
    report: dict[str, Any] = {}
    try:
        inbox = ApprovalInbox(lab.engine, timeout=60)

        def backend(agent_id: str, profile: str) -> CodexBackend:
            agent = lab.agents[agent_id]
            return CodexBackend(
                agent, run_id=lab.run.run_id, inbox=inbox, auth=auth_profile(profile)
            )

        a, b, a2 = backend("gpt-a", A), backend("gpt-b", B), backend("gpt-a2", A)
        for item in (a, b, a2):
            await item.start()
        report["two_profiles"] = await asyncio.gather(one_turn(a, "a"), one_turn(b, "b"))
        report["refresh_same_profile"] = await refresh_twice(A)
        report["same_profile"] = await asyncio.gather(one_turn(a, "a#2"), one_turn(a2, "a2"))
        report["after"] = await one_turn(a, "a#3")
        report["accounts"] = {p: (await refresh_state(p)) for p in (A, B)}
        for item in (a, b, a2):
            await item.close()
    finally:
        remove_cleanup(lab)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "q13.json").write_text(json.dumps(report, indent=2, default=str))
        print(json.dumps(report, indent=2, default=str))


async def refresh_state(profile: str) -> str:
    from arr.backends.codex_login import codex_account

    return (await codex_account(auth_profile(profile))).describe()


if __name__ == "__main__":
    asyncio.run(main())

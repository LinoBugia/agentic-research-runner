"""Q13: two clients in parallel with separate login folders (CODEX_HOME).

Phases:
  probe     two clients with empty temp CODEX_HOMEs at the same time: which home does
            each app-server use, what is the account state, what leaks from ~/.codex?
  login     device-code login into temp home A. Prints only verification URL and code.
  parallel  client A (temp home, new login) and client B (default ~/.codex) run one
            turn each concurrently and report their account kind (2 turns).
  cleanup   logout in home A and delete the temp home (it holds credentials).
Login data is never read: only file names in the temp home are listed.
"""

import asyncio
import json
import shutil
import sys

from common import (
    MODEL,
    OUT,
    TMP,
    RawTap,
    check_budget,
    fresh_dir,
    make_codex,
    record_usage,
    sanitize,
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import SkillsListResponse

HOMES = {"A": TMP / "codex_home_a", "B": TMP / "codex_home_b"}


def env_for(name: str) -> dict[str, str]:
    HOMES[name].mkdir(parents=True, exist_ok=True)
    return {"CODEX_HOME": str(HOMES[name])}


def account_kind(resp) -> dict:
    acc = resp.account.root if resp.account is not None else None
    return {
        "type": getattr(acc, "type", None),
        "plan": str(getattr(acc, "plan_type", None)),
        "requires_openai_auth": resp.requires_openai_auth,
    }


async def describe(name: str, env: dict[str, str] | None) -> dict:
    async with make_codex(env=env) as codex:
        init_raw = codex.metadata.model_dump()
        account = await codex.account()
        skills = await codex._client.request(
            "skills/list", {"forceReload": True}, response_model=SkillsListResponse
        )
        scopes: dict[str, int] = {}
        for entry in skills.data:
            for s in entry.skills:
                scopes[s.scope.value] = scopes.get(s.scope.value, 0) + 1
        return {
            "client": name,
            "env": env,
            "user_agent": init_raw.get("userAgent"),
            "account": account_kind(account),
            "skills_by_scope": scopes,
        }


async def probe() -> None:
    for home in HOMES.values():
        shutil.rmtree(home, ignore_errors=True)
    results = await asyncio.gather(describe("A", env_for("A")), describe("B", env_for("B")))
    for r in sanitize(list(results)):
        print(json.dumps(r, indent=1))
    for name, home in HOMES.items():
        print(name, "files after start:", sorted(p.name for p in home.iterdir()))


async def login() -> None:
    async with make_codex(env=env_for("A")) as codex:
        handle = await codex.login_chatgpt_device_code()
        print(f"LOGIN-URL: {handle.verification_url}", flush=True)
        print(f"DEVICE-CODE: {handle.user_code}", flush=True)
        done = await handle.wait()
        print("login success:", done.success, "| error:", done.error, flush=True)
        print("account:", account_kind(await codex.account()))
    print("files in home A:", sorted(p.name for p in HOMES["A"].iterdir()))


async def one_turn(name: str, env: dict[str, str] | None) -> dict:
    work = fresh_dir(f"q13_{name}")
    tap = RawTap(OUT / f"q13_{name}_raw.jsonl")
    async with make_codex(tap=tap, env=env) as codex:
        account = account_kind(await codex.account())
        thread = await codex.thread_start(
            cwd=str(work),
            model=MODEL,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
        )
        result = await thread.run(f"Reply with exactly: client {name} ok")
        record_usage("q13", f"parallel client {name}", result)
        info = await thread.read()
    tap.close()
    return {
        "client": name,
        "account": account,
        "codex_home": codex.metadata.model_dump().get("codexHome"),
        "rollout_under": str(info.thread.path).split("/sessions/")[0] if info.thread.path else None,
        "final": result.final_response,
        "started": result.started_at,
        "completed": result.completed_at,
    }


async def parallel() -> None:
    check_budget(2)
    results = await asyncio.gather(one_turn("A", env_for("A")), one_turn("B", None))
    for r in sanitize(list(results)):
        print(json.dumps(r, indent=1))
    print("fixture:", save_fixture(OUT / "q13_A_raw.jsonl", "codex_home_separate_login"))


async def cleanup() -> None:
    async with make_codex(env=env_for("A")) as codex:
        await codex.logout()
        print("after logout:", account_kind(await codex.account()))
    for home in HOMES.values():
        shutil.rmtree(home, ignore_errors=True)
    print("temp homes removed:", [not h.exists() for h in HOMES.values()])


if __name__ == "__main__":
    phases = {"probe": probe, "login": login, "parallel": parallel, "cleanup": cleanup}
    asyncio.run(phases[sys.argv[1]]())

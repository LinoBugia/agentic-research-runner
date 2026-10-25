"""Q2: models the ChatGPT plan offers through Codex (model/list), plus account type."""

import asyncio

from common import dump, make_codex


async def main() -> None:
    async with make_codex() as codex:
        meta = codex.metadata
        print("server:", meta.serverInfo, "| platform:", meta.platformFamily, meta.platformOs)
        account = await codex.account()
        acc = account.account.root if account.account is not None else None
        # Only the account kind and plan, never ids or e-mail.
        print(
            "account type:", getattr(acc, "type", None), "| plan:", getattr(acc, "plan_type", None)
        )
        print("requires_openai_auth:", account.requires_openai_auth)

        rows = []
        for include_hidden in (False, True):
            listing = await codex.models(include_hidden=include_hidden)
            for m in listing.data:
                rows.append(
                    {
                        "listing": "hidden" if include_hidden else "visible",
                        "id": m.id,
                        "model": m.model,
                        "display_name": m.display_name,
                        "hidden": m.hidden,
                        "is_default": m.is_default,
                        "default_effort": m.default_reasoning_effort.value,
                        "efforts": [
                            e.reasoning_effort.value for e in m.supported_reasoning_efforts or []
                        ],
                        "default_service_tier": m.default_service_tier,
                        "description": m.description,
                    }
                )
        seen = set()
        for r in rows:
            if (r["id"], r["listing"]) in seen:
                continue
            seen.add((r["id"], r["listing"]))
            print(
                f"{r['listing']:7} {r['id']:28} hidden={r['hidden']!s:5} "
                f"default={r['is_default']!s:5} effort={r['default_effort']:6} "
                f"{r['efforts']} tier={r['default_service_tier']}"
            )
        dump("q02_models.json", rows)


if __name__ == "__main__":
    asyncio.run(main())

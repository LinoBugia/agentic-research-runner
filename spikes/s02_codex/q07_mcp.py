"""Q7: stdio MCP server per thread via thread_start(config=...) or via .codex/config.toml.

Variants (1 turn each):
  thread     config={"mcp_servers": {"room": ...}} on thread_start
  project    .codex/config.toml in cwd, project not trusted
  trusted    same file, trust given only at runtime (-c projects."<cwd>".trust_level)
Usage: q07_mcp.py [variant ...]   (default: all three)
"""

import asyncio
import json
import sys
from pathlib import Path

from common import (
    MODEL,
    OUT,
    SPIKE_DIR,
    RawTap,
    check_budget,
    fresh_dir,
    make_codex,
    method_counts,
    record_usage,
    sanitize,
    save_fixture,
)
from openai_codex import ApprovalMode, Sandbox

PROMPT = (
    "Call the MCP tool whoami of the server 'room' and report its result verbatim. "
    "Then post 'hello from codex' to channel main with the room tool post. "
    "If there is no room server, answer exactly NO-ROOM."
)


def server_spec(agent_id: str, room_log: str) -> dict:
    return {
        "command": sys.executable,
        "args": [str(SPIKE_DIR / "room_mcp.py")],
        "env": {"AGENT_ID": agent_id, "ROOM_LOG": room_log},
        # Without this, approval_policy=never fails every MCP call (first run of q07).
        "default_tools_approval_mode": "approve",
    }


def toml_for(spec: dict) -> str:
    env = ", ".join(f'{k} = "{v}"' for k, v in spec["env"].items())
    args = ", ".join(f'"{a}"' for a in spec["args"])
    return (
        f'[mcp_servers.room]\ncommand = "{spec["command"]}"\nargs = [{args}]\n'
        f'env = {{ {env} }}\ndefault_tools_approval_mode = "approve"\n'
    )


def read_posts(log: Path) -> list[dict]:
    """Messages the room server received (proof that AGENT_ID reached the server)."""
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


async def run_variant(variant: str) -> dict:
    work = fresh_dir(f"q07_{variant}")
    room_log = str(work.parent / f"q07_{variant}_room.jsonl")
    spec = server_spec(f"codex-{variant}", room_log)
    thread_config = None
    overrides: tuple[str, ...] = ()
    if variant == "thread":
        thread_config = {"mcp_servers": {"room": spec}}
    else:
        (work / ".codex").mkdir()
        (work / ".codex" / "config.toml").write_text(toml_for(spec), encoding="utf-8")
        if variant == "trusted":
            overrides = (f'projects."{work}".trust_level="trusted"',)

    tap = RawTap(OUT / f"q07_{variant}_raw.jsonl")
    async with make_codex(tap=tap, extra_overrides=overrides) as codex:
        thread = await codex.thread_start(
            cwd=str(work),
            model=MODEL,
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.deny_all,
            config=thread_config,
        )
        result = await thread.run(PROMPT)
    tap.close()
    record_usage("q07", f"mcp via {variant}", result)

    mcp_items = [
        i.root.model_dump(by_alias=True, mode="json")
        for i in result.items
        if i.root.type == "mcpToolCall"
    ]
    startup = []
    warnings = []
    for line in tap.path.read_text(encoding="utf-8").splitlines():
        msg = json.loads(line)["msg"]
        if msg.get("method") == "mcpServer/startupStatus/updated":
            startup.append(msg["params"])
        if msg.get("method") in ("configWarning", "warning", "error"):
            warnings.append(msg)
    posts = read_posts(Path(room_log))
    print(f"fixture {variant}:", save_fixture(tap.path, f"turn_mcp_{variant}"))
    return sanitize(
        {
            "variant": variant,
            "final": result.final_response,
            "items": [i.root.type for i in result.items],
            "mcp_tool_calls": mcp_items,
            "startup_status": startup,
            "warnings": warnings,
            "room_posts": posts,
            "methods": method_counts(tap.path),
        }
    )


async def main(variants: list[str]) -> None:
    check_budget(len(variants))
    for v in variants:
        print(json.dumps(await run_variant(v), indent=1, ensure_ascii=False)[:4000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["thread", "project", "trusted"]))

"""Question 5 (addendum, review M1): which switch keeps the claude.ai connectors out?
No model turns: connect, poll get_mcp_status(), disconnect.

Variants: neither switch, strict_mcp_config only, ENABLE_CLAUDEAI_MCP_SERVERS=false only, both.

Run: uv run --group spikes python spikes/s01_claude/q01b_connectors.py
"""

import asyncio
import time

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from common import HAIKU, clean_env, workdir

POLL_S = 20
VARIANTS = [
    ("keiner", False, {}),
    ("nur strict_mcp_config", True, {}),
    ("nur ENABLE_CLAUDEAI_MCP_SERVERS=false", False, {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}),
    ("beide", True, {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}),
]


async def probe(name: str, strict: bool, env: dict) -> None:
    options = ClaudeAgentOptions(
        model=HAIKU,
        cwd=workdir("q01b_connectors"),
        tools=["Read"],
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=strict,
        env=env,
    )
    seen: dict[str, tuple[str, int]] = {}
    t0 = time.monotonic()
    async with ClaudeSDKClient(options=options) as client:
        # Connectors are fetched asynchronously after start; poll for a while.
        while time.monotonic() - t0 < POLL_S:
            status = await client.get_mcp_status()
            for server in status.get("mcpServers", []):
                seen[server["name"]] = (server.get("status"), len(server.get("tools") or []))
            await asyncio.sleep(2)
    summary = {k: f"{s}, {n} Tools" for k, (s, n) in seen.items()} or "keine Server"
    print(f"[{name}] strict_mcp_config={strict} env={env} → nach {POLL_S}s: {summary}")


async def main() -> None:
    clean_env()
    for name, strict, env in VARIANTS:
        await probe(name, strict, env)


if __name__ == "__main__":
    asyncio.run(main())

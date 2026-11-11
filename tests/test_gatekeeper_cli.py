"""The supervising agent's call runs without tools (W4-04 §3.3), proven with the real CLI.

The real Claude CLI of the SDK runs with the options of `ClaudeGatekeeperModel` against a local
stand-in of the Messages API (`fake_messages_api`): no model, no request leaves the machine, a
test `HOME`, the login through the real start program with a test secret (never the keychain).
The API sees every request body: no tool and no MCP server is offered, the system prompt is the
supervising agent's. When the „model“ asks for a Bash call anyway, nothing runs.
Counter-check: the same CLI with the default tool set offers tools, and the same Bash call runs.
"""

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_messages_api import FakeMessagesApi, ToolCall, _text_stream, _tool_stream

from arr.backends.claude_start import shim_text
from arr.models import AuthKind, AuthProfile
from arr.permissions.gatekeeper import (
    SYSTEM_PROMPT,
    CallFailed,
    ClaudeGatekeeperModel,
    parse_answer,
)

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="the CLI probe runs on the Mac only"),
]

ANSWER = '{"decision": "allow", "reason": "sympy ist ein übliches Mathe-Paket."}'
TOKEN = "sk-ant-api03-" + "T" * 40


class RecordingApi(FakeMessagesApi):
    """Answers every request with `reply` (text or a tool call) and keeps every body."""

    def __init__(self, reply: str | ToolCall) -> None:
        super().__init__([])
        self.reply = reply
        self.bodies: list[dict[str, Any]] = []

    def answer(self, body: Any) -> bytes:
        self.bodies.append(body)
        model = str(body.get("model", "fake"))
        if isinstance(self.reply, ToolCall) and not any(
            isinstance(m.get("content"), list)
            and any(b.get("type") == "tool_result" for b in m["content"] if isinstance(b, dict))
            for m in body.get("messages", [])
        ):
            return _tool_stream(self.reply, model)
        return _text_stream(ANSWER, model)


@pytest.fixture
def lab(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    base = Path(os.path.realpath(tmp_path))
    home = base / "home"
    home.mkdir()
    arr_home.mkdir(parents=True, exist_ok=True)
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n  - {name: probe, kind: claude_api_key}\n", "utf-8"
    )
    secret = base / "secret"
    secret.write_text(TOKEN)
    secret.chmod(0o600)
    shim = base / "start" / "claude-start-probe"
    shim.parent.mkdir()
    entry = (
        "from pathlib import Path; from arr.backends.claude_start import main; "
        f"main(reader=lambda service, account: Path({str(secret)!r}).read_text())"
    )
    shim.write_text(shim_text(entry))
    shim.chmod(0o700)
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: TOKEN)
    return {"home": home, "shim": shim, "marker": base / "marker.txt"}


def model_for(lab: dict[str, Any], api: RecordingApi) -> ClaudeGatekeeperModel:
    env = {
        "ANTHROPIC_BASE_URL": api.url,
        "HOME": str(lab["home"]),
        "DISABLE_AUTOUPDATER": "1",
    }
    return ClaudeGatekeeperModel(
        model="claude-haiku-5-5",
        profile=AuthProfile(name="probe", kind=AuthKind.CLAUDE_API_KEY),
        extra_env=env,
        cli_path=str(lab["shim"]),
    )


def main_requests(api: RecordingApi) -> list[dict[str, Any]]:
    return [b for b in api.bodies if SYSTEM_PROMPT[:40] in str(b.get("system"))]


def test_the_call_offers_no_tool_and_no_mcp_server(lab: dict[str, Any]) -> None:
    with RecordingApi(ANSWER) as api:
        reply = asyncio.run(
            asyncio.wait_for(model_for(lab, api).ask(SYSTEM_PROMPT, "Anfrage"), 120)
        )
    assert parse_answer(reply.text) is not None
    assert api.bodies, "the CLI asked nothing"
    for body in api.bodies:
        assert not body.get("tools"), [t.get("name") for t in body.get("tools") or []]
        assert not body.get("mcp_servers")
    assert len(main_requests(api)) == 1  # one model request for the decision
    print(f"\nAnfragen an die API je Aufruf: {len(api.bodies)}")  # noqa: T201


def test_a_tool_request_of_the_model_runs_nothing(lab: dict[str, Any]) -> None:
    marker = lab["marker"]
    call = ToolCall("Bash", {"command": f"touch {marker}", "description": "Probe"})
    with RecordingApi(call) as api:
        try:
            reply = asyncio.run(
                asyncio.wait_for(model_for(lab, api).ask(SYSTEM_PROMPT, "Anfrage"), 120)
            )
        except CallFailed:
            reply = None
    assert not marker.exists()
    assert reply is None or parse_answer(reply.text) is None or "Bash" not in reply.text
    for body in api.bodies:
        assert not body.get("tools")


def test_counter_check_with_the_default_tools_the_same_call_runs(lab: dict[str, Any]) -> None:
    """Without `tools=[]` and the denials the probe sees tools and the Bash call runs."""
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk.types import PermissionResultAllow

    async def allow(_tool: str, tool_input: dict[str, Any], _context: Any) -> Any:
        return PermissionResultAllow(updated_input=tool_input)

    marker = lab["marker"]
    call = ToolCall("Bash", {"command": f"touch {marker}", "description": "Probe"})

    async def run(api: RecordingApi) -> None:
        options = model_for(lab, api).options(SYSTEM_PROMPT)
        options = replace(
            options, tools=None, disallowed_tools=[], can_use_tool=allow, hooks=None, max_turns=3
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query("Anfrage")
            async for _ in client.receive_response():
                pass
        finally:
            await client.disconnect()

    with RecordingApi(call) as api:
        asyncio.run(asyncio.wait_for(run(api), 120))
    assert any(body.get("tools") for body in api.bodies)
    assert marker.exists()

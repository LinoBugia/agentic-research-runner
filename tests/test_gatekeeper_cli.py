"""The supervising agent's call runs without tools (W4-04 §3.3), proven with the real CLI.

The real Claude CLI of the SDK runs with the options of `ClaudeGatekeeperModel` against a local
stand-in of the Messages API (`fake_messages_api`): no model, a test `HOME` and `ARR_HOME`, the
login through the real start program with a test secret. The API sees every request body: no
tool and no MCP server is offered, the system prompt is the supervising agent's. When the
„model“ asks for a Bash call anyway, nothing runs. Counter-check: the same CLI with the default
tool set offers tools, and the same Bash call runs.

The lab keeps the real keychain and the real API out (W4-04.1, Punkt 0; SECURITY.md §5.1):
`login_guard_kit.lab_env` (no credential of this process, test `HOME`), a `security` stub first
on the `PATH` that records each call, and the CLI starts under `sandbox-exec` with a profile that
forbids `/usr/bin/security` and every connection but to this machine. The tests check that the
stub was asked and that the real `security` cannot run.
"""

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_messages_api import FakeMessagesApi, ToolCall, _text_stream, _tool_stream

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
SEATBELT = (
    "(version 1)(allow default)"
    '(deny process-exec (literal "/usr/bin/security"))'
    '(deny network-outbound (remote ip "*:*"))'
    '(allow network-outbound (remote ip "localhost:*"))'
)
"""Around the CLI: the real `security` never runs, and nothing but this machine is reached."""


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
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    import login_guard_kit as kit

    base = Path(os.path.realpath(tmp_path))
    home = base / "home"
    (home / ".arr").mkdir(parents=True)
    calls = base / "security-calls.txt"
    with kit.lab_env(base, home):
        # the stub of `guard_bin`, now recording every call (it still finds nothing)
        stub = base / "guard-bin" / "security"
        stub.write_text(f'#!/bin/sh\necho "$0 $*" >> {shlex.quote(str(calls))}\nexit 44\n')
        stub.chmod(0o755)
        (home / ".arr" / "profiles.yaml").write_text(
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
        shim.write_text(
            "#!/bin/sh\nexec /usr/bin/sandbox-exec -p "
            f"{shlex.quote(SEATBELT)} {shlex.quote(sys.executable)} -I -c "
            f'{shlex.quote(entry)} "$@"\n'
        )
        shim.chmod(0o700)
        # `claude_env` only asks whether there is a secret; never the keychain
        monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: TOKEN)
        yield {"home": home, "shim": shim, "marker": base / "marker.txt", "calls": calls}


def model_for(lab: dict[str, Any], api: RecordingApi) -> ClaudeGatekeeperModel:
    return ClaudeGatekeeperModel(
        model="claude-haiku-5-5",
        profile=AuthProfile(name="probe", kind=AuthKind.CLAUDE_API_KEY),
        extra_env={"ANTHROPIC_BASE_URL": api.url, "DISABLE_AUTOUPDATER": "1"},
        cli_path=str(lab["shim"]),
    )


def main_requests(api: RecordingApi) -> list[dict[str, Any]]:
    return [b for b in api.bodies if SYSTEM_PROMPT[:40] in str(b.get("system"))]


def keychain_kept_out(lab: dict[str, Any]) -> None:
    """The CLI asked the stub (never the real `security`, which the seatbelt forbids)."""
    calls = lab["calls"].read_text().splitlines() if lab["calls"].exists() else []
    assert calls, "the CLI did not ask the security stub"
    assert all(
        line.split()[0] == str(lab["calls"].parent / "guard-bin" / "security") for line in calls
    )


def test_the_seatbelt_of_the_lab_keeps_the_real_security_and_the_network_out() -> None:
    """Counter-check of the lab itself: under its profile `/usr/bin/security` cannot start
    and no host outside this machine is reached."""
    run = ["/usr/bin/sandbox-exec", "-p", SEATBELT]
    security = subprocess.run([*run, "/usr/bin/security", "-h"], capture_output=True, timeout=10)
    assert security.returncode == 71  # exec denied: the tool never ran
    probe = "import socket; socket.create_connection(('192.0.2.1', 443), timeout=2)"
    network = subprocess.run(
        [*run, sys.executable, "-I", "-c", probe], capture_output=True, timeout=20
    )
    assert network.returncode != 0 and b"PermissionError" in network.stderr
    allowed = subprocess.run([*run, "/bin/echo", "ok"], capture_output=True, timeout=10)
    assert allowed.stdout == b"ok\n"  # the profile allows everything else


def test_the_call_offers_no_tool_and_no_mcp_server(lab: dict[str, Any]) -> None:
    with RecordingApi(ANSWER) as api:
        call = model_for(lab, api).ask(SYSTEM_PROMPT, "Anfrage")
        reply = asyncio.run(asyncio.wait_for(call, 120))
    assert parse_answer(reply.text) is not None
    assert api.bodies, "the CLI asked nothing"
    for body in api.bodies:
        assert not body.get("tools"), [t.get("name") for t in body.get("tools") or []]
        assert not body.get("mcp_servers")
    assert len(main_requests(api)) == 1  # one model request for the decision
    keychain_kept_out(lab)
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

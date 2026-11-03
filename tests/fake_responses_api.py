"""A local stand-in for the OpenAI Responses API, so the real Codex app-server runs without a model.

The counterpart of `fake_messages_api.py`: `POST /v1/responses` answers with one
`exec_command` call per step of `script` (a `Patch` step becomes a call of the `apply_patch`
tool), the app-server runs it for real, and the output comes back here. Codex reaches it as
its own provider (`provider_overrides`), so no request leaves the machine and no login is
needed. Headers are never read or stored.
"""

import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_USAGE = {
    "input_tokens": 1,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 1,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 2,
}


YIELD_MS = 120_000
"""How long `exec_command` waits for the command before the turn goes on (default 10 s)."""


@dataclass(frozen=True)
class Patch:
    """A step that calls Codex's own `apply_patch` tool with `text` (W3-06.4)."""

    text: str


def add_file(path: str, line: str = "x") -> Patch:
    return Patch(f"*** Begin Patch\n*** Add File: {path}\n+{line}\n*** End Patch\n")


def _sse(data: Mapping[str, Any]) -> bytes:
    return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n".encode()


def _item(step: int, command: "str | Patch | None") -> dict[str, Any]:
    if isinstance(command, Patch):
        return {
            "type": "custom_tool_call",
            "id": f"ctc_{step}",
            "call_id": f"call_{step}",
            "name": "apply_patch",
            "input": command.text,
        }
    if command is None:
        content = [{"type": "output_text", "text": "fertig"}]
        return {"type": "message", "role": "assistant", "id": f"msg_{step}", "content": content}
    return {
        "type": "function_call",
        "id": f"fc_{step}",
        "call_id": f"call_{step}",
        "name": "exec_command",
        "arguments": json.dumps({"cmd": command, "yield_time_ms": YIELD_MS}),
    }


class FakeResponsesApi:
    """Plays `script` (shell lines) across all turns; use as a context manager.

    `outputs` holds the output of every command in order.
    """

    def __init__(self, script: Iterable["str | Patch"]) -> None:
        self.script = list(script)
        self.outputs: list[str] = []
        self.requests = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def provider_overrides(self) -> tuple[str, ...]:
        """`--config` lines that make the app-server use this server as its model."""
        provider = (
            f'{{name="fake",base_url="{self.url}",wire_api="responses",requires_openai_auth=false}}'
        )
        return ('model_provider="fake"', f"model_providers.fake={provider}")

    def __enter__(self) -> "FakeResponsesApi":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def answer(self, body: Mapping[str, Any]) -> bytes:
        """Outputs arrive as the whole history; their number is the next step."""
        outputs = [
            str(item.get("output", ""))
            for item in body.get("input", [])
            if isinstance(item, dict)
            and item.get("type") in ("function_call_output", "custom_tool_call_output")
        ]
        with self._lock:
            self.requests += 1
            self.outputs += outputs[len(self.outputs) :]
            step = len(outputs)
        command = self.script[step] if step < len(self.script) else None
        response = f"resp_{self.requests}"
        return (
            _sse({"type": "response.created", "response": {"id": response}})
            + _sse(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": _item(step, command),
                }
            )
            + _sse({"type": "response.completed", "response": {"id": response, "usage": _USAGE}})
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _send(self, status: int, payload: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._send(404, b"{}", "application/json")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                if not self.path.endswith("/responses"):
                    self._send(404, b"{}", "application/json")
                    return
                body = json.loads(raw or b"{}")
                self._send(200, api.answer(body), "text/event-stream")

        return Handler

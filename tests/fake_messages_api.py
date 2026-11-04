"""A local stand-in for the Anthropic Messages API, so the real CLI runs without a model.

The Claude sandbox only exists around the CLI's Bash tool, so proving it needs the real CLI.
This server answers `POST /v1/messages` with a fixed list of tool calls, the CLI executes
them for real, and the results come back here. No request leaves the machine.

It keeps tool results and nothing else: headers (credentials) are never stored or logged, a
request only counts up `credential_seen` when it carries the marker of `credential`.
"""

import json
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One step of the script: the model „calls“ `name` with `input`."""

    name: str
    input: Mapping[str, Any]


def _usage() -> dict[str, int]:
    return {"input_tokens": 1, "output_tokens": 1}


def _sse(event: str, data: Mapping[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _start(model: str) -> bytes:
    message = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": model,
        "stop_reason": None,
        "stop_sequence": None,
        "usage": _usage(),
    }
    return _sse("message_start", {"type": "message_start", "message": message})


def _stop(reason: str) -> bytes:
    delta = {"stop_reason": reason, "stop_sequence": None}
    return _sse(
        "message_delta", {"type": "message_delta", "delta": delta, "usage": _usage()}
    ) + _sse("message_stop", {"type": "message_stop"})


def _tool_stream(call: ToolCall, model: str) -> bytes:
    block = {
        "type": "tool_use",
        "id": f"toolu_{uuid.uuid4().hex[:24]}",
        "name": call.name,
        "input": {},
    }
    delta = {"type": "input_json_delta", "partial_json": json.dumps(call.input)}
    return (
        _start(model)
        + _sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": block},
        )
        + _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        + _stop("tool_use")
    )


def _text_stream(text: str, model: str) -> bytes:
    block = {"type": "text", "text": ""}
    delta = {"type": "text_delta", "text": text}
    return (
        _start(model)
        + _sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": block},
        )
        + _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        + _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        + _stop("end_turn")
    )


def _result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


class FakeMessagesApi:
    """Plays `script` once per turn-opening prompt; use as a context manager.

    `results` holds the text of every tool result in order, `errors` whether it was an error.
    """

    def __init__(self, script: Iterable[ToolCall], *, credential: str | None = None) -> None:
        self.script = list(script)
        self.credential = credential
        self.credential_seen = 0
        self.results: list[str] = []
        self.errors: list[bool] = []
        self._lock = threading.Lock()
        self._seen = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "FakeMessagesApi":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def note_credential(self, *headers: str | None) -> None:
        """Compare and forget: `headers` are the values of the two login headers."""
        if self.credential and any(self.credential in (value or "") for value in headers):
            with self._lock:
                self.credential_seen += 1

    def _note_results(self, body: Mapping[str, Any]) -> None:
        """Results arrive as the whole history; only new ones count."""
        blocks = [
            block
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        for block in blocks[self._seen :]:
            self.results.append(_result_text(block))
            self.errors.append(bool(block.get("is_error")))
        self._seen = len(blocks)

    def answer(self, body: Mapping[str, Any]) -> bytes:
        model = str(body.get("model", "fake"))
        if not body.get("tools"):  # a side request of the CLI, e.g. a title
            return _text_stream("ok", model)
        with self._lock:
            self._note_results(body)
            step = self._seen
        if step < len(self.script):
            return _tool_stream(self.script[step], model)
        return _text_stream("fertig", model)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass  # the request line stays out of the test output

            def _send(self, status: int, payload: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._send(404, b"{}", "application/json")

            do_HEAD = do_GET  # noqa: N815

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                api.note_credential(
                    self.headers.get("Authorization"), self.headers.get("x-api-key")
                )
                if not self.path.startswith("/v1/messages") or "count_tokens" in self.path:
                    self._send(200, b'{"input_tokens": 1}', "application/json")
                    return
                body = json.loads(raw or b"{}")
                if body.get("stream"):
                    self._send(200, api.answer(body), "text/event-stream")
                    return
                reply = {
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "model": str(body.get("model", "fake")),
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": _usage(),
                }
                self._send(200, json.dumps(reply).encode(), "application/json")

        return Handler

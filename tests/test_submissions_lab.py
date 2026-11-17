"""`submit` with nested `fields` through both real engines, in the lab (W4b-01 A9).

The real Claude CLI (against `FakeMessagesApi`) and the real Codex app-server (against a fake
Responses API) call the room tool `submit` of a `phases` run, as a model would, with a review:
a verdict and a list of objections, each an object. No model, no real login: the lab of the
login guard (`login_guard_kit.py`), with a test `HOME`, a `security` stub and only localhost.
"""

import json
import re
import sys
from collections.abc import Mapping
from typing import Any

import login_guard_kit as kit
import pytest
from fake_messages_api import ToolCall
from fake_responses_api import FakeResponsesApi, _sse

from arr.models import ResolvedRun, TopologyKind
from arr.resolve import dump_yaml_model, load_yaml_model
from arr.room.store import RoomStore

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt exists on macOS only"),
]

FIELDS = {
    "verdict": "major",
    "objections": [
        {"text": "Der Basisfall n = 1 fehlt."},
        {"ref": "E1", "status": "behoben", "text": "Lemma 2 ist jetzt bewiesen."},
    ],
}
SUBMIT = {
    "status": "done",
    "summary": "Zwei Einwände, einer neu.",
    "fields": FIELDS,
}


def make_phases(lab: kit.Lab) -> RoomStore:
    """The lab run becomes a `phases` run whose newest assignment expects a review."""
    run_yaml = lab.run.run_dir / "run.yaml"
    run = load_yaml_model(run_yaml, ResolvedRun)
    topology = run.topology.model_copy(update={"kind": TopologyKind.PHASES})
    phases = run.model_copy(update={"topology": topology})
    run_yaml.write_text(dump_yaml_model(phases), encoding="utf-8")
    store = RoomStore(lab.run.run_dir)
    store.add_assignment(
        lab.agent.id,
        1,
        "Prüfe.",
        stage="pruefung",
        context={"submission": {"format": "review", "known_objections": ["E1"]}},
    )
    return store


def check_filed(store: RoomStore, agent: str) -> dict[str, Any]:
    [sub] = store.submissions()
    assert (sub.agent, sub.format, sub.stage) == (agent, "review", "pruefung")
    assert sub.fields == FIELDS, "the nested fields arrived unchanged"
    data = json.loads((store.run_dir / sub.path / "submission.json").read_text())
    assert data["fields"] == FIELDS
    return {"id": sub.id, "path": sub.path, "fields": sub.fields}


def test_claude_cli_calls_submit_with_nested_fields() -> None:
    with kit.open_lab("claude") as lab:
        store = make_phases(lab)
        api = kit.run_file_tools(
            lab,
            [
                ToolCall("mcp__arr-room__submit", {**SUBMIT, "fields": {"verdict": "gut"}}),
                ToolCall("mcp__arr-room__submit", SUBMIT),
            ],
        )
        print("Claude-CLI, Antworten des Werkzeugs:", json.dumps(api.results, ensure_ascii=False))
        assert len(api.results) == 2
        assert (
            "„fields.verdict“: Erwartet: einer von accept, minor, major, reject" in api.results[0]
        )
        assert "Abgabe #1 angenommen (review, done, Phase pruefung" in api.results[1]
        filed = check_filed(store, lab.agent.id)
        print("Claude-CLI, abgelegt:", json.dumps(filed, ensure_ascii=False))


NOT_READY = "ROOM-NOCH-NICHT-BEREIT"

CELL = """// @exec: {"yield_time_ms": 60000}
if (typeof tools.mcp__arr_room__%(tool)s !== "function") {
  text("%(not_ready)s " + Object.keys(tools).filter((n) => n.includes("arr")).join(","));
  exit();
}
const answer = await tools.mcp__arr_room__%(tool)s(%(arguments)s);
text(JSON.stringify(answer));
"""
"""An `exec` cell as a model writes it: Codex offers MCP tools only inside `functions.exec`, as
`tools.mcp__arr_room__<tool>` (W4b-00, prompt `codex.md`). A server that is not ready yet
gives no function; then the cell says so and the next request sends it again."""


class McpResponsesApi(FakeResponsesApi):
    """`FakeResponsesApi` whose steps call a room tool from an `exec` cell (`CELL`)."""

    def __init__(self, steps: list[tuple[str, dict[str, Any]]], tries: int = 20) -> None:
        super().__init__([])
        self.steps = steps
        self.tries = tries
        self.offered: list[str] = []

    def answer(self, body: Mapping[str, Any]) -> bytes:
        outputs = [
            str(item.get("output", ""))
            for item in body.get("input", [])
            if isinstance(item, dict)
            and item.get("type") in ("function_call_output", "custom_tool_call_output")
        ]
        with self._lock:
            self.requests += 1
            self.outputs += outputs[len(self.outputs) :]
            done = [o for o in self.outputs if NOT_READY not in o]
            waiting = [o for o in self.outputs if NOT_READY in o]
            if waiting:
                self.offered = sorted(set(re.findall(r"mcp__arr_room__\w+", waiting[-1])))
            step = len(done)
        response = f"resp_{self.requests}"
        if step < len(self.steps) and len(waiting) < self.tries:
            tool, arguments = self.steps[step]
            cell = CELL % {
                "tool": tool,
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "not_ready": NOT_READY,
            }
            item: dict[str, Any] = {
                "type": "custom_tool_call",
                "id": f"ctc_{self.requests}",
                "call_id": f"call_{self.requests}",
                "name": "exec",
                "namespace": "functions",
                "input": cell,
            }
        else:
            content = [{"type": "output_text", "text": "fertig"}]
            item = {"type": "message", "role": "assistant", "id": f"msg_{step}", "content": content}
        return (
            _sse({"type": "response.created", "response": {"id": response}})
            + _sse({"type": "response.output_item.done", "output_index": 0, "item": item})
            + _sse(
                {
                    "type": "response.completed",
                    "response": {"id": response, "usage": {"input_tokens": 1, "output_tokens": 1}},
                }
            )
        )

    @property
    def answers(self) -> list[str]:
        """The outputs of the cells that reached the room tool."""
        return [o for o in self.outputs if NOT_READY not in o]


def test_codex_app_server_calls_submit_with_nested_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    script = [("submit", {**SUBMIT, "fields": {"verdict": "gut"}}), ("submit", SUBMIT)]
    with kit.open_lab("codex") as lab:
        store = make_phases(lab)
        api = McpResponsesApi(script)
        monkeypatch.setattr(kit, "FakeResponsesApi", lambda _script: api)
        kit.run_codex_backend(lab, [])
        print("Codex, Antworten des Werkzeugs:", json.dumps(api.answers, ensure_ascii=False))
        assert len(api.answers) == 2, api.outputs
        assert (
            "„fields.verdict“: Erwartet: einer von accept, minor, major, reject" in api.answers[0]
        )
        assert "Abgabe #1 angenommen (review, done, Phase pruefung" in api.answers[1]
        filed = check_filed(store, lab.agent.id)
        print("Codex, abgelegt:", json.dumps(filed, ensure_ascii=False))


def test_without_phases_the_app_server_offers_no_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counter-check with the real app-server: a `solo` run has no `submit` (A10), the same
    cell reaches `submit_result` there."""
    with kit.open_lab("codex") as lab:
        api = McpResponsesApi([("submit", SUBMIT)], tries=3)
        monkeypatch.setattr(kit, "FakeResponsesApi", lambda _script: api)
        kit.run_codex_backend(lab, [])
        print("Codex ohne phases, Room-Werkzeuge:", api.offered)
        assert api.answers == [] and api.offered
        assert "mcp__arr_room__submit_result" in api.offered
        assert "mcp__arr_room__submit" not in api.offered

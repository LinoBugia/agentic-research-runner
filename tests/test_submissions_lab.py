"""`submit` with nested `fields` through both real engines, in the lab (W4b-01 A9).

The real Claude CLI (against `FakeMessagesApi`) and the real Codex app-server (against a fake
Responses API) call the room tool `submit` of a `phases` run, as a model would, with a review:
a verdict and a list of objections, each an object. No model, no real login: the lab of the
login guard (`login_guard_kit.py`), with a test `HOME`, a `security` stub and only localhost.
"""

import json
import re
import sys
import time
from collections.abc import Mapping
from typing import Any

import login_guard_kit as kit
import pytest
from fake_messages_api import ToolCall
from fake_responses_api import _USAGE, FakeResponsesApi, _sse

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
gives no function; then the cell says so and the next request sends it again, after a pause,
for at most `WAIT_S`. Under load the room server needs some seconds (W4b-03 §6.9: 0.3 s
alone, 1.5 s beside 32 and 5.2 s beside 100 busy processes on 10 cores); Codex 0.163.0 waits
about 30 s for it (`startup_timeout_sec`) and adds its tools to the running turn."""

WAIT_S = 90.0
PAUSE_S = 0.5


class McpResponsesApi(FakeResponsesApi):
    """`FakeResponsesApi` whose steps call a room tool from an `exec` cell (`CELL`)."""

    def __init__(
        self, steps: list[tuple[str, dict[str, Any]]], *, stop_when_room_seen: bool = False
    ) -> None:
        super().__init__([])
        self.steps = steps
        self.stop_when_room_seen = stop_when_room_seen
        self.offered: list[str] = []
        self.started = time.monotonic()

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
            again = bool(self.outputs) and NOT_READY in self.outputs[-1]
        response = f"resp_{self.requests}"
        go_on = step < len(self.steps) and time.monotonic() - self.started < WAIT_S
        if again and self.stop_when_room_seen and self.offered:
            go_on = False  # the counter-check only needs the list of room tools
        elif again and go_on:
            time.sleep(PAUSE_S)  # the room server is still starting
        if go_on:
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
            # the whole usage: without `total_tokens` Codex 0.163.0 cannot read the event, takes
            # the stream for broken, sends the same request again five times and then ends the
            # turn with „stream disconnected before completion“ (W4b-03 §6.9). That ended the
            # turn after about 7 s, before a room server under load was ready.
            + _sse({"type": "response.completed", "response": {"id": response, "usage": _USAGE}})
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


def test_every_run_offers_submit_and_no_submit_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counter-check with the real app-server (A10), since W4b-06b the other way round (A3):
    every run offers `submit`, none `submit_result`. The cell asks for `submit_result`, finds
    no such tool and lists the room tools it does find."""
    with kit.open_lab("codex") as lab:
        api = McpResponsesApi([("submit_result", SUBMIT)], stop_when_room_seen=True)
        monkeypatch.setattr(kit, "FakeResponsesApi", lambda _script: api)
        kit.run_codex_backend(lab, [])
        print("Codex im Labor-Run, Room-Werkzeuge:", api.offered)
        assert "mcp__arr_room__submit" in api.offered
        assert not {"mcp__arr_room__submit_result", "mcp__arr_room__submit_rating"} & set(
            api.offered
        )
        assert api.answers == []

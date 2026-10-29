"""EventLogger and transcript renderer (W1-04 A1, A2): format, crash safety, blobs, snapshot."""

import dataclasses
import json
import subprocess
import sys
import textwrap
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import AgentEvent, EventKind
from arr.runs.eventlog import (
    BLOB_THRESHOLD,
    EventLogger,
    read_events,
    render_transcript,
    write_transcript_md,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fake"
RUN = "20261023-1742_collatz_exploration_a3f9"


def event(kind: EventKind, data: dict[str, Any], *, turn: int = 1, raw: Any = None) -> AgentEvent:
    return AgentEvent(
        run_id=RUN, agent_id="explorer-1", turn=turn, kind=kind, data=data, ts=1761234000.5, raw=raw
    )


def tool_result(content: str | None, call_id: str = "toolu_01") -> AgentEvent:
    return event(
        EventKind.TOOL_RESULT, {"id": call_id, "is_error": False, "content": content, "blob": None}
    )


# --------------------------------------------------------------------------------------------
# A1: format and crash safety
# --------------------------------------------------------------------------------------------


def test_transcript_lines_follow_docs_02_6(tmp_path: Path) -> None:
    with EventLogger(tmp_path / "explorer-1") as logger:
        logger.log(event(EventKind.TURN_START, {"prompt": "Stufe 1", "stage": "explore"}))
        logger.log(event(EventKind.TEXT, {"text": "Hallo"}))
        logger.log(
            event(
                EventKind.TOOL_CALL, {"id": "toolu_01", "name": "Bash", "input": {"command": "ls"}}
            )
        )
    lines = (tmp_path / "explorer-1" / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert set(records[0]) == {"seq", "ts", "run", "agent", "turn", "kind", "data"}
    assert records[0] == {
        "seq": 1,
        "ts": 1761234000.5,
        "run": RUN,
        "agent": "explorer-1",
        "turn": 1,
        "kind": "turn_start",
        "data": {"prompt": "Stufe 1", "stage": "explore"},
    }
    assert records[2]["data"]["input"] == {"command": "ls"}


def test_seq_is_assigned_on_event_and_returned_record(tmp_path: Path) -> None:
    ev = event(EventKind.TEXT, {"text": "x"})
    with EventLogger(tmp_path) as logger:
        record = logger.log(ev)
        assert ev.seq == record["seq"] == logger.seq == 1
        logger.log(event(EventKind.TEXT, {"text": "y"}))
        assert logger.seq == 2


def test_every_event_is_readable_before_close(tmp_path: Path) -> None:
    """Flush per event: a second reader sees each event right after `log`."""
    logger = EventLogger(tmp_path)
    try:
        for n in range(1, 4):
            logger.log(event(EventKind.TEXT, {"text": f"t{n}"}, raw={"n": n}))
            assert [r["seq"] for r in read_events(tmp_path / "transcript.jsonl")] == list(
                range(1, n + 1)
            )
            assert [r["seq"] for r in read_events(tmp_path / "raw.jsonl")] == list(range(1, n + 1))
    finally:
        logger.close()


def test_crash_without_close_leaves_file_readable(tmp_path: Path) -> None:
    """A process that dies mid-run (no close, no cleanup) loses no event it had logged."""
    code = textwrap.dedent(
        """
        import os, sys
        from arr.backends.base import AgentEvent, EventKind
        from arr.runs.eventlog import EventLogger

        logger = EventLogger(sys.argv[1])
        for n in range(5):
            logger.log(AgentEvent("r", "a", 1, EventKind.TEXT, {"text": f"t{n}"}, raw={"n": n}))
        os._exit(1)  # hard exit: no atexit, no buffer flush
        """
    )
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True)
    assert result.returncode == 1
    records = read_events(tmp_path / "transcript.jsonl")
    assert [r["data"]["text"] for r in records] == [f"t{n}" for n in range(5)]
    assert len(read_events(tmp_path / "raw.jsonl")) == 5


def test_torn_last_line_is_skipped_and_repaired_on_reopen(tmp_path: Path) -> None:
    with EventLogger(tmp_path) as logger:
        logger.log(event(EventKind.TEXT, {"text": "eins"}))
        logger.log(event(EventKind.TEXT, {"text": "zwei"}))
    transcript = tmp_path / "transcript.jsonl"
    with transcript.open("ab") as handle:
        handle.write(b'{"seq":3,"ts":1.0,"run":"r","agent":"a","turn":1,"kind":"te')  # torn write
    assert [r["seq"] for r in read_events(transcript)] == [1, 2]

    with EventLogger(tmp_path) as logger:  # service restart
        assert logger.seq == 2
        logger.log(event(EventKind.TEXT, {"text": "drei"}))
    records = read_events(transcript)
    assert [(r["seq"], r["data"]["text"]) for r in records] == [
        (1, "eins"),
        (2, "zwei"),
        (3, "drei"),
    ]
    assert all(line.endswith("}") for line in transcript.read_text(encoding="utf-8").splitlines())


def test_unicode_and_line_separators_stay_on_one_line(tmp_path: Path) -> None:
    text = "Größe ≥ 2 – α β γ\nzweite Zeile"
    with EventLogger(tmp_path) as logger:
        logger.log(event(EventKind.TEXT, {"text": text}))
    raw_lines = (tmp_path / "transcript.jsonl").read_bytes().split(b"\n")
    assert len(raw_lines) == 2 and raw_lines[1] == b""  # exactly one line
    assert "Größe ≥ 2".encode() in raw_lines[0]  # not escaped to \uXXXX
    assert read_events(tmp_path / "transcript.jsonl")[0]["data"]["text"] == text


@dataclasses.dataclass
class _SdkMessage:
    content: object
    handle: object


def test_unserializable_raw_objects_do_not_break_logging(tmp_path: Path) -> None:
    circular: list[Any] = []
    circular.append(circular)

    class Weird:
        def __str__(self) -> str:
            raise RuntimeError("no str")

    raws = [object(), {1, 2}, b"bytes\xff", _SdkMessage(content=object(), handle=Weird()), circular]
    with EventLogger(tmp_path) as logger:
        for raw in raws:
            logger.log(event(EventKind.TEXT, {"text": "x"}, raw=raw))
        logger.log(event(EventKind.TOOL_CALL, {"id": "t", "name": "n", "input": {"o": object()}}))
        logger.log(event(EventKind.TEXT, {"text": "danach"}))
    assert [r["seq"] for r in read_events(tmp_path / "raw.jsonl")] == [1, 2, 3, 4, 5]
    raw_records = read_events(tmp_path / "raw.jsonl")
    assert raw_records[1]["raw"] == [1, 2]
    assert raw_records[3]["raw"]["content"].startswith("<object object")
    assert raw_records[4]["raw"] == ["<zirkulär>"]  # only the cycle is replaced
    transcript = read_events(tmp_path / "transcript.jsonl")
    assert [r["seq"] for r in transcript] == [1, 2, 3, 4, 5, 6, 7]
    assert transcript[5]["data"]["input"]["o"].startswith("<object object")


def test_events_without_raw_write_no_raw_line(tmp_path: Path) -> None:
    with EventLogger(tmp_path) as logger:
        logger.log(event(EventKind.TEXT, {"text": "a"}))
        logger.log(event(EventKind.TEXT, {"text": "b"}, raw={"sdk": 1}))
    raw = read_events(tmp_path / "raw.jsonl")
    assert raw == [{"seq": 2, "ts": 1761234000.5, "raw": {"sdk": 1}}]


def test_log_after_close_raises_and_close_is_idempotent(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path)
    logger.close()
    logger.close()
    with pytest.raises(ValueError, match="geschlossen"):
        logger.log(event(EventKind.TEXT, {"text": "x"}))


# --------------------------------------------------------------------------------------------
# A2: blobs
# --------------------------------------------------------------------------------------------


def test_large_tool_output_goes_to_blobs(tmp_path: Path) -> None:
    big = "ä" * 600_000  # 1.2 MB in UTF-8, only 600k characters
    with EventLogger(tmp_path) as logger:
        ev = tool_result(big, call_id="toolu/01:x")
        record = logger.log(ev)
    assert record["data"]["content"] is None
    assert record["data"]["blob"] == "blobs/000001-toolu_01_x.txt"
    assert (tmp_path / record["data"]["blob"]).read_text(encoding="utf-8") == big
    on_disk = read_events(tmp_path / "transcript.jsonl")[0]
    assert on_disk["data"] == {
        "id": "toolu/01:x",
        "is_error": False,
        "content": None,
        "blob": "blobs/000001-toolu_01_x.txt",
    }
    assert ev.data["content"] == big  # the caller's event is untouched
    assert "ä" * 100 not in (tmp_path / "transcript.jsonl").read_text(encoding="utf-8")


def test_blob_threshold_is_one_megabyte_in_bytes(tmp_path: Path) -> None:
    assert BLOB_THRESHOLD == 1_000_000
    with EventLogger(tmp_path) as logger:
        at_limit = logger.log(tool_result("a" * 1_000_000, "at"))
        above = logger.log(tool_result("a" * 1_000_001, "above"))
    assert at_limit["data"]["blob"] is None and len(at_limit["data"]["content"]) == 1_000_000
    assert above["data"]["blob"] == "blobs/000002-above.txt"
    assert (tmp_path / above["data"]["blob"]).stat().st_size == 1_000_001


def test_blob_is_complete_before_transcript_references_it(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path, blob_threshold=10)
    try:
        record = logger.log(tool_result("0123456789abc"))
        # readable without closing the logger
        assert (tmp_path / record["data"]["blob"]).read_text() == "0123456789abc"
    finally:
        logger.close()


def test_only_tool_results_are_offloaded(tmp_path: Path) -> None:
    with EventLogger(tmp_path, blob_threshold=10) as logger:
        text = logger.log(event(EventKind.TEXT, {"text": "x" * 100}))
        empty = logger.log(tool_result(None))
        small = logger.log(tool_result("kurz"))
    assert text["data"]["text"] == "x" * 100
    assert empty["data"]["blob"] is None and small["data"]["content"] == "kurz"
    assert not (tmp_path / "blobs").exists()


def test_non_string_tool_content_is_offloaded_as_json(tmp_path: Path) -> None:
    content = [{"type": "text", "text": "y" * 50}]
    with EventLogger(tmp_path, blob_threshold=10) as logger:
        record = logger.log(tool_result(content))  # type: ignore[arg-type]
    assert json.loads((tmp_path / record["data"]["blob"]).read_text()) == content


def test_blob_names_continue_after_reopen(tmp_path: Path) -> None:
    with EventLogger(tmp_path, blob_threshold=1) as logger:
        logger.log(tool_result("aa", "t1"))
    with EventLogger(tmp_path, blob_threshold=1) as logger:
        record = logger.log(tool_result("bb", "t1"))
    assert record["seq"] == 2 and record["data"]["blob"] == "blobs/000002-t1.txt"
    assert sorted(p.name for p in (tmp_path / "blobs").iterdir()) == [
        "000001-t1.txt",
        "000002-t1.txt",
    ]


# --------------------------------------------------------------------------------------------
# transcript.md
# --------------------------------------------------------------------------------------------


def test_transcript_md_snapshot() -> None:
    records = read_events(FIXTURES / "transcript_sample.jsonl")
    rendered = render_transcript(records, tz=UTC)
    assert rendered == (FIXTURES / "transcript_sample.md").read_text(encoding="utf-8")


def test_renderer_sets_off_thinking_and_tool_calls() -> None:
    rendered = render_transcript(read_events(FIXTURES / "transcript_sample.jsonl"), tz=UTC)
    assert "> **Gedanken (Zusammenfassung)**" in rendered
    assert "**Werkzeug** `Bash` · 15:40:05" in rendered
    assert "**Ergebnis** `toolu_01` · ok" in rendered
    assert "Ausgabe ausgelagert: `blobs/000009-toolu_03.txt`" in rendered
    assert "… (20 weitere Zeilen nicht gezeigt)" in rendered
    assert "Zeile 41" not in rendered
    assert "*Turn ohne Abschluss: der Lauf wurde unterbrochen.*" in rendered


def test_renderer_fence_survives_backticks_in_output() -> None:
    rendered = render_transcript(read_events(FIXTURES / "transcript_sample.jsonl"), tz=UTC)
    assert "````\nalle Fälle ok\n```\nRand\n```\n````" in rendered


def test_renderer_handles_empty_transcript() -> None:
    assert render_transcript([], agent_id="x") == "# Transkript – x\n"


def test_write_transcript_md_from_agent_folder(tmp_path: Path) -> None:
    agent_dir = tmp_path / "explorer-1"
    with EventLogger(agent_dir) as logger:
        logger.log(event(EventKind.TURN_START, {"prompt": "los", "stage": None}))
        logger.log(event(EventKind.TEXT, {"text": "Antwort"}))
        logger.log(
            event(
                EventKind.TURN_END,
                {
                    "status": "ok",
                    "usage": {"input": 10, "output": 5},
                    "cost_usd": 18.4,
                    "cost_kind": "billed",
                    "session": None,
                    "num_turns": 1,
                },
            )
        )
    target = write_transcript_md(agent_dir, tz=UTC)
    text = target.read_text(encoding="utf-8")
    assert target == agent_dir / "transcript.md"
    assert text.startswith("# Transkript – explorer-1")
    assert "Antwort" in text and "*Turn 1 beendet: ok · ein 10 · aus 5 · 18,40 $*" in text


# --------------------------------------------------------------------------------------------
# W1-04.1
# --------------------------------------------------------------------------------------------


def test_seq_continues_after_a_raw_line_without_transcript_line(tmp_path: Path) -> None:
    """Crash between the raw write and the transcript write must not reuse a `seq` (point 3)."""
    with EventLogger(tmp_path) as logger:
        logger.log(event(EventKind.TEXT, {"text": "eins"}, raw={"n": 1}))
    with (tmp_path / "raw.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "ts": 1.0, "raw": {"n": 2}}\n')  # transcript line 2 never written
    with EventLogger(tmp_path) as logger:
        assert logger.seq == 2
        logger.log(event(EventKind.TEXT, {"text": "drei"}, raw={"n": 3}))
    assert [r["seq"] for r in read_events(tmp_path / "raw.jsonl")] == [1, 2, 3]
    assert [r["seq"] for r in read_events(tmp_path / "transcript.jsonl")] == [1, 3]


def test_seq_continues_when_only_the_raw_file_has_a_torn_line(tmp_path: Path) -> None:
    with EventLogger(tmp_path) as logger:
        logger.log(event(EventKind.TEXT, {"text": "eins"}, raw={"n": 1}))
    with (tmp_path / "raw.jsonl").open("ab") as handle:
        handle.write(b'{"seq": 2, "raw"')
    with EventLogger(tmp_path) as logger:
        assert logger.log(event(EventKind.TEXT, {"text": "zwei"}, raw={"n": 2}))["seq"] == 2
    assert [r["seq"] for r in read_events(tmp_path / "raw.jsonl")] == [1, 2]


def strict_json(path: Path) -> list[Any]:
    """Parse like a strict parser (browser `JSON.parse`): `NaN`/`Infinity` are errors."""

    def refuse(name: str) -> Any:
        raise ValueError(f"nicht erlaubt: {name}")

    return [json.loads(line, parse_constant=refuse) for line in path.read_text().splitlines()]


def test_non_finite_numbers_are_written_as_null(tmp_path: Path) -> None:
    @dataclasses.dataclass
    class Sdk:
        ratio: float

    nan, inf = float("nan"), float("inf")
    with EventLogger(tmp_path) as logger:
        logger.log(
            event(
                EventKind.RATE_LIMIT,
                {"status": "x", "utilization": nan, "resets_at": -inf, "more": [inf, 1.5]},
                raw={"nan": nan, "sdk": Sdk(ratio=inf)},
            )
        )
        logger.log(event(EventKind.TEXT, {"text": "danach"}))
    transcript = strict_json(tmp_path / "transcript.jsonl")
    assert transcript[0]["data"] == {
        "status": "x",
        "utilization": None,
        "resets_at": None,
        "more": [None, 1.5],
    }
    assert transcript[1]["data"]["text"] == "danach"
    assert strict_json(tmp_path / "raw.jsonl")[0]["raw"] == {"nan": None, "sdk": {"ratio": None}}


def test_open_turns_in_the_middle_are_marked_and_restarts_shown() -> None:
    """Snapshot covers it; this pins the individual rules (point 4)."""
    rendered = render_transcript(read_events(FIXTURES / "transcript_sample.jsonl"), tz=UTC)
    assert rendered.count("*Turn ohne Abschluss: der Lauf wurde unterbrochen.*") == 2
    mid = rendered.index("## Turn 3 · ")
    assert rendered.index("Turn ohne Abschluss") < mid  # turn 2 is marked before turn 3 starts
    restart = rendered[rendered.index("## Turn 3 (Neustart)") :]
    assert "> Fasse noch einmal zusammen." in restart and "**Stufe:** wrapup" in restart


def test_second_turn_start_in_the_same_turn_is_a_restart_with_prompt() -> None:
    records = [
        {"seq": 1, "ts": 0, "kind": "turn_start", "turn": 1, "data": {"prompt": "alt"}},
        {"seq": 2, "ts": 1, "kind": "turn_start", "turn": 1, "data": {"prompt": "neu"}},
        {"seq": 3, "ts": 2, "kind": "turn_end", "turn": 1, "data": {"status": "ok"}},
    ]
    rendered = render_transcript(records, tz=UTC)
    assert "## Turn 1 · 00:00:00" in rendered and "## Turn 1 (Neustart) · 00:00:01" in rendered
    assert "> alt" in rendered and "> neu" in rendered
    assert rendered.count("Turn ohne Abschluss") == 1  # the first start never ended
    assert rendered.rstrip().endswith("*Turn 1 beendet: ok*")


def test_renderer_tolerates_damaged_records(tmp_path: Path) -> None:
    """JSON-valid but wrongly shaped records must not stop `write_transcript_md` (point 5)."""
    agent_dir = tmp_path / "explorer-1"
    agent_dir.mkdir()
    damaged = [
        {"seq": 1, "ts": 0, "run": "r", "agent": "a", "turn": 1, "kind": "turn_start", "data": {}},
        {"seq": 2, "ts": "abc", "turn": 1, "kind": "text", "data": {"text": "Zeit kaputt"}},
        {"seq": 3, "ts": 3, "turn": 1, "kind": "tool_call", "data": ["keine", "Map"]},
        {
            "seq": 4,
            "ts": None,
            "turn": 1,
            "kind": "tool_call",
            "data": {"name": "Bash", "input": 1},
        },
        {
            "seq": 5,
            "ts": 5,
            "turn": 1,
            "kind": "turn_end",
            "data": {"status": "ok", "usage": "kaputt"},
        },
        {
            "seq": 6,
            "ts": 6,
            "turn": 2,
            "kind": "turn_end",
            "data": {"cost_usd": "viel", "usage": []},
        },
        {"seq": 7, "ts": 7, "turn": 2, "kind": "rate_limit", "data": "x"},
        {"seq": 8, "ts": 8, "turn": 2, "kind": "hint", "data": None},
        {"seq": 9, "ts": float("nan"), "turn": 3, "kind": "text", "data": {"text": "NaN-Zeit"}},
    ]
    (agent_dir / "transcript.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in damaged) + '[1, 2]\n"nur ein String"\n',
        encoding="utf-8",
    )
    text = write_transcript_md(agent_dir, tz=UTC).read_text(encoding="utf-8")
    assert "Zeit kaputt" in text and "NaN-Zeit" in text
    assert "**Werkzeug** `Bash` · –" in text  # unusable time stamp shows as „–“
    assert "*Turn 1 beendet: ok*" in text  # usage "kaputt" is ignored
    assert "## Turn 3 · –" in text


def test_renderer_notes_a_record_it_cannot_render(caplog: pytest.LogCaptureFixture) -> None:
    class Boom(dict):  # type: ignore[type-arg]
        def get(self, key: str, default: Any = None) -> Any:
            if key == "kind":
                raise RuntimeError("kaputt")
            return super().get(key, default)

    rendered = render_transcript([Boom(seq=4), {"seq": 5, "kind": "text", "data": {"text": "ok"}}])
    assert "*(Ereignis 4 nicht darstellbar)*" in rendered and "ok" in rendered


def test_rechnerisch_only_at_the_first_estimated_amount() -> None:
    def end(seq: int, turn: int, cost: float, kind: str | None) -> dict[str, Any]:
        data = {"status": "ok", "usage": {}, "cost_usd": cost, "cost_kind": kind}
        return {"seq": seq, "ts": seq, "turn": turn, "kind": "turn_end", "data": data}

    rendered = render_transcript(
        [end(1, 1, 18.4, "billed"), end(2, 2, 0.5, "estimated"), end(3, 3, 0.25, "estimated")],
        tz=UTC,
    )
    assert "Turn 1 beendet: ok · 18,40 $*" in rendered  # billed: no ≈
    assert "Turn 2 beendet: ok · ≈ 0,50 $ (rechnerisch)*" in rendered
    assert "Turn 3 beendet: ok · ≈ 0,25 $*" in rendered
    assert rendered.count("(rechnerisch)") == 1


# --------------------------------------------------------------------------------------------
# W2-01: large tool inputs (input_blob)
# --------------------------------------------------------------------------------------------


def test_large_tool_input_goes_to_blobs(tmp_path: Path) -> None:
    big = "ä" * 600_000  # 1.2 MB in UTF-8
    tool_input = {"file_path": "proof.tex", "content": big}
    ev = event(EventKind.TOOL_CALL, {"id": "toolu/02", "name": "Write", "input": tool_input})
    with EventLogger(tmp_path) as logger:
        record = logger.log(ev)
    assert record["data"]["input"] == {"file_path": "proof.tex"}
    assert record["data"]["input_blob"] == "blobs/000001-toolu_02.input.json"
    assert json.loads((tmp_path / record["data"]["input_blob"]).read_text("utf-8")) == tool_input
    assert ev.data["input"]["content"] == big  # the caller's event is untouched
    assert "ä" * 100 not in (tmp_path / "transcript.jsonl").read_text(encoding="utf-8")
    md = render_transcript(read_events(tmp_path / "transcript.jsonl"))
    assert "Eingabe ausgelagert: `blobs/000001-toolu_02.input.json`" in md


def test_small_tool_input_stays_inline(tmp_path: Path) -> None:
    with EventLogger(tmp_path) as logger:
        record = logger.log(
            event(EventKind.TOOL_CALL, {"id": "t", "name": "Bash", "input": {"command": "ls"}})
        )
    assert record["data"] == {"id": "t", "name": "Bash", "input": {"command": "ls"}}
    assert not (tmp_path / "blobs").exists()

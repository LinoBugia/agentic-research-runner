"""EventLogger: crash-safe per-agent logs, and the readable `transcript.md`.

Per agent the logger appends to `transcript.jsonl` (normalized, docs/02 §6) and `raw.jsonl`
(raw SDK objects). Both files are only appended to and flushed after every event, so a
crash loses at most the event in flight. Tool output above 1 MB goes to `blobs/` and the
transcript refers to it. `render_transcript` turns a transcript into Markdown for
`transcript.md` (written by the finalizer, docs/02 §7).

See docs/02-workfolder-und-runs.md §4, §6.
"""

import dataclasses
import json
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, tzinfo
from pathlib import Path
from typing import IO, Any, Self

from pydantic import BaseModel

from arr.backends.base import AgentEvent, EventKind

log = logging.getLogger(__name__)

BLOB_THRESHOLD = 1_000_000
"""Tool output above this many bytes (UTF-8) is stored in `blobs/` (docs/02 §4, §6)."""

TRANSCRIPT_FILE = "transcript.jsonl"
RAW_FILE = "raw.jsonl"
BLOBS_DIR = "blobs"
TRANSCRIPT_MD_FILE = "transcript.md"

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


# --------------------------------------------------------------------------------------------
# JSON encoding that never fails
# --------------------------------------------------------------------------------------------


def _safe_str(obj: object) -> str:
    try:
        return str(obj)
    except Exception:
        return f"<{type(obj).__name__}: nicht darstellbar>"


def _json_default(obj: object) -> Any:
    """Fallback for objects `json` cannot serialize: models and dataclasses as dicts, else `str`."""
    try:
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        if isinstance(obj, bytes | bytearray):
            return bytes(obj).decode("utf-8", errors="replace")
        if isinstance(obj, set | frozenset):
            return list(obj)
    except Exception:
        pass
    return _safe_str(obj)


def _dumps(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=_json_default)
    # Keep one record per line even for tools that split on the Unicode line separators.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _encode_line(record: dict[str, Any], payload_key: str) -> str:
    """Encode `record` as one JSON line; if `record[payload_key]` cannot be serialized
    (circular references, non-string keys, …), replace it with a note and its `repr`."""
    try:
        return _dumps(record)
    except Exception:
        note = {"unserializable": _safe_str(record.get(payload_key))[:10_000]}
        return _dumps({**record, payload_key: note})


# --------------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------------


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Read the records of a `transcript.jsonl` (or `raw.jsonl`), skipping damaged lines.

    A crash can leave a half-written last line; it is skipped, everything before it is
    returned. A missing file gives an empty list.
    """
    path = Path(path)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="\n") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Beschädigte Zeile %d in %s übersprungen", number, path)
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


# --------------------------------------------------------------------------------------------
# EventLogger
# --------------------------------------------------------------------------------------------


class EventLogger:
    """Appends the events of one agent to `transcript.jsonl` and `raw.jsonl` (docs/02 §6).

    `agent_dir` is `runs/<run-id>/agents/<agent-id>/`. The logger assigns `seq` (1, 2, …) and
    flushes after every event (to the OS; it does not `fsync`, so a crash of arr loses nothing
    but a power cut might). When the files already exist (service restart), it repairs a torn
    last line and continues the numbering. Not for concurrent use from several agents, but
    safe to call from several threads for one agent.
    """

    def __init__(self, agent_dir: str | Path, *, blob_threshold: int = BLOB_THRESHOLD) -> None:
        self.agent_dir = Path(agent_dir)
        self.blob_threshold = blob_threshold
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.agent_dir / TRANSCRIPT_FILE
        self.raw_path = self.agent_dir / RAW_FILE
        self.blobs_dir = self.agent_dir / BLOBS_DIR
        self._lock = threading.Lock()
        self._seq = self._repair(self.transcript_path)
        self._repair(self.raw_path)
        self._transcript: IO[str] | None = self.transcript_path.open(
            "a", encoding="utf-8", newline="\n"
        )
        self._raw: IO[str] | None = self.raw_path.open("a", encoding="utf-8", newline="\n")

    @staticmethod
    def _repair(path: Path) -> int:
        """Cut a torn last line from `path` and return the highest `seq` found (0 if none)."""
        if not path.exists():
            return 0
        data = path.read_bytes()
        end = data.rfind(b"\n") + 1  # 0 when there is no newline at all
        if end != len(data):
            log.warning("Unvollständige letzte Zeile in %s entfernt", path)
            with path.open("r+b") as handle:
                handle.truncate(end)
        last = 0
        for record in read_events(path):
            seq = record.get("seq")
            if isinstance(seq, int):
                last = max(last, seq)
        return last

    @property
    def seq(self) -> int:
        """`seq` of the last logged event (0 before the first)."""
        return self._seq

    def log(self, event: AgentEvent) -> dict[str, Any]:
        """Log one event; set `event.seq` and return the record written to `transcript.jsonl`.

        A `tool_result` with output above the threshold is stored in `blobs/<seq>-<id>.txt`;
        the record then has `content: null` and `blob` set to the path relative to the agent
        folder. The blob is written before the transcript line, so the reference never dangles.
        """
        with self._lock:
            if self._transcript is None or self._raw is None:
                raise ValueError("EventLogger ist geschlossen")
            self._seq += 1
            event.seq = self._seq
            record = event.to_record()
            if event.kind is EventKind.TOOL_RESULT:
                record["data"] = self._offload(event)
            if event.raw is not None:
                raw = {"seq": event.seq, "ts": event.ts, "raw": event.raw}
                self._raw.write(_encode_line(raw, "raw") + "\n")
                self._raw.flush()
            self._transcript.write(_encode_line(record, "data") + "\n")
            self._transcript.flush()
            return record

    def _offload(self, event: AgentEvent) -> dict[str, Any]:
        data = dict(event.data)
        content = data.get("content")
        if content is None:
            return data
        text = content if isinstance(content, str) else _dumps(content)
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self.blob_threshold:
            return data
        name = f"{event.seq:06d}-{_UNSAFE_NAME.sub('_', str(data.get('id', 'tool')))[:60]}.txt"
        self.blobs_dir.mkdir(exist_ok=True)
        with (self.blobs_dir / name).open("wb") as handle:
            handle.write(encoded)
            handle.flush()
        data["content"] = None
        data["blob"] = f"{BLOBS_DIR}/{name}"
        return data

    def close(self) -> None:
        """Close both files; safe to call more than once."""
        with self._lock:
            for handle in (self._transcript, self._raw):
                if handle is not None:
                    handle.close()
            self._transcript = self._raw = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------------------------
# transcript.md
# --------------------------------------------------------------------------------------------

MAX_RESULT_LINES = 40
MAX_RESULT_CHARS = 4_000


def _fence(text: str, lang: str = "") -> str:
    """Wrap `text` in a code fence that is longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.splitlines() or [""])


def _clock(ts: float, tz: tzinfo | None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")


def _tokens(n: int) -> str:
    """Token count as in docs/08 §8.7: `412 k`, `1,9 M` (one decimal below 10 k)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",") + " M"
    if n >= 10_000:
        return f"{round(n / 1_000)} k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".replace(".", ",") + " k"
    return str(n)


def _money(value: float, kind: str | None) -> str:
    """Cost as in docs/08 §8.7: `18,40 $`; estimated (subscription) with `≈`."""
    digits = 2 if value >= 0.01 or value == 0 else 4
    text = f"{value:.{digits}f}".replace(".", ",") + " $"
    return f"≈ {text} (rechnerisch)" if kind == "estimated" else text


def _truncate(text: str) -> str:
    lines = text.splitlines()
    shown = lines[:MAX_RESULT_LINES]
    cut = "\n".join(shown)
    if len(cut) > MAX_RESULT_CHARS:
        cut = cut[:MAX_RESULT_CHARS]
        shown = cut.splitlines()
    omitted = len(lines) - len(shown)
    if omitted > 0 or len(cut) < len(text):
        cut += f"\n… ({max(omitted, 1)} weitere Zeilen nicht gezeigt)"
    return cut


def _usage_text(data: Mapping[str, Any]) -> str:
    usage = data.get("usage") or {}
    parts = []
    for label, key in (("ein", "input"), ("aus", "output"), ("Cache", "cache_read")):
        if isinstance(usage.get(key), int):
            parts.append(f"{label} {_tokens(usage[key])}")
    if isinstance(data.get("cost_usd"), int | float):
        parts.append(_money(float(data["cost_usd"]), data.get("cost_kind")))
    return " · ".join(parts)


def _render_event(rec: Mapping[str, Any], tz: tzinfo | None) -> str | None:
    kind = rec.get("kind")
    data: Mapping[str, Any] = rec.get("data") or {}
    clock = _clock(float(rec.get("ts", 0)), tz)
    match kind:
        case "text":
            return str(data.get("text", ""))
        case "thinking":
            label = "Gedanken (Zusammenfassung)" if data.get("summarized") else "Gedanken"
            return _quote(f"**{label}**\n\n{data.get('text', '')}")
        case "tool_call":
            body = json.dumps(data.get("input", {}), indent=2, ensure_ascii=False, default=str)
            return f"**Werkzeug** `{data.get('name', '?')}` · {clock}\n\n{_fence(body, 'json')}"
        case "tool_result":
            status = "Fehler" if data.get("is_error") else "ok"
            head = f"**Ergebnis** `{data.get('id', '?')}` · {status}"
            if data.get("blob"):
                return f"{head}\n\nAusgabe ausgelagert: `{data['blob']}`"
            content = data.get("content")
            if not content:
                return f"{head}\n\n*(leer)*"
            return f"{head}\n\n{_fence(_truncate(str(content)))}"
        case "rate_limit":
            used = data.get("utilization")
            extra = f", Auslastung {round(used * 100)} %" if isinstance(used, int | float) else ""
            return f"*Rate-Limit: {data.get('status', '?')}{extra}*"
        case "error":
            tail = "behebbar" if data.get("recoverable") else "nicht behebbar"
            return f"**Fehler** ({data.get('type', '?')}, {tail}): {data.get('message', '')}"
        case "hint":
            where = f" aus #{data['channel']}" if data.get("channel") else ""
            return _quote(f"**Hinweis{where}** ({data.get('method', '?')}): {data.get('text', '')}")
        case "usage":
            return None  # token counts show up summed in `turn_end`
        case _:
            return None


def render_transcript(
    records: Iterable[Mapping[str, Any]],
    *,
    agent_id: str | None = None,
    tz: tzinfo | None = None,
) -> str:
    """Render normalized events (records of `transcript.jsonl`) as readable Markdown.

    Thinking is set off as a quote, tool calls and results as labeled code blocks, each turn
    has a heading and a closing line with status, tokens and cost. A turn without `turn_end`
    (crash) is marked. `tz` is the time zone of the time stamps (default: local).
    """
    records = list(records)
    agent = agent_id or (records[0].get("agent") if records else None) or "?"
    out: list[str] = [f"# Transkript – {agent}"]
    if records and records[0].get("run"):
        out.append(f"Run `{records[0]['run']}` · {len(records)} Ereignisse")
    turn: Any = None
    open_turn = False
    for rec in records:
        if rec.get("turn") != turn:
            turn = rec.get("turn")
            open_turn = True
            head = f"## Turn {turn} · {_clock(float(rec.get('ts', 0)), tz)}"
            if rec.get("kind") == "turn_start":
                data = rec.get("data") or {}
                if data.get("stage"):
                    head += f"\n\n**Stufe:** {data['stage']}"
                head += f"\n\n{_quote(str(data.get('prompt', '')))}"
            out.append(head)
            if rec.get("kind") == "turn_start":
                continue
        if rec.get("kind") == "turn_end":
            data = rec.get("data") or {}
            summary = f"Turn {turn} beendet: {data.get('status', '?')}"
            if details := _usage_text(data):
                summary += f" · {details}"
            out.append(f"---\n*{summary}*")
            open_turn = False
            continue
        if rendered := _render_event(rec, tz):
            out.append(rendered)
    if open_turn:
        out.append("---\n*Turn ohne Abschluss: der Lauf wurde unterbrochen.*")
    return "\n\n".join(out) + "\n"


def write_transcript_md(agent_dir: str | Path, *, tz: tzinfo | None = None) -> Path:
    """Render `<agent_dir>/transcript.jsonl` to `<agent_dir>/transcript.md` and return its path."""
    agent_dir = Path(agent_dir)
    records = read_events(agent_dir / TRANSCRIPT_FILE)
    target = agent_dir / TRANSCRIPT_MD_FILE
    text = render_transcript(records, agent_id=agent_dir.name, tz=tz)
    temp = target.with_suffix(".md.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    return target

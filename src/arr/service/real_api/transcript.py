"""Transcripts of agents for the GUI: an index over `transcript.jsonl`, pages, bundling.

A transcript can hold tens of thousands of lines (docs/08 §6.8); the GUI reads pages of at
most 200 entries, the latest ones first (docs/08 §6.9). `TranscriptIndex` therefore reads the
file once per growth: it keeps, per line, the byte offset and the few fields the paging needs
(`seq`, `ts`, `turn`, `kind`, tool ID), parsed with regular expressions instead of full JSON.
Only the lines of a page are then decoded. The file is append-only, so a grown file costs
only its new bytes; a shrunken one (truncated, replaced) is indexed afresh. A torn last line
(crash, write in progress) waits until its newline arrives.

`tool_call` + `tool_result` become one entry `tool` (docs/08 §5.0 `TranscriptEvent`), so the
page size counts bundled entries. `to_event` maps one line (with its result) to the API model.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arr.backends.base import Usage
from arr.runs.eventlog import RATE_LIMIT_ALLOWED
from arr.service.api_models import (
    ToolBundle,
    ToolStatus,
    TranscriptEvent,
    TranscriptKind,
    TranscriptPage,
    TurnRange,
)

PAGE_LIMIT = 200
"""Largest page of a transcript (docs/08 §6.9)."""
EXCERPT_LINES = 10
"""Lines of a tool output shown in the GUI (docs/08 §5.0 `output_excerpt`)."""
SUMMARY_CHARS = 120

_SEQ = re.compile(rb'"seq":\s*(\d+)')
_TS = re.compile(rb'"ts":\s*([0-9.eE+-]+)')
_TURN = re.compile(rb'"turn":\s*(\d+)')
_KIND = re.compile(rb'"kind":\s*"([a-z_]+)"')
_ID = re.compile(rb'"id":\s*"((?:[^"\\]|\\.)*)"')
_POST = re.compile(rb'"name":\s*"mcp__arr-room__post_message"')

TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"


@dataclass(slots=True)
class Line:
    """One line of `transcript.jsonl` as the index keeps it."""

    offset: int
    length: int
    seq: int
    ts: float
    turn: int
    kind: str
    tool_id: str | None
    posts: bool


class TranscriptIndex:
    """Index over one agent's `transcript.jsonl` (see the module docstring)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lines: list[Line] = []
        self._end = 0
        self._rest = b""
        self._results: dict[str, int] = {}
        self._calls: dict[str, int] = {}
        self._turn_ends: list[int] = []
        self._usage_cache: tuple[int, Usage, int] | None = None

    # -- building ---------------------------------------------------------------------------

    def _reset(self) -> None:
        self.lines = []
        self._end = 0
        self._rest = b""
        self._results = {}
        self._calls = {}
        self._turn_ends = []
        self._usage_cache = None

    def refresh(self) -> list[Line]:
        """Read what the file gained since the last call; returns the new complete lines."""
        try:
            size = self.path.stat().st_size
        except OSError:
            if self.lines or self._rest:
                self._reset()
            return []
        if size < self._end:
            self._reset()
        if size == self._end:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._end)
                chunk = handle.read(size - self._end)
        except OSError:
            return []
        cursor = self._end - len(self._rest)
        self._end += len(chunk)
        data = self._rest + chunk
        *parts, self._rest = data.split(b"\n")
        new: list[Line] = []
        for raw in parts:
            length = len(raw)
            offset = cursor
            cursor += length + 1
            line = self._parse(raw, offset, length)
            if line is None:
                continue
            index = len(self.lines)
            self.lines.append(line)
            new.append(line)
            if line.kind == TOOL_CALL and line.tool_id is not None:
                self._calls.setdefault(line.tool_id, index)
            elif line.kind == TOOL_RESULT and line.tool_id is not None:
                self._results.setdefault(line.tool_id, index)
            elif line.kind == "turn_end":
                self._turn_ends.append(index)
        return new

    @staticmethod
    def _parse(raw: bytes, offset: int, length: int) -> Line | None:
        if not raw.strip():
            return None
        seq = _SEQ.search(raw)
        kind = _KIND.search(raw)
        if seq is None or kind is None:
            return None
        ts = _TS.search(raw)
        turn = _TURN.search(raw)
        kind_text = kind.group(1).decode("ascii")
        tool_id = None
        if kind_text in (TOOL_CALL, TOOL_RESULT):
            # `data.id` is the first "id" in the line (the record itself has none before it)
            found = _ID.search(raw, kind.end())
            if found is not None:
                try:
                    tool_id = json.loads(b'"' + found.group(1) + b'"')
                except ValueError:
                    tool_id = found.group(1).decode("utf-8", errors="replace")
        try:
            ts_value = float(ts.group(1)) if ts else 0.0
        except ValueError:
            ts_value = 0.0
        return Line(
            offset=offset,
            length=length,
            seq=int(seq.group(1)),
            ts=ts_value,
            turn=int(turn.group(1)) if turn else 0,
            kind=kind_text,
            tool_id=tool_id,
            posts=kind_text == TOOL_CALL and _POST.search(raw) is not None,
        )

    # -- reading ----------------------------------------------------------------------------

    @property
    def entry_indexes(self) -> list[int]:
        """Indexes of the lines that are entries of their own (everything but results)."""
        return [i for i, line in enumerate(self.lines) if line.kind != TOOL_RESULT]

    def total(self) -> int:
        """Number of bundled entries."""
        return sum(1 for line in self.lines if line.kind != TOOL_RESULT)

    def result_index(self, line: Line) -> int | None:
        """Index of the `tool_result` line of a `tool_call`, if it has arrived."""
        if line.kind != TOOL_CALL or line.tool_id is None:
            return None
        return self._results.get(line.tool_id)

    def call_index(self, line: Line) -> int | None:
        """Index of the `tool_call` line of a `tool_result`."""
        if line.kind != TOOL_RESULT or line.tool_id is None:
            return None
        return self._calls.get(line.tool_id)

    def read(self, indexes: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Decode the lines with these indexes (unreadable or damaged ones are left out)."""
        wanted = sorted(set(indexes))
        out: dict[int, dict[str, Any]] = {}
        if not wanted:
            return out
        try:
            with self.path.open("rb") as handle:
                for index in wanted:
                    line = self.lines[index]
                    handle.seek(line.offset)
                    raw = handle.read(line.length)
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        out[index] = record
        except OSError:
            pass
        return out

    def last_line(self) -> Line | None:
        return self.lines[-1] if self.lines else None

    def max_turn(self) -> int:
        return max((line.turn for line in self.lines), default=0)

    def turn_ends(self) -> list[dict[str, Any]]:
        """The decoded `turn_end` records (one per finished turn)."""
        records = self.read(self._turn_ends)
        return [records[i] for i in self._turn_ends if i in records]

    def usage(self) -> tuple[Usage, int]:
        """Summed usage of all finished turns and their number (cached per file growth)."""
        key = len(self.lines)
        if self._usage_cache is not None and self._usage_cache[0] == key:
            return self._usage_cache[1], self._usage_cache[2]
        total = Usage()
        turns = 0
        for record in self.turn_ends():
            data = record.get("data") or {}
            if not isinstance(data, Mapping):
                continue
            tokens = {
                k: v
                for k, v in dict(data.get("usage") or {}).items()
                if k in ("input", "output", "cache_read", "cache_write", "reasoning")
                and isinstance(v, int)
                and not isinstance(v, bool)
                and v >= 0
            }
            cost = data.get("cost_usd")
            kind = data.get("cost_kind")
            try:
                part = Usage(
                    **tokens,
                    cost_usd=float(cost) if isinstance(cost, int | float) and cost >= 0 else None,
                    cost_kind=kind if kind in ("billed", "estimated") else None,
                )
            except ValueError:
                continue
            total = total + part
            turns += 1
        self._usage_cache = (key, total, turns)
        return total, turns

    # -- pages ------------------------------------------------------------------------------

    def page_indexes(self, from_seq: int | None, limit: int) -> tuple[list[int], int | None]:
        """Indexes of the entries of a page and the `seq` of the entry after it."""
        entries = self.entry_indexes
        if from_seq is None:
            chosen = entries[-limit:] if limit else []
            return chosen, None
        start = 0
        while start < len(entries) and self.lines[entries[start]].seq < from_seq:
            start += 1
        chosen = entries[start : start + limit]
        rest = entries[start + limit :]
        return chosen, (self.lines[rest[0]].seq if rest else None)

    def older(self, before_index: int | None) -> TurnRange | None:
        """Summary of the entries before `before_index` (docs/08 §5.0 `older`, G5)."""
        if before_index is None or before_index <= 0:
            return None
        lines = self.lines[:before_index]
        entries = [line for line in lines if line.kind != TOOL_RESULT]
        if not entries:
            return None
        turns = [line.turn for line in entries if line.turn > 0]
        return TurnRange(
            turn_from=min(turns) if turns else 1,
            turn_to=max(turns) if turns else 1,
            start_ts=entries[0].ts,
            end_ts=entries[-1].ts,
            events=len(entries),
            tool_calls=sum(1 for line in entries if line.kind == TOOL_CALL),
            messages_posted=sum(1 for line in entries if line.posts),
        )

    def page(
        self,
        agent_id: str,
        *,
        from_seq: int | None,
        limit: int,
        kinds: set[TranscriptKind] | None,
        run_dir: Path | None = None,
    ) -> TranscriptPage:
        """A page of bundled entries, oldest first (without `from_seq` the latest `limit`)."""
        limit = max(1, min(limit, PAGE_LIMIT))
        if kinds is None:
            indexes, next_seq = self.page_indexes(from_seq, limit)
        else:
            indexes, next_seq = self._filtered_page(from_seq, limit, kinds)
        results = [r for r in (self.result_index(self.lines[i]) for i in indexes) if r is not None]
        records = self.read([*indexes, *results])
        items: list[TranscriptEvent] = []
        for index in indexes:
            record = records.get(index)
            if record is None:
                continue
            result_index = self.result_index(self.lines[index])
            result = records.get(result_index) if result_index is not None else None
            event = to_event(record, result, run_dir=run_dir, agent_id=agent_id)
            if event is not None:
                items.append(event)
        return TranscriptPage(
            agent_id=agent_id,
            items=items,
            total=self.total(),
            next_from_seq=next_seq,
            older=self.older(indexes[0]) if indexes else None,
        )

    def _filtered_page(
        self, from_seq: int | None, limit: int, kinds: set[TranscriptKind]
    ) -> tuple[list[int], int | None]:
        wanted = {k.value for k in kinds}
        if TranscriptKind.TOOL in kinds:
            wanted.add(TOOL_CALL)
        entries = [i for i in self.entry_indexes if self.lines[i].kind in wanted]
        if from_seq is None:
            return entries[-limit:], None
        start = 0
        while start < len(entries) and self.lines[entries[start]].seq < from_seq:
            start += 1
        chosen = entries[start : start + limit]
        rest = entries[start + limit :]
        return chosen, (self.lines[rest[0]].seq if rest else None)


# --------------------------------------------------------------------------------------------
# Mapping of records to `TranscriptEvent`
# --------------------------------------------------------------------------------------------


def _text(value: Any, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def input_summary(name: str, tool_input: Mapping[str, Any]) -> str:
    """The short argument of a tool call for the one-line view (docs/08 K6)."""
    for key in ("command", "file_path", "path", "pattern", "url", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return _text(value)
    if name.startswith("mcp__arr-room__"):
        parts = []
        for key in ("channel", "action", "status", "kind", "title", "body"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                parts.append(f"#{value}" if key == "channel" else _text(value, 60))
        if parts:
            return " · ".join(parts)
    for value in tool_input.values():
        if isinstance(value, str) and value:
            return _text(value)
    try:
        return _text(json.dumps(tool_input, ensure_ascii=False)) if tool_input else ""
    except (TypeError, ValueError):
        return ""


def _excerpt(content: Any) -> tuple[str | None, int | None]:
    if content is None:
        return None, None
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    lines = text.splitlines() or ([""] if text else [])
    shown = "\n".join(lines[:EXCERPT_LINES])
    if len(shown) > 4000:
        shown = shown[:4000]
    return shown, len(lines)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def to_event(
    record: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    *,
    run_dir: Path | None,
    agent_id: str,
) -> TranscriptEvent | None:
    """One line of `transcript.jsonl` as `TranscriptEvent`; `tool_result` lines give None, and
    so does a `rate_limit` with `allowed`, which changes nothing (Nachtest V5). `allowed_warning`
    is served: it throttles the profile, the GUI shows it as a hint (W3-09.1)."""
    kind = str(record.get("kind") or "")
    if kind == TOOL_RESULT:
        return None
    data = record.get("data")
    data = dict(data) if isinstance(data, Mapping) else {}
    seq = record.get("seq")
    turn = record.get("turn")
    ts = _number(record.get("ts")) or 0.0
    base = {
        "seq": seq if isinstance(seq, int) and seq >= 0 else 0,
        "ts": ts,
        "turn": turn if isinstance(turn, int) and turn >= 0 else 0,
    }
    if kind == TOOL_CALL:
        name = str(data.get("name") or "?")
        tool_input = data.get("input") if isinstance(data.get("input"), Mapping) else {}
        tool_id = str(data.get("id") or f"tc_{base['seq']}")
        status = ToolStatus.RUNNING
        duration_ms = None
        excerpt: str | None = None
        lines: int | None = None
        blob_path: str | None = None
        if result is not None:
            rdata = result.get("data") if isinstance(result.get("data"), Mapping) else {}
            status = ToolStatus.ERROR if rdata.get("is_error") else ToolStatus.OK
            result_ts = _number(result.get("ts"))
            if result_ts is not None and ts:
                duration_ms = max(0, int((result_ts - ts) * 1000))
            excerpt, lines = _excerpt(rdata.get("content"))
            blob = rdata.get("blob")
            if isinstance(blob, str) and blob:
                blob_path = f"agents/{agent_id}/{blob}"
        return TranscriptEvent(
            **base,
            kind=TranscriptKind.TOOL,
            tool=ToolBundle(
                id=tool_id,
                name=name,
                input_summary=input_summary(name, tool_input),
                input=dict(tool_input),
                status=status,
                duration_ms=duration_ms,
                output_excerpt=excerpt,
                output_lines=lines,
                blob_path=blob_path,
            ),
        )
    try:
        api_kind = TranscriptKind(kind)
    except ValueError:
        return None
    status = str(data.get("status") or "")
    if api_kind is TranscriptKind.RATE_LIMIT and status in RATE_LIMIT_ALLOWED:
        return None  # V5: the window was reported, nothing changed
    return TranscriptEvent(**base, kind=api_kind, data=data)

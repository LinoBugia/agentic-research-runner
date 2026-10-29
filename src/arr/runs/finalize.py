"""Finalize a run: exports, diffs against the base snapshot, summary.md and usage.json.

`finalize_run(prepared)` is the `Finalizer` of the RunManager (W2-04). It runs in
`finalizing`, after backends and event loggers are closed, also after a stop, a limit, an
error or a half finished setup, and sets no status. Steps, each on its own (a failing step is
logged as `error` with `where: "finalizer"` in `orchestrator.jsonl`, named in `summary.md` and
does not stop the others):

1. `groupchats/<kanal>.md` and `.jsonl` per channel.
2. `agents/<id>/transcript.md` per agent.
3. `agents/<id>/changes.diff`: the working folder against `base/` (empty base for `empty`
   workspaces). Members of a shared folder `shared/<name>/` get the same diff. Text files as
   unified diff, binary and very large files as one line, symlinks with their target; files
   written by arr (`AgentFile.generated_paths`) are left out. Symlinks are never followed.
   A link in `base/` to a file excluded by `copy_exclude` is broken and therefore missing in
   the copy; it is left out with a hint in `summary.md`.
4. `output/results/NNN-<agent>-<kind>.md` per result (unless the submitted file has that
   name), `output/results.json`, `output/ratings.json`.
5. `usage.json`, and an empty `approvals.jsonl` if there were no requests.
6. `output/summary.md` (German): status and reason, duration, agents, stages with attempts and
   scores, results with links, a summarizer's summary, usage sums, hints.

Running it again overwrites every file with the same content (idempotent).
See docs/02-workfolder-und-runs.md §4, §7.
"""

import asyncio
import difflib
import json
import logging
import os
import stat
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from arr.models import RunStatus, WorkspaceMode
from arr.room.store import RESULTS_DIR, ROOM_DIR, RoomStore
from arr.room.types import Rating, Result
from arr.runs.eventlog import write_transcript_md
from arr.runs.orchestrator import ORCHESTRATOR_LOG, OrchestratorLog
from arr.runs.prepare import AgentFile, PreparedRun, RunMeta, load_run_meta
from arr.usage import RunUsage, UsageTotal, compute_run_usage, write_usage_json

__all__ = ["DiffResult", "changes_diff", "finalize_run"]

logger = logging.getLogger(__name__)

DIFF_FILE = "changes.diff"
APPROVALS_FILE = "approvals.jsonl"
SUMMARY_FILE = "output/summary.md"
RESULTS_JSON = "output/results.json"
RATINGS_JSON = "output/ratings.json"
SUMMARY_KIND = "summary"
MAX_DIFF_BYTES = 256 * 1024
"""Files larger than this are listed as changed without a line diff (difflib is slow)."""
MAX_DIFF_LINES = 10_000
"""Files with more lines than this (on either side) get no line diff either."""
_SNIFF_BYTES = 8192

_STATUS_TEXT = {
    RunStatus.DONE: "abgeschlossen",
    RunStatus.STOPPED: "gestoppt",
    RunStatus.FAILED: "fehlgeschlagen",
    RunStatus.INTERRUPTED: "unterbrochen",
}
_LIMIT_TEXT = {
    "wall_time": "Die Gesamtlaufzeit des Runs ist erreicht.",
    "turns_per_agent": "Die Zahl der Turns pro Agent ist erreicht.",
    "budget_usd": "Das Budget des Runs ist erreicht.",
}
_DECISION_TEXT = {
    "passed": "bestanden",
    "rework": "Nacharbeit",
    "failed": "nicht bestanden",
    "escalated": "eskaliert",
}
_OUTCOME_TEXT = {
    "passed": "bestanden",
    "best_accepted": "bester Versuch übernommen",
    "stage_failed": "nicht bestanden, Run beendet",
}
_REASON_TEXT = {"no_result": "kein Ergebnis", "no_rating": "kein Rating"}


async def finalize_run(prepared: PreparedRun) -> None:
    """Write the finalizer's files into the run folder (see module docstring); never raises."""
    await asyncio.to_thread(_Finalizer(prepared).run)


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    """Write atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _read_log(run_dir: Path) -> list[dict[str, Any]]:
    """Entries of `orchestrator.jsonl`; damaged lines are skipped."""
    path = run_dir / ORCHESTRATOR_LOG
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            entries.append(entry)
    return entries


def _number(value: float | None) -> str:
    """German number with up to two decimals, `–` for None."""
    if value is None:
        return "–"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S") if ts else "–"


def _duration(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _safe_kind(kind: str) -> str:
    """Same file-name rule as `AgentRoom.submit_result`."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in kind)


def result_file(result: Result) -> str:
    """`output/results/NNN-<agent>-<kind>.md`, relative to the run folder."""
    return f"{RESULTS_DIR}/{result.id:03d}-{result.agent}-{_safe_kind(result.kind)}.md"


# --------------------------------------------------------------------------------------------
# changes.diff
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DiffResult:
    """Text of a `changes.diff` and the hints for `summary.md` (left-out links)."""

    text: str
    hints: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Entry:
    kind: str  # "file" | "link" | "unreadable_dir" | "unreadable"
    path: Path
    target: str | None = None


_UNREADABLE = ("unreadable_dir", "unreadable")


def _excluded(rel: str, exclude: Sequence[str]) -> bool:
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in exclude)


def _scan(root: Path | None, exclude: Sequence[str]) -> dict[str, _Entry]:
    """Files and symlinks below `root` by POSIX path, without following any link.

    A folder or entry that cannot be read is recorded as `unreadable_dir` / `unreadable`
    (the root as `.`) instead of raising.
    """
    found: dict[str, _Entry] = {}
    if root is None or not root.is_dir() or root.is_symlink():
        return found

    def walk(folder: Path, rel: str) -> None:
        try:
            with os.scandir(folder) as scan:
                entries = sorted(scan, key=lambda e: e.name)
        except OSError:
            found[rel or "."] = _Entry("unreadable_dir", folder)
            return
        for entry in entries:
            rel_path = f"{rel}/{entry.name}" if rel else entry.name
            if _excluded(rel_path, exclude):
                continue
            try:
                if entry.is_symlink():
                    found[rel_path] = _Entry("link", Path(entry.path), os.readlink(entry.path))
                elif entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), rel_path)
                elif entry.is_file(follow_symlinks=False):
                    found[rel_path] = _Entry("file", Path(entry.path))
            except OSError:
                found[rel_path] = _Entry("unreadable", Path(entry.path))

    walk(root, "")
    return found


def _read(path: Path) -> tuple[bytes, int]:
    """First `MAX_DIFF_BYTES + 1` bytes of a regular file (never through a link) and its size."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return b"", 0
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(MAX_DIFF_BYTES + 1), info.st_size
    finally:
        os.close(fd)


def _text(data: bytes) -> str | None:
    """The content as text, None for binary data (NUL byte or not UTF-8)."""
    if b"\0" in data[:_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _lines(text: str) -> list[str]:
    return [line if line.endswith("\n") else line + "\n" for line in text.splitlines(True)]


def _file_diff(rel: str, old: _Entry | None, new: _Entry | None) -> str:
    """Diff of one regular file; `None` side = file did not exist."""
    try:
        return _readable_file_diff(rel, old, new)
    except OSError:
        return f"Datei nicht lesbar: {rel}\n"


def _readable_file_diff(rel: str, old: _Entry | None, new: _Entry | None) -> str:
    old_data, old_size = _read(old.path) if old else (b"", 0)
    new_data, new_size = _read(new.path) if new else (b"", 0)
    if (
        old
        and new
        and old_size == new_size
        and old_data == new_data
        and (old_size <= MAX_DIFF_BYTES or _same_file(old.path, new.path))
    ):
        return ""
    what = "geändert" if old and new else ("neu" if new else "gelöscht")
    if max(old_size, new_size) > MAX_DIFF_BYTES:
        return f"Datei {what} (zu groß für einen Zeilen-Diff): {rel}\n"
    old_text = _text(old_data) if old else ""
    new_text = _text(new_data) if new else ""
    if old_text is None or new_text is None:
        return f"Binärdatei {what}: {rel}\n"
    old_lines, new_lines = _lines(old_text), _lines(new_text)
    if max(len(old_lines), len(new_lines)) > MAX_DIFF_LINES:
        return f"Datei {what} (zu groß für einen Zeilen-Diff): {rel}\n"
    diff = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel}" if old else "/dev/null",
            tofile=f"b/{rel}" if new else "/dev/null",
        )
    )
    if not diff:  # only the line end at the end of the file differs
        return f"--- a/{rel}\n+++ b/{rel}\nNur das Zeilenende am Dateiende hat sich geändert.\n"
    return diff


def _same_file(a: Path, b: Path) -> bool:
    """Byte-wise comparison of two large files in chunks."""
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            chunk_a, chunk_b = fa.read(1 << 20), fb.read(1 << 20)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


def changes_diff(base: Path | None, work: Path, exclude: Sequence[str] = ()) -> DiffResult:
    """Changes of `work` against `base` (None: against an empty folder), see module docstring.

    `exclude` are paths relative to `work` (files or folders) left out on both sides.
    """
    old = _scan(base, exclude)
    new = _scan(work, exclude)
    parts: list[str] = []
    hints: list[str] = []
    unreadable = sorted(
        rel for side in (old, new) for rel, e in side.items() if e.kind == "unreadable_dir"
    )
    for rel in sorted(old.keys() | new.keys()):
        if any(rel != d and (d == "." or rel.startswith(d + "/")) for d in unreadable):
            continue  # inside a folder one side cannot read: no false "new"/"deleted"
        a, b = old.get(rel), new.get(rel)
        if (a and a.kind in _UNREADABLE) or (b and b.kind in _UNREADABLE):
            what = "Ordner" if "unreadable_dir" in (a and a.kind, b and b.kind) else "Datei"
            parts.append(f"{what} nicht lesbar: {rel}\n")
            continue
        if a and a.kind == "link" and b is None and not os.path.exists(a.path):
            hints.append(
                f"Der Symlink „{rel}“ zeigt in base/ auf eine durch „copy_exclude“ ausgeschlossene "
                "oder fehlende Datei und fehlt deshalb im Arbeitsordner. Er steht nicht in "
                "changes.diff."
            )
            continue
        if a and b and a.kind == b.kind == "link":
            if a.target != b.target:
                parts.append(f"Symlink geändert: {rel} -> {b.target} (vorher {a.target})\n")
            continue
        if a and a.kind == "link":
            parts.append(f"Symlink gelöscht: {rel} (-> {a.target})\n")
            a = None
        if b and b.kind == "link":
            parts.append(f"Symlink neu: {rel} -> {b.target}\n")
            b = None
        if a or b:
            parts.append(_file_diff(rel, a, b))
    return DiffResult("".join(parts), hints)


# --------------------------------------------------------------------------------------------
# Finalizer
# --------------------------------------------------------------------------------------------


class _Finalizer:
    def __init__(self, prepared: PreparedRun) -> None:
        self.prepared = prepared
        self.run_dir = prepared.run_dir
        self.failures: list[str] = []
        self.hints: list[str] = []
        self.room: RoomStore | None = None
        self.results: list[Result] = []
        self.ratings: list[Rating] = []
        self.usage: RunUsage | None = None

    def run(self) -> None:
        steps: list[tuple[str, Callable[[], None]]] = [
            ("Gruppenchat-Speicher", self._open_room),
            ("Kanal-Exporte", self._exports),
            ("Transkripte", self._transcripts),
            ("changes.diff", self._diffs),
            ("Ergebnisse und Ratings", self._results),
            ("usage.json", self._usage),
            ("approvals.jsonl", self._approvals),
        ]
        for label, step in steps:
            self._step(label, step)
        try:
            _write_text(self.run_dir / SUMMARY_FILE, self._summary())
        except Exception as exc:
            logger.exception("summary.md von %s fehlgeschlagen", self.prepared.run_id)
            self._log_failure("summary.md", exc)

    def _step(self, label: str, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception as exc:
            logger.exception("Finalizer-Schritt „%s“ von %s fehlgeschlagen", label, self.run_id)
            self.failures.append(f"{label}: {type(exc).__name__}: {exc}")
            self._log_failure(label, exc)

    @property
    def run_id(self) -> str:
        return self.prepared.run_id

    def _log_failure(self, label: str, exc: Exception) -> None:
        try:
            OrchestratorLog(self.run_dir).write(
                "error", message=str(exc), type=type(exc).__name__, where="finalizer", step=label
            )
        except OSError:
            logger.exception("orchestrator.jsonl von %s nicht beschreibbar", self.run_id)

    # -- steps ------------------------------------------------------------------------------

    def _open_room(self) -> None:
        if not (self.run_dir / ROOM_DIR / "room.sqlite").is_file():
            if self.prepared.resolved.groupchats:
                self.hints.append(
                    "Die Gruppenchat-Datenbank fehlt (der Run wurde nicht fertig vorbereitet). "
                    "Es gibt keine Kanal-Exporte, Ergebnisse und Ratings."
                )
            return
        self.room = RoomStore(self.run_dir)
        self.results = self.room.results()
        self.ratings = self.room.ratings()

    def _exports(self) -> None:
        if self.room is not None:
            self.room.export_all()

    def _transcripts(self) -> None:
        for agent in self.prepared.agents.values():
            if agent.agent_dir.is_dir():
                write_transcript_md(agent.agent_dir)

    def _diffs(self) -> None:
        cache: dict[Path, DiffResult] = {}
        for agent in self.prepared.agents.values():
            work = agent.work_dir
            if work not in cache:
                cache[work] = self._diff_of(agent.file, work)
            if agent.agent_dir.is_dir():
                _write_text(agent.agent_dir / DIFF_FILE, cache[work].text)

    def _diff_of(self, file: AgentFile, work: Path) -> DiffResult:
        """The diff of one working folder; an error stays with this folder."""
        empty = file.config.workspace.mode is WorkspaceMode.EMPTY
        base = None if empty else self.run_dir / "base"
        try:
            result = changes_diff(base, work, file.generated_paths)
        except Exception as exc:
            logger.exception("changes.diff für %s fehlgeschlagen", file.work_dir)
            self.failures.append(f"changes.diff für {file.work_dir}: {type(exc).__name__}")
            self._log_failure(f"changes.diff für {file.work_dir}", exc)
            return DiffResult(f"Änderungen nicht ermittelbar ({type(exc).__name__}).\n")
        self.hints += result.hints
        return result

    def _results(self) -> None:
        by_result: dict[int, list[Rating]] = {}
        for rating in self.ratings:
            by_result.setdefault(rating.result_id, []).append(rating)
        entries = []
        for result in self.results:
            ratings = by_result.get(result.id, [])
            target = result_file(result)
            if result.path != target:
                _write_text(self.run_dir / target, _result_markdown(result, ratings))
            entries.append(
                {
                    **result.model_dump(mode="json"),
                    "file": target,
                    "score": fmean(r.score for r in ratings) if ratings else None,
                    "ratings": [r.id for r in ratings],
                }
            )
        _write_json(
            self.run_dir / RESULTS_JSON, {"version": 1, "run": self.run_id, "results": entries}
        )
        _write_json(
            self.run_dir / RATINGS_JSON,
            {
                "version": 1,
                "run": self.run_id,
                "ratings": [r.model_dump(mode="json") for r in self.ratings],
            },
        )

    def _approvals(self) -> None:
        """An empty `approvals.jsonl` when nobody asked for anything (docs/02 §4)."""
        (self.run_dir / APPROVALS_FILE).touch(exist_ok=True)

    def _usage(self) -> None:
        configs = [a.file.config for a in self.prepared.agents.values()]
        write_usage_json(self.run_dir, configs, run_id=self.run_id)
        self.usage = compute_run_usage(self.run_dir, configs, run_id=self.run_id)

    # -- summary.md -------------------------------------------------------------------------

    def _summary(self) -> str:
        entries = _read_log(self.run_dir)
        meta = self._meta()
        resolved = self.prepared.resolved
        status, reason = _status_and_reason(meta, entries)
        lines = [f"# Zusammenfassung – {self.run_id}", ""]
        if resolved.name:
            lines += [f"**{resolved.name}**", ""]
        lines.append(f"- **Status:** {_STATUS_TEXT.get(status, status.value)} ({status.value})")
        lines.append(f"- **Grund:** {reason}")
        lines.append(f"- **Projekt:** {resolved.project}")
        what = resolved.skill or "eigene Gruppe"
        lines.append(f"- **Topologie:** {resolved.topology.kind.value} ({what})")
        lines += self._times(meta, entries)
        lines += ["", "## Agenten", ""]
        for agent in resolved.agents:
            role = f", Rolle {Path(agent.role).stem}" if agent.role else ""
            lines.append(f"- `{agent.id}`: {agent.provider.value}, {agent.model}{role}")
        lines += self._stages(entries)
        lines += self._results_section(entries)
        lines += self._summarizer()
        lines += self._usage_section()
        lines += self._hints(entries)
        return "\n".join(lines).rstrip("\n") + "\n"

    def _meta(self) -> RunMeta:
        try:
            return load_run_meta(self.run_dir)
        except (OSError, ValueError):
            return self.prepared.meta

    @staticmethod
    def _times(meta: RunMeta, entries: Sequence[Mapping[str, Any]]) -> list[str]:
        if meta.started is None:
            return ["- **Dauer:** nicht gestartet"]
        # the run's work ends with `finalizing`: the same duration on every call (idempotent)
        finalizing = [
            e["ts"]
            for e in entries
            if e["kind"] == "status" and e["data"].get("status") == RunStatus.FINALIZING
        ]
        end = finalizing[0] if finalizing else meta.ended
        if end is None:
            end = entries[-1]["ts"] if entries else time.time()
        return [
            f"- **Beginn:** {_clock(meta.started)}",
            f"- **Dauer:** {_duration(end - meta.started)}",
        ]

    @staticmethod
    def _stages(entries: Sequence[Mapping[str, Any]]) -> list[str]:
        rows = []
        ends = []
        for entry in entries:
            data = entry["data"]
            if entry["kind"] != "checkpoint":
                continue
            if "decision" in data:
                decision = _DECISION_TEXT.get(data["decision"], str(data["decision"]))
                if data.get("reason") in _REASON_TEXT:
                    decision += f" ({_REASON_TEXT[data['reason']]})"
                result = f"#{data['result']}" if data.get("result") else "–"
                if data.get("results"):
                    result = ", ".join(f"#{r}" for r in data["results"])
                rows.append(
                    f"| {data.get('stage')} | {data.get('attempt', '–')} | "
                    f"{data.get('agent', '–')} | {result} | {_number(data.get('score'))} | "
                    f"{decision} |"
                )
            elif data.get("phase") == "stage_end":
                accepted = f"#{data['accepted']}" if data.get("accepted") else "keins"
                outcome = _OUTCOME_TEXT.get(data.get("outcome"), str(data.get("outcome")))
                ends.append(
                    f"- **{data.get('stage')}:** {outcome}, akzeptiert: {accepted}, Score "
                    f"{_number(data.get('score'))}, "
                    f"{_count(data.get('attempts') or 0, 'Versuch', 'Versuche')}"
                )
        if not rows and not ends:
            return []
        lines = ["", "## Stufen", ""]
        if rows:
            lines += [
                "| Stufe | Versuch | Agent | Ergebnis | Score | Entscheidung |",
                "|---|---|---|---|---|---|",
                *rows,
            ]
        if ends:
            lines += ["", *ends] if rows else ends
        return lines

    def _results_section(self, entries: Sequence[Mapping[str, Any]]) -> list[str]:
        lines = ["", "## Ergebnisse", ""]
        if not self.results:
            return [*lines, "Keine Ergebnisse abgegeben."]
        accepted = {
            e["data"].get("accepted")
            for e in entries
            if e["kind"] == "checkpoint" and e["data"].get("phase") == "stage_end"
        }
        scores: dict[int, list[float]] = {}
        for rating in self.ratings:
            scores.setdefault(rating.result_id, []).append(rating.score)
        for result in self.results:
            link = Path(result_file(result)).relative_to("output").as_posix()
            text = f"- [#{result.id} {result.title}]({link}) · `{result.agent}` · {result.kind}"
            if result.stage:
                text += f" · Stufe {result.stage}, Versuch {result.attempt}"
            if result.id in scores:
                values = scores[result.id]
                text += (
                    f" · Ø {_number(fmean(values))} ({_count(len(values), 'Rating', 'Ratings')})"
                )
            if result.id in accepted:
                text += " · ✓ akzeptiert"
            if result.path and result.path != result_file(result):
                text += f" · Datei: [{Path(result.path).name}](../{result.path})"
            lines.append(text)
        return lines

    def _summarizer(self) -> list[str]:
        summaries = [r for r in self.results if r.kind == SUMMARY_KIND]
        if not summaries:
            return []
        last = summaries[-1]
        body = last.body or f"Siehe [{Path(str(last.path)).name}](../{last.path})."
        return ["", f"## Zusammenfassung von {last.agent}", "", f"**{last.title}**", "", body]

    def _usage_section(self) -> list[str]:
        if self.usage is None:
            return []
        lines = ["", "## Usage", "", _usage_line("Gesamt", self.usage.total)]
        lines += [_usage_line(f"`{agent}`", total) for agent, total in self.usage.by_agent.items()]
        return lines

    def _hints(self, entries: Sequence[Mapping[str, Any]]) -> list[str]:
        logged = [
            str(e["data"].get("message"))
            for e in entries
            if e["data"].get("message")
            and (
                e["kind"] == "warning"
                or (e["kind"] == "error" and e["data"].get("where") == "topology")
            )
        ]
        hints = [*dict.fromkeys([*logged, *self.hints])]
        failures = [f"Schritt des Finalizers gescheitert: {f}" for f in self.failures]
        if not hints and not failures:
            return []
        return ["", "## Hinweise", "", *(f"- {h}" for h in [*failures, *hints])]


def _usage_line(label: str, total: UsageTotal) -> str:
    usage = total.usage
    parts = [
        _count(total.turns, "Turn", "Turns"),
        f"{usage.input or 0} Tokens ein",
        f"{usage.output or 0} aus",
    ]
    if total.cost_billed_usd:
        parts.append(f"{_number(total.cost_billed_usd)} $ abgerechnet")
    if total.cost_estimated_usd:
        parts.append(f"≈ {_number(total.cost_estimated_usd)} $ rechnerisch")
    if total.cost_unknown_turns:
        parts.append(_count(total.cost_unknown_turns, "Turn", "Turns") + " ohne Kosten")
    return f"- {label}: " + ", ".join(parts)


def _result_markdown(result: Result, ratings: Iterable[Rating]) -> str:
    lines = [f"# {result.title}", ""]
    meta = [f"Ergebnis #{result.id}", f"`{result.agent}`", result.kind]
    if result.stage:
        meta.append(f"Stufe {result.stage}, Versuch {result.attempt}")
    meta.append(_clock(result.ts))
    lines += [" · ".join(meta), ""]
    if result.path:
        name = Path(result.path).name
        lines += [f"Datei: [{name}]({name})", ""]
    if result.body:
        lines += [result.body, ""]
    ratings = list(ratings)
    if ratings:
        lines += ["## Ratings", ""]
        for rating in ratings:
            text = f"- `{rating.rater}`: {_number(rating.score)}"
            if rating.criteria:
                text += (
                    " (" + ", ".join(f"{k} {_number(v)}" for k, v in rating.criteria.items()) + ")"
                )
            if rating.comment:
                text += f" – {rating.comment}"
            lines.append(text)
    return "\n".join(lines).rstrip("\n") + "\n"


def _status_and_reason(
    meta: RunMeta, entries: Sequence[Mapping[str, Any]]
) -> tuple[RunStatus, str]:
    """Final status as the RunManager will set it, and the reason in German."""
    errors = [
        e["data"]
        for e in entries
        if e["kind"] == "error" and e["data"].get("where") not in ("finalizer", "topology")
    ]
    stops = [e["data"] for e in entries if e["kind"] == "stop"]
    limits = [e["data"] for e in entries if e["kind"] == "limit"]
    ran = any(
        e["kind"] == "status" and e["data"].get("status") == RunStatus.RUNNING for e in entries
    )
    failed_stage = next(
        (
            e["data"].get("stage")
            for e in entries
            if e["kind"] == "checkpoint" and e["data"].get("reason") == "stage_failed"
        ),
        None,
    )
    if meta.status.is_final:
        status = meta.status
    elif stops or (not ran and not errors):
        status = RunStatus.STOPPED
    elif errors:
        status = RunStatus.FAILED
    else:
        status = RunStatus.DONE
    if status is RunStatus.STOPPED:
        reason = (
            "Der User hat den Run gestoppt." if ran else "Der Run wurde vor dem Start gestoppt."
        )
    elif status is RunStatus.FAILED and errors:
        reason = f"Fehler: {errors[0].get('message') or errors[0].get('type')}"
    elif status is RunStatus.INTERRUPTED:
        reason = "arr wurde während des Runs beendet."
    elif limits:
        kind = str(limits[0].get("limit"))
        reason = "Limit: " + _LIMIT_TEXT.get(kind, kind)
    elif failed_stage:
        reason = f"Die Stufe „{failed_stage}“ wurde nicht bestanden (stage_failed)."
    else:
        reason = "Die Topologie ist regulär durchgelaufen."
    return status, reason

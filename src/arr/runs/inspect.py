"""Read-only views of run folders for `arr runs list|status` (W2-05) and later the service.

Everything comes from the files of docs/02 §4: `meta.json`, `orchestrator.jsonl`,
`approvals.jsonl`, `usage.json`, plus `stop.request`, the stop request of `arr runs stop`
(`arr.runs.launch`), and `run.lock`, the lock an `arr run` process holds while it executes
the run (`run_lock_held`).
"""

import contextlib
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from arr.errors import ArrError
from arr.models import RunStatus
from arr.runs.finalize import failed_stage_text
from arr.runs.orchestrator import OrchestratorLog, stopped_text
from arr.runs.prepare import RunMeta, load_run_meta
from arr.usage import USAGE_FILE, RunUsage

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no flock, no run is ever seen as held
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "LOCKS_AVAILABLE",
    "RUN_LOCK",
    "STATUS_TEXT",
    "STOP_REQUEST",
    "RunLock",
    "RunStatusView",
    "TurnCount",
    "describe_checkpoint",
    "format_duration",
    "format_runs",
    "format_status",
    "list_runs",
    "open_approvals",
    "run_folder",
    "run_lock_held",
    "run_reason",
    "run_status",
]

STOP_REQUEST = "stop.request"
"""File in the run folder that asks the running `arr run` process to stop (docs/02 §4)."""

RUN_LOCK = "run.lock"
"""File in the run folder on which the executing `arr run` process holds an `fcntl.flock`
(W2-05.1 M1). The lock falls with the process, also after `os._exit` or a crash; the file
itself stays. A run in an active status without the lock is one whose process is gone."""

LOCKS_AVAILABLE = fcntl is not None
"""False on platforms without `fcntl` (Windows): then nothing is locked or checked."""

ACTIVE_STATUSES = frozenset(
    {RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.FINALIZING}
)
"""Statuses in which some process should be executing the run."""


def run_lock_held(run_dir: str | Path) -> bool:
    """True if a process (or another file handle) holds the run's lock right now.

    Probes with a non-blocking *shared* lock on a fresh handle and releases it at once: it
    fails only while an executing process holds the exclusive lock, and several probes (the
    service asks often, W3-01) never block each other. A missing lock file counts as not held.
    Without `fcntl` always False.
    """
    if fcntl is None:
        return False
    try:
        handle = (Path(run_dir) / RUN_LOCK).open("rb")
    except OSError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


LOCK_RETRIES = 20
LOCK_RETRY_WAIT_S = 0.05
"""`RunLock.acquire` tries this often, this far apart, before it gives up: a probe of
`run_lock_held` holds the file for a moment, and a starting run must not fail on it."""


class RunLock:
    """Owner mark of the run this process executes: `fcntl.flock` on `<run>/run.lock`.

    Taken right after the run folder is reserved (`arr run`: `run_launch`; service: the same
    path; a `RunManager` with a reserved `run_id` takes it before the folder is prepared) and
    held until the run is over, the secret scan included. Other processes see it with
    `run_lock_held`: a second `arr run` leaves the run alone when it marks crashed runs, and
    `arr runs stop` tells when no process executes a run any more. The lock falls with the
    process (also after `os._exit` and a crash); the file stays. Without `fcntl` (Windows)
    nothing is locked.
    """

    def __init__(self, run_dir: str | Path) -> None:
        self.path = Path(run_dir) / RUN_LOCK
        self._handle: Any = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, retries: int = LOCK_RETRIES, wait_s: float = LOCK_RETRY_WAIT_S) -> None:
        """Take the lock; `ArrError` if another process holds it.

        A failed attempt is repeated `retries` times `wait_s` apart, so a brief probe of
        `run_lock_held` from another process never makes a start fail (W3-01).
        """
        if fcntl is None or self._handle is not None:
            return
        handle = self.path.open("ab")
        for attempt in range(retries + 1):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if attempt < retries:
                    time.sleep(wait_s)
                    continue
                handle.close()
                raise ArrError(
                    f"Den Run {self.path.parent.name} führt schon ein anderer arr-Prozess aus."
                ) from None
            self._handle = handle
            return

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


STATUS_TEXT = {
    RunStatus.DRAFT: "Entwurf",
    RunStatus.AWAITING_APPROVAL: "wartet auf Freigabe",
    RunStatus.QUEUED: "in der Warteschlange",
    RunStatus.PREPARING: "wird vorbereitet",
    RunStatus.RUNNING: "läuft",
    RunStatus.PAUSED: "pausiert",
    RunStatus.FINALIZING: "wird abgeschlossen",
    RunStatus.DONE: "abgeschlossen",
    RunStatus.STOPPED: "gestoppt",
    RunStatus.FAILED: "fehlgeschlagen",
    RunStatus.INTERRUPTED: "unterbrochen",
}
"""German words for the run statuses (docs/02 §7)."""

TURN_TEXT = {"ok": "ok", "interrupted": "unterbrochen", "limit": "Limit", "error": "Fehler"}
LIMIT_TEXT = {
    "wall_time": "Die Gesamtlaufzeit des Runs ist erreicht.",
    "turns_per_agent": "Die Zahl der Turns pro Agent ist erreicht.",
    "budget_usd": "Das Budget des Runs ist erreicht.",
}
DECISION_TEXT = {
    "passed": "bestanden",
    "rework": "Nacharbeit",
    "failed": "nicht bestanden",
    "escalated": "eskaliert",
}
OUTCOME_TEXT = {
    "passed": "bestanden",
    "best_accepted": "bester Versuch übernommen",
    "stage_failed": "nicht bestanden",
}
_REASON_TEXT = {"no_result": "kein Ergebnis", "no_rating": "kein Rating"}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------------------------
# Folders and lists
# --------------------------------------------------------------------------------------------


def run_folder(workfolder: str | Path, run_id: str) -> Path:
    """The folder of an existing run; `ArrError` for odd IDs or unknown runs."""
    runs = Path(workfolder) / "runs"
    if not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
        raise ArrError(f"„{run_id}“ ist keine Run-ID. Die IDs zeigt „arr runs list“.")
    folder = runs / run_id
    if not (folder / "meta.json").is_file():
        raise ArrError(
            f"Den Run „{run_id}“ gibt es nicht in {runs}. Die IDs zeigt „arr runs list“."
        )
    return folder


def list_runs(workfolder: str | Path) -> list[RunMeta]:
    """`meta.json` of every run of the workfolder, newest first; unreadable ones are left out."""
    runs = Path(workfolder) / "runs"
    metas = []
    for meta_path in runs.glob("*/meta.json"):
        try:
            metas.append(load_run_meta(meta_path.parent))
        except (OSError, ValueError):
            continue
    return sorted(metas, key=lambda m: (m.created, m.run_id), reverse=True)


# --------------------------------------------------------------------------------------------
# Status of one run
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class TurnCount:
    """Turns of one agent so far (from `turn_end` entries)."""

    turns: int = 0
    last_status: str | None = None
    cost_usd: float = 0.0


@dataclass(slots=True)
class RunStatusView:
    """What `arr runs status` shows."""

    meta: RunMeta
    run_dir: Path
    reason: str
    checkpoint: dict[str, Any] | None = None
    turns: dict[str, TurnCount] = field(default_factory=dict)
    usage: RunUsage | None = None
    open_approvals: list[dict[str, Any]] = field(default_factory=list)
    stop_requested: bool = False
    locked: bool = False
    secret_scan: dict[str, Any] | None = None


def run_reason(meta: RunMeta, entries: Sequence[Mapping[str, Any]]) -> str:
    """Why the run is in its status, in German (also for runs that are still active)."""
    status = meta.status
    errors = [
        e["data"]
        for e in entries
        if e["kind"] == "error" and e["data"].get("where") not in ("finalizer", "topology")
    ]
    limits = [e["data"] for e in entries if e["kind"] == "limit"]
    ran = any(
        e["kind"] == "status" and e["data"].get("status") == RunStatus.RUNNING for e in entries
    )
    failed = failed_stage_text(entries)
    if status is RunStatus.STOPPED:
        return stopped_text(entries, ran=ran)
    if status is RunStatus.FAILED:
        first = errors[0] if errors else {}
        return f"Fehler: {first.get('message') or first.get('type') or 'unbekannt'}"
    if status is RunStatus.INTERRUPTED:
        return "arr wurde während des Runs beendet."
    if status is RunStatus.DONE:
        if limits:
            kind = str(limits[0].get("limit"))
            return "Limit: " + LIMIT_TEXT.get(kind, kind)
        if failed:
            return failed
        return "Die Topologie ist regulär durchgelaufen."
    if status is RunStatus.PAUSED:
        reasons = [e["data"].get("reason") for e in entries if e["kind"] == "pause"]
        return f"Pausiert ({reasons[-1] if reasons else 'user'})."
    if status in (RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.FINALIZING):
        return "Der Run ist noch aktiv."
    return "Der Run wurde noch nicht gestartet."


def open_approvals(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Entries `open` of `approvals.jsonl` that no later event closed, oldest first.

    Damaged lines are skipped: they must not take down what reads the run.
    """
    opened: dict[str, dict[str, Any]] = {}
    for entry in entries:
        item_id = entry.get("item_id") if isinstance(entry, Mapping) else None
        if not item_id or not isinstance(item_id, str):
            continue
        if entry.get("event") == "open":
            opened[item_id] = entry
        elif entry.get("event") in ("decided", "expired", "obsolete"):
            opened.pop(item_id, None)
    return list(opened.values())


def _approval_entries(run_dir: Path) -> list[dict[str, Any]]:
    from arr.permissions.policy import ApprovalLog

    try:
        return ApprovalLog(run_dir / "approvals.jsonl").entries()
    except (OSError, ValueError):
        return []


def run_status(run_dir: str | Path) -> RunStatusView:
    """Collect the status view of a run folder (readable at every point of its life)."""
    folder = Path(run_dir)
    meta = load_run_meta(folder)
    entries = OrchestratorLog(folder).entries()
    view = RunStatusView(meta=meta, run_dir=folder, reason=run_reason(meta, entries))
    for entry in entries:
        data = entry["data"]
        match entry["kind"]:
            case "checkpoint":
                view.checkpoint = dict(data)
            case "turn_end":
                count = view.turns.setdefault(str(data.get("agent")), TurnCount())
                count.turns += 1
                count.last_status = data.get("status")
                count.cost_usd += float(data.get("cost_usd") or 0.0)
            case "secret_scan":
                view.secret_scan = dict(data)
    usage_file = folder / USAGE_FILE
    if usage_file.is_file():
        try:
            view.usage = RunUsage.model_validate_json(usage_file.read_text(encoding="utf-8"))
        except ValueError:
            view.usage = None
    view.open_approvals = open_approvals(_approval_entries(folder))
    view.stop_requested = (folder / STOP_REQUEST).exists()
    view.locked = run_lock_held(folder)
    return view


# --------------------------------------------------------------------------------------------
# German text
# --------------------------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """`2 h 5 min`, `3 min 12 s` or `45 s`."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "–"


def _number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "–"
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def _run_duration(meta: RunMeta, now: float | None = None) -> str:
    if meta.started is None:
        return "–"
    end = meta.ended if meta.ended is not None else (now or time.time())
    text = format_duration(end - meta.started)
    return text if meta.ended is not None else f"{text} (läuft)"


def format_runs(
    workfolder: str | Path, metas: Sequence[RunMeta], *, now: float | None = None
) -> str:
    """Table of `arr runs list`: ID, status, start, duration (German)."""
    runs = Path(workfolder) / "runs"
    if not metas:
        return f"Keine Runs in {runs}."
    rows = [
        (
            m.run_id,
            f"{STATUS_TEXT.get(m.status, m.status.value)} ({m.status.value})",
            _clock(m.started or m.created),
            _run_duration(m, now),
        )
        for m in metas
    ]
    header = ("Run-ID", "Status", "Start", "Dauer")
    widths = [max(len(r[i]) for r in (header, *rows)) for i in range(4)]
    lines = [f"Runs in {runs} (neueste zuerst):"]
    for row in (header, *rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


_LINE_MAX = 200
"""At most this many characters of one value from the log in a progress line (W4b-07 §6.16)."""
_LINE_ALL_MAX = 600
"""At most this many characters of a whole progress line."""
_BREAKING = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Unicode categories that never reach a progress line: control and format characters, line
and paragraph separators (U+2028, U+2029; NEL and the like are `Cc`)."""

_HOW_TEXT = {
    "normal": "",
    "retry": " (Wiederholung nach Route „retry“)",
    "user_retry": " (noch ein Durchlauf, von dir gewählt)",
}
"""`how` of a checkpoint `pass_start` of a phases run (W4b-02b §5.2)."""
_ASKED_TEXT = {
    "gate_user": "Gate „user“",
    "escalate": "Route „escalate“",
    "exhausted": "Durchläufe aufgebraucht",
    "no_result": "Gate ohne Ergebnis",
}
"""`asked_user` of a checkpoint `route`: why the user was asked."""
_CHOICE_TEXT = {"continue": "weiter", "retry": "noch ein Durchlauf", "end": "Run beenden"}
"""`choice` of a checkpoint `route` and `option` of `escalation_decided` in a phases run."""
_PHASE_OUTCOME_TEXT = {
    "done": "erledigt",
    "passed": "bestanden",
    "best_accepted": "beste Abgabe übernommen",
    "stage_failed": "nicht bestanden",
}
"""`outcome` of a checkpoint `stage_end` of a phases run."""


def _line_text(value: Any, limit: int = _LINE_MAX) -> str:
    """A value from the log (agent text, a name, any JSON value) on one line: every control,
    format and separator character and every run of white space becomes one space; at most
    `limit` characters, cut with „…“. Escaping for HTML is up to the caller."""
    text = value if isinstance(value, str) else str(value)
    cut = len(text) > limit * 8  # bound the work for very long values
    if cut:
        text = text[: limit * 8]
    text = "".join(" " if unicodedata.category(c) in _BREAKING else c for c in text)
    text = " ".join(text.split())
    if cut or len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _names(value: Any) -> str:
    """A list of names from the log as `a, b`, shortened; any other value as one text."""
    if isinstance(value, list | tuple):
        return _line_text(", ".join(str(v) for v in value))
    return _line_text(value)


def _phase_checkpoint(data: Mapping[str, Any], stage: str) -> str | None:
    """The checkpoints of a phases run (W4b-02b §5.2), None for any other checkpoint."""
    kind = data.get("phase")
    where = f"Phase „{stage}“, Durchlauf {_line_text(data.get('attempt', '–'), 20)}"
    if kind == "pass_start":
        how = str(data.get("how"))
        text = f"Phase „{stage}“: Durchlauf {_line_text(data.get('attempt', '–'), 20)} beginnt"
        text += _HOW_TEXT.get(how, f" ({_line_text(how, 40)})")
        return text + (f" mit {_names(data['agents'])}" if data.get("agents") else "")
    if kind == "gate" and ("gate" in data or "value" in data):
        gate = f"Gate „{_line_text(data['gate'], 40)}“" if data.get("gate") else "Gate"
        user = data.get("gate") == "user" and data.get("value") is None
        if user:  # the user decides: no result of its own
            text = f"{where}: {gate}, du entscheidest"
        elif data.get("value") is not None:
            text = f"{where}: {gate} ergibt „{_line_text(data['value'], 80)}“"
        else:
            reason = str(data.get("reason"))
            text = f"{where}: {gate} ohne Ergebnis"
            if reason in _REASON_TEXT:
                text += f" ({_REASON_TEXT[reason]})"
        if data.get("tie"):
            text += ", Gleichstand"
        if "decision" in data and not (user and data["decision"] == "escalated"):
            decision = str(data["decision"])
            text += f" – {DECISION_TEXT.get(decision, _line_text(decision, 40))}"
        if data.get("score") is not None:
            text += f", Score {_number(data['score'])}"
        if data.get("missing"):
            text += f", es fehlt: {_names(data['missing'])}"
        return text
    if kind == "route":
        route = data.get("route")
        text = f"{where}: Route „{_line_text(route, 80)}“" if route else f"{where}:"
        if data.get("asked_user"):
            asked = str(data["asked_user"])
            choice = str(data.get("choice"))
            text += "," if route else ""
            text += f" du entscheidest ({_ASKED_TEXT.get(asked, _line_text(asked, 40))}): "
            text += _CHOICE_TEXT.get(choice, _line_text(choice, 40))
            if "hint" in data:
                text += " mit Hinweis" if data["hint"] else " ohne Hinweis"
        elif not route:
            text += " keine Route"
        if "entered" in data:
            entered = data["entered"]
            text += f" → Phase „{_line_text(entered, 80)}“" if entered else " → Ende des Runs"
        if data.get("exhausted"):
            text += f" (aufgebraucht: {_names(data['exhausted'])})"
        return text
    if kind == "stage_end" and "valid" in data:  # the old topologies write no `valid`
        outcome = str(data.get("outcome"))
        text = f"Phase „{stage}“ abgeschlossen nach Durchlauf "
        text += f"{_line_text(data.get('attempt', '–'), 20)}: "
        text += _PHASE_OUTCOME_TEXT.get(outcome, _line_text(outcome, 40))
        if data.get("accepted"):
            text += f", gilt: Abgabe #{_line_text(data['accepted'], 20)}"
        if data.get("score") is not None:
            text += f", Score {_number(data['score'])}"
        return text
    if kind == "seed":
        seeds = data.get("seeds")
        if isinstance(seeds, Mapping) and seeds:
            listed = ", ".join(f"{k} #{v}" for k, v in seeds.items())
            return f"Vorgaben (seed) vor Phase „{stage}“ abgelegt: {_line_text(listed)}"
        return f"Vorgaben (seed) vor Phase „{stage}“ abgelegt"
    return None


def describe_checkpoint(data: Mapping[str, Any], *, phases: bool = False) -> str:
    """One German line for a `checkpoint` entry of `orchestrator.jsonl`.

    Always exactly one line: values from the log are shortened (`_line_text`); a checkpoint
    with unexpected types gets the generic line instead of an error (W4b-07 §6.1, §6.16).
    `phases`: the run has `topology: phases`; the discussion of a phase `chat` then names its
    „Phase“, not its „Stufe“ (W4b-06b). Old runs keep their lines word for word."""
    if not isinstance(data, Mapping):
        return "Checkpoint" + (f" ({_line_text(data, 300)})" if data else "")
    try:
        text = _describe_checkpoint(data, phases_run=phases)
    except (TypeError, ValueError, AttributeError, LookupError, ArithmeticError):
        text = _generic_checkpoint(data)
    return _line_text(text, _LINE_ALL_MAX)


def _generic_checkpoint(data: Mapping[str, Any]) -> str:
    extra = ", ".join(f"{k}={v}" for k, v in data.items() if k != "stage")
    stage = _line_text(data.get("stage"), 80)
    return f"Checkpoint „{stage}“" + (f" ({_line_text(extra, 300)})" if extra else "")


def _describe_checkpoint(data: Mapping[str, Any], *, phases_run: bool = False) -> str:
    stage = data.get("stage")
    if stage == "plan":
        stages = data.get("stages") or []
        return "Plan: " + (" → ".join(str(s) for s in stages) if stages else "keine Stufen")
    phases = _phase_checkpoint(data, _line_text(stage, 80))
    if phases is not None:
        return phases
    if "decision" in data:
        decision = DECISION_TEXT.get(str(data["decision"]), str(data["decision"]))
        if data.get("reason") in _REASON_TEXT:
            decision += f" ({_REASON_TEXT[str(data['reason'])]})"
        text = f"Stufe „{stage}“, Versuch {data.get('attempt', '–')} ({data.get('agent', '–')}): "
        text += decision
        if data.get("score") is not None:
            text += f", Score {_number(data['score'])}"
        return text
    if data.get("phase") == "stage_end":
        outcome = OUTCOME_TEXT.get(str(data.get("outcome")), str(data.get("outcome")))
        text = f"Stufe „{stage}“ abgeschlossen: {outcome}"
        if data.get("accepted"):
            text += f", akzeptiert #{data['accepted']}"
        return text
    if data.get("phase") == "discussion":
        word = "Phase" if phases_run else "Stufe"
        return f"{word} „{stage}“: Diskussion in #{_line_text(data.get('channel'), 80)}"
    return _generic_checkpoint(data)


def _approval_line(entry: Mapping[str, Any]) -> str:
    request = entry.get("request") or {}
    what = request.get("command") or ", ".join(request.get("hosts") or request.get("paths") or [])
    text = f"{entry.get('item_id')}: {request.get('agent_id')} · {request.get('tool')}"
    return text + (f" · {what}" if what else "")


def format_status(view: RunStatusView, *, now: float | None = None) -> str:
    """Text of `arr runs status` (German)."""
    meta = view.meta
    status = f"{STATUS_TEXT.get(meta.status, meta.status.value)} ({meta.status.value})"
    what = meta.project + (f", {meta.name}" if meta.name else "")
    lines = [
        f"Run:       {meta.run_id} ({what})",
        f"Status:    {status}",
        f"Grund:     {view.reason}",
        f"Beginn:    {_clock(meta.started)} · Dauer: {_run_duration(meta, now)}",
    ]
    if view.checkpoint is not None:
        lines.append(f"Zuletzt:   {describe_checkpoint(view.checkpoint)}")
    if view.turns:
        lines.append("Turns je Agent:")
        for agent, count in view.turns.items():
            last = TURN_TEXT.get(str(count.last_status), str(count.last_status))
            cost = f", {_number(count.cost_usd)} $" if count.cost_usd else ""
            noun = "Turn" if count.turns == 1 else "Turns"
            lines.append(f"  {agent}: {count.turns} {noun}, zuletzt {last}{cost}")
    else:
        lines.append("Turns je Agent: noch keine")
    if view.usage is not None:
        total = view.usage.total
        usage = total.usage
        parts = [f"{usage.input or 0} Input", f"{usage.output or 0} Output"]
        if usage.cache_read:
            parts.append(f"{usage.cache_read} aus dem Cache")
        if usage.cost_usd is not None:
            kind = "abgerechnet" if usage.cost_kind == "billed" else "rechnerisch"
            parts.append(f"{_number(usage.cost_usd)} $ ({kind})")
        lines.append("Usage:     " + ", ".join(parts))
    if view.open_approvals:
        lines.append("Offene Freigaben:")
        lines += [f"  - {_approval_line(e)}" for e in view.open_approvals]
    else:
        lines.append("Offene Freigaben: keine")
    if view.stop_requested:
        lines.append("Stopp angefordert: ja (stop.request liegt vor)")
    if meta.status in ACTIVE_STATUSES and LOCKS_AVAILABLE:
        if view.locked:
            lines.append("Prozess:   ein arr-run-Prozess führt den Run aus (hält run.lock)")
        else:
            lines.append(
                "Prozess:   keiner hält run.lock – der arr-run-Prozess ist wohl abgestürzt; "
                "beim nächsten „arr run“ gilt der Run als unterbrochen (interrupted)"
            )
    if view.secret_scan is not None:
        scan = view.secret_scan
        state = "sauber" if scan.get("clean") else f"{len(scan.get('findings') or [])} Funde"
        lines.append(f"Secret-Scan: {state} ({scan.get('files', 0)} Dateien)")
    lines.append(f"Ordner:    {view.run_dir}")
    return "\n".join(lines)

"""Run folders as the GUI sees them: cached readers and the mapping to `api_models`.

The data source of `RealApi` are the files of docs/02 §4, so runs of the service, runs of
`arr run` and finished runs look the same: `meta.json` (status, times), `run.yaml` (the
resolved configuration), `agents/*/agent.json` and `transcript.jsonl`, `orchestrator.jsonl`
(stages, turns, limits, errors), `approvals.jsonl`, `usage.json`, `output/` and
`groupchats/room.sqlite` (read only). `RunStore` keeps one `RunRecord` per run folder with
what was read so far; files that only grow are read incrementally (`JsonlTail`,
`TranscriptIndex`), the others by their modification time. So a list of 200 runs and a run
detail with tens of thousands of transcript lines stay below a second (W3-01 §3.3).

The mapping functions at the end turn a record into the models of the API contract (W1-06),
with the counting rules of docs/08 §8.3.
"""

import errno
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from arr.backends.base import CostKind, Usage
from arr.errors import ArrError
from arr.models import (
    AgentStatus,
    Decider,
    Provider,
    ResolvedRun,
    RunStatus,
    Starter,
    TopologyKind,
    parse_duration,
)
from arr.permissions.policy import is_codex_escalation
from arr.permissions.types import RequestCategory
from arr.resolve import parse_yaml_text, validate_model
from arr.room.store import RoomStore
from arr.room.types import Message as RoomMessage
from arr.runs.eventlog import RATE_LIMIT_PASSING
from arr.runs.inspect import describe_checkpoint, open_approvals, run_lock_held, run_reason
from arr.runs.launch import CHANNEL_WAIT_TEXT, ORPHAN_MIN_AGE_S, describe_entry
from arr.runs.prepare import AgentFile, RunMeta, load_agent_file, load_run_meta
from arr.runs.slim import SLIM_MARKER
from arr.service.api_models import (
    Action,
    Activity,
    ActivityKind,
    AgentChanges,
    AgentDisplayState,
    AgentsByState,
    AgentState,
    AgentWait,
    ApprovalTop,
    AttachmentInfo,
    Attempt,
    BestResult,
    BlockedBy,
    BlockedKind,
    ChangeInfo,
    ChannelInfo,
    ChannelMemberInfo,
    DeliveryInfo,
    DiscussionState,
    FileChange,
    LastMessage,
    LogEntry,
    Message,
    MessagesPage,
    RatingInfo,
    Result,
    ResultKind,
    ResultStatus,
    RunAction,
    RunDetail,
    RunError,
    RunLimits,
    RunResults,
    RunSummary,
    ScanFinding,
    SecretScanInfo,
    SizeInfo,
    StageInfo,
    StageRef,
    StageStatus,
    ToolAccess,
    TranscriptKind,
    TranscriptPage,
    Vendor,
    WaitReason,
)
from arr.service.real_api.transcript import (
    TOOL_CALL,
    TOOL_RESULT,
    TranscriptIndex,
    input_summary,
    to_event,
)
from arr.usage import USAGE_FILE, RunUsage

ORCHESTRATOR_LOG = "orchestrator.jsonl"
APPROVALS_LOG = "approvals.jsonl"
SUMMARY_FILE = "output/summary.md"
SIZE_TTL_S = 5.0
"""How long the size of an active run is kept before it is measured again."""
MAX_SIZE_FILES = 20_000
"""Files counted per run at most (protocols and output; working copies belong to W5-01)."""
EXCERPT_CHARS = 300
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MODEL_LABELS = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
}
_SIZE_SKIP = frozenset({"work", "base", "shared", ".venv", "tmp", "blobs"})


# --------------------------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------------------------


def model_label(model: str) -> str:
    """Display name of a model („Claude Haiku 5.5“); unknown models keep their ID."""
    return _MODEL_LABELS.get(model, model)


def vendor_of(provider: Provider) -> Vendor:
    return Vendor(provider.vendor)


def role_label(role: str | None, fallback: str) -> str:
    """„Strategist“ from `roles/strategist.md`; the agent's base ID without a role."""
    stem = Path(role).stem if role else fallback
    return stem[:1].upper() + stem[1:] if stem else fallback


def _excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# --------------------------------------------------------------------------------------------
# Incremental readers
# --------------------------------------------------------------------------------------------


class JsonlTail:
    """Reads a growing JSONL file in pieces and keeps all its entries.

    A torn last line waits for its newline, damaged lines are skipped, a file that shrank
    (truncated, replaced) is read again from the start, a missing file gives no entries.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self._end = 0
        self._rest = b""

    def refresh(self) -> list[dict[str, Any]]:
        """Read new complete lines; returns the new entries."""
        try:
            size = self.path.stat().st_size
        except OSError:
            if self.entries or self._rest:
                self.entries, self._end, self._rest = [], 0, b""
            return []
        if size < self._end:
            self.entries, self._end, self._rest = [], 0, b""
        if size == self._end:
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._end)
                chunk = handle.read(size - self._end)
        except OSError:
            return []
        self._end += len(chunk)
        *lines, self._rest = (self._rest + chunk).split(b"\n")
        new = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                new.append(entry)
        self.entries.extend(new)
        return new


_PEEK_KIND = re.compile(r"^topology:\n(?:[ ]{2}.*\n)*?[ ]{2}kind:[ ]*([a-z_]+)", re.M)
_PEEK_PASS = re.compile(r"^topology:\n(?:[ ]{2}.*\n)*?[ ]{4}pass_score:[ ]*([0-9.]+)", re.M)
_PEEK_LIMITS = re.compile(r"^limits:\n((?:[ ]{2}.*\n)*)", re.M)
_PEEK_LIMIT = re.compile(r"^[ ]{2}([a-z_]+):[ ]*(\S*)", re.M)
_PEEK_AGENTS = re.compile(r"^agents:\n((?:[ ]{2}.*\n)*)", re.M)
_PEEK_AGENT = re.compile(r"^[ ]{2}-[ ]id:", re.M)


@dataclass(slots=True)
class RunPeek:
    """The few facts of `run.yaml` a list needs, read without the YAML parser.

    `run.yaml` is written by arr itself (`dump_yaml_model`, block style, two-space indent),
    so a handful of patterns find topology, `pass_score`, limits and the number of agents in
    microseconds; a full `ResolvedRun` (milliseconds) is loaded only for detail views. A file
    that does not match (hand-edited) falls back to the full parse.
    """

    topology: TopologyKind
    agent_count: int
    wall_time_s: int | None
    turns_per_agent: int | None
    budget_usd: float | None
    pass_score: float | None


def peek_run_yaml(text: str) -> RunPeek | None:
    """`RunPeek` of a `run.yaml` text, or None if the text does not look like arr wrote it."""
    kind = _PEEK_KIND.search(text)
    agents = _PEEK_AGENTS.search(text)
    if kind is None or agents is None:
        return None
    try:
        topology = TopologyKind(kind.group(1))
    except ValueError:
        return None
    limits: dict[str, str] = {}
    block = _PEEK_LIMITS.search(text)
    if block is not None:
        limits = {m.group(1): m.group(2) for m in _PEEK_LIMIT.finditer(block.group(1))}

    def number(value: str | None) -> float | None:
        try:
            return float(value) if value not in (None, "", "null", "~") else None
        except ValueError:
            return None

    wall = limits.get("wall_time")
    wall_s: int | None = None
    if wall and wall not in ("null", "~"):
        try:
            wall_s = int(parse_duration(wall).total_seconds())
        except (ValueError, TypeError, AttributeError):
            wall_s = None
    turns = number(limits.get("turns_per_agent"))
    pass_match = _PEEK_PASS.search(text)
    return RunPeek(
        topology=topology,
        agent_count=len(_PEEK_AGENT.findall(agents.group(1))),
        wall_time_s=wall_s,
        turns_per_agent=int(turns) if turns is not None else None,
        budget_usd=number(limits.get("budget_usd")),
        pass_score=number(pass_match.group(1)) if pass_match else None,
    )


MAX_FILE_BYTES = 1_000_000
"""Upper bound for a file of a run folder read in one piece (`run.yaml`, `task.md`, summary)."""
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class FileTooLarge(ValueError):
    """A file of a run folder is larger than the bound of `read_regular`."""


def read_regular(path: Path, limit: int = MAX_FILE_BYTES, *, prefix: bool = False) -> str:
    """Text of a regular file of a run folder, guarded against what an agent could place there.

    W4-01 (review W3-10, finding 5): no link is followed (`O_NOFOLLOW`), a FIFO or device never
    blocks (`O_NONBLOCK`, then `S_ISREG` on the open file), and at most `limit` bytes are read.
    A larger file raises `FileTooLarge`, with `prefix` its first `limit` bytes are returned.
    Invalid UTF-8 becomes `�`.
    """
    fd = os.open(path, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "keine normale Datei", str(path))
        if info.st_size > limit and not prefix:
            raise FileTooLarge(f"{path.name} ist größer als {limit} Bytes")
        chunks: list[bytes] = []
        size = 0
        while size <= limit:
            chunk = os.read(fd, min(1 << 16, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > limit:
        if not prefix:
            raise FileTooLarge(f"{path.name} ist größer als {limit} Bytes")
        data = data[:limit]
    return data.decode("utf-8", errors="replace")


class _Cached[T]:
    """A value read from one file, refreshed when the file's size or mtime changes.

    The file itself is looked at without following a link (`lstat`); `read` should use
    `read_regular`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.key: tuple[int, int] | None = None
        self.value: T | None = None

    def get(self, read: Any) -> T | None:
        try:
            st = os.lstat(self.path)
        except OSError:
            self.key, self.value = None, None
            return None
        key = (st.st_mtime_ns, st.st_size)
        if key != self.key:
            try:
                self.value = read(self.path)
            except (OSError, ValueError, ArrError):
                self.value = None
            self.key = key
        return self.value


# --------------------------------------------------------------------------------------------
# Room (read only)
# --------------------------------------------------------------------------------------------


class RoomReader:
    """Read-only view of `groupchats/room.sqlite` (docs/02 §5) for the GUI.

    Typed rows come from `RoomStore`'s public readers; counts and the newest IDs from small
    read-only queries. A missing, locked or broken database gives empty answers, never an
    error.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.db_path = run_dir / "groupchats" / "room.sqlite"
        self._store: RoomStore | None = None

    @property
    def available(self) -> bool:
        return self.db_path.is_file()

    def _intact(self) -> bool:
        """False for a file that is no database: `RoomStore` readers would raise."""
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)
            try:
                con.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                con.close()
        except sqlite3.OperationalError:
            return True  # locked for the moment
        except sqlite3.Error:
            return False
        return True

    @property
    def store(self) -> RoomStore | None:
        if self._store is None and self.available and self._intact():
            try:
                self._store = RoomStore(self.run_dir)
            except ArrError:
                return None
        return self._store

    def rows(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        if not self.available:
            return []
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.Error:
            return []
        try:
            con.row_factory = sqlite3.Row
            return con.execute(sql, tuple(args)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()

    def scalar(self, sql: str, args: Sequence[Any] = (), default: Any = 0) -> Any:
        rows = self.rows(sql, args)
        if not rows or rows[0][0] is None:
            return default
        return rows[0][0]

    def max_message_id(self) -> int:
        return int(self.scalar("SELECT max(id) FROM msg"))

    def max_result_id(self) -> int:
        return int(self.scalar("SELECT max(id) FROM result"))

    def max_rating_id(self) -> int:
        return int(self.scalar("SELECT max(id) FROM rating"))

    def channel_stats(self) -> dict[str, dict[str, Any]]:
        """Per channel: message count, newest ID, open questions, pinned IDs."""
        stats: dict[str, dict[str, Any]] = {}
        empty: dict[str, Any] = {"count": 0, "last": None, "open": 0, "pinned": []}
        sql = "SELECT channel, count(*) AS n, max(id) AS last FROM msg GROUP BY channel"
        for row in self.rows(sql):
            stats[row["channel"]] = {**empty, "count": row["n"], "last": row["last"], "pinned": []}
        sql = "SELECT channel, count(*) AS n FROM msg WHERE open=1 GROUP BY channel"
        for row in self.rows(sql):
            stats.setdefault(row["channel"], {"count": 0, "last": None, "open": 0, "pinned": []})
            stats[row["channel"]]["open"] = row["n"]
        for row in self.rows("SELECT channel, id FROM msg WHERE pinned=1 ORDER BY id"):
            stats.setdefault(row["channel"], {"count": 0, "last": None, "open": 0, "pinned": []})
            stats[row["channel"]]["pinned"].append(row["id"])
        return stats

    def open_questions(self) -> list[RoomMessage]:
        """Open questions of all channels (for „Fragen an dich“ and per agent)."""
        store = self.store
        if store is None:
            return []
        ids = [row["id"] for row in self.rows("SELECT id FROM msg WHERE open=1 ORDER BY id")]
        out = []
        for msg_id in ids:
            try:
                out.append(store.message(msg_id))
            except ArrError:
                continue
        return out

    def last_message(self) -> RoomMessage | None:
        store = self.store
        if store is None:
            return None
        msg_id = self.max_message_id()
        if not msg_id:
            return None
        try:
            return store.message(msg_id)
        except ArrError:
            return None

    def reply_counts(self, ids: Iterable[int]) -> dict[int, int]:
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        marks = ",".join("?" for _ in wanted)
        sql = (
            f"SELECT reply_to, count(*) AS n FROM msg WHERE reply_to IN ({marks}) GROUP BY reply_to"
        )
        return {row["reply_to"]: row["n"] for row in self.rows(sql, wanted)}

    def page_ids(self, channel: str, *, before_id: int | None, limit: int) -> list[int]:
        """IDs of the newest `limit` messages of a channel before `before_id`, ascending."""
        before = before_id if before_id is not None else 2**62
        rows = self.rows(
            "SELECT id FROM msg WHERE channel=? AND id<? ORDER BY id DESC LIMIT ?",
            (channel, before, limit),
        )
        return sorted(row["id"] for row in rows)

    def count_before(self, channel: str, msg_id: int) -> int:
        sql = "SELECT count(*) FROM msg WHERE channel=? AND id<?"
        return int(self.scalar(sql, (channel, msg_id)))

    def count_channel(self, channel: str) -> int:
        return int(self.scalar("SELECT count(*) FROM msg WHERE channel=?", (channel,)))


# --------------------------------------------------------------------------------------------
# Records and the store
# --------------------------------------------------------------------------------------------


@dataclass
class AgentRecord:
    """One agent of a run: `agent.json` (once) and the index of its transcript."""

    id: str
    agent_dir: Path
    file: AgentFile | None
    transcript: TranscriptIndex


SCAN_GRACE_S = 60.0
"""A finished run is read again for this long until its `secret_scan` entry is there."""


class RunRecord:
    """What the store knows about one run folder (see the module docstring)."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_id = run_dir.name
        self.meta: RunMeta | None = None
        self._meta_key: tuple[int, int] | None = None
        self.resolved: ResolvedRun | None = None
        self.run_yaml_text: str | None = None
        """The text `load_resolved` parsed (W4-01: `run.yaml` is read once)."""
        self._resolved_key: tuple[int, int] | None = None
        self._peek: _Cached[RunPeek] = _Cached(run_dir / "run.yaml")
        self._final_extras: tuple[BestResult | None, LastMessage | None] | None = None
        self.log = JsonlTail(run_dir / ORCHESTRATOR_LOG)
        self.approvals = JsonlTail(run_dir / APPROVALS_LOG)
        self.item_lines: Callable[[], list[dict[str, Any]] | None] = lambda: None
        """Item lines as the run's writer in this process holds them; None: only the file."""
        self.agents: dict[str, AgentRecord] = {}
        self._agents_key: int | None = None
        self._usage: _Cached[RunUsage] = _Cached(run_dir / USAGE_FILE)
        self._summary_md: _Cached[str] = _Cached(run_dir / SUMMARY_FILE)
        self._task_md: _Cached[str] = _Cached(run_dir / "task.md")
        self._slim_mark: _Cached[bool] = _Cached(run_dir / SLIM_MARKER)
        self.room = RoomReader(run_dir)
        self._size: SizeInfo | None = None
        self._size_at = 0.0
        self._size_final = False
        self.version = 0
        """Grows with every change seen; mappers may cache by it."""
        self.settled = False
        """True once everything was read after the run reached a final status: from then on
        the files do not change any more and readers need not look again."""

    # -- files --------------------------------------------------------------------------------

    def refresh_meta(self) -> bool:
        """Reload `meta.json` if it changed; True when it did (or vanished)."""
        try:
            st = (self.run_dir / "meta.json").stat()
        except OSError:
            changed = self.meta is not None
            self.meta, self._meta_key = None, None
            return changed
        key = (st.st_mtime_ns, st.st_size)
        if key == self._meta_key:
            return False
        try:
            self.meta = load_run_meta(self.run_dir)
        except (OSError, ValueError):
            return False  # half written: keep the last good one, try again next time
        self._meta_key = key
        self.version += 1
        self.settled = False
        return True

    def refresh(self) -> bool:
        """Reload everything that changed on disk; True when something did."""
        changed = self.refresh_meta()
        if self.meta is None:
            return changed
        if self.log.refresh():
            changed = True
        if self.approvals.refresh():
            changed = True
        if self._discover_agents():
            changed = True
        for agent in self.agents.values():
            if agent.transcript.refresh():
                changed = True
        if changed:
            self.version += 1
        self.settled = self.final and self._scan_over()
        return changed

    def _scan_over(self) -> bool:
        """The secret scan comes after the final status (U3): wait for its entry, at most
        `SCAN_GRACE_S` after the end (a run without scan settles as well)."""
        if any(e.get("kind") == "secret_scan" for e in reversed(self.log.entries[-5:])):
            return True
        ended = self.meta.ended if self.meta is not None else None
        return ended is None or time.time() - ended > SCAN_GRACE_S

    def _discover_agents(self) -> bool:
        agents_dir = self.run_dir / "agents"
        try:
            key = agents_dir.stat().st_mtime_ns
        except OSError:
            return False
        if key == self._agents_key and self.agents:
            return False
        self._agents_key = key
        changed = False
        try:
            names = sorted(p.name for p in agents_dir.iterdir() if p.is_dir())
        except OSError:
            return False
        for name in names:
            if name in self.agents:
                if self.agents[name].file is None:
                    self.agents[name].file = self._load_agent_file(name)
                continue
            agent_dir = agents_dir / name
            self.agents[name] = AgentRecord(
                id=name,
                agent_dir=agent_dir,
                file=self._load_agent_file(name),
                transcript=TranscriptIndex(agent_dir / "transcript.jsonl"),
            )
            changed = True
        return changed

    def _load_agent_file(self, agent_id: str) -> AgentFile | None:
        try:
            return load_agent_file(self.run_dir, agent_id)
        except (OSError, ValueError):
            return None

    def load_resolved(self) -> ResolvedRun | None:
        """`run.yaml` as `ResolvedRun` (read once; again if the file changes).

        Read with `read_regular` (no link, no FIFO, at most `MAX_FILE_BYTES`); the text stays
        in `run_yaml_text` for the inputs view, so it is never read a second time (W4-01).
        """
        path = self.run_dir / "run.yaml"
        try:
            st = os.lstat(path)
        except OSError:
            return self.resolved
        key = (st.st_mtime_ns, st.st_size)
        if key != self._resolved_key:
            try:
                text = read_regular(path)
                data, lines = parse_yaml_text(text, str(path))
                self.resolved = validate_model(ResolvedRun, data, source=str(path), lines=lines)
                self.run_yaml_text = text
            except (OSError, ArrError, ValueError):
                self.resolved = None
                self.run_yaml_text = None
            self._resolved_key = key
        return self.resolved

    def peek(self) -> RunPeek | None:
        """The list facts of `run.yaml` (regex, cached by mtime); a loaded `ResolvedRun` wins."""
        if self.resolved is not None:
            limits = self.resolved.limits
            pass_score = self.resolved.params.get("pass_score")
            return RunPeek(
                topology=self.resolved.topology.kind,
                agent_count=len(self.resolved.agents),
                wall_time_s=int(limits.wall_time.total_seconds()) if limits.wall_time else None,
                turns_per_agent=limits.turns_per_agent,
                budget_usd=limits.budget_usd,
                pass_score=float(pass_score) if isinstance(pass_score, int | float) else None,
            )
        peek = self._peek.get(lambda p: peek_run_yaml(read_regular(p)))
        if peek is None and (self.run_dir / "run.yaml").is_file():
            resolved = self.load_resolved()  # a run.yaml arr did not write: full parse
            if resolved is not None:
                return self.peek()
        return peek

    @property
    def usage(self) -> RunUsage | None:
        return self._usage.get(lambda p: RunUsage.model_validate_json(read_regular(p)))

    @property
    def summary_md(self) -> str | None:
        return self._summary_md.get(read_regular)

    @property
    def task_md(self) -> str | None:
        return self._task_md.get(read_regular)

    @property
    def slimmed(self) -> bool:
        return bool(self._slim_mark.get(lambda _path: True))

    @property
    def status(self) -> RunStatus:
        return self.meta.status if self.meta is not None else RunStatus.DRAFT

    @property
    def final(self) -> bool:
        return self.meta is not None and self.meta.status.is_final

    def agent_ids(self) -> list[str]:
        """Agents in configuration order (from `run.yaml`), then any further folders."""
        resolved = self.load_resolved()
        ordered = [a.id for a in resolved.agents] if resolved is not None else []
        return ordered + [a for a in self.agents if a not in ordered]

    def size(self) -> SizeInfo:
        """Disk use of protocols and output (cached; working copies come with W5-01)."""
        now = time.time()
        if self._size is not None and (self._size_final or now - self._size_at < SIZE_TTL_S):
            return self._size
        logs, output = _measure(self.run_dir)
        total = logs + output
        self._size = SizeInfo(actual_bytes=total, logical_bytes=total, logs=logs, output=output)
        self._size_at = now
        self._size_final = self.final
        return self._size


def _measure(run_dir: Path) -> tuple[int, int]:
    """Bytes of the protocol files and of `output/`; working copies are left out."""
    logs = output = 0
    count = 0
    stack = [run_dir]
    while stack and count < MAX_SIZE_FILES:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    count += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _SIZE_SKIP:
                                stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            if folder == run_dir / "output" or run_dir / "output" in folder.parents:
                                output += size
                            else:
                                logs += size
                    except OSError:
                        continue
        except OSError:
            continue
    return logs, output


class RunStore:
    """All run folders of a workfolder, each with its `RunRecord` (see the module docstring)."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.records: dict[str, RunRecord] = {}
        self.item_lines: Callable[[str], list[dict[str, Any]] | None] = lambda _run_id: None
        """Set by the approvals area, see `RunRecord.item_lines`."""
        self._dir_key: int | None = None

    def scan(self, *, refresh: bool = True) -> list[RunRecord]:
        """Find new and vanished run folders; returns all records, newest first.

        With `refresh` every record's `meta.json` is reloaded if it changed; without, only
        new folders are read (the watcher refreshes active runs itself).
        """
        try:
            key = self.runs_dir.stat().st_mtime_ns
        except OSError:
            self.records.clear()
            self._dir_key = None
            return []
        if key != self._dir_key or not self.records:
            self._dir_key = key
            try:
                names = {
                    e.name
                    for e in os.scandir(self.runs_dir)
                    if e.is_dir(follow_symlinks=False) and _RUN_ID_RE.fullmatch(e.name)
                }
            except OSError:
                names = set()
            for name in names - self.records.keys():
                self._add(name)
            for name in self.records.keys() - names:
                del self.records[name]
            refresh = True
        if refresh:
            for record in self.records.values():
                record.refresh_meta()
        return self.sorted()

    def _add(self, run_id: str) -> RunRecord:
        record = self.records[run_id] = RunRecord(self.runs_dir / run_id)
        record.item_lines = partial(self._item_lines_of, run_id)
        return record

    def _item_lines_of(self, run_id: str) -> list[dict[str, Any]] | None:
        return self.item_lines(run_id)

    def sorted(self) -> list[RunRecord]:
        """Records with a readable `meta.json`, newest first."""
        known = [r for r in self.records.values() if r.meta is not None]
        return sorted(known, key=lambda r: (r.meta.created, r.run_id), reverse=True)  # type: ignore[union-attr]

    def get(self, run_id: str) -> RunRecord | None:
        """The record of a run (scanning for it when unknown), or None."""
        if not _RUN_ID_RE.fullmatch(run_id) or run_id in (".", ".."):
            return None
        record = self.records.get(run_id)
        if record is None:
            if not (self.runs_dir / run_id / "meta.json").is_file():
                return None
            record = self._add(run_id)
        record.refresh_meta()
        return record if record.meta is not None else None


# --------------------------------------------------------------------------------------------
# Derived facts from the logs
# --------------------------------------------------------------------------------------------


def _data(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    data = entry.get("data")
    return data if isinstance(data, Mapping) else {}


def plan_and_stages(entries: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Planned stages (checkpoint `plan`) and the stages reached so far, in order."""
    plan: list[str] = []
    reached: list[str] = []
    for entry in entries:
        if entry.get("kind") != "checkpoint":
            continue
        data = _data(entry)
        stage = data.get("stage")
        if stage == "plan":
            stages = data.get("stages")
            if isinstance(stages, list):
                plan = [str(s) for s in stages]
            continue
        if not isinstance(stage, str) or not stage:
            continue
        if stage not in plan:
            plan.append(stage)
        if stage not in reached:
            reached.append(stage)
    return plan, reached


def turn_counts(entries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Finished turns per agent (from `turn_end` entries)."""
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.get("kind") == "turn_end":
            agent = str(_data(entry).get("agent"))
            counts[agent] = counts.get(agent, 0) + 1
    return counts


def spent_usd(entries: Sequence[Mapping[str, Any]]) -> float | None:
    total = None
    for entry in entries:
        if entry.get("kind") != "turn_end":
            continue
        cost = _data(entry).get("cost_usd")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            total = (total or 0.0) + float(cost)
    return total


def status_since(entries: Sequence[Mapping[str, Any]], meta: RunMeta) -> float | None:
    for entry in reversed(entries):
        if entry.get("kind") == "status":
            ts = entry.get("ts")
            if isinstance(ts, int | float):
                return float(ts)
    return meta.started or meta.created


def run_alive(record: RunRecord) -> bool:
    """True while a process executes the run: not over, and `run.lock` is held."""
    return not record.final and run_lock_held(record.run_dir)


def item_entries(record: RunRecord) -> list[dict[str, Any]]:
    """The lines items are read from: memory for a run of this service, else `approvals.jsonl`.

    Card, decision, events and counters use the same lines, so a line added to the file of such
    a run changes none of them (W3-03.1, W3-03.2).
    """
    own = record.item_lines()
    return record.approvals.entries if own is None else own


def open_items(record: RunRecord) -> list[dict[str, Any]]:
    """Open entries of a run (`item_entries`); none for a run nobody executes any more."""
    if record.final:
        return []
    items = open_approvals(item_entries(record))
    return items if items and run_alive(record) else []


def _item_is_escalation(entry: Mapping[str, Any]) -> bool:
    return (
        entry.get("request") is None
        and entry.get("question") is None
        and entry.get("run_start") is None  # a run start of Claude Code (W4-01)
    )


def _adjusted(record: RunRecord) -> list[ChangeInfo]:
    """Your changes of „Anpassen“ of a run start of Claude Code (W4-01), if any."""
    from arr.service.real_api.run_starts import change_infos, decision_of

    decision = decision_of(item_entries(record))
    if decision is None or decision.get("action") != "adjust":
        return []
    changes = decision.get("changes")
    return change_infos(changes) if isinstance(changes, list) else []


def item_question(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The question of an item „Frage“ (L16), None for other items."""
    question = entry.get("question")
    return question if isinstance(question, Mapping) else None


def open_escalation_id(record: RunRecord) -> str | None:
    """The item a paused run waits for, if an escalation paused it."""
    if record.status is not RunStatus.PAUSED:
        return None
    item = next((i for i in open_items(record) if _item_is_escalation(i)), None)
    return str(item.get("item_id")) if item is not None else None


def item_title(entry: Mapping[str, Any], *, stage: str | None = None, attempts: int = 0) -> str:
    """The sentence of an item (title of the card), without the full command (docs/08 R5)."""
    question = item_question(entry)
    if question is not None:
        return f"{question.get('agent_id') or 'Ein Agent'} fragt dich"
    request = entry.get("request") or {}
    if not isinstance(request, Mapping) or not request:
        if not stage:
            return entry.get("note") or "Eine Eskalation wartet auf deine Entscheidung"
        tries = f" nach {attempts} Versuchen" if attempts > 1 else ""
        return f"Stufe „{stage}“{tries} nicht bestanden"
    agent = str(request.get("agent_id"))
    categories = request.get("categories") or []
    hosts = request.get("hosts") or []
    if is_codex_escalation(request):
        return f"{agent} möchte einen Befehl mit erhöhten Rechten ausführen"
    if RequestCategory.INSTALL.value in categories:
        return f"{agent} möchte etwas installieren"
    if hosts:
        return f"{agent} möchte auf {', '.join(hosts)} zugreifen"
    if RequestCategory.READ_OUTSIDE.value in categories:
        return f"{agent} möchte außerhalb seines Arbeitsbereichs lesen"
    if RequestCategory.WRITE_OUTSIDE.value in categories:
        return f"{agent} möchte außerhalb des Runs schreiben"
    if RequestCategory.NETWORK.value in categories:
        return f"{agent} möchte aufs Netzwerk zugreifen"
    tool = request.get("tool")
    return f"{agent} möchte {tool} nutzen" if tool else agent


def describe_item(entry: Mapping[str, Any]) -> str:
    """One German line for an open item (banner „Wartet auf dich“)."""
    request = entry.get("request") or {}
    title = item_title(entry)
    question = item_question(entry)
    if question is not None:
        return f"{title}: {_excerpt(str(question.get('text') or ''), 120)}"
    if not isinstance(request, Mapping) or not request:
        return title
    what = request.get("command") or ", ".join(request.get("paths") or [])
    return title + (f": {what}" if what else "")


# --------------------------------------------------------------------------------------------
# Mapping: summary and detail
# --------------------------------------------------------------------------------------------


def _limits(record: RunRecord, peek: RunPeek | None, now: float) -> RunLimits:
    meta = record.meta
    assert meta is not None
    entries = record.log.entries
    elapsed = 0
    if meta.started is not None:
        elapsed = int(max(0.0, (meta.ended or now) - meta.started))
    counts = turn_counts(entries)
    spent = spent_usd(entries)
    usage = record.usage
    if usage is not None and usage.total.usage.cost_usd is not None:
        spent = usage.total.usage.cost_usd
    return RunLimits(
        wall_time_s=peek.wall_time_s if peek is not None else None,
        elapsed_s=elapsed,
        budget_usd=peek.budget_usd if peek is not None else None,
        spent_usd=round(spent, 4) if spent is not None else None,
        turns_per_agent=peek.turns_per_agent if peek is not None else None,
        max_turns_used=max(counts.values(), default=0),
    )


def _tokens(usage: Usage) -> int | None:
    parts = [usage.input, usage.output, usage.cache_read, usage.cache_write, usage.reasoning]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def run_usage(record: RunRecord) -> tuple[int | None, float | None, CostKind | None]:
    """Tokens, cost and cost kind of a run: `usage.json` when it exists, else the transcripts."""
    usage = record.usage
    if usage is not None:
        total = usage.total.usage
        return _tokens(total), total.cost_usd, total.cost_kind
    if not record.agents:
        return None, spent_usd(record.log.entries), None
    total = Usage()
    for agent in record.agents.values():
        part, _turns = agent.transcript.usage()
        total = total + part
    return _tokens(total), total.cost_usd, total.cost_kind


def _agent_counts(record: RunRecord, states: list[AgentState] | None) -> AgentsByState:
    if states is None:
        return AgentsByState()
    counts: dict[str, int] = {}
    for state in states:
        counts[state.state.value] = counts.get(state.state.value, 0) + 1
    return AgentsByState(**counts)


def best_result(record: RunRecord, peek: RunPeek | None) -> BestResult | None:
    store = record.room.store
    if store is None or not record.final:
        return None
    try:
        results = store.results()
        ratings = store.ratings()
    except ArrError:
        return None
    if not results:
        return None
    scores: dict[int, list[float]] = {}
    for rating in ratings:
        scores.setdefault(rating.result_id, []).append(rating.score)
    best = None
    for result in results:
        values = scores.get(result.id)
        avg = sum(values) / len(values) if values else None
        if best is None or (avg is not None and (best[1] is None or avg > best[1])):
            best = (result, avg)
    if best is None:
        return None
    return BestResult(
        title=best[0].title,
        score=best[1],
        pass_score=peek.pass_score if peek is not None else None,
    )


def final_extras(
    record: RunRecord, peek: RunPeek | None
) -> tuple[BestResult | None, LastMessage | None]:
    """Best result and last message; computed once for a run that is over."""
    if record.final and record._final_extras is not None:
        return record._final_extras
    extras = (best_result(record, peek), last_message(record))
    if record.final and record.settled:
        record._final_extras = extras
    return extras


def last_message(record: RunRecord) -> LastMessage | None:
    msg = record.room.last_message()
    if msg is None:
        return None
    return LastMessage(
        channel=msg.channel,
        sender=msg.sender_alias or msg.sender,
        excerpt=_excerpt(msg.body, 80) or "",
        ts=msg.ts,
    )


def counts_as_active(record: RunRecord, now: float) -> bool:
    """„Aktiv“ of docs/08 §8.3, without a reservation nobody took (W3-01 review G5).

    A `queued` folder older than `ORPHAN_MIN_AGE_S` whose `run.lock` nobody holds was left by a
    process that died after `reserve_run`; it stays until the next start clears it.
    """
    meta = record.meta
    if meta is None or not meta.status.is_active:
        return False
    if meta.status is RunStatus.QUEUED and now - meta.created >= ORPHAN_MIN_AGE_S:
        return run_lock_held(record.run_dir)
    return True


def run_error(record: RunRecord, base_url: str) -> RunError | None:
    meta = record.meta
    assert meta is not None
    if meta.status not in (RunStatus.FAILED, RunStatus.INTERRUPTED):
        return None
    reason = run_reason(meta, record.log.entries)
    if meta.status is RunStatus.INTERRUPTED:
        when = time.strftime("%d.%m. um %H:%M", time.localtime(meta.ended or meta.created))
        reason = (
            f"arr wurde am {when} beendet, während dieser Run lief. Alles bis dahin ist "
            "gespeichert und lesbar. Fortsetzen geht in v1 nicht, du kannst den Run mit "
            "gleicher Konfiguration neu starten."
        )
    else:
        reason = f"Run fehlgeschlagen: {reason.removeprefix('Fehler: ')}"
    return RunError(
        message=reason,
        action=Action(label="Neu starten", url=f"{base_url}/neuer-run?wie={record.run_id}"),
    )


def run_summary(
    record: RunRecord,
    *,
    base_url: str,
    now: float,
    resolved: ResolvedRun | None = None,
    agents: list[AgentState] | None = None,
) -> RunSummary:
    """`RunSummary` of a run folder (docs/08 §5.0; counting rules §8.3)."""
    meta = record.meta
    if meta is None:
        raise ArrError(f"Der Run-Ordner {record.run_dir} hat keine lesbare meta.json.")
    if resolved is not None:
        record.resolved = resolved
    peek = record.peek()
    entries = record.log.entries
    items = open_items(record)
    plan, reached = plan_and_stages(entries)
    stage = None
    if reached:
        current = reached[-1]
        stage = StageRef(
            index=plan.index(current) + 1 if current in plan else len(reached),
            count=max(len(plan), len(reached)),
            label=current,
        )
    tokens, cost, kind = run_usage(record)
    topology = peek.topology if peek is not None else TopologyKind.SOLO
    skill = meta.skill
    name = meta.name or f"{skill or topology.value} · {meta.project}"
    agent_count = peek.agent_count if peek is not None else len(record.agents)
    info_parts = []
    if stage is not None:
        info_parts.append(f"{stage.label} ({stage.index} von {stage.count})")
    if agent_count:
        info_parts.append(f"{agent_count} Agent{'en' if agent_count != 1 else ''}")
    open_escalation = meta.status is RunStatus.PAUSED and any(_item_is_escalation(i) for i in items)
    duration = None
    if meta.started is not None:
        duration = int(max(0.0, (meta.ended or now) - meta.started))
    best, last = final_extras(record, peek)
    changes = _adjusted(record) if meta.starter is Starter.USER_AGENT else []
    return RunSummary(
        id=record.run_id,
        name=name,
        project=meta.project,
        skill=skill,
        topology=topology,
        status=meta.status,
        status_since=status_since(entries, meta),
        active=counts_as_active(record, now),
        open_approvals=len(items),
        open_escalation=open_escalation,
        starter=meta.starter,
        created_at=meta.created,
        started_at=meta.started,
        ended_at=meta.ended,
        duration_s=duration,
        stage=stage,
        agents_by_state=_agent_counts(record, agents),
        limits=_limits(record, peek, now),
        tokens_total=tokens,
        cost_usd=round(cost, 4) if cost is not None else None,
        cost_kind=kind if cost is not None else None,
        size=record.size(),
        slimmed=record.slimmed,
        best_result=best,
        last_message=last,
        error=run_error(record, base_url),
        info=" · ".join(info_parts) or None,
        adjusted_by_user=bool(changes),
        changes=changes,
        secret_scan=secret_scan(entries),
        gui_url=f"{base_url}/runs/{record.run_id}",
    )


def secret_scan(entries: Sequence[Mapping[str, Any]]) -> SecretScanInfo | None:
    """The last `secret_scan` of the run (U3): clean, or the hits with file and line only."""
    for entry in reversed(entries):
        if entry.get("kind") != "secret_scan":
            continue
        data = _data(entry)
        findings = []
        for item in data.get("findings") or []:
            if not isinstance(item, Mapping):
                continue
            line = item.get("line")
            findings.append(
                ScanFinding(
                    file=str(item.get("file") or "?"),
                    line=line if isinstance(line, int) else 0,
                    kind=str(item.get("kind") or "?"),
                )
            )  # only these three fields: a value is never taken over
        files = data.get("files")
        return SecretScanInfo(
            clean=bool(data.get("clean")) and not findings,
            files=files if isinstance(files, int) and files >= 0 else 0,
            findings=findings,
        )
    return None


# --------------------------------------------------------------------------------------------
# Waits and the discussion (docs/09 §4.6)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _FlowView:
    """What `orchestrator.jsonl` says about waits: per agent and of the open discussion."""

    waits: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    """The agent's current `waiting` entry: a new turn ends every wait, a turn end every wait
    but `wait_until` and `result_from` (they start with the turn end)."""
    silent: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    """`turn_silent` of a turn that is still running."""
    channel: str | None = None
    channel_wait: Mapping[str, Any] | None = None
    round: int = 0
    of: int | None = None


_LONG_WAITS = ("wait_until", "result_from")
"""Reasons of `waiting` for a wait after the turn (`wait_until`, with `until_result_from` L14)."""


def flow_view(entries: Sequence[Mapping[str, Any]]) -> _FlowView:
    """Read the waits from the log: the last state per agent and of a running discussion."""
    view = _FlowView()
    for entry in entries:
        kind = entry.get("kind")
        data = _data(entry)
        agent = data.get("agent") if isinstance(data.get("agent"), str) else None
        if kind == "discussion_start":
            view.channel, view.channel_wait = data.get("channel"), None
            view.round, view.of = 0, None
        elif kind == "discussion_end":
            view.channel, view.channel_wait = None, None
        elif kind == "round" and data.get("channel") == view.channel:
            number, of = data.get("number"), data.get("of")
            view.round = number if isinstance(number, int) else view.round
            view.of = of if isinstance(of, int) else view.of
        elif kind == "waiting" and data.get("channel") and data.get("channel") == view.channel:
            view.channel_wait = entry
        elif agent is None:
            continue
        elif kind == "waiting":
            view.waits[agent] = entry
        elif kind == "turn_silent":
            view.silent[agent] = entry
        elif kind == "wait_end":
            view.waits.pop(agent, None)
        elif kind == "turn":
            view.waits.pop(agent, None)
            view.silent.pop(agent, None)
        elif kind == "turn_end":
            view.silent.pop(agent, None)
            if _data(view.waits.get(agent, {})).get("reason") not in _LONG_WAITS:
                view.waits.pop(agent, None)
    return view


def _number_of(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _asked(record: RunRecord, agent_id: str) -> list[str]:
    """Addressees of the agent's latest message with `to` (whom it waits for)."""
    rows = record.room.rows(
        "SELECT to_agents FROM msg WHERE sender=? AND to_agents NOT IN ('[]', '') "
        "ORDER BY id DESC LIMIT 1",
        (agent_id,),
    )
    try:
        names = json.loads(rows[0]["to_agents"]) if rows else []
    except (ValueError, TypeError):
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


def agent_wait(
    record: RunRecord,
    agent_id: str,
    status: AgentStatus | None,
    view: _FlowView,
    waits: Mapping[str, Mapping[str, Any]],
) -> AgentWait | None:
    """Why the agent waits and until when, None if it does not wait (docs/09 §4.6)."""
    if record.final or status is AgentStatus.DONE:
        return None
    row = waits.get(agent_id)
    if row is not None and row.get("kind") == "wait_for_messages":
        file = row.get("file")
        asked = ["dir" if name == "user" else name for name in _asked(record, agent_id)]
        text = f"wartet auf Antwort von {', '.join(asked)}" if asked else "wartet auf Nachrichten"
        if file:
            text += f" oder auf „{file}“"
        return AgentWait(
            reason=WaitReason.MESSAGES, text=text, until=_number_of(row.get("until")), file=file
        )
    last = view.waits.get(agent_id)
    data = _data(last) if last is not None else {}
    waiting = last is not None
    if status is AgentStatus.WAITING and waiting and data.get("reason") == "result_from":
        awaited = str(data.get("result_from") or "?")
        file = data.get("file") if isinstance(data.get("file"), str) else None
        text = f"wartet auf Ergebnis von {awaited}" + (f" oder auf „{file}“" if file else "")
        return AgentWait(
            reason=WaitReason.RESULT,
            text=text,
            until=_number_of(data.get("until")),
            file=file,
            result_from=awaited,
        )
    if status is AgentStatus.WAITING and waiting and data.get("reason") == "wait_until":
        file = data.get("file") if isinstance(data.get("file"), str) else None
        text = f"wartet auf „{file}“" if file else "wartet auf Nachrichten (Turn beendet)"
        return AgentWait(
            reason=WaitReason.UNTIL, text=text, until=_number_of(data.get("until")), file=file
        )
    if waiting and data.get("reason") == "capacity":
        return AgentWait(
            reason=WaitReason.CAPACITY,
            text=f"wartet auf freien Platz im Profil {data.get('profile')}",
            until=_number_of(data.get("until")),
        )
    channel = _data(view.channel_wait) if view.channel_wait is not None else {}
    if status is AgentStatus.IDLE and channel.get("reason") == "idle":
        if channel.get("next") == "end":
            return AgentWait(
                reason=WaitReason.END,
                text="untätig · Diskussion endet",
                until=_number_of(channel.get("until")),
            )
        return AgentWait(
            reason=WaitReason.ROUND,
            text=f"untätig · Runde {view.round + 1} von {view.of or '?'}",
            until=_number_of(channel.get("until")),
        )
    return None


def agent_warning(agent_id: str, view: _FlowView) -> str | None:
    """„seit 12 min kein Ereignis“ while a turn is silent (L5, docs/09 §4.5)."""
    last = view.silent.get(agent_id)
    if last is None:
        return None
    minutes = int(_data(last).get("seconds") or 0) // 60
    return f"seit {minutes} min kein Ereignis im Turn"


def discussion_state(view: _FlowView) -> DiscussionState | None:
    """Head of the run detail: „Runde 2 von 3“, „Ruhe, Ende in …“, „wartet auf Freigabe“."""
    if view.channel is None:
        return None
    data = _data(view.channel_wait) if view.channel_wait is not None else {}
    reason = str(data.get("reason") or "none")
    of = view.of if view.of is not None else data.get("of")
    of = of if isinstance(of, int) else None
    rounds = f"Runde {view.round} von {of}" if view.round and of else None
    match reason:
        case "idle":
            if data.get("next") == "end":
                text = "Ruhe, Ende"
            else:
                text = f"Ruhe, Runde {view.round + 1}" + (f" von {of}" if of else "")
        case "agents":
            text = "wartet auf wartende Agenten"
        case "approval":
            text = "wartet auf deine Freigabe"
        case "capacity":
            text = "wartet auf freien Platz"
        case "mixed":
            parts = [CHANNEL_WAIT_TEXT.get(str(p), str(p)) for p in data.get("parts") or []]
            text = "wartet auf " + " und ".join(parts)
        case "wake":
            text = "weckt einen Agenten"
        case _:
            text = rounds or "Diskussion läuft"
    return DiscussionState(
        channel=str(view.channel),
        reason=reason,
        text=text,
        until=_number_of(data.get("until")) if reason != "none" else None,
        round=view.round,
        of=of,
    )


def stage_infos(record: RunRecord, resolved: ResolvedRun | None) -> list[StageInfo]:
    """Progress bar of a run from the checkpoints (docs/08 §5.0 `StageInfo`)."""
    entries = record.log.entries
    plan, reached = plan_and_stages(entries)
    if not plan and resolved is not None:
        plan = [s.name for s in resolved.stages]
    ended: dict[str, str] = {}
    attempts: dict[str, dict[int, Attempt]] = {}
    pass_score = resolved.params.get("pass_score") if resolved is not None else None
    pass_value = float(pass_score) if isinstance(pass_score, int | float) else None
    for entry in entries:
        if entry.get("kind") != "checkpoint":
            continue
        data = _data(entry)
        stage = data.get("stage")
        if not isinstance(stage, str) or stage == "plan":
            continue
        if "decision" in data:
            n = data.get("attempt")
            if isinstance(n, int) and n >= 1:
                score = data.get("score")
                decision = str(data.get("decision"))
                attempts.setdefault(stage, {})[n] = Attempt(
                    n=n,
                    avg_score=float(score) if isinstance(score, int | float) else None,
                    pass_score=pass_value,
                    passed=None if decision == "escalated" else decision == "passed",
                )
        elif data.get("phase") == "stage_end":
            ended[stage] = str(data.get("outcome"))
    status = record.status
    current = reached[-1] if reached else None
    paused_escalation = open_escalation_id(record) is not None
    agents_of: dict[str, list[str]] = {}
    if resolved is not None:
        for spec in resolved.stages:
            agents_of.setdefault(spec.name, []).append(spec.agent)
    out = []
    for index, name in enumerate(plan, start=1):
        if name in ended:
            state = StageStatus.FAILED if ended[name] == "stage_failed" else StageStatus.DONE
        elif name == current:
            if paused_escalation:
                state = StageStatus.WAITING_USER
            elif status.is_active:
                state = StageStatus.RUNNING
            else:
                state = StageStatus.FAILED if status is RunStatus.FAILED else StageStatus.DONE
        elif name in reached:
            state = StageStatus.DONE
        else:
            state = StageStatus.TODO
        out.append(
            StageInfo(
                key=name,
                index=index,
                label=name,
                status=state,
                agents=agents_of.get(name, []),
                attempts=[attempts[name][n] for n in sorted(attempts.get(name, {}))],
            )
        )
    return out


_HANDOFF_STATE = {
    RunStatus.DONE: "ist fertig",
    RunStatus.STOPPED: "wurde gestoppt",
    RunStatus.FAILED: "ist fehlgeschlagen",
}


def actions_allowed(
    status: RunStatus,
    *,
    owned: bool,
    foreign_process: bool,
    slimmed: bool = False,
    open_escalation: bool = False,
) -> list[RunAction]:
    """Actions the GUI may offer (W3-01 §3.5): own runs pause, resume, stop; runs of another
    process only stop; finished runs can be slimmed once (archive, delete, pin: W5-01). An open
    escalation is decided on its card, not with „Fortsetzen“ (docs/07 §6)."""
    if status.is_final:
        return [] if slimmed else [RunAction.SLIM]
    if owned:
        if status is RunStatus.RUNNING:
            return [RunAction.PAUSE, RunAction.STOP]
        if status is RunStatus.PAUSED and not open_escalation:
            return [RunAction.RESUME, RunAction.STOP]
        return [RunAction.STOP]
    if foreign_process and status in (RunStatus.PREPARING, RunStatus.RUNNING, RunStatus.PAUSED):
        return [RunAction.STOP]
    return []


def handoff_text(record: RunRecord) -> str:
    if record.status is RunStatus.INTERRUPTED:  # no finalizer, so no summary
        return (
            f"Run {record.run_id} wurde abgebrochen und hat keine Zusammenfassung. Lies die "
            f"Ergebnisse (read_results). Ordner: {record.run_dir}"
        )
    if record.final:
        state = _HANDOFF_STATE.get(record.status, "ist beendet")
        return (
            f"Run {record.run_id} {state}. Lies die Zusammenfassung und die Ergebnisse "
            f"(read_run_summary, read_results). Ordner: {record.run_dir}"
        )
    return f"Run {record.run_id} läuft noch. Schau mit run_status nach, wie weit er ist."


def run_detail(
    record: RunRecord,
    *,
    base_url: str,
    now: float,
    owned: bool,
    agents: list[AgentState] | None = None,
) -> RunDetail:
    """Head of the run detail (docs/08 §6.9)."""
    resolved = record.load_resolved()
    summary = run_summary(record, base_url=base_url, now=now, resolved=resolved, agents=agents)
    items = open_items(record)
    top = None
    if items:
        first = items[0]
        item_id = str(first.get("item_id"))
        top = ApprovalTop(
            id=item_id,
            summary=describe_item(first),
            more=len(items) - 1,
            gui_url=f"{base_url}/freigaben/{item_id}",
        )
    questions = sum(1 for m in record.room.open_questions() if "user" in m.to)
    foreign = not owned and summary.status.is_active and run_lock_held(record.run_dir)
    discussion = None if record.final else discussion_state(flow_view(record.log.entries))
    return RunDetail(
        run=summary,
        open_approvals_top=top,
        questions_to_user=questions,
        decider=resolved.approvals.decider if resolved is not None else Decider.USER,
        actions_allowed=actions_allowed(
            summary.status,
            owned=owned,
            foreign_process=foreign,
            slimmed=summary.slimmed,
            open_escalation=summary.open_escalation,
        ),
        stages=stage_infos(record, resolved),
        discussion=discussion,
        handoff_text=handoff_text(record),
        run_dir=str(record.run_dir),
        task_md=record.task_md,
    )


# --------------------------------------------------------------------------------------------
# Mapping: agents
# --------------------------------------------------------------------------------------------


def _rate_limit_stops(record: Mapping[str, Any] | None) -> bool:
    """A `rate_limit` event that holds the profile up (not `allowed`/`allowed_warning`, V5)."""
    data = record.get("data") if record is not None else None
    status = str(data.get("status") or "") if isinstance(data, Mapping) else ""
    return status not in RATE_LIMIT_PASSING


def _activity(agent: AgentRecord, *, final: bool) -> Activity | None:
    index = agent.transcript
    lines = index.lines
    if not lines:
        return None
    # the last line that says something about the agent (usage lines are bookkeeping, and so
    # is a `rate_limit` line that held nothing up, V5)
    pos = len(lines) - 1
    while pos > 0 and (
        lines[pos].kind == "usage"
        or (lines[pos].kind == "rate_limit" and not _rate_limit_stops(index.read([pos]).get(pos)))
    ):
        pos -= 1
    line = lines[pos]
    wanted = [pos]
    call_pos = index.call_index(line) if line.kind == TOOL_RESULT else None
    if call_pos is not None:
        wanted.append(call_pos)
    records = index.read(wanted)
    record = records.get(pos)
    if record is None:
        return None
    data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
    kind = line.kind
    if kind == TOOL_CALL:
        name = str(data.get("name") or "Werkzeug")
        tool_input = data.get("input") if isinstance(data.get("input"), Mapping) else {}
        running = index.result_index(line) is None and not final
        return Activity(
            kind=ActivityKind.TOOL,
            label=name,
            arg=input_summary(name, tool_input) or None,
            since=line.ts or None,
            running=running,
        )
    if kind == TOOL_RESULT:
        call = records.get(call_pos) if call_pos is not None else None
        cdata = call.get("data") if call and isinstance(call.get("data"), Mapping) else {}
        name = str(cdata.get("name") or "Werkzeug")
        tool_input = cdata.get("input") if isinstance(cdata.get("input"), Mapping) else {}
        return Activity(
            kind=ActivityKind.TOOL,
            label=name,
            arg=input_summary(name, tool_input) or None,
            since=lines[call_pos].ts if call_pos is not None else line.ts,
        )
    if kind == "text":
        return Activity(
            kind=ActivityKind.TEXT,
            label="Text",
            arg=_excerpt(str(data.get("text") or ""), 80),
            since=line.ts or None,
        )
    if kind == "thinking":
        return Activity(kind=ActivityKind.THINKING, label="denkt nach", since=line.ts or None)
    if kind == "turn_start":
        return Activity(kind=ActivityKind.TEXT, label="Auftrag erhalten", since=line.ts or None)
    if kind == "hint":
        return Activity(kind=ActivityKind.TEXT, label="Hinweis aus dem Chat", since=line.ts or None)
    if kind == "error":
        return Activity(
            kind=ActivityKind.ERROR,
            label="Fehler",
            arg=_excerpt(str(data.get("message") or ""), 80),
            since=line.ts or None,
        )
    if kind == "rate_limit":
        resets = data.get("resets_at")
        until = (
            f"weiter um {time.strftime('%H:%M', time.localtime(resets))}"
            if isinstance(resets, int | float)
            else None
        )
        return Activity(
            kind=ActivityKind.BLOCKED,
            label="Nutzungslimit erreicht",
            arg=until or str(data.get("status") or "") or None,
            since=line.ts or None,
        )
    if kind == "turn_end":
        status = str(data.get("status") or "")
        if status == "error":
            return Activity(kind=ActivityKind.ERROR, label="Turn mit Fehler beendet", since=line.ts)
        if final:
            return Activity(kind=ActivityKind.DONE, label="fertig", since=line.ts or None)
        return Activity(
            kind=ActivityKind.WAIT_MESSAGES, label="auf neue Nachrichten", since=line.ts or None
        )
    return None


def _display_state(
    agent_id: str,
    room_status: AgentStatus | None,
    *,
    final: bool,
    last_turn_error: bool,
    items: Sequence[Mapping[str, Any]],
    not_started: bool = False,
) -> tuple[AgentDisplayState, BlockedBy | None]:
    if not_started:
        return AgentDisplayState.NOT_STARTED, None
    if final:
        return (AgentDisplayState.ERROR if last_turn_error else AgentDisplayState.DONE), None
    for item in items:
        request = item.get("request")
        if isinstance(request, Mapping) and request.get("agent_id") == agent_id:
            return AgentDisplayState.WAITING_USER, BlockedBy(
                kind=BlockedKind.APPROVAL,
                approval_id=str(item.get("item_id")),
                summary=describe_item(item),
            )
    if room_status is AgentStatus.BLOCKED:
        return AgentDisplayState.BLOCKED, BlockedBy(
            kind=BlockedKind.APPROVAL, summary="wartet auf eine Freigabe"
        )
    if room_status is AgentStatus.WORKING:
        return AgentDisplayState.WORKING, None
    if room_status is AgentStatus.DONE:
        return AgentDisplayState.DONE, None
    return AgentDisplayState.IDLE, None


def agent_states(record: RunRecord, resolved: ResolvedRun | None = None) -> list[AgentState]:
    """Agent cards of a run (docs/08 §5.0 `AgentState`)."""
    resolved = resolved or record.load_resolved()
    if resolved is None:
        return []
    room = record.room
    store = room.store
    states = {}
    assignments: dict[str, Any] = {}
    results_count: dict[str, int] = {}
    ratings_count: dict[str, int] = {}
    if store is not None:
        try:
            states = {s.agent: s for s in store.agent_states()}
            for result in store.results():
                results_count[result.agent] = results_count.get(result.agent, 0) + 1
            for rating in store.ratings():
                ratings_count[rating.rater] = ratings_count.get(rating.rater, 0) + 1
        except ArrError:
            pass
    questions = room.open_questions() if room.available else []
    items = open_items(record)
    final = record.final
    channels = {c.name: c for c in resolved.groupchats}
    view = flow_view(record.log.entries)
    waits = {
        str(r["agent"]): dict(r)
        for r in record.room.rows("SELECT * FROM agent_wait WHERE kind='wait_for_messages'")
    }
    out = []
    for config in resolved.agents:
        agent = record.agents.get(config.id)
        index = agent.transcript if agent is not None else None
        turn_ends = index.turn_ends() if index is not None else []
        last_error = (
            bool(turn_ends) and str((turn_ends[-1].get("data") or {}).get("status")) == "error"
        )
        room_state = states.get(config.id)
        state, blocked = _display_state(
            config.id,
            room_state.status if room_state is not None else None,
            final=final,
            last_turn_error=last_error,
            items=items,
            not_started=record.status is RunStatus.FAILED
            and (index is None or not index.max_turn()),
        )
        usage, _turns = index.usage() if index is not None else (Usage(), 0)
        cache_ratio = None
        if usage.cache_read and (usage.input or 0) + usage.cache_read > 0:
            cache_ratio = usage.cache_read / ((usage.input or 0) + usage.cache_read)
        assignment = None
        if store is not None and config.id not in assignments:
            try:
                assignment = store.assignment(config.id)
            except ArrError:
                assignment = None
        max_wakeups = [
            channels[name].max_wakeups_per_agent for name in config.channels if name in channels
        ]
        file = agent.file if agent is not None else None
        work_dir = (
            str(record.run_dir / file.work_dir)
            if file is not None
            else str(record.run_dir / "agents" / config.id / "work")
        )
        out.append(
            AgentState(
                id=config.id,
                role_label=role_label(config.role, config.base_id),
                role_file=file.role_file if file is not None else None,
                provider=config.provider,
                vendor=vendor_of(config.provider),
                model=config.model,
                model_label=model_label(config.model),
                auth_profile=config.auth,
                effort=config.effort,
                skills=list(config.skills),
                channels=list(config.channels),
                tools=ToolAccess(shell=config.tools.shell, web=config.tools.web),
                workspace_mode=config.workspace.mode,
                read_dirs=[str(d) for d in config.read_dirs],
                assignment_label=_excerpt(assignment.prompt, 80) if assignment else None,
                state=state,
                state_since=room_state.ts if room_state is not None else None,
                blocked_by=blocked,
                waiting=agent_wait(
                    record,
                    config.id,
                    room_state.status if room_state is not None else None,
                    view,
                    waits,
                ),
                warning=None if final else agent_warning(config.id, view),
                activity=_activity(agent, final=final) if agent is not None else None,
                turn=index.max_turn() if index is not None else 0,
                turns_limit=resolved.limits.turns_per_agent,
                wakeups=room_state.wakeups if room_state is not None else 0,
                max_wakeups=max(max_wakeups) if max_wakeups else None,
                cost_usd=usage.cost_usd,
                cost_kind=usage.cost_kind if usage.cost_usd is not None else None,
                tokens=_tokens(usage),
                cache_read_ratio=cache_ratio,
                open_questions_to=sum(1 for q in questions if config.id in q.to),
                questions_to_user=sum(
                    1 for q in questions if q.sender == config.id and "user" in q.to
                ),
                results_count=results_count.get(config.id, 0),
                ratings_count=ratings_count.get(config.id, 0),
                work_dir=work_dir,
                reachable=not final
                and state is AgentDisplayState.DONE
                and view.channel in config.channels,
            )
        )
    return out


# --------------------------------------------------------------------------------------------
# Mapping: channels, messages, results, log, changes
# --------------------------------------------------------------------------------------------


def channel_infos(record: RunRecord, resolved: ResolvedRun | None = None) -> list[ChannelInfo]:
    """Channels of a run (docs/08 §5.0 `ChannelInfo`)."""
    store = record.room.store
    resolved = resolved or record.load_resolved()
    roles = {a.id: role_label(a.role, a.base_id) for a in resolved.agents} if resolved else {}
    infos = []
    if store is not None:
        try:
            infos = store.channels()
        except ArrError:
            return []
    if not infos:  # the room comes up while the run prepares; until then `run.yaml` tells
        if resolved is None:
            return []
        return [
            ChannelInfo(
                name=c.name,
                purpose=c.purpose,
                members=[ChannelMemberInfo(agent=m, role_label=roles.get(m)) for m in c.members],
                mode=c.mode,
                moderator=c.moderator,
                open=not c.open_from,
                opens_at_stage=c.open_from,
                anonymous=c.anonymous,
            )
            for c in resolved.groupchats
        ]
    stats = record.room.channel_stats()
    out = []
    for info in infos:
        stat = stats.get(info.name, {})
        out.append(
            ChannelInfo(
                name=info.name,
                purpose=info.purpose,
                members=[
                    ChannelMemberInfo(
                        agent=m.agent or "?", alias=m.alias, role_label=roles.get(m.agent or "")
                    )
                    for m in info.members
                ],
                mode=info.mode,
                moderator=info.moderator,
                open=info.open,
                opens_at_stage=info.open_from,
                anonymous=info.anonymous,
                message_count=int(stat.get("count", info.message_count)),
                last_message_id=stat.get("last"),
                open_questions=int(stat.get("open", 0)),
                pinned=list(stat.get("pinned", [])),
            )
        )
    return out


def to_message(msg: RoomMessage, reply_count: int = 0) -> Message:
    """A room message as API `Message` (docs/08 §5.0)."""
    return Message(
        id=msg.id,
        ts=msg.ts,
        channel=msg.channel,
        sender=msg.sender,
        sender_alias=msg.sender_alias,
        via=msg.via,
        kind=msg.kind,
        body=msg.body,
        to=list(msg.to),
        reply_to=msg.reply_to,
        reply_count=reply_count,
        refs=[AttachmentInfo(name=a.name, size=a.size, path=a.path) for a in msg.refs],
        pinned=msg.pinned,
        open=msg.open,
        delivery=DeliveryInfo(status=msg.delivery.status, ts=msg.delivery.ts)
        if msg.delivery is not None
        else None,
    )


def messages_page(
    record: RunRecord,
    channel: str,
    *,
    before_id: int | None,
    since_id: int | None,
    limit: int,
) -> MessagesPage | None:
    """A page of a channel, oldest first within the page; None if the channel is unknown or
    the room is broken. A channel of `run.yaml` the room does not hold yet is empty, not gone."""
    room = record.room
    store = room.store
    limit = max(1, min(limit, 500))
    if store is None and room.available:
        return None
    try:
        names = store.channel_names() if store is not None else []
    except ArrError:
        return None
    if channel not in names:
        resolved = record.load_resolved()
        if resolved is None or all(c.name != channel for c in resolved.groupchats):
            return None
        return MessagesPage(channel=channel, items=[], total=0, older_count=0)
    total = room.count_channel(channel)
    try:
        if since_id is not None:
            rows = store.messages(channel, after_id=since_id, limit=limit)
        else:
            ids = room.page_ids(channel, before_id=before_id, limit=limit)
            rows = store.messages(channel, after_id=ids[0] - 1, limit=len(ids)) if ids else []
    except ArrError:
        return None
    replies = room.reply_counts(m.id for m in rows)
    older = room.count_before(channel, rows[0].id) if rows else total
    return MessagesPage(
        channel=channel,
        items=[to_message(m, replies.get(m.id, 0)) for m in rows],
        total=total,
        older_count=older,
    )


def _result_kind(kind: str) -> ResultKind:
    try:
        return ResultKind(kind)
    except ValueError:
        return ResultKind.OTHER


def _result_body(record: RunRecord, body: str | None, path: str | None) -> str | None:
    if body:
        return _excerpt(body)
    if path:
        file = (record.run_dir / path) if not Path(path).is_absolute() else Path(path)
        try:
            if record.run_dir.resolve() in file.resolve().parents:
                return _excerpt(read_regular(file, 16_000, prefix=True)[:4000])
        except (OSError, ValueError):
            return None
    return None


def run_results(record: RunRecord, resolved: ResolvedRun | None = None) -> RunResults:
    """Results with ratings and the summary (docs/08 §6.9)."""
    resolved = resolved or record.load_resolved()
    store = record.room.store
    items: list[Result] = []
    if store is not None:
        try:
            results = store.results()
            ratings = store.ratings()
        except ArrError:
            results, ratings = [], []
        pass_score = resolved.params.get("pass_score") if resolved is not None else None
        pass_value = float(pass_score) if isinstance(pass_score, int | float) else None
        decisions: dict[tuple[str | None, int], str] = {}
        for entry in record.log.entries:
            data = _data(entry)
            if entry.get("kind") == "checkpoint" and "decision" in data:
                attempt = data.get("attempt")
                if isinstance(attempt, int):
                    decisions[(data.get("stage"), attempt)] = str(data["decision"])
        by_result: dict[int, list[RatingInfo]] = {}
        for rating in ratings:
            by_result.setdefault(rating.result_id, []).append(
                RatingInfo(
                    rater=rating.rater,
                    score=rating.score,
                    criteria=dict(rating.criteria),
                    comment=rating.comment,
                )
            )
        for result in results:
            rs = by_result.get(result.id, [])
            avg = sum(r.score for r in rs) / len(rs) if rs else None
            decision = decisions.get((result.stage, result.attempt))
            if decision == "passed":
                status = ResultStatus.ACCEPTED
            elif decision == "rework":
                status = ResultStatus.REWORK
            elif decision == "failed":
                status = ResultStatus.FAILED
            elif avg is None:
                status = ResultStatus.UNRATED
            elif pass_value is None or avg >= pass_value:
                status = ResultStatus.ACCEPTED
            else:
                status = ResultStatus.REWORK
            items.append(
                Result(
                    id=result.id,
                    ts=result.ts,
                    agent=result.agent,
                    stage=result.stage,
                    stage_label=result.stage,
                    kind=_result_kind(result.kind),
                    title=result.title,
                    body_excerpt=_result_body(record, result.body, result.path),
                    path=result.path,
                    attempt=result.attempt,
                    ratings=rs,
                    avg_score=avg,
                    pass_score=pass_value,
                    status=status,
                )
            )
    summary = record.summary_md
    return RunResults(
        items=items,
        summary_md=summary,
        summary_path=SUMMARY_FILE if summary is not None else None,
        empty_text=None
        if items
        else "Noch kein Ergebnis. Agenten geben Ergebnisse über den Gruppenchat ab "
        "(submit_result); sie erscheinen hier mit ihren Bewertungen.",
    )


def log_entries(record: RunRecord) -> list[LogEntry]:
    """`orchestrator.jsonl` as German lines (view „Ablauf“)."""
    out = []
    for entry in record.log.entries:
        text = describe_entry(entry, record.run_dir)
        if not text:
            continue
        ts = entry.get("ts")
        stage = _data(entry).get("stage")
        out.append(
            LogEntry(
                ts=float(ts) if isinstance(ts, int | float) else 0.0,
                kind=str(entry.get("kind")),
                text=text,
                stage=str(stage) if isinstance(stage, str) and stage != "plan" else None,
            )
        )
    return out


def parse_diff(text: str) -> list[FileChange]:
    """Changed files with added and removed line counts from a `changes.diff`."""
    files: dict[str, FileChange] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            if name == "/dev/null":
                continue
            current = name.removeprefix("b/")
            files.setdefault(current, FileChange(path=current, added=0, removed=0))
        elif line.startswith("--- "):
            name = line[4:].strip()
            if name != "/dev/null":
                current = name.removeprefix("a/")
                files.setdefault(current, FileChange(path=current, added=0, removed=0))
        elif current is not None and line.startswith("+"):
            files[current].added += 1
        elif current is not None and line.startswith("-"):
            files[current].removed += 1
    return list(files.values())


def agent_changes(record: RunRecord) -> list[AgentChanges]:
    """File changes per agent from `agents/<id>/changes.diff` (view „Änderungen“)."""
    out = []
    for agent_id in record.agent_ids():
        path = record.run_dir / "agents" / agent_id / "changes.diff"
        try:
            text = path.read_text("utf-8", errors="replace") if path.is_file() else ""
        except OSError:
            text = ""
        out.append(AgentChanges(agent=agent_id, files=parse_diff(text)))
    return out


def transcript_page(
    record: RunRecord,
    agent_id: str,
    *,
    from_seq: int | None,
    limit: int,
    kinds: Iterable[TranscriptKind] | None,
) -> TranscriptPage | None:
    """A transcript page of an agent, or None if the run has no such agent."""
    agent = record.agents.get(agent_id)
    if agent is None:
        resolved = record.load_resolved()
        if resolved is None or all(a.id != agent_id for a in resolved.agents):
            return None
        return TranscriptPage(agent_id=agent_id, items=[], total=0)
    agent.transcript.refresh()
    return agent.transcript.page(
        agent_id,
        from_seq=from_seq,
        limit=limit,
        kinds=set(kinds) if kinds is not None else None,
        run_dir=record.run_dir,
    )


def transcript_event(record: RunRecord, agent_id: str, line_index: int) -> Any:
    """The bundled event of one line (for the live stream); None for result lines."""
    agent = record.agents.get(agent_id)
    if agent is None:
        return None
    index = agent.transcript
    line = index.lines[line_index]
    result_index = index.result_index(line)
    records = index.read([line_index] + ([result_index] if result_index is not None else []))
    record_line = records.get(line_index)
    if record_line is None:
        return None
    result = records.get(result_index) if result_index is not None else None
    return to_event(record_line, result, run_dir=record.run_dir, agent_id=agent_id)


def checkpoint_text(entry: Mapping[str, Any]) -> str:
    return describe_checkpoint(_data(entry))

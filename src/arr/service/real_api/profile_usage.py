"""„Nachweis je Profil“: what ran through arr on one auth profile (W3-06, docs/08 §5.8).

**All known workfolders (W4-00 B15).** The Not-Aus works in every workfolder through the
saved block, so the record counts in every workfolder arr knows: this service's own and those
in `~/.arr/workfolders.json` (the ones the service was opened with, at most `MAX_RECENT`). Each
row names its workfolder. A workfolder that is missing or cannot be read is listed with the
reason, never skipped in silence; so is a list that cannot be read.

**Source.** `usage.json` of a finished run. A run without one (`interrupted`, or one that is
still active) is summed from its transcripts (only their `turn_end` lines, as `arr.usage`), so
aborted runs and runs of `arr run` count too. Only the agents on this profile count in a mixed
run; which those are says `run.yaml`, so a run without a finished turn shows too, with zero.
A period takes a run by the moment it started.

**Hostile folders.** Agents write into run folders, so everything below a workfolder's `runs/`
is opened relative to its folder (`dir_fd`) with `O_NOFOLLOW`: no link is followed, also not
in a folder on the way. Files open with `O_NONBLOCK`, so a FIFO does not block, and only
regular files are read: small ones up to `SMALL_MAX_BYTES`, transcripts line by line with
lines up to `LINE_MAX_BYTES`. What cannot be read counts as missing; a run folder whose
`meta.json` is there but unreadable is counted as unreadable.

**Off the event loop (B8).** `RealApi` runs `get` in a worker thread, so a cold record over
many large transcripts does not stop the GUI. It shares nothing with the GUI's `RunStore`
(which the event loop changes) and keeps its own caches under a lock.

No use of Claude Code or Codex outside arr: `NOTE` says so on the page.
"""

import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML, YAMLError

from arr.models import AgentConfig, ResolvedRun, RunStatus
from arr.runs.prepare import RunMeta
from arr.service.api_models import (
    ProfileRunUsage,
    ProfileUsage,
    ProfileUsageFolder,
    ProfileUsageSum,
)
from arr.service.real_api.base import ServiceState
from arr.service.state import workfolders_file
from arr.usage import (
    USAGE_FILE,
    AgentUsage,
    RunUsage,
    UsageTotal,
    aggregate_usage,
    usage_records,
)

NOTE = (
    "Erfasst sind die Läufe über arr in allen Workfoldern, die arr kennt (die zuletzt im Dienst "
    "geöffneten), auch aus „arr run“. Workfolder, die nie im Dienst geöffnet wurden, und die "
    "direkte Nutzung von Claude Code oder Codex fehlen hier."
)
ABORTED = (RunStatus.STOPPED, RunStatus.FAILED, RunStatus.INTERRUPTED)
WEEK_S = 7 * 24 * 3600
SMALL_MAX_BYTES = 8 * 1024 * 1024
"""`meta.json`, `run.yaml`, `usage.json` and the workfolder list: larger counts as unreadable."""
LINE_MAX_BYTES = 1024 * 1024
"""A longer transcript line is skipped; a `turn_end` line is a few hundred bytes."""

_TOKENS = ("input", "output", "cache_read", "cache_write", "reasoning")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_TURN_END = b'"turn_end"'

MISSING = "fehlt"
NOT_A_FOLDER = "ist kein Ordner"
UNREADABLE = "lässt sich nicht lesen"
LIST_UNREADABLE = "Die Liste der Workfolder lässt sich nicht lesen. Gezählt ist nur dieser."


# --------------------------------------------------------------------------------------------
# Reading below a workfolder, without following links and without blocking
# --------------------------------------------------------------------------------------------


type Key = tuple[int, int, int]
"""Inode, mtime and size of a file: a cached value is read again when one of them changes."""


def _key(info: os.stat_result) -> Key:
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def _open_dir(name: str, dir_fd: int) -> int | None:
    try:
        return os.open(name, _DIR, dir_fd=dir_fd)
    except OSError:
        return None


def _open_regular(name: str, dir_fd: int | None) -> tuple[int, os.stat_result] | None:
    """An open regular file and its `fstat`; None for a link, FIFO, folder or error."""
    try:
        fd = os.open(name, _FILE, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None
    return fd, info


def _exists(name: str, dir_fd: int) -> bool:
    """True if anything is there under `name`, also a dangling link."""
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_fd(fd: int, limit: int) -> bytes | None:
    """At most `limit` bytes; None if the file is longer or cannot be read."""
    chunks: list[bytes] = []
    size = 0
    try:
        while chunk := os.read(fd, min(1 << 20, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                return None
    except OSError:
        return None
    return b"".join(chunks)


def _turn_end_lines(fd: int) -> Iterator[dict[str, Any]]:
    """The `turn_end` records of a transcript; damaged and overlong lines are skipped."""
    with os.fdopen(os.dup(fd), "rb") as handle:
        skipping = False
        while line := handle.readline(LINE_MAX_BYTES):
            complete = line.endswith(b"\n")
            if skipping or not complete and len(line) >= LINE_MAX_BYTES:
                skipping = not complete
                continue
            if _TURN_END not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _parse_meta(data: bytes) -> RunMeta:
    return RunMeta.model_validate_json(data)


def _parse_run_yaml(data: bytes) -> ResolvedRun:
    return ResolvedRun.model_validate(YAML(typ="safe").load(data.decode("utf-8")))


def _parse_usage(data: bytes) -> RunUsage:
    return RunUsage.model_validate_json(data)


_PARSE_ERRORS = (ValueError, ValidationError, YAMLError, UnicodeDecodeError, TypeError)


@dataclass(slots=True)
class _Run:
    """What the record needs of one run folder."""

    run_id: str
    workfolder: Path
    current: bool
    meta: RunMeta
    agents: dict[str, AgentConfig]
    """Agents of `run.yaml`; empty if it cannot be read."""
    usage: RunUsage | None


class _Reader:
    """Cached, link-free reading of run folders (see the module docstring). Not thread-safe
    on its own: `ProfileUsageArea` holds its lock while it reads."""

    def __init__(self) -> None:
        self._files: dict[tuple[str, str], tuple[Key, Any]] = {}
        self._settled: dict[str, tuple[Key, RunUsage]] = {}
        """Usage summed from the transcripts of an ended run (keyed by its `meta.json`)."""

    def small(
        self, folder: str, name: str, dir_fd: int | None, parse: Callable[[bytes], Any]
    ) -> tuple[bool, Any]:
        """(found, value): `found` is False if there is no such regular file; `value` is
        None if it could not be read or parsed."""
        opened = _open_regular(name, dir_fd)
        if opened is None:
            self._files.pop((folder, name), None)
            return False, None
        fd, info = opened
        try:
            ident, key = (folder, name), _key(info)
            known = self._files.get(ident)
            if known is not None and known[0] == key:
                return True, known[1]
            value = None
            if info.st_size <= SMALL_MAX_BYTES and (data := _read_fd(fd, SMALL_MAX_BYTES)):
                try:
                    value = parse(data)
                except _PARSE_ERRORS:
                    value = None
            self._files[ident] = (key, value)
            return True, value
        finally:
            os.close(fd)

    def runs(self, workfolder: Path, *, current: bool) -> tuple[list[_Run], int, str | None]:
        """The readable runs of a workfolder, the number of unreadable run folders, and why
        the workfolder itself could not be read (None if it could)."""
        try:
            info = os.stat(workfolder)  # the workfolder itself is the user's choice
        except FileNotFoundError:
            return [], 0, MISSING
        except OSError:
            return [], 0, UNREADABLE
        if not stat.S_ISDIR(info.st_mode):
            return [], 0, NOT_A_FOLDER
        try:
            root = os.open(workfolder, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return [], 0, UNREADABLE
        try:
            try:  # `runs` may be a link the user made, like the workfolder; nothing below it
                runs_fd = os.open("runs", os.O_RDONLY | os.O_DIRECTORY, dir_fd=root)
            except FileNotFoundError:
                return [], 0, None  # no run yet
            except OSError:
                return [], 0, UNREADABLE
            try:
                return self._runs_in(workfolder, runs_fd, current)
            finally:
                os.close(runs_fd)
        finally:
            os.close(root)

    def _runs_in(
        self, workfolder: Path, runs_fd: int, current: bool
    ) -> tuple[list[_Run], int, str | None]:
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(runs_fd)
                if entry.is_dir(follow_symlinks=False) and _RUN_ID.fullmatch(entry.name)
            )
        except OSError:
            return [], 0, UNREADABLE
        runs: list[_Run] = []
        unreadable = 0
        for run_id in names:
            run_fd = _open_dir(run_id, runs_fd)
            if run_fd is None:
                unreadable += 1
                continue
            try:
                run = self._run(workfolder, run_id, run_fd, current)
            finally:
                os.close(run_fd)
            if run is None:
                continue
            if run is False:
                unreadable += 1
                continue
            runs.append(run)
        return runs, unreadable, None

    def _run(
        self, workfolder: Path, run_id: str, run_fd: int, current: bool
    ) -> "_Run | None | bool":
        folder = str(workfolder / "runs" / run_id)
        found, meta = self.small(folder, "meta.json", run_fd, _parse_meta)
        if not found:
            # no regular meta.json: not a run yet (being reserved), unless something else is
            # there in its place (a link, a FIFO, a folder)
            return None if not _exists("meta.json", run_fd) else False
        if meta is None:
            return False
        _, resolved = self.small(folder, "run.yaml", run_fd, _parse_run_yaml)
        agents = {a.id: a for a in resolved.agents} if resolved is not None else {}
        usage = None
        if meta.status.is_final:
            _, usage = self.small(folder, USAGE_FILE, run_fd, _parse_usage)
        if usage is None and agents:
            usage = self._transcript_usage(folder, run_id, run_fd, meta, agents)
        return _Run(run_id, workfolder, current, meta, agents, usage)

    def _transcript_usage(
        self, folder: str, run_id: str, run_fd: int, meta: RunMeta, agents: dict[str, AgentConfig]
    ) -> RunUsage:
        meta_key = self._files[(folder, "meta.json")][0]
        settled = self._settled.get(folder)
        if settled is not None and settled[0] == meta_key and meta.status.is_final:
            return settled[1]
        records = []
        agents_fd = _open_dir("agents", run_fd)
        if agents_fd is not None:
            try:
                for agent_id, config in agents.items():
                    records += self._agent_records(agents_fd, agent_id, config)
            finally:
                os.close(agents_fd)
        usage = aggregate_usage(records, run_id=run_id, agents=sorted(agents))
        if meta.status.is_final:
            self._settled[folder] = (meta_key, usage)
        return usage

    @staticmethod
    def _agent_records(agents_fd: int, agent_id: str, config: AgentConfig) -> list[Any]:
        if "/" in agent_id or agent_id in (".", ".."):
            return []
        agent_fd = _open_dir(agent_id, agents_fd)
        if agent_fd is None:
            return []
        try:
            opened = _open_regular("transcript.jsonl", agent_fd)
            if opened is None:
                return []
            fd, _info = opened
            try:
                return usage_records(_turn_end_lines(fd), config, agent=agent_id)
            except OSError:
                return []
            finally:
                os.close(fd)
        finally:
            os.close(agent_fd)

    def workfolder_list(self) -> tuple[list[Path], bool]:
        """The workfolders of `workfolders.json` and whether the list could be read (a
        missing list is no problem: then only the current workfolder is known)."""
        path = workfolders_file()
        found, value = self.small(str(path.parent), str(path), None, json.loads)
        if not found:
            return [], not os.path.lexists(path)
        items = value.get("workfolders") if isinstance(value, dict) else None
        if not isinstance(items, list):
            return [], False
        return [Path(item) for item in items if isinstance(item, str) and item], True


# --------------------------------------------------------------------------------------------
# Summing
# --------------------------------------------------------------------------------------------


def _sum_fields(total: UsageTotal) -> dict[str, Any]:
    tokens = {name: getattr(total.usage, name) or 0 for name in _TOKENS}
    return tokens | {
        "turns": total.turns,
        "cost_billed_usd": total.cost_billed_usd,
        "cost_estimated_usd": total.cost_estimated_usd,
        "cost_unknown_turns": total.cost_unknown_turns,
    }


def _added(total: UsageTotal, part: UsageTotal) -> UsageTotal:
    return UsageTotal(
        usage=total.usage + part.usage,
        turns=total.turns + part.turns,
        cost_billed_usd=total.cost_billed_usd + part.cost_billed_usd,
        cost_estimated_usd=total.cost_estimated_usd + part.cost_estimated_usd,
        cost_unknown_turns=total.cost_unknown_turns + part.cost_unknown_turns,
    )


def _agents_on(usage: RunUsage | None, name: str, planned: dict[str, str]) -> dict[str, AgentUsage]:
    """Agents of the run on profile `name`. An agent without a finished turn has no profile in
    the usage, so `planned` (`run.yaml`) names it: a run that is still in its first turn,
    or ended before one finished, shows with zero (review W3-06, B4)."""
    known = usage.by_agent if usage is not None else {}
    agents = {agent: item for agent, item in known.items() if item.profile == name}
    for agent, profile in planned.items():
        if profile == name and known.get(agent, AgentUsage()).profile is None:
            agents[agent] = known.get(agent) or AgentUsage(profile=name)
    return agents


def _period(label: str, rows: list[tuple[ProfileRunUsage, UsageTotal]]) -> ProfileUsageSum:
    total = UsageTotal()
    for _, part in rows:
        total = _added(total, part)
    return ProfileUsageSum(label=label, runs=len(rows), **_sum_fields(total))


def _row(run: _Run, name: str) -> tuple[ProfileRunUsage, UsageTotal] | None:
    planned = {agent: config.auth for agent, config in run.agents.items() if config.auth}
    agents = _agents_on(run.usage, name, planned)
    if not agents:
        return None
    total = UsageTotal()
    for item in agents.values():
        total = _added(total, item)
    meta = run.meta
    row = ProfileRunUsage(
        label=run.run_id,
        runs=1,
        run_id=run.run_id,
        status=meta.status,
        agents=sorted(agents),
        started=meta.started or meta.created,
        ended=meta.ended,
        aborted=meta.status in ABORTED,
        active=meta.status.is_active,
        url=f"/runs/{run.run_id}" if run.current else "",
        workfolder=str(run.workfolder),
        **_sum_fields(total),
    )
    return row, total


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


class ProfileUsageArea:
    """`get_profile_usage` of `ApiClient`; `find` raises for an unknown profile."""

    def __init__(self, state: ServiceState, find: Callable[[str], Any]) -> None:
        self.state = state
        self.find = find
        self._reader = _Reader()
        self._lock = threading.Lock()

    def _workfolders(self) -> tuple[list[Path], bool]:
        """This service's workfolder first, then the known others, each once."""
        current = self.state.workfolder
        others, readable = self._reader.workfolder_list()
        folders = [current]
        for folder in others:
            folder = folder.expanduser()
            if folder.is_absolute() and not any(_same(folder, known) for known in folders):
                folders.append(folder)
        return folders, readable

    def get(self, name: str, *, now: datetime | None = None) -> ProfileUsage:
        """The record of `name`; blocking file reads, so the service calls it in a thread."""
        self.find(name)
        moment = now or datetime.now()
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        week = moment.timestamp() - WEEK_S
        rows: list[tuple[ProfileRunUsage, UsageTotal]] = []
        folders: list[ProfileUsageFolder] = []
        with self._lock:
            workfolders, list_readable = self._workfolders()
            for index, workfolder in enumerate(workfolders):
                runs, unreadable, problem = self._reader.runs(workfolder, current=index == 0)
                found = [row for run in runs if (row := _row(run, name)) is not None]
                rows += found
                folders.append(
                    ProfileUsageFolder(
                        path=str(workfolder),
                        name=workfolder.name or str(workfolder),
                        current=index == 0,
                        runs=len(found),
                        unreadable_runs=unreadable,
                        problem=problem,
                    )
                )
        if not list_readable:
            path = workfolders_file()
            folders.append(
                ProfileUsageFolder(path=str(path), name=path.name, problem=LIST_UNREADABLE)
            )
        rows.sort(key=lambda item: -(item[0].started or 0))
        return ProfileUsage(
            profile=name,
            today=_period("Heute", [r for r in rows if (r[0].started or 0) >= midnight]),
            week=_period("7 Tage", [r for r in rows if (r[0].started or 0) >= week]),
            total=_period("Gesamt", rows),
            runs=[row for row, _ in rows],
            note=NOTE,
            workfolders=folders,
        )

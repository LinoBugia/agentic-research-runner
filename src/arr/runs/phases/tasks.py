"""Assignment files of a phases run: read from the run folder, checked, placeholders inserted,
filed (W4b-02b §6.2; docs/10 §4.3; Entscheidung 45, 48).

Every `task` of an action and of a rating action is a file. The runner reads it at its start
from the snapshot in the run folder, never from the workfolder: first from the skill
(`skills/<skill>/`), then from the project (`base/`), the order `prepare_run` uses for roles.
The rules of roles hold: a relative path without `..`, a regular file, no link out of its
folder; the file has at most `MAX_TASK_BYTES` and is UTF-8 text. Placeholders
`{{ params.<name> }}` are inserted with the final parameters by the rule of
`arr.resolve._substitute` for longer text. Every phase is checked before the first turn; all
problems come at once as a `ConfigError` naming phase, path in the YAML and file.

The texts are filed in the run folder (`tasks/`), one Markdown file per assignment and
`tasks/index.json` listing them (phase, kind, key, agents, the path in the YAML, where the
file was found, the filed copy). Agents get the text in the prompt and through
`get_assignment`, never as a file.

A run without a skill (W4b-06b, F1: the agent `solver` with one phase `arbeit`) has no skill
and usually no assignment in its project: its one assignment `NO_SKILL_TASK` comes from arr's
package (`templates/run/ohne-skill.md`). Only a run without a skill reads it, and only under
this one name; a run with a skill never does.
"""

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from arr.errors import ConfigError, ConfigIssue
from arr.models import ResolvedRun, ResolvedTask
from arr.resolve import _substitute, format_loc
from arr.runs.phases.text import MAX_TASK_BYTES, line
from arr.submissions.text import clean_text

__all__ = ["NO_SKILL_TASK", "TASKS_DIR", "TASKS_INDEX", "TaskText", "Tasks", "load_tasks"]

TASKS_DIR = "tasks"
TASKS_INDEX = "index.json"
KIND_WORK = "work"
KIND_GATE = "gate"
NO_SKILL_TASK = "tasks/ohne-skill.md"
"""The assignment of a run without a skill; its text comes from the package (F1)."""
NO_SKILL_LABEL = "arr"
"""Where the assignment of a run without a skill was found (`tasks/index.json`)."""

_GUARD = "\x00"
"""Put in front of a text so that a file that is exactly one placeholder is still „longer
text“ for `_substitute` (the value is inserted as text, never kept as a number or list). A
task file never holds this character: such a file is refused as not being text."""


@dataclass(frozen=True)
class TaskText:
    """One assignment: of the action `key` of a phase (`kind` `work`) or of its gate (`gate`)."""

    phase: str
    index: int
    kind: str
    key: str
    agents: list[str]
    source: str
    found_in: str
    file: str
    text: str


Tasks = dict[tuple[str, str, str], TaskText]
"""Assignments by (phase, kind, key); the gate's has the key ``""``."""


def _problem(rel: str, folders: list[tuple[str, Path]]) -> tuple[Path | None, str, str | None]:
    """The file of `rel` in the first folder that has it: (path, folder label, problem)."""
    path = PurePosixPath(rel)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in rel:
        return None, "", "liegt außerhalb des Skills und des Projekts (relativer Pfad ohne „..“)"
    for label, folder in folders:
        candidate = folder / rel
        if not os.path.lexists(candidate):
            continue
        try:
            resolved = candidate.resolve()
            inside = resolved.is_relative_to(folder.resolve())
        except (OSError, RuntimeError):
            return None, label, "lässt sich nicht auflösen (Kreis aus Links?)"
        if not inside:
            return None, label, f"ist ein Link aus „{label}/“ hinaus"
        if not resolved.is_file():
            return None, label, "ist keine normale Datei"
        return resolved, label, None
    where = " und ".join(f"„{label}/“" for label, _ in folders)
    return None, "", f"gibt es nicht (gesucht in {where})"


def _read(path: Path) -> tuple[str | None, str | None]:
    """Text of a task file, or a problem; no link followed, at most `MAX_TASK_BYTES`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, "lässt sich nicht öffnen"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "ist keine normale Datei"
        if info.st_size > MAX_TASK_BYTES:
            return None, f"ist größer als {MAX_TASK_BYTES // 1024} KB"
        data = os.read(fd, MAX_TASK_BYTES + 1)
    except OSError:
        return None, "lässt sich nicht lesen"
    finally:
        os.close(fd)
    if len(data) > MAX_TASK_BYTES:
        return None, f"ist größer als {MAX_TASK_BYTES // 1024} KB"
    if b"\x00" in data:
        return None, "ist keine Textdatei"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "ist keine Textdatei in UTF-8"


def _entries(run: ResolvedRun) -> list[tuple[Any, str, str, list[str], ResolvedTask, tuple]]:
    out = []
    for phase in run.phases:
        keys: dict[str, list[str]] = {}
        tasks: dict[str, ResolvedTask] = {}
        for action in phase.actions:
            keys.setdefault(action.key, []).append(action.agent)
            tasks.setdefault(action.key, action)
        for key, agents in keys.items():
            loc = ("phases", phase.index, "actions", key, "task")
            out.append((phase, KIND_WORK, key, agents, tasks[key], loc))
        if phase.gate.action is not None:
            loc = ("phases", phase.index, "gate", "action", "task")
            out.append((phase, KIND_GATE, "", list(phase.gate.raters), phase.gate.action, loc))
    return out


def _no_skill_text() -> str:
    """The assignment of a run without a skill, from the package (F1)."""
    from arr.workfolder import read_template  # late: keeps this module light

    return read_template("run", "ohne-skill.md")


def load_tasks(run_folder: Path, run: ResolvedRun, params: Mapping[str, Any]) -> Tasks:
    """Read, check and insert every assignment of every phase; file them under `tasks/`.

    Raises `ConfigError` listing every problem (phase, path in the YAML, file) before
    anything is filed. Blocking file access: call it outside the event loop.
    """
    folders: list[tuple[str, Path]] = []
    if run.skill:
        folders.append((f"skills/{run.skill}", run_folder / "skills" / run.skill))
    folders.append(("base", run_folder / "base"))
    issues: list[ConfigIssue] = []
    found: list[tuple[Any, str, str, list[str], str, str, str, str]] = []
    cache: dict[str, tuple[str | None, str, str | None]] = {}
    for phase, kind, key, agents, task, loc in _entries(run):
        path_text = format_loc(loc)
        shown = line(task.task, 120)
        where = f"Phase „{phase.name}“: Der Auftrag „{shown}“"
        if task.task not in cache and not run.skill and task.task == NO_SKILL_TASK:
            cache[task.task] = (_no_skill_text(), NO_SKILL_LABEL, None)
        if task.task not in cache:
            path, label, problem = _problem(task.task, folders)
            text = None
            if path is not None:
                text, problem = _read(path)
            cache[task.task] = (text, label, problem)
        text, label, problem = cache[task.task]
        if text is None:
            issues.append(ConfigIssue(path_text, f"{where} {problem}."))
            continue
        local: list[ConfigIssue] = []
        inserted = _substitute(_GUARD + text, params, loc, local)
        if local:
            issues += [ConfigIssue(path_text, f"{where}: {i.message}") for i in local]
            continue
        assert isinstance(inserted, str)
        if len(inserted[1:].encode("utf-8")) > MAX_TASK_BYTES:  # the limit holds after inserting
            issues.append(
                ConfigIssue(
                    path_text,
                    f"{where} ist nach dem Einsetzen der Platzhalter größer als "
                    f"{MAX_TASK_BYTES // 1024} KB.",
                )
            )
            continue
        found.append((phase, kind, key, agents, task.task, label, path_text, inserted[1:]))
    if issues:
        raise ConfigError(issues, source="run.yaml")
    target = run_folder / TASKS_DIR
    target.mkdir(parents=True, exist_ok=True)
    tasks: Tasks = {}
    counters: dict[tuple[int, str], int] = {}
    for phase, kind, key, agents, source, label, _path, text in found:
        number = counters[(phase.index, kind)] = counters.get((phase.index, kind), 0) + 1
        name = f"{phase.index:02d}-{phase.name}-{kind}-{number}.md"
        clean = clean_text(text)
        (target / name).write_text(clean, encoding="utf-8")
        tasks[(phase.name, kind, key)] = TaskText(
            phase=phase.name,
            index=phase.index,
            kind=kind,
            key=key,
            agents=agents,
            source=source,
            found_in=label,
            file=f"{TASKS_DIR}/{name}",
            text=clean,
        )
    index = [{k: v for k, v in asdict(t).items() if k != "text"} for t in tasks.values()]
    (target / TASKS_INDEX).write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return tasks

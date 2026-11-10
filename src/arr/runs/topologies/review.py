"""Topology `review`: an artifact is reviewed by independent judges, discussed, judged and, if
needed, revised by its author. See docs/03-skills-und-research-groups.md §4.5.

Phases (`PHASES`, the plan of the run; `open_from` must name one of them, W4-03 F2):

0. `artefakt`: the first version comes from the project (`artifact`, a path in the project,
   read from the clean snapshot `base/`) or the author writes it (`submit_result`, a file or
   text). arr keeps every version under `output/artifacts/v<n>/` (F7).
1. `pruefung`: the judges review in parallel and independently (F4): the judges channel is
   closed, their results are sealed (files in `groupchats/sealed/`, readable for no agent)
   until the phase ends; file names and packages name judges by their alias. With
   `judge_memory: fresh` every round from the second on starts fresh sessions (F5). A review
   gives a verdict (`Urteil: accept|minor|major|reject`), objections and notes „Für die nächste
   Runde“; without a review or verdict arr asks once more, then goes on with a note.
2. `diskussion`: the channel opens, `discussion_rounds` rounds with the channel's pacing
   (skipped with 0 rounds or one judge). A new review in the discussion replaces the verdict.
3. `urteil`: `majority` (the most frequent verdict, the stricter on a tie) or `unanimous`
   (`accept` only if all accept, else the strictest), F6. Only a checkpoint, no turn.
4. `revision`: not accepted and revisions left: the author gets verdict, objections and
   reviews and submits a new version; arr writes the diff (`difflib`), back to 1.
5. `zusammenfassung`: arr builds an overview (verdict, rounds, open objections) into the
   checkpoint and `summary.md`; with `summarizer` that agent writes a summary (F9).

The context package of a judge (F7): `task`, `round`, `version`, `artifact` (absolute `path`,
`excerpt` of at most 4 KB), `diff` (path), `objections` (ID, text, status, by, round: built by
arr from the reviews, without a model), `previous` (the notes „Für die nächste Runde“ of the
last round) and, in the discussion, `reviews` (by alias, verdict, path, short excerpt).

Checkpoints: `artefakt` (`version`, `path`, `source`), `pruefung` per round at the start and
with `phase: "end"` and the verdicts, `diskussion` (start by `discuss`, end), `urteil`
(`round`, `verdict`, `rule`, `verdicts`, `accepted`), `revision` (`round`, `version`, `diff`
or `reason`), `zusammenfassung` (`verdict`, `reason`, `rounds`, `overview`).

Parameters: `max_revisions` (2), `discussion_rounds` (1), `verdict` (`majority` |
`unanimous`; `meta_judge` comes with `moderated` in wave 5), `judge_memory` (`fresh` |
`continue`), `artifact` (none), `author` (default: the agent `author`), `judges` (a number is
only for `count`; an ID, pattern or list names them, default: the channel's members except
author and summarizer), `channel` (`judges`), `summarizer` (none).
"""

import asyncio
import contextlib
import difflib
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from arr.errors import ArrError, ConfigIssue
from arr.models import ChannelMode, ResolvedRun, WorkspaceMode
from arr.room.types import Result
from arr.runs.context import RunContext
from arr.runs.topologies import (
    agent_param,
    agents_param,
    ask_for_result,
    choice_param,
    describe,
    ensure_valid,
    int_param,
    phase_issues,
    require_task,
    suggest,
)
from arr.workfolder import read_template

PHASES = ("artefakt", "pruefung", "diskussion", "urteil", "revision", "zusammenfassung")
ARTEFAKT, PRUEFUNG, DISKUSSION, URTEIL, REVISION, ZUSAMMENFASSUNG = PHASES
VERDICTS = ("accept", "minor", "major", "reject")
"""From mild to strict."""
RULES = ("majority", "unanimous")
MEMORIES = ("fresh", "continue")
CHANNEL = "judges"
MAX_REVISIONS = 2
DISCUSSION_ROUNDS = 1
EXCERPT_BYTES = 4 * 1024
"""Inline excerpt of the artifact in a package (F7); everything else only as a path."""
REVIEW_EXCERPT = 1000
NOTE_CHARS = 1000
OBJECTION_CHARS = 300
MAX_OBJECTIONS = 100
MAX_TEXT_BYTES = 1024 * 1024
"""Versions larger than this get no excerpt (read with this limit)."""
MAX_DIFF_BYTES = 200 * 1024
MAX_DIFF_LINES = 2000
"""A diff only between versions up to these sizes: `difflib` grows much faster than the text
(review W4-03 H2: repeated lines, 128 KB took minutes). Larger versions get a note instead."""
DIFF_TIMEOUT_S = 10.0
"""The diff runs in a process of its own; after this long arr ends it and writes a note."""
PARSE_CHARS = 64 * 1024
"""arr reads at most this much of a review (body and file together) for verdict, objections
and notes; title at most `TITLE_CHARS`, a line at most `LINE_CHARS` (H2: linear, bounded)."""
TITLE_CHARS = 500
LINE_CHARS = 2000

_VERDICT_AT = re.compile(
    r"(?:urteil|verdict)[ \t*_]*[:=][ \t*_]*(accept|minor|major|reject)\b", re.IGNORECASE
)
_VERDICT_WORD = re.compile(r"\b(accept|minor|major|reject)\b", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*•]|\d{1,4}[.)])[ \t]+(.*)$")
_REF = re.compile(
    r"^\[?(E\d{1,4})\]?[ \t]*[:–—-]?[ \t]*(offen|behoben|strittig|open|resolved|disputed)?"
    r"[ \t]*[:–—-]?[ \t]*(.*)$",
    re.IGNORECASE,
)
_STATUS = {
    "offen": "offen",
    "open": "offen",
    "behoben": "behoben",
    "resolved": "behoben",
    "strittig": "strittig",
    "disputed": "strittig",
}
_REASON_TEXT = {
    "accepted": "angenommen",
    "revisions_used": "keine Revision mehr übrig",
    "no_revision": "der Autor hat keine neue Fassung abgegeben",
    "no_verdict": "kein Judge hat ein Urteil abgegeben",
    "no_author": "ohne Autor keine Revision",
    "no_artifact": "es gab kein Artefakt",
}


# --------------------------------------------------------------------------------------------
# Parsing what judges write (docs/09 principle 3)
# --------------------------------------------------------------------------------------------


def unified_diff(before: str, after: str, old_name: str, new_name: str) -> str:
    """The unified diff of two texts, in this process (`DIFF_PROGRAM` does the same apart)."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=old_name,
        tofile=new_name,
    )
    return "".join(lines)


DIFF_PROGRAM = (
    "import difflib, json, sys\n"
    "d = json.load(sys.stdin)\n"
    "sys.stdout.write(''.join(difflib.unified_diff(d['a'].splitlines(True), "
    "d['b'].splitlines(True), fromfile=d['n'][0], tofile=d['n'][1])))\n"
)
"""The diff as a program of its own: the texts come as data on stdin, nothing is run from
them; `python -I` loads nothing from the current folder or the environment."""


async def diff_elsewhere(
    before: str,
    after: str,
    names: tuple[str, str],
    *,
    timeout_s: float,
    stopped: Callable[[], bool],
) -> str | None:
    """The unified diff in a process of its own (H2): the event loop stays free, and arr ends
    the process (by its handle) after `timeout_s` or as soon as `stopped()` is true; then None."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        DIFF_PROGRAM,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    data = json.dumps({"a": before, "b": after, "n": list(names)}).encode("utf-8")
    talk = asyncio.ensure_future(process.communicate(data))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while not talk.done():
            left = deadline - loop.time()
            if left <= 0 or stopped():
                return None
            await asyncio.wait({talk}, timeout=min(0.1, left))
        out, _ = talk.result()
        return out.decode("utf-8", errors="replace") if process.returncode == 0 else None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if not talk.done():
            talk.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await talk


def _lines(text: str) -> list[str]:
    """The lines arr reads of a text: at most `PARSE_CHARS`, each cut to `LINE_CHARS`, without
    quoted lines (`>`) and fenced code blocks, which cite other texts (G2). Linear in the
    text; every pattern below runs on one short line and has no nested repetition (H2)."""
    lines, fenced = [], False
    for raw in text[:PARSE_CHARS].splitlines():
        line = raw[:LINE_CHARS].strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or line.startswith(">"):
            continue
        lines.append(line)
    return lines


def parse_verdict(title: str, text: str) -> str | None:
    """The verdict of a review: the first line `Urteil: x` in title or text (not quoted), else
    the only verdict word of the title; None if there is none."""
    title = title[:TITLE_CHARS]
    for source in (title, text):
        for line in _lines(source):
            found = _VERDICT_AT.match(line.lstrip("*_#- \t"))
            if found:
                return found.group(1).lower()
    words = {w.lower() for w in _VERDICT_WORD.findall(title)}
    return words.pop() if len(words) == 1 else None


def _heading(line: str) -> str | None:
    """The text of a heading line (`# x`, `**x**`, `x:` up to 40 characters), else None.
    Plain string operations, no backtracking pattern (H2)."""
    if line.startswith("#"):
        text = line.lstrip("#").strip().rstrip("#").strip()
    elif line.startswith("**") and line.rstrip(":").rstrip().endswith("**") and len(line) > 4:
        text = line.rstrip(":").rstrip()[2:-2].strip()
    elif line.endswith(":") and len(line) <= 41:
        text = line[:-1].strip()
    else:
        return None
    return text.casefold() if text else None


def _sections(text: str) -> dict[str, list[str]]:
    """Lines per section: `einwaende`, `naechste` (others are dropped)."""
    out: dict[str, list[str]] = {"einwaende": [], "naechste": []}
    current: str | None = None
    for line in _lines(text):
        head = _heading(line) if line and not _BULLET.match(line) else None
        if head is not None:
            if "einwänd" in head or "einwaend" in head or "objection" in head:
                current = "einwaende"
            elif "nächste runde" in head or "naechste runde" in head or "next round" in head:
                current = "naechste"
            elif line.startswith("#") or line.startswith("**"):
                current = None
            continue
        if current is not None:
            out[current].append(line)
    return out


def parse_objections(text: str) -> list[tuple[str | None, str | None, str]]:
    """The points of the section „Einwände“: `(ref, status, text)`, `ref` like `E3` when the
    point refers to a known objection (`- [E3] behoben: …`)."""
    points = []
    for line in _sections(text)["einwaende"]:
        bullet = _BULLET.match(line)
        if not bullet:
            continue
        item = bullet.group(1).strip()
        ref = _REF.match(item)
        if ref and item.lstrip("[").upper().startswith("E") and ref.group(1):
            status = _STATUS.get((ref.group(2) or "").lower())
            points.append((ref.group(1).upper(), status, ref.group(3).strip()))
        elif item:
            points.append((None, None, item))
    return points


def parse_next_round(text: str) -> str:
    """The section „Für die nächste Runde“, cut to `NOTE_CHARS`."""
    note = "\n".join(_sections(text)["naechste"]).strip()
    return note if len(note) <= NOTE_CHARS else note[:NOTE_CHARS] + " …"


def aggregate(verdicts: list[str | None], rule: str) -> str | None:
    """F6: `majority` takes the most frequent verdict, on a tie the stricter; `unanimous`
    takes `accept` only if every judge gave `accept`, else the strictest given verdict, at
    least `minor` when a verdict is missing (G1). None without any verdict."""
    given = [v for v in verdicts if v is not None]
    if not given:
        return None
    if rule == "unanimous":
        if len(given) == len(verdicts) and all(v == "accept" for v in given):
            return "accept"
        return max([*given, "minor"], key=VERDICTS.index)
    counts = Counter(given)
    top = max(counts.values())
    return max((v for v, n in counts.items() if n == top), key=VERDICTS.index)


# --------------------------------------------------------------------------------------------
# Check
# --------------------------------------------------------------------------------------------


def _author(resolved: ResolvedRun) -> str | None:
    value = resolved.params.get("author")
    if isinstance(value, str):
        return value
    return next((a.id for a in resolved.agents if "author" in (a.id, a.base_id)), None)


def _channel(resolved: ResolvedRun) -> str:
    value = resolved.params.get("channel")
    return value if isinstance(value, str) else CHANNEL


def _judges(resolved: ResolvedRun, issues: list[ConfigIssue]) -> list[str]:
    value = resolved.params.get("judges")
    others = {_author(resolved), resolved.params.get("summarizer")}
    if isinstance(value, str | list):
        found = agents_param(resolved, "judges", issues)
    else:
        try:
            found = list(resolved.channel(_channel(resolved)).members)
        except KeyError:
            found = []
    return [j for j in found if j not in others]


def check(resolved: ResolvedRun) -> list[ConfigIssue]:
    """Problems of the `review` parameters, roles and channels (see `check_topology`)."""
    issues: list[ConfigIssue] = []
    params = resolved.params
    max_revisions = int_param(params, "max_revisions", MAX_REVISIONS, issues)
    int_param(params, "discussion_rounds", DISCUSSION_ROUNDS, issues)
    if params.get("verdict") == "meta_judge":
        issues.append(
            ConfigIssue(
                "params.verdict",
                "„verdict: meta_judge“ ist noch nicht wählbar: Der Meta-Judge braucht die "
                "Taktung „moderated“, die mit Welle 5 kommt. Nimm „majority“ oder „unanimous“.",
            )
        )
    else:
        choice_param(params, "verdict", RULES, "majority", issues)
    choice_param(params, "judge_memory", MEMORIES, "fresh", issues)
    artifact = params.get("artifact")
    if artifact is not None:
        parts = PurePosixPath(artifact).parts if isinstance(artifact, str) else ()
        if not parts or artifact.startswith("/") or ".." in parts:
            issues.append(
                ConfigIssue(
                    "params.artifact",
                    f"Parameter „artifact“ muss ein Pfad im Projekt sein (relativ, ohne „..“), "
                    f"gefunden {describe(artifact)}.",
                )
            )
    if "author" in params:
        agent_param(resolved, "author", issues)
    author = _author(resolved)
    if author is None and (artifact is None or max_revisions > 0):
        issues.append(
            ConfigIssue(
                "params.author",
                "„review“ braucht einen Autor, der das Artefakt schreibt oder überarbeitet. Lege "
                "einen Agenten „author“ an oder nenne ihn im Parameter „author“ (ohne Autor "
                "nur mit „artifact“ und „max_revisions: 0“).",
            )
        )
    agent_param(resolved, "summarizer", issues)
    name = _channel(resolved)
    names = [c.name for c in resolved.groupchats]
    judges = _judges(resolved, issues)
    if name not in names:
        issues.append(
            ConfigIssue(
                "params.channel",
                f"„review“ braucht den Kanal der Judges „{name}“.{suggest(name, names)} "
                f"Vorhanden: {', '.join(names) or 'keine'}.",
            )
        )
    else:
        config = resolved.channel(name)
        if config.mode is ChannelMode.MODERATED:
            issues.append(
                ConfigIssue(
                    "params.channel",
                    f"Der Kanal „{name}“ ist moderiert. Die Taktung „moderated“ kommt mit Welle "
                    "5, nimm „rounds“ oder „event“.",
                )
            )
        outside = [j for j in judges if j not in config.members]
        if outside:
            issues.append(
                ConfigIssue(
                    "params.judges",
                    f"Die Judges {', '.join(outside)} sind nicht Mitglied im Kanal „{name}“ und "
                    "könnten nicht mitdiskutieren. Nimm sie in „members“ auf.",
                )
            )
        for index, chat in enumerate(resolved.groupchats):
            shared = [j for j in judges if j in chat.members]
            if chat.name != name and len(shared) > 1:
                issues.append(
                    ConfigIssue(
                        f"groupchats[{index}].members",
                        f"Die Judges {', '.join(shared)} teilen sich den Kanal „{chat.name}“. "
                        "Für die unabhängige Prüfung dürfen Judges nur den Kanal "
                        f"„{name}“ gemeinsam haben.",
                    )
                )
    if not judges:
        issues.append(
            ConfigIssue("params.judges", f"„review“ braucht mindestens einen Judge in „{name}“.")
        )
    issues += _independence_issues(resolved, judges)
    issues += phase_issues(resolved, PHASES)
    return issues


def _independence_issues(resolved: ResolvedRun, judges: list[str]) -> list[ConfigIssue]:
    """F4 in the configuration: judges share no working folder and read no run folder of
    another agent (`read_dirs` with `run:` outside `output/`)."""
    issues = []
    folders: dict[str, list[str]] = {}
    for index, agent in enumerate(resolved.agents):
        if agent.id not in judges:
            continue
        if agent.workspace.mode is WorkspaceMode.SHARED:
            folders.setdefault(str(agent.workspace.name), []).append(agent.id)
        for entry in agent.read_dirs:
            if entry.startswith("run:") and not entry.removeprefix("run:").startswith("output"):
                issues.append(
                    ConfigIssue(
                        f"agents[{index}].read_dirs",
                        f"Der Judge „{agent.id}“ liest den Run-Ordner „{entry}“. Für die "
                        "unabhängige Prüfung lesen Judges im Run nur ihren Arbeitsordner und "
                        "„output/“.",
                    )
                )
    for folder, shared in folders.items():
        if len(shared) > 1:
            issues.append(
                ConfigIssue(
                    "agents",
                    f"Die Judges {', '.join(shared)} teilen den Arbeitsordner „{folder}“. Für die "
                    "unabhängige Prüfung braucht jeder Judge einen eigenen.",
                )
            )
    return issues


# --------------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------------


class _Stopped(Exception):
    """The run stops (user stop or limit): the topology returns."""


@dataclass(slots=True)
class _Version:
    n: int
    path: str
    source: str
    result: int | None = None
    diff: str | None = None


@dataclass(slots=True)
class _Objection:
    id: str
    text: str
    by: str
    round: int
    status: str = "offen"


@dataclass(slots=True)
class _Review:
    judge: str
    result: Result | None = None
    verdict: str | None = None


@dataclass(slots=True)
class _Round:
    n: int
    version: int
    verdicts: dict[str, str | None]
    verdict: str | None


class _ReviewRun:
    def __init__(self, ctx: RunContext) -> None:
        ensure_valid(ctx, check)
        self.ctx = ctx
        params = ctx.params
        self.task = require_task(ctx)
        self.author = _author(ctx.config)
        self.channel = _channel(ctx.config)
        self.judges = _judges(ctx.config, [])
        self.summarizer = params.get("summarizer")
        self.artifact = params.get("artifact")
        self.rule = str(params.get("verdict") or "majority")
        self.memory = str(params.get("judge_memory") or "fresh")
        self.max_revisions = int_param(params, "max_revisions", MAX_REVISIONS, [])
        self.rounds = int_param(params, "discussion_rounds", DISCUSSION_ROUNDS, [])
        self.objections: list[_Objection] = []
        self.notes: dict[str, str] = {}
        self.history: list[_Round] = []
        self.guide = read_template("prompts", "review_judge.md").replace("{channel}", self.channel)

    def _check_stop(self) -> None:
        if self.ctx.stop_requested:
            raise _Stopped

    async def run(self) -> None:
        try:
            await self._run()
        except _Stopped:
            return

    async def _run(self) -> None:
        self.ctx.close_channel(self.channel)
        version = await self._first_version()
        self._check_stop()
        if version is None:
            await self._finish(None, "no_artifact", None)
            return
        revisions = 0
        while True:
            number = len(self.history) + 1
            reviews = await self._review(number, version)
            self._check_stop()
            await self._discuss(number, version, reviews)
            self._check_stop()
            verdict = await self._judge(number, version, reviews)
            if verdict == "accept":
                reason = "accepted"
            elif verdict is None:
                reason = "no_verdict"
            elif self.author is None:
                reason = "no_author"
            elif revisions >= self.max_revisions:
                reason = "revisions_used"
            else:
                revisions += 1
                new = await self._revise(number, version, verdict, reviews)
                self._check_stop()
                if new is not None:
                    version = new
                    continue
                reason = "no_revision"
            break
        await self._finish(verdict, reason, version)

    # -- names, files ---------------------------------------------------------------------

    def _alias(self, judge: str) -> str:
        """The name of a judge in every package and file, also the author's and the
        summarizer's: its alias in the judges channel (F4), the ID if it is not anonymous."""
        return self.ctx.shown_name(judge, judge)

    def _label(self, judge: str) -> str:
        alias = self._alias(judge)
        return alias if alias == judge else f"{judge} ({alias})"

    def _abs(self, rel: str) -> str:
        return str(self.ctx.run_folder / rel)

    def _file_ref(self, rel: str) -> dict[str, Any]:
        ref: dict[str, Any] = {"path": self._abs(rel)}
        text = self.ctx.read_output(rel, MAX_TEXT_BYTES)
        if text is not None:
            data = text.encode("utf-8")
            ref["excerpt"] = data[:EXCERPT_BYTES].decode("utf-8", errors="ignore")
            ref["complete"] = len(data) <= EXCERPT_BYTES
        return ref

    # -- versions -------------------------------------------------------------------------

    async def _first_version(self) -> _Version | None:
        if self.artifact:
            name = PurePosixPath(self.artifact).name
            try:
                path = self.ctx.save_output(f"artifacts/v1/{name}", source=f"base/{self.artifact}")
            except ArrError as exc:
                await self.ctx.checkpoint(
                    ARTEFAKT, version=None, reason="artifact_unreadable", error=str(exc)
                )
                return None
            version = _Version(1, path, "project")
        else:
            assert self.author is not None, "checked by ensure_valid"
            prompt = (
                f"## Aufgabe\n\n{self.task}\n\nSchreibe das Artefakt zu dieser Aufgabe, z. B. "
                "einen Beweis, als Datei in deinem Arbeitsordner, und gib es mit "
                '`submit_result(kind="artifact", path="<Datei>")` ab (oder den Text in `body`). '
                "Danach prüfen es unabhängige Judges."
            )
            result = await ask_for_result(
                self.ctx,
                self.author,
                prompt,
                'Du hast das Artefakt noch nicht abgegeben. Gib es jetzt mit `submit_result(kind="'
                'artifact", path="<Datei>")` ab.',
                stage=ARTEFAKT,
                context={"task": self.task, "version": 1},
            )
            self._check_stop()
            if result is None:
                await self.ctx.checkpoint(ARTEFAKT, version=None, reason="no_result")
                return None
            version = await self._version_from(1, result, None, ARTEFAKT)
            if version is None:
                return None
        await self.ctx.checkpoint(
            ARTEFAKT,
            version=1,
            path=version.path,
            source=version.source,
            result=version.result,
        )
        return version

    async def _version_from(
        self, n: int, result: Result, previous: _Version | None, stage: str
    ) -> _Version | None:
        """Version `n` from the author's result: its file, else the file `artifact` in the
        author's working copy (when set), else the text (F7). Copies are safe (no links)."""
        folder = f"artifacts/v{n}"
        try:
            if result.path:
                suffix = PurePosixPath(result.path).suffix
                path = self.ctx.save_output(f"{folder}/artefakt{suffix}", source=result.path)
                source = "result_file"
            elif self.artifact and self.author:
                name = PurePosixPath(self.artifact).name
                path = self.ctx.save_output(
                    f"{folder}/{name}", agent=self.author, source=self.artifact
                )
                source = "work_copy"
            else:
                path = self.ctx.save_output(f"{folder}/artefakt.md", text=result.body or "")
                source = "result_text"
        except ArrError as exc:
            await self.ctx.checkpoint(
                stage, version=None, reason="artifact_unreadable", error=str(exc)
            )
            return None
        version = _Version(n, path, source, result.id)
        if previous is not None:
            version.diff = await self._diff(previous, version)
        return version

    async def _diff(self, old: _Version, new: _Version) -> str:
        """`output/artifacts/v<n>/diff.patch`, made by arr itself with `difflib` (F7).

        Only for text versions up to `MAX_DIFF_BYTES` and `MAX_DIFF_LINES`, in a process of its
        own with `DIFF_TIMEOUT_S` (`diff_elsewhere`), so no version holds the event loop or a
        stop (H2); otherwise a note."""
        before = self.ctx.read_output(old.path, MAX_TEXT_BYTES)
        after = self.ctx.read_output(new.path, MAX_TEXT_BYTES)
        names = (
            f"v{old.n}/{PurePosixPath(old.path).name}",
            f"v{new.n}/{PurePosixPath(new.path).name}",
        )
        compare = (
            f"Vergleiche die Fassungen direkt: {self._abs(old.path)} und {self._abs(new.path)}."
        )
        if before is None or after is None:
            text = (
                f"Kein Diff von v{old.n} zu v{new.n}: Eine Fassung ist kein Text oder größer "
                f"als {MAX_TEXT_BYTES // 1024} KB. {compare}\n"
            )
        elif any(
            len(t.encode("utf-8")) > MAX_DIFF_BYTES or t.count("\n") > MAX_DIFF_LINES
            for t in (before, after)
        ):
            text = (
                f"Kein Diff von v{old.n} zu v{new.n}: Eine Fassung ist größer als "
                f"{MAX_DIFF_LINES} Zeilen oder {MAX_DIFF_BYTES // 1024} KB. {compare}\n"
            )
        else:
            text = await diff_elsewhere(
                before,
                after,
                names,
                timeout_s=DIFF_TIMEOUT_S,
                stopped=lambda: self.ctx.stop_requested,
            )
            if text is None:
                text = (
                    f"Kein Diff von v{old.n} zu v{new.n}: Er dauerte länger als "
                    f"{DIFF_TIMEOUT_S:g} s oder der Run wurde gestoppt. {compare}\n"
                )
            text = text or f"Keine Änderung von v{old.n} zu v{new.n}.\n"
        return self.ctx.save_output(f"artifacts/v{new.n}/diff.patch", text=text)

    # -- (1) independent review -------------------------------------------------------------

    def _package(self, viewer: str, number: int, version: _Version) -> dict[str, Any]:
        package: dict[str, Any] = {
            "task": self.task,
            "round": number,
            "version": version.n,
            "artifact": self._file_ref(version.path),
            "verdicts": list(VERDICTS),
            "objections": [
                {
                    "id": o.id,
                    "text": o.text,
                    "status": o.status,
                    "by": self._alias(o.by),
                    "round": o.round,
                }
                for o in self.objections[:MAX_OBJECTIONS]
            ],
            "previous": [
                {"by": self._alias(judge), "round": number - 1, "next_round": note}
                for judge, note in self.notes.items()
                if note
            ],
        }
        if version.diff:
            package["diff"] = {"path": self._abs(version.diff)}
        return package

    async def _review(self, number: int, version: _Version) -> dict[str, _Review]:
        if number > 1 and self.memory == "fresh":
            for judge in self.judges:
                await self.ctx.reset_session(judge)
        self.ctx.close_channel(self.channel)
        self.ctx.seal_results(PRUEFUNG, self.channel)
        mark = max((r.id for r in self.ctx.results()), default=0)
        await self.ctx.checkpoint(
            PRUEFUNG, round=number, version=version.n, judges=self.judges, memory=self.memory
        )
        try:
            reviews = await asyncio.gather(
                *(self._one_review(judge, number, version, mark) for judge in self.judges)
            )
        finally:  # also after a stop or an error: no review stays hidden in the run folder
            self.ctx.release_results(PRUEFUNG)
        found = {r.judge: r for r in reviews}
        self._take_objections(number, found)
        await self.ctx.checkpoint(
            PRUEFUNG,
            phase="end",
            round=number,
            version=version.n,
            verdicts={j: r.verdict for j, r in found.items()},
            reviews={j: r.result.id if r.result else None for j, r in found.items()},
            missing=[j for j, r in found.items() if r.verdict is None],
        )
        return found

    def _verdict(self, result: Result) -> str | None:
        return parse_verdict(result.title, self.ctx.result_text(result.id))

    async def _one_review(self, judge: str, number: int, version: _Version, mark: int) -> _Review:
        what = f"Prüfrunde {number}, Fassung {version.n}"
        prompt = (
            f"{what}: Prüfe das Artefakt unabhängig von den anderen Judges. Aufgabe, Fassung"
            + (", Diff zur Vorfassung" if version.diff else "")
            + " und Einwände stehen in `get_assignment()`. Gib dein Gutachten mit "
            '`submit_result(kind="review")` ab.\n\n' + self.guide
        )

        def again(last: Result | None) -> str:
            if last is None:
                return (
                    f"Du hast für {what} noch kein Gutachten abgegeben. Gib es jetzt mit "
                    '`submit_result(kind="review")` ab, mit der Zeile `Urteil: accept|minor|'
                    "major|reject`."
                )
            return (
                f"Dein Gutachten #{last.id} nennt kein Urteil. Gib es noch einmal ab, mit der "
                "Zeile `Urteil: accept`, `Urteil: minor`, `Urteil: major` oder `Urteil: reject`."
            )

        result = await ask_for_result(
            self.ctx,
            judge,
            prompt,
            again,
            stage=PRUEFUNG,
            context=self._package(judge, number, version),
            accept=lambda r: self._verdict(r) is not None,
        )
        if result is None:  # a review without verdict still counts, with a note
            mine = [r for r in self.ctx.results(agent=judge, stage=PRUEFUNG) if r.id > mark]
            return _Review(judge, mine[-1] if mine else None, None)
        return _Review(judge, result, self._verdict(result))

    def _take_objections(self, number: int, reviews: dict[str, _Review]) -> None:
        """The objection list from the structured reviews, without a model (F7): new points
        get an ID; on a known one judges say offen/behoben/strittig, and if they disagree it is
        „strittig“."""
        known = {o.id: o for o in self.objections}
        updates: dict[str, set[str]] = {}
        self.notes = {}
        for judge, review in reviews.items():
            if review.result is None:
                continue
            text = self.ctx.result_text(review.result.id)
            self.notes[judge] = parse_next_round(text)
            for ref, status, body in parse_objections(text):
                if ref is not None and ref in known:
                    updates.setdefault(ref, set()).add(status or "offen")
                elif body and len(self.objections) < MAX_OBJECTIONS:
                    if len(body) > OBJECTION_CHARS:
                        body = body[:OBJECTION_CHARS] + " …"
                    objection = _Objection(f"E{len(self.objections) + 1}", body, judge, number)
                    self.objections.append(objection)
        for ref, statuses in updates.items():
            known[ref].status = statuses.pop() if len(statuses) == 1 else "strittig"

    def _review_refs(self, viewer: str, number: int, reviews: dict[str, _Review]) -> list[Any]:
        refs = []
        for judge, review in reviews.items():
            ref: dict[str, Any] = {"by": self._alias(judge), "verdict": None}
            if review.result is not None:
                result = review.result
                rel = result.path
                if rel is None:
                    label = "".join(c if c.isalnum() else "-" for c in self._alias(judge).lower())
                    rel = self.ctx.save_output(
                        f"review/runde-{number}/gutachten-{label}.md", text=result.body or ""
                    )
                text = self.ctx.result_text(result.id, MAX_TEXT_BYTES)
                excerpt = text[:REVIEW_EXCERPT] + (" …" if len(text) > REVIEW_EXCERPT else "")
                ref |= {"verdict": review.verdict, "path": self._abs(rel), "excerpt": excerpt}
            refs.append(ref)
        return refs

    # -- (2) discussion, (3) verdict ------------------------------------------------------

    async def _discuss(self, number: int, version: _Version, reviews: dict[str, _Review]) -> None:
        self.ctx.open_channel(self.channel)
        if self.rounds == 0 or len(self.judges) < 2:
            return
        viewer = self.judges[0]  # all judges see the same aliases in the channel
        package = self._package(viewer, number, version)
        package["reviews"] = self._review_refs(viewer, number, reviews)
        mark = max((r.id for r in self.ctx.results()), default=0)
        prompt = (
            f"Diskussion zu Prüfrunde {number} in #{self.channel}: Die Gutachten aller Judges "
            "stehen jetzt in `get_assignment()` unter `reviews`, die Einwände unter "
            "`objections`. Besprecht strittige Einwände und das Urteil. Ändert sich dein "
            'Urteil, gib ein neues Gutachten mit `submit_result(kind="review")` und der Zeile '
            "`Urteil: …` ab. Hast du nichts mehr beizutragen, melde `done`."
        )
        outcome = await self.ctx.discuss(
            self.channel, prompt, stage=DISKUSSION, rounds=self.rounds, context=package
        )
        changed = []
        for judge in self.judges:
            newer = [
                r
                for r in self.ctx.results(agent=judge, stage=DISKUSSION)
                if r.id > mark and self._verdict(r) is not None
            ]
            if newer:
                verdict = self._verdict(newer[-1])
                if verdict != reviews[judge].verdict:
                    changed.append(judge)
                reviews[judge] = _Review(judge, newer[-1], verdict)
        await self.ctx.checkpoint(
            DISKUSSION,
            phase="end",
            round=number,
            ended_by=outcome.ended_by.value,
            changed=changed,
        )

    async def _judge(
        self, number: int, version: _Version, reviews: dict[str, _Review]
    ) -> str | None:
        verdicts = {j: r.verdict for j, r in reviews.items()}
        verdict = aggregate(list(verdicts.values()), self.rule)
        self.history.append(_Round(number, version.n, verdicts, verdict))
        await self.ctx.checkpoint(
            URTEIL,
            round=number,
            version=version.n,
            verdict=verdict,
            rule=self.rule,
            verdicts=verdicts,
            accepted=verdict == "accept",
        )
        return verdict

    # -- (4) revision ---------------------------------------------------------------------

    async def _revise(
        self, number: int, version: _Version, verdict: str, reviews: dict[str, _Review]
    ) -> _Version | None:
        assert self.author is not None
        package = self._package(self.author, number, version)
        package["verdict"] = verdict
        package["reviews"] = self._review_refs(self.author, number, reviews)
        where = (
            f"Überarbeite die Datei „{self.artifact}“ in deinem Arbeitsordner"
            if self.artifact
            else "Überarbeite das Artefakt"
        )
        prompt = (
            f"Revision nach Prüfrunde {number}: Das Urteil ist „{verdict}“. Gutachten, Einwände "
            f"mit Status und Fassung {version.n} stehen in `get_assignment()`. {where} und gib "
            f'Fassung {version.n + 1} mit `submit_result(kind="artifact", path="<Datei>")` ab. '
            "Hältst du einen Einwand für falsch, begründe das im Ergebnis."
        )
        result = await ask_for_result(
            self.ctx,
            self.author,
            prompt,
            f"Du hast Fassung {version.n + 1} noch nicht abgegeben. Gib sie jetzt mit "
            '`submit_result(kind="artifact")` ab.',
            stage=REVISION,
            context=package,
        )
        self._check_stop()
        if result is None:
            await self.ctx.checkpoint(REVISION, round=number, version=None, reason="no_revision")
            return None
        new = await self._version_from(version.n + 1, result, version, REVISION)
        if new is None:
            return None
        await self.ctx.checkpoint(
            REVISION,
            round=number,
            version=new.n,
            path=new.path,
            diff=new.diff,
            result=result.id,
        )
        return new

    # -- (5) summary ----------------------------------------------------------------------

    def _overview(
        self, verdict: str | None, reason: str, version: _Version | None, name: Any
    ) -> str:
        lines = [
            f"**Urteil:** {verdict or 'keines'} (Regel {self.rule}) · "
            f"{_REASON_TEXT.get(reason, reason)}"
        ]
        if version is not None:
            lines.append(f"**Letzte Fassung:** v{version.n}, `{version.path}`")
        if self.history:
            lines += ["", "| Runde | Fassung | Urteile | Ergebnis |", "|---|---|---|---|"]
            for item in self.history:
                given = ", ".join(f"{name(j)}: {v or '–'}" for j, v in item.verdicts.items())
                lines.append(f"| {item.n} | v{item.version} | {given} | {item.verdict or '–'} |")
        still = [o for o in self.objections if o.status != "behoben"]
        lines += ["", f"**Offene Einwände:** {len(still)} von {len(self.objections)}"]
        lines += [f"- {o.id} ({o.status}, {name(o.by)}): {o.text}" for o in still]
        return "\n".join(lines)

    async def _finish(self, verdict: str | None, reason: str, version: _Version | None) -> None:
        overview = self._overview(verdict, reason, version, self._label)
        await self.ctx.checkpoint(
            ZUSAMMENFASSUNG,
            verdict=verdict,
            reason=reason,
            rounds=len(self.history),
            version=version.n if version else None,
            open_objections=[o.id for o in self.objections if o.status != "behoben"],
            overview=overview,
        )
        if not self.summarizer or self.ctx.stop_requested:
            return
        context: dict[str, Any] = {
            "task": self.task,
            "overview": self._overview(verdict, reason, version, self._alias),
        }
        if version is not None:
            context["artifact"] = {"path": self._abs(version.path)}
        await self.ctx.turn(
            self.summarizer,
            "Der Review ist beendet. Fasse ihn zusammen: Urteil, Verlauf der Runden, offene "
            "Einwände. Die Übersicht steht in `get_assignment()`. Gib die Zusammenfassung mit "
            '`submit_result(kind="summary")` ab.',
            stage=ZUSAMMENFASSUNG,
            context=context,
        )


async def run(ctx: RunContext) -> None:
    """Run `review` (docs/03 §4.5)."""
    await _ReviewRun(ctx).run()

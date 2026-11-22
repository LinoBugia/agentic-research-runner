"""Helpers for W4b-06a: the start skills as phases against the old topologies (P6).

Both sides run with FakeBackend agents whose turns a Python policy writes (`phases_kit`):

- the **old side** runs the old `group.yaml` of a start skill from
  `tests/fixtures/start_skills_old/<name>/` (the trees of `81523c5`) with its old topology,
  `solo` as run data; its policies hand in with `submit_result`/`submit_rating`,
- the **new side** runs the real start skill from the package (`start_skill_files`), `solo` as
  `tests/fixtures/phases/solo.yaml`; its policies hand in with `submit`.

`old_flow` and `new_flow` turn a finished run into the same shape, already on the names of the
phases (the target flow, „Soll-Ablauf“): turns per section, submissions per phase and agent,
named items and their versions, gate results and routes, the end, what the user has. The old
side writes or checks it as `tests/fixtures/template_flows/<template>-<case>.json`
(`ARR_WRITE_TEMPLATE_FLOWS=1` writes; a normal run only checks); the new side compares with it
(`compare`), except the deviations it names (`Deviation`).
"""

import json
import os
import re
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phases_kit import TASK, Log, Policy, policy_factory, silent
from plan_kit import SETTINGS
from run_kit import FAST, make_settings, room

from arr.models import GroupSpec, RunInput, TopologyKind
from arr.resolve import load_group_spec, parse_yaml_text, resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.finalize import SUMMARY_FILE, finalize_run
from arr.runs.manager import RunManager
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.phases import runner as phase_runner
from arr.skills import install_start_skills, start_skill_files

TESTS = Path(__file__).parent
OLD_SKILLS = TESTS / "fixtures" / "start_skills_old"
FLOWS = TESTS / "fixtures" / "template_flows"
SOLO = TESTS / "fixtures" / "phases" / "solo.yaml"
WRITE_FLOWS = "ARR_WRITE_TEMPLATE_FLOWS"
"""Set to 1 to (re)write the target flows from the old topologies; never happens otherwise."""
TOOL_SKILLS = ("sympy-check", "latex-writeup")
PROJECT_PROOF = "beweis.md"
"""A proof in the project `demo` for the parameter `artifact`."""
PROJECT_PROOF_TEXT = "Beweis aus dem Projekt"
"""Its heading: the comparison looks for it in the turns (the seed's text, W4b-06a.3)."""

# --------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------


def _write_tree(target: Path, files: Mapping[str, bytes]) -> None:
    if target.exists():
        shutil.rmtree(target)
    for rel, data in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _tree(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _prepare(workfolder: Path) -> None:
    install_start_skills(workfolder, TOOL_SKILLS)
    project = workfolder / "projects" / "demo" / PROJECT_PROOF
    if not project.exists():
        project.write_text(f"# {PROJECT_PROOF_TEXT}\n\nTrivial.\n", encoding="utf-8")


def _manager(
    workfolder: Path,
    name: str,
    policies: Mapping[str, Policy],
    params: Mapping[str, Any] | None,
    decider: Any,
    log: Log | None,
    topology: Any,
) -> tuple[RunManager, Log]:
    log = log or Log()
    spec = load_group_spec(workfolder / "skills" / name / "group.yaml")
    run_input = RunInput(project="demo", task=TASK, skill=name)
    settings = make_settings().model_copy(update={"defaults": SETTINGS.defaults})
    resolved = resolve_run(run_input, spec, settings, dict(params or {}))
    mgr = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=topology(resolved),
        backend_factory=policy_factory(policies, log),
        settings=make_settings(),
        pacing=FAST,
        decider=decider,
        finalizer=finalize_run,
    )
    return mgr, log


def old_manager(
    workfolder: Path,
    name: str,
    policies: Mapping[str, Policy],
    *,
    params: Mapping[str, Any] | None = None,
    decider: Any = None,
    log: Log | None = None,
) -> tuple[RunManager, Log]:
    """The old start skill `name` (tree of `81523c5`) with its old topology."""
    from arr.runs.topologies import topology_for  # the old side only (W4b-06b removes it)

    _prepare(workfolder)
    _write_tree(workfolder / "skills" / name, _tree(OLD_SKILLS / name))
    return _manager(
        workfolder,
        name,
        policies,
        params,
        decider,
        log,
        lambda resolved: topology_for(resolved.topology.kind),
    )


def new_manager(
    workfolder: Path,
    name: str,
    policies: Mapping[str, Policy],
    *,
    params: Mapping[str, Any] | None = None,
    decider: Any = None,
    log: Log | None = None,
    files: Mapping[str, bytes] | None = None,
) -> tuple[RunManager, Log]:
    """The real start skill `name` from the package (or `files` instead, for a counter-check)
    with the phase runner."""
    _prepare(workfolder)
    _write_tree(workfolder / "skills" / name, files or start_skill_files(name))

    def topology(resolved: Any) -> Any:
        assert resolved.topology.kind is TopologyKind.PHASES, name
        return phase_runner.run

    return _manager(workfolder, name, policies, params, decider, log, topology)


def old_solo_manager(
    workfolder: Path, policies: Mapping[str, Policy], *, max_turns: int = 3
) -> tuple[RunManager, Log]:
    """The old topology `solo` as run data: one agent `worker`, `max_turns`."""
    from arr.runs.topologies import topology_for

    log = Log()
    data = {
        "project": "demo",
        "task": TASK,
        "topology": {"kind": "solo", "params": {"max_turns": max_turns}},
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
        "agents": [{"id": "worker"}],
    }
    run_input = validate_model(RunInput, data)
    resolved = resolve_run(run_input, None, make_settings())
    mgr = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=topology_for("solo"),
        backend_factory=policy_factory(policies, log),
        settings=make_settings(),
        pacing=FAST,
        finalizer=finalize_run,
    )
    return mgr, log


def new_solo_manager(
    workfolder: Path, policies: Mapping[str, Policy], *, max_turns: int = 3
) -> tuple[RunManager, Log]:
    """`tests/fixtures/phases/solo.yaml` as the skill `solo` (roles and tasks as stubs)."""
    log = Log()
    text = SOLO.read_text(encoding="utf-8")
    data, _ = parse_yaml_text(text, "solo.yaml")
    skill = workfolder / "skills" / "solo"
    _write_tree(
        skill,
        {
            "group.yaml": text.encode(),
            "SKILL.md": b"---\nname: solo\ndescription: solo als Phase\n---\nTest.\n",
            "roles/worker.md": b"Rolle worker.\n",
            "tasks/arbeit.md": b"Bearbeite die Aufgabe.\n",
        },
    )
    spec = validate_model(GroupSpec, dict(data), source="group.yaml")
    run_input = RunInput(project="demo", task=TASK, skill="solo")
    settings = make_settings().model_copy(update={"defaults": SETTINGS.defaults})
    resolved = resolve_run(run_input, spec, settings, {"max_turns": max_turns})
    mgr = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=phase_runner.run,
        backend_factory=policy_factory({**policies}, log),
        settings=make_settings(),
        pacing=FAST,
        finalizer=finalize_run,
    )
    return mgr, log


# --------------------------------------------------------------------------------------------
# Policies of the old side (`submit_result`, `submit_rating`)
# --------------------------------------------------------------------------------------------


def old_result(kind: str, title: str = "Ergebnis", body: str | None = None, path: str = "") -> Any:
    args: dict[str, Any] = {"kind": kind, "title": title}
    if body is not None:
        args["body"] = body
    if path:
        args["path"] = path
    return room("submit_result", **args)


def old_review(verdict: str, objections: list[str] | None = None, known: str = "") -> str:
    lines = [f"Urteil: {verdict}", "", "## Einwände", ""]
    lines += [f"- {o}" for o in objections or []]
    if known:
        lines.append(known)
    return "\n".join(lines) + "\n"


def write_file(view: Any, name: str, text: str) -> None:
    (view.work_dir / name).write_text(text, encoding="utf-8")


def old_rate(score: float, ref: str) -> dict[str, Any]:
    return room("submit_rating", result_id=ref, score=score, comment="begründet")


def nothing(view: Any) -> list[dict[str, Any]]:
    """A turn that hands in nothing (the agent stays silent)."""
    return silent(view)


# --------------------------------------------------------------------------------------------
# The flow of a run on the names of the phases
# --------------------------------------------------------------------------------------------

OLD_KIND_FORMAT = {
    "artifact": "artifact",
    "review": "review",
    "note": "artifact",
    "idea": "list",
    "exploration": "text",
    "summary": "text",
}
"""Kind of an old result → format of the submission that replaces it."""


def _entries(mgr: RunManager) -> list[dict[str, Any]]:
    return list(OrchestratorLog(mgr.run_dir).entries())


def _sections(seq: list[tuple[str, int, str]]) -> list[list[Any]]:
    """Consecutive turns of the same (phase, visit) as one section with a count per agent."""
    out: list[list[Any]] = []
    for phase, visit, agent in seq:
        if out and out[-1][0] == phase and out[-1][1] == visit:
            out[-1][2][agent] = out[-1][2].get(agent, 0) + 1
        else:
            out.append([phase, visit, {agent: 1}])
    return out


def _summary_file(mgr: RunManager) -> bool:
    return (mgr.run_dir / SUMMARY_FILE).is_file()


def _summary(mgr: RunManager) -> dict[str, Any]:
    """What `summary.md` says about the end: the line `Grund` and the lines of the stages with
    a threshold (H3). „Phase“ in a phases run is „Stufe“ in an old one; result numbers differ
    by design and read `#n`."""
    path = mgr.run_dir / SUMMARY_FILE
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    reason = next((line for line in text.splitlines() if line.startswith("- **Grund:**")), "")
    reason = reason.replace("Die Phase „", "Die Stufe „")
    outcomes = ("bestanden", "nicht bestanden", "bester Versuch übernommen")
    stages = [
        re.sub(r"#\d+", "#n", line)
        for line in text.splitlines()
        if re.match(r"- \*\*[\w-]+:\*\* ", line) and line.split(":** ", 1)[1].startswith(outcomes)
    ]
    return {"reason": reason, "stage_lines": stages}


class _Sight:
    """What an agent sees in a turn: names of its inputs („Eingaben“, mapped per template onto
    the same words for both sides), the open group channels it is a member of, whether the
    submissions of its phase are sealed (M2 of the review), and which texts of the seeds and
    the list of ideas appear in its prompt or context package („texte“, W4b-06a.3 NP1)."""

    def __init__(self, mgr: RunManager, log: Any, words: Sequence[str] = ()) -> None:
        self.words = list(words)
        resolved = mgr.prepared.resolved if mgr.prepared else None
        chats = list(resolved.groupchats) if resolved else []
        self.members = {c.name: set(c.members) for c in chats}
        self.open = {c.name for c in chats if not c.open_from}
        self.sealed: set[str] = set()
        self.views: dict[str, list[Any]] = {}
        for view in log.views if log is not None else []:
            self.views.setdefault(view.agent, []).append(view)
        self.seen: Counter[str] = Counter()
        self.found: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}

    def event(self, kind: str, data: Mapping[str, Any], phase_of: Any) -> None:
        channel = str(data.get("channel") or "")
        if kind == "channel_open" and channel in self.members:
            self.open.add(channel)
        elif kind == "channel_close":
            self.open.discard(channel)
        elif kind == "results_sealed":
            self.sealed.add(phase_of(data.get("stage")))
        elif kind == "results_released":
            self.sealed.discard(phase_of(data.get("stage")))

    def turn(self, agent: str, phase: str, visit: int, tags: Any) -> None:
        """A turn began (in the order of the log; also note turns, which the views hold)."""
        mine = self.views.get(agent, [])
        index = self.seen[agent]
        self.seen[agent] += 1
        view = mine[index] if index < len(mine) else None
        here = self.found.setdefault((phase, visit), {}).setdefault(
            agent, {"eingaben": set(), "kanaele": set(), "versiegelt": False, "texte": set()}
        )
        if view is not None:
            here["eingaben"] |= set(tags(view))
            seen = view.prompt + json.dumps(view.context, ensure_ascii=False, default=str)
            here["texte"] |= {w for w in self.words if w in seen}
        # a channel counts if the agent shares it with another one (alone it carries nothing)
        here["kanaele"] |= {
            c
            for c in self.open
            if self.members.get(c, set()) - {agent} and agent in self.members[c]
        }
        here["versiegelt"] = here["versiegelt"] or phase in self.sealed

    def skip(self, agent: str) -> None:
        self.seen[agent] += 1

    def sections(self, turns: list[list[Any]]) -> list[list[Any]]:
        out = []
        for phase, visit, agents in turns:
            got = self.found.get((phase, visit), {})
            out.append(
                [
                    phase,
                    visit,
                    {
                        a: {
                            "eingaben": sorted(got[a]["eingaben"]),
                            "kanaele": sorted(got[a]["kanaele"]),
                            "versiegelt": got[a]["versiegelt"],
                            "texte": sorted(got[a]["texte"]),
                        }
                        for a in agents
                        if a in got
                    },
                ]
            )
        return out


def _new_words(mgr: RunManager) -> list[str]:
    """The texts of the seeds a phases run had: the proof from the project, the titles of the
    list `ideen` (given or from the ideator)."""
    params = mgr.prepared.resolved.params if mgr.prepared else {}
    words = [PROJECT_PROOF_TEXT] if params.get("artifact") else []
    latest = RoomStore(mgr.run_dir).latest_version("ideen")
    for entry in (latest.fields.get("items") or []) if latest is not None else []:
        if isinstance(entry, dict) and entry.get("title"):
            words.append(str(entry["title"]))
    return words


def _old_words(mgr: RunManager, params: Mapping[str, Any]) -> list[str]:
    """The same texts in a run of an old topology (ideas given or results `idea`)."""
    words = [PROJECT_PROOF_TEXT] if params.get("artifact") else []
    given = params.get("ideas") or []
    words += [str(i.get("title") if isinstance(i, dict) else i) for i in given]
    words += [r.title for r in RoomStore(mgr.run_dir).results() if r.kind == "idea"]
    return words


def _new_tags(template: str) -> Any:
    def tags(view: Any) -> set[str]:
        ctx = view.context
        fmt = (ctx.get("submission") or {}).get("format")
        out: set[str] = set()
        for item in ctx.get("inputs") or []:
            kind, _, name = str(item.get("source", "")).partition(":")
            if fmt == "rating":
                out.add("zu-bewerten")
            elif kind == "channel":
                out.add("verlauf")
            elif kind == "name" and name == "beweis" or item.get("name") == "beweis":
                out.add("beweis")
            elif kind == "name" and name == "ideen" or item.get("name") == "ideen":
                out.add("ideen")  # the list of ideas, from the ideator or given (seed)
                if item.get("text"):
                    out.add("ideen-inhalt")  # with its entries (W4b-06a.3 NP1)
            elif kind == "phase" and template == "proof-review":
                out.add("gutachten")
            elif kind == "phase" and template == "proof":
                out.add("vorstufen")
            elif kind == "phase" and name.startswith("exploration"):
                out.add("staende")
            elif kind == "phase" and name == "ideen":
                out.add("ideen")
            elif kind == "phase" and template == "roundtable":
                out.add("fazits")
        if ctx.get("objections"):
            out.add("einwaende")
        if ctx.get("entry"):
            out.add("eintrag")
        if ctx.get("peers"):
            out.add("auszuege")
        if ctx.get("feedback"):
            out.add("bewertungen")
        return out

    return tags


def _old_tags(view: Any) -> set[str]:
    ctx = view.context
    out: set[str] = set()
    if "artifact" in ctx:
        out.add("beweis")
    if ctx.get("reviews"):
        out.add("gutachten")
    if ctx.get("objections"):
        out.add("einwaende")
    if ctx.get("idea"):
        out.add("eintrag")
    if ctx.get("digest"):
        out.add("auszuege")
    if ctx.get("result_id") or ctx.get("result_ids"):
        out.add("zu-bewerten")
    elif ctx.get("results"):
        out.add("staende")
    if ctx.get("accepted"):
        out.add("vorstufen")
    if ctx.get("rework"):
        out.add("bewertungen")
    if view.stage == "zusammenfassung" and ctx.get("channel"):
        out.add("verlauf")
    if ctx.get("overview"):  # the summary of `exploration`: ranking with the ideas
        out |= {"ideen", "ideen-inhalt"}
    return out


def new_flow(mgr: RunManager, status: Any, log: Any = None, template: str = "") -> dict[str, Any]:
    """The flow of a `phases` run."""
    sight = _Sight(mgr, log, _new_words(mgr))
    tags = _new_tags(template)
    seq: list[tuple[str, int, str]] = []
    visits: Counter[str] = Counter()
    gates: list[list[Any]] = []
    ends: list[list[Any]] = []
    routes: list[str] = []
    for entry in _entries(mgr):
        data = entry["data"]
        if entry["kind"] == "checkpoint":
            kind = data.get("phase")
            if kind == "pass_start":
                visits[data["stage"]] = int(data["attempt"])
            elif kind == "gate" and data["value"] != "done":
                gates.append([data["stage"], data["value"]])
            elif kind == "route":
                routes.append(_route_end(mgr, data))
            elif kind == "stage_end" and data.get("outcome") != "done":
                ends.append([data["stage"], data["outcome"], data.get("accepted")])
        elif entry["kind"] == "turn" and data.get("purpose"):
            sight.skip(data["agent"])
        elif entry["kind"] == "turn":
            stage = data.get("stage")
            seq.append((stage, visits[stage], data["agent"]))
            sight.turn(data["agent"], stage, visits[stage], tags)
        else:
            sight.event(entry["kind"], data, str)
    store = RoomStore(mgr.run_dir)
    subs = store.submissions()
    by_id = {s.id: s for s in subs}
    counted: dict[str, dict[str, Any]] = {}
    files: set[tuple[str, str, str]] = set()
    for sub in subs:
        if sub.agent in ("arr", "orchestrator", "user") or sub.stage is None:
            continue  # a seed
        n = 1
        if sub.format == "rating":
            n = len(sub.fields.get("ratings") or [])
        elif sub.format == "list":
            n = len(sub.fields.get("items") or [])
        key = f"{sub.stage}/{sub.agent}"
        item = counted.setdefault(key, {"format": sub.format, "count": 0})
        item["count"] += n
        if (mgr.run_dir / sub.path).is_dir():
            files.add((sub.stage, sub.agent, sub.format))
    items: dict[str, dict[str, Any]] = {}
    for sub in subs:
        if sub.name is None or sub.format == "list":
            continue
        by = sub.agent if sub.stage is not None else "projekt"
        items[sub.name] = {"versions": int(sub.version or 0), "last_by": by}
    ranking: list[int] = []
    for entry in _entries(mgr):
        data = entry["data"]
        if entry["kind"] == "checkpoint" and data.get("phase") == "gate" and data.get("ranking"):
            ranking = [_entry_number(by_id[i]) for i in data["ranking"]]
    accepted = [
        [stage, outcome, by_id[acc].attempt if acc in by_id else None]
        for stage, outcome, acc in ends
    ]
    turns = _sections(seq)
    return {
        "turns": turns,
        "sight": sight.sections(turns),
        "submissions": dict(sorted(counted.items())),
        "items": dict(sorted(items.items())),
        "gates": gates,
        "stage_ends": accepted,
        "ranking": ranking,
        "end": {
            "status": str(getattr(status, "value", status)),
            "how": routes[-1] if routes and routes[-1] != "weiter" else "ende",
            **_summary(mgr),
        },
        **_sessions(mgr),
        "files": sorted([list(f) for f in files])
        + ([["summary.md"]] if _summary_file(mgr) else []),
    }


def _route_end(mgr: RunManager, data: Mapping[str, Any]) -> str:
    """„weiter“ while the run goes on; at its end „ende“ if it left the last phase over `next`
    (also an exhausted one with `on_exhausted: next`, or the user's „Weiter“), else „stopp“."""
    if data.get("entered", "") is not None:
        return "weiter"
    if data.get("asked_user"):
        return "ende" if data.get("choice") == "continue" else "stopp"
    route = str(data.get("route"))
    exhausted = data.get("exhausted") or []
    if exhausted and mgr.prepared is not None:
        route = str(mgr.prepared.resolved.phase(exhausted[-1]).on_exhausted)
    return "ende" if route == "next" else "stopp"


def _entry_number(sub: Any) -> int:
    action = str(sub.action or "")
    return int(action.rsplit("-", 1)[1]) if "-" in action else 1


@dataclass(frozen=True)
class OldMap:
    """How an old topology's stages map to phases."""

    template: str
    params: Mapping[str, Any]

    def phase(self, stage: str | None) -> str:
        p = self.params
        if self.template == "solo":
            return "arbeit"
        if self.template == "proof-review":
            if stage == "artefakt":
                return "fassung"
            if stage == "pruefung" and int(p.get("discussion_rounds", 1)) == 0:
                return "pruefung-allein"
            return str(stage)
        if self.template == "exploration":
            if stage in ("exploration", "bewertung"):
                return {"none": "exploration", "digest": "exploration-digest"}.get(
                    str(p.get("exchange", "none")), "exploration-kanal"
                )
            return str(stage)
        return str(stage)


def old_flow(mgr: RunManager, status: Any, template: str, log: Any = None) -> dict[str, Any]:
    """The flow of a run of an old topology, on the names of the phases."""
    resolved = mgr.prepared.resolved if mgr.prepared else None
    params = dict(resolved.params) if resolved else {}
    mapping = OldMap(template, params)
    sight = _Sight(mgr, log, _old_words(mgr, params))
    seq: list[tuple[str, int, str]] = []
    gates: list[list[Any]] = []
    ends: list[list[Any]] = []
    ranking: list[int] = []
    round_ = 1
    attempts: Counter[str] = Counter()
    how = "ende"
    for entry in _entries(mgr):
        data = entry["data"]
        if entry["kind"] == "checkpoint":
            stage = data.get("stage")
            kind = data.get("phase")
            if template == "proof-review":
                if stage == "pruefung" and kind is None:
                    round_ = int(data["round"])
                elif stage == "urteil":
                    where = "diskussion" if _discussed(params, mgr) else mapping.phase("pruefung")
                    gates.append([where, data["verdict"]])
                elif stage == "zusammenfassung":
                    how = "stopp"  # accepted, revisions used up: the run stops after a round
            elif template == "proof":
                if kind == "stage_end":
                    ends.append([stage, data["outcome"], data.get("accepted")])
                elif "decision" in data and "attempt" in data:
                    attempts[stage] = int(data["attempt"])
                    gates.append([stage, "pass" if data["decision"] == "passed" else "fail"])
            elif (
                template == "exploration"
                and stage == "zusammenfassung"
                and data.get("ranking") is not None
            ):
                scored = [r for r in data["ranking"] if r.get("score") is not None]
                ranking = [int(str(r["idea"]).removeprefix("I")) for r in scored]
                if data["ranking"]:
                    gates.append([mapping.phase("bewertung"), "ranked"])
        elif entry["kind"] == "turn" and data.get("purpose"):
            sight.skip(data["agent"])
        elif entry["kind"] == "turn":
            stage = data.get("stage")
            phase = mapping.phase(stage)
            if template == "proof-review" and phase != "fassung":
                visit = round_
            elif template == "proof":
                visit = attempts[stage] + 1
            else:
                visit = 1
            seq.append((phase, visit, data["agent"]))
            sight.turn(data["agent"], phase, visit, _old_tags)
        else:
            sight.event(entry["kind"], data, mapping.phase)
    store = RoomStore(mgr.run_dir)
    results = store.results()
    by_id = {r.id: r for r in results}
    counted: dict[str, dict[str, Any]] = {}
    files: set[tuple[str, str, str]] = set()
    for result in results:
        fmt = OLD_KIND_FORMAT.get(result.kind, "text")
        key = f"{mapping.phase(result.stage)}/{result.agent}"
        item = counted.setdefault(key, {"format": fmt, "count": 0})
        item["count"] += 1
        files.add((mapping.phase(result.stage), result.agent, fmt))
    for rating in store.ratings():
        rated = by_id.get(rating.result_id)
        key = f"{mapping.phase(rated.stage if rated else None)}/{rating.rater}"
        item = counted.setdefault(key, {"format": "rating", "count": 0})
        item["count"] += 1
        files.add((mapping.phase(rated.stage if rated else None), rating.rater, "rating"))
    items: dict[str, dict[str, Any]] = {}
    if template == "proof-review":
        versions = sorted((mgr.run_dir / "output" / "artifacts").glob("v*"))
        if versions:
            by = "projekt" if params.get("artifact") else "author"
            if len(versions) > 1 or not params.get("artifact"):
                by = "author"
            items["beweis"] = {"versions": len(versions), "last_by": by}
    elif template == "proof":
        for stage in ("strategie", "skizze", "beweis"):
            mine = [r for r in results if r.stage == stage]
            if mine:
                items[stage] = {"versions": len(mine), "last_by": mine[-1].agent}
    accepted = [
        [stage, outcome, by_id[acc].attempt if acc in by_id else None]
        for stage, outcome, acc in ends
    ]
    if template == "proof" and ends and ends[-1][1] == "stage_failed":
        how = "stopp"
    turns = _sections(seq)
    return {
        "turns": turns,
        "sight": sight.sections(turns),
        "submissions": dict(sorted(counted.items())),
        "items": dict(sorted(items.items())),
        "gates": gates,
        "stage_ends": accepted,
        "ranking": ranking,
        "end": {"status": str(getattr(status, "value", status)), "how": how, **_summary(mgr)},
        **_sessions(mgr),
        "files": sorted([list(f) for f in files])
        + ([["summary.md"]] if _summary_file(mgr) else []),
    }


def _sessions(mgr: RunManager) -> dict[str, dict[str, int]]:
    """New sessions (`session_reset`) and fresh working folders (`folder_fresh`) per agent."""
    resets: Counter[str] = Counter()
    fresh: Counter[str] = Counter()
    for entry in _entries(mgr):
        agent = entry["data"].get("agent")
        if entry["kind"] == "session_reset":
            resets[agent] += 1
        elif entry["kind"] == "folder_fresh":
            fresh[agent] += 1
    return {"resets": dict(sorted(resets.items())), "fresh_folders": dict(sorted(fresh.items()))}


def _discussed(params: Mapping[str, Any], mgr: RunManager) -> bool:
    return int(params.get("discussion_rounds", 1)) > 0


# --------------------------------------------------------------------------------------------
# Target flows and the comparison
# --------------------------------------------------------------------------------------------


def flow_file(template: str, case: str) -> Path:
    return FLOWS / f"{template}-{case}.json"


def write_or_check(template: str, case: str, flow: Mapping[str, Any], params: Any) -> None:
    """Write the target flow (with `ARR_WRITE_TEMPLATE_FLOWS=1`) or check that the old
    topology still produces exactly it."""
    data = {"template": template, "case": case, "params": dict(params), **flow}
    path = flow_file(template, case)
    if os.environ.get(WRITE_FLOWS) == "1":
        FLOWS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return
    assert path.is_file(), f"{path.name} fehlt; schreiben mit {WRITE_FLOWS}=1"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == json.loads(json.dumps(data)), (
        f"{path.name} beschreibt die alte Topologie nicht"
    )


def load_flow(template: str, case: str) -> dict[str, Any]:
    return json.loads(flow_file(template, case).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------
# Cases (§6.3) and equivalent policies for both sides
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One case of the comparison: template, name, parameters and how the agents act.

    `act` (per template): `proof-review` `verdicts` (judge → verdict per round); `proof`
    `scores` ("stage/attempt" → score of every rater); `exploration` `items` (ideas the ideator
    hands in), `silent` (explorers that never hand in); `solo` `hand_in`. `decider` answers an
    escalation (an `EscalationOption` value)."""

    template: str
    name: str
    params: Mapping[str, Any]
    act: Mapping[str, Any]
    decider: str | None = None

    @property
    def id(self) -> str:
        return f"{self.template}-{self.name}"


def _verdicts(*rounds: str, judges: int = 2) -> dict[str, list[str]]:
    return {f"judge-{i}": list(rounds) for i in range(1, judges + 1)}


CASES: tuple[Case, ...] = (
    Case("proof-review", "standard", {}, {"verdicts": _verdicts("accept")}),
    Case("proof-review", "revision", {}, {"verdicts": _verdicts("minor", "accept")}),
    Case(
        "proof-review",
        "aufgebraucht",
        {"max_revisions": 1},
        {"verdicts": _verdicts("major", "minor")},
    ),
    Case(
        "proof-review",
        "ohne-diskussion",
        {"discussion_rounds": 0},
        {"verdicts": _verdicts("minor", "accept")},
    ),
    Case(
        "proof-review",
        "ein-judge",
        {"judges": 1},
        {"verdicts": _verdicts("minor", "accept", judges=1)},
    ),
    Case(
        "proof-review",
        "gedaechtnis-neu",
        {"judge_memory": "fresh"},
        {"verdicts": _verdicts("minor", "accept")},
    ),
    Case(
        "proof-review",
        "gedaechtnis-behalten",
        {"judge_memory": "continue"},
        {"verdicts": _verdicts("minor", "accept")},
    ),
    Case(
        "proof-review",
        "artefakt-aus-projekt",
        {"artifact": PROJECT_PROOF},
        {"verdicts": _verdicts("minor", "accept")},
    ),
    Case("proof-review", "reject", {}, {"verdicts": _verdicts("reject")}),
    Case(
        "proof-review",
        "reject-ohne-revision",
        {"max_revisions": 0},
        {"verdicts": _verdicts("reject")},
    ),
    Case(
        "proof-review",
        "einstimmig",
        {"verdict": "unanimous"},
        {"verdicts": {"judge-1": ["accept", "accept"], "judge-2": ["minor", "accept"]}},
    ),
    Case(
        "proof-review",
        "drei-judges-mehrheit",
        {"judges": 3},
        {"verdicts": {"judge-1": ["accept"], "judge-2": ["accept"], "judge-3": ["minor"]}},
    ),
    Case("proof", "bestanden", {}, {"scores": {}}),
    Case("proof", "nacharbeit", {}, {"scores": {"strategie/1": 4, "strategie/2": 8}}),
    Case(
        "proof",
        "aufgebraucht-weiter",
        {"on_fail": "continue"},
        {"scores": {"skizze/1": 6, "skizze/2": 5}},
    ),
    Case(
        "proof",
        "aufgebraucht-stopp",
        {"on_fail": "stop"},
        {"scores": {"skizze/1": 6, "skizze/2": 5}},
    ),
    Case(
        "proof",
        "aufgebraucht-eskalation",
        {"on_fail": "escalate"},
        {"scores": {"skizze/1": 6, "skizze/2": 5}},
        decider="end",
    ),
    Case(
        "proof",
        "hohe-schwelle",
        {"pass_score": 9},
        {"scores": {}},
    ),
    Case("exploration", "ideator", {}, {"items": 3}),
    Case("exploration", "zwei-ranker", {"n_rankers": 2}, {"items": 3}),
    Case("exploration", "ein-explorer", {"n_explorers": 1, "n_ideas": 2}, {"items": 2}),
    Case("exploration", "mehr-ideen", {"n_ideas": 2, "turns_per_idea": 1}, {"items": 4}),
    Case("exploration", "vorgegeben", {"ideas": ["Modulo 4", "Gaußsche Zahlen"]}, {}),
    Case("exploration", "digest", {"exchange": "digest"}, {"items": 3}),
    Case("exploration", "kanal", {"exchange": "channel"}, {"items": 3}),
    Case(
        "exploration",
        "stiller-explorer",
        {"n_ideas": 2, "turns_per_idea": 2},
        {"items": 2, "silent": ["explorer-2"]},
    ),
    Case("roundtable", "eine-runde", {"rounds": 1}, {}),
    Case("roundtable", "drei-runden", {"rounds": 3}, {}),
    Case("solo", "mit-abgabe", {"max_turns": 3}, {"hand_in": True}),
    Case("solo", "ohne-abgabe", {"max_turns": 3}, {"hand_in": False}),
)
"""The cases of §6.3 of the order W4b-06a, with those of the review (W4b-06a.1: `reject`,
mixed verdicts with `unanimous` and a majority of three, the last stage with `next`, two
rankers); `proof-review-reject-ohne-revision` is the target of the deviation
`reject-beendet`."""

NEW_PARAMS = {
    "judge_memory": {"fresh": "reset", "continue": "keep"},
    "on_fail": {"continue": "next"},
}
"""Old parameter values → new ones (W4b-02a §5.8)."""


def new_params(case: Case) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in case.params.items():
        out[key] = NEW_PARAMS.get(key, {}).get(value, value) if isinstance(value, str) else value
    if case.template == "solo":
        return out
    return out


def _score(case: Case, stage: str | None, attempt: int) -> float:
    return float(case.act.get("scores", {}).get(f"{stage}/{attempt}", 8))


def _idea_titles(case: Case) -> list[str]:
    given = case.params.get("ideas")
    if given:
        return [str(t) for t in given]
    return [f"Idee {k}" for k in range(1, int(case.act.get("items", 3)) + 1)]


def _entry_score(number: int) -> float:
    """Ranking scores fall with the number of the idea, so the ranking is 1, 2, 3 …"""
    return float(max(1, 10 - number))


_ROUND = re.compile(r"Runde (\d+) von (\d+) in #")


def last_round(view: Any) -> bool:
    """The turn is in the last round of its discussion, as the prompt of the phase (or of the
    old topology) says it, not as the case expects it (M2 of the review); a turn without a
    round (an ask-again) counts as the last."""
    found = _ROUND.findall(view.prompt)
    if not found:
        return True
    number, of = found[-1]
    return int(number) >= int(of)


def talk(channel: str, view: Any) -> dict[str, Any]:
    return room("post_message", channel=channel, body=f"Beitrag von {view.agent}.")


def old_policies(case: Case) -> dict[str, Policy]:
    """Policies of the old side for `case` (`submit_result`, `submit_rating`)."""
    t = case.template
    if t == "solo":
        hand = case.act.get("hand_in", True)
        return {
            "worker": (lambda v: [old_result("result", "Ergebnis", "Inhalt")]) if hand else nothing
        }
    if t == "proof-review":
        verdicts = case.act["verdicts"]

        def author(view: Any) -> list[dict[str, Any]]:
            path = str(case.params.get("artifact") or "beweis.md")
            write_file(view, path, f"Beweis, {view.stage}\n")
            return [old_result("artifact", "Beweis", path=path)]

        def judge(view: Any) -> list[dict[str, Any]]:
            if view.stage == "diskussion" and not last_round(view):
                return [talk("judges", view)]
            mine = verdicts[view.agent]
            verdict = mine[min(int(view.context.get("round", 1)), len(mine)) - 1]
            objections = [] if verdict == "accept" else [f"{view.agent}: Lücke"]
            return [old_result("review", "Gutachten", old_review(verdict, objections))]

        return {"author": author, **{j: judge for j in verdicts}}
    if t == "proof":

        def worker(view: Any) -> list[dict[str, Any]]:
            write_file(view, f"{view.stage}.md", f"{view.stage}, Versuch {view.attempt}\n")
            return [old_result("note", view.stage, path=f"{view.stage}.md")]

        def rater(view: Any) -> list[dict[str, Any]]:
            ctx = view.context
            score = _score(case, ctx.get("stage"), int(ctx.get("attempt", 1)))
            return [old_rate(score, str(ctx["result_id"]))]

        raters = {f"rater-{i}": rater for i in range(1, 5)}
        return {"strategist": worker, "prover": worker, **raters}
    if t == "exploration":
        titles = _idea_titles(case)
        silent_ones = set(case.act.get("silent", []))

        def ideator(view: Any) -> list[dict[str, Any]]:
            return [old_result("idea", title, f"Ansatz {title}") for title in titles]

        def explorer(view: Any) -> list[dict[str, Any]]:
            if view.agent in silent_ones:
                return nothing(view)
            return [old_result("exploration", "Stand", f"## Stand\nText von {view.agent}")]

        def ranker(view: Any) -> list[dict[str, Any]]:
            steps = []
            for entry in view.context.get("results", []):
                number = next(
                    (i for i, title in enumerate(titles, 1) if str(entry["idea"]) == title), 9
                )
                steps.append(old_rate(_entry_score(number), str(entry["result_id"])))
            return steps

        def summarizer(view: Any) -> list[dict[str, Any]]:
            return [old_result("summary", "Zusammenfassung", "## Gesichert\nwenig")]

        explorers = {f"explorer-{i}": explorer for i in range(1, 7)}
        rankers = {f"ranker-{i}": ranker for i in range(1, 4)}
        return {"ideator": ideator, **explorers, **rankers, "summarizer": summarizer}
    if t == "roundtable":

        def diskutant(view: Any) -> list[dict[str, Any]]:
            return [talk("runde", view)]

        def summarizer(view: Any) -> list[dict[str, Any]]:
            if view.stage == "diskussion":
                return [room("set_status", status="done", note="fertig")]
            return [old_result("summary", "Zusammenfassung", "## Gesichert\nwenig")]

        return {**{f"diskutant-{i}": diskutant for i in range(1, 6)}, "summarizer": summarizer}
    raise KeyError(t)


def new_policies(case: Case) -> dict[str, Policy]:
    """Policies of the new side for `case` (`submit`), acting as `old_policies` does."""
    from phases_kit import answer, hand_in

    t = case.template
    if t == "solo":
        return {"worker": answer() if case.act.get("hand_in", True) else nothing}
    if t == "proof-review":
        verdicts = case.act["verdicts"]

        def judge(view: Any) -> list[dict[str, Any]]:
            if view.stage == "diskussion" and not last_round(view):
                return [talk("judges", view)]
            mine = verdicts[view.agent]
            verdict = mine[min(view.attempt, len(mine)) - 1]
            objections = [] if verdict == "accept" else [{"text": f"{view.agent}: Lücke"}]
            return hand_in(view, verdict=verdict, objections=objections)

        return {"author": answer(), **{j: judge for j in verdicts}}
    if t == "proof":

        def score(view: Any, ref: int) -> float:
            return _score(case, view.stage, view.attempt)

        raters = {f"rater-{i}": answer(score=score) for i in range(1, 5)}
        return {"strategist": answer(), "prover": answer(), **raters}
    if t == "exploration":
        silent_ones = set(case.act.get("silent", []))

        def ranker(view: Any, ref: int) -> float:
            run_dir = view.work_dir.parents[2]
            return _entry_score(_entry_number(RoomStore(run_dir).submission(int(ref))))

        explorers = {
            f"explorer-{i}": nothing if f"explorer-{i}" in silent_ones else answer()
            for i in range(1, 7)
        }
        rankers = {f"ranker-{i}": answer(score=ranker) for i in range(1, 4)}
        items = int(case.act.get("items", 3))
        return {"ideator": answer(items=items), **explorers, **rankers, "summarizer": answer()}
    if t == "roundtable":

        def diskutant(view: Any) -> list[dict[str, Any]]:
            if not last_round(view):
                return [talk("runde", view)]
            return [talk("runde", view), *hand_in(view)]

        return {**{f"diskutant-{i}": diskutant for i in range(1, 6)}, "summarizer": answer()}
    raise KeyError(t)


def decider_for(case: Case) -> Any:
    from phases_kit import Choose

    from arr.permissions.types import EscalationOption

    if case.decider is None:
        return None
    return Choose(EscalationOption(case.decider))


async def run_old(workfolder: Path, case: Case) -> tuple[RunManager, dict[str, Any]]:
    from phases_kit import run

    if case.template == "solo":
        mgr, log = old_solo_manager(
            workfolder, old_policies(case), max_turns=int(case.params["max_turns"])
        )
    else:
        mgr, log = old_manager(
            workfolder,
            case.template,
            old_policies(case),
            params=case.params,
            decider=decider_for(case),
        )
    status = await run(mgr)
    return mgr, old_flow(mgr, status, case.template, log)


async def run_new(
    workfolder: Path, case: Case, files: Mapping[str, bytes] | None = None
) -> tuple[RunManager, dict[str, Any]]:
    from phases_kit import run

    if case.template == "solo":
        mgr, log = new_solo_manager(
            workfolder, new_policies(case), max_turns=int(case.params["max_turns"])
        )
    else:
        mgr, log = new_manager(
            workfolder,
            case.template,
            new_policies(case),
            params=new_params(case),
            decider=decider_for(case),
            files=files,
        )
    status = await run(mgr)
    return mgr, new_flow(mgr, status, log, case.template)


# --------------------------------------------------------------------------------------------
# Wanted deviations (§6.3 of the order) and the comparison
# --------------------------------------------------------------------------------------------


def _section(flow: dict[str, Any], phase: str, visit: int = 1) -> dict[str, int]:
    for name, number, agents in flow["turns"]:
        if name == phase and number == visit:
            return agents
    raise KeyError((phase, visit))


def _add_submission(flow: dict[str, Any], key: str, fmt: str, count: int) -> None:
    item = flow["submissions"].setdefault(key, {"format": fmt, "count": 0})
    item["count"] += count
    flow["submissions"] = dict(sorted(flow["submissions"].items()))
    phase, agent = key.split("/")
    if [phase, agent, fmt] not in flow["files"]:
        rest = [f for f in flow["files"] if f != ["summary.md"]]
        tail = [f for f in flow["files"] if f == ["summary.md"]]
        flow["files"] = sorted([*rest, [phase, agent, fmt]]) + tail


def _explorers(case: Case) -> int:
    return int(case.params.get("n_explorers", 2))


def _per_idea(case: Case) -> int:
    return int(case.params.get("turns_per_idea", 2))


def _solo_extra_turn(flow: dict[str, Any], case: Case) -> None:
    _section(flow, "arbeit")["worker"] += 1


def _ask_per_repetition(flow: dict[str, Any], case: Case) -> None:
    ideas = int(case.act.get("items", 3))
    phase = OldMap("exploration", case.params).phase("exploration")
    for agent in case.act["silent"]:
        index = int(agent.rsplit("-", 1)[1]) - 1
        entries = len(range(index, ideas, _explorers(case)))
        _section(flow, phase)[agent] += entries * (_per_idea(case) - 1)


def _more_ideas(flow: dict[str, Any], case: Case) -> None:
    phase = OldMap("exploration", case.params).phase("exploration")
    section = _section(flow, phase)
    seen = _sight_of(flow, phase)
    titles = _idea_titles(case)
    for number in range(int(case.params["n_ideas"]) + 1, int(case.act["items"]) + 1):
        explorer = f"explorer-{(number - 1) % _explorers(case) + 1}"
        section[explorer] += _per_idea(case)
        _add_submission(flow, f"{phase}/{explorer}", "text", _per_idea(case))
        _add_submission(flow, f"{phase}/ranker-1", "rating", 1)
        flow["ranking"].append(number)
        title = titles[number - 1]
        for agent in (explorer, "ranker-1"):
            seen[agent]["texte"] = sorted({*seen[agent]["texte"], title})
        summary = _sight_of(flow, "zusammenfassung")["summarizer"]
        summary["texte"] = sorted({*summary["texte"], title})


def _sight_of(flow: dict[str, Any], phase: str, visit: int = 1) -> dict[str, Any]:
    for name, number, agents in flow["sight"]:
        if name == phase and number == visit:
            return agents
    raise KeyError((phase, visit))


def _summarizer_not_in_discussion(flow: dict[str, Any], case: Case) -> None:
    del _section(flow, "diskussion")["summarizer"]
    del _sight_of(flow, "diskussion")["summarizer"]
    _sight_of(flow, "zusammenfassung")["summarizer"]["kanaele"] = []  # #runde is an input
    for i in range(1, int(case.params.get("n_diskutanten", 2)) + 1):
        _add_submission(flow, f"diskussion/diskutant-{i}", "text", 1)


def _one_judge_discusses(flow: dict[str, Any], case: Case) -> None:
    turns = []
    for name, visit, agents in flow["turns"]:
        turns.append([name, visit, agents])
        if name == "pruefung":
            turns.append(["diskussion", visit, {"judge-1": 1}])
    flow["turns"] = turns
    seen = {"eingaben": ["beweis", "einwaende", "gutachten"], "kanaele": []}  # alone in #judges
    sight = []
    for name, visit, agents in flow["sight"]:
        sight.append([name, visit, agents])
        if name == "pruefung":
            here = {**seen, "versiegelt": False, "texte": []}
            sight.append(["diskussion", visit, {"judge-1": here}])
    flow["sight"] = sight
    rounds = sum(1 for t in turns if t[0] == "diskussion")
    _add_submission(flow, "diskussion/judge-1", "review", rounds)


def _reset_gives_a_fresh_folder(flow: dict[str, Any], case: Case) -> None:
    flow["fresh_folders"] = {a: n for a, n in flow["resets"].items() if a.startswith("judge")}


def _reject_ends(flow: dict[str, Any], case: Case) -> None:
    """The run ends after the first `reject` as the old topology does without revisions."""
    target = load_flow("proof-review", "reject-ohne-revision")
    flow.clear()
    flow.update({k: v for k, v in target.items() if k not in META})


def _raters_see_their_submission(flow: dict[str, Any], case: Case) -> None:
    for _name, visit, agents in flow["sight"]:
        for agent, seen in agents.items():
            if agent.startswith("rater"):
                seen["eingaben"] = ["zu-bewerten"] + (["bewertungen"] if visit > 1 else [])
                seen["eingaben"] = sorted(seen["eingaben"])


def _explorers_see_the_list(flow: dict[str, Any], case: Case) -> None:
    for _name, _visit, agents in flow["sight"]:
        for agent, seen in agents.items():
            if agent.startswith("explorer"):
                seen["eingaben"] = sorted({*seen["eingaben"], "ideen"})


def _peers_without_titles(flow: dict[str, Any], case: Case) -> None:
    titles = _idea_titles(case)
    for name, _visit, agents in flow["sight"]:
        if name != "exploration-digest":
            continue
        for agent, seen in agents.items():
            if agent.startswith("explorer"):
                k = int(agent.rsplit("-", 1)[1]) - 1
                own = titles[k :: _explorers(case)]
                seen["texte"] = sorted(own)


def _same(flow: dict[str, Any], case: Case) -> None:
    """No change in the flow: the deviation is normalized (`OLD_KIND_FORMAT`) or checked by
    a test of its own."""


@dataclass(frozen=True)
class Deviation:
    """A wanted difference of the new template against the old topology."""

    key: str
    template: str
    reason: str
    applies: Any
    adjust: Any = _same


DEVIATIONS: tuple[Deviation, ...] = (
    Deviation(
        "solo-ein-turn-mehr",
        "solo",
        "`turns` plus eine Nachfrage statt `max_turns` insgesamt (Codeplan „Vorlagen gegen "
        "heute“, W4b-02a §5.8 Nr. 5)",
        lambda c: not c.act.get("hand_in", True),
        _solo_extra_turn,
    ),
    Deviation(
        "nachfrage-je-wiederholung",
        "exploration",
        "ein stiller Explorer bekommt je Wiederholung eine Nachfrage (Entscheidung 58)",
        lambda c: bool(c.act.get("silent")),
        _ask_per_repetition,
    ),
    Deviation(
        "mehr-ideen-als-n-ideas",
        "exploration",
        "`over` verteilt jeden Eintrag der Liste, die alte Topologie nur die ersten `n_ideas` "
        "(Entscheidung 77)",
        lambda c: (
            "ideas" not in c.params and int(c.act.get("items", 3)) > int(c.params.get("n_ideas", 3))
        ),
        _more_ideas,
    ),
    Deviation(
        "summarizer-diskutiert-nicht",
        "roundtable",
        "der Summarizer ist in `diskussion` nicht dabei, jeder Diskutant gibt am Ende ab "
        "(W4b-02a §5.8 Nr. 4, W4b-03 §7.1)",
        lambda c: True,
        _summarizer_not_in_discussion,
    ),
    Deviation(
        "ein-judge-diskutiert-allein",
        "proof-review",
        "ein Judge mit Diskussion bekommt in `diskussion` einen Turn allein (Entscheidung 47)",
        lambda c: (
            int(c.params.get("judges", 2)) == 1 and int(c.params.get("discussion_rounds", 1)) > 0
        ),
        _one_judge_discusses,
    ),
    Deviation(
        "notiz-bleibt-privat",
        "proof-review",
        "`note_next` geht nicht an die anderen Judges; sie bekommen Kurzfassungen und "
        "Einwandliste (Entscheidung 28; eigener Test)",
        lambda c: True,
    ),
    Deviation(
        "reset-mit-frischem-ordner",
        "proof-review",
        "`reset` gibt den Judges auch einen frischen Arbeitsordner (P16, W4b-02a §5.8 Nr. 2)",
        lambda c: c.params.get("judge_memory", "fresh") == "fresh",
        _reset_gives_a_fresh_folder,
    ),
    Deviation(
        "reject-beendet",
        "proof-review",
        "`reject` beendet den Run (Gate `reject: stop`); die alte Topologie überarbeitete auch "
        "nach `reject`, solange Revisionen übrig waren (Entwurf 10 §5, Zeile `urteil`: „reject → "
        "Stopp“; dem User am 22.11. gemeldet). Soll: der alte Ablauf ohne Revisionen",
        lambda c: (
            any("reject" in v for v in c.act.get("verdicts", {}).values())
            and int(c.params.get("max_revisions", 1)) > 0
        ),
        _reject_ends,
    ),
    Deviation(
        "befund-bewerter-sehen-ihre-abgabe",
        "proof",
        "Befund: Die Rater bekommen nur die Abgabe, die sie bewerten (die alte Topologie gab "
        "ihnen auch die angenommenen Vorstufen), und bei einer Wiederholung wie alle Teilnehmer "
        "das Ergebnis des Gates (Entscheidung 16); `from` liest keine Bewertungen (Entscheidung "
        "58). In der Vorlage nicht änderbar: Die Bewertungs-Action hat keine eigenen Eingaben",
        lambda c: True,
        _raters_see_their_submission,
    ),
    Deviation(
        "befund-explorer-sehen-die-liste",
        "exploration",
        "Befund: Schlägt der Ideator die Ansätze vor, bekommen die Explorer die ganze Liste "
        "`ideen` als Standard-Eingabe (Abgabe der Phase `ideen`), die alte Topologie nur ihre "
        "eigene Idee. Nicht bei vorgegebenen Ideen (der Seed ist keine Abgabe einer Phase) und "
        "nicht bei `exchange: digest` (die Phase nennt ihre Eingaben selbst)",
        lambda c: "ideas" not in c.params and c.params.get("exchange") != "digest",
        _explorers_see_the_list,
    ),
    Deviation(
        "befund-auszuege-ohne-ideen-titel",
        "exploration",
        "Befund (06a.3 sichtbar geworden): Bei `exchange: digest` nannte der Auszug der alten "
        "Topologie die Idee der anderen Explorer; `peers: summary` nennt nur ihre Kurzfassung "
        "und die Nummer des Eintrags (gebaut in `runs/phases/runner.py`, außerhalb des "
        "Bereichs). Übertrag nach Welle 5",
        lambda c: c.params.get("exchange") == "digest",
        _peers_without_titles,
    ),
    Deviation(
        "abgabe-ueber-submit",
        "*",
        "Abgabe über `submit` mit Format statt `submit_result`/`submit_rating` und Textzeile "
        "(P1, Entscheidung 11); im Ablauf auf Formate abgebildet",
        lambda c: True,
    ),
)


def deviations_for(case: Case) -> list[Deviation]:
    return [d for d in DEVIATIONS if d.template in (case.template, "*") and bool(d.applies(case))]


META = ("template", "case", "params")


def expected_flow(case: Case) -> tuple[dict[str, Any], list[str]]:
    """The target flow of `case` with its wanted deviations applied; their keys."""
    import copy

    flow = {k: v for k, v in copy.deepcopy(load_flow(case.template, case.name)).items()}
    for key in META:
        flow.pop(key, None)
    applied = []
    for deviation in deviations_for(case):
        deviation.adjust(flow, case)
        applied.append(deviation.key)
    return flow, applied


def compare(case: Case, flow: Mapping[str, Any]) -> list[str]:
    """Assert that the new flow is the target flow up to the named deviations; returns their
    keys. The assertion names every part that differs."""
    expected, applied = expected_flow(case)
    got = json.loads(json.dumps(flow))
    wrong = {
        k: (expected.get(k), got.get(k))
        for k in set(expected) | set(got)
        if expected.get(k) != got.get(k)
    }
    assert not wrong, f"{case.id}: Abweichung ohne Ausnahme ({', '.join(applied)}): {wrong}"
    return applied


# --------------------------------------------------------------------------------------------
# Structure of the templates (M2 c of the review)
# --------------------------------------------------------------------------------------------

FIXTURE_OF = {
    "proof-review": "review",
    "proof": "waterfall",
    "exploration": "exploration",
    "roundtable": "roundtable",
}
"""Start skill → its phases in `tests/fixtures/phases/` (W4b-02a)."""


INPUTS_BEYOND_TEST_DATA: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("proof-review", "diskussion"): [{"name": "beweis", "content": "full"}, {"from": "pruefung"}],
    ("proof-review", "revision"): [
        {"name": "beweis", "content": "full"},
        {"from": "pruefung-allein"},
        {"from": "diskussion"},
    ],
    ("exploration", "zusammenfassung"): [
        {"name": "ideen", "content": "full"},
        {"from": "exploration"},
        {"from": "exploration-digest"},
        {"from": "exploration-kanal"},
    ],
}
"""`inputs` the templates have beyond the test data of W4b-02a (W4b-06a.2): a seed (the
proof from the project, the given ideas) is no submission of a phase, so the standard inputs
would not reach the phases the old topologies fed with it. The test data stay as they are
(other tests read them)."""


def _as_test_data_would_be(name: str, fixture: dict[str, Any]) -> dict[str, Any]:
    """The test data of W4b-02a with the differences the templates have on purpose."""
    for phase in fixture.get("phases") or []:  # `inputs` for the seeds (W4b-06a.2)
        added = INPUTS_BEYOND_TEST_DATA.get((name, phase["name"]))
        if added is not None:
            phase["inputs"] = added
    for (template, key, field), value in GROUP_BEYOND_TEST_DATA.items():
        if template != name:
            continue
        if value is DROPPED:
            fixture[key].pop(field)
        else:
            fixture[key][field] = value
    return fixture


DROPPED = object()
"""Marks a key the template does not have although the test data do."""

GROUP_BEYOND_TEST_DATA: dict[tuple[str, str, str], Any] = {
    ("proof-review", "limits", "turns_per_agent"): 12,  # A4: 3 passes × 4 turns
    ("exploration", "limits", "turns_per_agent"): 16,  # A4: 4 ideas × 2 repeats × 2 turns
    ("exploration", "params", "n_rankers"): {
        "default": 1,
        "min": 1,
        "max": 3,
        "doc": "Anzahl der Ranker, mindestens einer",
    },  # Entscheidung 42
    ("exploration", "params", "ideator"): DROPPED,  # it does nothing any more (fixed agent)
    ("exploration", "params", "summarizer"): {
        "default": "summarizer",
        "choices": ["summarizer", "none"],
        "doc": "summarizer: der Agent summarizer fasst am Ende zusammen; none: keine "
        "Zusammenfassung",
    },  # a switch (§6.1, N8)
    ("roundtable", "params", "summarizer"): {
        "default": "summarizer",
        "choices": ["summarizer", "none"],
        "doc": "summarizer: der Agent summarizer fasst am Ende zusammen; none: keine "
        "Zusammenfassung",
    },
}
"""Keys of a template that differ from the test data of W4b-02a on purpose (§5.1 of the
report): the limits A4 asks for, `n_rankers` from 1, no `ideator`, `summarizer` as a switch."""

STRUCTURE_KEYS = (
    "kind",
    "version",
    "topology",
    "description",
    "params",
    "rubric",
    "limits",
    "defaults",
    "agents",
    "channels",
    "seed",
    "phases",
)
"""Everything of a `group.yaml` the structure check compares with the test data (W4b-06a.3,
NP2: models, roles, `mix`, members of channels, limits, rubric, seeds as today)."""

TASK_ANCHORS: dict[tuple[str, str], tuple[str, ...]] = {
    ("exploration", "tasks/ideen.md"): ("{{ params.n_ideas }}",),
    ("proof-review", "tasks/pruefen.md"): ("unabhängig",),
}
"""Words a task file must keep: the number of ideas (§6.1), the independent check."""


def structure_issues(name: str, files: Mapping[str, bytes] | None = None) -> list[str]:
    """What a template does differently from the agreed structure: everything of its
    `group.yaml` equals the test data of W4b-02a up to the named differences
    (`INPUTS_BEYOND_TEST_DATA`, `GROUP_BEYOND_TEST_DATA`); every action and rating action has
    `turns: 1` (Festlegung des Leads, 22.11.), the checks (`pruefung*`) are isolated, every
    task file named is in the skill, and the task files keep their anchors (`TASK_ANCHORS`)."""
    files = files or start_skill_files(name)
    group, _ = parse_yaml_text(files["group.yaml"].decode("utf-8"), "group.yaml")
    fixture_text = (TESTS / "fixtures" / "phases" / f"{FIXTURE_OF[name]}.yaml").read_text("utf-8")
    fixture, _ = parse_yaml_text(fixture_text, "fixture.yaml")
    expected = _as_test_data_would_be(name, fixture)
    issues = []
    for key in STRUCTURE_KEYS:
        if group.get(key) != expected.get(key):
            issues.append(f"{key} weichen von den Testdaten ab")
    for key in sorted(set(group) - set(STRUCTURE_KEYS)):
        issues.append(f"{key}: nicht in den Testdaten")
    for phase in group.get("phases") or []:
        actions = list((phase.get("actions") or {}).values())
        gate = phase.get("gate") or {}
        if isinstance(gate.get("action"), dict):
            actions.append(gate["action"])
        for action in actions:
            if action.get("turns") != 1:
                issues.append(f"{phase['name']}: turns {action.get('turns')!r} statt 1")
            if action.get("task") not in files:
                issues.append(f"{phase['name']}: Auftrag {action.get('task')} fehlt")
        if str(phase["name"]).startswith("pruefung") and phase.get("isolated") is not True:
            issues.append(f"{phase['name']}: nicht isoliert")
    for (template, task), words in TASK_ANCHORS.items():
        if template != name:
            continue
        text = files.get(task, b"").decode("utf-8", errors="replace")
        issues += [f"{task}: „{w}“ fehlt" for w in words if w not in text]
    return issues

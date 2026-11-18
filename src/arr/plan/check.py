"""Static check of a resolved `phases` run (docs/10 §4.10, W4b-02a §6.4, §6.5, §6.9).

`check(resolved)` is a pure function: it reads only the `ResolvedRun` and reports every
problem at once, as German `ConfigIssue`s with the path in the file (`phases[4].gate.routes
.minor`), the reason and what to do. Errors and hints come back apart (`PlanCheck`); a hint
prevents nothing. The rules carry the numbers of the order: S (structure), B (actions and
submissions), G (gates), L (loops), I (isolation and folders).

`arr.resolve` reports beforehand what only the input shows: names that exist nowhere in the
file (agents, phases, channels, submission names, parameters), routes that are no route,
placeholders, and keys of the old topologies that `resolve_run` cannot carry (`open_from`,
channels `moderated`). This check repeats the reference checks on the resolved run, so it also
holds for a resolved run from elsewhere.

No text of the file is repeated in full: values are quoted on one line and cut short.
"""

import difflib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from arr.errors import ConfigIssue
from arr.models import (
    ContextMode,
    FolderMode,
    GateRule,
    GateType,
    Goto,
    PhaseTopology,
    Pick,
    ResolvedAction,
    ResolvedPhase,
    ResolvedRun,
    ResolvedTask,
    Route,
    RouteKeyword,
    Rubric,
    TopologyKind,
    WorkspaceMode,
)
from arr.plan import graph as graphs
from arr.plan.read import ACTION_GATE, ACTION_WORK, build_expectation
from arr.submissions.formats import (
    BUILTIN,
    DEFAULT_LIST_ITEMS,
    PUBLIC_KINDS,
    RESERVED_NAMES,
    VERDICTS,
    FieldKind,
    FormatSpec,
    format_for,
)

__all__ = ["PlanCheck", "check"]

STATUSES = ("done", "not_solved")
"""Values of `status` in the head of every submission."""
FIXED_FIELDS = "status, verdict, choice oder ein „choice“-Feld eines eigenen Formats"
_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
_GATE_KEYS = {
    GateType.DONE: {"routes"},
    GateType.SIGNAL: {"field", "rule", "routes", "on_tie"},
    GateType.JUDGE: {"raters", "action", "rule", "pass_score", "criteria", "routes", "on_tie"},
    GateType.USER: set(),
}
"""Keys of the gate that fit its `type` (docs/10 §4.10)."""


@dataclass
class PlanCheck:
    """Result of `check`: errors (the run cannot start) and hints (shown, nothing blocked)."""

    errors: list[ConfigIssue] = field(default_factory=list)
    warnings: list[ConfigIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True without errors."""
        return not self.errors


def _q(value: Any, limit: int = 40) -> str:
    """A value of the file for a message: „…“, on one line, cut after `limit` characters."""
    text = " ".join(str(value).split())
    return "„" + (text if len(text) <= limit else text[: limit - 1] + "…") + "“"


def _suggest(value: str, known: list[str]) -> str:
    close = difflib.get_close_matches(value, known, n=1, cutoff=0.5)
    return f" Meintest du {_q(close[0])}?" if close else ""


def _route_text(route: Route | None) -> str:
    if isinstance(route, Goto):
        return f"{{ goto: {route.goto} }}"
    return str(route.value if isinstance(route, RouteKeyword) else route)


class _Checker:
    """Collects the issues of one run; one method per group of rules."""

    def __init__(self, run: ResolvedRun) -> None:
        self.run = run
        self.result = PlanCheck()
        self.agents = {a.id: a for a in run.agents}
        self.channels = {c.name: c for c in run.groupchats}
        self.phase_names = [p.name for p in run.phases]
        self.rubric = run.rubric or Rubric()

    def error(self, path: str, message: str) -> None:
        self.result.errors.append(ConfigIssue(path, message))

    def warn(self, path: str, message: str) -> None:
        self.result.warnings.append(ConfigIssue(path, message))

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    def at(phase: ResolvedPhase, *parts: str | int) -> str:
        path = f"phases[{phase.index}]"
        for part in parts:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        return path

    def action_path(self, phase: ResolvedPhase, action: ResolvedAction, *parts: str) -> str:
        return self.at(phase, "actions", action.key, *parts)

    def tasks(self, phase: ResolvedPhase) -> Iterator[tuple[ResolvedTask, str, str | None]]:
        """Every action of a phase with its path and agent; the gate's action without one."""
        for action in phase.actions:
            yield action, self.action_path(phase, action), action.agent
        if phase.gate.action is not None:
            yield phase.gate.action, self.at(phase, "gate", "action"), None

    def own_format(self, name: str) -> FormatSpec | None:
        return self.run.formats.get(name) if name not in BUILTIN else None

    # -- S: structure ----------------------------------------------------------------------

    def structure(self) -> None:
        run = self.run
        if not run.phases:
            self.error(
                "phases",
                "„topology: phases“ braucht mindestens eine Phase. Es ist keine angegeben, oder "
                "alle entfallen durch „when“ bzw. ausgeschaltete Agenten. Lege unter „phases“ "
                "eine Phase an oder prüfe die Bedingungen.",
            )
        if run.stages:
            self.error(
                "stages",
                "„stages“ gibt es bei „topology: phases“ nicht. Schreibe je Stufe eine Phase "
                "„solo“ mit einem Gate „judge“ (Regel „mean“, „pass_score“).",
            )
        if run.raters:
            self.error(
                "raters",
                "„raters“ gibt es bei „topology: phases“ nicht auf Ebene der Group. Nenne die "
                "Rater am Gate „judge“ der Phase („gate.raters“).",
            )
        for index, chat in enumerate(run.groupchats):
            if chat.open_from is not None:
                self.error(
                    f"channels[{index}].open_from",
                    f"„open_from“ gibt es bei „topology: phases“ nicht (Kanal {_q(chat.name)}). "
                    "Nenne den Kanal unter „channels“ der Phasen, in denen er offen ist.",
                )
            if chat.mode.value == "moderated":
                self.error(
                    f"channels[{index}].mode",
                    f"Ein Kanal mit „mode: moderated“ gibt es bei „topology: phases“ nicht (Kanal "
                    f"{_q(chat.name)}). Nimm eine Phase mit „topology: moderated“.",
                )
        seen: dict[str, ResolvedPhase] = {}
        for phase in run.phases:
            if phase.name in seen:
                self.error(
                    self.at(phase, "name"),
                    f"Die Phase {_q(phase.name)} gibt es mehrfach (erste Stelle: "
                    f"phases[{seen[phase.name].index}]). Benenne eine um.",
                )
            seen.setdefault(phase.name, phase)
        for phase in run.phases:
            self.keys(phase)
            self.participants(phase)
            self.phase_channels(phase)

    def keys(self, phase: ResolvedPhase) -> None:
        """S4: keys that fit the topology."""
        topo = phase.topology
        wrong: list[tuple[str, str]] = []
        if phase.rounds is not None and topo is not PhaseTopology.CHAT:
            wrong.append(("rounds", "nur bei „chat“"))
        if phase.isolated and topo is not PhaseTopology.PARALLEL:
            wrong.append(("isolated", "nur bei „parallel“"))
        if phase.over is not None and topo is not PhaseTopology.PARALLEL:
            wrong.append(("over", "nur bei „parallel“"))
        if phase.repeat != 1 and topo not in (PhaseTopology.SOLO, PhaseTopology.PARALLEL):
            wrong.append(("repeat", "nur bei „solo“ und „parallel“"))
        if phase.max_assignments is not None and topo is not PhaseTopology.MODERATED:
            wrong.append(("max_assignments", "nur bei „moderated“"))
        for key, where in wrong:
            self.error(
                self.at(phase, key),
                f"„{key}“ gibt es {where}, die Phase {_q(phase.name)} ist „{topo.value}“. "
                f"Entferne „{key}“ oder ändere „topology“.",
            )
        if topo is PhaseTopology.MODERATED and phase.max_assignments is None:
            self.error(
                self.at(phase, "max_assignments"),
                f"Die Phase {_q(phase.name)} ist „moderated“ und braucht „max_assignments“, die "
                "Höchstzahl der Aufträge des Leads, z. B. „max_assignments: 5“.",
            )
        if phase.standby and phase.isolated:
            self.error(
                self.at(phase, "standby"),
                f"Die Phase {_q(phase.name)} ist isoliert und kann keine Agenten auf Abruf haben: "
                "ein gemeinsamer Sleeper trüge Wissen zwischen den Teilnehmern weiter "
                "(Entscheidung 15). Entferne „standby“ oder „isolated“.",
            )

    def participants(self, phase: ResolvedPhase) -> None:
        """S5: every entry names an agent, nobody twice, the right number of agents."""
        known = list(self.agents)
        holders: dict[str, str] = {}
        for action in phase.actions:
            path = self.action_path(phase, action)
            if action.agent not in self.agents:
                self.error(
                    path,
                    f"Unbekannter Agent {_q(action.agent)}.{_suggest(action.agent, known)}",
                )
            if action.agent in holders and holders[action.agent] != action.key:
                self.error(
                    path,
                    f"Der Agent {_q(action.agent)} hat in der Phase {_q(phase.name)} schon eine "
                    f"Action (über {_q(holders[action.agent])}). Ein Agent steht in einer Phase "
                    "nur einmal; ändere eins der Muster.",
                )
            holders.setdefault(action.agent, action.key)
        for index, agent in enumerate(phase.standby):
            path = self.at(phase, "standby", index)
            if agent not in self.agents:
                self.error(path, f"Unbekannter Agent {_q(agent)}.{_suggest(agent, known)}")
            if agent in holders:
                self.error(
                    path,
                    f"Der Agent {_q(agent)} hat in der Phase {_q(phase.name)} eine Action und "
                    "kann nicht zugleich auf Abruf sein. Entferne ihn aus „standby“.",
                )
        if phase.standby and len(set(phase.standby)) != len(phase.standby):
            self.error(self.at(phase, "standby"), "„standby“ nennt einen Agenten doppelt.")
        agents = phase.agents
        if phase.topology is PhaseTopology.SOLO and len(agents) != 1:
            self.error(
                self.at(phase, "actions"),
                f"Eine Phase „solo“ hat genau einen Agenten mit Action, {_q(phase.name)} hat "
                f"{len(agents)}. Nimm „parallel“ oder nenne nur einen Agenten.",
            )
        if phase.topology is PhaseTopology.MODERATED:
            if len(agents) != 1:
                self.error(
                    self.at(phase, "actions"),
                    f"In „moderated“ hat genau ein Agent eine Action, der Lead; {_q(phase.name)} "
                    f"hat {len(agents)}. Die anderen gehören unter „standby“.",
                )
            if not phase.standby:
                self.error(
                    self.at(phase, "standby"),
                    f"Die Phase {_q(phase.name)} ist „moderated“ und braucht mindestens einen "
                    "Agenten unter „standby“, an den der Lead Aufträge vergibt.",
                )

    def phase_channels(self, phase: ResolvedPhase) -> None:
        """S6: channels exist; `chat` discusses in its first; isolation allows only own ones."""
        for index, name in enumerate(phase.channels):
            if name not in self.channels:
                self.error(
                    self.at(phase, "channels", index),
                    f"Den Kanal {_q(name)} gibt es nicht.{_suggest(name, list(self.channels))}",
                )
        if phase.topology is PhaseTopology.CHAT:
            if not phase.channels:
                self.error(
                    self.at(phase, "channels"),
                    f"Die Phase {_q(phase.name)} ist „chat“ und braucht unter „channels“ den "
                    "Kanal, in dem diskutiert wird (der erste Eintrag).",
                )
            elif phase.channels[0] in self.channels:
                members = set(self.channels[phase.channels[0]].members)
                missing = [a for a in phase.agents if a not in members]
                if missing:
                    self.error(
                        self.at(phase, "channels", 0),
                        f"Im Diskussionskanal {_q(phase.channels[0])} fehlen "
                        f"{', '.join(missing)}. Jeder Agent mit Action muss Mitglied des ersten "
                        "Kanals sein (Entscheidung 39).",
                    )
        if phase.isolated:
            for index, name in enumerate(phase.channels):
                chat = self.channels.get(name)
                if chat is not None and len(chat.members) != 1:
                    self.error(
                        self.at(phase, "channels", index),
                        f"Die Phase {_q(phase.name)} ist isoliert; offen bleibt nur ein Kanal "
                        f"mit genau einem Mitglied, {_q(name)} hat {len(chat.members)} "
                        "(Entscheidung 15). Entferne den Kanal aus der Phase.",
                    )

    # -- B: actions and submissions ----------------------------------------------------------

    def formats(self) -> None:
        """B1 for the own formats themselves."""
        for name, spec in self.run.formats.items():
            path = f"formats.{name}"
            if name in BUILTIN:
                self.error(
                    path,
                    f"Das Format {_q(name)} ist eingebaut ({', '.join(BUILTIN)}). Gib dem eigenen "
                    "Format einen anderen Namen.",
                )
                continue
            if spec.content or spec.needs_files:
                self.error(path, "Ein eigenes Format hat nur „fields“. Entferne den Rest.")
            if spec.kinds() - set(PUBLIC_KINDS):
                self.error(
                    path, "Ein eigenes Format nutzt nur die Feldarten choice, number, text, list."
                )
            if not spec.fields:
                self.error(path, "Ein eigenes Format braucht mindestens ein Feld unter „fields“.")
            for field_name, spec_field in _walk_fields(spec):
                if spec_field.type is FieldKind.CHOICE and not spec_field.choices:
                    self.error(
                        f"{path}.fields.{field_name}",
                        "Ein Feld vom Typ „choice“ braucht „choices“.",
                    )

    def actions(self) -> None:
        """B1–B3, B7, B8 for every action and gate action."""
        names: dict[str, tuple[str, str]] = {}
        for seed_name, seed in self.run.seed.items():
            kind = "artifact" if seed.file is not None else "list"
            names[seed_name] = (kind, f"seed.{seed_name}")
        for phase in self.run.phases:
            for task, path, agent in self.tasks(phase):
                fmt = task.submission.format
                if fmt not in BUILTIN and fmt not in self.run.formats:
                    known = [*BUILTIN, *self.run.formats]
                    self.error(
                        f"{path}.submission.format",
                        f"Das Format {_q(fmt)} gibt es nicht.{_suggest(fmt, known)} Eingebaut: "
                        f"{', '.join(BUILTIN)}; eigene stehen unter „formats“.",
                    )
                    continue
                self.submission_name(task, path, names)
                if (task.submission.choices is not None) != (fmt == "decision"):
                    self.error(
                        f"{path}.submission.choices",
                        "„choices“ steht genau bei „format: decision“ und ist dort Pflicht: die "
                        "Möglichkeiten, aus denen der Agent wählt.",
                    )
                self.task_path(task.task, f"{path}.task")
                self.expectation(phase, task, path, agent)

    def submission_name(
        self, task: ResolvedTask, path: str, names: dict[str, tuple[str, str]]
    ) -> None:
        """B2: a plain name, not reserved, one format per name (seeds included)."""
        name = task.submission.name
        if name is None:
            return
        where = f"{path}.submission.name"
        if not (name[:1].isalnum() and set(name) <= _SLUG_CHARS and len(name) <= 64):
            self.error(
                where,
                f"Der Name {_q(name)} ist kein einfacher Bezeichner. Erlaubt sind Kleinbuchstaben, "
                "Ziffern, „-“ und „_“, z. B. „beweis“.",
            )
            return
        if name in RESERVED_NAMES:
            self.error(
                where,
                f"Der Name {_q(name)} ist in output/ schon belegt "
                f"({', '.join(sorted(RESERVED_NAMES))}). Wähle einen anderen.",
            )
            return
        fmt = task.submission.format
        if name in names and names[name][0] != fmt:
            first_format, first_path = names[name]
            self.error(
                where,
                f"Der Name {_q(name)} hat hier das Format {_q(fmt)}, bei {first_path} aber "
                f"{_q(first_format)}. Fassungen eines Namens haben ein Format (ein „seed“ mit "
                "„file“ ist ein „artifact“, mit „param“ eine „list“).",
            )
        names.setdefault(name, (fmt, where))

    def task_path(self, task: str, path: str) -> None:
        """B7: a relative path without `..`."""
        pure = PurePosixPath(task)
        if pure.is_absolute() or ".." in pure.parts or task.startswith("~") or "\\" in task:
            self.error(
                path,
                f"Der Auftrag {_q(task)} muss ein relativer Pfad im Skill oder Projekt sein, ohne "
                "„..“, z. B. „tasks/pruefen.md“.",
            )

    def expectation(
        self, phase: ResolvedPhase, task: ResolvedTask, path: str, agent: str | None
    ) -> None:
        """B8: arr can build the expectation and `format_for` takes it."""
        try:
            expectation = build_expectation(
                self.run,
                phase,
                task,
                action=ACTION_WORK if agent is not None else ACTION_GATE,
                rate=[1] if task.submission.format == "rating" else None,
            )
            format_for(expectation, self.run.rubric)
        except (ValidationError, ValueError, KeyError) as exc:
            self.error(
                f"{path}.submission",
                "Aus dieser Abgabe lässt sich keine gültige Erwartung bauen, „submit“ müsste "
                f"jede Abgabe ablehnen: {_first_message(exc)}",
            )

    def over_and_inputs(self) -> None:
        """B4, B5."""
        lists = {
            a.submission.name: phase.name
            for phase in self.run.phases
            for a in phase.actions
            if a.submission.format == "list" and a.submission.name
        }
        produced = {
            task.submission.name
            for phase in self.run.phases
            for task, _path, _agent in self.tasks(phase)
            if task.submission.name
        }
        for phase in self.run.phases:
            if phase.over is not None:
                other_phases = {
                    p.name
                    for p in self.run.phases
                    if p.name != phase.name
                    for a in p.actions
                    if a.submission.format == "list" and a.submission.name == phase.over
                }
                seed = self.run.seed.get(phase.over)
                if not other_phases and not (seed is not None and seed.param is not None):
                    hint = (
                        " Die Liste entsteht nur in dieser Phase selbst."
                        if lists.get(phase.over) == phase.name
                        else ""
                    )
                    self.error(
                        self.at(phase, "over"),
                        f"„over: {phase.over}“ braucht eine Liste: eine Abgabe mit „format: list“ "
                        f"und „name: {phase.over}“ in einer anderen Phase oder einen „seed“ mit "
                        f"„param“.{hint}",
                    )
            for index, entry in enumerate(phase.inputs or []):
                self.input_entry(phase, index, entry, produced)

    def input_entry(self, phase: ResolvedPhase, index: int, entry: Any, produced: set[str]) -> None:
        path = self.at(phase, "inputs", index)
        sources = [
            key
            for key, value in (
                ("from", entry.from_),
                ("name", entry.name),
                ("channel", entry.channel),
                ("peers", entry.peers),
            )
            if value is not None
        ]
        if len(sources) != 1:
            self.error(
                path,
                "Ein Eintrag in „inputs“ hat genau eine Quelle: „from: <phase>“, „name: <abgabe>“, "
                f"„channel: <kanal>“ oder „peers: summary“; hier {len(sources)}.",
            )
            return
        if entry.from_ is not None and entry.from_ not in self.phase_names:
            self.error(
                f"{path}.from",
                f"Die Phase {_q(entry.from_)} gibt es nicht."
                f"{_suggest(entry.from_, self.phase_names)}",
            )
        known_names = produced | set(self.run.seed)
        if entry.name is not None and entry.name not in known_names:
            self.error(
                f"{path}.name",
                f"Keine Phase gibt eine Abgabe {_q(entry.name)} ab, und es gibt keinen solchen "
                f"„seed“.{_suggest(entry.name, sorted(known_names))}",
            )
        if entry.channel is not None and entry.channel not in self.channels:
            self.error(
                f"{path}.channel",
                f"Den Kanal {_q(entry.channel)} gibt es nicht."
                f"{_suggest(entry.channel, list(self.channels))}",
            )
        if entry.peers is not None and phase.repeat <= 1:
            self.error(
                f"{path}.peers",
                "„peers: summary“ zeigt die Kurzfassungen der anderen aus der vorigen "
                "Wiederholung und braucht „repeat“ größer 1.",
            )
        if entry.pick is Pick.BEST:
            source = next((p for p in self.run.phases if p.name == entry.from_), None)
            if (
                source is None
                or source.gate.type is not GateType.JUDGE
                or source.gate.rule not in (GateRule.MEAN, GateRule.RANK)
            ):
                self.error(
                    f"{path}.pick",
                    "„pick: best“ geht nur mit „from“ aus einer Phase mit Gate „judge“ und "
                    "Regel „mean“ oder „rank“; nur dort gibt es Punkte.",
                )

    def seeds(self) -> None:
        """B6."""
        for name, seed in self.run.seed.items():
            path = f"seed.{name}"
            if seed.file is not None:
                self.task_path(seed.file, f"{path}.file")
                continue
            if seed.param not in self.run.params:
                self.error(f"{path}.param", f"Den Parameter {_q(seed.param)} gibt es nicht.")
                continue
            items = seed.items
            if not isinstance(items, list):
                self.error(
                    f"{path}.param",
                    f"Der Parameter {_q(seed.param)} ist keine Liste. „seed“ mit „param“ braucht "
                    "eine Liste von Texten oder Einträgen mit „title“ und „text“.",
                )
                continue
            if len(items) > DEFAULT_LIST_ITEMS:
                self.error(
                    f"{path}.param",
                    f"Die Liste aus {_q(seed.param)} hat {len(items)} Einträge, eine „list“ "
                    f"höchstens {DEFAULT_LIST_ITEMS}.",
                )
            bad = [
                i
                for i, item in enumerate(items)
                if not isinstance(item, str)
                and not (
                    isinstance(item, dict)
                    and set(item) <= {"title", "text"}
                    and all(isinstance(v, str) for v in item.values())
                )
            ]
            if bad:
                self.error(
                    f"{path}.param",
                    f"Eintrag {bad[0] + 1} der Liste aus {_q(seed.param)} ist kein Text und kein "
                    "Eintrag mit „title“ und „text“.",
                )

    # -- G: gates ----------------------------------------------------------------------------

    def gates(self) -> None:
        for phase in self.run.phases:
            gate = phase.gate
            given = {
                key
                for key, value in (
                    ("field", gate.field),
                    ("rule", gate.rule),
                    ("routes", gate.routes),
                    ("on_tie", gate.on_tie),
                    ("raters", gate.raters),
                    ("action", gate.action),
                    ("pass_score", gate.pass_score),
                    ("criteria", gate.criteria),
                )
                if value not in (None, [], {})
            }
            for key in sorted(given - _GATE_KEYS[gate.type]):
                text = (
                    "Ein Gate „user“ hat keine Routen: der User wählt weiter, wiederholen mit "
                    "Hinweis oder Stopp."
                    if gate.type is GateType.USER and key == "routes"
                    else f"„{key}“ passt nicht zu einem Gate „{gate.type.value}“."
                )
                self.error(self.at(phase, "gate", key), f"{text} Entferne „{key}“.")
            for outcome, route in gate.routes.items():
                self.goto(phase, route, self.at(phase, "gate", "routes", outcome))
            if gate.on_tie is not None:
                self.goto(phase, gate.on_tie, self.at(phase, "gate", "on_tie"))
            if gate.type is GateType.DONE:
                self.outcomes(phase, ["done"])
            elif gate.type is GateType.SIGNAL:
                self.signal(phase)
            elif gate.type is GateType.JUDGE:
                self.judge(phase)

    def goto(self, phase: ResolvedPhase, route: Route, path: str) -> None:
        """G1 on the resolved run: a goto names a phase of the run."""
        if isinstance(route, Goto) and route.goto not in self.phase_names:
            self.error(
                path,
                f"Die Phase {_q(route.goto)} gibt es nicht."
                f"{_suggest(route.goto, self.phase_names)}",
            )

    def outcomes(self, phase: ResolvedPhase, expected: list[str]) -> None:
        """Every outcome of the gate has exactly one route, and there is no other."""
        routes = phase.gate.routes
        missing = [o for o in expected if o not in routes]
        foreign = [o for o in routes if o not in expected]
        if missing:
            self.error(
                self.at(phase, "gate", "routes"),
                f"Es fehlt eine Route für {', '.join(missing)}. Das Gate „{phase.gate.type.value}“ "
                f"der Phase {_q(phase.name)} hat die Ergebnisse {', '.join(expected)}.",
            )
        for outcome in foreign:
            self.error(
                self.at(phase, "gate", "routes", outcome),
                f"Das Gate „{phase.gate.type.value}“ hat kein Ergebnis {_q(outcome)}."
                f"{_suggest(outcome, expected)} Ergebnisse: {', '.join(expected)}.",
            )

    def field_values(self, task: ResolvedTask, name: str) -> list[str] | None:
        """The fixed values of field `name` in the format of an action, None if it has none."""
        fmt = task.submission.format
        if name == "status":
            return list(STATUSES)
        if fmt == "review" and name == "verdict":
            return list(VERDICTS)
        if fmt == "decision" and name == "choice":
            return list(task.submission.choices or [])
        own = self.own_format(fmt)
        if own is not None and name in own.fields and own.fields[name].type is FieldKind.CHOICE:
            return list(own.fields[name].choices or [])
        return None

    def signal(self, phase: ResolvedPhase) -> None:
        """G3, G4 (W4b-02a §6.5)."""
        gate = phase.gate
        if gate.field is None:
            self.error(
                self.at(phase, "gate", "field"),
                f"Ein Gate „signal“ braucht „field“, das Feld der Abgabe mit festen Werten "
                f"({FIXED_FIELDS}).",
            )
            return
        values: list[str] | None = None
        for action in phase.actions:
            found = self.field_values(action, gate.field)
            if found is None:
                self.error(
                    self.action_path(phase, action, "submission", "format"),
                    f"Die Abgabe im Format {_q(action.submission.format)} hat kein Feld "
                    f"{_q(gate.field)} mit festen Werten ({FIXED_FIELDS}).",
                )
                return
            if values is None:
                values = found
            elif sorted(found) != sorted(values):
                self.error(
                    self.action_path(phase, action, "submission"),
                    f"Das Feld {_q(gate.field)} hat bei den Actions der Phase {_q(phase.name)} "
                    "verschiedene Werte; das Gate braucht überall dieselben.",
                )
                return
        if values is None:
            return
        self.outcomes(phase, values)
        agents = len(phase.agents)
        rule = gate.rule
        if rule is None and agents >= 2:
            self.error(
                self.at(phase, "gate", "rule"),
                f"Die Phase {_q(phase.name)} hat {agents} Agenten mit Action; das Gate braucht "
                "„rule“ (all, any, majority oder unanimous).",
            )
            return
        if rule in (GateRule.MEAN, GateRule.RANK):
            self.error(
                self.at(phase, "gate", "rule"),
                f"„{rule.value}“ rechnet mit Ratings und gehört an ein Gate „judge“. Ein Gate "
                "„signal“ kennt all, any, majority und unanimous.",
            )
            return
        if rule in (GateRule.ALL, GateRule.ANY) and gate.field != "status":
            self.error(
                self.at(phase, "gate", "rule"),
                f"„{rule.value}“ gibt es nur für das Feld „status“ (Entscheidung 38). Nimm "
                "„majority“ oder „unanimous“.",
            )
            return
        needs_tie = gate.field != "verdict" and (
            rule is GateRule.UNANIMOUS or (rule is GateRule.MAJORITY and agents >= 2)
        )
        if needs_tie and gate.on_tie is None:
            self.error(
                self.at(phase, "gate", "on_tie"),
                f"Bei „{rule.value if rule else ''}“ über {_q(gate.field)} ist ein Gleichstand "
                "möglich; das Gate braucht eine Route „on_tie“ (Entscheidung 8, 38).",
            )
        if not needs_tie and gate.on_tie is not None:
            self.warn(
                self.at(phase, "gate", "on_tie"),
                "Hier ist kein Gleichstand möglich, „on_tie“ wird nie genommen. Du kannst es "
                "entfernen.",
            )

    def judge(self, phase: ResolvedPhase) -> None:
        """G5."""
        gate = phase.gate
        if not gate.raters:
            self.error(
                self.at(phase, "gate", "raters"),
                f"Das Gate „judge“ der Phase {_q(phase.name)} hat keinen aktiven Rater. Nenne "
                "unter „raters“ mindestens einen Agenten, der nicht ausgeschaltet ist.",
            )
        for index, rater in enumerate(gate.raters):
            if rater in phase.agents:
                self.error(
                    self.at(phase, "gate", "raters", index),
                    f"Der Rater {_q(rater)} hat in der Phase {_q(phase.name)} eine Action und "
                    "würde sich selbst bewerten. Nimm ihn aus „raters“ oder aus „actions“.",
                )
            if rater not in self.agents:
                self.error(
                    self.at(phase, "gate", "raters", index), f"Unbekannter Agent {_q(rater)}."
                )
        rule = gate.rule
        if rule not in (GateRule.MEAN, GateRule.RANK, GateRule.MAJORITY, GateRule.UNANIMOUS):
            self.error(
                self.at(phase, "gate", "rule"),
                "Ein Gate „judge“ braucht „rule“: mean (Schwelle), rank (Rangliste), majority "
                "oder unanimous (Urteile).",
            )
            return
        wanted = "rating" if rule in (GateRule.MEAN, GateRule.RANK) else "review"
        if gate.action is None:
            self.error(
                self.at(phase, "gate", "action"),
                f"Ein Gate „judge“ braucht „action“, die Bewertungs-Action der Rater mit "
                f"„format: {wanted}“.",
            )
        elif gate.action.submission.format != wanted:
            self.error(
                self.at(phase, "gate", "action", "submission", "format"),
                f"Bei „rule: {rule.value}“ geben die Rater „{wanted}“ ab, nicht "
                f"{_q(gate.action.submission.format)}.",
            )
        if gate.criteria is not None and wanted != "rating":
            self.error(
                self.at(phase, "gate", "criteria"),
                "„criteria“ gibt es nur, wenn die Rater ein „rating“ abgeben.",
            )
        single = len(phase.agents) == 1 and phase.over is None
        if rule is not GateRule.RANK and not single:
            self.error(
                self.at(phase, "gate", "rule"),
                f"„rule: {rule.value}“ setzt genau eine Abgabe je Durchlauf voraus: ein Agent "
                "mit Action und kein „over“ (Entscheidung 41). Für mehrere Abgaben nimm "
                "„rank“.",
            )
        if rule is GateRule.MEAN:
            low, high = self.rubric.scale
            if gate.pass_score is None:
                self.error(
                    self.at(phase, "gate", "pass_score"),
                    "„rule: mean“ braucht eine Schwelle: „pass_score“ am Gate oder in der Rubrik.",
                )
            elif not low <= gate.pass_score <= high:
                self.error(
                    self.at(phase, "gate", "pass_score"),
                    f"„pass_score“ {gate.pass_score:g} liegt außerhalb der Skala "
                    f"[{low:g}, {high:g}].",
                )
        expected = {
            GateRule.MEAN: ["pass", "fail"],
            GateRule.RANK: ["ranked"],
        }.get(rule, list(VERDICTS))
        self.outcomes(phase, expected)
        if gate.on_tie is not None:
            self.warn(
                self.at(phase, "gate", "on_tie"),
                "Ein Gate „judge“ entscheidet Gleichstände selbst (das strengere Urteil, die "
                "Rangliste), „on_tie“ wird nie genommen.",
            )

    # -- L: loops ----------------------------------------------------------------------------

    def loops(self) -> None:
        phases = self.run.phases
        graph = graphs.build(self.run)
        for phase in phases:
            routes = [*phase.gate.routes.items()]
            if phase.gate.on_tie is not None:
                routes.append(("on_tie", phase.gate.on_tie))
            for outcome, route in routes:
                if route is RouteKeyword.RETRY and phase.max_visits is None:
                    path = (
                        self.at(phase, "gate", "on_tie")
                        if outcome == "on_tie"
                        else self.at(phase, "gate", "routes", outcome)
                    )
                    self.error(
                        path,
                        f"„retry“ wiederholt {_q(phase.name)} und braucht dort „max_visits“, die "
                        "Höchstzahl der Durchläufe, und „on_exhausted“.",
                    )
            if phase.max_visits is not None and phase.on_exhausted is None:
                self.error(
                    self.at(phase, "on_exhausted"),
                    f"Die Phase {_q(phase.name)} hat „max_visits“ und braucht „on_exhausted“: die "
                    "Route, wenn die Durchläufe aufgebraucht sind, z. B. „next“ oder „stop“.",
                )
            if phase.on_exhausted is not None:
                if phase.max_visits is None:
                    self.warn(
                        self.at(phase, "on_exhausted"),
                        "„on_exhausted“ gilt nur mit „max_visits“ und wird hier nie genommen.",
                    )
                if phase.on_exhausted is RouteKeyword.RETRY:
                    self.error(
                        self.at(phase, "on_exhausted"),
                        "„on_exhausted“ kann nicht „retry“ sein: die Durchläufe sind ja "
                        "aufgebraucht. Nimm next, stop, escalate oder „{ goto: … }“.",
                    )
                self.goto(phase, phase.on_exhausted, self.at(phase, "on_exhausted"))
        reported: set[frozenset[int]] = set()
        for node in range(len(phases)):
            cycle = graphs.exhaust_cycle(phases, node)
            if cycle and frozenset(cycle) not in reported:
                reported.add(frozenset(cycle))
                names = " → ".join(phases[i].name for i in [*cycle, cycle[0]])
                self.error(
                    self.at(phases[cycle[0]], "on_exhausted"),
                    f"Die Routen „on_exhausted“ führen im Kreis ({names}). Mindestens eine muss "
                    "aus dem Kreis heraus führen, z. B. „next“ oder „stop“.",
                )
        cycle = graphs.unbounded_cycle(graph)
        if cycle:
            names = " → ".join(phases[i].name for i in [*cycle, cycle[0]])
            self.error(
                self.at(phases[cycle[0]], "max_visits"),
                f"Die Schleife {names} hat keine Grenze: keine ihrer Phasen hat „max_visits“ "
                "(auch über aufgebrauchte Phasen gezählt). Gib einer Phase der Schleife "
                "„max_visits“ und „on_exhausted“.",
            )
        reach = graphs.reachable(graph)
        for node, phase in enumerate(phases):
            if node not in reach:
                self.error(
                    self.at(phase, "name"),
                    f"Die Phase {_q(phase.name)} ist von der ersten Phase aus über keine Route "
                    "erreichbar. Leite eine Route dorthin („next“ oder „{ goto: … }“) oder "
                    "entferne sie.",
                )

    # -- I: isolation and folders ------------------------------------------------------------

    def isolation(self) -> None:
        for phase in self.run.phases:
            if phase.isolated:
                folders: dict[str, list[str]] = {}
                for agent_id in phase.agents:
                    agent = self.agents.get(agent_id)
                    if agent is None:
                        continue
                    if agent.workspace.mode is WorkspaceMode.SHARED:
                        folders.setdefault(str(agent.workspace.name), []).append(agent_id)
                    for entry in agent.read_dirs:
                        if entry.startswith("run:") and entry != "run:output":
                            self.error(
                                self.at(phase, "isolated"),
                                f"Der Agent {_q(agent_id)} liest den Run-Ordner {_q(entry)}. In "
                                f"der isolierten Phase {_q(phase.name)} lesen die Agenten im Run "
                                "nur ihren Arbeitsordner und „output/“; ändere „read_dirs“.",
                            )
                for folder, shared in folders.items():
                    if len(shared) > 1:
                        self.error(
                            self.at(phase, "isolated"),
                            f"{', '.join(shared)} teilen den Arbeitsordner {_q(folder)}. In der "
                            f"isolierten Phase {_q(phase.name)} braucht jeder einen eigenen "
                            "(„workspace“).",
                        )
            participants = set(phase.participants)
            for key, values in (("context", phase.context), ("folder", phase.folder)):
                for agent_id in values:
                    if agent_id not in participants:
                        self.error(
                            self.at(phase, key),
                            f"{_q(agent_id)} ist in der Phase {_q(phase.name)} kein Teilnehmer "
                            "(Action, „standby“, Rater). Entferne den Eintrag.",
                        )
            for agent_id, mode in phase.folder.items():
                agent = self.agents.get(agent_id)
                shared = agent is not None and agent.workspace.mode is WorkspaceMode.SHARED
                if shared and mode is FolderMode.FRESH:
                    self.error(
                        self.at(phase, "folder"),
                        f"{_q(agent_id)} arbeitet im gemeinsamen Ordner und kann keinen frischen "
                        "bekommen: das nähme den anderen die Arbeit weg. Setze „folder: keep“.",
                    )
            for agent_id, mode in phase.context.items():
                agent = self.agents.get(agent_id)
                shared = agent is not None and agent.workspace.mode is WorkspaceMode.SHARED
                if shared and mode is ContextMode.RESET:
                    self.warn(
                        self.at(phase, "context"),
                        f"{_q(agent_id)} arbeitet im gemeinsamen Ordner: „reset“ erneuert nur die "
                        "Sitzung, die Notizen im gemeinsamen Ordner überleben ihn.",
                    )

    def run_all(self) -> PlanCheck:
        self.structure()
        self.formats()
        self.actions()
        self.over_and_inputs()
        self.seeds()
        self.gates()
        self.loops()
        self.isolation()
        return self.result


def _walk_fields(spec: FormatSpec) -> Iterator[tuple[str, Any]]:
    stack = [(name, f) for name, f in spec.fields.items()]
    while stack:
        name, spec_field = stack.pop()
        yield name, spec_field
        if spec_field.item is not None:
            stack.append((f"{name}.item", spec_field.item))
        for inner, value in (spec_field.fields or {}).items():
            stack.append((f"{name}.fields.{inner}", value))


def _first_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            ctx = errors[0].get("ctx") or {}
            text = str(ctx.get("error", errors[0]["msg"]))
            return text.removeprefix("Value error, ")
    return str(exc)


def check(resolved: ResolvedRun) -> PlanCheck:
    """Every static rule of W4b-02a §6.4 and §6.5 for a resolved `phases` run; errors and
    hints apart, all at once. A run of another topology has nothing to check here."""
    if resolved.topology.kind is not TopologyKind.PHASES:
        return PlanCheck()
    return _Checker(resolved).run_all()

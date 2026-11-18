"""W4b-02a: `arr.plan.check` and the skills page (docs/10 §4.7, §4.10, §4.12, §7).

A6: every rule S1–S6, B1–B8, G1–G5, L1–L4, I1–I3 reports its error with a path, and a close
variant passes (counter-check); several errors come together. A7: the rules at a gate `signal`
(W4b-02a §6.5). A8: the hint of I2. A11: the skills page. A16: limits and hostile input.
Mutations start from the example of docs/10 §4.9 (phases: 0 ideen, 1 exploration, 2 beweis,
3 pruefung, 4 urteil, 5 revision, 6 zusammenfassung). No model, no run.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from plan_kit import findings, load, resolve, text
from run_kit import make_workfolder, write_profiles

from arr.errors import ArrError
from arr.models import (
    MAX_ASSIGNMENTS,
    MAX_FORMATS,
    MAX_INPUTS,
    MAX_PHASES,
    MAX_REPEAT,
    MAX_ROUNDS,
    MAX_ROUTES,
    MAX_TURNS,
    MAX_VISITS,
    WorkfolderSettings,
)
from arr.plan.check import check
from arr.plan.estimate import turn_range
from arr.runs.launch import plan_launch
from arr.service.api_models import Severity
from arr.service.real_api.catalog import _check_group, check_group
from arr.skills import discover_skills

Mutation = Callable[[dict[str, Any]], Any]


def _ph(data: dict[str, Any], index: int) -> dict[str, Any]:
    return data["phases"][index]


def _set(index: int, **values: Any) -> Mutation:
    return lambda d: _ph(d, index).update(values)


def _action(index: int, agent: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda d: _ph(d, index)["actions"][agent]


def _submission(index: int, agent: str, **values: Any) -> Mutation:
    return lambda d: _ph(d, index)["actions"][agent]["submission"].update(values)


def _gate(index: int, **values: Any) -> Mutation:
    return lambda d: _ph(d, index)["gate"].update(values)


def _both(*steps: Mutation) -> Mutation:
    def run(data: dict[str, Any]) -> None:
        for step in steps:
            step(data)

    return run


def _drop(index: int, *keys: str) -> Mutation:
    def run(data: dict[str, Any]) -> None:
        for key in keys:
            _ph(data, index).pop(key, None)

    return run


def _gate_drop(index: int, key: str) -> Mutation:
    return lambda d: _ph(d, index)["gate"].pop(key)


def _shared(agent: int, name: str = "tisch") -> Mutation:
    return lambda d: d["agents"][agent].update(workspace={"mode": "shared", "name": name})


DECISION = {
    "task": "tasks/abstimmen.md",
    "submission": {"format": "decision", "choices": ["ja", "nein"]},
}
RATING_GATE = {
    "type": "judge",
    "raters": ["judge-*"],
    "action": {"task": "tasks/bewerten.md", "submission": {"format": "rating"}},
    "rule": "mean",
    "routes": {"pass": {"goto": "pruefung"}, "fail": "retry"},
}
OWN = {"fields": {"ansatz": {"type": "choice", "choices": ["a", "b"]}}}
GOTO_REVISION = _set(6, gate={"type": "done", "routes": {"done": {"goto": "revision"}}})

# (rule, change that breaks it, path of the error, part of the message, close change that passes)
RULES: list[tuple[str, Mutation, str, str, Mutation | None]] = [
    ("S1-keine-phasen", lambda d: d.update(phases=[]), "phases", "mindestens eine Phase", None),
    (
        "S1-phasen-alte-topologie",
        lambda d: d.update(topology="solo"),
        "phases",
        "nur bei „topology: phases“",
        lambda d: (d.update(topology="solo"), d.pop("phases")),
    ),
    (
        "S2-stages",
        lambda d: d.update(stages=[{"name": "x", "agent": "prover"}]),
        "stages",
        "je Stufe eine Phase",
        None,
    ),
    ("S2-raters", lambda d: d.update(raters=["judge-*"]), "raters", "gate.raters", None),
    (
        "S2-open_from",
        lambda d: d["channels"][1].update(open_from="urteil"),
        "channels[1].open_from",
        "channels",
        None,
    ),
    (
        "S2-moderiert",
        lambda d: d["channels"][1].update(mode="moderated", moderator="judge-1"),
        "channels[1].mode",
        "topology: moderated",
        lambda d: d["channels"][1].update(mode="event"),
    ),
    ("S3", _set(6, name="revision"), "phases[6].name", "mehrfach", None),
    ("S4-rounds", _set(0, rounds=2), "phases[0].rounds", "nur bei „chat“", _set(4, rounds=2)),
    (
        "S4-isolated",
        _set(6, isolated=True),
        "phases[6].isolated",
        "nur bei „parallel“",
        _set(6, isolated=False),
    ),
    ("S4-over", _set(6, over="ideen"), "phases[6].over", "nur bei „parallel“", None),
    (
        "S4-repeat",
        _set(4, repeat=2),
        "phases[4].repeat",
        "„solo“ und „parallel“",
        _set(6, repeat=2),
    ),
    (
        "S4-max_assignments",
        _set(6, max_assignments=3),
        "phases[6].max_assignments",
        "nur bei „moderated“",
        None,
    ),
    (
        "S4-moderated-pflicht",
        _set(2, topology="moderated"),
        "phases[2].max_assignments",
        "braucht „max_assignments“",
        _set(2, topology="moderated", max_assignments=3),
    ),
    ("S4-standby-isoliert", _set(3, standby=["writer"]), "phases[3].standby", "isoliert", None),
    (
        "S5-unbekannt",
        lambda d: d["phases"][6].update(actions={"writr": _ph(d, 6)["actions"]["writer"]}),
        "phases[6].actions.writr",
        "Meintest du „writer“?",
        None,
    ),
    (
        "S5-doppelt",
        lambda d: _ph(d, 3)["actions"].update({"judge-1": dict(_ph(d, 3)["actions"]["judge-*"])}),
        "phases[3].actions.judge-1",
        "schon eine Action",
        None,
    ),
    (
        "S5-action-und-standby",
        _set(2, standby=["strategist", "prover"]),
        "phases[2].standby[1]",
        "nicht zugleich auf Abruf",
        None,
    ),
    (
        "S5-solo-zwei",
        lambda d: _ph(d, 6)["actions"].update(ideator=dict(_ph(d, 6)["actions"]["writer"])),
        "phases[6].actions",
        "genau einen Agenten",
        None,
    ),
    (
        "S5-moderated-ohne-standby",
        _set(2, topology="moderated", max_assignments=3, standby=[], channels=[]),
        "phases[2].standby",
        "mindestens einen Agenten",
        _set(2, topology="moderated", max_assignments=3),
    ),
    (
        "S6-unbekannt",
        _set(2, channels=["rueckfrag"]),
        "phases[2].channels[0]",
        "Meintest du „rueckfrage“?",
        None,
    ),
    (
        "S6-chat-ohne-kanal",
        _set(4, channels=[]),
        "phases[4].channels",
        "braucht unter „channels“",
        None,
    ),
    (
        "S6-erster-kanal",
        _set(4, channels=["rueckfrage", "judges"]),
        "phases[4].channels[0]",
        "Entscheidung 39",
        _set(4, channels=["judges", "rueckfrage"]),
    ),
    (
        "S6-isoliert",
        _set(3, channels=["judges"]),
        "phases[3].channels[0]",
        "genau einem Mitglied",
        _both(
            lambda d: d["channels"].append({"name": "frage", "members": ["judge-1"]}),
            _set(3, channels=["frage"]),
        ),
    ),
    (
        "B1-unbekannt",
        _submission(6, "writer", format="txt"),
        "phases[6].actions.writer.submission.format",
        "Meintest du „text“?",
        None,
    ),
    (
        "B1-eingebauter-name",
        lambda d: d.update(formats={"text": OWN}),
        "formats.text",
        "ist eingebaut",
        lambda d: (d.update(formats={"eigen": OWN}), _submission(6, "writer", format="eigen")(d)),
    ),
    (
        "B1-interne-feldart",
        lambda d: (
            d.update(formats={"eigen": {"fields": {"s": {"type": "scores", "choices": ["a"]}}}}),
            _submission(6, "writer", format="eigen")(d),
        ),
        "formats.eigen",
        "nur die Feldarten",
        None,
    ),
    (
        "B1-choice-ohne-choices",
        lambda d: d.update(formats={"eigen": {"fields": {"c": {"type": "choice"}}}}),
        "formats.eigen.fields.c",
        "braucht „choices“",
        None,
    ),
    (
        "B2-bezeichner",
        _submission(2, "prover", name="Beweis!"),
        "phases[2].actions.prover.submission.name",
        "einfacher Bezeichner",
        None,
    ),
    (
        "B2-reserviert",
        _submission(6, "writer", name="review"),
        "phases[6].actions.writer.submission.name",
        "schon belegt",
        None,
    ),
    (
        "B2-format-je-name",
        _submission(6, "writer", name="beweis"),
        "phases[6].actions.writer.submission.name",
        "Fassungen eines Namens haben ein Format",
        _submission(6, "writer", name="zusammenfassung"),
    ),
    (
        "B2-seed",
        lambda d: d.update(seed={"ideen": {"file": "notizen/ideen.md"}}),
        "phases[0].actions.ideator.submission.name",
        "„seed“ mit „file“ ist ein „artifact“",
        lambda d: d.update(seed={"vorlage": {"file": "notizen/ideen.md"}}),
    ),
    (
        "B3-choices-ohne-decision",
        _submission(6, "writer", choices=["a"]),
        "phases[6].actions.writer.submission.choices",
        "genau bei „format: decision“",
        None,
    ),
    (
        "B3-decision-ohne-choices",
        _submission(6, "writer", format="decision"),
        "phases[6].actions.writer.submission.choices",
        "dort Pflicht",
        _submission(6, "writer", format="decision", choices=["ja", "nein"]),
    ),
    ("B4-unbekannt", _set(1, over="ideeen"), "phases[1].over", "Meintest du „ideen“?", None),
    ("B4-keine-liste", _set(1, over="beweis"), "phases[1].over", "braucht eine Liste", None),
    (
        "B4-eigene-phase",
        _both(_set(1, over="liste"), _submission(1, "explorer-*", format="list", name="liste")),
        "phases[1].over",
        "nur in dieser Phase selbst",
        None,
    ),
    (
        "B5-zwei-quellen",
        _set(2, inputs=[{"from": "exploration", "name": "ideen"}]),
        "phases[2].inputs[0]",
        "genau eine Quelle",
        None,
    ),
    (
        "B5-phase-unbekannt",
        _set(2, inputs=[{"from": "explorasion"}]),
        "phases[2].inputs[0].from",
        "Meintest du „exploration“?",
        None,
    ),
    (
        "B5-name-unbekannt",
        _set(2, inputs=[{"name": "ideeen"}]),
        "phases[2].inputs[0].name",
        "Meintest du „ideen“?",
        _set(2, inputs=[{"name": "ideen"}]),
    ),
    (
        "B5-kanal-unbekannt",
        _set(6, inputs=[{"channel": "judgs"}]),
        "phases[6].inputs[0].channel",
        "Meintest du „judges“?",
        _set(6, inputs=[{"channel": "judges"}]),
    ),
    (
        "B5-best",
        _set(6, inputs=[{"from": "beweis", "pick": "best"}]),
        "phases[6].inputs[0].pick",
        "Gate „judge“",
        _set(6, inputs=[{"from": "exploration", "pick": "best"}]),
    ),
    (
        "B6-absolut",
        lambda d: d.update(seed={"vorlage": {"file": "/etc/hosts"}}),
        "seed.vorlage.file",
        "relativer Pfad",
        lambda d: d.update(seed={"vorlage": {"file": "tex/beweis.tex"}}),
    ),
    (
        "B6-punkte",
        lambda d: d.update(seed={"vorlage": {"file": "../geheim.md"}}),
        "seed.vorlage.file",
        "ohne „..“",
        None,
    ),
    (
        "B6-keine-liste",
        lambda d: d.update(params={"ideas": {"default": "a"}}, seed={"liste": {"param": "ideas"}}),
        "seed.liste.param",
        "keine Liste",
        lambda d: d.update(
            params={"ideas": {"default": ["a"]}}, seed={"liste": {"param": "ideas"}}
        ),
    ),
    (
        "B6-param-unbekannt",
        lambda d: d.update(seed={"liste": {"param": "ideas"}}),
        "seed.liste.param",
        "unbekannten Parameter",
        None,
    ),
    (
        "B7-absolut",
        lambda d: _action(6, "writer")(d).update(task="/tmp/t.md"),
        "phases[6].actions.writer.task",
        "relativer Pfad",
        None,
    ),
    (
        "B7-gate",
        lambda d: _ph(d, 1)["gate"]["action"].update(task="../t.md"),
        "phases[1].gate.action.task",
        "ohne „..“",
        None,
    ),
    (
        "B8",
        _submission(6, "writer", format="decision"),
        "phases[6].actions.writer.submission",
        "keine gültige Erwartung",
        None,
    ),
    (
        "G1-phasenname",
        _gate(4, routes={**load()["phases"][4]["gate"]["routes"], "reject": "zusammenfassung"}),
        "phases[4].gate.routes.reject",
        "{ goto: zusammenfassung }",
        None,
    ),
    (
        "G1-wort",
        lambda d: _ph(d, 4)["gate"]["routes"].update(reject="nxt"),
        "phases[4].gate.routes.reject",
        "Meintest du „next“?",
        None,
    ),
    (
        "G1-ziel",
        lambda d: _ph(d, 4)["gate"]["routes"].update(reject={"goto": "zusamenfassung"}),
        "phases[4].gate.routes.reject",
        "Meintest du „zusammenfassung“?",
        None,
    ),
    (
        "G2-done",
        _gate(5, routes={"fertig": "next"}),
        "phases[5].gate.routes.fertig",
        "kein Ergebnis",
        None,
    ),
    (
        "G2-user",
        _set(0, gate={"type": "user", "routes": {"done": "next"}}),
        "phases[0].gate.routes",
        "keine Routen",
        None,
    ),
    ("G3-ohne-field", _gate_drop(2, "field"), "phases[2].gate.field", "braucht „field“", None),
    (
        "G3-feld-ohne-werte",
        _gate(2, field="summary"),
        "phases[2].actions.prover.submission.format",
        "festen Werten",
        None,
    ),
    (
        "G3-route-fehlt",
        lambda d: _ph(d, 4)["gate"]["routes"].pop("reject"),
        "phases[4].gate.routes",
        "fehlt eine Route für reject",
        None,
    ),
    (
        "G3-fremde-route",
        lambda d: _ph(d, 4)["gate"]["routes"].update(vielleicht="stop"),
        "phases[4].gate.routes.vielleicht",
        "kein Ergebnis",
        None,
    ),
    ("G3-rule-pflicht", _gate_drop(4, "rule"), "phases[4].gate.rule", "braucht „rule“", None),
    (
        "G3-gleiche-werte",
        _both(
            _set(
                4,
                actions={
                    "judge-1": DECISION,
                    "judge-2": {
                        **DECISION,
                        "submission": {"format": "decision", "choices": ["ja", "eher"]},
                    },
                },
            ),
            _set(
                4,
                gate={
                    "type": "signal",
                    "field": "choice",
                    "rule": "majority",
                    "on_tie": "stop",
                    "routes": {"ja": "stop", "nein": {"goto": "revision"}},
                },
            ),
        ),
        "phases[4].actions.judge-2.submission",
        "verschiedene Werte",
        None,
    ),
    (
        "G4-all-verdict",
        _gate(4, rule="all"),
        "phases[4].gate.rule",
        "nur für das Feld „status“",
        None,
    ),
    (
        "G4-on_tie",
        _both(
            _set(4, actions={"judge-*": DECISION}),
            _set(
                4,
                gate={
                    "type": "signal",
                    "field": "choice",
                    "rule": "majority",
                    "routes": {"ja": "stop", "nein": {"goto": "revision"}},
                },
            ),
        ),
        "phases[4].gate.on_tie",
        "Gleichstand",
        _both(
            _set(4, actions={"judge-*": DECISION}),
            _set(
                4,
                gate={
                    "type": "signal",
                    "field": "choice",
                    "rule": "majority",
                    "on_tie": "stop",
                    "routes": {"ja": "stop", "nein": {"goto": "revision"}},
                },
            ),
        ),
    ),
    (
        "G4-unanimous-status",
        _gate(2, rule="unanimous"),
        "phases[2].gate.on_tie",
        "Gleichstand",
        _gate(2, rule="any"),
    ),
    (
        "G4-mean-am-signal",
        _gate(4, rule="mean"),
        "phases[4].gate.rule",
        "gehört an ein Gate „judge“",
        None,
    ),
    ("G5-keine-rater", _gate(1, raters=[]), "phases[1].gate.raters", "keinen aktiven Rater", None),
    (
        "G5-rater-mit-action",
        _gate(1, raters=["explorer-1"]),
        "phases[1].gate.raters[0]",
        "selbst bewerten",
        None,
    ),
    (
        "G5-mean-mehrere",
        _gate(1, rule="mean", pass_score=7, routes={"pass": "next", "fail": "next"}),
        "phases[1].gate.rule",
        "genau eine Abgabe",
        None,
    ),
    (
        "G5-mean-ohne-schwelle",
        _set(5, gate=RATING_GATE),
        "phases[5].gate.pass_score",
        "braucht eine Schwelle",
        _set(5, gate={**RATING_GATE, "pass_score": 7}),
    ),
    (
        "G5-schwelle-skala",
        _set(5, gate={**RATING_GATE, "pass_score": 11}),
        "phases[5].gate.pass_score",
        "außerhalb der Skala",
        None,
    ),
    (
        "G5-routen",
        _gate(1, routes={"pass": "next"}),
        "phases[1].gate.routes",
        "fehlt eine Route für ranked",
        None,
    ),
    (
        "G5-criteria",
        _set(
            5,
            gate={
                **RATING_GATE,
                "rule": "majority",
                "criteria": ["x"],
                "action": {"task": "t.md", "submission": {"format": "review"}},
                "routes": {v: "next" for v in ("accept", "minor", "major", "reject")},
            },
        ),
        "phases[5].gate.criteria",
        "nur, wenn die Rater ein „rating“",
        None,
    ),
    (
        "G5-format",
        lambda d: _ph(d, 1)["gate"]["action"]["submission"].update(format="review"),
        "phases[1].gate.action.submission.format",
        "geben die Rater „rating“ ab",
        None,
    ),
    ("G5-ohne-action", _gate_drop(1, "action"), "phases[1].gate.action", "Bewertungs-Action", None),
    (
        "L1",
        lambda d: _ph(d, 2)["gate"]["routes"].update(not_solved="retry"),
        "phases[2].gate.routes.not_solved",
        "braucht dort „max_visits“",
        _both(
            lambda d: _ph(d, 2)["gate"]["routes"].update(not_solved="retry"),
            _set(2, max_visits=2, on_exhausted="stop"),
        ),
    ),
    ("L2", _drop(5, "max_visits", "on_exhausted"), "phases[3].max_visits", "keine Grenze", None),
    (
        "L2-ueber-aufgebrauchte",
        _both(GOTO_REVISION, _set(5, on_exhausted={"goto": "zusammenfassung"})),
        "phases[6].max_visits",
        "keine Grenze",
        _both(GOTO_REVISION, _set(5, on_exhausted="stop")),
    ),
    (
        "L3-pflicht",
        _drop(5, "on_exhausted"),
        "phases[5].on_exhausted",
        "braucht „on_exhausted“",
        None,
    ),
    (
        "L3-retry",
        _set(5, on_exhausted="retry"),
        "phases[5].on_exhausted",
        "kann nicht „retry“ sein",
        None,
    ),
    (
        "L3-kreis",
        _set(6, max_visits=1, on_exhausted={"goto": "revision"}),
        "phases[5].on_exhausted",
        "im Kreis",
        _set(6, max_visits=1, on_exhausted="stop"),
    ),
    (
        "L4",
        _both(
            lambda d: _ph(d, 4)["gate"]["routes"].update(accept="stop"),
            _set(5, on_exhausted="stop"),
        ),
        "phases[6].name",
        "über keine Route erreichbar",
        lambda d: _ph(d, 4)["gate"]["routes"].update(accept="stop"),
    ),
    ("I1-ordner", _shared(4), "phases[3].isolated", "teilen den Arbeitsordner", None),
    (
        "I1-read_dirs",
        lambda d: d["agents"][4].update(read_dirs=["run:shared/tisch"]),
        "phases[3].isolated",
        "liest den Run-Ordner",
        lambda d: d["agents"][4].update(read_dirs=["run:output"]),
    ),
    (
        "I2",
        _both(_shared(3), _set(5, folder="fresh")),
        "phases[5].folder",
        "gemeinsamen Ordner",
        _both(_shared(3), _set(5, folder="keep")),
    ),
    (
        "I3",
        _set(3, context={"judge-*": "reset", "writer": "keep"}),
        "phases[3].context",
        "kein Teilnehmer",
        _set(3, context={"judge-*": "reset"}),
    ),
]


def test_a6_the_example_is_the_counter_check_of_every_rule() -> None:
    found = findings(load())
    assert found.errors == [] and found.warnings == []


@pytest.mark.parametrize(
    ("change", "path", "part", "good"), [r[1:] for r in RULES], ids=[r[0] for r in RULES]
)
def test_a6_every_rule_reports_its_error_with_a_path(
    change: Mutation, path: str, part: str, good: Mutation | None
) -> None:
    data = load()
    change(data)
    issue = findings(data).error(path)
    assert part in issue.message, issue.message
    if good is not None:
        data = load()
        good(data)
        found = findings(data)
        assert found.errors == [], [str(i) for i in found.errors]


def test_a6_every_rule_has_a_test() -> None:
    rules = {name.split("-")[0] for name, *_ in RULES}
    wanted = {f"S{i}" for i in range(1, 7)} | {f"B{i}" for i in range(1, 9)}
    wanted |= {f"G{i}" for i in range(1, 6)} | {f"L{i}" for i in range(1, 5)}
    wanted |= {"I1", "I2", "I3"}
    assert wanted <= rules, wanted - rules


def test_a6_several_errors_come_together() -> None:
    data = load()
    _set(0, rounds=2)(data)
    _set(4, repeat=2)(data)
    _gate(1, raters=["explorer-1"])(data)
    _drop(5, "on_exhausted")(data)
    _set(3, context={"judge-*": "reset", "writer": "keep"})(data)
    paths = findings(data).paths
    for path in (
        "phases[0].rounds",
        "phases[4].repeat",
        "phases[1].gate.raters[0]",
        "phases[5].on_exhausted",
        "phases[3].context",
    ):
        assert path in paths


def test_a6_messages_repeat_no_long_text_of_the_file() -> None:
    long = "x" * 150
    data = load()
    _ph(data, 4)["gate"]["routes"]["reject"] = long
    message = findings(data).error("phases[4].gate.routes.reject").message
    assert long not in message and "…" in message


# --------------------------------------------------------------------------------------------
# A7: rules at a gate `signal` (W4b-02a §6.5)
# --------------------------------------------------------------------------------------------


def _signal_group(field: str, rule: str | None, agents: int, on_tie: bool) -> dict[str, Any]:
    submission: dict[str, Any] = {
        "status": {"format": "text"},
        "verdict": {"format": "review"},
        "choice": {"format": "decision", "choices": ["a", "b"]},
        "ansatz": {"format": "eigen"},
    }[field]
    values = {
        "status": ["done", "not_solved"],
        "verdict": ["accept", "minor", "major", "reject"],
        "choice": ["a", "b"],
        "ansatz": ["a", "b"],
    }[field]
    gate: dict[str, Any] = {
        "type": "signal",
        "field": field,
        "routes": dict.fromkeys(values, "next"),
    }
    if rule is not None:
        gate["rule"] = rule
    if on_tie:
        gate["on_tie"] = "stop"
    return {
        "topology": "phases",
        "formats": {"eigen": OWN},
        "agents": [{"id": "a", "count": agents}, {"id": "w"}],
        "phases": [
            {
                "name": "p",
                "topology": "parallel",
                "actions": {"a-*": {"task": "t.md", "submission": submission}},
                "gate": gate,
            },
            {
                "name": "ende",
                "topology": "solo",
                "actions": {"w": {"task": "t.md", "submission": {"format": "text"}}},
            },
        ],
    }


SIGNAL = [
    # field, rule, agents with an action, on_tie given, error path (None: passes)
    ("status", "all", 2, False, None),
    ("status", "any", 2, False, None),
    ("status", "majority", 2, False, "phases[0].gate.on_tie"),
    ("status", "majority", 2, True, None),
    ("status", "majority", 1, False, None),
    ("status", "unanimous", 1, False, "phases[0].gate.on_tie"),
    ("status", "unanimous", 2, True, None),
    ("status", None, 2, False, "phases[0].gate.rule"),
    ("status", None, 1, False, None),
    ("verdict", "majority", 3, False, None),
    ("verdict", "unanimous", 3, False, None),
    ("verdict", "all", 2, False, "phases[0].gate.rule"),
    ("verdict", "any", 2, False, "phases[0].gate.rule"),
    ("choice", "majority", 2, False, "phases[0].gate.on_tie"),
    ("choice", "majority", 2, True, None),
    ("choice", "unanimous", 1, False, "phases[0].gate.on_tie"),
    ("choice", "unanimous", 1, True, None),
    ("choice", "any", 2, True, "phases[0].gate.rule"),
    ("ansatz", "majority", 3, True, None),
    ("ansatz", "majority", 3, False, "phases[0].gate.on_tie"),
    ("ansatz", "rank", 2, True, "phases[0].gate.rule"),
]


@pytest.mark.parametrize(("field", "rule", "agents", "on_tie", "path"), SIGNAL)
def test_a7_signal_rules(
    field: str, rule: str | None, agents: int, on_tie: bool, path: str | None
) -> None:
    found = findings(_signal_group(field, rule, agents, on_tie))
    if path is None:
        assert found.errors == [], [str(i) for i in found.errors]
    else:
        assert path in found.paths, [str(i) for i in found.errors]


def test_a7_on_tie_where_no_tie_is_possible_is_a_hint() -> None:
    found = findings(_signal_group("verdict", "majority", 3, True))
    assert found.errors == []
    assert [w.path for w in found.warnings] == ["phases[0].gate.on_tie"]


# --------------------------------------------------------------------------------------------
# A8: hint of I2; A11: the skills page
# --------------------------------------------------------------------------------------------


def test_a8_reset_in_a_shared_folder_is_a_hint_not_an_error() -> None:
    data = load()
    _shared(3)(data)
    _set(5, context="reset")(data)
    found = findings(data)
    assert found.errors == []
    [hint] = found.warnings
    assert hint.path == "phases[5].context" and "überleben" in hint.message
    assert found.resolved is not None
    assert found.resolved.phase("revision").folder == {"prover": "keep"}, "a shared folder stays"


def _skill(root: Path, group: str) -> Path:
    folder = root / "skills" / "baukasten"
    (folder / "roles").mkdir(parents=True)
    (folder / "tasks").mkdir()
    (folder / "group.yaml").write_text(group, encoding="utf-8")
    for role in ("ideator", "explorer", "strategist", "prover", "judge", "writer"):
        (folder / "roles" / f"{role}.md").write_text("Rolle.", encoding="utf-8")
    for task in (
        "ideen",
        "ausarbeiten",
        "bewerten",
        "beweis",
        "pruefen",
        "besprechen",
        "ueberarbeiten",
        "zusammenfassen",
    ):
        (folder / "tasks" / f"{task}.md").write_text("Auftrag.", encoding="utf-8")
    return folder


def test_a11_skills_page_shows_check_task_files_and_hints(tmp_path: Path) -> None:
    folder = _skill(tmp_path, text("beispiel"))
    catalog = discover_skills(tmp_path / "skills")
    settings = WorkfolderSettings()
    spec, issues = check_group(folder, catalog, settings)
    assert spec is not None and issues == [], issues
    (folder / "tasks" / "beweis.md").unlink()
    group = text("beispiel").replace("    topology: chat\n", "    topology: chat\n    repeat: 2\n")
    group = group.replace(
        "{ id: prover,     role: roles/prover.md,",
        "{ id: prover, workspace: { mode: shared, name: w }, role: roles/prover.md,",
    )
    group = group.replace("    max_visits: 2\n", "    max_visits: 2\n    context: reset\n")
    (folder / "group.yaml").write_text(group, encoding="utf-8")
    _spec, issues = check_group(folder, catalog, settings)
    by_field = {i.field: i for i in issues}
    lines = group.splitlines()
    task = by_field["phases[2].actions.prover.task"]
    assert "Auftrag tasks/beweis.md nicht gefunden" in task.message
    assert task.line is not None and "prover:" in lines[task.line - 1]
    repeat = by_field["phases[4].repeat"]
    assert repeat.severity is Severity.ERROR and lines[repeat.line - 1].strip() == "repeat: 2"
    hint = by_field["phases[5].context"]
    assert hint.severity is Severity.WARNING and lines[hint.line - 1].strip() == "context: reset"


def test_a11_a_task_outside_the_skill_or_a_link(tmp_path: Path) -> None:
    group = text("beispiel").replace("tasks/zusammenfassen.md", "tasks/link.md")
    folder = _skill(tmp_path, group)
    (folder / "tasks" / "link.md").symlink_to(tmp_path / "draussen.md")
    (tmp_path / "draussen.md").write_text("x", encoding="utf-8")
    _spec, issues = check_group(folder, discover_skills(tmp_path / "skills"), WorkfolderSettings())
    [issue] = issues
    assert issue.field == "phases[6].actions.writer.task" and "Link aus dem Skill" in issue.message


PHASES_RUN = """\
version: 1
project: demo
task: Teste arr.
topology: {kind: phases}
defaults: {provider: claude, model: fake-model, auth: fake-auth}
agents: [{id: a}]
phases:
  - name: arbeit
    topology: solo
    actions: { a: { task: tasks/arbeit.md, submission: { format: text } } }
"""


def test_a11_plan_launch_still_refuses_phases(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n", "utf-8")
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    run_file = workfolder / "run.yaml"
    run_file.write_text(PHASES_RUN, encoding="utf-8")
    with pytest.raises(ArrError, match="„phases“ ist noch nicht verfügbar"):
        plan_launch(run_file, cwd=workfolder)
    assert list((workfolder / "runs").iterdir()) == []


# --------------------------------------------------------------------------------------------
# A16: limits, size and hostile input
# --------------------------------------------------------------------------------------------

LIMITS: list[tuple[str, Mutation, str]] = [
    ("phasen", lambda d: d.update(phases=d["phases"] * (MAX_PHASES // 7 + 1)), "phases"),
    (
        "turns",
        lambda d: _action(6, "writer")(d).update(turns=MAX_TURNS + 1),
        "phases[6].actions.writer.turns",
    ),
    ("rounds", _set(4, rounds=MAX_ROUNDS + 1), "phases[4].rounds"),
    ("repeat", _set(1, repeat=MAX_REPEAT + 1), "phases[1].repeat"),
    ("max_visits", _set(5, max_visits=MAX_VISITS + 1), "phases[5].max_visits"),
    ("max_assignments", _set(2, max_assignments=MAX_ASSIGNMENTS + 1), "phases[2].max_assignments"),
    ("inputs", _set(2, inputs=[{"from": "exploration"}] * (MAX_INPUTS + 1)), "phases[2].inputs"),
    (
        "routes",
        _gate(4, routes={f"w{i}": "next" for i in range(MAX_ROUTES + 1)}),
        "phases[4].gate.routes",
    ),
    (
        "formats",
        lambda d: d.update(formats={f"f{i}": OWN for i in range(MAX_FORMATS + 1)}),
        "formats",
    ),
    ("actions", _set(6, actions={f"a{i}": DECISION for i in range(51)}), "phases[6].actions"),
    (
        "pass_score",
        _set(5, gate={**RATING_GATE, "pass_score": float("nan")}),
        "phases[5].gate.pass_score",
    ),
]


@pytest.mark.parametrize(("change", "path"), [r[1:] for r in LIMITS], ids=[r[0] for r in LIMITS])
def test_a16_limits_take_effect(change: Mutation, path: str) -> None:
    data = load()
    change(data)
    assert path in findings(data).paths


def _large(phases: int, agents: int) -> dict[str, Any]:
    """A group at the limits: `phases` phases, each with `agents` agents, a judge gate and
    loops back to every earlier phase."""
    group: dict[str, Any] = {
        "topology": "phases",
        "rubric": {"pass_score": 5},
        "agents": [{"id": "a", "count": agents}, {"id": "r", "count": 2}, {"id": "w"}],
        "phases": [],
    }
    for i in range(phases):
        group["phases"].append(
            {
                "name": f"p{i}",
                "topology": "parallel",
                "max_visits": MAX_VISITS,
                "on_exhausted": "next",
                "inputs": [{"from": f"p{max(i - 1, 0)}"}] * MAX_INPUTS,
                "actions": {
                    "a-*": {"task": "t.md", "submission": {"format": "text"}, "turns": MAX_TURNS}
                },
                "repeat": MAX_REPEAT,
                "gate": {
                    "type": "judge",
                    "raters": ["r-*"],
                    "action": {"task": "r.md", "submission": {"format": "rating"}},
                    "rule": "rank",
                    "routes": {"ranked": {"goto": f"p{(i * 7) % phases}"}},
                },
            }
        )
    group["phases"][-1]["gate"]["routes"]["ranked"] = "next"
    return group


def _seconds(group: dict[str, Any]) -> float:
    resolved = resolve(group)
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        result = check(resolved)
        turn_range(resolved)
        best = min(best, time.perf_counter() - start)
    assert result.errors == [], [str(i) for i in result.errors][:3]
    return best


def test_a16_check_and_estimate_stay_fast_at_the_limits() -> None:
    small = _seconds(_large(3, 2))
    large = _seconds(_large(MAX_PHASES, 50))
    # 10× the phases and 25× the agents: linear growth would be ~250×; allow 20× that,
    # far below the growth of an exponential walk over the loops.
    assert large < small * 5000, (small, large)


HEAD = "topology: phases\nagents: [{id: a}]\n"
SOLO = "topology: solo, actions: {a: {task: t, submission: {format: text}}}"
HOSTILE = [
    HEAD + "phases: 7\n",
    HEAD + "phases: [1, null, {}]\n",
    HEAD + "phases: [{name: p, topology: solo, actions: {a: 5}}]\n",
    HEAD
    + "phases: [{name: p, topology: solo, actions: {a: {task: t, submission: {format: [x]}}},"
    + " gate: {type: signal, routes: {x: {goto: {goto: p}}}}}]\n",
    HEAD
    + "formats: {f: {fields: {x: {type: list, item: {type: list, item: {type: text}}}}}}\n"
    + "phases: [{name: p, topology: solo, actions: {a: {task: t, submission: {format: f}}}}]\n",
    HEAD
    + "seed: {s: {param: '{{ params.x }}'}}\n"
    + 'phases: [{name: p, topology: solo, actions: {a: {task: "\\0", submission: '
    + "{format: text}}}}]\n",
    HEAD + "phases: [{name: p, max_visits: 1e400, " + SOLO + "}]\n",
    HEAD
    + "params: {t: {default: [1]}}\n"
    + "phases: [{name: p, topology: '{{ params.t }}', actions: {'{{ params.a }}': "
    + "{task: t, submission: {format: text}}}}]\n",
    "topology: phases\nagents: [{id: a, count: 50}]\n"
    + "phases: [{name: p, topology: parallel, actions: {'a-*': {task: "
    + "x" * 5000
    + ", submission: {format: text}}}, gate: {type: signal, field: status, rule: majority, "
    + "on_tie: retry, routes: {done: retry, not_solved: retry}}}]\n",
]


@pytest.mark.parametrize("group", HOSTILE)
def test_a16_hostile_groups_are_issues_never_exceptions(tmp_path: Path, group: str) -> None:
    folder = tmp_path / "skills" / "feind"
    folder.mkdir(parents=True)
    (folder / "group.yaml").write_text(group, encoding="utf-8")
    catalog = discover_skills(tmp_path / "skills")
    _spec, issues = _check_group(folder, catalog, WorkfolderSettings())  # raises nothing
    assert issues, "every hostile group has a finding"
    assert not any("Die Prüfung ist fehlgeschlagen" in i.message for i in issues)
    assert all(len(i.message) < 1200 for i in issues)

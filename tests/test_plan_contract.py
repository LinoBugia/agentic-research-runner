"""W4b-02a.1: rework after the review of W4b-02a (docs/10 §4.7, §4.10; Entscheidungen 42–47).

Point by point: P1 path and line of `channels` (M1, G12), P2 short listings (M2), P3 gate
`signal` counts submissions (M3), P4 the contract for the next orders (rating only at a gate,
inputs from channels, standard inputs, one rule for exhausted phases), P5 decisions 43, 44, 46,
P6 a narrower JSON schema (G11), P7 the small findings (G1, G3, G5, G6, G8, G9) and P8 a test
for every mutation that survived the review (M9 … M62). No model, no run.
"""

from pathlib import Path
from typing import Any

import pytest
from plan_kit import findings, load, phase, resolve

from arr.errors import ConfigError
from arr.models import (
    DEFAULT_TURNS,
    GroupSpec,
    InputContent,
    Pick,
    RouteKeyword,
    RunInput,
    WorkfolderSettings,
)
from arr.plan import graph as graphs
from arr.plan.estimate import ASSIGNMENT_TURNS, phase_turns
from arr.plan.read import ACTION_GATE, ACTION_WORK, Entry, enter, expectation, follow
from arr.plan.schema import json_schema
from arr.resolve import load_run_input, validate_model
from arr.service.real_api.catalog import _check_group
from arr.skills import discover_skills


def act(fmt: str = "text", **extra: Any) -> dict[str, Any]:
    """An action with the task `t.md`; `name`, `choices` go to the submission."""
    submission: dict[str, Any] = {"format": fmt}
    submission |= {k: v for k, v in extra.items() if k in ("name", "choices")}
    out: dict[str, Any] = {"task": "t.md", "submission": submission}
    if "turns" in extra:
        out["turns"] = extra["turns"]
    return out


AGENTS = [{"id": x} for x in ("a", "b", "c", "s", "r1", "r2")]
CHANNELS = [
    {"name": "rund", "members": ["a", "b", "s"], "mode": "rounds", "max_wakeups_per_agent": 2},
    {"name": "los", "members": ["a", "b", "s"], "mode": "event", "max_wakeups_per_agent": 2},
]


def group(*phases: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A `phases` group with agents a, b, c, s, r1, r2 and the channels `rund` and `los`."""
    data: dict[str, Any] = {
        "topology": "phases",
        "agents": AGENTS,
        "channels": CHANNELS,
        "phases": list(phases),
    }
    return data | extra


def solo(name: str, agent: str = "a", **extra: Any) -> dict[str, Any]:
    return {"name": name, "topology": "solo", "actions": {agent: act()}} | extra


def lister(name: str = "quelle", list_name: str = "li") -> dict[str, Any]:
    """A phase whose agent `c` hands in a list `list_name`."""
    return {"name": name, "topology": "solo", "actions": {"c": act("list", name=list_name)}}


def issues_of(tmp_path: Path, text: str) -> list[Any]:
    """The issues the skills page shows for a `group.yaml`."""
    folder = tmp_path / "skills" / "x"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "group.yaml").write_text(text, encoding="utf-8")
    _spec, issues = _check_group(folder, discover_skills(tmp_path / "skills"), WorkfolderSettings())
    return issues


# --------------------------------------------------------------------------------------------
# P1 (M1, G12): `channels` keeps its spelling, path and line, also for errors when loading
# --------------------------------------------------------------------------------------------

HEAD = "kind: research-group\nversion: 1\ntopology: phases\nagents: [{id: a}, {id: b}]\n"
PHASE = (
    "phases:\n  - name: p\n    topology: solo\n"
    "    actions: {a: {task: t.md, submission: {format: text}}}\n"
)
LOAD_ERRORS = {
    # case: (section, path after the key, text of the line it names)
    "unbekanntes Feld": ("{key}:\n  - name: c\n    membres: [a, b]\n", "[0].membres", "membres"),
    "falscher Typ": (
        "{key}:\n  - name: c\n    members: [a, b]\n    anonymous: vielleicht\n",
        "[0].anonymous",
        "anonymous",
    ),
    "Name ungültig": ("{key}:\n  - name: C D\n    members: [a, b]\n", "[0].name", "C D"),
    "members fehlt": ("{key}:\n  - name: c\n", "[0].members", "name: c"),
    "Liste ist Zahl": ("{key}: 5\n", "", ": 5"),
    "zweiter Eintrag": (
        "{key}:\n  - name: c\n    members: [a, b]\n  - name: d\n    mode: laut\n    members: [a]\n",
        "[1].mode",
        "laut",
    ),
}


@pytest.mark.parametrize("key", ["channels", "groupchats"])
@pytest.mark.parametrize("case", list(LOAD_ERRORS))
def test_p1_load_errors_name_the_key_of_the_file_and_its_line(
    tmp_path: Path, key: str, case: str
) -> None:
    section, rest, marker = LOAD_ERRORS[case]
    text = HEAD + section.format(key=key) + PHASE
    [issue] = issues_of(tmp_path, text)
    assert issue.field == key + rest
    assert issue.line is not None and marker in text.splitlines()[issue.line - 1]


@pytest.mark.parametrize("key", ["channels", "groupchats"])
def test_p1_run_yaml_names_the_key_and_line(tmp_path: Path, key: str) -> None:
    run = tmp_path / "run.yaml"
    run.write_text(
        f"version: 1\nproject: p\ntask: t\nagents: [{{id: a}}]\n{key}:\n  - name: c\n"
        "    membres: [a]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as info:
        load_run_input(run)
    assert [(i.path, i.line) for i in info.value.issues] == [(f"{key}[0].membres", 7)]


def test_p1_a_typo_suggests_channels() -> None:
    for model, data in (
        (GroupSpec, {"topology": "phases", "agents": [{"id": "a"}], "chanels": []}),
        (RunInput, {"project": "p", "task": "t", "chanels": []}),
    ):
        with pytest.raises(ConfigError) as info:
            validate_model(model, data)
        message = info.value.issue_message
        assert "Meintest du „channels“?" in message
        assert "channels" in message.split("Erlaubt sind:")[1]
        assert "groupchats" not in message


# --------------------------------------------------------------------------------------------
# P2 (M2): listings in messages stay short
# --------------------------------------------------------------------------------------------


def _many_names(names: int, errors: int) -> str:
    lines = [
        "kind: research-group",
        "version: 1",
        "topology: phases",
        "agents: [{id: a, count: 50}]",
        "phases:",
    ]
    for p in range(names // 50):
        lines.append(f"  - name: p{p}\n    topology: parallel\n    actions:")
        for k in range(50):
            name = f"n{p * 50 + k:04d}" + "x" * 190
            lines.append(
                f"      a-{k + 1}: {{task: t.md, submission: {{format: text, name: {name}}}}}"
            )
    lines.append("  - name: q\n    topology: solo")
    lines.append("    actions: {a-1: {task: t.md, submission: {format: text}}}")
    lines.append("    inputs:")
    lines += [f"      - {{name: unbekannt{e}}}" for e in range(errors)]
    return "\n".join(lines) + "\n"


def test_p2_many_long_names_give_short_messages(tmp_path: Path) -> None:
    text = _many_names(800, 20)
    issues = issues_of(tmp_path, text)
    assert len(issues) == 20
    total = sum(len(i.message) for i in issues)
    assert total < len(text) / 10, total  # was 41 MB from 209 KB before W4b-02a.1
    assert all(len(i.message) < 800 for i in issues)
    assert "… und 790 weitere" in issues[0].message
    assert "x" * 60 not in issues[0].message


def test_p2_listings_in_check_are_short() -> None:
    choices = [f"wahl{i:02d}" + "y" * 90 for i in range(50)]
    data = group(
        {
            "name": "p",
            "topology": "solo",
            "actions": {"a": act("decision", choices=choices)},
            "gate": {"type": "signal", "field": "choice", "routes": {"zzz": "next"}},
        }
    )
    found = findings(data)
    message = found.error("phases[0].gate.routes").message
    assert "… und 40 weitere" in message and len(message) < 1200
    assert "y" * 60 not in message


# --------------------------------------------------------------------------------------------
# P3 (M3): a gate `signal` counts submissions per pass, `over` counts as several
# --------------------------------------------------------------------------------------------


def _over_signal(**gate: Any) -> dict[str, Any]:
    return group(
        lister(),
        {
            "name": "w",
            "topology": "parallel",
            "over": "li",
            "actions": {"b": act()},
            "gate": {"type": "signal", "field": "status"}
            | {"routes": {"done": "next", "not_solved": "stop"}}
            | gate,
        },
    )


def test_p3_over_needs_a_rule_and_on_tie_like_several_agents() -> None:
    found = findings(_over_signal())
    assert "„over: li“" in found.error("phases[1].gate.rule").message
    assert "phases[1].gate.on_tie" in findings(_over_signal(rule="majority")).paths
    assert "phases[1].gate.on_tie" in findings(_over_signal(rule="unanimous")).paths
    # counter-checks: a rule without a tie, a majority with on_tie, one agent without over
    assert findings(_over_signal(rule="all")).errors == []
    assert findings(_over_signal(rule="majority", on_tie="stop")).errors == []
    plain = _over_signal()
    del plain["phases"][1]["over"]
    assert findings(plain).errors == []


# --------------------------------------------------------------------------------------------
# P4: contract for the next orders (Entscheidung 45)
# --------------------------------------------------------------------------------------------


def test_p4_rating_only_as_the_action_of_a_judge_gate() -> None:
    data = group(solo("p"))
    data["phases"][0]["actions"]["a"] = act("rating")
    issue = findings(data).error("phases[0].actions.a.submission.format")
    assert "Gates „judge“" in issue.message
    assert findings(load()).errors == []  # the example rates at its gate


def test_p4_inputs_from_a_channel_are_full_and_have_no_pick() -> None:
    data = group(solo("p", inputs=[{"channel": "rund"}, {"from": "p"}]))
    resolved = resolve(data)
    channel, source = resolved.phases[0].inputs or []
    assert (channel.content, channel.pick) == (InputContent.FULL, None)
    assert (source.content, source.pick) == (InputContent.SUMMARY, Pick.LATEST)  # M62
    bad = group(solo("p", inputs=[{"channel": "rund", "content": "summary"}]))
    assert "content: full" in findings(bad).error("phases[0].inputs[0].content").message
    for entry in ({"channel": "rund", "pick": "all"}, {"peers": "summary", "pick": "latest"}):
        found = findings(group(solo("p", repeat=2, inputs=[entry])))
        assert "nur mit „from“ oder „name“" in found.error("phases[0].inputs[0].pick").message


def test_p4_dropped_sources_and_the_standard_inputs() -> None:
    params = {"mit": {"default": False}}
    first = solo("weg", when={"param": "mit", "equals": True})
    some = group(
        first, solo("p", "b", inputs=[{"from": "weg"}, {"channel": "rund"}]), params=params
    )
    [left] = resolve(some).phase("p").inputs or []
    assert left.channel == "rund", "the input from the dropped phase is left out (M57)"
    every = group(first, solo("p", "b", inputs=[{"from": "weg"}]), params=params)
    assert resolve(every).phase("p").inputs is None, "standard inputs, not none (G7)"
    explicit = group(solo("p", inputs=[]))
    assert resolve(explicit).phases[0].inputs == []


def _exhaust_group() -> dict[str, Any]:
    """a → b (max 1, on_exhausted next) → c (max 0, on_exhausted {goto: e}) … d (escalate)."""
    return group(
        solo("a", gate={"type": "done", "routes": {"done": "next"}}),
        solo("b", max_visits=1, on_exhausted="next"),
        solo("c", max_visits=0, on_exhausted={"goto": "e"}),
        solo("d", max_visits=1, on_exhausted="escalate"),
        solo("e"),
    )


def test_p4_enter_follows_exhausted_phases_with_one_rule() -> None:
    run = resolve(_exhaust_group())
    assert enter(run, "b", {}) == Entry("b")
    # b used up: `next` of b is c, which is never entered (max_visits 0) and leads to e
    assert enter(run, "b", {"b": 1}) == Entry("e", exhausted=("b", "c"))
    assert enter(run, "c", {}) == Entry("e", exhausted=("c",))
    assert enter(run, "d", {"d": 1}) == Entry("d", ask_user=True, exhausted=("d",))
    assert enter(run, None, {}) == Entry(None)
    last = resolve(group(solo("a"), solo("b", max_visits=1, on_exhausted="next")))
    assert enter(last, "b", {"b": 1}) == Entry(None, exhausted=("b",))
    # the graph and the estimate use the same rule
    phases = list(run.phases)
    assert graphs.entry(phases, 1, [0, 1, 0, 0, 0]) == (4, [1, 2])
    assert graphs.entry(phases, 3, [0, 0, 0, 1, 0]) == (graphs.USER, [3])


def test_p4_a_phase_with_max_visits_0_is_never_entered() -> None:
    data = group(
        solo("a", gate={"type": "done", "routes": {"done": {"goto": "x"}}}),
        solo(
            "x",
            max_visits=0,
            on_exhausted="stop",
            gate={"type": "done", "routes": {"done": {"goto": "y"}}},
        ),
        solo("y"),
    )
    found = findings(data)
    assert found.paths == ["phases[2].name"], found.paths  # y is reachable only through x (M31)
    assert "phases[1].name" not in found.paths, "x counts as reached (Entscheidung 40)"


def test_p4_follow_and_expectations() -> None:
    run = resolve(load())
    assert follow(run, "revision", RouteKeyword.NEXT) == "zusammenfassung"  # M50
    assert follow(run, "zusammenfassung", RouteKeyword.NEXT) is None
    assert follow(run, "revision", RouteKeyword.RETRY) == "revision"
    assert follow(run, "urteil", RouteKeyword.STOP) is None
    with pytest.raises(ValueError):
        follow(run, "urteil", RouteKeyword.ESCALATE)
    assert expectation(run, "exploration", "explorer-1").action == ACTION_WORK
    rater = expectation(run, "exploration", "judge-1", rate=[1, 2])
    assert (rater.action, rater.format, rater.rate) == (ACTION_GATE, "rating", [1, 2])  # M49
    data = load()
    phase(data, "exploration")["gate"]["criteria"] = ["neuheit"]
    rated = expectation(resolve(data), "exploration", "judge-2", rate=[3])
    assert rated.criteria == ["neuheit"]  # M51
    with pytest.raises(KeyError):
        expectation(run, "beweis", "writer")


# --------------------------------------------------------------------------------------------
# P5: Entscheidungen 43, 44, 46
# --------------------------------------------------------------------------------------------


def test_p5_peers_without_repeat_is_a_hint() -> None:
    found = findings(group(solo("p", inputs=[{"peers": "summary"}])))
    assert found.errors == [] and [w.path for w in found.warnings] == ["phases[0].inputs[0].peers"]


def test_p5_standard_turns_is_3_and_written_out() -> None:
    resolved = resolve(load())
    assert DEFAULT_TURNS == 3
    assert {a.turns for p in resolved.phases for a in p.actions} == {3}
    assert phase_turns(resolved, resolved.phase("zusammenfassung"))[:2] == (1, 4)


@pytest.mark.parametrize(
    ("where", "value"),
    [
        ("rounds", True),
        ("max_visits", True),
        ("repeat", False),
        ("max_assignments", True),
        ("turns", True),
        ("pass_score", True),
        ("isolated", "yes"),
        ("isolated", 1),
    ],
)
def test_p5_booleans_are_no_numbers(where: str, value: Any) -> None:
    data = group(solo("p"))
    entry = data["phases"][0]
    path = f"phases[0].{where}"
    if where == "turns":
        entry["actions"]["a"]["turns"] = value
        path = "phases[0].actions.a.turns"
    elif where == "pass_score":
        entry["gate"] = {"type": "judge", "pass_score": value}
        path = "phases[0].gate.pass_score"
    else:
        entry[where] = value
    with pytest.raises(ConfigError) as info:
        validate_model(GroupSpec, data)
    assert path in [i.path for i in info.value.issues]


def test_p5_numbers_and_switches_as_text_stay_allowed() -> None:
    data = group({"name": "p", "topology": "chat", "channels": ["rund"], "rounds": "2"})
    data["phases"][0]["actions"] = {"a": act(), "b": act()}
    assert resolve(data).phases[0].rounds == 2
    switch = group(
        {"name": "p", "topology": "parallel", "isolated": "true", "actions": {"a": act()}}
    )
    assert resolve(switch).phases[0].isolated is True


def test_p5_formats_seed_and_phases_report_together() -> None:
    data = group(
        solo("p", actions={"aa": act()}),
        formats={"f": {"fields": {"x": {"type": "text", "max_chars": "{{ params.n }}"}}}},
        seed={"s": {"param": "gibtsnicht"}},
        params={"n": {"default": 0}},
    )
    with pytest.raises(ConfigError) as info:
        resolve(data)
    paths = [i.path for i in info.value.issues]
    assert "formats.f.fields.x.max_chars" in paths
    assert "seed.s.param" in paths
    assert "phases[0].actions.aa" in paths


# --------------------------------------------------------------------------------------------
# P6 (G11): the JSON schema refuses what the model refuses, where it can
# --------------------------------------------------------------------------------------------


def _example() -> dict[str, Any]:
    return load()


SCHEMA_REFUSES = [
    ("Route leer", lambda d: d["phases"][5]["gate"]["routes"].update(done="")),
    (
        "goto Großbuchstaben",
        lambda d: d["phases"][5]["gate"]["routes"].update(done={"goto": "Pruefung"}),
    ),
    ("compact 0%", lambda d: d["agents"][3]["compact"].update(at="0%")),
    ("compact 150%", lambda d: d["agents"][3]["compact"].update(at="150%")),
    ("compact 0 Token", lambda d: d["agents"][3]["compact"].update(at=0)),
    ("compact Text", lambda d: d["agents"][3]["compact"].update(at="viel")),
    ("seed file und param", lambda d: d.update(seed={"s": {"file": "a", "param": "b"}})),
    ("seed leer", lambda d: d.update(seed={"s": {}})),
    ("seed Schlüssel groß", lambda d: d.update(seed={"S": {"file": "a"}})),
    ("formats Name groß", lambda d: d.update(formats={"F": {"fields": {"x": {"type": "text"}}}})),
    ("when ohne Bedingung", lambda d: d["phases"][0].update(when={"param": "x"})),
    ("Phasenname groß", lambda d: d["phases"][0].update(name="Ideen")),
]


@pytest.mark.parametrize(
    "change", [c for _n, c in SCHEMA_REFUSES], ids=[n for n, _c in SCHEMA_REFUSES]
)
def test_p6_schema_refuses_what_the_model_refuses(change: Any) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    data = _example()
    change(data)
    assert not jsonschema.Draft202012Validator(json_schema()).is_valid(data)
    with pytest.raises(ConfigError):
        validate_model(GroupSpec, data)


def test_p6_schema_still_takes_the_test_data_and_a_run_needs_its_task() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    group_schema = jsonschema.Draft202012Validator(json_schema())
    for name in ("beispiel", "solo", "roundtable", "waterfall", "review", "exploration"):
        assert group_schema.is_valid(load(name)), name
    run_schema = jsonschema.Draft202012Validator(json_schema("run"))
    run = {"project": "p", **{k: v for k, v in load().items() if k not in ("kind", "topology")}}
    assert not run_schema.is_valid(run), "without task"
    assert run_schema.is_valid({**run, "task": "Aufgabe"})
    assert not run_schema.is_valid({**run, "task": "a", "task_file": "t.md"})
    assert "prüft arr beim Laden" in json_schema()["description"]


# --------------------------------------------------------------------------------------------
# P7: small findings
# --------------------------------------------------------------------------------------------


def test_p7_g1_standby_patterns_are_joined() -> None:
    data = group(
        {
            "name": "p",
            "topology": "moderated",
            "max_assignments": 2,
            "actions": {"a": act()},
            "standby": ["r*", "r1"],
        }
    )
    found = findings(data)
    assert found.errors == [] and found.resolved is not None
    assert found.resolved.phases[0].standby == ["r1", "r2"]


def test_p7_g3_pass_score_at_rank_is_a_hint() -> None:
    data = load()
    phase(data, "exploration")["gate"]["pass_score"] = 7
    found = findings(data)
    assert found.errors == []
    assert [w.path for w in found.warnings] == ["phases[1].gate.pass_score"]


def test_p7_g5_one_message_per_pair_of_patterns() -> None:
    data = {
        "topology": "phases",
        "agents": [{"id": "a", "count": 20}],
        "phases": [
            {
                "name": "p",
                "topology": "parallel",
                "actions": {"a-*": act(), "a-?": act(), "a-??": act(), "a-1*": act()},
            }
        ],
    }
    found = findings(data)
    assert len(found.errors) <= 6, len(found.errors)  # was one per agent
    assert all("schon eine Action" in i.message for i in found.errors)


def test_p7_g6_g8_task_files_of_every_phase_cleaned(tmp_path: Path) -> None:
    text = (
        "kind: research-group\nversion: 1\ntopology: phases\nagents: [{id: a}]\n"
        "params: {mit: {default: false}, t: {default: x.md}}\n"
        "phases:\n"
        "  - {name: p, topology: solo, actions: {a: {task: t.md, submission: {format: text}}}}\n"
        "  - name: q\n    topology: solo\n    when: {param: mit, equals: true}\n"
        "    actions: {a: {task: tasks/fehlt.md, submission: {format: text}}}\n"
        "  - name: r\n    topology: solo\n    when: {param: mit, equals: true}\n"
        "    actions: {a: {task: '{{ params.t }}', submission: {format: text}}}\n"
        "  - name: s\n    topology: solo\n    when: {param: mit, equals: true}\n"
        '    actions: {a: {task: "t\\x1b[31m\\nb.md", submission: {format: text}}}\n'
    )
    folder = tmp_path / "skills" / "x"
    folder.mkdir(parents=True)
    (folder / "t.md").write_text("Auftrag.", encoding="utf-8")
    by_field = {i.field: i for i in issues_of(tmp_path, text)}
    assert "Auftrag tasks/fehlt.md nicht gefunden" in by_field["phases[1].actions.a.task"].message
    assert "phases[2].actions.a.task" not in by_field, "a placeholder is checked at the start"
    dirty = by_field["phases[3].actions.a.task"].message
    assert "\x1b" not in dirty and "\n" not in dirty
    long_key = group(solo("p", actions={"a\n" + "k" * 100: act()}))
    [issue] = findings(long_key).errors
    assert "\n" not in issue.path and len(issue.path) < 100


def test_p7_g9_the_placeholder_is_named_at_routes_agents_and_seed() -> None:
    params = {"wohin": {"default": "weiter"}, "ziel": {"default": "nix"}}
    params |= {"wer": {"default": "zz"}, "datei": {"default": 5}}
    data = group(
        solo(
            "p",
            standby=["{{ params.wer }}"],
            gate={"type": "done", "routes": {"done": "{{ params.wohin }}"}},
        ),
        solo("q", gate={"type": "done", "routes": {"done": {"goto": "{{ params.ziel }}"}}}),
        params=params,
        seed={"s": {"file": "{{ params.datei }}"}},
    )
    with pytest.raises(ConfigError) as info:
        resolve(data)
    by_path = {i.path: i.message for i in info.value.issues}
    assert "„{{ params.wohin }}“" in by_path["phases[0].gate.routes.done"]
    assert "„{{ params.ziel }}“" in by_path["phases[1].gate.routes.done"]
    assert "„{{ params.wer }}“" in by_path["phases[0].standby[0]"]
    assert "„{{ params.datei }}“" in by_path["seed.s.file"]


# --------------------------------------------------------------------------------------------
# P8: the mutations that survived the review
# --------------------------------------------------------------------------------------------


def test_m9_over_a_seed_file_is_no_list() -> None:
    data = group({"name": "w", "topology": "parallel", "over": "s", "actions": {"a": act()}})
    data["seed"] = {"s": {"file": "x.md"}}
    assert "braucht eine Liste" in findings(data).error("phases[0].over").message
    data["seed"] = {"s": {"param": "li"}}
    data["params"] = {"li": {"default": ["x"]}}
    assert findings(data).errors == []


def _judged(rule: str) -> dict[str, Any]:
    gate: dict[str, Any] = {"type": "judge", "raters": ["r1"], "rule": rule}
    if rule == "majority":
        gate["action"] = act("review")
        gate["routes"] = dict.fromkeys(("accept", "minor", "major", "reject"), "next")
    else:
        gate["action"] = act("rating")
        gate["routes"] = {"ranked": "next"}
    return group(solo("p", gate=gate), solo("q", "b", inputs=[{"from": "p", "pick": "best"}]))


def test_m11_pick_best_only_from_mean_or_rank() -> None:
    assert "phases[1].inputs[0].pick" in findings(_judged("majority")).paths
    assert findings(_judged("rank")).errors == []


@pytest.mark.parametrize(("count", "ok"), [(50, True), (51, False)])
def test_m12_a_seed_list_holds_50_entries(count: int, ok: bool) -> None:
    data = group(
        solo("p"),
        params={"li": {"default": [f"i{k}" for k in range(count)]}},
        seed={"s": {"param": "li"}},
    )
    assert (findings(data).errors == []) is ok


def test_m19_mean_with_over_is_several_submissions() -> None:
    gate = {
        "type": "judge",
        "raters": ["r1"],
        "rule": "mean",
        "pass_score": 5,
        "action": act("rating"),
        "routes": {"pass": "next", "fail": "next"},
    }
    data = group(
        lister(),
        {"name": "w", "topology": "parallel", "over": "li", "actions": {"b": act()}, "gate": gate},
    )
    assert "genau eine Abgabe" in findings(data).error("phases[1].gate.rule").message
    del data["phases"][1]["over"]
    assert findings(data).errors == []


def test_m30_check_quotes_long_values_short() -> None:
    long = "n" + "x" * 150
    data = group(solo("p", actions={"a": act(name=long)}))
    message = findings(data).error("phases[0].actions.a.submission.name").message
    assert long not in message and "…" in message


def test_m39_m42_m44_m46_m41_parts_of_the_upper_bound() -> None:
    run = resolve(
        group(
            lister(),
            solo("rep", repeat=2, actions={"a": act(turns=2)}),
            solo("schlaf", actions={"a": act(turns=1)}, standby=["s"], channels=["rund", "los"]),
            {
                "name": "rede",
                "topology": "chat",
                "channels": ["los"],
                "rounds": 2,
                "actions": {"a": act(), "b": act()},
            },
            {
                "name": "lead",
                "topology": "moderated",
                "max_assignments": 3,
                "standby": ["b"],
                "actions": {"a": act(turns=1)},
            },
            {"name": "liste", "topology": "parallel", "over": "li", "actions": {"a": act(turns=1)}},
        )
    )
    assert phase_turns(run, run.phase("rep"))[:2] == (2, 2 * 3)  # M39: repeat × (turns + 1)
    assert phase_turns(run, run.phase("schlaf"))[:2] == (1, 2 + 2 + 2)  # M42: wake-ups per channel
    assert phase_turns(run, run.phase("rede"))[:2] == (2, 2 * (1 + 2 + 2) + 2)  # M44: event
    # M46: assignments (W4b-04 §6.8: per assignment its turns and one return turn of the lead)
    assert phase_turns(run, run.phase("lead"))[:2] == (1, 2 + 3 * (ASSIGNMENT_TURNS + 1))
    least, most, notes = phase_turns(run, run.phase("liste"))
    assert (least, most) == (1, 50 * 2) and "höchstens 50" in notes[0]  # M41


def test_m54_m55_m62_written_out_defaults() -> None:
    data = group(
        {
            "name": "rede",
            "topology": "chat",
            "channels": ["rund"],
            "actions": {"a": act(), "b": act()},
        },
        solo(
            "p",
            inputs=[{"from": "rede"}, {"name": "x"}],
            actions={"a": act(name="x")},
            gate={
                "type": "judge",
                "raters": ["r1"],
                "rule": "mean",
                "action": act("rating"),
                "routes": {"pass": "next", "fail": "next"},
            },
        ),
        rubric={"pass_score": 6},
    )
    run = resolve(data)
    assert run.phase("rede").rounds == 3  # M54
    assert run.phase("p").gate.pass_score == 6  # M55
    assert [i.pick for i in run.phase("p").inputs or []] == [Pick.LATEST, Pick.LATEST]  # M62

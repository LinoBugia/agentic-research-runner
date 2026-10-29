from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from arr.errors import ConfigError
from arr.models import (
    ChannelMode,
    Decider,
    GroupSpec,
    ProjectSettings,
    Provider,
    ResolvedRun,
    RunInput,
    TopologyKind,
    WorkfolderSettings,
    WorkspaceMode,
)
from arr.resolve import (
    check_group_spec,
    dump_yaml_model,
    format_loc,
    load_group_spec,
    load_run_input,
    load_yaml_model,
    resolve_run,
    validate_model,
)

CONFIG = Path(__file__).parent / "fixtures" / "config"
INVALID = CONFIG / "invalid"


@pytest.fixture
def settings() -> WorkfolderSettings:
    return load_yaml_model(CONFIG / "arr.yaml", WorkfolderSettings)


@pytest.fixture
def group() -> GroupSpec:
    return load_group_spec(CONFIG / "group.yaml")


def group_run(**extra: Any) -> RunInput:
    """Minimal run input that starts the proof-review group skill."""
    data = {"project": "collatz", "skill": "proof-review", "task": "Prüfe den Beweis.", **extra}
    return validate_model(RunInput, data)


def free_run(agents: list[dict[str, Any]], **extra: Any) -> RunInput:
    data = {"project": "p", "task": "t", "topology": {"kind": "solo"}, "agents": agents}
    return validate_model(RunInput, {**data, **extra})


def ids(resolved: ResolvedRun) -> list[str]:
    return [agent.id for agent in resolved.agents]


def members(resolved: ResolvedRun) -> dict[str, list[str]]:
    return {channel.name: channel.members for channel in resolved.groupchats}


# --- A1: example files ---------------------------------------------------------------------


def test_run_yaml_from_doc_resolves(settings: WorkfolderSettings) -> None:
    resolved = resolve_run(load_run_input(CONFIG / "run.yaml"), None, settings)

    assert ids(resolved) == ["explorer-1", "explorer-2", "explorer-gpt", "ranker"]
    assert members(resolved) == {
        "main": ["explorer-1", "explorer-2", "explorer-gpt"],
        "ranking": ["ranker"],
    }
    explorer = resolved.agent("explorer-2")
    assert (explorer.provider, explorer.model, explorer.auth) == (
        Provider.CLAUDE,
        "claude-opus-5-5",
        "max-privat",
    )
    assert (explorer.base_id, explorer.replica, explorer.skills) == ("explorer", 2, ["sympy-check"])
    gpt = resolved.agent("explorer-gpt")
    assert (gpt.provider, gpt.model, gpt.auth, gpt.effort) == (
        Provider.CODEX,
        "gpt-6.1-sol",
        "chatgpt-privat",
        "high",
    )
    ranker = resolved.agent("ranker")
    assert ranker.workspace.mode is WorkspaceMode.EMPTY
    assert ranker.read_dirs == ["run:output"]
    assert ranker.channels == ["ranking"]
    assert resolved.topology.kind is TopologyKind.EXPLORATION
    assert resolved.params["ideas"] == ["2-adische Darstellung", "Stoppzeiten-Statistik"]
    assert resolved.limits.wall_time == timedelta(hours=3)
    assert resolved.limits.budget_usd == 25
    assert resolved.approvals.decider is Decider.GATEKEEPER_THEN_USER


def test_group_yaml_from_doc_resolves_with_defaults(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    resolved = resolve_run(group_run(), group, settings)

    assert ids(resolved) == ["author", "judge-1", "judge-2", "judge-3"]
    assert members(resolved) == {
        "author-desk": ["author"],
        "judges": ["judge-1", "judge-2", "judge-3"],
    }
    judges = [resolved.agent(f"judge-{i}") for i in (1, 2, 3)]
    assert [(j.provider, j.model, j.auth) for j in judges] == [
        (Provider.CLAUDE, "claude-opus-5-5", "max-privat"),
        (Provider.CODEX, "gpt-6.1-sol", "chatgpt-privat"),
        (Provider.CLAUDE, "claude-opus-5-5", "max-privat"),
    ]
    assert resolved.topology.kind is TopologyKind.REVIEW
    assert resolved.params == {
        "judges": 3,
        "max_revisions": 2,
        "discussion_rounds": 1,
        "verdict": "majority",
        "judge_memory": "fresh",
        "artifact": None,
    }
    assert resolved.rubric is not None and resolved.rubric.pass_score == 8
    judges_channel = resolved.channel("judges")
    assert judges_channel.anonymous and judges_channel.mode is ChannelMode.ROUNDS
    assert judges_channel.open_from == "discussion"
    assert resolved.limits.wall_time == timedelta(hours=2)


def test_group_yaml_with_meta_judge(group: GroupSpec, settings: WorkfolderSettings) -> None:
    resolved = resolve_run(group_run(), group, settings, {"verdict": "meta_judge", "judges": 2})

    assert ids(resolved) == ["author", "judge-1", "judge-2", "meta-judge"]
    assert members(resolved)["judges"] == ["judge-1", "judge-2", "meta-judge"]
    assert resolved.agent("meta-judge").model == "claude-sonnet-5-5"
    assert resolved.agent("meta-judge").channels == ["judges"]


def test_resolved_run_round_trips_through_yaml(
    group: GroupSpec, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    resolved = resolve_run(group_run(), group, settings)
    path = tmp_path / "run.yaml"
    path.write_text(dump_yaml_model(resolved), encoding="utf-8")

    assert load_yaml_model(path, ResolvedRun) == resolved
    assert "wall_time: 2h" in path.read_text(encoding="utf-8")


def test_run_input_dump_keeps_only_given_fields(tmp_path: Path) -> None:
    run = load_run_input(CONFIG / "run.yaml")
    text = dump_yaml_model(run, exclude_unset=True)
    assert "rubric" not in text and "approvals" not in text
    path = tmp_path / "run.input.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_run_input(path) == run


def test_other_example_files_load() -> None:
    load_yaml_model(CONFIG / "project.yaml", ProjectSettings)


# --- A2: error kinds -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "path", "fragment"),
    [
        ("unknown_field.run.yaml", "agents[0].modell", "Unbekanntes Feld „modell“"),
        ("wrong_type.run.yaml", "limits.turns_per_agent", "erwartet eine ganze Zahl"),
        ("unknown_member.run.yaml", "groupchats[1].members[1]", "Unbekannter Agent „critic“"),
        ("param_out_of_range.group.yaml", "params.max_revisions", "größer als das Maximum 5"),
        ("param_not_in_choices.group.yaml", "params.judge_memory", "Erlaubt sind: fresh"),
        ("when_unknown_param.group.yaml", "agents[2].when.param", "Parameter „verdikt“"),
        (
            "placeholder_unknown_param.group.yaml",
            "agents[1].count",
            "„{{ params.reviewers }}“ verweist auf keinen Parameter",
        ),
    ],
)
def test_invalid_fixture_reports_path_and_german_message(
    file: str, path: str, fragment: str, settings: WorkfolderSettings
) -> None:
    with pytest.raises(ConfigError) as info:
        if file.endswith(".group.yaml"):
            load_group_spec(INVALID / file)
        else:
            resolve_run(load_run_input(INVALID / file), None, settings)
    assert info.value.path == path
    assert fragment in info.value.issue_message
    assert path in str(info.value)


def test_load_errors_carry_line_numbers() -> None:
    with pytest.raises(ConfigError) as info:
        load_run_input(INVALID / "unknown_field.run.yaml")
    assert info.value.issues[0].line == 28
    assert "Meintest du „model“?" in info.value.issue_message
    assert "(Zeile 28)" in str(info.value)


@pytest.mark.parametrize(
    ("overrides", "path", "fragment"),
    [
        ({"judges": 7}, "params.judges", "größer als das Maximum 6"),
        ({"judges": 0}, "params.judges", "kleiner als das Minimum 1"),
        ({"verdict": "vote"}, "params.verdict", "„vote“ ist nicht erlaubt"),
        ({"judges": "drei"}, "params.judges", "Falscher Typ: erwartet ganze Zahl"),
        ({"reviewers": 2}, "params.reviewers", "Unbekannter Parameter „reviewers“"),
    ],
)
def test_param_overrides_are_checked_against_declaration(
    group: GroupSpec,
    settings: WorkfolderSettings,
    overrides: dict[str, Any],
    path: str,
    fragment: str,
) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(group_run(), group, settings, overrides)
    assert info.value.path == path
    assert fragment in info.value.issue_message


def test_run_yaml_params_are_checked_against_declaration(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    run = group_run(topology={"kind": "review", "params": {"max_revisions": 9}})
    with pytest.raises(ConfigError) as info:
        resolve_run(run, group, settings)
    assert info.value.path == "topology.params.max_revisions"


def test_overrides_win_over_run_yaml_params(group: GroupSpec, settings: WorkfolderSettings) -> None:
    run = group_run(topology={"kind": "review", "params": {"judges": 2}})
    assert len(resolve_run(run, group, settings).agent("judge-1").id) > 0
    resolved = resolve_run(run, group, settings, {"judges": 4})
    assert ids(resolved)[-1] == "judge-4"


def test_unknown_agent_in_members_suggests_pattern(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "judge", "count": 2}],
        groupchats=[{"name": "main", "members": ["judge"]}],
    )
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "groupchats[0].members[0]"
    assert "Meintest du „judge-*“" in info.value.issue_message


def test_wrong_enum_value_lists_allowed_values(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": "a", "provider": "gemini"}]), None, settings)
    assert info.value.path == "agents[0].provider"
    assert "Erlaubt sind: claude, codex" in info.value.issue_message


def test_invalid_placeholder_form(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a", "model": "{{ model }}"}])
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "agents[0].model"
    assert "Erlaubt ist nur „{{ params.<name> }}“" in info.value.issue_message


def test_placeholder_without_param_in_free_run(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a", "count": "{{ params.n }}"}])
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "agents[0].count"
    assert "vorhanden: keine" in info.value.issue_message


def test_check_group_spec_finds_placeholders_in_channels(group: GroupSpec) -> None:
    data = group.model_dump(by_alias=True, exclude_unset=True)
    data["groupchats"][0]["purpose"] = "Runde {{ params.runden }}"
    with pytest.raises(ConfigError) as info:
        check_group_spec(GroupSpec.model_validate(data))
    assert info.value.path == "groupchats[0].purpose"


def test_yaml_syntax_error_has_line(tmp_path: Path) -> None:
    path = tmp_path / "run.yaml"
    path.write_text("project: p\ntask: t\nagents: [\n  - id: a\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_run_input(path)
    assert "kein gültiges YAML" in info.value.issue_message
    assert info.value.issues[0].line is not None


def test_duplicate_yaml_key(tmp_path: Path) -> None:
    path = tmp_path / "run.yaml"
    path.write_text("project: p\nproject: q\ntask: t\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_run_input(path)
    assert "doppelt" in info.value.issue_message
    assert info.value.issues[0].line == 2


def test_missing_and_empty_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Datei nicht gefunden"):
        load_run_input(tmp_path / "fehlt.yaml")
    (tmp_path / "leer.yaml").write_text("# nur Kommentar\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Die Datei ist leer"):
        load_run_input(tmp_path / "leer.yaml")
    (tmp_path / "liste.yaml").write_text("- a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="schlüssel: wert"):
        load_run_input(tmp_path / "liste.yaml")


def test_several_issues_are_reported_together(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a"}],
        groupchats=[{"name": "main", "members": ["b", "c"]}],
    )
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert [issue.path for issue in info.value.issues] == [
        "groupchats[0].members[0]",
        "groupchats[0].members[1]",
    ]


# --- A3: count, mix, patterns, placeholders, when --------------------------------------------


def test_count_one_still_gets_suffix(group: GroupSpec, settings: WorkfolderSettings) -> None:
    resolved = resolve_run(group_run(), group, settings, {"judges": 1})
    assert ids(resolved) == ["author", "judge-1"]
    assert resolved.agent("judge-1").replica == 1
    assert members(resolved)["judges"] == ["judge-1"]


def test_without_count_id_is_unchanged(settings: WorkfolderSettings) -> None:
    resolved = resolve_run(free_run([{"id": "solo"}]), None, settings)
    assert ids(resolved) == ["solo"]
    assert resolved.agents[0].replica is None


def test_count_zero_drops_agent_and_its_members(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "lead"}, {"id": "critic", "count": 0}],
        groupchats=[{"name": "main", "members": ["lead", "critic-*"]}],
    )
    resolved = resolve_run(run, None, settings)
    assert ids(resolved) == ["lead"]
    assert members(resolved) == {"main": ["lead"]}


def test_mix_is_round_robin(settings: WorkfolderSettings) -> None:
    mix = [{"model": "m-a"}, {"model": "m-b"}, {"provider": "codex", "model": "m-c"}]
    resolved = resolve_run(free_run([{"id": "x", "count": 5, "mix": mix}]), None, settings)
    assert [(a.model, a.provider) for a in resolved.agents] == [
        ("m-a", Provider.CLAUDE),
        ("m-b", Provider.CLAUDE),
        ("m-c", Provider.CODEX),
        ("m-a", Provider.CLAUDE),
        ("m-b", Provider.CLAUDE),
    ]


def test_mix_entry_wins_over_agent_fields(settings: WorkfolderSettings) -> None:
    agent = {"id": "x", "count": 2, "model": "own", "mix": [{"effort": "low"}, {"model": "mixed"}]}
    resolved = resolve_run(free_run([agent]), None, settings)
    assert [(a.model, a.effort) for a in resolved.agents] == [("own", "low"), ("mixed", "high")]


def test_mix_without_count_is_an_error(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": "x", "mix": [{"model": "m"}]}]), None, settings)
    assert info.value.path == "agents[0]"
    assert "braucht „count“" in info.value.issue_message


def test_patterns_keep_agent_order_and_deduplicate(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a", "count": 2}, {"id": "b"}, {"id": "a-x"}],
        groupchats=[{"name": "main", "members": ["b", "a-*", "a-1", "?"]}],
    )
    resolved = resolve_run(run, None, settings)
    assert members(resolved)["main"] == ["b", "a-1", "a-2", "a-x"]


def test_pattern_matching_only_disabled_agents_is_empty(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    data = group.model_dump(by_alias=True, exclude_unset=True)
    data["groupchats"].append({"name": "meta", "members": ["meta-*"]})
    data["groupchats"].append({"name": "mixed", "members": ["author", "meta-*"]})
    spec = GroupSpec.model_validate(data)

    resolved = resolve_run(group_run(), spec, settings)
    assert "meta" not in members(resolved), "channel without active members is dropped"
    assert members(resolved)["mixed"] == ["author"]

    resolved = resolve_run(group_run(), spec, settings, {"verdict": "meta_judge"})
    assert members(resolved)["meta"] == ["meta-judge"]


def test_pattern_matching_nothing_is_an_error(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a"}], groupchats=[{"name": "main", "members": ["a", "z*"]}])
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "groupchats[0].members[1]"
    assert "passt auf keinen Agenten" in info.value.issue_message


def test_whole_placeholder_keeps_type_embedded_becomes_text(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    data = group.model_dump(by_alias=True, exclude_unset=True)
    data["agents"][0]["role"] = "roles/author-{{ params.judge_memory }}.md"
    data["groupchats"][1]["purpose"] = "{{params.judges}} Judges, Urteil {{ params.verdict }}"
    spec = GroupSpec.model_validate(data)

    resolved = resolve_run(group_run(), spec, settings, {"judges": 2})
    assert resolved.agent("author").role == "roles/author-fresh.md"
    assert resolved.channel("judges").purpose == "2 Judges, Urteil majority"
    assert ids(resolved) == ["author", "judge-1", "judge-2"]


def test_placeholder_with_wrong_value_type_for_count(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a", "count": "{{ params.n }}"}], topology={"kind": "solo"})
    run.topology.params["n"] = "viele"  # type: ignore[union-attr]
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "agents[0].count"
    assert "ganze Zahl" in info.value.issue_message


def test_free_run_uses_its_own_params_for_placeholders(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a", "count": "{{ params.n }}"}],
        topology={"kind": "roundtable", "params": {"n": 2}},
    )
    assert ids(resolve_run(run, None, settings)) == ["a-1", "a-2"]
    assert ids(resolve_run(run, None, settings, {"n": 3})) == ["a-1", "a-2", "a-3"]


def test_when_with_in(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a"}, {"id": "b", "when": {"param": "mode", "in": ["x", "y"]}}],
        topology={"kind": "solo", "params": {"mode": "y"}},
    )
    assert ids(resolve_run(run, None, settings)) == ["a", "b"]
    assert ids(resolve_run(run, None, settings, {"mode": "z"})) == ["a"]


def test_when_needs_exactly_one_condition(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": "a", "when": {"param": "p"}}]), None, settings)
    assert info.value.path == "agents[0].when"


def test_when_unknown_param_in_free_run(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a"}, {"id": "b", "when": {"param": "p", "equals": 1}}])
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "agents[1].when.param"


def test_when_equals_null(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a"}, {"id": "writer", "when": {"param": "artifact", "equals": None}}],
        topology={"kind": "solo", "params": {"artifact": None}},
    )
    assert ids(resolve_run(run, None, settings)) == ["a", "writer"]
    assert ids(resolve_run(run, None, settings, {"artifact": "tex/b.tex"})) == ["a"]


# --- Defaults, limits, approvals and other rules ---------------------------------------------


def test_defaults_of_another_provider_do_not_leak(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "c"}, {"id": "g", "provider": "codex"}],
        defaults={"provider": "claude", "model": "claude-opus-5-5", "auth": "max-uni"},
    )
    resolved = resolve_run(run, None, settings)
    assert (resolved.agent("c").model, resolved.agent("c").auth) == ("claude-opus-5-5", "max-uni")
    assert (resolved.agent("g").model, resolved.agent("g").auth) == (
        "gpt-6.1-sol",
        "chatgpt-privat",
    )


def test_tools_merge_field_by_field(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a", "tools": {"web": True}}], defaults={"tools": {"shell": False}})
    tools = resolve_run(run, None, settings).agents[0].tools
    assert (tools.shell, tools.web) == (False, True)


def test_project_read_dirs_are_default(settings: WorkfolderSettings) -> None:
    project = ProjectSettings(read_dirs=["~/Literatur"])
    run = free_run([{"id": "a"}, {"id": "b", "read_dirs": []}])
    resolved = resolve_run(run, None, settings, project_settings=project)
    assert resolved.agent("a").read_dirs == ["~/Literatur"]
    assert resolved.agent("b").read_dirs == []


def test_missing_model_and_auth(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": "a"}]), None, WorkfolderSettings())
    assert [i.path for i in info.value.issues] == ["agents[0].model", "agents[0].auth"]
    assert "Für claude fehlt ein Modell („a“)" in info.value.issues[0].message
    assert "Für claude fehlt ein Auth-Profil" in info.value.issues[1].message
    assert "defaults.claude" in info.value.issues[1].message


def test_duplicate_ids_after_count(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": "a", "count": 2}, {"id": "a-2"}]), None, settings)
    assert info.value.path == "agents[1].id"
    assert "mehrfach" in info.value.issue_message


@pytest.mark.parametrize("agent_id", ["user", "orchestrator", "Judge", "a b"])
def test_invalid_or_reserved_agent_ids(settings: WorkfolderSettings, agent_id: str) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": agent_id}]), None, settings)
    assert info.value.path == "agents[0].id"


def test_limits_merge_field_by_field(group: GroupSpec, settings: WorkfolderSettings) -> None:
    data = group.model_dump(by_alias=True, exclude_unset=True)
    data["limits"] = {"budget_usd": 5}
    spec = GroupSpec.model_validate(data)
    resolved = resolve_run(group_run(limits={"turns_per_agent": 3}), spec, settings)
    assert resolved.limits.model_dump() == {
        "wall_time": timedelta(hours=2),
        "turns_per_agent": 3,
        "budget_usd": 5,
    }


def test_run_approvals_override_workfolder(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a"}], approvals={"decider": "user"})
    approvals = resolve_run(run, None, settings).approvals
    assert approvals.decider is Decider.USER
    assert approvals.timeout == timedelta(minutes=20)


def test_topology_mismatch_with_skill(group: GroupSpec, settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(group_run(topology="waterfall"), group, settings)
    assert info.value.path == "topology.kind"


def test_free_run_needs_topology_and_agents(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(RunInput(project="p", task="t", agents=[{"id": "a"}]), None, settings)
    assert info.value.path == "topology"
    with pytest.raises(ConfigError) as info:
        resolve_run(RunInput(project="p", task="t", topology="solo"), None, settings)
    assert info.value.path == "agents"


def test_run_agents_replace_group_agents(group: GroupSpec, settings: WorkfolderSettings) -> None:
    run = group_run(
        agents=[{"id": "author"}, {"id": "judge", "count": "{{ params.judges }}"}],
        groupchats=[{"name": "all", "members": ["*"]}],
    )
    resolved = resolve_run(run, group, settings, {"judges": 2})
    assert ids(resolved) == ["author", "judge-1", "judge-2"]
    assert members(resolved) == {"all": ["author", "judge-1", "judge-2"]}


def test_moderator_must_be_member(settings: WorkfolderSettings) -> None:
    chats = [{"name": "main", "members": ["a"], "mode": "moderated", "moderator": "b"}]
    run = free_run([{"id": "a"}, {"id": "b"}], groupchats=chats)
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "groupchats[0].moderator"


def test_stages_and_raters(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "prover"}, {"id": "rater", "count": 2}],
        topology={"kind": "waterfall"},
        stages=[{"name": "skizze", "agent": "prover", "produces": "sketch.md"}],
        raters=["rater-*"],
    )
    resolved = resolve_run(run, None, settings)
    assert resolved.raters == ["rater-1", "rater-2"]
    assert resolved.stages[0].agent == "prover"

    bad = run.model_copy(update={"stages": [run.stages[0].model_copy(update={"agent": "x"})]})
    with pytest.raises(ConfigError) as info:
        resolve_run(bad, None, settings)
    assert info.value.path == "stages[0].agent"


def test_format_loc() -> None:
    assert format_loc(("agents", 1, "model")) == "agents[1].model"
    assert format_loc(("skills", "list[str]", 0)) == "skills[0]"
    assert format_loc(()) == ""


# --- W2-04: carry-overs from the W1-01.1 review (N1, N2, N4) -------------------------------


def test_w2_04_stage_with_an_active_and_a_disabled_variant_of_one_id(
    settings: WorkfolderSettings,
) -> None:
    """N1: the active agent counts first, a switched-off variant of the same ID does not."""
    variants = [
        {"id": "p", "when": {"param": "mode", "equals": "a"}},
        {"id": "p", "when": {"param": "mode", "equals": "b"}},
        {"id": "r"},
    ]
    run = free_run(
        variants,
        topology={"kind": "waterfall", "params": {"mode": "a"}},
        stages=[{"name": "s1", "agent": "p"}],
    )
    assert resolve_run(run, None, settings).stages[0].agent == "p"
    # a switched-off count agent `j` next to an own agent `j-1`
    run = free_run(
        [{"id": "j", "count": 0}, {"id": "j-1"}],
        topology={"kind": "waterfall"},
        stages=[{"name": "s1", "agent": "j-1"}],
    )
    assert resolve_run(run, None, settings).stages[0].agent == "j-1"
    # only disabled: still the specific message
    run = free_run(
        [{"id": "p", "when": {"param": "mode", "equals": "b"}}, {"id": "r"}],
        topology={"kind": "waterfall", "params": {"mode": "a"}},
        stages=[{"name": "s1", "agent": "p"}],
    )
    with pytest.raises(ConfigError, match="ausgeschaltet") as info:
        resolve_run(run, None, settings)
    assert info.value.path == "stages[0].agent"


@pytest.mark.parametrize("value", [2.5, -1, 51, True])
def test_w2_04_count_error_from_a_placeholder_names_the_parameter(
    settings: WorkfolderSettings, value: Any
) -> None:
    """N2: the message names the start parameter, not only `count`."""
    run = free_run([{"id": "judge", "count": "{{ params.judges }}"}], topology={"kind": "solo"})
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings, {"judges": value})
    assert info.value.path == "agents[0].count"
    assert info.value.issue_message.startswith("Parameter „judges“ = ")
    assert "wird als „count“ benutzt und muss eine ganze Zahl" in info.value.issue_message


def test_w2_04_integral_float_counts_as_integer(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "judge", "count": "{{ params.judges }}"}], topology={"kind": "solo"})
    assert ids(resolve_run(run, None, settings, {"judges": 2.0})) == ["judge-1", "judge-2"]


def test_w2_04_timeout_null_has_a_german_message(settings: WorkfolderSettings) -> None:
    """N4: `approvals: {timeout: null}` no longer shows the English pydantic text."""
    run = free_run([{"id": "a"}], approvals={"timeout": None})
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "approvals.timeout"
    assert info.value.issue_message.startswith("Ungültige Dauer leer.")
    assert "valid" not in info.value.issue_message

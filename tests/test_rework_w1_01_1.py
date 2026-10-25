"""Tests for the rework W1-01.1 (points 1-12 of umsetzung/welle-1/W1-01.1_…_Auftrag.md)."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import CostKind, TurnEndData, Usage
from arr.errors import ConfigError
from arr.models import (
    Decider,
    GroupSpec,
    ParamSpec,
    Provider,
    ResolvedRun,
    RunInput,
    WorkfolderSettings,
)
from arr.permissions.types import ApprovalItem, Decision
from arr.resolve import (
    check_group_spec,
    load_group_spec,
    load_yaml_model,
    resolve_run,
    validate_model,
)

CONFIG = Path(__file__).parent / "fixtures" / "config"


@pytest.fixture
def settings() -> WorkfolderSettings:
    return load_yaml_model(CONFIG / "arr.yaml", WorkfolderSettings)


@pytest.fixture
def claude_only() -> WorkfolderSettings:
    """Workfolder settings without `defaults.codex`."""
    return validate_model(
        WorkfolderSettings,
        {"defaults": {"claude": {"auth": "max-privat", "model": "claude-sonnet-5-5"}}},
    )


@pytest.fixture
def group() -> GroupSpec:
    return load_group_spec(CONFIG / "group.yaml")


def free_run(agents: list[dict[str, Any]], **extra: Any) -> RunInput:
    data = {"project": "p", "task": "t", "topology": {"kind": "solo"}, "agents": agents}
    return validate_model(RunInput, {**data, **extra})


def group_run(**extra: Any) -> RunInput:
    return validate_model(RunInput, {"project": "p", "task": "t", **extra})


def ids(resolved: ResolvedRun) -> list[str]:
    return [agent.id for agent in resolved.agents]


def members(resolved: ResolvedRun) -> dict[str, list[str]]:
    return {channel.name: channel.members for channel in resolved.groupchats}


# --- 1: limits field by field over safe built-in values ------------------------------------


def test_partial_workfolder_limits_keep_builtin_values() -> None:
    settings = validate_model(WorkfolderSettings, {"limits": {"wall_time": "3h"}})
    limits = resolve_run(free_run([{"id": "a", "model": "m", "auth": "p"}]), None, settings).limits
    assert (limits.wall_time, limits.turns_per_agent, limits.budget_usd) == (
        timedelta(hours=3),
        40,
        20,
    )


def test_workfolder_without_limits_uses_builtin_values() -> None:
    limits = resolve_run(
        free_run([{"id": "a", "model": "m", "auth": "p"}]), None, WorkfolderSettings()
    ).limits
    assert (limits.wall_time, limits.turns_per_agent, limits.budget_usd) == (
        timedelta(hours=2),
        40,
        20,
    )


def test_limit_is_switched_off_only_explicitly() -> None:
    settings = validate_model(WorkfolderSettings, {"limits": {"budget_usd": None}})
    run = free_run([{"id": "a", "model": "m", "auth": "p"}], limits={"turns_per_agent": None})
    limits = resolve_run(run, None, settings).limits
    assert (limits.wall_time, limits.turns_per_agent, limits.budget_usd) == (
        timedelta(hours=2),
        None,
        None,
    )


# --- 2: E15 as invariant of ApprovalItem -----------------------------------------------------


@pytest.mark.parametrize("decided_by", ["gatekeeper", "fixed_rule", "mandate_rule"])
@pytest.mark.parametrize("action", ["allow", "deny"])
def test_run_start_is_decided_by_user_only(decided_by: str, action: str) -> None:
    decision = Decision(action=action, decided_by=decided_by)
    with pytest.raises(ValueError, match="E15"):
        ApprovalItem(id="x", type="run_start", run_id="r", status="decided", decision=decision)


@pytest.mark.parametrize("scope", ["always", "agent", "run"])
def test_run_start_decision_is_once(scope: str) -> None:
    decision = Decision(action="allow", decided_by="user", scope=scope)
    with pytest.raises(ValueError, match="einmalig"):
        ApprovalItem(id="x", type="run_start", run_id="r", status="decided", decision=decision)


@pytest.mark.parametrize("scope", ["always", "run"])
def test_escalation_decision_is_once(scope: str) -> None:
    decision = Decision(action="option", option="retry", decided_by="user", scope=scope)
    with pytest.raises(ValueError, match="E15"):
        ApprovalItem(
            id="x",
            type="escalation",
            run_id="r",
            options=["retry", "end"],
            status="decided",
            decision=decision,
        )


def test_run_start_by_user_once_is_valid() -> None:
    for action in ("allow", "deny"):
        decision = Decision(action=action, decided_by="user")
        item = ApprovalItem(
            id="x", type="run_start", run_id="r", status="decided", decision=decision
        )
        assert item.decision is not None and item.decision.decided_by == "user"


# --- 3: provider-bound fields per layer ------------------------------------------------------


def test_mix_switching_provider_does_not_take_agent_model(settings: WorkfolderSettings) -> None:
    """Review case (a): model on agent level, mix switches to codex without a model."""
    agent = {
        "id": "j",
        "count": 2,
        "model": "claude-opus-5-5",
        "mix": [{"provider": "claude"}, {"provider": "codex"}],
    }
    resolved = resolve_run(free_run([agent]), None, settings)
    j1, j2 = resolved.agents
    assert (j1.provider, j1.model, j1.auth) == (Provider.CLAUDE, "claude-opus-5-5", "max-privat")
    assert (j2.provider, j2.model, j2.auth) == (Provider.CODEX, "gpt-6.1-sol", "chatgpt-privat")


def test_mix_switching_provider_without_codex_defaults_is_error(
    claude_only: WorkfolderSettings,
) -> None:
    agent = {
        "id": "j",
        "count": 2,
        "model": "claude-opus-5-5",
        "mix": [{"provider": "claude"}, {"provider": "codex"}],
    }
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([agent]), None, claude_only)
    assert [i.path for i in info.value.issues] == [
        "agents[0].mix[1].model",
        "agents[0].mix[1].auth",
    ]
    message = info.value.issues[0].message
    assert message.startswith("Für codex fehlt ein Modell („j-2“). Ergänze „model“ im mix-Eintrag")
    assert "arr.yaml „defaults.codex“" in message
    assert "„claude-opus-5-5“ gilt für claude" in message


def test_defaults_without_provider_are_claude_defaults(settings: WorkfolderSettings) -> None:
    """Review case (b): run defaults with a model but without provider."""
    run = free_run(
        [{"id": "c"}, {"id": "g", "provider": "codex"}],
        defaults={"model": "claude-opus-5-5"},
    )
    resolved = resolve_run(run, None, settings)
    assert resolved.agent("c").model == "claude-opus-5-5"
    assert (resolved.agent("g").model, resolved.agent("g").auth) == (
        "gpt-6.1-sol",
        "chatgpt-privat",
    )


def test_defaults_without_provider_and_no_codex_defaults_is_error(
    claude_only: WorkfolderSettings,
) -> None:
    run = free_run([{"id": "g", "provider": "codex"}], defaults={"model": "claude-opus-5-5"})
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, claude_only)
    assert info.value.path == "agents[0].model"
    assert "Für codex fehlt ein Modell" in info.value.issue_message


def test_defaults_provider_is_inherited_by_agent_and_mix(
    claude_only: WorkfolderSettings,
) -> None:
    """Agent and mix entries without provider take the provider of the defaults layer."""
    agent = {"id": "j", "count": 2, "auth": "max-privat", "mix": [{}, {"provider": "codex"}]}
    run = free_run([agent], defaults={"provider": "codex", "model": "gpt-6.1-sol", "auth": "gpt"})
    resolved = resolve_run(run, None, claude_only)
    assert [(a.provider, a.model, a.auth) for a in resolved.agents] == [
        (Provider.CODEX, "gpt-6.1-sol", "max-privat"),
        (Provider.CODEX, "gpt-6.1-sol", "max-privat"),
    ]


def test_doc_fixtures_unchanged_by_provider_rule(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    resolved = resolve_run(group_run(), group, settings)
    assert [(a.id, a.provider, a.model, a.auth, a.effort) for a in resolved.agents] == [
        ("author", Provider.CLAUDE, "claude-opus-5-5", "max-privat", "high"),
        ("judge-1", Provider.CLAUDE, "claude-opus-5-5", "max-privat", "high"),
        ("judge-2", Provider.CODEX, "gpt-6.1-sol", "chatgpt-privat", "high"),
        ("judge-3", Provider.CLAUDE, "claude-opus-5-5", "max-privat", "high"),
    ]


# --- 4: approvals checked after merging ------------------------------------------------------


def test_run_approvals_gatekeeper_from_workfolder(settings: WorkfolderSettings) -> None:
    run = free_run([{"id": "a"}], approvals={"decider": "gatekeeper"})
    approvals = resolve_run(run, None, settings).approvals
    assert approvals.decider is Decider.GATEKEEPER
    assert approvals.gatekeeper is not None and approvals.gatekeeper.model == "claude-sonnet-5-5"


def test_run_approvals_gatekeeper_missing_after_merge(claude_only: WorkfolderSettings) -> None:
    run = free_run([{"id": "a"}], approvals={"decider": "gatekeeper"})
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, claude_only)
    assert info.value.path == "approvals"
    assert info.value.source == "run.yaml"
    assert "fehlt der Abschnitt „gatekeeper“" in info.value.issue_message


# --- 5: cost_kind ----------------------------------------------------------------------------


def test_turn_end_data_has_optional_cost_kind() -> None:
    assert "cost_kind" in TurnEndData.__optional_keys__


def test_usage_sum_unknown_beats_estimated() -> None:
    unknown = Usage(cost_usd=1)
    billed = Usage(cost_usd=2, cost_kind=CostKind.BILLED)
    estimated = Usage(cost_usd=3, cost_kind=CostKind.ESTIMATED)
    assert (unknown + billed).cost_kind is None
    assert (estimated + unknown).cost_kind is None
    assert (billed + estimated).cost_kind is CostKind.ESTIMATED
    assert (billed + Usage(input=5)).cost_kind is CostKind.BILLED


# --- 6: placeholders inside text -------------------------------------------------------------


def _text_run(role: str, params: dict[str, Any]) -> RunInput:
    return free_run([{"id": "a", "role": role}], topology={"kind": "solo", "params": params})


def test_bool_in_text_is_lowercase(settings: WorkfolderSettings) -> None:
    run = _text_run("roles/{{ params.art }}-{{ params.b }}.md", {"art": "x", "b": True})
    assert resolve_run(run, None, settings).agents[0].role == "roles/x-true.md"


def test_none_in_text_is_error(settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(_text_run("roles/{{ params.art }}.md", {"art": None}), None, settings)
    assert info.value.path == "agents[0].role"
    assert "Parameter „art“ hat keinen Wert" in info.value.issue_message


@pytest.mark.parametrize(("value", "what"), [(["x"], "eine Liste"), ({"k": 1}, "ein Abschnitt")])
def test_list_or_section_in_text_is_error(
    settings: WorkfolderSettings, value: Any, what: str
) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(_text_run("roles/{{ params.art }}.md", {"art": value}), None, settings)
    assert f"Parameter „art“ ist {what}" in info.value.issue_message


def test_whole_value_placeholder_unchanged(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a", "model": "{{ params.model }}", "count": "{{ params.n }}"}],
        topology={"kind": "solo", "params": {"model": "claude-opus-5-5", "n": 1}},
    )
    agent = resolve_run(run, None, settings).agents[0]
    assert (agent.id, agent.model) == ("a-1", "claude-opus-5-5")


def test_group_spec_with_null_default_in_text_loads(group: GroupSpec) -> None:
    data = group.model_dump(by_alias=True, exclude_unset=True)
    data["agents"][0]["role"] = "roles/author-{{ params.artifact }}.md"
    check_group_spec(GroupSpec.model_validate(data))  # value is only known at start


# --- 7: count: 0 switches off every <id>-<n> ------------------------------------------------


def test_count_zero_switches_off_every_replica_id(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "lead"}, {"id": "j", "count": 0}],
        groupchats=[{"name": "main", "members": ["lead", "j-1", "j-2", "j-[3-9]"]}],
        raters=["j-7"],
    )
    resolved = resolve_run(run, None, settings)
    assert members(resolved) == {"main": ["lead"]}
    assert resolved.raters == []


def test_when_switched_off_count_agent_beyond_count(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    data = group.model_dump(by_alias=True, exclude_unset=True)
    data["agents"].append(
        {"id": "extra", "count": 2, "when": {"param": "verdict", "equals": "unanimous"}}
    )
    data["groupchats"].append({"name": "x", "members": ["author", "extra-5"]})
    resolved = resolve_run(group_run(), GroupSpec.model_validate(data), settings)
    assert members(resolved)["x"] == ["author"]


# --- 8: stage with a switched-off agent ------------------------------------------------------


def test_stage_with_switched_off_agent(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "prover"}, {"id": "checker", "when": {"param": "check", "equals": True}}],
        topology={"kind": "waterfall", "params": {"check": False}},
        stages=[{"name": "pruefen", "agent": "checker"}],
    )
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "stages[0].agent"
    assert "durch „when“ bzw. „count“ ausgeschaltet" in info.value.issue_message
    assert "unbekannt" not in info.value.issue_message


# --- 9: source of parameter errors -----------------------------------------------------------


def test_param_error_sources(group: GroupSpec, settings: WorkfolderSettings) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(
            group_run(topology={"kind": "review", "params": {"judges": 9}}), group, settings
        )
    assert (info.value.path, info.value.source) == ("topology.params.judges", "run.yaml")
    with pytest.raises(ConfigError) as info:
        resolve_run(group_run(), group, settings, {"judges": 9})
    assert (info.value.path, info.value.source) == ("params.judges", "Start-Parameter")
    assert "Start-Parameter" in str(info.value)


# --- 10: upper limit for count ---------------------------------------------------------------


def test_count_upper_limit(settings: WorkfolderSettings) -> None:
    assert len(resolve_run(free_run([{"id": "a", "count": 50}]), None, settings).agents) == 50
    with pytest.raises(ConfigError) as info:
        resolve_run(free_run([{"id": "a", "count": 5000}]), None, settings)
    assert info.value.path == "agents[0].count"
    assert "höchstens 50" in info.value.issue_message


def test_count_upper_limit_through_param(settings: WorkfolderSettings) -> None:
    run = free_run(
        [{"id": "a", "count": "{{ params.n }}"}], topology={"kind": "solo", "params": {"n": 51}}
    )
    with pytest.raises(ConfigError) as info:
        resolve_run(run, None, settings)
    assert info.value.path == "agents[0].count"


# --- 11: decimal number for an integer default -----------------------------------------------


def test_decimal_for_integer_default(settings: WorkfolderSettings) -> None:
    assert ParamSpec(default=7).problem(7.5) is None
    assert ParamSpec(default=0.5, min=0, max=1).problem(1) is None
    assert ParamSpec(default=7).problem(True) is not None


def test_decimal_used_as_count_is_rejected_by_count(
    group: GroupSpec, settings: WorkfolderSettings
) -> None:
    with pytest.raises(ConfigError) as info:
        resolve_run(group_run(), group, settings, {"judges": 2.5})
    assert info.value.path == "agents[1].count"
    assert "ganze Zahl" in info.value.issue_message


def test_decimal_param_in_waterfall(settings: WorkfolderSettings) -> None:
    spec = validate_model(
        GroupSpec,
        {
            "topology": "waterfall",
            "params": {"pass_score": {"default": 7, "min": 0, "max": 10}},
            "agents": [{"id": "prover"}],
        },
    )
    resolved = resolve_run(group_run(), spec, settings, {"pass_score": 7.5})
    assert resolved.params["pass_score"] == 7.5


# --- 12: long form of GroupSpec.topology -----------------------------------------------------


def test_group_topology_long_form() -> None:
    spec = validate_model(GroupSpec, {"topology": {"kind": "review"}, "agents": [{"id": "a"}]})
    assert spec.topology == "review"


def test_group_topology_params_point_to_params() -> None:
    with pytest.raises(ConfigError) as info:
        validate_model(
            GroupSpec,
            {"topology": {"kind": "review", "params": {"judges": 3}}, "agents": [{"id": "a"}]},
        )
    assert info.value.path == "topology"
    assert "unter „params:“" in info.value.issue_message


def test_group_topology_long_form_unknown_key() -> None:
    with pytest.raises(ConfigError) as info:
        validate_model(GroupSpec, {"topology": {"kind": "review", "x": 1}, "agents": [{"id": "a"}]})
    assert "unbekannt: x" in info.value.issue_message

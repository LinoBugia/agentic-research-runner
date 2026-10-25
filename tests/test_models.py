from datetime import timedelta
from typing import Any

import pytest

from arr.errors import ConfigError
from arr.models import (
    ACTIVE_RUN_STATUSES,
    AgentStatus,
    AuthKind,
    AuthProfile,
    ChannelMode,
    GroupChatConfig,
    Limits,
    ParamSpec,
    ProfilesFile,
    ProjectSettings,
    Provider,
    RoundOrder,
    Rubric,
    RunInput,
    RunStatus,
    Starter,
    WakeOn,
    WhenCondition,
    WorkfolderSettings,
    Workspace,
    format_duration,
    parse_duration,
)
from arr.resolve import validate_model


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2h", timedelta(hours=2)),
        ("90m", timedelta(minutes=90)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("45s", timedelta(seconds=45)),
        ("1d", timedelta(days=1)),
        (600, timedelta(minutes=10)),
    ],
)
def test_parse_duration(text: Any, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "2 Stunden", "h", "-5m", -1, True])
def test_parse_duration_rejects(bad: Any) -> None:
    with pytest.raises(ValueError, match="Ungültige Dauer"):
        parse_duration(bad)


def test_format_duration_is_inverse() -> None:
    for text in ("2h", "1h30m", "45s", "1d2h", "0s"):
        assert format_duration(parse_duration(text)) == text


def test_limits_json_dump_uses_text_duration() -> None:
    limits = Limits(wall_time="3h", turns_per_agent=5)
    assert limits.model_dump(mode="json")["wall_time"] == "3h"
    assert limits.model_dump()["wall_time"] == timedelta(hours=3)


def test_workspace_rules() -> None:
    assert Workspace(mode="shared", name="beweis").name == "beweis"
    with pytest.raises(ValueError, match="fehlt „name“"):
        Workspace(mode="shared")
    with pytest.raises(ValueError, match="nur bei „mode: shared“"):
        Workspace(mode="copy", name="x")


def test_groupchat_defaults_match_doc() -> None:
    chat = GroupChatConfig(name="main", members=["a"])
    assert chat.mode is ChannelMode.EVENT
    assert chat.order is RoundOrder.PARALLEL
    assert chat.wake_on == [WakeOn.MENTION, WakeOn.QUESTION]
    assert (chat.debounce_s, chat.max_wakeups_per_agent, chat.quiet_after_s) == (20, 12, 300)
    assert (chat.max_message_chars, chat.summary_every) == (3000, 15)
    assert (chat.moderator, chat.open_from, chat.anonymous) == (None, None, False)


def test_groupchat_moderator_rules() -> None:
    with pytest.raises(ValueError, match="„moderator“ fehlt"):
        GroupChatConfig(name="m", members=["a"], mode="moderated")
    with pytest.raises(ValueError, match="nur bei „mode: moderated“"):
        GroupChatConfig(name="m", members=["a"], moderator="a")


def test_run_input_needs_exactly_one_task() -> None:
    with pytest.raises(ConfigError, match="genau einmal"):
        validate_model(RunInput, {"project": "p"})
    with pytest.raises(ConfigError, match="genau einmal"):
        validate_model(RunInput, {"project": "p", "task": "t", "task_file": "task.md"})


def test_topology_short_form() -> None:
    run = validate_model(RunInput, {"project": "p", "task": "t", "topology": "solo"})
    assert run.topology is not None and run.topology.params == {}


def test_param_spec_checks() -> None:
    spec = ParamSpec(default=3, min=1, max=6)
    assert spec.problem(4) is None
    assert "Maximum" in (spec.problem(7) or "")
    assert "Typ" in (spec.problem(True) or "")
    assert ParamSpec(default=None).problem("tex/b.tex") is None
    assert ParamSpec(default=0.5).problem(1) is None
    with pytest.raises(ValueError, match="größer als „max“"):
        ParamSpec(min=3, max=1)


def test_when_condition() -> None:
    assert WhenCondition(param="v", equals="x").matches("x")
    assert not WhenCondition(param="v", equals="x").matches("y")
    assert WhenCondition.model_validate({"param": "v", "in": [1, 2]}).matches(2)


def test_rubric_pass_score_in_scale() -> None:
    with pytest.raises(ValueError, match="außerhalb der Skala"):
        Rubric(scale=(1, 10), pass_score=11)


def test_workfolder_defaults_without_file() -> None:
    settings = WorkfolderSettings()
    assert settings.limits.wall_time == timedelta(hours=2)
    assert settings.approvals.timeout == timedelta(minutes=20)
    assert settings.user_agent_runs.require_approval is True


def test_require_approval_cannot_be_switched_off() -> None:
    with pytest.raises(ConfigError) as info:
        validate_model(WorkfolderSettings, {"user_agent_runs": {"require_approval": False}})
    assert info.value.path == "user_agent_runs.require_approval"
    assert "E15" in info.value.issue_message


def test_gatekeeper_decider_needs_gatekeeper() -> None:
    with pytest.raises(ConfigError) as info:
        validate_model(WorkfolderSettings, {"approvals": {"decider": "gatekeeper"}})
    assert info.value.path == "approvals"


def test_unknown_provider_key_in_workfolder_defaults() -> None:
    with pytest.raises(ConfigError) as info:
        validate_model(WorkfolderSettings, {"defaults": {"gemini": {"model": "x"}}})
    assert info.value.path == "defaults.gemini"


def test_project_settings_skills() -> None:
    assert ProjectSettings().skills == "all"
    assert ProjectSettings(skills=["arr"]).skills == ["arr"]
    with pytest.raises(ConfigError) as info:
        validate_model(ProjectSettings, {"skills": "some"})
    assert info.value.path == "skills"


def test_project_read_dirs_validate_run_prefix() -> None:
    settings = ProjectSettings(read_dirs=["run:shared/beweis", "~/x"])
    assert settings.read_dirs == ["run:shared/beweis", "~/x"]
    with pytest.raises(ValueError, match="Unbekannter Run-Ordner"):
        ProjectSettings(read_dirs=["run:agents"])


@pytest.mark.parametrize("secret_field", ["token", "api_key", "secret", "password"])
def test_auth_profile_has_no_secret_field(secret_field: str) -> None:
    data = {"name": "claude-api-uni", "kind": "claude_api_key", secret_field: "sk-ant-xyz"}
    with pytest.raises(ConfigError) as info:
        validate_model(AuthProfile, data)
    assert info.value.path == secret_field
    assert "sk-ant" not in str(info.value), "the secret must not appear in the message"


def test_auth_profile_storage_rules() -> None:
    profile = AuthProfile(name="max-privat", kind="claude_oauth")
    assert profile.keychain is None
    codex = AuthProfile(name="chatgpt-privat", kind=AuthKind.CODEX_CHATGPT, login_dir="x")
    assert codex.login_dir == "x"
    with pytest.raises(ValueError, match="Login-Ordner"):
        AuthProfile(name="c", kind="codex_chatgpt", keychain={"account": "c"})
    with pytest.raises(ValueError, match="login_dir"):
        AuthProfile(name="c", kind="claude_oauth", login_dir="x")


def test_profiles_file_unique_names() -> None:
    profile = {"name": "a", "kind": "claude_oauth"}
    with pytest.raises(ValueError, match="doppelt"):
        ProfilesFile.model_validate({"profiles": [profile, profile]})


def test_status_enums() -> None:
    assert RunStatus.AWAITING_APPROVAL.is_active
    assert {s for s in RunStatus if s.is_active} == ACTIVE_RUN_STATUSES
    assert {s for s in RunStatus if s.is_final} == {"done", "stopped", "failed", "interrupted"}
    assert not RunStatus.DRAFT.is_active and not RunStatus.DRAFT.is_final
    assert [s.value for s in AgentStatus] == ["working", "idle", "done", "blocked"]
    assert Starter.USER_AGENT == "user_agent"
    assert (Provider.CLAUDE.vendor, Provider.CODEX.vendor) == ("anthropic", "openai")

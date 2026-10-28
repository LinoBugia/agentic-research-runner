"""Translation SandboxProfile → Codex launch settings (no SDK, no model)."""

import tomllib
from pathlib import Path

import pytest

from arr.backends.codex_config import (
    PERMISSION_PROFILE,
    ROOM_SERVER,
    SECRET_ENV,
    build_launch,
    filesystem_entries,
    isolation_overrides,
    toml_value,
)
from arr.models import AgentConfig, Provider, RunVenvMode, Toolchain
from arr.permissions.policy import PermissionRules, RunPaths, profile_for
from arr.runs.prepare import AgentFile, PreparedAgent


def agent_config(**changes: object) -> AgentConfig:
    data: dict[str, object] = {
        "id": "gpt-1",
        "base_id": "gpt-1",
        "provider": Provider.CODEX,
        "model": "gpt-6-luna",
        "auth": "chatgpt-test",
        "channels": ["main"],
    }
    data.update(changes)
    return AgentConfig.model_validate(data)


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    wf = home / "wf"
    run = wf / "runs" / "R"
    work = run / "agents" / "gpt-1" / "work"
    for folder in (work, run / "tmp", home / "readdir", home / ".ssh"):
        folder.mkdir(parents=True)
    (run / "agents" / "gpt-1" / "role.md").write_text("Du bist gpt-1.\n")
    return {"home": home, "wf": wf, "run": run, "work": work}


def setup(layout: dict[str, Path], **changes: object):
    changes.setdefault("read_dirs", [str(layout["home"] / "readdir")])
    config = agent_config(**changes)
    paths = RunPaths(layout["wf"], layout["run"], Toolchain())
    profile = profile_for(
        config, paths, PermissionRules(), home=layout["home"], case_insensitive=False
    )
    agent = PreparedAgent(
        AgentFile(
            config=config,
            work_dir="agents/gpt-1/work",
            role_file="agents/gpt-1/role.md",
            context_file="agents/gpt-1/context.md",
            path_prepend=["/opt/base/bin"],
        ),
        layout["run"],
    )
    return agent, profile, paths


def test_entries_follow_profile(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run, home = layout["run"], layout["home"]
    assert entries[":root"] == "read"
    assert entries[str(layout["work"])] == "write"
    assert entries[str(run / "tmp")] == "write"
    assert entries[str(run / ".venv")] == "write"
    assert entries[str(home / "readdir")] == "read"
    assert entries[str(run / "output")] == "read"
    assert entries[str(home)] == "deny"
    assert entries[str(layout["wf"])] == "deny"
    assert entries[str(home / ".ssh")] == "deny"
    assert entries["/Volumes"] == "deny"


def test_skills_snapshot_and_plugin_denied(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths)
    run = layout["run"]
    assert profile.can_read(run / "skills")  # the SandboxProfile allows it …
    assert entries[str(run / "skills")] == "deny"  # … Codex agents do not need it (L14)
    assert entries[str(run / "agents" / "gpt-1" / "plugin")] == "deny"


def test_only_own_channel_attachments(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main", "review"])
    att = layout["run"] / "groupchats" / "attachments"
    assert entries[str(att)] == "deny"
    assert entries[str(att / "main")] == "read"
    assert entries[str(att / "review")] == "read"
    assert str(att / "other") not in entries


def test_broad_read_dir_does_not_reopen_skills(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, read_dirs=[str(layout["run"])])
    entries = filesystem_entries(profile, paths, ["main"])
    assert entries[str(layout["run"])] == "read"
    assert entries[str(layout["run"] / "skills")] == "deny"
    assert entries[str(layout["run"] / "groupchats" / "attachments")] == "deny"


def test_sensitive_wins_over_read_dir(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, read_dirs=[str(layout["home"])])
    entries = filesystem_entries(profile, paths)
    assert entries[str(layout["home"])] == "deny"  # tie: deny wins (SandboxProfile)
    assert entries[str(layout["home"] / ".ssh")] == "deny"


def test_overrides_parse_as_toml(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    overrides = isolation_overrides(profile, paths, model="gpt-6-luna", channels=["main"])
    parsed: dict[str, object] = {}
    for item in overrides:
        key, _, value = item.partition("=")
        parsed[key] = tomllib.loads(f"v = {value}")["v"]
    assert parsed["default_permissions"] == PERMISSION_PROFILE
    fs = parsed[f"permissions.{PERMISSION_PROFILE}.filesystem"]
    assert fs == filesystem_entries(profile, paths, ["main"])
    assert parsed["notify"] == []
    assert parsed["project_root_markers"] == []
    assert parsed["skills.bundled.enabled"] is False
    assert parsed["web_search"] == "disabled"
    assert parsed["features.plugins"] is False
    assert "features.code_mode_host" not in parsed  # would remove the shell
    assert not any(k.startswith("mcp_servers") for k in parsed)
    assert not any("auto_review" in str(v) for v in parsed.values())


def test_web_search_follows_tools_web(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, tools={"web": True})
    overrides = isolation_overrides(profile, paths, model="m")
    assert 'web_search="live"' in overrides


def test_toml_value_escapes() -> None:
    value = {'a "b"\\': ["x", True, 3]}
    assert tomllib.loads(f"v = {toml_value(value)}")["v"] == value


def test_launch_env_and_thread_config(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    environ = {"PATH": "/usr/bin:/opt/base/bin", "OPENAI_API_KEY": "sk-not-real-000000000000"}
    launch = build_launch(agent, profile, paths, codex_home=tmp_path / "ch", environ=environ)
    run = layout["run"]
    env = launch.env
    assert env["CODEX_HOME"] == str(tmp_path / "ch")
    assert env["TMPDIR"] == str(run / "tmp")
    assert env["HOME"] == str(run / "tmp" / "home" / "gpt-1")
    assert env["PATH"].split(":") == [str(run / ".venv/bin"), "/opt/base/bin", "/usr/bin"]
    assert env["OPENAI_API_KEY"] == ""
    assert all(env[name] == "" for name in SECRET_ENV if name in environ)
    assert env["MPLCONFIGDIR"].startswith(str(run / "tmp"))
    room = launch.thread_config["mcp_servers"][ROOM_SERVER]
    assert room["args"] == ["-m", "arr.room.server"]
    assert room["env"] == {"AGENT_ID": "gpt-1", "RUN_DIR": str(run)}
    assert room["default_tools_approval_mode"] == "approve"
    assert launch.cwd == str(layout["work"])
    assert launch.developer_instructions.startswith("Du bist gpt-1.")
    assert "Arbeitsumgebung (arr)" in launch.developer_instructions
    assert not launch.network_access


def test_launch_without_run_venv(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    file = agent.file.model_copy(update={"run_venv": RunVenvMode.OFF})
    agent = PreparedAgent(file, agent.run_dir)
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ={"PATH": ""})
    assert launch.env["PATH"] == "/opt/base/bin"

"""The shell environment of a Codex agent, checked with the real app-server (W2-02.1 H1).

No model and no login: the app-server's `command/exec` runs `/usr/bin/env` with the same
environment policy and sandbox as the agent's commands. Only variable names are compared,
never values. macOS only (Seatbelt, the Codex binary of the wheel).
"""

import asyncio
import sys
from pathlib import Path

import pytest

from arr.backends.codex_config import build_launch, isolation_overrides
from arr.models import AgentConfig, Provider
from arr.permissions import policy
from arr.permissions.policy import PermissionRules, RunPaths, profile_for
from arr.runs.prepare import AgentFile, PreparedAgent

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt sandbox, macOS only"),
]

# Dummy values; what matters is whether the names reach the command.
INHERITED = {
    "ARR_PROBE_TOKEN": "x",
    "GITHUB_TOKEN": "x",
    "MY_SECRET": "x",
    "SOME_API_KEY": "x",
    "AWS_ACCESS_KEY_ID": "x",
    "AWS_SECRET_ACCESS_KEY": "x",
    "CLAUDE_CODE_USER_EMAIL": "x",
    "CLAUDE_CODE_ACCOUNT_UUID": "x",
    "CLAUDE_CODE_MESSAGING_TOKEN": "x",
}


def _forbidden(name: str) -> bool:
    upper = name.upper()
    return (
        any(word in upper for word in ("TOKEN", "SECRET", "KEY"))
        or upper.startswith(("AWS_", "CLAUDE_CODE_"))
        or upper == "CODEX_HOME"
    )


def _launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])
    run = tmp_path / "wf" / "runs" / "R"
    work = run / "agents" / "gpt-1" / "work"
    work.mkdir(parents=True)
    (run / "tmp").mkdir()
    (run / "agents" / "gpt-1" / "role.md").write_text("Du bist gpt-1.\n")
    config = AgentConfig.model_validate(
        {
            "id": "gpt-1",
            "base_id": "gpt-1",
            "provider": Provider.CODEX,
            "model": "m",
            "auth": "chatgpt-test",
        }  # fmt: skip
    )
    paths = RunPaths(tmp_path / "wf", run)
    profile = profile_for(config, paths, PermissionRules(), home=tmp_path, case_insensitive=False)
    file = AgentFile(
        config=config,
        work_dir="agents/gpt-1/work",
        role_file="agents/gpt-1/role.md",
        context_file="agents/gpt-1/context.md",
    )
    home = tmp_path / "codex_home"
    home.mkdir()
    launch = build_launch(PreparedAgent(file, run), profile, paths, codex_home=home)
    return launch, profile, paths


async def _env_names(env: dict[str, str], overrides: tuple[str, ...], cwd: str) -> set[str]:
    from openai_codex import AsyncCodex, CodexConfig
    from openai_codex.generated.v2_all import CommandExecResponse

    codex = AsyncCodex(CodexConfig(env=env, config_overrides=overrides))
    async with codex:
        result = await codex._client.request(
            "command/exec",
            {"command": ["/usr/bin/env"], "cwd": cwd, "timeoutMs": 20000},
            response_model=CommandExecResponse,
        )
    assert result.exit_code == 0, result.stderr
    return {line.split("=", 1)[0] for line in result.stdout.splitlines() if "=" in line}


def test_shell_gets_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch, _, _ = _launch(tmp_path, monkeypatch)
    names = asyncio.run(
        _env_names({**INHERITED, **launch.env}, launch.config_overrides, launch.cwd)
    )
    assert not {n for n in names if _forbidden(n)}
    assert {"HOME", "PATH", "TMPDIR", "VIRTUAL_ENV", "UV_NO_CONFIG"} <= names


def test_without_the_policy_the_secrets_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-check: the state before W2-02.1 (no `shell_environment_policy`)."""
    launch, profile, paths = _launch(tmp_path, monkeypatch)
    old = tuple(o for o in launch.config_overrides if not o.startswith("shell_environment_policy"))
    assert old != launch.config_overrides
    names = asyncio.run(_env_names({**INHERITED, **launch.env}, old, launch.cwd))
    assert {"ARR_PROBE_TOKEN", "AWS_ACCESS_KEY_ID", "CODEX_HOME"} <= names
    assert isolation_overrides(profile, paths, model="m") != old  # the policy is the default

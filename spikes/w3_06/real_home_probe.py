"""The program of the live probe in both real sandboxes, with the real `HOME`, without a model.

No request leaves the Mac: Codex runs through `codex sandbox`, Claude through the real CLI
against the local fake server. The rules of the sandbox are the ones of a real agent of this
user. Only exit codes and counts are printed. A control outside the sandbox shows that the
targets exist and that a code other than 0 means something.

    uv run python spikes/w3_06/real_home_probe.py
"""

import asyncio
import dataclasses
import json
import os
import pwd
import subprocess
import sys
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path[:0] = [str(Path(__file__).parents[2] / "tests"), str(Path(__file__).parent)]

import login_guard_kit as kit  # noqa: E402
from claude_agent_sdk import ClaudeSDKClient  # noqa: E402
from claude_agent_sdk.types import PermissionResultAllow  # noqa: E402
from fake_messages_api import FakeMessagesApi  # noqa: E402
from live_probe import PROGRAM  # noqa: E402

from arr.auth import CLAUDE_SWITCHES  # noqa: E402
from arr.backends.claude_options import build_options, remove_tmp_link, tmp_link  # noqa: E402
from arr.permissions.policy import PermissionRules, profile_for  # noqa: E402

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def stamp(folder: Path) -> list[tuple[str, int]]:
    """Names and times of the files directly in `folder`, never a content."""
    if not folder.is_dir():
        return []
    return sorted((p.name, p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file())


def real_lab(lab: kit.Lab) -> kit.Lab:
    profile = profile_for(lab.agent.file.config, lab.run_paths, PermissionRules(), home=HOME)
    return dataclasses.replace(lab, profile=profile)


def control(folder: Path) -> dict[str, Any]:
    (folder / "probe.py").write_text(PROGRAM)
    subprocess.run([sys.executable, "probe.py"], cwd=folder, check=True, capture_output=True)
    return json.loads((folder / "result.json").read_text())


def in_codex() -> dict[str, Any]:
    with kit.open_lab("codex") as plain:
        lab = real_lab(plain)
        (lab.agent.work_dir / "probe.py").write_text(PROGRAM)
        entries, env, cwd = kit.codex_entries(lab)
        done = kit.in_codex_sandbox(lab, entries, env, cwd, "python3 probe.py")
        assert done.returncode == 0, done.stderr[-300:]
        return json.loads((lab.agent.work_dir / "result.json").read_text())


async def _turn(lab: kit.Lab, api: FakeMessagesApi) -> None:
    async def allow(_tool: str, tool_input: dict[str, Any], _context: Any) -> PermissionResultAllow:
        return PermissionResultAllow(updated_input=tool_input)

    link = tmp_link(lab.profile.tmp_dir, f"{lab.run.run_id}/{lab.agent.id}")
    env = dict(CLAUDE_SWITCHES) | kit.fake_model_env(api.url)
    env |= {
        "HOME": str(HOME),
        "CLAUDE_CONFIG_DIR": str(lab.home / "claude-config"),
        "ANTHROPIC_API_KEY": "marker-not-a-key",
    }
    options = build_options(
        lab.agent, lab.profile, env=env, can_use_tool=allow, hooks=None, tmpdir=link
    )
    client = ClaudeSDKClient(options=replace(options, max_budget_usd=None))
    try:
        await client.connect()
        await client.query("x")
        async for _ in client.receive_response():
            pass
        await client.disconnect()
    finally:
        remove_tmp_link(link, lab.profile.tmp_dir)


def in_claude() -> dict[str, Any]:
    with kit.open_lab("claude") as plain:
        lab = real_lab(plain)
        (lab.agent.work_dir / "probe.py").write_text(PROGRAM)
        with FakeMessagesApi([kit.bash("python3 probe.py")]) as api:
            asyncio.run(asyncio.wait_for(_turn(lab, api), 120))
        assert not any(api.errors), api.errors
        return json.loads((lab.agent.work_dir / "result.json").read_text())


def main() -> None:
    warnings.simplefilter("ignore")
    before = stamp(HOME / ".claude"), stamp(HOME / ".codex")
    folder = Path(os.environ.get("ARR_PROBE_TMP", "/private/tmp")) / f"arr-real-{os.getpid()}"
    folder.mkdir()
    outside = control(folder)
    for name in ("probe.py", "result.json"):
        (folder / name).unlink()
    folder.rmdir()
    table = {"außerhalb": outside, "Codex-Sandbox": in_codex(), "Claude-Sandbox": in_claude()}
    for key in outside:
        print(f"{key:18}", *(f"{name}: {row[key]:<5}" for name, row in table.items()))
    same = before == (stamp(HOME / ".claude"), stamp(HOME / ".codex"))
    print("~/.claude und ~/.codex unverändert:", same)


if __name__ == "__main__":
    main()

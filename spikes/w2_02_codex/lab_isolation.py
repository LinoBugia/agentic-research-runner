"""Offline isolation check (no model turn): what does the agent's app-server load?

Starts the app-server with arr's real launch settings (`arr.backends.codex_config`) in a
throwaway CODEX_HOME and asks it, without any turn: effective config and its layers for the
work folder, skills, MCP server status, permission profiles, and a thread start (instruction
sources, sandbox, approvals). Then runs shell probes with `codex sandbox -P arr` under the
same overrides.

    uv run python spikes/w2_02_codex/lab_isolation.py
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from common import LAB, build_lab, dump, remove_cleanup
from openai_codex import AsyncCodex, CodexConfig
from openai_codex.generated.v2_all import (
    ConfigReadResponse,
    ListMcpServerStatusResponse,
    PermissionProfileListResponse,
    SkillsListResponse,
)

from arr.backends.codex_config import build_launch


async def app_server(lab, launch) -> dict:
    codex = AsyncCodex(CodexConfig(env=launch.env, config_overrides=launch.config_overrides))
    report: dict = {}
    async with codex:
        client = codex._client
        cfg = await client.request(
            "config/read",
            {"cwd": launch.cwd, "includeLayers": True},
            response_model=ConfigReadResponse,
        )
        data = cfg.model_dump(mode="json", by_alias=True)
        report["layers"] = [
            {k: v for k, v in layer.items() if k != "config"} for layer in data.get("layers") or []
        ]
        eff = data.get("config") or {}
        report["effective"] = {
            k: eff.get(k)
            for k in (
                "model",
                "default_permissions",
                "mcp_servers",
                "notify",
                "web_search",
                "project_root_markers",
                "features",
                "skills",
            )
        }
        skills = await client.request(
            "skills/list",
            {"cwds": [launch.cwd], "forceReload": True},
            response_model=SkillsListResponse,
        )
        report["skills"] = [
            {"name": s.name, "scope": str(s.scope.value), "path": str(s.path)}
            for entry in skills.data
            for s in entry.skills
        ]
        profiles = await client.request(
            "permissionProfile/list",
            {"cwd": launch.cwd},
            response_model=PermissionProfileListResponse,
        )
        report["permission_profiles"] = profiles.model_dump(mode="json")["data"]
        started = await client.thread_start(
            {
                "cwd": launch.cwd,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "developerInstructions": launch.developer_instructions,
                "config": launch.thread_config,
                "ephemeral": True,
            }
        )
        st = started.model_dump(mode="json", by_alias=True)
        report["thread_start"] = {
            k: st.get(k)
            for k in (
                "approvalPolicy",
                "approvalsReviewer",
                "cwd",
                "instructionSources",
                "model",
                "sandbox",
                "activePermissionProfile",
            )
        }
        await asyncio.sleep(3)
        mcp = await client.request(
            "mcpServerStatus/list", {}, response_model=ListMcpServerStatusResponse
        )
        report["mcp"] = [
            {"name": s.get("name"), "status": s.get("status") or s.get("startupStatus")}
            for s in mcp.model_dump(mode="json", by_alias=True)["data"]
        ]
    return report


def sandbox_probes(lab, launch) -> list[dict]:
    codex = str(bundled_codex_path())
    overrides = [a for o in launch.config_overrides for a in ("-c", o)]
    env = {**__import__("os").environ, **launch.env}
    c = lab.canaries
    home = Path.home()
    cases = [
        ("L7 work", "cat AGENTS.md >/dev/null && echo ok"),
        ("L8 output dir", f"ls {lab.run.run_dir / 'output'} >/dev/null && echo ok"),
        ("L9 read dir", f"cat {lab.root / 'readdir' / 'paper.md'}"),
        ("L9 write read dir", f"echo x > {lab.root / 'readdir' / 'x.txt'}"),
        ("L1 ~/.ssh exit", f"cat {home}/.ssh/known_hosts >/dev/null 2>&1; echo exit=$?"),
        ("L3 ~/.arr canary", f"python3 -c \"print(open('{c['arr_home']}').read())\""),
        ("L5 other project", f"cat {c['other_project']}"),
        ("outside canary", f"cat {c['outside']}"),
        ("L14 run skills", f"cat {c['run_skills']}"),
        ("L14 own skills", "cat .agents/skills/demo-skill/SKILL.md | head -2"),
        ("L15 foreign att.", f"cat {c['foreign_attachment']}"),
        ("L15 own att.", f"cat {c['own_attachment']}"),
        ("tmp write", "echo x > /tmp/arr-w202-probe && echo WROTE"),
        ("TMPDIR write", 'echo x > "$TMPDIR/probe" && echo ok "$TMPDIR"'),
        ("L13 hardlink", f"ln {c['outside']} hl.txt && cat hl.txt"),
        ("L13 py link", f"python3 -c \"import os; os.link('{c['outside']}', 'hl2.txt')\""),
        ("symlink", f"ln -s {c['outside']} sl.txt && cat sl.txt"),
        ("L6 home search", f"grep -rl CANARY {home} 2>/dev/null | head -3; echo done"),
        ("case ~/.SSH", f"cat {home}/.SSH/known_hosts >/dev/null 2>&1; echo exit=$?"),
        ("sensitive in rd", f"cat {c['sensitive_in_readdir']}"),
        (
            "sensitive CASE",
            f"cat {str(c['sensitive_in_readdir']).replace('.secret', '.SECRET')}",
        ),
        ("L10 sympy", "python3 -c 'import sympy, sys; print(sympy.__version__, sys.prefix)'"),
        ("L10 site-pkgs", "python3 -c 'import sympy; print(open(sympy.__file__).read()[:20])'"),
        ("which python", "command -v python3 pip"),
        ("L11 python ssl", "python3 -c 'import ssl; print(\"ssl ok\")'"),
        ("network", "curl -sS -m 5 -o /dev/null https://example.com; echo exit=$?"),
        ("write original", f"echo x > {lab.workfolder / 'projects' / 'demo' / 'x.txt'}"),
    ]
    results = []
    for label, cmd in cases:
        proc = subprocess.run(
            [codex, *overrides, "sandbox", "-P", "arr", "-C", launch.cwd, "--"]
            + ["/bin/zsh", "-c", cmd],
            env=env,
            capture_output=True,
            text=True,
            cwd=launch.cwd,
        )
        text = (proc.stdout + proc.stderr).strip().replace(str(home), "~")
        results.append({"case": label, "exit": proc.returncode, "out": text[-300:]})
        print(f"{proc.returncode:>3}  {label:<20} {text.splitlines()[-1] if text else ''}")
    probe = Path("/tmp/arr-w202-probe")
    if probe.exists():
        print("!! /tmp probe written")
        probe.unlink()
    return results


def main() -> None:
    lab = build_lab()
    try:
        home = LAB / "codex_home"
        home.mkdir()
        launch = build_launch(lab.agent, lab.profile, lab.run_paths, codex_home=home)
        dump("lab_overrides.json", list(launch.config_overrides))
        report = asyncio.run(app_server(lab, launch))
        print(json.dumps(report, indent=2).replace(str(Path.home()), "~"))
        report["sandbox"] = sandbox_probes(lab, launch)
        dump("lab_isolation.json", report)
    finally:
        remove_cleanup(lab)


if __name__ == "__main__":
    sys.exit(main())

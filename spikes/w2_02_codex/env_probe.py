"""Offline (no model): the agent's shell environment and toolchain under the real app-server.

Runs commands with `command/exec` in the lab's working folder, with the adapter's overrides
(`shell_environment_policy` core + set, permission profile). Prints variable **names** only.

    uv run python spikes/w2_02_codex/env_probe.py
"""

import asyncio
import os
from pathlib import Path

from common import build_lab, remove_cleanup
from openai_codex import AsyncCodex, CodexConfig
from openai_codex.generated.v2_all import CommandExecResponse

from arr.backends.codex_config import build_launch

TOOLS = [
    ("python3 sympy", ["python3", "-c", "import sympy; print(sympy.__version__)"]),
    ("pip list", ["python3", "-m", "pip", "list", "--disable-pip-version-check"]),
    ("uv pip list", ["uv", "pip", "list"]),
    ("git --version", ["git", "--version"]),
    ("pdflatex --version", ["pdflatex", "--version"]),
    ("tempfile", ["python3", "-c", "import tempfile; print(tempfile.mkstemp()[1])"]),
]


async def main() -> None:
    lab = build_lab()
    try:
        home = Path(__file__).parent / "tmp" / "env_home"
        home.mkdir(parents=True, exist_ok=True)
        launch = build_launch(lab.agent, lab.profile, lab.run_paths, codex_home=home)
        codex = AsyncCodex(CodexConfig(env=launch.env, config_overrides=launch.config_overrides))
        async with codex:

            async def run(argv: list[str]) -> CommandExecResponse:
                return await codex._client.request(
                    "command/exec",
                    {"command": argv, "cwd": launch.cwd, "timeoutMs": 60000},
                    response_model=CommandExecResponse,
                )

            env = await run(["/usr/bin/env"])
            names = sorted(line.split("=", 1)[0] for line in env.stdout.splitlines() if "=" in line)
            print("Variablen beim Befehl:", ", ".join(names))
            print("Variablen des arr-Prozesses:", len(os.environ), "→ beim Befehl:", len(names))
            for label, argv in TOOLS:
                result = await run(argv)
                last = (result.stdout or result.stderr).strip().splitlines()[-1:]
                text = " ".join(last).replace(str(Path.home()), "~")[:80]
                print(f"  {result.exit_code:>3}  {label:<20} {text}")
    finally:
        remove_cleanup(lab)


if __name__ == "__main__":
    asyncio.run(main())

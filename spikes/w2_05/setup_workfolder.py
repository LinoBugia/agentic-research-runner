"""Create the test workfolder for the M1 live run of W2-05 outside the repo (throwaway code).

    uv run python spikes/w2_05/setup_workfolder.py ~/arr-m1 [--python ~/.venvs/mathe]
        [--claude-auth max] [--codex-auth chatgpt-w2-02a]

- `arr init <ziel>`, then `arr.yaml` with the defaults per provider (model, auth profile,
  effort), limits, the agents' Python environment (`toolchain.python`) and `approvals`.
- Copies the project `m1/project` to `<ziel>/projects/collatz-mini` and the scenario
  (`run.yaml`, `task.md`) to `<ziel>/m1/`.

Profiles (`~/.arr/profiles.yaml`) are not touched; the names are only written into `arr.yaml`.
Never imported by arr.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from arr.workfolder import init_workfolder

SPIKE = Path(__file__).resolve().parent
SCENARIO = SPIKE / "m1"

ARR_YAML = """\
# Test-Workfolder für den M1-Live-Lauf (W2-05). Keine Secrets hier.
version: 1
defaults:
  claude: {{ auth: {claude_auth}, model: claude-haiku-5-5, effort: low }}
  codex:  {{ auth: {codex_auth}, model: gpt-6-luna, effort: low }}
concurrency:
  {claude_auth}: 3
  {codex_auth}: 2
limits:
  wall_time: 20m
  turns_per_agent: 6
  budget_usd: 0.5
user_agent_runs:
  require_approval: true
toolchain:
  python: {python}
  run_venv: overlay
approvals:
  decider: user
  timeout: 10m
retention:
  slim_after_days: 14
  archive_after_days: 90
  max_runs_size_gb: 50
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", help="Ordner des Test-Workfolders, z. B. ~/arr-m1")
    parser.add_argument("--python", default="~/.venvs/mathe", help="Basis-venv der Agenten")
    parser.add_argument("--claude-auth", default="max", help="Auth-Profil für Claude")
    parser.add_argument("--codex-auth", default="chatgpt-w2-02a", help="Auth-Profil für Codex")
    args = parser.parse_args()

    python = Path(args.python).expanduser()
    if not (python / "bin" / "python").exists():
        print(f"Die Basis-venv {args.python} fehlt (python3.12 -m venv …; pip install sympy).")
        return 1
    root = init_workfolder(Path(args.target).expanduser())
    (root / "arr.yaml").write_text(
        ARR_YAML.format(
            claude_auth=args.claude_auth, codex_auth=args.codex_auth, python=args.python
        ),
        encoding="utf-8",
    )
    project = root / "projects" / "collatz-mini"
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(SCENARIO / "project", project)
    scenario = root / "m1"
    scenario.mkdir(exist_ok=True)
    for name in ("run.yaml", "task.md"):
        shutil.copy(SCENARIO / name, scenario / name)
    print(f"Workfolder bereit: {root}")
    print("Start des M1-Laufs:")
    print(f"  cd {root} && uv run --project <repo> arr run m1/run.yaml")
    print(f"Prüfung danach: uv run python spikes/w2_05/check_m1.py {root}/runs/<run-id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())

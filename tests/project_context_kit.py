"""The project context (`agents/<id>/context.md`) of the four start skills, for the comparison
with `main` (W4b-06b §6.3, A7).

`start_skill_contexts(root)` prepares one run of every start skill in a fresh workfolder
(`prepare_run` only, no turn) and returns the context text of every agent. W4b-06b wrote
`tests/fixtures/project_context/main.json` once with the code of `main` (`9a7fccc`, before the
removal); `test_texts_one_version.py` compares the code of today with it, character for
character (the workfolder in the text is `@@WORKFOLDER@@`).
"""

import json
from datetime import datetime
from pathlib import Path

from arr.models import RunInput, WorkfolderSettings
from arr.resolve import load_group_spec, resolve_run
from arr.runs.prepare import prepare_run
from arr.workfolder import init_workfolder

MAIN = Path(__file__).parent / "fixtures" / "project_context" / "main.json"
SKILLS = ("proof-review", "proof", "exploration", "roundtable")
SETTINGS = {
    "toolchain": {"run_venv": "off"},
    "defaults": {
        "claude": {"auth": "fake-auth", "model": "fake-model"},
        "codex": {"auth": "fake-codex", "model": "fake-gpt"},
    },
}


def start_skill_contexts(root: Path) -> dict[str, str]:
    """`<skill>/<agent>` → the agent's `context.md`, with the workfolder as `@@WORKFOLDER@@`."""
    workfolder = init_workfolder(root / "wf")
    project = workfolder / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "notes.md").write_text("# Notizen\nLemma A gilt für n ≡ 1 mod 4.\n", "utf-8")
    (project / "CLAUDE.md").write_text("# Projekt demo\n\nZahlentheorie.\n", "utf-8")
    settings = WorkfolderSettings.model_validate(SETTINGS)
    out: dict[str, str] = {}
    for number, skill in enumerate(SKILLS, 1):
        spec = load_group_spec(workfolder / "skills" / skill / "group.yaml")
        run_input = RunInput(project="demo", skill=skill, task="Zeige Lemma A.")
        resolved = resolve_run(run_input, spec, settings)
        prepared = prepare_run(
            workfolder,
            run_input,
            resolved,
            settings,
            run_id=f"20261123-1200_demo_{skill}_{number:04x}",
            now=datetime(2026, 11, 23, 12, 0),
        )
        for agent_id, agent in prepared.agents.items():
            text = (agent.agent_dir / "context.md").read_text("utf-8")
            out[f"{skill}/{agent_id}"] = text.replace(str(workfolder), "@@WORKFOLDER@@")
    return out


if __name__ == "__main__":  # write the file of `main`: uv run python tests/project_context_kit.py
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        found = start_skill_contexts(Path(tmp))
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else MAIN
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(found, ensure_ascii=False, indent=1, sort_keys=True) + "\n")

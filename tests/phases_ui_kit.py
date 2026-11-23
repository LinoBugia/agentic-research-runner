"""Helpers for the tests of the GUI for phases (W4b-07) and a workfolder for the UX review.

- `install_skill`: a group of `tests/fixtures/phases/` (or own text) as a skill `fx-<name>`
  with roles and task files, as `phases_kit.fixture_manager` writes it.
- `real_api`: the service's `RealApi` over a workfolder (FakeBackend profiles, no keychain).
- As a script: `uv run python tests/phases_ui_kit.py <folder>` builds a workfolder in
  `<folder>` with the fixture skills, finished phases runs (`beispiel`, `review`,
  `waterfall`), an old run for comparison and the files of a run the service starts and that
  then waits for you (`live_scenario`). It prints the command that starts the service.
"""

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from phases_kit import FIXTURES, task_files  # noqa: E402

from arr.resolve import parse_yaml_text  # noqa: E402

AUTH = "fake-auth"


def install_skill(workfolder: Path, name: str, text: str | None = None) -> Path:
    """`tests/fixtures/phases/<name>.yaml` (or `text`) as skill `fx-<name>` with every role and
    task file; returns the skill folder."""
    text = text if text is not None else (FIXTURES / f"{name}.yaml").read_text("utf-8")
    data, _lines = parse_yaml_text(text, "group.yaml")
    skill = workfolder / "skills" / f"fx-{name}"
    if skill.exists():
        shutil.rmtree(skill)
    skill.mkdir(parents=True)
    (skill / "group.yaml").write_text(text, encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: fx-{name}\ndescription: Testgruppe {name}\n---\nTest.\n", encoding="utf-8"
    )
    for agent in data.get("agents") or []:
        for tool in agent.get("skills") or []:
            stub = workfolder / "skills" / tool
            if not stub.exists():
                stub.mkdir(parents=True)
                (stub / "SKILL.md").write_text(
                    f"---\nname: {tool}\ndescription: Stub {tool}\n---\nStub.\n", encoding="utf-8"
                )
        if agent.get("role"):
            role = skill / agent["role"]
            role.parent.mkdir(parents=True, exist_ok=True)
            role.write_text(f"Rolle {agent['id']}.\n", encoding="utf-8")
    for rel in task_files(data):
        path = skill / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Auftrag aus {rel} (Phase-Test).\n", encoding="utf-8")
    return skill


def write_fake_profiles(arr_home: Path) -> None:
    """Auth profiles of the FakeBackend agents (`claude`/`codex` defaults of the fixtures)."""
    from run_kit import write_profiles

    write_profiles(
        arr_home, {AUTH: "claude_oauth", "max": "claude_oauth", "chatgpt": "codex_chatgpt"}
    )


def real_api(workfolder: Path) -> Any:
    """`RealApi` over `workfolder` without a scan (the test reads what it needs)."""
    from arr.service.real_api import RealApi

    return RealApi(workfolder, base_url="http://127.0.0.1:8765", scan=False)


# --------------------------------------------------------------------------------------------
# Workfolder for the UX review of the user
# --------------------------------------------------------------------------------------------


async def _runs(workfolder: Path) -> list[str]:
    """A finished `beispiel` run (gate `user`: once „Noch ein Versuch“ with a hint, then
    „Weiter“), a finished `review` run (revision over `goto`) and a finished `waterfall` run
    (retry with scores)."""
    from phases_kit import Choose, TurnView, answer, fixture_manager, hand_in, run, say

    from arr.permissions.types import EscalationOption

    def asker(view: TurnView) -> list[dict[str, Any]]:
        if view.turn == 1:
            return [
                say(
                    f"user.{view.agent}",
                    "@user Darf ich annehmen, dass n prim ist?",
                    kind="question",
                ),
                *hand_in(view, note_next="privat"),
            ]
        return hand_in(view)

    agents = ["ideator", "explorer-1", "explorer-2", "strategist", "judge-1", "judge-2", "writer"]
    policies: dict[str, Any] = {a: answer() for a in agents}
    policies["prover"] = asker
    decider = Choose((EscalationOption.RETRY, "Bitte mehr Ideen."), EscalationOption.CONTINUE)
    mgr, _ = fixture_manager(workfolder, "beispiel", policies, decider=decider)
    await run(mgr)
    out = [mgr.run_dir.name]

    def judge(view: TurnView) -> list[dict[str, Any]]:
        verdict = "minor" if view.stage == "diskussion" and view.attempt == 1 else "accept"
        return hand_in(view, verdict=verdict, objections=[{"text": "Lücke in Schritt 2"}])

    mgr, _ = fixture_manager(
        workfolder, "review", {"author": answer(), "judge-1": judge, "judge-2": judge}
    )
    await run(mgr)
    out.append(mgr.run_dir.name)
    scores = {("strategie", 1): 8, ("skizze", 1): 6, ("skizze", 2): 5, ("beweis", 1): 9}

    def score(view: TurnView, ref: int) -> float:
        return scores.get((view.stage, view.attempt), 8)

    raters = {f"rater-{i}": answer(score=score) for i in (1, 2)}
    mgr, _ = fixture_manager(
        workfolder, "waterfall", {"strategist": answer(), "prover": answer(), **raters}
    )
    await run(mgr)
    out.append(mgr.run_dir.name)
    # an old run (topology `solo`, written by `main` before W4b-06b) to compare with
    from old_runs_kit import SOLO, restore

    out.append(restore(workfolder, SOLO).name)
    return out


def _room(action: str, **args: Any) -> dict[str, Any]:
    return {"do": "room", "action": action, "args": args}


LIVE_ITEMS = [
    {"title": "Idee 1", "text": "Zwei-Quadrate-Satz über Gaußsche Zahlen"},
    {"title": "Idee 2", "text": "Abstieg nach Fermat"},
]


def live_scenario() -> tuple[dict[str, Any], dict[str, Any]]:
    """FakeBackend scripts and the run input of a run the service starts and that then waits
    for you: a tightened request (`decider: user` at `ideator`), a question in the private
    channel, a gate `user` (twice: „Noch ein Versuch“ with a hint, then „Weiter“), submissions
    with agent text that tries to be HTML, an isolated phase with reviews."""
    done = {"steps": [{"do": "text", "text": "fertig"}]}
    scripts: dict[str, Any] = {
        "ideator": {
            "asks": [
                {
                    "tool": "Bash",
                    "command": "curl -s https://arxiv.org/abs/math/0101001",
                    "hosts": ["arxiv.org"],
                    "categories": ["network"],
                    "reason": "Ich will eine Quelle nachlesen.",
                }
            ],
            "turns": [
                {
                    "steps": [
                        _room(
                            "post_message",
                            channel="user.ideator",
                            body="@user Soll ich auch Ansätze ohne Primzahlen aufnehmen?",
                            kind="question",
                        ),
                        _room(
                            "submit",
                            status="done",
                            summary="Zwei Ideen",
                            fields={"items": LIVE_ITEMS},
                        ),
                    ]
                },
                {
                    "steps": [
                        _room(
                            "submit",
                            status="done",
                            summary="Zwei Ideen, geschärft",
                            fields={"items": LIVE_ITEMS},
                        )
                    ]
                },
                done,
            ],
        },
        "prover": {
            "turns": [
                {
                    "steps": [
                        _room(
                            "submit",
                            status="done",
                            summary="<img src=x onerror=alert(1)> Beweisskizze\u2028zweite Zeile",
                            body="# Beweis\n\nSei $p \\equiv 1 \\pmod 4$ …",
                            note_next="Diese Notiz ist privat und erscheint nie.",
                        )
                    ]
                },
                done,
            ]
        },
    }
    for judge in ("judge-1", "judge-2"):
        review = {"verdict": "minor", "objections": [{"text": "<script>x</script> Lücke"}]}
        scripts[judge] = {
            "turns": [
                {
                    "steps": [
                        _room(
                            "submit", status="done", summary=f"Gutachten von {judge}", fields=review
                        )
                    ]
                },
                done,
            ]
        }
    run = {
        "project": "demo",
        "name": "Phasen-Probe",
        "task": "Zeige den Zwei-Quadrate-Satz.",
        "topology": {"kind": "phases"},
        "defaults": {"provider": "claude", "model": "fake-model", "auth": AUTH},
        "agents": [
            {"id": "ideator", "decider": "user"},
            {"id": "prover"},
            {"id": "judge", "count": 2},
        ],
        "phases": [
            {
                "name": "ideen",
                "topology": "solo",
                "actions": {
                    "ideator": {
                        "task": "tasks/ideen.md",
                        "turns": 1,
                        "submission": {"format": "list", "name": "ideen"},
                    }
                },
                "gate": {"type": "user"},
            },
            {
                "name": "beweis",
                "topology": "solo",
                "actions": {
                    "prover": {
                        "task": "tasks/beweis.md",
                        "turns": 1,
                        "submission": {"format": "text"},
                    }
                },
            },
            {
                "name": "pruefung",
                "topology": "parallel",
                "isolated": True,
                "actions": {
                    "judge-*": {
                        "task": "tasks/pruefen.md",
                        "turns": 1,
                        "submission": {"format": "review"},
                    }
                },
            },
        ],
    }
    return scripts, run


def build(folder: Path) -> Path:
    """The workfolder for the UX review in `folder` (see the module docstring), with `home/`
    (`ARR_HOME` with the FakeBackend profiles) and `scripts.json`, `autostart.json` for the run
    the service starts and that waits for you."""
    import json

    from service_kit import make_workfolder

    folder.mkdir(parents=True, exist_ok=True)
    workfolder = make_workfolder(folder)
    for name in ("beispiel", "review", "waterfall", "exploration", "roundtable", "solo"):
        install_skill(workfolder, name)
    runs = asyncio.run(_runs(workfolder))
    tasks = workfolder / "projects" / "demo" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    for task in ("ideen", "beweis", "pruefen"):
        (tasks / f"{task}.md").write_text(f"Auftrag {task}.\n", "utf-8")
    write_fake_profiles(folder / "home")
    scripts, run = live_scenario()
    (folder / "scripts.json").write_text(json.dumps(scripts, ensure_ascii=False), "utf-8")
    (folder / "autostart.json").write_text(json.dumps(run, ensure_ascii=False), "utf-8")
    print(f"Workfolder: {workfolder}")
    for run_id in runs:
        print(f"  Run: {run_id}")
    print("Dienst starten (Port 8807, FakeBackend, eigenes ARR_HOME):")
    print(
        f'  ARR_HOME="{folder / "home"}" ARR_TEST_PORT=8807 ARR_TEST_WORKFOLDER="{workfolder}" '
        f'ARR_TEST_SCRIPTS="{folder / "scripts.json"}" '
        f'ARR_TEST_AUTOSTART="{folder / "autostart.json"}" uv run python tests/service_child.py'
    )
    print(f"Adresse mit Token: in {folder / 'home' / 'service.json'} (host, port, token)")
    return workfolder


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Aufruf: uv run python tests/phases_ui_kit.py <ordner>")
    build(Path(sys.argv[1]).expanduser())

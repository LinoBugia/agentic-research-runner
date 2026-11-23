"""W4b-06b F1 (§6.5, A9): a run without a skill is a phases run with the agent `solver` and one
phase `arbeit` (`solo`, submission `text`); its assignment comes from arr's package.

„Neuer Run“ (`RealApi.start_run` with the form) and `start_run` of Claude Code with only a
`task` end `done` with FakeBackend, with the submission in the output folder; `check` finds no
error; a run with a skill never reads the package file. No model.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_kit import make_api, prepare_workfolder, until_final
from run_kit import make_workfolder, room, turns, write_profiles

from arr.models import RunStatus, TopologyKind
from arr.plan.check import check
from arr.runs.phases import tasks as tasks_module
from arr.runs.phases.tasks import NO_SKILL_TASK
from arr.service.real_api.runs import NO_SKILL_PHASE, SOLO_AGENT, form_input
from arr.workfolder import read_template

HAND_IN = turns(
    [room("submit", status="done", summary="Lemma A gilt.", body="Beweis: n = 4k + 1 …")]
)


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return prepare_workfolder(make_workfolder(tmp_path))


def test_the_form_without_a_skill_is_a_phases_run() -> None:
    data, params, _ = form_input({"project": "demo", "task_md": "Zeige Lemma A."})
    assert data["topology"] == {"kind": "phases"} and params == {}
    assert data["agents"] == [{"id": SOLO_AGENT}]
    assert data["phases"] == [
        {
            "name": NO_SKILL_PHASE,
            "topology": "solo",
            "actions": {"solver": {"task": NO_SKILL_TASK, "submission": {"format": "text"}}},
        }
    ]


async def check_done(api: Any, run_id: str, wf: Path) -> Path:
    assert await until_final(api, run_id) is RunStatus.DONE
    run_dir = wf / "runs" / run_id
    [sub] = json.loads((run_dir / "output" / "submissions.json").read_text("utf-8"))["submissions"]
    assert (sub["agent"], sub["stage"], sub["summary"]) == ("solver", "arbeit", "Lemma A gilt.")
    assert (run_dir / sub["path"] / "body.md").read_text("utf-8").startswith("Beweis")
    index = json.loads((run_dir / "tasks" / "index.json").read_text("utf-8"))
    assert [(t["source"], t["found_in"]) for t in index] == [(NO_SKILL_TASK, "arr")]
    package = read_template("run", "ohne-skill.md")
    assert (run_dir / index[0]["file"]).read_text("utf-8").strip() == package.strip()
    return run_dir


async def test_neuer_run_without_a_skill_ends_done(wf: Path) -> None:
    api = make_api(wf, {SOLO_AGENT: HAND_IN})
    form = {"project": "demo", "task_md": "Zeige Lemma A."}
    resolved = await api.resolve_run(form)
    assert resolved.errors == [], resolved.errors
    started = await api.start_run(form)
    run_dir = await check_done(api, started.run_id, wf)
    detail = await api.get_run(started.run_id)
    assert detail.run.topology is TopologyKind.PHASES
    inputs = await api.get_run_inputs(started.run_id)
    assert inputs.name == "ohne Skill · demo"
    assert (run_dir / "output" / "summary.md").is_file()
    await api.close()


async def test_start_run_of_claude_code_with_only_a_task(wf: Path) -> None:
    from arr.permissions.types import DecisionAction
    from arr.service.api_models import DecisionRequest
    from arr.service.user_api import UserApi

    api = make_api(wf, {SOLO_AGENT: HAND_IN})
    users = UserApi(api)
    estimate = await users.estimate_run("demo", task="Zeige Lemma A.")
    assert estimate["topology"] == "phases"
    started = await users.start_run("demo", task="Zeige Lemma A.")
    [item] = await api.list_approvals(run_id=started["run_id"])  # the run start waits for you
    await api.decide_approval(item.id, DecisionRequest(action=DecisionAction.ALLOW))
    await check_done(api, started["run_id"], wf)
    await api.close()


def test_check_finds_no_error_in_a_run_without_a_skill(wf: Path) -> None:
    from arr.models import RunInput
    from arr.runs.launch import plan_run_input

    data, params, _ = form_input({"project": "demo", "task_md": "Zeige Lemma A."})
    launch = plan_run_input(RunInput.model_validate(data), workfolder=wf, params=params)
    result = check(launch.resolved)
    assert result.errors == [] and result.warnings == []


async def test_a_run_with_a_skill_never_reads_the_package_file(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skill `runde` names `tasks/ohne-skill.md` itself: its own file counts; the package
    file is never read (also not by `check`)."""
    from mcp_kit import GROUP_YAML, group_skill

    read: list[int] = []
    real = tasks_module._no_skill_text

    def counted() -> str:
        read.append(1)
        return real()

    monkeypatch.setattr(tasks_module, "_no_skill_text", counted)
    folder = group_skill(wf, "eigen", GROUP_YAML.replace("tasks/runde.md", NO_SKILL_TASK))
    (folder / NO_SKILL_TASK).write_text("# Eigener Auftrag\n\nDiskutiere.\n", "utf-8")
    scripts = {a: HAND_IN for a in ("m-1", "m-2", "critic")}
    api = make_api(wf, scripts)
    started = await api.start_run(
        {"project": "demo", "skill": "eigen", "task_md": "Zeige Lemma A."}
    )
    assert await until_final(api, started.run_id) is RunStatus.DONE
    index = json.loads((wf / "runs" / started.run_id / "tasks" / "index.json").read_text())
    assert {t["found_in"] for t in index} == {"skills/eigen"}
    assert read == []
    await api.close()

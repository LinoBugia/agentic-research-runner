"""W3-10 V8/V9 on `RealApi`: the inputs of a run and the input that repeats it.

A run started from the form is read back (`get_run_inputs`); its repeat goes through the same
check and start as the form, and its `run.yaml` equals the old one except for the name.
Missing project, skill or profile stay in the repeat and come back as field errors.
"""

import shutil
from pathlib import Path
from typing import Any

import yaml
from service_kit import hold_lock, make_run_folder
from test_gui_runs_real import FORM, make_api, until_final, wf

from arr.models import Decider, RunStatus
from arr.service.api_models import REPEAT_SUFFIX

__all__ = ["wf"]  # the fixture

FAKE_KEY = "sk-ant-api03-W310TESTVALUE0000"
"""Looks like a key to the secret filter; it must never come back."""


def changed_form(**extra: Any) -> dict[str, Any]:
    form = {
        **FORM,
        "name": "Erster Versuch",
        "agents": {"m-2": {"web": True, "read_dirs": ["~/Documents/Literatur"]}},
        "limits": {"wall_time_s": 20 * 60, "budget_usd": 2.0},
        "decider": Decider.USER.value,
    }
    form.update(extra)
    return form


def run_yaml(wf: Path, run_id: str) -> dict[str, Any]:
    return yaml.safe_load((wf / "runs" / run_id / "run.yaml").read_text("utf-8"))


async def started(api: Any, form: dict[str, Any]) -> str:
    run_id = (await api.start_run(form)).run_id
    await until_final(api, run_id)
    return run_id


async def test_inputs_of_a_finished_run(wf: Path) -> None:
    api = make_api(wf)
    try:
        run_id = await started(api, changed_form())
        inputs = await api.get_run_inputs(run_id)
    finally:
        await api.close()
    assert inputs.available and inputs.name == "Erster Versuch"
    assert (inputs.project, inputs.skill, inputs.topology.value) == ("demo", "runde", "roundtable")
    assert inputs.task_md == "# Aufgabe\n\nZwei Ideen prüfen."
    rounds = next(p for p in inputs.params if p.name == "rounds")
    assert (rounds.value, rounds.default, rounds.doc, rounds.changed) == (1, 2, "Runden", True)
    agents = {a.id: a for a in inputs.agents}
    assert set(agents) == {"m-1", "m-2"}
    m2 = agents["m-2"]
    assert (m2.model, m2.profile, m2.effort.value, m2.web) == (
        "fake-model",
        "fake-auth",
        "low",
        True,
    )
    assert m2.read_dirs == ["~/Documents/Literatur"] and m2.channels == ["main"]
    assert agents["m-1"].web is False and agents["m-1"].role_label == "M"
    channel = inputs.channels[0]
    assert (channel.name, channel.members, channel.mode.value) == ("main", ["m-1", "m-2"], "event")
    assert channel.idle_after_s is not None and channel.wake_on
    limits = inputs.limits
    assert (limits.wall_time_s, limits.turns_per_agent, limits.budget_usd) == (1200, 6, 2.0)
    assert inputs.decider is Decider.USER and inputs.approval_timeout_s == 300
    assert inputs.run_yaml == (wf / "runs" / run_id / "run.yaml").read_text("utf-8")


async def test_the_repeat_gives_the_same_run_yaml_but_the_name(wf: Path) -> None:
    api = make_api(wf)
    try:
        first = await started(api, changed_form())
        inputs = await api.get_run_inputs(first)
        repeat = inputs.repeat
        # only what differs from the skill goes as a change of the agents
        assert repeat["agents"] == {"m-2": {"web": True, "read_dirs": ["~/Documents/Literatur"]}}
        assert repeat["name"] == f"Erster Versuch{REPEAT_SUFFIX}"
        assert repeat["params"] == {"rounds": 1} and repeat["decider"] == "user"
        assert inputs.repeat_notes == []
        check = await api.resolve_run(repeat)
        assert check.errors == []
        second = await started(api, repeat)
    finally:
        await api.close()
    old, new = run_yaml(wf, first), run_yaml(wf, second)
    assert new.pop("name") == "Erster Versuch (Wiederholung)"
    assert old.pop("name") == "Erster Versuch"
    assert old == new
    api = make_api(wf)
    try:
        again = (await api.get_run_inputs(second)).repeat
    finally:
        await api.close()
    assert again["name"] == "Erster Versuch (Wiederholung)", "the suffix is not doubled"
    assert (wf / "runs" / second / "task.md").read_text("utf-8") == (
        wf / "runs" / first / "task.md"
    ).read_text("utf-8")


async def test_a_run_without_changes_repeats_without_changes(wf: Path) -> None:
    api = make_api(wf)
    try:
        first = await started(api, {**FORM, "name": ""})
        inputs = await api.get_run_inputs(first)
        second = await started(api, inputs.repeat)
    finally:
        await api.close()
    assert inputs.repeat["agents"] == {}
    assert inputs.repeat["name"] == f"runde · demo{REPEAT_SUFFIX}"
    old, new = run_yaml(wf, first), run_yaml(wf, second)
    assert old.pop("name") is None and new.pop("name") == "runde · demo (Wiederholung)"
    assert old == new


async def test_profile_and_effort_of_the_old_run_win_over_new_standards(wf: Path) -> None:
    api = make_api(wf)
    try:
        first = await started(api, {**FORM})
        arr_yaml = wf / "arr.yaml"
        arr_yaml.write_text(
            arr_yaml.read_text("utf-8").replace("effort: low}", "effort: high}", 1), "utf-8"
        )
        inputs = await api.get_run_inputs(first)
        second = await started(api, inputs.repeat)
    finally:
        await api.close()
    assert inputs.repeat["agents"] == {"m-1": {"effort": "low"}, "m-2": {"effort": "low"}}
    old, new = run_yaml(wf, first), run_yaml(wf, second)
    assert [a["effort"] for a in new["agents"]] == ["low", "low"]
    old.pop("name"), new.pop("name")
    assert old == new


async def test_missing_project_skill_and_profile_become_field_errors(wf: Path) -> None:
    api = make_api(wf)
    try:
        first = await started(api, changed_form())
        shutil.rmtree(wf / "projects" / "demo")
        repeat = (await api.get_run_inputs(first)).repeat
        assert repeat["project"] == "demo", "the repeat keeps what the run had"
        assert set(repeat["agents"]) == {"m-1", "m-2"}, "without a skill to compare: all"
        assert repeat["agents"]["m-1"]["auth"] == "fake-auth"
        paths = {e.path for e in (await api.resolve_run(repeat)).errors}
        assert "project" in paths
        gone_profile = {**repeat, "project": "demo"}
        (wf / "projects" / "demo").mkdir(parents=True)
        (wf / "projects" / "demo" / "CLAUDE.md").write_text("# Demo\n", "utf-8")
        gone_profile["agents"] = {"m-1": {"auth": "gibt-es-nicht"}}
        errors = (await api.resolve_run(gone_profile)).errors
        assert any(e.path == "agents.m-1.profile" for e in errors), errors
        shutil.rmtree(wf / "skills" / "runde")
        errors = (await api.resolve_run(repeat)).errors
        assert errors and any("runde" in e.message for e in errors), errors
    finally:
        await api.close()


async def test_a_secret_in_the_task_never_comes_back(wf: Path) -> None:
    api = make_api(wf)
    try:
        task = f"# Aufgabe\n\nNimm ANTHROPIC_API_KEY={FAKE_KEY} und rechne."
        first = await started(api, changed_form(task_md=task))
        inputs = await api.get_run_inputs(first)
    finally:
        await api.close()
    dumped = inputs.model_dump_json()
    assert FAKE_KEY not in dumped and "W310TESTVALUE" not in dumped
    assert "rechne" in (inputs.task_md or "")
    assert any("Geheimnis" in note for note in inputs.repeat_notes)


async def test_inputs_of_a_stopped_slimmed_and_a_queued_run(wf: Path) -> None:
    stopped = make_run_folder(wf, status=RunStatus.STOPPED, name="Gestoppt")
    (stopped / ".slimmed").write_text("")
    queued = make_run_folder(wf, status=RunStatus.QUEUED)
    lock = hold_lock(queued)
    (queued / "run.yaml").unlink()
    api = make_api(wf)
    try:
        inputs = await api.get_run_inputs(stopped.name)
        assert inputs.available and inputs.name == "Gestoppt" and inputs.agents
        assert inputs.run_yaml is not None
        waiting = await api.get_run_inputs(queued.name)
        assert not waiting.available and waiting.run_yaml is None and waiting.agents == []
    finally:
        lock.release()
        await api.close()


# --- W3-10.1 point 2: no secret anywhere in the inputs ----------------------------------------

HIDDEN = "sk-ant-api03-W3101FIELDS000000000"
HIDDEN_MARK = "W3101FIELDS"


def _edit_run_yaml(run_dir: Path, change: Any) -> None:
    data = yaml.safe_load((run_dir / "run.yaml").read_text("utf-8"))
    change(data)
    (run_dir / "run.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8"
    )


def _secret_fields(data: dict[str, Any]) -> None:
    data["topology"]["params"] = {"token": HIDDEN}
    data["groupchats"][0]["purpose"] = f"Zweck {HIDDEN}"
    data["agents"][0]["read_dirs"] = [f"/tmp/{HIDDEN}"]
    data["agents"][0]["role"] = f"roles/{HIDDEN}.md"


async def test_a_secret_in_any_field_of_the_inputs_never_comes_back(wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.DONE, name=f"Name {HIDDEN}")
    _edit_run_yaml(run_dir, _secret_fields)
    api = make_api(wf)
    try:
        inputs = await api.get_run_inputs(run_dir.name)
    finally:
        await api.close()
    assert inputs.available
    dumped = inputs.model_dump_json()
    assert HIDDEN_MARK not in dumped, "params, purpose, read_dirs, role, name, repeat, run.yaml"
    assert "«entfernt»" in (inputs.run_yaml or ""), "run.yaml is filtered value by value"
    assert "Zweck «entfernt»" in inputs.channels[0].purpose
    assert any("Geheimnis" in note for note in inputs.repeat_notes)

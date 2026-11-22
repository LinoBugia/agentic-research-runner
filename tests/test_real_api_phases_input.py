"""W4b-07 A17 (§6.14): a phases run keeps `phases`, `formats` and `seed` when one agent changes.

`RunsArea._plan` (overrides of „Neuer Run“) and `RunsArea.plan_request` (`overrides.agents`
of Claude Code) write the resolved agents, channels, stages and raters back into the input and
resolve again. The resolved run with a change to one agent must equal the one without it,
except for that agent. The group is the fixture `beispiel.yaml` (agents with `count`, actions
on `judge-*`) with an own format and a seed added here; no model, no profile secret.
"""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from mcp_kit import make_api
from run_kit import write_profiles
from service_kit import make_workfolder

from arr.models import ResolvedRun
from arr.resolve import parse_yaml_text

FIXTURE = Path(__file__).parent / "fixtures" / "phases" / "beispiel.yaml"
SKILL = "fx-beispiel"
TASK = "Zeige, dass jede Primzahl p ≡ 1 mod 4 eine Summe zweier Quadrate ist."
ARR_YAML = """version: 1
toolchain: {run_venv: off}
defaults:
  claude: {model: claude-sonnet-5-5, auth: fake-auth}
  codex: {model: gpt-5.5, auth: fake-codex}
"""
CHANGED = "judge-2"
NEW_MODEL = "gpt-5.5-mini"
FORMAT = {"fields": {"kern": {"type": "text", "max_chars": 400}}}
SEED_FILE = "notes.md"


def group(fmt: str = "notiz", seed: str = "vorlage") -> dict[str, Any]:
    """`beispiel.yaml` with an own format `fmt` (used by the writer) and a seed `seed` (an
    input of the first phase): a fresh copy every time."""
    data, _lines = parse_yaml_text(FIXTURE.read_text(encoding="utf-8"), "beispiel.yaml")
    data = copy.deepcopy(data)
    data["formats"] = {fmt: copy.deepcopy(FORMAT)}
    data["seed"] = {seed: {"file": SEED_FILE}}
    phases = {p["name"]: p for p in data["phases"]}
    phases["ideen"]["inputs"] = [{"name": seed}]
    phases["zusammenfassung"]["actions"]["writer"]["submission"] = {"format": fmt}
    return data


def task_files(data: dict[str, Any]) -> list[str]:
    out = []
    for phase in data["phases"]:
        out += [action["task"] for action in phase["actions"].values()]
        gate = phase.get("gate") or {}
        if isinstance(gate.get("action"), dict):
            out.append(gate["action"]["task"])
    return list(dict.fromkeys(out))


def install(wf: Path, data: dict[str, Any]) -> None:
    """The group as skill `fx-beispiel` with roles and task files (like
    `phases_kit.fixture_manager`)."""
    skill = wf / "skills" / SKILL
    if skill.exists():
        shutil.rmtree(skill)
    skill.mkdir(parents=True)
    (skill / "group.yaml").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: Testgruppe beispiel\n---\nTest.\n", encoding="utf-8"
    )
    for agent in data["agents"]:
        role = skill / agent["role"]
        role.parent.mkdir(parents=True, exist_ok=True)
        role.write_text(f"Rolle {agent['id']}.\n", encoding="utf-8")
    for rel in task_files(data):
        path = skill / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Auftrag aus {rel}.\n", encoding="utf-8")


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text(ARR_YAML, "utf-8")
    install(workfolder, group())
    return workfolder


def plan_fields(resolved: ResolvedRun) -> dict[str, Any]:
    """What the phases runner reads besides the agents."""
    return resolved.model_dump(
        mode="json",
        include={"topology", "phases", "formats", "seed", "groupchats", "stages", "raters"},
    )


def agents_of(resolved: ResolvedRun) -> dict[str, dict[str, Any]]:
    return {a.id: a.model_dump(mode="json") for a in resolved.agents}


def assert_only_the_agent_changed(
    before: ResolvedRun, after: ResolvedRun, fmt: str = "notiz", seed: str = "vorlage"
) -> None:
    # the run really is a phases run with its format, its seed and the count agents in it
    assert [p.name for p in before.phases] == [
        "ideen",
        "exploration",
        "beweis",
        "pruefung",
        "urteil",
        "revision",
        "zusammenfassung",
    ]
    assert list(before.formats) == [fmt] and list(before.seed) == [seed]
    assert before.seed[seed].file == SEED_FILE
    ideen = next(p for p in before.phases if p.name == "ideen")
    assert [i.name for i in ideen.inputs or []] == [seed]
    urteil = next(p for p in before.phases if p.name == "urteil")
    assert [a.agent for a in urteil.actions] == ["judge-1", "judge-2"]
    assert next(p for p in before.phases if p.name == "beweis").decider is not None

    assert plan_fields(after) == plan_fields(before)
    old, new = agents_of(before), agents_of(after)
    assert list(new) == list(old)
    assert new[CHANGED]["model"] == NEW_MODEL != old[CHANGED]["model"]
    assert {**new[CHANGED], "model": old[CHANGED]["model"]} == old[CHANGED]
    assert {k: v for k, v in new.items() if k != CHANGED} == {
        k: v for k, v in old.items() if k != CHANGED
    }


# --------------------------------------------------------------------------------------------
# „Neuer Run“: `_plan` with `overrides` per agent
# --------------------------------------------------------------------------------------------


def form(**agents: Any) -> dict[str, Any]:
    return {"project": "demo", "skill": SKILL, "task_md": TASK, "params": {}, "agents": agents}


def test_new_run_with_a_changed_judge_keeps_phases_formats_and_seed(wf: Path) -> None:
    runs = make_api(wf).runs
    before = runs._plan(form()).resolved
    after = runs._plan(form(**{CHANGED: {"model": NEW_MODEL}})).resolved
    assert_only_the_agent_changed(before, after)


# --------------------------------------------------------------------------------------------
# Claude Code: `plan_request` with `overrides.agents` and an own `config`
# --------------------------------------------------------------------------------------------


def request(config: dict[str, Any] | None, **agents: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "project": "demo",
        "task": TASK,
        "skill": SKILL,
        "params": {},
        "overrides": {"agents": agents} if agents else {},
    }
    if config is not None:
        out["config"] = config
    return out


OWN_FORMAT, OWN_SEED = "eigen", "start"


def own_config() -> dict[str, Any]:
    """A whole group as `config` (JSON object) with the new key `channels` and a format and a
    seed of other names than the skill's: if the round trip lost `phases`, `formats` or
    `seed`, those of the skill would come back and the comparison would see it. The skill
    gives roles and task files."""
    data = group(OWN_FORMAT, OWN_SEED)
    data.pop("kind")
    data.pop("rubric")
    assert "channels" in data and "groupchats" not in data
    return data


def test_claude_code_with_a_changed_judge_keeps_phases_formats_and_seed(wf: Path) -> None:
    runs = make_api(wf).runs
    before = runs.plan_request(request(None)).resolved
    after = runs.plan_request(request(None, **{CHANGED: {"model": NEW_MODEL}})).resolved
    assert_only_the_agent_changed(before, after)


def test_claude_code_with_own_config_and_a_changed_judge_keeps_its_phases(wf: Path) -> None:
    """Before W4b-07 this failed: the `channels` of the config came back as `groupchats`
    next to it (both keys are an error)."""
    runs = make_api(wf).runs
    before = runs.plan_request(request(own_config())).resolved
    after = runs.plan_request(request(own_config(), **{CHANGED: {"model": NEW_MODEL}})).resolved
    assert_only_the_agent_changed(before, after, OWN_FORMAT, OWN_SEED)

"""The five old topologies get one message everywhere (W4b-06b §6.1, A2).

`old_topology_message` (`arr.plan.legacy`) as a `ConfigError` at `topology.kind` (`run.yaml`) or
`topology` (`group.yaml`), in four places: `plan_run_input` before `resolve_run` (`arr run`,
„Neuer Run“, `start_run` and `estimate_run` of Claude Code), the check of a group skill in the
service, `check_topology`/`topology_for`, and the `RunManager`, even with a topology function
of its own. A group whose resolution would fail gets the same message. No model.
"""

from pathlib import Path
from typing import Any

import pytest
from mcp_kit import ARR_YAML, SKILL_MD, make_api
from run_kit import make_manager, make_run_input, make_settings, make_workfolder, write_profiles
from typer.testing import CliRunner

from arr.cli import app
from arr.errors import ArrError, ConfigError
from arr.models import RunInput, TopologyKind
from arr.plan.legacy import old_topology_message
from arr.resolve import resolve_run, validate_model
from arr.runs.launch import plan_launch, plan_run_input
from arr.runs.topology import check_topology, topology_for

OLD = ("solo", "roundtable", "waterfall", "review", "exploration")
UPDATE = (
    "Hast du den Start-Skill nie geändert, bringt „Start-Skills aktualisieren“ auf der "
    "Skills-Seite (oder „arr skills update“) ihn auf die neue Fassung."
)

RUN_YAML = """version: 1
project: demo
task: Prüfe Lemma A.
topology: {{kind: {kind}}}
defaults: {{provider: claude, model: fake-model, auth: fake-auth}}
agents: [{{id: a}}]
"""

BROKEN_GROUP = """kind: research-group
version: 1
topology: {kind}
agents:
  - id: a
    role: roles/fehlt.md
stages:
  - {{name: s, agent: niemand}}
raters: [keiner]
"""
"""An old group whose resolution fails (unknown agents in `stages` and `raters`)."""


def message(kind: str) -> str:
    text = old_topology_message(kind)
    assert text is not None
    return text


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text(ARR_YAML, "utf-8")
    return workfolder


def old_group(wf: Path, kind: str, name: str = "alt") -> Path:
    folder = wf / "skills" / name
    (folder / "roles").mkdir(parents=True)
    (folder / "SKILL.md").write_text(SKILL_MD.format(name=name), "utf-8")
    (folder / "group.yaml").write_text(BROKEN_GROUP.format(kind=kind), "utf-8")
    return folder


def only(exc: ConfigError, path: str, kind: str) -> None:
    assert [(i.path, i.message) for i in exc.issues] == [(path, message(kind))], exc


# --------------------------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", OLD)
def test_the_message_names_the_way_to_update_a_start_skill(kind: str) -> None:
    text = message(kind)
    assert text.startswith(f"Die Topologie „{kind}“ gibt es nicht mehr.")
    assert (UPDATE in text) is (kind != "solo"), "solo has no start skill as template"
    assert text.endswith("Eine automatische Umstellung gibt es nicht.")
    assert old_topology_message("phases") is None


def test_a_missing_topology_suggests_phases() -> None:
    data = {"project": "demo", "task": "x", "agents": [{"id": "a"}]}
    with pytest.raises(ConfigError) as info:
        resolve_run(validate_model(RunInput, data), None, make_settings())
    [issue] = info.value.issues
    assert issue.path == "topology"
    assert issue.message == (
        "Es fehlt „topology“. Gib z. B. „topology: {kind: phases}“ mit einer Liste „phases“ an "
        "oder starte den Run aus einem Group-Skill."
    )


# --------------------------------------------------------------------------------------------
# 1. plan_run_input: arr run, „Neuer Run“, start_run, estimate_run
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", OLD)
def test_arr_run_refuses_an_old_topology_of_the_run_yaml(wf: Path, kind: str) -> None:
    run_file = wf / "run.yaml"
    run_file.write_text(RUN_YAML.format(kind=kind), "utf-8")
    with pytest.raises(ConfigError) as info:
        plan_launch(run_file, cwd=wf)
    only(info.value, "topology.kind", kind)
    assert info.value.source == str(run_file)
    result = CliRunner().invoke(app, ["run", str(run_file)])
    assert result.exit_code != 0
    assert f"Die Topologie „{kind}“ gibt es nicht mehr." in " ".join(result.output.split())
    assert list((wf / "runs").iterdir()) == [], "nothing was prepared"


@pytest.mark.parametrize("kind", OLD)
def test_plan_run_input_refuses_an_old_group_before_its_resolution(wf: Path, kind: str) -> None:
    old_group(wf, kind)
    run_input = validate_model(RunInput, {"project": "demo", "task": "x", "skill": "alt"})
    with pytest.raises(ConfigError) as info:
        plan_run_input(run_input, workfolder=wf)
    only(info.value, "topology", kind)
    assert info.value.source == "group.yaml"


@pytest.mark.parametrize("kind", OLD)
async def test_neuer_run_refuses_an_old_group(wf: Path, kind: str) -> None:
    from arr.service.api_models import ApiException

    old_group(wf, kind)
    api = make_api(wf)
    form = {"project": "demo", "skill": "alt", "task_md": "Prüfe Lemma A."}
    found = await api.resolve_run(form)
    assert [e.message for e in found.errors] == [message(kind)]
    with pytest.raises(ApiException) as info:
        await api.start_run(form)
    assert info.value.error.message == message(kind)
    assert list((wf / "runs").iterdir()) == []


@pytest.mark.parametrize("kind", OLD)
@pytest.mark.parametrize("tool", ["start_run", "estimate_run"])
async def test_claude_code_gets_the_message(wf: Path, kind: str, tool: str) -> None:
    from arr.service.user_api import ToolProblem, UserApi, scrub

    old_group(wf, kind)
    users = UserApi(make_api(wf))
    with pytest.raises(ToolProblem) as info:
        await getattr(users, tool)("demo", skill="alt", task="Prüfe Lemma A.")
    assert message(kind) in str(info.value)
    config = {"topology": {"kind": kind}}
    with pytest.raises(ToolProblem) as info:
        await getattr(users, tool)("demo", task="Prüfe Lemma A.", config=config)
    # values of the arguments are scrubbed from the answer (`scrub`), the kind as well
    masked = scrub(message(kind), config)
    assert f"- topology.kind: {masked}" in str(info.value)
    assert list((wf / "runs").iterdir()) == []


# --------------------------------------------------------------------------------------------
# 2. The check of a group skill in the service
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", OLD)
async def test_the_skills_page_shows_one_message_for_an_old_group(wf: Path, kind: str) -> None:
    old_group(wf, kind)
    info = await make_api(wf).get_skill("alt")
    assert [(i.file, i.field, i.message) for i in info.issues] == [
        ("group.yaml", "topology", message(kind))
    ], "one message, no check of roles, stages or raters"
    assert info.issues[0].line == 3, "the line of `topology:`"


# --------------------------------------------------------------------------------------------
# 3. check_topology and topology_for
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", OLD)
def test_check_topology_and_topology_for_refuse(kind: str) -> None:
    resolved = resolve_run(
        make_run_input([{"id": "a"}], topology={"kind": kind}), None, make_settings()
    )
    with pytest.raises(ConfigError) as info:
        check_topology(resolved)
    only(info.value, "topology.kind", kind)
    with pytest.raises(ArrError) as refused:
        topology_for(kind)
    assert isinstance(refused.value, ConfigError)
    only(refused.value, "topology.kind", kind)
    with pytest.raises(ConfigError):
        topology_for(TopologyKind(kind))


def test_topology_for_phases_and_unknown_kinds() -> None:
    from arr.runs.phases import runner

    assert topology_for("phases") is runner.run
    with pytest.raises(ArrError) as info:
        topology_for("ring")
    assert info.value.message == (
        "Die Topologie „ring“ gibt es nicht. Ein Run beschreibt seinen Ablauf mit "
        "„topology: phases“."
    )


# --------------------------------------------------------------------------------------------
# 4. The RunManager
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", OLD)
def test_the_run_manager_never_starts_an_old_topology(tmp_path: Path, kind: str) -> None:
    called: list[Any] = []

    async def own(ctx: Any) -> None:  # a topology function of its own
        called.append(ctx)

    workfolder = make_workfolder(tmp_path)
    with pytest.raises(ConfigError) as info:
        make_manager(workfolder, own, {"a": {"turns": []}}, run_extra={"topology": {"kind": kind}})
    only(info.value, "topology.kind", kind)
    assert called == [] and list((workfolder / "runs").iterdir()) == []

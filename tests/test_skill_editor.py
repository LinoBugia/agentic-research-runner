"""W4-02: skill check, editor, duplicate and new skills, write protection, estimate.

Real files below `tmp_path`, `RealApi` with FakeBackend builders, no model. The GUI part runs
on the `MockApi` (`arr gui --mock`, A9); the browser check is in the report.
"""

import asyncio
import os
import re
import stat
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from run_kit import FAST, done, fake_builders, say, turns, write_profiles
from service_kit import make_workfolder
from test_gui import gui_user
from test_gui_screens import _walk, editor_text, see

from arr.errors import ConfigError
from arr.models import (
    AgentConfig,
    ApprovalSettings,
    ChannelMode,
    GroupChatConfig,
    Limits,
    Provider,
    ResolvedRun,
    StageSpec,
    TopologyConfig,
    TopologyKind,
)
from arr.resolve import load_group_spec
from arr.service.api_models import (
    ApiException,
    CreateSkillRequest,
    DuplicateSkillRequest,
    ErrorCode,
    SaveSkillFileRequest,
    SkillKind,
)
from arr.service.gui.pages.neuer_run import _estimate_text
from arr.service.gui.pages.skills import file_href
from arr.service.mock_api import MockApi
from arr.service.real_api import RealApi
from arr.service.real_api import catalog as catalog_module
from arr.service.real_api import skill_files as sf
from arr.service.real_api.runs import estimate_run
from arr.skills import discover_skills

BASE = "http://127.0.0.1:8765"
DEFAULTS = {"provider": "claude", "model": "fake-model", "auth": "fake-auth"}

SKILL_MD = "---\nname: {name}\ndescription: Eine Runde zum Testen.\n---\n# Runde\n"
GROUP_YAML = """kind: research-group
version: 1
topology: roundtable
params:
  rounds: {{ default: {rounds}, min: 1, max: 4, doc: "Runden" }}
limits:
  wall_time: 20m
  turns_per_agent: 6
  budget_usd: 2
agents:
  - id: m
    count: 2
    role: roles/m.md{skills}
  - id: critic
    role: {critic_role}
groupchats:
  - name: main
    members: [{members}]
    mode: rounds
"""


@pytest.fixture(autouse=True)
def no_open_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from nicegui import Client

    monkeypatch.setattr(Client, "instances", {})


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


def make_api(wf: Path) -> RealApi:
    return RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders({"a": turns([say("los"), done()])}),
        manager_kwargs={"pacing": FAST},
    )


def group_skill(
    wf: Path,
    name: str = "runde",
    *,
    rounds: Any = 2,
    skills: str = "",
    critic_role: str = "roles/critic.md",
    members: str = "m-*, critic",
    group_yaml: str | None = None,
) -> Path:
    folder = wf / "skills" / name
    (folder / "roles").mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(SKILL_MD.format(name=name), "utf-8")
    (folder / "roles" / "m.md").write_text("# Rolle m\n", "utf-8")
    (folder / "roles" / "critic.md").write_text("# Rolle critic\n", "utf-8")
    text = group_yaml or GROUP_YAML.format(
        rounds=rounds, skills=skills, critic_role=critic_role, members=members
    )
    (folder / "group.yaml").write_text(text, "utf-8")
    return folder


def snapshot(folder: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            out[str(path.relative_to(folder))] = b"->" + os.readlink(path).encode()
        elif path.is_file():
            out[str(path.relative_to(folder))] = path.read_bytes()
    return out


# --------------------------------------------------------------------------------------------
# A1: the check names file, line and what to do; invalid skills cannot start
# --------------------------------------------------------------------------------------------


async def test_a_valid_group_skill_passes_the_check(wf: Path) -> None:
    group_skill(wf)
    skill = await make_api(wf).get_skill("runde")
    assert skill.valid and skill.issues == [] and skill.kind is SkillKind.GROUP
    assert skill.agents_count == 3


@pytest.mark.parametrize(
    ("change", "line", "field", "words"),
    [
        (
            {"critic_role": "roles/kritiker.md"},
            15,
            "agents[1].role",
            ["Rolle roles/kritiker.md nicht gefunden", "Lege sie an", "entferne den Agenten"],
        ),
        (
            {"rounds": '"drei"'},
            5,
            "params.rounds",
            ["Falscher Typ", "Passe „default“ an"],
        ),
        ({"rounds": 9}, 5, "params.rounds", ["größer als das Maximum 4", "Passe „default“ an"]),
        (
            {"members": "m-*, judge-*"},
            18,
            "groupchats[0].members[1]",
            ["Das Muster „judge-*“ passt auf keinen Agenten", "Passe das Muster an"],
        ),
        (
            {"skills": "\n    skills: [sympy-chek]"},
            14,
            "agents[0].skills[0]",
            ["Den Skill „sympy-chek“ gibt es im Workfolder nicht", "Lege ihn"],
        ),
    ],
)
async def test_check_names_file_line_and_what_to_do(
    wf: Path, change: dict[str, Any], line: int, field: str, words: list[str]
) -> None:
    folder = group_skill(wf, **change)
    skill = await make_api(wf).get_skill("runde")
    assert not skill.valid
    [issue] = skill.issues
    assert (issue.file, issue.line, issue.field) == ("group.yaml", line, field)
    text = (folder / "group.yaml").read_text().splitlines()[line - 1]
    assert re.findall(r"[a-z_]+", field)[-1] + ":" in text  # the line is the place
    for word in words:
        assert word in issue.message, issue.message
    assert str(wf) not in issue.message


async def test_broken_yaml_and_broken_skill_md_have_lines(wf: Path) -> None:
    folder = group_skill(wf)
    (folder / "group.yaml").write_text("kind: research-group\nagents:\n  - [kaputt\n", "utf-8")
    skill = await make_api(wf).get_skill("runde")
    [issue] = skill.issues
    assert issue.file == "group.yaml" and issue.line == 4 and "kein gültiges YAML" in issue.message
    assert "Prüfe in dieser Zeile" in issue.message
    (folder / "SKILL.md").write_text("---\nname: runde\ndescription: ''\n---\n", "utf-8")
    skill = await make_api(wf).get_skill("runde")
    assert skill.kind is SkillKind.GROUP and not skill.valid
    md = [i for i in skill.issues if i.file == "SKILL.md"]
    assert md and md[0].line == 3 and md[0].field == "description"


async def test_list_shows_the_count_and_an_invalid_skill_cannot_start(wf: Path) -> None:
    group_skill(wf, critic_role="roles/kritiker.md")
    api = make_api(wf)
    [skill] = await api.list_skills(SkillKind.GROUP)
    assert not skill.valid and len(skill.issues) == 1
    run_input = {"project": "demo", "skill": "runde", "task": "x", "defaults": DEFAULTS}
    # the resolution finds the missing role itself and names the agent (W3-02 G1)
    result = await api.resolve_run(run_input)
    assert any(i.path == "agents.critic.role" and "kritiker" in i.message for i in result.errors)
    # a role only in the project passes the preparation, but not the check of the skill
    (wf / "projects" / "demo" / "roles").mkdir(parents=True, exist_ok=True)
    (wf / "projects" / "demo" / "roles" / "kritiker.md").write_text("# Kritiker\n", "utf-8")
    result = await api.resolve_run(run_input)
    [issue] = [i for i in result.errors if i.path == "skill"]
    assert "lässt sich nicht starten" in issue.message
    assert "group.yaml, Zeile 15: Rolle roles/kritiker.md nicht gefunden" in issue.message
    with pytest.raises(ApiException) as error:
        await api.start_run(run_input)
    assert error.value.error.code is ErrorCode.CONFIG_INVALID
    assert not list((wf / "runs").glob("*"))


# --------------------------------------------------------------------------------------------
# A2, A3: saving (also invalid files) and conflicts
# --------------------------------------------------------------------------------------------


async def test_save_writes_the_file_and_checks_the_skill_right_after(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    file = await api.get_skill_file("runde", "group.yaml")
    broken = file.content.replace("roles/critic.md", "roles/kritiker.md")
    result = await api.save_skill_file(
        "runde", "group.yaml", SaveSkillFileRequest(content=broken, version=file.version)
    )
    assert (folder / "group.yaml").read_text() == broken  # saved although invalid
    assert not result.skill.valid and "kritiker" in result.skill.issues[0].message
    assert result.file.version != file.version
    fixed = await api.save_skill_file(
        "runde",
        "group.yaml",
        SaveSkillFileRequest(content=file.content, version=result.file.version),
    )
    assert fixed.skill.valid and (folder / "group.yaml").read_text() == file.content


async def test_a_change_outside_is_not_overwritten(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    file = await api.get_skill_file("runde", "SKILL.md")
    (folder / "SKILL.md").write_text(file.content + "von außen\n", "utf-8")
    with pytest.raises(ApiException) as error:
        await api.save_skill_file(
            "runde", "SKILL.md", SaveSkillFileRequest(content="meins", version=file.version)
        )
    assert error.value.error.code is ErrorCode.CONFLICT
    assert "wurde außerhalb geändert" in error.value.error.message
    assert error.value.error.action is not None and error.value.error.action.label == "Neu laden"
    assert (folder / "SKILL.md").read_text().endswith("von außen\n")
    (folder / "SKILL.md").unlink()
    with pytest.raises(ApiException) as error:
        await api.save_skill_file(
            "runde", "SKILL.md", SaveSkillFileRequest(content="meins", version=file.version)
        )
    assert error.value.error.code is ErrorCode.CONFLICT and not (folder / "SKILL.md").exists()
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]  # no temp file left


async def test_saving_keeps_the_permissions(wf: Path) -> None:
    folder = group_skill(wf)
    (folder / "roles" / "m.md").chmod(0o640)
    api = make_api(wf)
    file = await api.get_skill_file("runde", "roles/m.md")
    await api.save_skill_file(
        "runde", "roles/m.md", SaveSkillFileRequest(content="neu\n", version=file.version)
    )
    assert stat.S_IMODE((folder / "roles" / "m.md").stat().st_mode) == 0o640


async def test_a_new_file_and_its_folders_are_created_but_never_replace_one(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    await api.save_skill_file("runde", "references/a/notiz.md", SaveSkillFileRequest(content="x"))
    assert (folder / "references" / "a" / "notiz.md").read_text() == "x"
    with pytest.raises(ApiException) as error:
        await api.save_skill_file("runde", "roles/m.md", SaveSkillFileRequest(content="weg"))
    assert error.value.error.code is ErrorCode.CONFLICT
    assert (folder / "roles" / "m.md").read_text() == "# Rolle m\n"


# --------------------------------------------------------------------------------------------
# A5: new skills and duplicates
# --------------------------------------------------------------------------------------------


async def test_new_group_skill_passes_the_check_and_can_be_resolved(wf: Path) -> None:
    api = make_api(wf)
    result = await api.create_skill(
        CreateSkillRequest(name="beweis-runde", kind="group", description="Zwei prüfen.")
    )
    assert result.skill.valid and result.skill.kind is SkillKind.GROUP
    assert sorted(result.skill.files) == ["SKILL.md", "group.yaml", "roles/teilnehmer.md"]
    assert [s.name for s in await api.list_skills(SkillKind.GROUP)] == ["beweis-runde"]
    resolved = await api.resolve_run(
        {"project": "demo", "skill": "beweis-runde", "task": "x", "defaults": DEFAULTS}
    )
    assert resolved.errors == [] and [a.id for a in resolved.agents] == [
        "teilnehmer-1",
        "teilnehmer-2",
    ]
    tool = await api.create_skill(CreateSkillRequest(name="notation", kind="tool"))
    assert (
        tool.skill.kind is SkillKind.TOOL and tool.skill.valid and tool.skill.files == ["SKILL.md"]
    )
    assert {s.name for s in await api.list_skills()} == {"beweis-runde", "notation"}


async def test_duplicate_copies_regular_files_and_renames(wf: Path, tmp_path: Path) -> None:
    folder = group_skill(wf)
    outside = tmp_path / "geheim.txt"
    outside.write_text("geheim", "utf-8")
    os.symlink(outside, folder / "link.md")
    os.mkfifo(folder / "pipe")
    api = make_api(wf)
    result = await api.duplicate_skill("runde", DuplicateSkillRequest(name="runde-streng"))
    copy = wf / "skills" / "runde-streng"
    assert result.skill.valid and "name: runde-streng" in (copy / "SKILL.md").read_text()
    assert (copy / "group.yaml").read_bytes() == (folder / "group.yaml").read_bytes()
    assert sorted(result.left_out) == ["link.md", "pipe"]
    assert sorted(p.name for p in copy.iterdir()) == ["SKILL.md", "group.yaml", "roles"]
    assert "Nicht kopiert" in result.message


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("runde", ErrorCode.CONFLICT),
        ("Runde", ErrorCode.INVALID),
        ("../x", ErrorCode.INVALID),
        ("arr", ErrorCode.INVALID),
        ("-a", ErrorCode.INVALID),
        ("a" * 65, ErrorCode.INVALID),
    ],
)
async def test_names_follow_the_rule_and_must_be_free(wf: Path, name: str, code: ErrorCode) -> None:
    group_skill(wf)
    api = make_api(wf)
    before = sorted(p.name for p in (wf / "skills").iterdir())
    for call in (
        api.create_skill(CreateSkillRequest(name=name, kind="tool")),
        api.duplicate_skill("runde", DuplicateSkillRequest(name=name)),
    ):
        with pytest.raises(ApiException) as error:
            await call
        assert error.value.error.code is code
    assert sorted(p.name for p in (wf / "skills").iterdir()) == before


# --------------------------------------------------------------------------------------------
# A6: write protection, with the check that nothing landed outside
# --------------------------------------------------------------------------------------------


@pytest.fixture
def hostile(wf: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A skill with links out (file, folder), a FIFO, a huge and a non-UTF-8 file."""
    folder = group_skill(wf)
    outside = tmp_path / "draussen"
    outside.mkdir()
    (outside / "ziel.md").write_text("draußen\n", "utf-8")
    os.symlink(outside / "ziel.md", folder / "link.md")
    os.symlink(outside, folder / "linkdir")
    os.mkfifo(folder / "pipe.md")
    (folder / "gross.md").write_bytes(b"x" * (sf.MAX_BYTES + 1))
    (folder / "latin.md").write_bytes("Größe".encode("latin-1"))
    return folder, outside


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../runde-x/SKILL.md", ErrorCode.INVALID),
        ("../../arr.yaml", ErrorCode.INVALID),
        ("/etc/hosts", ErrorCode.INVALID),
        ("roles/../../x.md", ErrorCode.INVALID),
        (".versteckt.md", ErrorCode.INVALID),
        ("link.md", ErrorCode.NOT_ALLOWED),
        ("linkdir/ziel.md", ErrorCode.NOT_ALLOWED),
        ("linkdir/neu.md", ErrorCode.NOT_ALLOWED),
        ("pipe.md", ErrorCode.NOT_ALLOWED),
        ("gross.md", ErrorCode.NOT_ALLOWED),
    ],
)
async def test_refused_writes_write_nothing_anywhere(
    wf: Path, hostile: tuple[Path, Path], path: str, code: ErrorCode
) -> None:
    folder, outside = hostile
    api = make_api(wf)
    before_out, before_wf = snapshot(outside), snapshot(wf)
    for version in (None, "0" * 16):
        with pytest.raises(ApiException) as error:
            await api.save_skill_file(
                "runde", path, SaveSkillFileRequest(content="böse", version=version)
            )
        assert error.value.error.code in (code, ErrorCode.CONFLICT), error.value.error.message
        assert str(wf) not in error.value.error.message
    assert snapshot(outside) == before_out
    assert snapshot(wf) == before_wf
    assert not list((wf.parent).glob("x.md"))


@pytest.mark.parametrize(
    "path", ["link.md", "linkdir/ziel.md", "pipe.md", "gross.md", "latin.md", "../arr.yaml"]
)
async def test_reading_follows_the_same_rules_and_never_hangs(
    wf: Path, hostile: tuple[Path, Path], path: str
) -> None:
    api = make_api(wf)
    with pytest.raises(ApiException) as error:
        await asyncio.wait_for(api.get_skill_file("runde", path), timeout=5)
    assert error.value.error.code in (ErrorCode.NOT_ALLOWED, ErrorCode.INVALID)
    assert "draußen" not in error.value.error.message


async def test_too_large_or_binary_content_is_refused(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    file = await api.get_skill_file("runde", "SKILL.md")
    for content in ("x" * (sf.MAX_BYTES + 1), "a\x00b"):
        with pytest.raises(ApiException) as error:
            await api.save_skill_file(
                "runde", "SKILL.md", SaveSkillFileRequest(content=content, version=file.version)
            )
        assert error.value.error.code is ErrorCode.INVALID
    assert (folder / "SKILL.md").read_text() == file.content


async def test_a_linked_skill_or_skills_folder_is_not_written(wf: Path, tmp_path: Path) -> None:
    real = group_skill(tmp_path / "fremd", "runde")  # a real skill outside the workfolder
    (wf / "skills").mkdir(exist_ok=True)
    os.symlink(real, wf / "skills" / "verlinkt")
    before = snapshot(real)
    api = make_api(wf)
    with pytest.raises(ApiException) as error:
        await api.save_skill_file("verlinkt", "neu.md", SaveSkillFileRequest(content="x"))
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    with pytest.raises(ApiException):
        await api.get_skill_file("verlinkt", "SKILL.md")
    assert snapshot(real) == before
    # the skills folder itself as a link
    second = tmp_path / "wf2"
    second.mkdir()
    (second / "arr.yaml").write_text("version: 1\n", "utf-8")
    os.symlink(real.parent, second / "skills")
    with pytest.raises(ApiException) as error:
        sf.write_file(second, "runde", "neu.md", "x", version=None)
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert snapshot(real) == before


async def test_listing_and_checking_survive_hostile_files(wf: Path, tmp_path: Path) -> None:
    folder = group_skill(wf)
    (folder / "group.yaml").unlink()
    os.mkfifo(folder / "group.yaml")
    other = wf / "skills" / "riesig"
    other.mkdir()
    (other / "SKILL.md").write_bytes(b"---\nname: riesig\n" + b"x" * (sf.MAX_BYTES + 10))
    pipe = wf / "skills" / "pipe"
    pipe.mkdir()
    os.mkfifo(pipe / "SKILL.md")
    skills = await asyncio.wait_for(make_api(wf).list_skills(), timeout=10)
    by_name = {s.name: s for s in skills}
    assert not by_name["riesig"].valid and "zu groß" in by_name["riesig"].issues[0].message
    assert not by_name["pipe"].valid and "keine normale Datei" in by_name["pipe"].issues[0].message
    assert by_name["runde"].kind is SkillKind.TOOL  # a FIFO is no group.yaml


# --------------------------------------------------------------------------------------------
# A7: estimate as ranges without invented prices
# --------------------------------------------------------------------------------------------


def resolved_run(
    kind: TopologyKind,
    *,
    agents: int = 2,
    params: dict[str, Any] | None = None,
    limits: Limits | None = None,
    mode: ChannelMode = ChannelMode.ROUNDS,
    provider: Provider = Provider.CLAUDE,
    stages: list[StageSpec] | None = None,
    raters: list[str] | None = None,
) -> ResolvedRun:
    ids = [f"a-{i}" for i in range(1, agents + 1)]
    return ResolvedRun(
        project="demo",
        task="x",
        topology=TopologyConfig(kind=kind, params=params or {}),
        limits=limits or Limits(wall_time=timedelta(minutes=20), turns_per_agent=6, budget_usd=2),
        agents=[
            AgentConfig(id=i, base_id="a", provider=provider, model="m", auth="p") for i in ids
        ],
        groupchats=[GroupChatConfig(name="main", members=ids, mode=mode)],
        stages=stages or [],
        raters=raters or [],
        approvals=ApprovalSettings(),
    )


def test_estimate_of_a_roundtable_comes_from_rounds_and_limits() -> None:
    est = estimate_run(resolved_run(TopologyKind.ROUNDTABLE, params={"rounds": 2}))
    assert (est.turns.min, est.turns.max) == (2, 4)
    assert est.cost_basis == "budget" and (est.cost_usd.min, est.cost_usd.max) == (0, 2)
    assert est.cost_kind is None  # no price, no „≈“
    assert est.duration_basis == "wall_time" and est.duration_s.max == 1200
    event = estimate_run(
        resolved_run(TopologyKind.ROUNDTABLE, params={"rounds": 3}, mode=ChannelMode.EVENT)
    )
    assert (event.turns.min, event.turns.max) == (2, 12)  # capped by 2 agents × 6 turns
    with_summary = estimate_run(
        resolved_run(TopologyKind.ROUNDTABLE, params={"rounds": 1, "summarizer": "a-1"})
    )
    assert (with_summary.turns.min, with_summary.turns.max) == (3, 3)


def test_estimate_of_solo_and_waterfall() -> None:
    solo = estimate_run(resolved_run(TopologyKind.SOLO, agents=1, params={"max_turns": 3}))
    assert (solo.turns.min, solo.turns.max) == (1, 3)
    stages = [StageSpec(name="s1", agent="a-1"), StageSpec(name="s2", agent="a-2")]
    water = estimate_run(
        resolved_run(
            TopologyKind.WATERFALL,
            agents=3,
            params={"max_rework": 1},
            stages=stages,
            raters=["a-3"],
            limits=Limits(turns_per_agent=40, budget_usd=5, wall_time=timedelta(hours=2)),
        )
    )
    # per stage: 1 attempt = author + 1 rater; at most 2 attempts with one re-ask each
    assert (water.turns.min, water.turns.max) == (4, 16)


def test_estimate_says_what_it_does_not_know() -> None:
    no_limits = estimate_run(
        resolved_run(
            TopologyKind.ROUNDTABLE,
            params={"rounds": 2},
            limits=Limits(wall_time=None, turns_per_agent=6, budget_usd=None),
        )
    )
    assert no_limits.cost_basis == "unknown" and no_limits.duration_basis == "unknown"
    assert any("kein Budget" in n for n in no_limits.notes)
    assert any("keine Laufzeitgrenze" in n for n in no_limits.notes)
    codex = estimate_run(resolved_run(TopologyKind.ROUNDTABLE, provider=Provider.CODEX))
    assert codex.cost_basis == "unknown" and any("Codex" in n for n in codex.notes)


async def test_resolve_run_fills_the_estimate(wf: Path) -> None:
    group_skill(wf)
    result = await make_api(wf).resolve_run(
        {"project": "demo", "skill": "runde", "task": "x", "defaults": DEFAULTS}
    )
    est = result.estimate
    assert est is not None and est.turns.min == 3 and est.turns.max == 6  # 3 members × 2 rounds
    assert est.cost_basis == "budget" and est.cost_usd.max == 2


# --------------------------------------------------------------------------------------------
# A8: the skill message names no absolute path
# --------------------------------------------------------------------------------------------


def test_unknown_skill_message_has_no_absolute_path(wf: Path) -> None:
    group_skill(wf)
    catalog = discover_skills(wf / "skills")
    issue = catalog.lookup_issue("rundee", "skill")
    assert issue is not None and "Meintest du „runde“?" in issue.message
    assert "gibt es im Workfolder nicht" in issue.message
    assert str(wf) not in issue.message and "/" not in issue.message
    (wf / "skills" / "kaputt").mkdir()
    (wf / "skills" / "kaputt" / "SKILL.md").write_text("ohne Kopf", "utf-8")
    broken = discover_skills(wf / "skills").lookup_issue("kaputt")
    assert broken is not None and str(wf) not in broken.message
    assert "skills/kaputt/SKILL.md" in broken.message


# --------------------------------------------------------------------------------------------
# A9: `arr gui --mock` with the editor
# --------------------------------------------------------------------------------------------


async def test_mock_has_the_editor_functions() -> None:
    api = MockApi()
    file = await api.get_skill_file("roundtable", "SKILL.md")
    assert file.version
    saved = await api.save_skill_file(
        "roundtable", "SKILL.md", SaveSkillFileRequest(content="neu", version=file.version)
    )
    assert (await api.get_skill_file("roundtable", "SKILL.md")).content == "neu"
    with pytest.raises(ApiException) as error:
        await api.save_skill_file(
            "roundtable", "SKILL.md", SaveSkillFileRequest(content="alt", version=file.version)
        )
    assert error.value.error.code is ErrorCode.CONFLICT and saved.file.version
    created = await api.create_skill(CreateSkillRequest(name="neu-g", kind="group"))
    assert created.skill.kind is SkillKind.GROUP
    assert "neu-g" in [s.name for s in await api.list_skills(SkillKind.GROUP)]
    copy = await api.duplicate_skill("neu-g", DuplicateSkillRequest(name="neu-g-kopie"))
    text = (await api.get_skill_file("neu-g-kopie", "SKILL.md")).content
    assert copy.skill.name == "neu-g-kopie" and "name: neu-g-kopie" in text
    with pytest.raises(ApiException):
        await api.save_skill_file("neu-g", "../x.md", SaveSkillFileRequest(content="x"))


async def test_gui_mock_edits_saves_and_asks_before_discarding() -> None:
    api = MockApi()
    async with gui_user(api) as user:
        await user.open("/skills/roundtable?tab=skill")
        assert "name: roundtable" in await editor_text(user)
        await see(user, "Laufende Runs nutzen ihren Schnappschuss")
        area = user.find(marker="editor-text")
        area.trigger("input", "---\nname: roundtable\ndescription: Neu.\n---\n")
        user.find(marker="editor-save").click()
        await see(user, "Gespeichert um")
        assert "description: Neu." in (await api.get_skill_file("roundtable", "SKILL.md")).content
        area.trigger("input", "verworfen")
        user.find(marker="editor-discard").click()
        await see(user, "Änderungen an SKILL.md verwerfen?")
        with user.client:
            [confirm] = [
                n
                for n in _walk(user.client.layout)
                if "dialog-confirm" in n._markers  # noqa: SLF001
            ]
        confirm.mark("confirm-discard")
        user.find(marker="confirm-discard").click()
        await see(user, "Änderungen verworfen.")
        assert "description: Neu." in await editor_text(user)
        await user.open("/skills/roundtable?tab=dateien")
        await see(user, "＋ Neue Datei")
        await user.open("/skills")
        await see(user, "＋ Neuer Skill")
        html = await see(user, "Duplizieren …")
        assert "kommen in einer späteren Version" not in html


# ================================================================================================
# W4-02.1: rework after the review
# ================================================================================================


def bomb(levels: int, fan: int = 9) -> str:
    """The alias bomb of the review (`yaml_bombe_messung.py`): 9^(levels+1) items expanded."""
    out = [f"a0: &a0 [{', '.join(['x'] * fan)}]"]
    for i in range(1, levels + 1):
        out.append(f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * fan)}]")
    out.append("kind: research-group\nversion: 1\ntopology: roundtable\nagents: []\n")
    return "\n".join(out)


async def timed(coro: Any, limit: float = 20.0) -> tuple[Any, float]:
    start = time.monotonic()
    result = await asyncio.wait_for(coro, timeout=limit)
    return result, time.monotonic() - start


@pytest.mark.parametrize("levels", [6, 7])
async def test_alias_bomb_is_refused_quickly_and_only_marks_its_skill(
    wf: Path, levels: int
) -> None:
    group_skill(wf)  # a healthy neighbour
    api = make_api(wf)
    _ok, healthy = await timed(api.list_skills())
    bad = wf / "skills" / "bombe"
    bad.mkdir()
    (bad / "SKILL.md").write_text(SKILL_MD.format(name="bombe"), "utf-8")
    (bad / "group.yaml").write_text(bomb(levels), "utf-8")
    skills, elapsed = await timed(api.list_skills())
    # relative: the bomb costs at most a few times a normal listing (before: 21 s / minutes)
    assert elapsed < max(2.0, 10 * healthy), (elapsed, healthy)
    by_name = {s.name: s for s in skills}
    assert by_name["runde"].valid
    [issue] = by_name["bombe"].issues
    assert issue.file == "group.yaml" and "zu verschachtelt" in issue.message
    with pytest.raises(ConfigError) as error:
        load_group_spec(bad / "group.yaml")  # the run start reads it the same way
    assert "zu verschachtelt" in error.value.issue_message


async def test_deep_nesting_in_group_yaml_is_an_issue_not_a_crash(wf: Path) -> None:
    group_skill(wf)
    bad = wf / "skills" / "tief"
    bad.mkdir()
    (bad / "SKILL.md").write_text(SKILL_MD.format(name="tief"), "utf-8")
    (bad / "group.yaml").write_text(
        "kind: research-group\ntopology: roundtable\nagents: " + "[" * 500 + "]" * 500 + "\n"
    )
    api = make_api(wf)
    skill = await api.get_skill("tief")
    assert not skill.valid and "zu verschachtelt" in skill.issues[0].message
    assert {s.name for s in await api.list_skills()} == {"runde", "tief"}
    (bad / "group.yaml").write_text("a: " + "[" * 5000 + "]" * 5000 + "\n")
    assert "zu verschachtelt" in (await api.get_skill("tief")).issues[0].message


async def test_deep_skill_md_in_another_skill_blocks_nothing_else(wf: Path) -> None:
    """Review `test_deep_skill_md_in_another_skill_blocks_every_run_start`."""
    group_skill(wf)
    bad = wf / "skills" / "tief"
    bad.mkdir()
    (bad / "SKILL.md").write_text(
        "---\nname: tief\ndescription: ok\nmetadata: " + "[" * 500 + "]" * 500 + "\n---\n", "utf-8"
    )
    catalog = discover_skills(wf / "skills")
    assert "runde" in catalog.skills and "tief" in catalog.problems
    assert "zu verschachtelt" in catalog.problems["tief"].issue_message
    api = make_api(wf)
    skills = {s.name: s for s in await api.list_skills()}
    assert skills["runde"].valid and not skills["tief"].valid
    run_input = {"project": "demo", "skill": "runde", "task": "x", "defaults": DEFAULTS}
    assert (await api.resolve_run(run_input)).errors == []
    started = await api.start_run(run_input)
    assert started.run_id
    await api.stop_runs()


async def test_frontmatter_alias_bomb_is_refused(wf: Path) -> None:
    folder = wf / "skills" / "bombe"
    folder.mkdir(parents=True)
    head = bomb(7).replace(
        "kind: research-group\nversion: 1\ntopology: roundtable\nagents: []\n", ""
    )
    (folder / "SKILL.md").write_text(
        "---\nname: bombe\ndescription: x\nmetadata:\n  " + head.replace("\n", "\n  ") + "\n---\n",
        "utf-8",
    )
    catalog, elapsed = await timed(asyncio.to_thread(discover_skills, wf / "skills"))
    assert elapsed < 5 and "zu verschachtelt" in catalog.problems["bombe"].issue_message


async def test_one_failing_check_never_breaks_the_list(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_skill(wf)
    group_skill(wf, "zweite")
    original = catalog_module.CatalogArea._skill

    def boom(self: Any, name: str, catalog: Any, settings: Any = None) -> Any:
        if name == "zweite":
            raise RuntimeError("kaputt")
        return original(self, name, catalog, settings)

    monkeypatch.setattr(catalog_module.CatalogArea, "_skill", boom)
    skills = {s.name: s for s in await make_api(wf).list_skills()}
    assert skills["runde"].valid
    assert not skills["zweite"].valid and "RuntimeError" in skills["zweite"].issues[0].message


# -- point 2: no raw errors when writing -------------------------------------------------------


async def test_lone_surrogate_is_a_clear_message(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    file = await api.get_skill_file("runde", "SKILL.md")
    with pytest.raises(ApiException) as error:
        await api.save_skill_file(
            "runde", "SKILL.md", SaveSkillFileRequest(content="a\ud800b", version=file.version)
        )
    assert error.value.error.code is ErrorCode.INVALID
    assert "Zeichen, die in keiner Textdatei stehen" in error.value.error.message
    assert (folder / "SKILL.md").read_text() == file.content


async def test_names_are_at_most_200_characters(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    before = snapshot(folder)
    for path in ("a" * 300 + ".md", "roles/" + "b" * 250 + ".md", "c" * 201):
        with pytest.raises(ApiException) as error:
            await api.save_skill_file("runde", path, SaveSkillFileRequest(content="x"))
        assert (
            error.value.error.code is ErrorCode.INVALID and "zu lang" in error.value.error.message
        )
    assert snapshot(folder) == before
    longest = "d" * 197 + ".md"  # 200 characters: the temporary file name still fits
    await api.save_skill_file("runde", longest, SaveSkillFileRequest(content="x"))
    assert (folder / longest).read_text() == "x"
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]


async def test_a_folder_in_place_of_the_file_is_a_clear_message(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = group_skill(wf)
    target = folder / "roles" / "m.md"
    api = make_api(wf)
    file = await api.get_skill_file("runde", "roles/m.md")
    original = sf._current
    calls = {"n": 0}

    def swap(folder_fd: int, name: str, label: str) -> Any:
        result = original(folder_fd, name, label)
        if calls["n"] == 1:  # the second check, right before `rename`
            target.unlink()
            target.mkdir()
        calls["n"] += 1
        return result

    monkeypatch.setattr(sf, "_current", swap)
    with pytest.raises(ApiException) as error:
        await api.save_skill_file(
            "runde", "roles/m.md", SaveSkillFileRequest(content="# neu\n", version=file.version)
        )
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert "Ordner" in error.value.error.message and "nichts geändert" in error.value.error.message
    assert target.is_dir() and not list(target.iterdir())
    assert not [p for p in (folder / "roles").iterdir() if p.name.startswith(".")]


async def test_only_the_exact_skill_name_reads_and_writes(wf: Path) -> None:
    folder = group_skill(wf)
    api = make_api(wf)
    file = await api.get_skill_file("runde", "SKILL.md")
    for call in (
        api.get_skill_file("RUNDE", "SKILL.md"),
        api.save_skill_file(
            "RUNDE", "SKILL.md", SaveSkillFileRequest(content="# alias\n", version=file.version)
        ),
    ):
        with pytest.raises(ApiException) as error:
            await call
        assert error.value.error.code is ErrorCode.NOT_FOUND
    assert (folder / "SKILL.md").read_text() == file.content


def _deep_tree(folder: Path, levels: int) -> None:
    """A folder tree deeper than any path may be, built relative to open folders."""
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i in range(levels):
            os.mkdir(f"d{i}", dir_fd=fd)
            child = os.open(f"d{i}", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.close(os.open("tief.md", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd))
    finally:
        os.close(fd)


async def test_a_very_deep_folder_tree_neither_breaks_the_list_nor_the_duplicate(
    wf: Path,
) -> None:
    folder = group_skill(wf)
    _deep_tree(folder, 400)
    api = make_api(wf)
    skill = await asyncio.wait_for(api.get_skill("runde"), timeout=20)
    assert skill.valid and not any("tief.md" in f for f in skill.files)
    result = await asyncio.wait_for(
        api.duplicate_skill("runde", DuplicateSkillRequest(name="runde-tief")), timeout=60
    )
    assert result.skill.valid and any(p.endswith("/") for p in result.left_out)


# -- point 3: the gaps the mutations M09, M14, M15, M24 showed --------------------------------


async def test_a_role_linked_out_of_the_skill_is_an_issue(wf: Path, tmp_path: Path) -> None:
    folder = group_skill(wf, critic_role="roles/aussen.md")
    outside = tmp_path / "aussen.md"
    outside.write_text("# draußen\n", "utf-8")
    os.symlink(outside, folder / "roles" / "aussen.md")
    [issue] = (await make_api(wf).get_skill("runde")).issues
    assert issue.field == "agents[1].role" and "ist ein Link aus dem Skill hinaus" in issue.message


@pytest.mark.parametrize("role", ["../geheim.md", "roles/../../geheim.md", "/etc/hosts"])
async def test_a_role_outside_the_skill_is_an_issue(wf: Path, role: str) -> None:
    group_skill(wf, critic_role=role)
    (wf / "skills" / "geheim.md").write_text("# geheim\n", "utf-8")
    [issue] = (await make_api(wf).get_skill("runde")).issues
    assert issue.field == "agents[1].role" and "liegt außerhalb des Skills" in issue.message


async def test_paths_deeper_than_the_limit_are_refused(wf: Path) -> None:
    folder = group_skill(wf)
    path = "/".join(["e"] * sf.MAX_DEPTH) + ".md"  # MAX_DEPTH + 1 parts would be needed: here
    deeper = "/".join(["e"] * (sf.MAX_DEPTH + 1))
    api = make_api(wf)
    with pytest.raises(ApiException) as error:
        await api.save_skill_file("runde", deeper, SaveSkillFileRequest(content="x"))
    assert error.value.error.code is ErrorCode.INVALID and "zu tief" in error.value.error.message
    assert not (folder / "e").exists()
    await api.save_skill_file("runde", path, SaveSkillFileRequest(content="x"))
    assert (folder / path).read_text() == "x"


def test_folders_with_underscore_are_no_skills_for_the_editor(wf: Path) -> None:
    hidden = wf / "skills" / "_roles"
    hidden.mkdir(parents=True)
    (hidden / "kritiker.md").write_text("# Kritiker\n", "utf-8")
    for call in (
        lambda: sf.read_file(wf, "_roles", "kritiker.md"),
        lambda: sf.write_file(wf, "_roles", "neu.md", "x", version=None),
    ):
        with pytest.raises(ApiException) as error:
            call()
        assert error.value.error.code is ErrorCode.NOT_FOUND
    assert sorted(p.name for p in hidden.iterdir()) == ["kritiker.md"]


# -- point 4: texts ----------------------------------------------------------------------------


def test_texts_follow_08_section_8_7() -> None:
    est = estimate_run(
        resolved_run(
            TopologyKind.ROUNDTABLE,
            limits=Limits(wall_time=timedelta(minutes=20), turns_per_agent=6, budget_usd=2.5),
        )
    )
    assert any("Budget von 2,50 $" in n for n in est.notes), est.notes
    assert "höchstens 2,50 $ (Budget)" in _estimate_text(est)
    stages = [StageSpec(name="s1", agent="a-1")]
    open_end = estimate_run(
        resolved_run(
            TopologyKind.WATERFALL,
            params={"on_fail": "escalate"},
            stages=stages,
            raters=["a-2"],
            limits=Limits(wall_time=timedelta(hours=1), turns_per_agent=None, budget_usd=2),
        )
    )
    assert open_end.turns_open and _estimate_text(open_end).startswith("mindestens 2 Turns")


async def test_new_file_names_say_which_letters_go(wf: Path) -> None:
    group_skill(wf)
    with pytest.raises(ApiException) as error:
        await make_api(wf).save_skill_file("runde", "prüfer.md", SaveSkillFileRequest(content=""))
    assert "a–z" in error.value.error.message and "Umlaute" in error.value.error.message


# -- point 5: file links are encoded ----------------------------------------------------------


async def test_file_links_are_encoded_and_such_files_open(wf: Path) -> None:
    folder = group_skill(wf)
    odd = "notiz & frage #1?.md"
    (folder / odd).write_text("# Notiz\n", "utf-8")
    href = file_href("runde", f"roles/{odd}")
    assert href == "/skills/runde?tab=dateien&datei=roles/notiz%20%26%20frage%20%231%3F.md"
    api = make_api(wf)
    file = await api.get_skill_file("runde", odd)
    assert file.content == "# Notiz\n"
    await api.save_skill_file(
        "runde", odd, SaveSkillFileRequest(content="# neu\n", version=file.version)
    )
    assert (folder / odd).read_text() == "# neu\n"
    with pytest.raises(ApiException):  # new files keep the strict names
        await api.save_skill_file("runde", "neu & alt.md", SaveSkillFileRequest(content="x"))


# -- point 6: a duplicate has a total limit -----------------------------------------------------


async def test_a_too_large_skill_is_not_duplicated(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = group_skill(wf)
    (folder / "daten.bin").write_bytes(b"x" * 4000)
    monkeypatch.setattr(sf, "MAX_COPY_TOTAL", 3000)
    before = sorted(p.name for p in (wf / "skills").iterdir())
    with pytest.raises(ApiException) as error:
        await make_api(wf).duplicate_skill("runde", DuplicateSkillRequest(name="runde-gross"))
    assert error.value.error.code is ErrorCode.NOT_ALLOWED
    assert "zu groß zum Duplizieren" in error.value.error.message
    assert "nichts angelegt" in error.value.error.message
    assert sorted(p.name for p in (wf / "skills").iterdir()) == before

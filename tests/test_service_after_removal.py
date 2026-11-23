"""W4b-06b §6.5: the service and the GUI after the removal of the old topologies.

- F2 (A11): the button „Start-Skills aktualisieren“, shown only when a start skill equals an
  earlier version arr shipped (`tests/fixtures/start_skills_old/`); a click acts like
  `arr skills update` (the same lines); a changed start skill stays. Real API and mock.
- F3 (A10): the template of a new group skill passes `check_group` without error or hint.
- An old group shows the message of §6.1 on the skills page.
- „Phase“ instead of „Stufe“ in phases runs; old runs word for word (A12). The card of an
  escalation reads pass and reason from the log entry; `D{n}` in `compact_card`.
- `run_results` and `run_summary` without the old topologies.

Real files, NiceGUI user simulation on a `RealApi` or the `MockApi`; no model.
"""

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from nicegui.testing import User
from run_kit import make_workfolder, write_profiles
from test_gui_real import BASE, see

from arr.models import RunStatus, TopologyKind, WorkfolderSettings
from arr.service.api_models import ApprovalItem
from arr.service.real_api import RealApi
from arr.skills import (
    START_SKILLS,
    SkillUpdate,
    updatable_start_skills,
    update_lines,
    update_start_skills,
)

OLD = Path(__file__).parent / "fixtures" / "start_skills_old"
GROUPS = ("exploration", "proof", "proof-review", "roundtable")


def old_start_skills(workfolder: Path) -> None:
    """The four group start skills as an earlier arr shipped them (old topologies)."""
    for name in GROUPS:
        target = workfolder / "skills" / name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(OLD / name, target)


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text(
        "version: 1\ntoolchain: {run_venv: off}\ndefaults:\n"
        "  claude: {auth: fake-auth, model: fake-model}\n"
        "  codex: {auth: fake-auth, model: fake-gpt}\n",
        "utf-8",
    )
    return workfolder


# --------------------------------------------------------------------------------------------
# F2: which start skills can be updated, and the update through the API
# --------------------------------------------------------------------------------------------


def test_the_read_function_finds_unchanged_earlier_versions(wf: Path) -> None:
    assert updatable_start_skills(wf) == []
    old_start_skills(wf)
    assert updatable_start_skills(wf) == sorted(GROUPS, key=START_SKILLS.index)
    (wf / "skills" / "proof" / "SKILL.md").write_text("geändert\n", "utf-8")
    assert "proof" not in updatable_start_skills(wf), "a changed one is never updated"
    before = {p: p.read_bytes() for p in (wf / "skills").rglob("*") if p.is_file()}
    updatable_start_skills(wf)
    after = {p: p.read_bytes() for p in (wf / "skills").rglob("*") if p.is_file()}
    assert before == after, "reading writes nothing"


def test_the_read_function_without_a_skills_folder(tmp_path: Path) -> None:
    assert updatable_start_skills(tmp_path) == []


async def test_the_api_updates_like_arr_skills_update(wf: Path, tmp_path: Path) -> None:
    old_start_skills(wf)
    (wf / "skills" / "proof" / "SKILL.md").write_text("geändert\n", "utf-8")
    twin = tmp_path / "zwilling"
    shutil.copytree(wf / "skills", twin / "skills")
    api = RealApi(wf, base_url=BASE)
    dry = await api.update_start_skills(dry_run=True)
    assert dry.updatable == ["exploration", "proof-review", "roundtable"]
    assert dry.updated == [] and dry.lines == []
    assert (wf / "skills" / "proof-review" / "group.yaml").read_text().find("phases") < 0
    result = await api.update_start_skills()
    assert result.lines == update_lines(update_start_skills(twin)), "the lines of the CLI"
    assert result.updated == ["exploration", "proof-review", "roundtable"]
    assert "proof: geändert, bleibt" in "\n".join(result.lines) or any(
        line.startswith("proof:") for line in result.lines
    )
    assert (wf / "skills" / "proof" / "SKILL.md").read_text() == "geändert\n"
    assert (await api.update_start_skills(dry_run=True)).updatable == []
    skills = {s.name: s for s in await api.list_skills()}
    assert skills["roundtable"].topology is TopologyKind.PHASES
    assert not [i for i in skills["roundtable"].issues if i.field == "topology"]
    assert "topology" in [i.field for i in skills["proof"].issues], "the changed one keeps it"
    await api.close()


async def test_the_lines_name_a_changed_start_skill(wf: Path) -> None:
    old_start_skills(wf)
    (wf / "skills" / "proof" / "SKILL.md").write_text("geändert\n", "utf-8")
    result = await RealApi(wf, base_url=BASE).update_start_skills()
    [proof] = [line for line in result.lines if line.startswith("proof:")]
    assert SkillUpdate.CHANGED.value in proof or "geändert" in proof


async def test_the_mock_has_the_method_and_nothing_to_update() -> None:
    from arr.service.mock_api import MockApi

    api = MockApi()
    assert (await api.update_start_skills(dry_run=True)).updatable == []
    result = await api.update_start_skills()
    assert result.updated == [] and result.message == "Kein Start-Skill war zu aktualisieren."
    assert len(result.lines) == len(START_SKILLS)
    groups = [s for s in await api.list_skills() if s.kind.value == "group"]
    assert groups and all(s.topology is TopologyKind.PHASES for s in groups), "nothing old"


# --------------------------------------------------------------------------------------------
# F2 in the GUI: the button
# --------------------------------------------------------------------------------------------


@asynccontextmanager
async def gui(wf: Path) -> AsyncIterator[tuple[User, RealApi]]:
    from test_gui_real import real_user

    async with real_user(wf) as (user, api):
        yield user, api


def html_of(user: User) -> str:
    from test_gui_real import page_html

    return page_html(user)


async def test_the_button_shows_only_for_an_earlier_version_and_updates(wf: Path) -> None:
    old_start_skills(wf)
    (wf / "skills" / "proof" / "SKILL.md").write_text("geändert\n", "utf-8")
    async with gui(wf) as (user, _api):
        await user.open("/skills/roundtable")
        await see(user, "Start-Skills aktualisieren")
        await see(user, "Die Topologie „roundtable“ gibt es nicht mehr.")
        with user.client:
            [button] = user.find(marker="update-start-skills").elements
            title = button.props.get("title") or button._props.get("title")
        assert title == "Unverändert, frühere Fassung: exploration, proof-review, roundtable"
        user.find(marker="update-start-skills").click()
        html = await see(user, "start-skills-lines", retries=200)
        assert "roundtable: aktualisiert" in html
        assert "proof:" in html, "the changed one is named"
        for _ in range(100):
            if 'data-marker="update-start-skills"' not in html_of(user):
                break
            await asyncio.sleep(0.05)
        assert 'data-marker="update-start-skills"' not in html_of(user), "nothing left"
    assert "topology: phases" in (wf / "skills" / "roundtable" / "group.yaml").read_text()


async def test_no_button_with_current_start_skills(wf: Path) -> None:
    from arr.skills import install_start_skills

    install_start_skills(wf)
    async with gui(wf) as (user, _api):
        await user.open("/skills")
        await see(user, "roundtable")
        await asyncio.sleep(0.2)
        assert "Start-Skills aktualisieren" not in html_of(user)


# --------------------------------------------------------------------------------------------
# F3: the template of a new group skill
# --------------------------------------------------------------------------------------------


def test_a_new_group_skill_passes_the_check(tmp_path: Path) -> None:
    from arr.service.gui.pages.skills import ROLE_TEMPLATE
    from arr.service.real_api.catalog import check_group
    from arr.service.real_api.skill_files import template_files
    from arr.skills import discover_skills

    files = template_files("meine-runde", "group", "Zwei Agenten diskutieren")
    folder = tmp_path / "skills" / "meine-runde"
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, "utf-8")
    settings = WorkfolderSettings.model_validate(
        {"toolchain": {"run_venv": "off"}, "defaults": {"claude": {"auth": "a", "model": "m"}}}
    )
    spec, issues = check_group(folder, discover_skills(tmp_path / "skills"), settings)
    assert spec is not None and spec.topology is TopologyKind.PHASES
    assert issues == [], "no error and no hint"
    assert [p.name for p in spec.phases] == ["diskussion"]
    assert sorted(files) == [
        "SKILL.md",
        "group.yaml",
        "roles/teilnehmer.md",
        "tasks/diskutieren.md",
    ]
    assert "`submit`" in files["roles/teilnehmer.md"] and "submit_result" not in str(files)
    assert "(`submit`)" in ROLE_TEMPLATE and "submit_result" not in ROLE_TEMPLATE


async def test_the_api_creates_a_group_skill_that_can_start(wf: Path) -> None:
    from arr.service.api_models import CreateSkillRequest

    api = RealApi(wf, base_url=BASE)
    result = await api.create_skill(
        CreateSkillRequest(name="runde-neu", kind="group", description="Zwei diskutieren")
    )
    assert result.skill.valid and result.skill.issues == []
    assert result.skill.topology is TopologyKind.PHASES and result.skill.phase_plan is not None
    await api.close()


async def test_the_mock_creates_the_same_kind_of_group() -> None:
    from arr.service.api_models import CreateSkillRequest
    from arr.service.mock_api import MockApi

    result = await MockApi().create_skill(
        CreateSkillRequest(name="runde-neu", kind="group", description="x")
    )
    assert result.skill.topology is TopologyKind.PHASES
    assert result.skill.channels_spec[0]["name"] == "runde"


async def test_the_choice_of_a_new_skill_names_phases(wf: Path) -> None:
    async with gui(wf) as (user, _api):
        await user.open("/skills")
        await see(user, "Neuer Skill")
        user.find(marker="new-skill").click()
        html = await see(user, "Startbare Research-Group in Phasen")
        assert "roundtable" not in html.split("Startbare Research-Group")[1][:200]


# --------------------------------------------------------------------------------------------
# An old group on the skills page
# --------------------------------------------------------------------------------------------


async def test_an_old_group_shows_its_message_on_the_skills_page(wf: Path) -> None:
    from arr.plan.legacy import old_topology_message

    old_start_skills(wf)
    async with gui(wf) as (user, _api):
        await user.open("/skills/proof-review")
        html = await see(user, "Ablauf (Topologie")
        message = old_topology_message("review")
        assert message is not None
        assert "Die Topologie „review“ gibt es nicht mehr." in html
        assert "Artefakt" not in html.split("Ablauf (Topologie")[1][:400], "no old flow"


# --------------------------------------------------------------------------------------------
# „Phase“ and „Stufe“, the escalation card, `run_results`, `run_summary`
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("phases", "word"), [(True, "Phase"), (False, "Stufe")])
def test_the_turn_separator_says_phase_in_a_phases_run(phases: bool, word: str) -> None:
    from arr.service.api_models import TranscriptEvent, TranscriptKind
    from arr.service.gui.components.transcript_event import event_html

    ev = TranscriptEvent(
        seq=1,
        ts=0.0,
        kind=TranscriptKind.TURN_START,
        turn=3,
        data={"stage": "beweis", "prompt": "Los."},
    )
    html = event_html(ev, agent_id="a", phases=phases)
    assert f" · {word} beweis" in html and (" · Stufe " in html) is (not phases)


def escalation(**data: Any) -> ApprovalItem:
    return ApprovalItem.model_validate(
        {
            "id": "apr_1",
            "type": "escalation",
            "status": "open",
            "created_at": 0.0,
            "run": {"id": "r", "name": "r", "project": "demo", "gui_url": "/runs/r"},
            "gui_url": "/freigaben/apr_1",
            "options": [{"key": "end", "label": "Run beenden", "help": ""}],
            "title": "x",
            **data,
        }
    )


@pytest.mark.parametrize(("topology", "mark"), [("phases", "D"), ("waterfall", "V")])
async def test_compact_card_marks_passes_with_d_in_a_phases_run(topology: str, mark: str) -> None:
    from test_gui import gui_user

    from arr.service.gui.components.approval_card import compact_card
    from arr.service.mock_api import MockApi

    item = escalation(
        topology=topology,
        attempts=[{"n": 1, "avg_score": 4.0, "pass_score": 6.0, "passed": False}],
    )
    async with gui_user(MockApi()) as user:
        await user.open("/")
        with user.client:
            card = compact_card(item, now=0.0)
            html = " ".join(str(getattr(c, "content", "")) for c in card.descendants())
    assert f'class="att fail">{mark}1 ' in html


async def test_the_escalation_card_reads_pass_and_reason_from_the_log_entry(tmp_path: Path) -> None:
    """§6.8: `escalation` carries `attempt` and `reason`; the card reads them from there, and
    from the first line of `summary` only for an entry without them (before W4b-06b)."""
    import json

    from phases_kit import Choose, answer, errors, fixture_manager, run
    from phases_ui_kit import real_api

    from arr.permissions.types import EscalationOption
    from arr.runs.orchestrator import ORCHESTRATOR_LOG

    workfolder = make_workfolder(tmp_path)
    agents = ["ideator", "explorer-1", "explorer-2", "strategist", "judge-1", "judge-2", "writer"]
    policies: dict[str, Any] = {a: answer() for a in [*agents, "prover"]}
    decider = Choose(*[EscalationOption.CONTINUE] * 4)
    mgr, _ = fixture_manager(workfolder, "beispiel", policies, decider=decider)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    log = mgr.run_dir / ORCHESTRATOR_LOG
    entries = [json.loads(x) for x in log.read_text("utf-8").split("\n") if x.strip()]
    first = next(e for e in entries if e["kind"] == "escalation")
    reason = "Gate „user“: Du entscheidest, wie es weitergeht."
    assert (first["data"]["attempt"], first["data"]["reason"]) == (1, reason)
    api = real_api(workfolder)
    [item] = [i for i in await api.list_approvals(run_id=mgr.run_dir.name, status=None) if i.stage][
        :1
    ]
    assert (item.attempt, item.phase_reason) == (1, reason)
    # the fields win over the summary; without them the summary counts (an older entry)
    first["data"]["summary"] = first["data"]["summary"].replace("Durchlauf 1.", "Durchlauf 9.")
    first["data"]["reason"] = "Aus dem Feld."
    log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), "utf-8")
    api = real_api(workfolder)
    [item] = [i for i in await api.list_approvals(run_id=mgr.run_dir.name, status=None) if i.stage][
        :1
    ]
    assert (item.attempt, item.phase_reason) == (1, "Aus dem Feld.")
    del first["data"]["attempt"], first["data"]["reason"]
    log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), "utf-8")
    api = real_api(workfolder)
    [item] = [i for i in await api.list_approvals(run_id=mgr.run_dir.name, status=None) if i.stage][
        :1
    ]
    assert (item.attempt, item.phase_reason) == (9, reason)


def test_run_results_and_run_summary_without_the_old_topologies(wf: Path) -> None:
    from service_kit import make_run_folder

    from arr.service.real_api.rundir import RunRecord, run_results, run_summary

    run_dir = make_run_folder(wf)
    (run_dir / "run.yaml").unlink()  # no `run.yaml` and no start line
    record = RunRecord(run_dir)
    record.refresh_meta()
    empty = run_results(record, None).empty_text or ""
    assert "submit_result" not in empty and "mit submit ab" in empty
    summary = run_summary(record, base_url=BASE, now=2.0)
    assert summary.topology is TopologyKind.PHASES, "the fallback is `phases`, not `solo`"

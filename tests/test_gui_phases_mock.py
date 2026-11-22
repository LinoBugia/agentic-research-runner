"""W4b-07 §6.17, A22: the mock data set „phasen“ with a phases skill and a phases run.

The skill is in every data set; the run, its open gate `user`, the tightened request, the
question in a private channel, the submissions and the run start of a second phases run are in
the data set „phasen“ (the scenario plus these runs), so the counters of the other data sets
stay as they are. The hand-made phase plan is checked against `real_api.phase_plan`.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from nicegui.element import Element
from nicegui.testing.user import User
from plan_kit import SETTINGS
from run_kit import make_settings
from test_gui import gui_user

from arr.models import GroupSpec, RunInput, RunStatus, TopologyKind
from arr.permissions.inbox import tightened_text
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecisionAction,
    EscalationOption,
    TightenedBy,
)
from arr.resolve import parse_yaml_text, resolve_run, validate_model
from arr.service.api_models import DecisionRequest, StageStatus
from arr.service.gui.components.approval_card import PHASE_HINT_LABEL
from arr.service.mock_api import (
    PHASES_PRIVATE,
    PHASES_SKILL,
    R_PHASES,
    R_PHASES_START,
    Dataset,
    MockApi,
)
from arr.service.real_api.approvals import _phase_option_infos
from arr.service.real_api.phase_plan import phase_plan


def _walk(element: Element) -> Iterator[Element]:
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def page_html(user: User) -> str:
    """Contents, texts and string props (placeholders, labels) of every element of the page."""
    with user.client:
        parts = []
        for node in _walk(user.client.layout):
            for value in (getattr(node, "content", None), getattr(node, "text", None)):
                if isinstance(value, str) and value:
                    parts.append(value)
            parts += [v for v in node._props.values() if isinstance(v, str)]
        return "\n".join(parts)


async def see(user: User, *texts: str, retries: int = 60) -> str:
    """Wait until every text is on the page (the page's timers fill it in) and return it."""
    html = ""
    for _ in range(retries):
        html = page_html(user)
        if all(t in html for t in texts):
            return html
        await asyncio.sleep(0.05)
    missing = [t for t in texts if t not in html]
    raise AssertionError(f"not on the page: {missing}\n{html[:3000]}")


@pytest.fixture
def api() -> MockApi:
    return MockApi(Dataset.PHASES)


@pytest.fixture
async def user(api: MockApi) -> AsyncIterator[User]:
    async with gui_user(api) as simulated:
        yield simulated


# --- data ------------------------------------------------------------------------------------


async def test_the_plan_of_the_mock_is_what_the_service_builds_from_its_group_yaml() -> None:
    """The hand-made rows equal `phase_plan` of the skill's `group.yaml` (decider aside: the
    test settings decide with `user`, the mock with the supervising agent)."""
    api = MockApi(Dataset.PHASES)
    skill = next(s for s in api.state.skills if s.name == PHASES_SKILL)
    text = (await api.get_skill_file(PHASES_SKILL, "group.yaml")).content
    data, lines = parse_yaml_text(text, "group.yaml")
    spec = validate_model(GroupSpec, dict(data), source="group.yaml")
    settings = make_settings().model_copy(update={"defaults": SETTINGS.defaults})
    run = resolve_run(RunInput(project="demo", task="t", skill=PHASES_SKILL), spec, settings, {})
    real = phase_plan(run, names=[p.name for p in spec.phases], lines=lines)
    assert real is not None and skill.phase_plan is not None
    skip = {"decider_text", "decider_user"}
    assert [r.model_dump(exclude=skip) for r in skill.phase_plan.rows] == [
        r.model_dump(exclude=skip) for r in real.rows
    ]
    assert skill.phase_plan.notes == real.notes
    deciders = {r.name: (r.decider_text, r.decider_user) for r in skill.phase_plan.rows}
    assert deciders["beweis"] == ("du (Phase)", True)
    assert deciders["ideen"] == ("Aufsichts-Agent, bei Zweifel du", False)
    for row in skill.phase_plan.rows:
        assert text.splitlines()[row.line - 1].strip() == f"- name: {row.name}"
    for path in skill.files:  # every file of the skill can be opened
        assert (await api.get_skill_file(PHASES_SKILL, path)).content


async def test_items_of_the_phases_run_are_worded_like_the_service(api: MockApi) -> None:
    gate = await api.get_approval("apr_0050")
    assert gate.type is ApprovalType.ESCALATION and gate.topology is TopologyKind.PHASES
    assert (gate.stage, gate.attempt) == ("ideen", 2)
    keys = [EscalationOption.CONTINUE, EscalationOption.RETRY, EscalationOption.END]
    assert gate.options == _phase_option_infos(keys, "ideen", 2, "exploration")
    request = await api.get_approval("apr_0051")
    assert request.tightened_by == {"source": "phase", "stage": "beweis", "attempt": 1}
    assert request.tightened_text == tightened_text(TightenedBy(**request.tightened_by))
    question = await api.get_approval("apr_0052")
    assert question.channel == PHASES_PRIVATE and question.msg_id == 2
    channels = {c.name: c for c in await api.list_channels(R_PHASES)}
    assert channels[PHASES_PRIVATE].private_of == "ideator-1"
    asked = (await api.get_messages(R_PHASES, PHASES_PRIVATE)).items[-1]
    assert asked.open and asked.to == ["user"] and asked.body == question.question_text
    start = await api.get_approval("apr_0053")
    assert start.phase_plan is not None and [d.path for d in start.deviations] == [
        "tightening.removed"
    ]
    assert start.deviations[0].sensitive
    assert not any(r.decider_user for r in start.phase_plan.rows)


async def test_other_data_sets_keep_their_runs_and_counters() -> None:
    scenario = MockApi()
    assert R_PHASES not in scenario.state.runs
    assert (await scenario.get_counts()).waiting == 3
    assert PHASES_SKILL in [s.name for s in await scenario.list_skills()]
    phases = MockApi(Dataset.PHASES)
    assert (await phases.list_runs(limit=1000)).total == 31
    assert {R_PHASES, R_PHASES_START} <= set(phases.state.runs)


async def test_answering_the_question_and_the_gate_in_the_mock(api: MockApi) -> None:
    answer = DecisionRequest(action=DecisionAction.ANSWER, comment="Ja, lass sie weg.")
    response = await api.decide_approval("apr_0052", answer)
    assert response.item.status is ApprovalStatus.DECIDED
    messages = (await api.get_messages(R_PHASES, PHASES_PRIVATE)).items
    assert not messages[1].open and messages[-1].body == "Ja, lass sie weg."
    retry = DecisionRequest(action=DecisionAction.OPTION, option_key=EscalationOption.RETRY)
    response = await api.decide_approval("apr_0050", retry)
    assert "„ideen“ läuft noch einmal" in response.message
    ideen = (await api.get_run(R_PHASES)).stages[0]
    assert ideen.status is StageStatus.RUNNING
    assert [(p.n, p.value, p.running) for p in ideen.passes] == [
        (1, "weiter", False),
        (2, "noch ein Versuch", False),
        (3, None, True),
    ]
    assert (await api.get_run(R_PHASES)).run.status is RunStatus.RUNNING


# --- screens ---------------------------------------------------------------------------------


async def test_skill_page_shows_the_phase_plan(user: User) -> None:
    await user.open(f"/skills/{PHASES_SKILL}")
    html = await see(user, 'data-mark="phase-plan"', "Phasenplan")
    for name in ("ideen", "exploration", "beweis", "zusammenfassung"):
        assert f'data-phase="{name}"' in html
    assert f"/skills/{PHASES_SKILL}?tab=yaml&amp;zeile=" in html
    assert "user: weiter, wiederholen mit Hinweis oder Stopp" in html
    assert "du (Phase)" in html


async def test_run_detail_shows_passes_plan_and_submissions(user: User) -> None:
    await user.open(f"/runs/{R_PHASES}")
    html = await see(user, "D1 weiter", "D2 läuft", "Ablauf in Phasen, 4 Phasen")
    assert "Kontext 48 % · verdichtet ab 60 %" in html
    assert "Kontext unbekannt" in html
    assert "davon 1 Notiz-Turn" in html
    assert "privat · ideator-1" in html
    await user.open(f"/runs/{R_PHASES}?ansicht=ablauf")
    html = await see(user, 'data-mark="run-phase-plan"', "Phasenplan des Runs")
    assert "2 / 3" in html and "bei dir" in html and "major" in html
    assert "Gate user: wartet auf deine Entscheidung" in html
    await user.open(f"/runs/{R_PHASES}?ansicht=ergebnisse")
    html = await see(user, "versiegelt bis zum Ende der Phase", "Phase beweis · Durchlauf 1")
    assert 'id="a008"' in html and 'id="a009"' in html  # the two sealed ones of ideen D2
    assert "verdict" in html and "objections" in html
    assert "Phase ideen · Durchlauf 2" in html


async def test_run_start_detail_renders(user: User) -> None:
    await user.open(f"/runs/{R_PHASES_START}")
    await see(user, "Ablauf in Phasen, 4 Phasen")


async def test_inbox_shows_gate_tightened_request_question_and_run_start(user: User) -> None:
    await user.open("/freigaben")
    html = await see(user, PHASE_HINT_LABEL, "Phase „ideen“ · Durchlauf 2")
    assert 'data-mark="escalation-summary"' in html
    assert "Gate „user“: Du entscheidest" in html
    assert "Warum bei dir" in html
    assert "Die Phase „beweis“ (Durchlauf 1) verlangt deine Entscheidung (decider: user)." in html
    assert "Vorher geprüft:" in html
    assert "privat · ideator-1" in html and "euren privaten Kanal" in html
    assert "Verschärfung aufgehoben (decider: user)" in html
    assert 'data-mark="run-start-hints"' in html


async def test_runs_list_shows_the_phases_run(user: User) -> None:
    await user.open("/runs")
    html = await see(user, "Zyklen mod 2ᵏ – von Ideen zum Beweis")
    assert "phases" in html

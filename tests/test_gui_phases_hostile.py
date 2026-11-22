"""W4b-07 §6.18, A20: agent text and text from the YAML in the new displays reach the page only
escaped. Every new field gets hostile values: control and format characters, U+2028, line
breaks, `<img onerror>`, `<script>`, `javascript:` links, very long text."""

import re

import pytest

from arr.service.api_models import (
    AgentDisplayState,
    AgentState,
    ApprovalItem,
    ApprovalRunRef,
    ApprovalType,
    EscalationOptionInfo,
    PassInfo,
    PhaseActionInfo,
    PhasePlan,
    PhaseRow,
    PhaseUsage,
    StageInfo,
    StageStatus,
    SubmissionInfo,
)
from arr.service.gui.components.agent_card import card_html, tooltip
from arr.service.gui.components.approval_card import phase_escalation_html, run_start_plan_html
from arr.service.gui.components.phase_table import phase_table_html
from arr.service.gui.components.stage_bar import pass_chip, stage_bar_html
from arr.service.gui.pages.run_detail_results import submissions_html
from arr.service.gui.texts import channel_label
from arr.service.real_api.phase_plan import one_line

HOSTILE = [
    '<img src=x onerror="alert(1)">',
    "<script>alert(1)</script>",
    '[klick](javascript:alert(1)) <a href="javascript:alert(1)">x</a>',
    "Zeile 1\nZeile 2\r\nZeile 3\u2028Zeile 4\u2029\x85Ende",
    "\x00\x07\x1b[31mrot\x1b[0m\u202eumgedreht\u200b",
    '" onmouseover="alert(1)" data-x="',
    "x" * 20_000,
]


def assert_safe(html: str) -> None:
    """No tag, handler or link from the input: every `<` of it is escaped."""
    assert "<img" not in html and "<script" not in html
    assert not re.search(r"<a [^>]*href=\"javascript:", html, re.I)
    assert not re.search(r"\son\w+=\"", html), "no event handler attribute"
    assert 'onmouseover="' not in html


@pytest.mark.parametrize("bad", HOSTILE)
def test_a20_phase_table(bad: str) -> None:
    row = PhaseRow(
        index=0,
        name=bad,
        topology="solo",
        topology_text=bad,
        actions=[PhaseActionInfo(key=bad, agents=[bad], task=bad, format=bad, name=bad)],
        standby=[bad],
        channels=[bad],
        context_text=bad,
        gate_text=bad,
        decider_text=bad,
        decider_user=True,
        line=3,
        visits=1,
        max_visits=2,
        gate_result=bad,
        usage=PhaseUsage(turns=2, tokens=10, cost_usd=0.1),
        estimate_text=bad,
    )
    plan = PhasePlan(rows=[row], dropped=[bad], notes=[bad], run_decider=bad)
    for html in (
        phase_table_html(plan, skill=bad, run=True),
        phase_table_html(plan, compact=True),
    ):
        assert_safe(html)


@pytest.mark.parametrize("bad", HOSTILE)
def test_a20_submissions(bad: str) -> None:
    sub = SubmissionInfo(
        id=1,
        ts=1.0,
        agent=bad,
        alias=bad,
        stage=bad,
        format=bad,
        name=bad,
        version=2,
        status=bad,
        summary=bad,
        fields={bad: bad, "objections": [{"text": bad}, bad], "nested": {bad: [bad]}},
        files=[bad],
        body_excerpt=bad,
        path=bad,
        score=7.5,
        sealed=True,
    )
    assert_safe(submissions_html([sub]))


@pytest.mark.parametrize("bad", HOSTILE)
def test_a20_escalation_and_run_start(bad: str) -> None:
    run = ApprovalRunRef(id="r", name=bad, project="p", gui_url="/runs/r")
    item = ApprovalItem(
        id="apr_1",
        type=ApprovalType.ESCALATION,
        created_at=1.0,
        gui_url="/freigaben/apr_1",
        title=bad,
        run=run,
        topology="phases",
        stage=bad,
        attempt=2,
        phase_reason=bad,
        summary_lines=[bad, bad],
        options=[EscalationOptionInfo(key="end", label=bad, help=bad)],
    )
    assert_safe(phase_escalation_html(item))
    plan = PhasePlan(
        rows=[PhaseRow(index=0, name="p", topology="solo", topology_text=bad, gate_text=bad)]
    )
    start = item.model_copy(
        update={"type": ApprovalType.RUN_START, "phase_plan": plan, "warnings": [bad]}
    )
    assert_safe(run_start_plan_html(start))


@pytest.mark.parametrize("bad", HOSTILE)
def test_a20_bar_card_and_channel(bad: str) -> None:
    chip = pass_chip(PassInfo(n=1, value=bad, passed=False))
    assert_safe(chip)
    stage = StageInfo(
        key=bad, index=1, label=bad, status=StageStatus.RUNNING, passes=[PassInfo(n=1, value=bad)]
    )
    assert_safe(stage_bar_html([stage], "phases"))
    agent = AgentState(
        id="a",
        role_label=bad,
        provider="claude",
        vendor="anthropic",
        model="m",
        model_label=bad,
        auth_profile="p",
        state=AgentDisplayState.IDLE,
        work_dir="/w",
        context_text=bad,
        note_turns=1,
    )
    assert_safe(card_html(agent, now=1.0))
    assert "Notiz-Turn" in tooltip(agent)
    label = channel_label(bad)
    assert "\n" not in label or bad.count("\n")  # a slug-less name stays as written, escaped later


@pytest.mark.parametrize("bad", HOSTILE)
def test_a20_texts_from_the_yaml_are_one_short_line(bad: str) -> None:
    text = one_line(bad)
    assert len(text) <= 120
    assert not re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e]", text)


HOSTILE_GROUP = """kind: research-group
version: 1
topology: phases
agents: [{ id: worker }]
phases:
  - name: arbeit
    topology: solo
    actions:
      worker:
        task: "tasks/<img src=x onerror=alert(1)>\u2028<script>x</script>.md"
        submission: { format: decision, name: "<b onclick=alert(1)>n</b>",
                      choices: ["<img src=y onerror=z>", "nein"] }
    gate:
      type: signal
      field: choice
      routes: { "<img src=y onerror=z>": next, nein: stop }
"""


def test_a20_hostile_names_in_a_resolved_plan_stay_text() -> None:
    """Task paths, names and values of `choices` come from the YAML (the user or Claude Code via
    `config`): the plan makes them one line, the table escapes them."""
    from plan_kit import SETTINGS
    from run_kit import make_settings

    from arr.models import GroupSpec, RunInput
    from arr.resolve import parse_yaml_text, resolve_run, validate_model
    from arr.service.real_api.phase_plan import phase_plan

    data, lines = parse_yaml_text(HOSTILE_GROUP, "group.yaml")
    spec = validate_model(GroupSpec, dict(data), source="group.yaml")
    settings = make_settings().model_copy(update={"defaults": SETTINGS.defaults})
    run = resolve_run(RunInput(project="demo", task="t", skill="fx"), spec, settings)
    plan = phase_plan(run, lines=lines)
    assert plan is not None
    [row] = plan.rows
    assert "\u2028" not in row.actions[0].task and "\n" not in row.gate_text
    html = phase_table_html(plan, skill="fx", run=True)
    assert_safe(html)
    assert "&lt;img" in html, "the text is there, escaped"

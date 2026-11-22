"""W4b-07: the service for runs and skills with `topology: phases` (A1–A4, A7, A11, A12, A18).

Phase plan for every fixture group, skills with hints of `check`, NG1 (task paths with a
placeholder in their text), every `TopologyKind` in the functions of the GUI, the fill level of
an agent's session, tightened requests in the inbox, the deviations of a run start and the
storage. Runs with FakeBackend agents are in `test_real_api_phases_runs.py`.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from phases_kit import FIXTURES
from phases_ui_kit import install_skill, real_api, write_fake_profiles
from plan_kit import SETTINGS
from run_kit import make_settings, make_workfolder

from arr.models import (
    Compact,
    Decider,
    GroupSpec,
    ResolvedRun,
    RunInput,
    TopologyKind,
)
from arr.permissions.inbox import tightened_text
from arr.permissions.types import TightenedBy, TightenedSource
from arr.resolve import parse_yaml_text, resolve_run, validate_model
from arr.service.api_models import (
    ApprovalItem,
    ApprovalRunRef,
    ApprovalType,
    EscalationOptionInfo,
    Severity,
    StageInfo,
    StageStatus,
)
from arr.service.real_api.phase_plan import phase_plan
from arr.service.real_api.phase_run import _AgentLog, context_fill
from arr.service.real_api.run_starts import tightening_deviations

FIXTURE_NAMES = sorted(p.stem for p in FIXTURES.glob("*.yaml"))


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def resolved_of(text: str, params: dict[str, Any] | None = None) -> tuple[ResolvedRun, Any]:
    data, lines = parse_yaml_text(text, "group.yaml")
    spec = validate_model(GroupSpec, dict(data), source="group.yaml")
    settings = make_settings().model_copy(update={"defaults": SETTINGS.defaults})
    run = resolve_run(RunInput(project="demo", task="t", skill="fx"), spec, settings, params or {})
    return run, (spec, lines)


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.yaml").read_text("utf-8")


# --------------------------------------------------------------------------------------------
# A2: the phase plan for every fixture
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_a2_every_fixture_has_a_plan_with_a_row_per_phase(name: str) -> None:
    run, (spec, lines) = resolved_of(fixture(name))
    plan = phase_plan(run, names=[p.name for p in spec.phases], lines=lines)
    assert plan is not None
    assert [r.name for r in plan.rows] == [p.name for p in run.phases]
    for row, phase in zip(plan.rows, run.phases, strict=True):
        assert row.index == phase.index and row.field == f"phases[{phase.index}]"
        assert row.line is not None and row.line >= 1, "jump into the group.yaml"
        assert text_at(fixture(name), row.line).lstrip().startswith(f"- name: {phase.name}")
        assert {a for action in row.actions for a in action.agents} == set(phase.agents)
        assert row.gate == phase.gate.type.value and row.gate_text
        assert row.decider_text and row.estimate_text
    assert sorted(plan.dropped) == sorted(
        {p.name for p in spec.phases} - {p.name for p in run.phases}
    )


def text_at(text: str, line: int) -> str:
    return text.splitlines()[line - 1]


def test_a2_the_example_plan_reads_like_the_table_of_docs_10() -> None:
    run, (spec, lines) = resolved_of(fixture("beispiel"))
    rows = {r.name: r for r in phase_plan(run, names=[p.name for p in spec.phases]).rows}
    assert rows["ideen"].gate_text == "user: weiter, wiederholen mit Hinweis oder Stopp"
    assert rows["exploration"].topology_text == "parallel, isoliert, über „ideen“"
    assert rows["beweis"].standby == ["strategist"] and rows["beweis"].channels == ["rueckfrage"]
    assert rows["beweis"].gate_text == "signal auf „status“: done → weiter, not_solved → Stopp"
    assert rows["pruefung"].context_text == "reset"
    assert rows["urteil"].gate_text.startswith("signal auf „verdict“ (Mehrheit): accept")
    assert "minor, major → revision" in rows["urteil"].gate_text
    assert rows["revision"].topology_text == "solo, höchstens 2×"
    assert rows["revision"].gate_text == "done → pruefung; danach → weiter"
    assert rows["zusammenfassung"].gate_text == "done → Ende"
    [prove] = rows["beweis"].actions
    assert (prove.key, prove.task, prove.format, prove.name) == (
        "prover",
        "tasks/beweis.md",
        "artifact",
        "beweis",
    )
    explore = rows["exploration"].actions[0]
    assert explore.key == "explorer-*" and explore.agents == ["explorer-1", "explorer-2"]


def test_a2_decider_per_phase_and_per_agent() -> None:
    text = fixture("beispiel").replace(
        "  - { id: writer,     role: roles/writer.md, model: claude-haiku-5-5 }",
        "  - { id: writer,     role: roles/writer.md, model: claude-haiku-5-5, decider: user }",
    )
    run, _ = resolved_of(text)
    run = run.model_copy(
        update={
            "approvals": run.approvals.model_copy(update={"decider": Decider.GATEKEEPER_THEN_USER})
        }
    )
    rows = {r.name: r for r in phase_plan(run).rows}
    assert (rows["beweis"].decider_text, rows["beweis"].decider_user) == ("du (Phase)", True)
    assert rows["zusammenfassung"].decider_text == (
        "du für writer; sonst Aufsichts-Agent, bei Zweifel du"
    )
    assert rows["ideen"].decider_text == "Aufsichts-Agent, bei Zweifel du"
    assert rows["ideen"].decider_user is False


def test_a2_dropped_phases_are_named_below_the_table() -> None:
    run, (spec, _) = resolved_of(fixture("review"), {"discussion_rounds": 0})
    plan = phase_plan(run, names=[p.name for p in spec.phases])
    assert [r.name for r in plan.rows] == ["fassung", "pruefung-allein", "revision"]
    assert plan.dropped == ["pruefung", "diskussion"]


def test_a2_moderated_shows_its_assignments() -> None:
    text = """kind: research-group
version: 1
topology: phases
agents: [{id: lead}, {id: helfer}]
phases:
  - name: arbeit
    topology: moderated
    max_assignments: 4
    actions:
      lead: { task: tasks/leiten.md, submission: { format: text } }
    standby: [helfer]
"""
    run, _ = resolved_of(text)
    [row] = phase_plan(run).rows
    assert row.topology_text == "moderated, höchstens 4 Aufträge"
    assert row.standby == ["helfer"]


# --------------------------------------------------------------------------------------------
# A1: every TopologyKind in every function of §6.1
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(TopologyKind))
def test_a1_every_topology_in_every_function(kind: TopologyKind) -> None:
    from arr.service.gui.components.stage_bar import stage_bar_html
    from arr.service.gui.pages.skills import flow_html
    from arr.service.gui.texts import TOPOLOGY, TOPOLOGY_LABEL

    assert TOPOLOGY[kind] and TOPOLOGY_LABEL[kind]
    assert "<ol" in flow_html(kind) or "Phasenplan" in flow_html(kind)
    stages = [StageInfo(key="a", index=1, label="a", status=StageStatus.RUNNING)]
    assert 'class="stagebar"' in stage_bar_html(stages, kind)
    item = ApprovalItem(
        id="apr_1",
        type=ApprovalType.ESCALATION,
        created_at=1.0,
        gui_url="/freigaben/apr_1",
        title="t",
        run=ApprovalRunRef(id="r", name="r", project="p", gui_url="/runs/r"),
        topology=kind,
        options=[EscalationOptionInfo(key="end", label="Run beenden", help="h")],
    )
    from types import SimpleNamespace

    from arr.service.gui.components.approval_card import ApprovalCard

    assert ApprovalCard._head(SimpleNamespace(item=item), 2.0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# A3, A4: hints of `check` never block; NG1
# --------------------------------------------------------------------------------------------

HINT = """kind: research-group
version: 1
topology: phases
agents: [{ id: worker, role: roles/worker.md }]
phases:
  - name: arbeit
    topology: solo
    on_exhausted: next
    actions:
      worker: { task: tasks/arbeit.md, submission: { format: text } }
"""


@pytest.fixture
def api(workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    write_fake_profiles(arr_home)
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return real_api(workfolder)


async def test_a3_a_hint_is_a_warning_and_blocks_nothing(workfolder: Path, api: Any) -> None:
    install_skill(workfolder, "hint", HINT)
    skill = await api.get_skill("fx-hint")
    assert skill.issues and {i.severity for i in skill.issues} == {Severity.WARNING}
    assert skill.valid, "a hint is no error (Entscheidungen 43, 44)"
    assert skill.phase_plan is not None and [r.name for r in skill.phase_plan.rows] == ["arbeit"]
    api.runs._refuse_invalid_skill("fx-hint")  # no ConfigError


async def test_a3_an_error_still_blocks(workfolder: Path, api: Any) -> None:
    from arr.errors import ConfigError

    install_skill(workfolder, "broken", HINT.replace("format: text", "format: unbekannt"))
    skill = await api.get_skill("fx-broken")
    assert not skill.valid
    assert any(i.severity is Severity.ERROR for i in skill.issues)
    with pytest.raises(ConfigError):
        api.runs._refuse_invalid_skill("fx-broken")


NG1 = """kind: research-group
version: 1
topology: phases
params:
  v: { default: eins }
  extra: { default: false }
agents: [{ id: worker, role: roles/worker.md }]
phases:
  - name: arbeit
    topology: solo
    actions:
      worker: { task: "tasks/{{ params.v }}.md", submission: { format: text } }
  - name: zusatz
    topology: solo
    when: { param: extra, equals: true }
    actions:
      worker: { task: "tasks/{{ params.v }}-gibtsnicht.md", submission: { format: text } }
"""


async def test_a4_a_task_with_a_placeholder_is_checked_with_the_defaults(
    workfolder: Path, api: Any
) -> None:
    skill = install_skill(workfolder, "ng1", NG1)
    (skill / "tasks").mkdir(exist_ok=True)
    (skill / "tasks" / "eins.md").write_text("Auftrag.", "utf-8")
    info = await api.get_skill("fx-ng1")
    assert not [i for i in info.issues if "Auftrag" in i.message], info.issues
    assert info.valid


async def test_a4_a_wrong_task_with_a_placeholder_is_reported(workfolder: Path, api: Any) -> None:
    install_skill(
        workfolder, "ng1", NG1.replace("tasks/{{ params.v }}.md", "aufgaben/{{ params.v }}.md")
    )
    info = await api.get_skill("fx-ng1")
    [issue] = [i for i in info.issues if "Auftrag" in i.message]
    assert "aufgaben/eins.md" in issue.message and issue.field == "phases[0].actions.worker.task"
    assert not info.valid


# --------------------------------------------------------------------------------------------
# A7: the fill level of the session
# --------------------------------------------------------------------------------------------


def turn_end(tokens: Any, window: Any) -> dict[str, Any]:
    return {"kind": "turn_end", "data": {"context_tokens": tokens, "context_window": window}}


@pytest.mark.parametrize(
    ("at", "tokens", "window", "log", "text"),
    [
        ("60%", 96_000, 200_000, None, "Kontext 48 % · verdichtet ab 60 %"),
        (120_000, 96_000, 200_000, None, "Kontext 48 % · verdichtet ab 60 %"),
        (120_000, 96_000, None, None, "Kontext 96 000 Token · verdichtet ab 120 000"),
        (
            "60%",
            96_000,
            None,
            None,
            "Kontext 96 000 Token · verdichtet ab 60 % (Fenster unbekannt)",
        ),
        ("60%", None, 200_000, None, "Kontext unbekannt"),
        ("60%", 96_000, 200_000, _AgentLog(no_fill=True), "Kontext unbekannt"),
        ("100%", 96_000, 200_000, None, "Kontext 48 % · Verdichten aus (at: 100 %)"),
        ("100%", None, None, None, "Verdichten aus (at: 100 %)"),
        (
            "60%",
            50_000,
            200_000,
            _AgentLog(compactions=2),
            "Kontext 25 % · verdichtet ab 60 % · 2× verdichtet",
        ),
    ],
)
def test_a7_fill_level(at: Any, tokens: Any, window: Any, log: Any, text: str) -> None:
    fill = context_fill(Compact(at=at), [turn_end(tokens, window)], log)
    assert fill is not None and fill.text == text


def test_a7_old_runs_without_a_measurement_show_nothing() -> None:
    assert context_fill(Compact(), [{"kind": "turn_end", "data": {"status": "ok"}}], None) is None
    assert context_fill(Compact(), [], None) is None


def test_a7_odd_values_never_break() -> None:
    for tokens, window in [("x", 3), ([1], {"a": 1}), (True, False), (-5, 0)]:
        fill = context_fill(Compact(), [turn_end(tokens, window)], None)
        assert fill is not None and fill.text == "Kontext unbekannt"


def test_a7_note_turns_are_counted_from_the_log() -> None:
    from arr.service.real_api.phase_run import agent_logs

    logs = agent_logs(
        [
            {"kind": "turn", "data": {"agent": "a", "turn": 3, "purpose": "compact"}},
            {"kind": "turn", "data": {"agent": "a", "turn": 4}},
            {"kind": "compact", "data": {"agent": "a", "reason": "auto"}},
            {"kind": "compact", "data": {"agent": "a", "reason": "engine"}},
            {"kind": "compact_no_fill", "data": {"agent": "b", "model": "m"}},
            {"kind": "compact", "data": ["kein", "objekt"]},
        ]
    )
    assert (logs["a"].note_turns, logs["a"].compactions, logs["a"].no_fill) == (1, 2, False)
    assert logs["b"].no_fill


# --------------------------------------------------------------------------------------------
# A11: tightened requests
# --------------------------------------------------------------------------------------------

REQUEST = {
    "agent_id": "prover",
    "tool": "Bash",
    "command": "pip install sympy",
    "categories": ["install"],
    "reason": "Ich brauche sympy.",
}


def write_items(run_dir: Path, lines: list[dict[str, Any]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "approvals.jsonl").open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


def open_line(item_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "event": "open",
        "ts": 1.0,
        "item_id": item_id,
        "request": REQUEST,
        "decision": {"action": "ask", "reason": "Installation", "risk": {"level": "medium"}},
        **extra,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"source": "agent"}, TightenedBy(source=TightenedSource.AGENT)),
        (
            {"source": "phase", "stage": "beweis", "attempt": 2},
            TightenedBy(source=TightenedSource.PHASE, stage="beweis", attempt=2),
        ),
        ({"source": "unknown"}, TightenedBy(source=TightenedSource.UNKNOWN)),
        ({"source": "erfunden"}, TightenedBy(source=TightenedSource.UNKNOWN)),
        ({"source": "phase"}, TightenedBy(source=TightenedSource.UNKNOWN)),
        (
            {"source": "phase", "stage": "<b>x</b>", "attempt": 1},
            TightenedBy(source=TightenedSource.UNKNOWN),
        ),
        (
            {"source": "phase", "stage": "beweis", "attempt": "2"},
            TightenedBy(source=TightenedSource.UNKNOWN),
        ),
        ("kein objekt", TightenedBy(source=TightenedSource.UNKNOWN)),
    ],
)
def test_a11_why_you_is_the_sentence_of_the_terminal(raw: Any, expected: TightenedBy) -> None:
    from arr.service.real_api.approvals import tightened_of

    found = tightened_of(open_line("apr_x_0001", tightened_by=raw))
    assert found == expected
    assert tightened_text(found) == tightened_text(expected)


def test_a11_without_the_field_nothing_changes() -> None:
    from arr.service.real_api.approvals import tightened_of

    assert tightened_of(open_line("apr_x_0001")) is None


# --------------------------------------------------------------------------------------------
# A12: deviations of a run start
# --------------------------------------------------------------------------------------------


def test_a12_removing_a_tightening_is_sensitive() -> None:
    standard, _ = resolved_of(fixture("beispiel"))
    no_phase = fixture("beispiel").replace(
        "    decider: user                      # hier", "    # decider weg               # hier"
    )
    requested, _ = resolved_of(no_phase)
    [dev] = tightening_deviations(requested, standard, standard)
    assert (dev.path, dev.sensitive) == ("tightening.removed", True)
    assert dev.value_text == "für Phase beweis"


def test_a12_an_agent_setting_gone_by_own_agents_is_sensitive() -> None:
    text = fixture("beispiel").replace(
        "  - { id: writer,     role: roles/writer.md, model: claude-haiku-5-5 }",
        "  - { id: writer,     role: roles/writer.md, model: claude-haiku-5-5, decider: user }",
    )
    standard, _ = resolved_of(text)
    requested, _ = resolved_of(fixture("beispiel"))
    devs = tightening_deviations(requested, standard, standard)
    [dev] = [d for d in devs if d.path == "tightening.removed"]
    assert dev.sensitive and dev.value_text == "für Agent writer"


DISCUSSION_USER = fixture("review").replace(
    "  - name: diskussion\n    topology: chat\n",
    "  - name: diskussion\n    topology: chat\n    decider: user\n",
)


def test_a12_a_phase_dropped_by_the_requests_parameters_counts_as_removed() -> None:
    """The defaults keep the phase; the request's parameters drop it (B1, W4b-07.1)."""
    defaults, _ = resolved_of(DISCUSSION_USER)
    standard, _ = resolved_of(DISCUSSION_USER, {"discussion_rounds": 0})
    requested, _ = resolved_of(DISCUSSION_USER, {"discussion_rounds": 0})
    [dev] = tightening_deviations(requested, standard, defaults)
    assert dev.sensitive and dev.value_text == "für Phase diskussion"


def test_a12_a_phase_the_defaults_drop_is_no_deviation() -> None:
    """B1: the skill's defaults already drop the phase; an unchanged request is no alarm."""
    text = DISCUSSION_USER.replace(
        "  discussion_rounds:\n    default: 1", "  discussion_rounds:\n    default: 0"
    )
    assert "default: 0" in text
    defaults, _ = resolved_of(text)
    assert "diskussion" not in {p.name for p in defaults.phases}
    requested, _ = resolved_of(text)
    assert tightening_deviations(requested, defaults, defaults) == []


def test_a12_an_added_tightening_is_named_but_not_sensitive() -> None:
    standard, _ = resolved_of(fixture("solo"))
    added = fixture("solo").replace("  - name: arbeit\n", "  - name: arbeit\n    decider: user\n")
    requested, _ = resolved_of(added)
    [dev] = tightening_deviations(requested, standard, standard)
    assert (dev.path, dev.sensitive, dev.value_text) == (
        "tightening.added",
        False,
        "für Phase arbeit",
    )


def test_a12_the_same_settings_give_no_deviation() -> None:
    standard, _ = resolved_of(fixture("beispiel"))
    requested, _ = resolved_of(fixture("beispiel"))
    assert tightening_deviations(requested, standard, standard) == []


# --------------------------------------------------------------------------------------------
# A18: storage
# --------------------------------------------------------------------------------------------


def test_a18_retired_and_scratch_are_no_protocols(tmp_path: Path) -> None:
    from arr.service.real_api.rundir import _measure

    run = tmp_path / "run"
    (run / "retired" / "a" / "001-p").mkdir(parents=True)
    (run / "retired" / "a" / "001-p" / "notes.md").write_text("x" * 5000, "utf-8")
    (run / "scratch" / "a").mkdir(parents=True)
    (run / "scratch" / "a" / "big.bin").write_bytes(b"0" * 7000)
    (run / "orchestrator.jsonl").write_text("{}\n", "utf-8")
    (run / "output").mkdir()
    (run / "output" / "summary.md").write_text("y" * 10, "utf-8")
    logs, output = _measure(run)
    assert (logs, output) == (3, 10)


def test_a18_the_slim_message_names_retired_scratch_and_tmp(tmp_path: Path) -> None:
    from arr.runs.slim import SlimPlan
    from arr.service.real_api.storage import _preview

    run = tmp_path / "run"
    targets = (
        run / "base",
        run / "retired",
        run / "scratch",
        run / "tmp",
        run / "agents" / "a" / "work",
    )
    text = _preview(SlimPlan(targets=targets, venv=None, logical_bytes=10))
    assert "Arbeitskopien, Snapshot, abgelegte Arbeitsordner, Scratch-Ordner und Run-tmp" in text
    plain = _preview(SlimPlan(targets=(run / "base",), venv=None, logical_bytes=10))
    assert "Arbeitskopien und Snapshot." in plain


def test_a2_plan_for_a_run_without_phases_is_none() -> None:
    from arr.models import ApprovalSettings, Limits, TopologyConfig

    run = ResolvedRun(
        project="p",
        topology=TopologyConfig(kind=TopologyKind.SOLO),
        limits=Limits(),
        approvals=ApprovalSettings(),
        agents=[],
    )
    assert phase_plan(run) is None

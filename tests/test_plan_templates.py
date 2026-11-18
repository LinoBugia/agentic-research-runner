"""W4b-02a A15: the five old topologies as `phases` groups (docs/10 §5).

The test data in `tests/fixtures/phases/` use the parameters of today's start skills (`solo`
those of its topology). Each passes the check with its defaults; each limit of a parameter has
a test. One limit cannot be expressed and is asserted as such: `n_rankers: 0` in `exploration`
(a gate `judge` without raters; the value goes away in W4b-06, Entscheidung 42). `exchange:
digest` with `turns_per_idea: 1` passes with a hint (Entscheidung 43). No model, no run.
"""

import pytest
from plan_kit import TEMPLATES, findings, load

from arr.models import GateRule, RouteKeyword


@pytest.mark.parametrize("name", TEMPLATES)
def test_a15_every_template_passes_with_its_defaults(name: str) -> None:
    found = findings(load(name))
    assert found.errors == [] and found.warnings == [], [str(i) for i in found.errors]


LIMITS = [
    # template, parameters, phases of the resolved run
    ("solo", {"max_turns": 1}, ["arbeit"]),
    ("solo", {"max_turns": 20}, ["arbeit"]),
    ("roundtable", {"rounds": 1}, ["diskussion", "zusammenfassung"]),
    ("roundtable", {"n_diskutanten": 5}, ["diskussion", "zusammenfassung"]),
    ("roundtable", {"summarizer": ""}, ["diskussion"]),
    ("waterfall", {"max_attempts": 1}, ["strategie", "skizze", "beweis"]),
    ("waterfall", {"on_fail": "stop"}, ["strategie", "skizze", "beweis"]),
    ("waterfall", {"on_fail": "escalate"}, ["strategie", "skizze", "beweis"]),
    ("waterfall", {"n_raters": 1, "pass_score": 10}, ["strategie", "skizze", "beweis"]),
    ("review", {"max_revisions": 0}, ["fassung", "pruefung", "diskussion", "revision"]),
    ("review", {"max_revisions": 2}, ["fassung", "pruefung", "diskussion", "revision"]),
    ("review", {"judges": 1}, ["fassung", "pruefung", "diskussion", "revision"]),
    ("review", {"judges": 5}, ["fassung", "pruefung", "diskussion", "revision"]),
    ("review", {"discussion_rounds": 0}, ["fassung", "pruefung-allein", "revision"]),
    ("review", {"artifact": "tex/beweis.tex"}, ["pruefung", "diskussion", "revision"]),
    ("review", {"judge_memory": "keep", "verdict": "unanimous"}, None),
    (
        "review",
        {"discussion_rounds": 0, "max_revisions": 0, "judges": 1, "artifact": "b.tex"},
        ["pruefung-allein", "revision"],
    ),
    ("exploration", {"ideas": ["Induktion", "Widerspruch"]}, ["exploration", "zusammenfassung"]),
    ("exploration", {"exchange": "digest"}, ["ideen", "exploration-digest", "zusammenfassung"]),
    ("exploration", {"exchange": "channel"}, ["ideen", "exploration-kanal", "zusammenfassung"]),
    ("exploration", {"turns_per_idea": 1}, ["ideen", "exploration", "zusammenfassung"]),
    ("exploration", {"n_explorers": 1}, ["ideen", "exploration", "zusammenfassung"]),
    ("exploration", {"n_explorers": 6, "n_rankers": 3}, None),
    ("exploration", {"summarizer": ""}, ["ideen", "exploration"]),
]


@pytest.mark.parametrize(("name", "params", "phases"), LIMITS)
def test_a15_limits_of_the_parameters(name: str, params: dict, phases: list[str] | None) -> None:
    found = findings(load(name), params)
    assert found.errors == [], [str(i) for i in found.errors]
    assert found.resolved is not None
    if phases is not None:
        assert [p.name for p in found.resolved.phases] == phases


def test_a15_review_limits_in_detail() -> None:
    resolved = findings(load("review"), {"max_revisions": 0}).resolved
    assert resolved is not None and resolved.phase("revision").max_visits == 0
    seeded = findings(load("review"), {"artifact": "tex/beweis.tex"}).resolved
    assert seeded is not None and seeded.seed["beweis"].file == "tex/beweis.tex"
    alone = findings(load("review"), {"discussion_rounds": 0}).resolved
    assert alone is not None
    # the revision goes back to the review that stays (Entscheidung 37)
    assert alone.phase("revision").gate.routes["done"].goto == "pruefung-allein"
    assert alone.phase("pruefung-allein").gate.rule is GateRule.MAJORITY
    keep = findings(load("review"), {"judge_memory": "keep"}).resolved
    assert keep is not None and set(keep.phase("pruefung").folder.values()) == {"keep"}
    strict = findings(load("review"), {"verdict": "unanimous"}).resolved
    assert strict is not None and strict.phase("diskussion").gate.rule is GateRule.UNANIMOUS


def test_a15_waterfall_limits_in_detail() -> None:
    resolved = findings(load("waterfall"), {"max_attempts": 1, "on_fail": "stop"}).resolved
    assert resolved is not None
    beweis = resolved.phase("beweis")
    assert (beweis.max_visits, beweis.on_exhausted) == (1, RouteKeyword.STOP)
    assert beweis.gate.pass_score == 7 and beweis.gate.raters == ["rater-1", "rater-2"]


def test_a15_what_cannot_be_expressed() -> None:
    no_rankers = findings(load("exploration"), {"n_rankers": 0})
    assert "phases[1].gate.raters" in no_rankers.paths


def test_a15_digest_with_one_pass_is_a_hint() -> None:
    digest = findings(load("exploration"), {"exchange": "digest", "turns_per_idea": 1})
    assert digest.errors == []
    assert [w.path for w in digest.warnings] == ["phases[2].inputs[0].peers"]

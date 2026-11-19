"""W4b-05b: the pure part of auto-compact (`arr.runs.compact`) – decision, note, package.

A3 (the rule as a pure function), A7 (the rebuild package: parts, limits, cut, cleaned agent
text), A13 (no input or output, no import from room, service, backends).
"""

import ast
from pathlib import Path

import pytest

from arr.models import Compact
from arr.runs import compact as module
from arr.runs.compact import (
    ACTION_MAX_CHARS,
    MAX_NOTE_FAILURES,
    NOTE_HEADING,
    NOTE_MAX_CHARS,
    PACKAGE_HEADING,
    PACKAGE_MAX_CHARS,
    QUESTIONS_MAX,
    SUBMISSIONS_MAX,
    AgentContext,
    OwnQuestion,
    OwnResult,
    OwnSubmission,
    Verdict,
    decide,
    note_from_text,
    rebuild_package,
    threshold,
)
from arr.workfolder import read_template

SIXTY = Compact()  # the built-in default, 60 %


# --------------------------------------------------------------------------------------------
# A3: the decision
# --------------------------------------------------------------------------------------------


def test_a3_share_and_tokens() -> None:
    assert SIXTY.at == "60%"
    assert threshold(SIXTY, 1_000_000) == 600_000
    assert threshold(SIXTY, 258_400) == 155_040
    assert threshold(Compact(at="33%"), 1_000) == 330
    assert threshold(Compact(at="1%"), 150) == 2  # rounded up, never below the share
    assert threshold(Compact(at=5_000), None) == 5_000
    assert threshold(SIXTY, None) is None


@pytest.mark.parametrize(
    ("at", "tokens", "window", "verdict"),
    [
        ("60%", 599_999, 1_000_000, Verdict.BELOW),
        ("60%", 600_000, 1_000_000, Verdict.COMPACT),  # exactly at the threshold
        ("60%", 900_000, 1_000_000, Verdict.COMPACT),
        ("60%", 900_000, None, Verdict.NO_WINDOW),  # a share needs the window
        (5_000, 4_999, None, Verdict.BELOW),  # tokens work without a window
        (5_000, 5_000, None, Verdict.COMPACT),
        (5_000, 5_000, 1_000_000, Verdict.COMPACT),
        ("60%", None, 1_000_000, Verdict.UNKNOWN),  # new session: no second compaction
        (5_000, None, None, Verdict.UNKNOWN),
    ],
)
def test_a3_decide(at: str | int, tokens: int | None, window: int | None, verdict: Verdict) -> None:
    decision = decide(Compact(at=at), tokens=tokens, window=window, turns_left=None)
    assert decision.verdict is verdict
    assert decision.compact is (verdict is Verdict.COMPACT)
    assert (decision.tokens, decision.window) == (tokens, window)


@pytest.mark.parametrize("tokens", [1, 999_999, 1_000_000, 5_000_000])
def test_a3_at_100_percent_turns_auto_compact_off(tokens: int) -> None:
    """A fixed rule (Entscheidung 49), not a result of the arithmetic: also at a full window."""
    off = Compact(at="100%")
    assert decide(off, tokens=tokens, window=1_000_000, turns_left=None).verdict is Verdict.OFF
    assert decide(off, tokens=tokens, window=None, turns_left=None).verdict is Verdict.OFF
    # counter-check: 99 % compacts at a full window
    near = Compact(at="99%")
    assert decide(near, tokens=1_000_000, window=1_000_000, turns_left=None).compact


def test_a3_turn_quota_and_failures() -> None:
    def verdict(turns_left: int | None, failures: int = 0) -> Verdict:
        decision = decide(
            Compact(at=10), tokens=20, window=None, turns_left=turns_left, failures=failures
        )
        return decision.verdict

    assert verdict(2) is Verdict.COMPACT  # note turn + working turn fit
    assert verdict(1) is Verdict.NO_TURNS
    assert verdict(0) is Verdict.NO_TURNS
    assert verdict(None) is Verdict.COMPACT
    assert verdict(None, MAX_NOTE_FAILURES - 1) is Verdict.COMPACT
    assert verdict(None, MAX_NOTE_FAILURES) is Verdict.GAVE_UP
    assert MAX_NOTE_FAILURES == 2


def test_a3_agent_context_forgets_on_a_new_session() -> None:
    ctx = AgentContext()
    ctx.measured(700, 1_000)
    ctx.session = {"session_id": "s1"}
    ctx.failures = 1
    ctx.follow({"session_id": "s1"})
    assert (ctx.tokens, ctx.failures) == (700, 1)
    ctx.follow({"session_id": "s2"})  # `reset_session` in between
    assert (ctx.tokens, ctx.window, ctx.failures) == (None, 1_000, 0)
    ctx.measured(None, None)  # a turn without a request: unknown stays unknown, window stays
    assert (ctx.tokens, ctx.window) == (None, 1_000)
    ctx.saw_window(2_000)
    ctx.saw_window(True)  # not a number
    assert ctx.window == 2_000


def test_a3_running_action() -> None:
    ctx = AgentContext()
    assert ctx.start_action("explore", None, "Erster Auftrag")
    assert not ctx.start_action("explore", None, "Fortsetzung")
    assert ctx.action == "Erster Auftrag"
    assert ctx.start_action("explore", 2, "Zweiter Durchlauf")  # a new attempt
    assert ctx.start_action("review", 2, "Andere Stufe")
    assert ctx.action == "Andere Stufe"


# --------------------------------------------------------------------------------------------
# Note
# --------------------------------------------------------------------------------------------


def test_note_is_cleaned_cut_and_none_when_empty() -> None:
    assert note_from_text(None) is None
    assert note_from_text("  \n​ ") is None
    assert note_from_text("### Stand‮\nok weiter") == "### Stand\nok\nweiter"
    long = note_from_text("x" * (NOTE_MAX_CHARS * 2))
    assert long is not None and len(long) <= NOTE_MAX_CHARS and long.endswith("[… gekürzt]")


def test_note_prompt_template() -> None:
    prompt = read_template("prompts", "compact_note.md")
    assert prompt.startswith(NOTE_HEADING)
    for section in (
        "### Stand",
        "### Offene Punkte",
        "### Nächste Schritte",
        "### Wichtige Dateien",
    ):
        assert section in prompt
    assert "keine Werkzeuge" in prompt


# --------------------------------------------------------------------------------------------
# A7: rebuild package
# --------------------------------------------------------------------------------------------

HEAD = read_template("prompts", "compact_rebuild.md")


def submission(n: int, **fields: object) -> OwnSubmission:
    data: dict[str, object] = {
        "id": n,
        "format": "proof",
        "status": "done",
        "summary": f"Kurzfassung {n}",
        "path": f"/run/output/submissions/{n}",
    }
    data.update(fields)
    return OwnSubmission(**data)  # type: ignore[arg-type]


def test_a7_package_parts_in_order() -> None:
    text = rebuild_package(
        HEAD,
        action="Beweise Lemma A.",
        submissions=[submission(3, name="beweis", version=2, stage="prove", attempt=2)],
        questions=[OwnQuestion(id=12, channel="main", text="Gilt n ≡ 1 mod 4?")],
        note="### Stand\nInduktion halb fertig.",
    )
    assert text.startswith(PACKAGE_HEADING)
    order = [
        "neu begonnen",
        "unverändert",
        "### Dein laufender Auftrag",
        "Beweise Lemma A.",
        "### Deine Abgaben",
        "#3 „beweis“ Fassung 2 · Format proof · Status done · Phase prove · Durchlauf 2",
        "Pfad: /run/output/submissions/3",
        "Kurzfassung: Kurzfassung 3",
        "### Deine offenen Fragen an den User",
        "#12 in #main: Gilt n ≡ 1 mod 4?",
        "### Deine eigene Notiz",
        "selbst geschrieben",
        "Induktion halb fertig.",
    ]
    positions = [text.index(part) for part in order]
    assert positions == sorted(positions), text


def test_a7_action_below_and_old_results() -> None:
    text = rebuild_package(
        HEAD,
        action="egal",
        action_below=True,
        results=[OwnResult(id=4, kind="exploration", title="Ansatz B", path="/run/out/4.md")],
    )
    assert "Er steht unten" in text and "egal" not in text
    assert "#4 · exploration · „Ansatz B“" in text and "Pfad: /run/out/4.md" in text
    assert "### Deine eigene Notiz" not in text  # without a note (compact_session(note=None))
    assert "### Deine offenen Fragen" not in text
    empty = rebuild_package(HEAD, action=None)
    assert "### Deine Abgaben\n\nKeine." in empty and "### Dein laufender Auftrag" not in empty


def test_a7_limits_and_cut() -> None:
    many = [submission(n, summary="s" * 2_000) for n in range(1, 60)]
    questions = [OwnQuestion(id=n, channel="main", text="q" * 2_000) for n in range(1, 30)]
    text = rebuild_package(
        HEAD,
        action="a" * (ACTION_MAX_CHARS * 3),
        submissions=many,
        questions=questions,
        note="n" * (NOTE_MAX_CHARS * 3),
    )
    assert len(text) <= PACKAGE_MAX_CHARS
    assert text.count("[… gekürzt]") >= 3  # action, note, the summaries say they are cut
    assert "ältere Abgaben weggelassen" in text and "ältere Fragen weggelassen" in text
    shown = [line for line in text.splitlines() if line.startswith("- #")]
    assert len([line for line in shown if "Format" in line]) <= SUBMISSIONS_MAX
    assert len([line for line in shown if " in #main:" in line]) <= QUESTIONS_MAX
    assert "#59 " in text and "#1 " not in text  # the newest are kept


def test_a7_agent_text_is_cleaned() -> None:
    hidden = "‮​\U000e0041\x07"
    text = rebuild_package(
        HEAD,
        action=f"Auftrag{hidden}",
        submissions=[submission(1, summary=f"Kurz{hidden}\nzweite Zeile", name=f"n{hidden}")],
        questions=[OwnQuestion(id=2, channel="main", text=f"Frage{hidden}")],
        note=f"Notiz{hidden}",
    )
    for char in hidden:
        assert char not in text
    assert "Kurzfassung: Kurz zweite Zeile" in text  # one line per entry
    assert "„n“" in text and "Frage" in text and "Notiz" in text


# --------------------------------------------------------------------------------------------
# A13: no input or output
# --------------------------------------------------------------------------------------------


def test_a13_compact_module_does_no_input_or_output() -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name if isinstance(node, ast.Import) else str(node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    banned = {"os", "pathlib", "subprocess", "socket", "shutil", "sqlite3", "io", "importlib"}
    assert not {name.split(".")[0] for name in imported} & banned, imported
    assert not any(
        name.startswith(("arr.room", "arr.service", "arr.backends", "arr.workfolder"))
        for name in imported
    ), imported
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"open", "eval", "exec", "compile", "__import__", "print", "input"}

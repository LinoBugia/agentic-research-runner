"""Formats and the check of `submit` without a room (W4b-01 A1–A4, A8).

`arr.submissions.formats` describes the formats as data, `arr.submissions.check` checks one
call against its expectation. Both do no input or output; the check is linear in its input.
"""

import ast
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import arr.submissions.check as check_module
import arr.submissions.formats as formats_module
from arr.models import Rubric
from arr.submissions.check import (
    MAX_BODY_CHARS,
    MAX_DEPTH,
    MAX_FIELDS_CHARS,
    MAX_FILES,
    MAX_NODES,
    MAX_SUMMARY_CHARS,
    SubmissionError,
    check_submission,
)
from arr.submissions.formats import (
    BUILTIN,
    RESERVED_NAMES,
    Expectation,
    FieldKind,
    FieldSpec,
    FormatSpec,
    format_for,
)


def expect(**data: Any) -> Expectation:
    return Expectation.model_validate(data)


def ok(fmt: str | dict[str, Any], fields: Any = None, **head: Any) -> Any:
    expectation = expect(format=fmt) if isinstance(fmt, str) else expect(**fmt)
    args = {"status": "done", "summary": "Kurz gesagt.", **head}
    return check_submission(**args, fields=fields, expectation=expectation)


def fails(fmt: str | dict[str, Any], fields: Any = None, **head: Any) -> str:
    with pytest.raises(SubmissionError) as info:
        ok(fmt, fields, **head)
    return str(info.value)


# --------------------------------------------------------------------------------------------
# A1: formats as data
# --------------------------------------------------------------------------------------------


def test_six_builtin_formats_and_four_public_kinds() -> None:
    assert list(BUILTIN) == ["text", "artifact", "review", "rating", "list", "decision"]
    assert {k.value for k in formats_module.PUBLIC_KINDS} == {"choice", "number", "text", "list"}
    review = BUILTIN["review"].fields
    assert review["verdict"].choices == ["accept", "minor", "major", "reject"]
    assert set(review["objections"].fields or {}) == {"text", "ref", "status"}
    assert set(BUILTIN["rating"].fields["ratings"].fields or {}) == {
        "ref",
        "score",
        "criteria",
        "comment",
    }
    assert set(BUILTIN["list"].fields["items"].fields or {}) == {"title", "text"}
    assert BUILTIN["text"].content and BUILTIN["artifact"].needs_files
    assert set(BUILTIN["decision"].fields) == {"choice"}


@pytest.mark.parametrize("module", [formats_module, check_module])
def test_format_modules_do_no_input_or_output(module: Any) -> None:
    """No file, process, network or room import in the two modules (A1)."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] if isinstance(node, ast.Import) else str(node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    banned = {"os", "pathlib", "subprocess", "socket", "shutil", "sqlite3", "io", "open"}
    assert not imported & banned, imported
    assert not any(name.startswith(("arr.room", "arr.runs", "arr.service")) for name in imported)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"open", "eval", "exec", "compile", "__import__"}


def test_own_format_from_the_four_kinds() -> None:
    spec = FormatSpec.model_validate(
        {
            "fields": {
                "ansatz": {"type": "choice", "choices": ["induktion", "widerspruch"]},
                "schritte": {"type": "number", "min": 1, "max": 20, "integer": True},
                "notiz": {"type": "text", "max_chars": 30, "required": False},
                "lemmata": {
                    "type": "list",
                    "max_items": 3,
                    "fields": {
                        "name": {"type": "text"},
                        "sicher": {"type": "choice", "choices": ["ja", "nein"]},
                    },
                },
                "tags": {"type": "list", "item": {"type": "text", "max_chars": 10}},
            }
        }
    )
    assert spec.kinds() == {FieldKind.CHOICE, FieldKind.NUMBER, FieldKind.TEXT, FieldKind.LIST}
    checked = ok(
        {"format": "plan", "spec": spec.model_dump()},
        {
            "ansatz": "induktion",
            "schritte": 4.0,
            "lemmata": [{"name": "A", "sicher": "ja"}],
            "tags": ["kurz"],
        },
    )
    assert checked.fields["schritte"] == 4 and isinstance(checked.fields["schritte"], int)
    assert "notiz" not in checked.fields


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"type": "choice", "choices": ["a"], "max_chars": 3}, "passt nicht zu einem Feld"),
        ({"type": "number", "min": 5, "max": 1}, "größer als „max“"),
        ({"type": "list"}, "genau eins von „item“ und „fields“"),
        ({"type": "list", "item": {"type": "list", "item": {"type": "text"}}}, "keine Liste"),
        ({"type": "text", "max_chars": 10**9}, "less than or equal"),
        ({"type": "choice", "choices": ["a", "a"]}, "doppelt"),
        ({"type": "zahl"}, "Input should be"),
    ],
)
def test_broken_field_specs_are_rejected(field: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        FieldSpec.model_validate(field)


def test_own_format_may_not_use_internal_kinds_or_head_rules() -> None:
    scores = {"fields": {"s": {"type": "scores", "choices": ["x"]}}}
    with pytest.raises(ValidationError, match="nur choice, number, text und list"):
        expect(format="eigen", spec=scores)
    with pytest.raises(ValidationError, match="nur „fields“"):
        expect(format="eigen", spec={"needs_files": True})
    with pytest.raises(ValidationError, match="braucht „choices“"):
        expect(format="eigen", spec={"fields": {"c": {"type": "choice"}}})


# --------------------------------------------------------------------------------------------
# A3: the expectation
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"format": "eigen"}, "braucht „spec“"),
        ({"format": "review", "spec": {"fields": {}}}, "eingebautes Format"),
        ({"format": "decision"}, "braucht „choices“"),
        ({"format": "rating"}, "braucht „rate“"),
        ({"format": "rating", "rate": [1, 1]}, "doppelt"),
        ({"format": "text", "name": "Beweis!"}, "Ungültige Bezeichnung"),
        ({"format": "text", "unbekannt": 1}, "unknown field"),
    ],
)
def test_invalid_expectations(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Expectation.model_validate(data)


@pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
def test_names_that_collide_in_output_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="schon belegt"):
        expect(format="artifact", name=name)
    expect(format="artifact", name=f"{name}-2")  # counter-check


# --------------------------------------------------------------------------------------------
# A2: every built-in format, valid and with its typical mistakes
# --------------------------------------------------------------------------------------------


def test_head_rules() -> None:
    checked = ok("text", body="Satz 1.​", summary=" Kurz.‮ ", note_next="weiter")
    assert (
        checked.summary == "Kurz." and checked.body == "Satz 1." and checked.note_next == "weiter"
    )
    assert ok("text", status="not_solved", summary="Kein Weg gefunden.").status == "not_solved"
    message = fails("text", status="fertig", summary="")
    assert "„status“: Erwartet: done oder not_solved. Bekommen: „fertig“." in message
    assert "„summary“: darf nicht leer sein." in message
    assert "„summary“" in fails("text", summary=None, body="x")
    long = "x" * (MAX_SUMMARY_CHARS + 1)
    message = fails("text", summary=long, body="x")
    assert f"zu lang ({MAX_SUMMARY_CHARS + 1} Zeichen, erlaubt {MAX_SUMMARY_CHARS})" in message
    assert long not in message, "no long input in the message"
    assert "„summary“: muss ein Text sein, bekommen eine Zahl." in fails(
        "text", summary=3, body="x"
    )


def test_text_needs_body_or_files_when_done() -> None:
    assert ok("text", body="Beweis.").body == "Beweis."
    assert ok("text", files=["a.md"]).files == ["a.md"]
    assert "Gib den Inhalt in „body“ an oder Dateien in „files“." in fails("text")
    assert "„fields.extra“: unbekanntes Feld. Erlaubt: keine." in fails(
        "text", {"extra": 1}, body="x"
    )


def test_artifact_needs_a_file_when_done() -> None:
    assert ok("artifact", files=["beweis.md"]).files == ["beweis.md"]
    assert "mindestens eine Datei" in fails("artifact", body="nur Text")
    assert ok("artifact", status="not_solved", summary="Lücke in Lemma 2.").files == []


def test_review_valid_and_typical_mistakes() -> None:
    fmt = {"format": "review", "known_objections": ["E1", 2]}
    checked = ok(
        fmt,
        {
            "verdict": "major",
            "objections": [
                {"text": "Basisfall fehlt."},
                {"ref": "E1", "status": "behoben", "text": "Ist jetzt drin."},
                {"ref": "2", "status": "strittig", "text": "Sehe ich anders."},
            ],
        },
    )
    assert checked.fields["verdict"] == "major" and len(checked.fields["objections"]) == 3
    message = fails(
        fmt,
        {
            "verdict": "gut",
            "objections": [
                {"id": 3, "text": "neu mit ID"},
                {"text": "neu", "status": "offen"},
                {"ref": "E9", "status": "offen", "text": "x"},
                {"ref": "E1", "text": "ohne Status"},
                {"ref": "E1", "status": "erledigt", "text": "x"},
            ],
        },
    )
    assert (
        "„fields.verdict“: Erwartet: einer von accept, minor, major, reject. Bekommen: „gut“."
        in message
    )
    assert "„fields.objections[0].id“: unbekanntes Feld. Erlaubt: text, ref, status." in message
    assert "„fields.objections[1].status“: Ein neuer Einwand hat keinen Status" in message
    assert "„fields.objections[2].ref“: Erwartet: einer von E1, 2. Bekommen: „E9“." in message
    assert "„fields.objections[3].status“: fehlt." in message
    assert "„fields.objections[4].status“: Erwartet: einer von offen, behoben, strittig." in message
    assert "„fields.verdict“: fehlt." in fails(fmt, {"objections": []})
    assert "„fields.objections“: fehlt." in fails(fmt, {"verdict": "accept"})
    duplicate = [{"ref": "E1", "status": "offen", "text": "a"}] * 2
    assert "steht schon in objections[0]" in fails(
        fmt, {"verdict": "minor", "objections": duplicate}
    )
    # without known objections no reference is possible
    assert "Erwartet: einer von (keine Werte)" in fails(
        "review",
        {"verdict": "minor", "objections": [{"ref": "E1", "status": "offen", "text": "a"}]},
    )


def test_list_valid_and_typical_mistakes() -> None:
    checked = ok("list", {"items": [{"title": "Idee A", "text": "Induktion."}]})
    assert checked.fields["items"][0]["title"] == "Idee A"
    assert "braucht mindestens 1 Einträge" in fails("list", {"items": []})
    assert "„fields.items[0].text“: fehlt." in fails("list", {"items": [{"title": "A"}]})
    assert "„fields.items[0].title“: ist zu lang" in fails(
        "list", {"items": [{"title": "x" * 201, "text": "a"}]}
    )
    too_many = [{"title": "t", "text": "x"}] * 51
    assert "hat zu viele Einträge (51, erlaubt 50)" in fails("list", {"items": too_many})
    assert "„fields.items“: Erwartet: eine Liste (title, text). Bekommen: ein Text." in fails(
        "list", {"items": "Idee A"}
    )


def test_decision_valid_and_typical_mistakes() -> None:
    fmt = {"format": "decision", "choices": ["weiter", "zurück"]}
    assert ok(fmt, {"choice": "weiter"}).fields == {"choice": "weiter"}
    assert "Erwartet: einer von weiter, zurück. Bekommen: „vielleicht“." in fails(
        fmt, {"choice": "vielleicht"}
    )
    assert "„fields.choice“: fehlt." in fails(fmt, {})
    assert "Bekommen: 1." in fails(fmt, {"choice": 1})


# --------------------------------------------------------------------------------------------
# A4: rating
# --------------------------------------------------------------------------------------------


def rating(ref: int, score: float, **extra: Any) -> dict[str, Any]:
    return {"ref": ref, "score": score, **extra}


def test_rating_every_submission_of_rate_exactly_once() -> None:
    fmt = {"format": "rating", "rate": [3, 5]}
    checked = ok(fmt, {"ratings": [rating(5, 7, comment="gut"), rating(3, 4)]})
    assert [r["ref"] for r in checked.fields["ratings"]] == [5, 3]
    # counter-checks: one missing, one twice, one that is not to be rated
    assert "Es fehlt eine Bewertung für #5." in fails(fmt, {"ratings": [rating(3, 4)]})
    message = fails(fmt, {"ratings": [rating(3, 4), rating(3, 5), rating(5, 1)]})
    assert "„fields.ratings[1].ref“: Die Abgabe #3 steht schon in ratings[0]." in message
    message = fails(fmt, {"ratings": [rating(3, 4), rating(5, 1), rating(9, 2)]})
    assert "„fields.ratings[2].ref“: #9 ist nicht zu bewerten. Zu bewerten: #3, #5." in message
    assert "Erwartet: eine ganze Zahl ab 1. Bekommen: 3.5." in fails(
        fmt, {"ratings": [rating(3.5, 4), rating(5, 1)]}
    )


def test_rating_scale_and_criteria_from_rubric_or_assignment() -> None:
    rubric = Rubric(scale=(0, 5), criteria=["Klarheit", "Tiefe"])
    fmt = {"format": "rating", "rate": [1]}

    def run(fields: Any, **extra: Any) -> Any:
        return check_submission(
            status="done",
            summary="s",
            fields=fields,
            expectation=expect(**fmt, **extra),
            rubric=rubric,
        )

    assert run({"ratings": [rating(1, 5, criteria={"Tiefe": 0})]}).fields["ratings"][0][
        "criteria"
    ] == {"Tiefe": 0.0}
    for score, text in (
        (6, "6 liegt außerhalb. Erwartet: eine Zahl von 0 bis 5."),
        (True, "Bekommen: true"),
    ):
        with pytest.raises(SubmissionError, match=text):
            run({"ratings": [rating(1, score)]})
    with pytest.raises(SubmissionError, match="unbekanntes Kriterium. Erlaubt: Klarheit, Tiefe."):
        run({"ratings": [rating(1, 3, criteria={"Witz": 2})]})
    with pytest.raises(SubmissionError, match="„fields.ratings\\[0\\].criteria.Tiefe“: „?9"):
        run({"ratings": [rating(1, 3, criteria={"Tiefe": 9})]})
    # Entscheidung 5: criteria of the assignment replace those of the rubric
    assert run({"ratings": [rating(1, 3, criteria={"Eleganz": 4})]}, criteria=["Eleganz"]).fields
    with pytest.raises(SubmissionError, match="Erlaubt: Eleganz"):
        run({"ratings": [rating(1, 3, criteria={"Tiefe": 4})]}, criteria=["Eleganz"])
    # without criteria anywhere none is accepted
    with pytest.raises(SubmissionError, match="keine Kriterien festgelegt"):
        check_submission(
            status="done",
            summary="s",
            fields={"ratings": [rating(1, 3, criteria={"Tiefe": 4})]},
            expectation=expect(**fmt),
        )
    assert format_for(expect(**fmt)).fields["ratings"].fields["score"].max == 10  # default scale


def test_scores_with_nan_and_infinity_are_rejected() -> None:
    fmt = {"format": "rating", "rate": [1]}
    for value in (float("nan"), float("inf")):
        assert "endliche Zahl" in fails(fmt, {"ratings": [rating(1, value)]})


# --------------------------------------------------------------------------------------------
# A3: own formats
# --------------------------------------------------------------------------------------------


OWN = {
    "format": "plan",
    "spec": {
        "fields": {
            "ansatz": {"type": "choice", "choices": ["induktion", "widerspruch"]},
            "schritte": {"type": "number", "min": 1, "max": 20, "integer": True},
            "tags": {"type": "list", "item": {"type": "text", "max_chars": 5}, "max_items": 2},
        }
    },
}


def test_own_format_is_checked_field_by_field() -> None:
    assert ok(OWN, {"ansatz": "induktion", "schritte": 3, "tags": ["a"]}).fields["tags"] == ["a"]
    message = fails(
        OWN, {"ansatz": "raten", "schritte": 30, "tags": ["sehr lang", None, "c"], "x": 1}
    )
    assert "„fields.x“: unbekanntes Feld. Erlaubt: ansatz, schritte, tags." in message
    assert (
        "„fields.ansatz“: Erwartet: einer von induktion, widerspruch. Bekommen: „raten“." in message
    )
    assert (
        "„fields.schritte“: 30 liegt außerhalb. Erwartet: eine ganze Zahl von 1 bis 20." in message
    )
    assert "„fields.tags“: hat zu viele Einträge (3, erlaubt 2)." in message
    message = fails(OWN, {"ansatz": "induktion", "schritte": 2, "tags": ["sehr lang", None]})
    assert "„fields.tags[0]“: ist zu lang (9 Zeichen, erlaubt 5)." in message
    assert "„fields.tags[1]“: fehlt." in message


# --------------------------------------------------------------------------------------------
# A7 (form) and A8: paths and limits
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/etc/passwd", "liegt nicht in deinem Arbeitsordner"),
        ("../agents/b/work/x.md", "liegt nicht in deinem Arbeitsordner"),
        ("a/../../x.md", "liegt nicht in deinem Arbeitsordner"),
        ("a\nb.md", "Steuer- oder unsichtbare Zeichen"),
        ("a‮b.md", "Steuer- oder unsichtbare Zeichen"),
        ("a\0b", "Steuer- oder unsichtbare Zeichen"),
        ("", "muss ein Pfad"),
        ("x" * 600, "Pfad zu lang"),
    ],
)
def test_file_paths_of_the_wrong_form(path: str, message: str) -> None:
    assert message in fails("artifact", files=[path])


def test_files_limits_and_duplicate_names() -> None:
    assert "zu viele Dateien" in fails("artifact", files=[f"f{i}.md" for i in range(MAX_FILES + 1)])
    assert "heißt wie files[0] („x.md“)" in fails("artifact", files=["a/x.md", "b/x.md"])
    assert "muss eine Liste von Pfaden sein" in fails("artifact", files="x.md")


def test_body_limit() -> None:
    assert ok("text", body="x" * MAX_BODY_CHARS).body
    assert "„body“: ist zu lang" in fails("text", body="x" * (MAX_BODY_CHARS + 1))
    assert "„body“: ist zu lang" in fails("text", body="x" * (3 * MAX_BODY_CHARS))


def nested(depth: int) -> Any:
    value: Any = "x"
    for _ in range(depth):
        value = [value]
    return value


def test_limits_of_fields() -> None:
    fmt = {"format": "list"}
    assert "zu tief verschachtelt" in fails(fmt, {"items": nested(MAX_DEPTH + 5)})
    assert "zu tief verschachtelt" in fails(fmt, {"items": nested(100_000)})  # no recursion
    assert f"zu viele Einträge (mehr als {MAX_NODES})" in fails(
        fmt, {"items": [{"title": "t", "text": "x"}] * MAX_NODES}
    )
    assert f"mehr als {MAX_FIELDS_CHARS} Zeichen" in fails(
        fmt, {"items": [{"title": "t", "text": "x" * (MAX_FIELDS_CHARS + 1)}]}
    )
    assert f"mehr als {MAX_FIELDS_CHARS} Zeichen" in fails(fmt, {"x" * (MAX_FIELDS_CHARS + 1): 1})
    assert "muss ein Objekt sein, bekommen eine Liste" in fails(fmt, [1, 2])
    assert "kein JSON" in fails(fmt, {"items": {1, 2}})
    many_errors = fails(OWN, {f"x{i}": 1 for i in range(30)})
    assert "… und" in many_errors and many_errors.count("\n- ") <= check_module.MAX_PROBLEMS + 1


def _time(call: Any, repeat: int = 3) -> float:
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        call()
        best = min(best, time.perf_counter() - started)
    return best


LONG_LIST = Expectation.model_validate(
    {
        "format": "liste",
        "spec": {
            "fields": {
                "items": {
                    "type": "list",
                    "max_items": 200,
                    "fields": {"title": {"type": "text"}, "text": {"type": "text"}},
                },
                "notes": {
                    "type": "list",
                    "required": False,
                    "item": {"type": "text", "max_chars": 20_000},
                },
            }
        },
    }
)


def test_check_time_grows_linearly() -> None:
    """A8: 16 times the input takes at most about 16 times as long (relative, no threshold in
    seconds): many list entries, and long texts full of hidden characters to clean."""

    def entries(n: int) -> dict[str, Any]:
        return {"items": [{"title": f"T{i}", "text": "x" * 40} for i in range(n)]}

    def hidden(n: int) -> dict[str, Any]:
        return {"items": [{"title": "t", "text": "x"}], "notes": ["a\u200b" * (n * 15)] * 4}

    for build, small_n in ((entries, 10), (hidden, 20)):
        small, large = build(small_n), build(small_n * 16)

        def call(fields: dict[str, Any]) -> None:
            check_submission(status="done", summary="s", fields=fields, expectation=LONG_LIST)

        call(small)  # both valid, and warm
        call(large)
        t_small = _time(lambda small=small: call(small))
        t_large = _time(lambda large=large: call(large))
        ratio = t_large / max(t_small, 1e-6)
        print(
            f"{build.__name__}: klein {t_small * 1000:.3f} ms, groß {t_large * 1000:.3f} ms, "
            f"Faktor {ratio:.1f} bei 16-facher Eingabe"
        )
        assert ratio < 16 * 3, f"not linear: factor {ratio:.1f} for 16 times the input"


def test_messages_carry_no_hidden_characters_of_the_input() -> None:
    """Unknown names and values are cleaned and cut before they reach the agent or the log."""
    sneaky = "verdict‮​" + "\u0007" + "x" * 500
    message = fails("review", {sneaky: 1, "verdict": "a‮b" * 100})
    assert "‮" not in message and "​" not in message and "\u0007" not in message
    assert "x" * 101 not in message and "„fields.verdictx" in message
    rating = {"format": "rating", "rate": [1], "criteria": ["Tiefe"]}
    message = fails(rating, {"ratings": [{"ref": 1, "score": 2, "criteria": {"T‮iefe": 1}}]})
    assert "‮" not in message and "unbekanntes Kriterium" in message

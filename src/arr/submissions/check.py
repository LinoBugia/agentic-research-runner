"""Check one call of `submit` against its expected format, without any input or output.

The check runs in arr, outside the sandbox (docs/10 §7, SECURITY.md principle 3), on data an
agent chose. So: every field and `fields` as a whole have size limits, nesting and the number
of entries are bounded, every value is looked at once (time linear in the input), nothing of
the data is run, evaluated or used as a path (file paths are only checked for their form here;
the room opens them by its own rules). Texts are cleaned like every text of an agent
(`clean_text`). Problems come back as one `SubmissionError` with German lines that name the
field and what is expected, never a long input.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from arr.models import Rubric
from arr.submissions.formats import (
    Expectation,
    FieldKind,
    FieldSpec,
    FormatSpec,
    SubmissionStatus,
    format_for,
)
from arr.submissions.text import clean_text, has_hidden

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_DEPTH",
    "MAX_FIELDS_CHARS",
    "MAX_FILES",
    "MAX_NODES",
    "MAX_NOTE_CHARS",
    "MAX_SUMMARY_CHARS",
    "Checked",
    "SubmissionError",
    "check_submission",
]

MAX_SUMMARY_CHARS = 800
"""One to three sentences (docs/10 §4.3); it goes into overviews and to gates."""
MAX_BODY_CHARS = 200_000
"""About 200 KB of text; longer content belongs in a file."""
MAX_NOTE_CHARS = 4_000
MAX_FILES = 20
MAX_PATH_CHARS = 512
MAX_FIELDS_CHARS = 100_000
"""All texts in `fields` together (keys included)."""
MAX_NODES = 2_000
"""All values in `fields` together: objects, lists, texts, numbers."""
MAX_DEPTH = 4
"""`fields` → list → object → criteria: the deepest a built-in format goes."""
MAX_KEY_CHARS = 100
MAX_PROBLEMS = 8
"""Problems named in one answer; the rest is counted."""
SHOWN_CHARS = 40
"""A value is quoted in a message with at most this many characters."""


class SubmissionError(Exception):
    """A submission that does not fit; `problems` are German lines for the agent."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        shown = problems[:MAX_PROBLEMS]
        text = "Die Abgabe passt nicht:\n" + "\n".join(f"- {p}" for p in shown)
        if len(problems) > len(shown):
            text += f"\n- … und {len(problems) - len(shown)} weitere."
        super().__init__(text + "\nBessere nach und gib noch einmal mit `submit` ab.")


@dataclass(frozen=True)
class Checked:
    """A submission whose form is right: cleaned head and fields."""

    status: SubmissionStatus
    summary: str
    body: str | None
    files: list[str]
    note_next: str | None
    fields: dict[str, Any] = field(default_factory=dict)


def _shown(value: Any) -> str:
    """A value for a message: short, cleaned, never the whole input."""
    if isinstance(value, str):
        text = clean_text(value[: SHOWN_CHARS * 2])
        return f"„{text[:SHOWN_CHARS]}…“" if len(value) > SHOWN_CHARS else f"„{text}“"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if value is None:
        return "null"
    return {dict: "ein Objekt", list: "eine Liste"}.get(type(value), "ein anderer Wert")


def _what(value: Any) -> str:
    return {dict: "ein Objekt", list: "eine Liste", str: "ein Text", bool: "true/false"}.get(
        type(value), "eine Zahl" if isinstance(value, int | float) else "null"
    )


class _Checker:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def add(self, path: str, text: str) -> None:
        self.problems.append(f"„{path}“: {text}")

    # -- bounds -----------------------------------------------------------------------------

    def bounds(self, fields: Any) -> bool:
        """Size, depth and number of values of `fields`, walked once without recursion."""
        nodes, chars = 0, 0
        stack: list[tuple[Any, int]] = [(fields, 0)]
        while stack:
            value, depth = stack.pop()
            nodes += 1
            if nodes > MAX_NODES:
                self.add("fields", f"zu viele Einträge (mehr als {MAX_NODES}).")
                return False
            if depth > MAX_DEPTH:
                self.add("fields", f"zu tief verschachtelt (mehr als {MAX_DEPTH} Ebenen).")
                return False
            if isinstance(value, str):
                chars += len(value)
            elif isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        self.add("fields", "Schlüssel müssen Texte sein.")
                        return False
                    chars += len(key)
                    stack.append((item, depth + 1))
            elif isinstance(value, list):
                stack += [(item, depth + 1) for item in value]
            elif not (value is None or isinstance(value, bool | int | float)):
                self.add("fields", "enthält einen Wert, der kein JSON ist.")
                return False
            if chars > MAX_FIELDS_CHARS:
                self.add("fields", f"zu lang (mehr als {MAX_FIELDS_CHARS} Zeichen Text).")
                return False
        return True

    # -- head -------------------------------------------------------------------------------

    def text(self, path: str, value: Any, limit: int, *, required: bool) -> str | None:
        """A cleaned text of at most `limit` characters; None if absent (and allowed)."""
        if value is None:
            if required:
                self.add(path, "fehlt.")
            return None
        if not isinstance(value, str):
            self.add(path, f"muss ein Text sein, bekommen {_what(value)}.")
            return None
        if len(value) > 2 * limit:  # not even cleaned: far too long
            self.add(path, f"ist zu lang ({len(value)} Zeichen, erlaubt {limit}).")
            return None
        text = clean_text(value)
        if not text.strip():
            if required:
                self.add(path, "darf nicht leer sein.")
            return None
        if len(text) > limit:
            self.add(path, f"ist zu lang ({len(text)} Zeichen, erlaubt {limit}).")
            return None
        return text

    def files(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            self.add("files", f"muss eine Liste von Pfaden sein, bekommen {_what(value)}.")
            return []
        if len(value) > MAX_FILES:
            self.add("files", f"zu viele Dateien ({len(value)}, erlaubt {MAX_FILES}).")
            return []
        paths, names = [], {}
        for i, rel in enumerate(value):
            where = f"files[{i}]"
            if not isinstance(rel, str) or not rel.strip():
                self.add(where, "muss ein Pfad relativ zu deinem Arbeitsordner sein.")
                continue
            if len(rel) > MAX_PATH_CHARS:
                self.add(where, f"Pfad zu lang (erlaubt {MAX_PATH_CHARS} Zeichen).")
                continue
            if has_hidden(rel) or "\0" in rel:
                self.add(where, "Der Pfad enthält Steuer- oder unsichtbare Zeichen.")
                continue
            parts = [p for p in rel.split("/") if p not in ("", ".")]
            if rel.startswith("/") or not parts or ".." in parts:
                self.add(
                    where,
                    f"{_shown(rel)} liegt nicht in deinem Arbeitsordner. Gib einen Pfad relativ "
                    "zu ihm an, z. B. „notes/beweis.md“.",
                )
                continue
            name = parts[-1]
            if name in names:
                self.add(where, f"heißt wie files[{names[name]}] ({_shown(name)}).")
                continue
            names[name] = i
            paths.append(rel)
        return paths

    # -- fields -----------------------------------------------------------------------------

    def object(self, path: str, value: Any, specs: dict[str, FieldSpec]) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.add(path, f"muss ein Objekt sein, bekommen {_what(value)}.")
            return {}
        allowed = ", ".join(specs) or "keine"
        out: dict[str, Any] = {}
        for key in value:
            if key not in specs:
                name = clean_text(key[:MAX_KEY_CHARS])
                self.add(f"{path}.{name}", f"unbekanntes Feld. Erlaubt: {allowed}.")
        for key, spec in specs.items():
            where = f"{path}.{key}"
            item = value.get(key)
            if item is None:
                if spec.required:
                    self.add(where, f"fehlt. {self.expected(spec)}")
                continue
            checked = self.value(where, item, spec)
            if checked is not None:
                out[key] = checked
        return out

    @staticmethod
    def expected(spec: FieldSpec) -> str:
        """What a field expects, in one short German sentence."""
        if spec.type is FieldKind.CHOICE:
            return f"Erwartet: einer von {', '.join(spec.choices or []) or '(keine Werte)'}."
        if spec.type is FieldKind.NUMBER:
            what = "eine ganze Zahl" if spec.integer else "eine Zahl"
            if spec.min is not None and spec.max is not None:
                return f"Erwartet: {what} von {spec.min:g} bis {spec.max:g}."
            if spec.min is not None:
                return f"Erwartet: {what} ab {spec.min:g}."
            return f"Erwartet: {what}."
        if spec.type is FieldKind.TEXT:
            return f"Erwartet: ein Text mit höchstens {spec.text_limit} Zeichen."
        if spec.type is FieldKind.SCORES:
            names = ", ".join(spec.choices or [])
            return f"Erwartet: ein Objekt Kriterium → Punkte, Kriterien: {names or 'keine'}."
        inner = ", ".join(spec.fields or {}) if spec.fields else "je ein Wert"
        return f"Erwartet: eine Liste ({inner})."

    def value(self, path: str, value: Any, spec: FieldSpec) -> Any:
        kind = spec.type
        if kind is FieldKind.TEXT:
            return self.text(path, value, spec.text_limit, required=True)
        if kind is FieldKind.CHOICE:
            choices = spec.choices or []
            text = clean_text(value[: MAX_KEY_CHARS * 2]) if isinstance(value, str) else None
            if text is None or text not in set(choices):
                self.add(path, f"{self.expected(spec)} Bekommen: {_shown(value)}.")
                return None
            return text
        if kind is FieldKind.NUMBER:
            return self.number(path, value, spec)
        if kind is FieldKind.SCORES:
            return self.scores(path, value, spec)
        return self.list(path, value, spec)

    def number(self, path: str, value: Any, spec: FieldSpec) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.add(path, f"{self.expected(spec)} Bekommen: {_shown(value)}.")
            return None
        if not math.isfinite(value):
            self.add(path, f"muss eine endliche Zahl sein. {self.expected(spec)}")
            return None
        if spec.integer:
            if isinstance(value, float) and not value.is_integer():
                self.add(path, f"{self.expected(spec)} Bekommen: {_shown(value)}.")
                return None
            value = int(value)
        if (spec.min is not None and value < spec.min) or (
            spec.max is not None and value > spec.max
        ):
            self.add(path, f"{_shown(value)} liegt außerhalb. {self.expected(spec)}")
            return None
        return value

    def scores(self, path: str, value: Any, spec: FieldSpec) -> dict[str, float] | None:
        if not isinstance(value, dict):
            self.add(path, f"{self.expected(spec)} Bekommen: {_what(value)}.")
            return None
        known = set(spec.choices or [])
        if value and not known:
            self.add(
                path, "Für diese Bewertung sind keine Kriterien festgelegt; lass das Feld weg."
            )
            return None
        number = FieldSpec(type=FieldKind.NUMBER, min=spec.min, max=spec.max)
        out = {}
        for name, score in value.items():
            if name not in known:
                self.add(
                    f"{path}.{clean_text(name[:MAX_KEY_CHARS])}",
                    f"unbekanntes Kriterium. Erlaubt: {', '.join(spec.choices or [])}.",
                )
                continue
            checked = self.number(f"{path}.{name}", score, number)
            if checked is not None:
                out[name] = float(checked)
        return out

    def list(self, path: str, value: Any, spec: FieldSpec) -> list[Any] | None:
        if not isinstance(value, list):
            self.add(path, f"{self.expected(spec)} Bekommen: {_what(value)}.")
            return None
        if len(value) < spec.min_items:
            self.add(path, f"braucht mindestens {spec.min_items} Einträge, bekommen {len(value)}.")
            return None
        if len(value) > spec.item_limit:
            self.add(path, f"hat zu viele Einträge ({len(value)}, erlaubt {spec.item_limit}).")
            return None
        out = []
        for i, item in enumerate(value):
            where = f"{path}[{i}]"
            if spec.fields is not None:
                out.append(self.object(where, item, spec.fields))
            else:
                assert spec.item is not None
                if item is None:
                    self.add(where, f"fehlt. {self.expected(spec.item)}")
                    continue
                checked = self.value(where, item, spec.item)
                if checked is not None:
                    out.append(checked)
        return out


def _review_rules(checker: _Checker, fields: dict[str, Any]) -> None:
    """A new objection has neither `ref` nor `status`; a reference has both, once each."""
    seen: dict[str, int] = {}
    for i, objection in enumerate(fields.get("objections") or []):
        where = f"fields.objections[{i}]"
        ref, status = objection.get("ref"), objection.get("status")
        if ref is None and status is not None:
            checker.add(
                f"{where}.status",
                "Ein neuer Einwand hat keinen Status; „status“ gilt nur zusammen mit „ref“.",
            )
        elif ref is not None and status is None:
            checker.add(f"{where}.status", "fehlt. Ein Bezug auf einen Einwand braucht „status“.")
        if ref is not None:
            if ref in seen:
                checker.add(f"{where}.ref", f"„{ref}“ steht schon in objections[{seen[ref]}].")
            seen[ref] = i


def _rating_rules(checker: _Checker, fields: dict[str, Any], rate: list[int]) -> None:
    """Every submission of `rate` exactly once, no other (Entscheidung 7)."""
    wanted = set(rate)
    seen: dict[int, int] = {}
    for i, entry in enumerate(fields.get("ratings") or []):
        ref = entry.get("ref")
        if ref is None:
            continue
        where = f"fields.ratings[{i}].ref"
        if ref in seen:
            checker.add(where, f"Die Abgabe #{ref} steht schon in ratings[{seen[ref]}].")
        elif ref not in wanted:
            ids = ", ".join(f"#{r}" for r in rate)
            checker.add(where, f"#{ref} ist nicht zu bewerten. Zu bewerten: {ids}.")
        seen.setdefault(ref, i)
    missing = [r for r in rate if r not in seen]
    if missing and "ratings" in fields:
        ids = ", ".join(f"#{r}" for r in missing)
        checker.add(
            "fields.ratings", f"Es fehlt eine Bewertung für {ids}. Bewerte jede genau einmal."
        )


def check_submission(
    *,
    status: Any,
    summary: Any,
    body: Any = None,
    files: Any = None,
    note_next: Any = None,
    fields: Any = None,
    expectation: Expectation,
    rubric: Rubric | None = None,
) -> Checked:
    """Check head and fields of a submission; `SubmissionError` names every problem."""
    checker = _Checker()
    spec: FormatSpec = format_for(expectation, rubric)
    if status not in tuple(SubmissionStatus):
        checker.add("status", f"Erwartet: done oder not_solved. Bekommen: {_shown(status)}.")
    summary_text = checker.text("summary", summary, MAX_SUMMARY_CHARS, required=True)
    body_text = checker.text("body", body, MAX_BODY_CHARS, required=False)
    note = checker.text("note_next", note_next, MAX_NOTE_CHARS, required=False)
    paths = checker.files(files)
    checked_fields: dict[str, Any] = {}
    if fields is None:
        fields = {}
    if checker.bounds(fields):
        checked_fields = checker.object("fields", fields, dict(spec.fields))
    if status == SubmissionStatus.DONE:
        if spec.content and body_text is None and not paths and "files" not in _failed(checker):
            checker.add("body", "Gib den Inhalt in „body“ an oder Dateien in „files“.")
        if spec.needs_files and not paths and "files" not in _failed(checker):
            checker.add("files", "Ein Artefakt braucht bei „done“ mindestens eine Datei.")
    if expectation.format == "review" and expectation.spec is None:
        _review_rules(checker, checked_fields)
    if expectation.format == "rating" and expectation.spec is None:
        _rating_rules(checker, checked_fields, list(expectation.rate or []))
    if checker.problems:
        raise SubmissionError(checker.problems)
    assert summary_text is not None
    return Checked(
        status=SubmissionStatus(status),
        summary=summary_text.strip(),
        body=body_text,
        files=paths,
        note_next=note,
        fields=checked_fields,
    )


def _failed(checker: _Checker) -> set[str]:
    """Head fields that already have a problem (no second message about them)."""
    return {p[1 : p.index("“")].split("[")[0] for p in checker.problems if p.startswith("„")}

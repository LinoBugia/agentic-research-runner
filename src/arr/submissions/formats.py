"""Formats of submissions as data, without any input or output (docs/10 §4.3, §4.10; W4b-01).

A format names the fields an agent hands in besides the head (`status`, `summary`, `body`,
`files`, `note_next`). Fields are built from four kinds (`FieldKind`): `choice` (one of
`choices`), `number` (`min`, `max`, optionally `integer`), `text` (`max_chars`) and `list`
(`min_items`, `max_items`; the entries are one `item` field or objects with `fields`). A list
holds no list: two levels are all a format can nest, which keeps every check short.

The six built-in formats are `text`, `artifact`, `review`, `rating`, `list` and `decision`
(`BUILTIN`). Own formats (`formats:` of a group, W4b-02a) use the same kinds. The parts a
built-in format takes from the assignment (`choices` of a decision, the IDs to `rate`, the
`known_objections`, the scale and criteria of the rubric) are filled in by
`format_for(expectation, rubric)`; `Expectation` is the key `submission` of the context of an
assignment. W4b-02a builds the JSON schema of `formats:` from `FormatSpec`.

Since W4b-02a the base types `StrictModel`, `Slug` and `Rubric` live here and `arr.models`
imports them from this module: a group's `formats:` holds `FormatSpec`s, so `arr.models` needs
this module, and this module must not need `arr.models` (no import cycle, no late import).
`from arr.models import StrictModel, Slug, Rubric` works as before.
"""

import difflib
import re
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

__all__ = [
    "BUILTIN",
    "DEFAULT_LIST_ITEMS",
    "DEFAULT_TEXT_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_SPEC_FIELDS",
    "MAX_TEXT_CHARS",
    "OBJECTION_STATUSES",
    "RESERVED_NAMES",
    "VERDICTS",
    "Expectation",
    "FieldKind",
    "FieldSpec",
    "FormatName",
    "FormatSpec",
    "Rubric",
    "Slug",
    "StrictModel",
    "SubmissionStatus",
    "format_for",
]


# --------------------------------------------------------------------------------------------
# Base types (in `arr.models` until W4b-02a; unchanged, re-exported there)
# --------------------------------------------------------------------------------------------


class StrictModel(BaseModel):
    """Base for models read from user files: unknown fields are errors (docs/02 §3.3).

    The check runs before field validation so the error carries the field name, the allowed
    fields and a spelling suggestion; `arr.resolve` turns it into a German `ConfigIssue`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        allowed: list[str] = []
        for name, info in cls.model_fields.items():
            allowed.append(info.alias or name)
            if info.alias:
                allowed.append(name)
        for key in data:
            if key not in allowed:
                public = sorted({info.alias or name for name, info in cls.model_fields.items()})
                suggestion = difflib.get_close_matches(str(key), public, n=1)
                raise PydanticCustomError(
                    "unknown_field",
                    "unknown field {field}",
                    {
                        "field": str(key),
                        "allowed": ", ".join(public),
                        "suggestion": suggestion[0] if suggestion else "",
                    },
                )
        return data


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _check_slug(value: Any, what: str, example: str) -> Any:
    if isinstance(value, str) and not _SLUG_RE.fullmatch(value):
        raise ValueError(
            f"Ungültige {what} „{value}“. Erlaubt sind Kleinbuchstaben, Ziffern, „-“ und „_“ "
            f"(höchstens 64 Zeichen), z. B. „{example}“."
        )
    return value


Slug = Annotated[str, BeforeValidator(lambda v: _check_slug(v, "Bezeichnung", "main"))]
"""Name of a channel, profile, shared folder, …: lowercase slug (docs/06 §2)."""


class Rubric(StrictModel):
    """Rating scale and criteria for `submit_rating` (docs/03 §3, docs/01 §5)."""

    scale: tuple[float, float] = (1, 10)
    pass_score: float | None = None
    criteria: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _pass_score_in_scale(self) -> Self:
        low, high = self.scale
        if low >= high:
            raise ValueError("„scale“ braucht zwei Werte [min, max] mit min < max.")
        if self.pass_score is not None and not low <= self.pass_score <= high:
            raise ValueError(
                f"„pass_score“ {self.pass_score:g} liegt außerhalb der Skala [{low:g}, {high:g}]."
            )
        return self


# --------------------------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------------------------


class SubmissionStatus(StrEnum):
    """`status` in the head of a submission: `not_solved` with a reason is a valid one."""

    DONE = "done"
    NOT_SOLVED = "not_solved"


class FieldKind(StrEnum):
    """Kinds of fields; `scores` is internal (criteria of a `rating`), not for own formats."""

    CHOICE = "choice"
    NUMBER = "number"
    TEXT = "text"
    LIST = "list"
    SCORES = "scores"


PUBLIC_KINDS = (FieldKind.CHOICE, FieldKind.NUMBER, FieldKind.TEXT, FieldKind.LIST)
"""The kinds an own format may use (docs/10 §4.10)."""

DEFAULT_TEXT_CHARS = 4_000
"""`max_chars` of a `text` field without one: a long paragraph; longer goes in `body`."""
MAX_TEXT_CHARS = 20_000
"""The most a format may allow for one text field (all together: `check.MAX_FIELDS_CHARS`)."""
DEFAULT_LIST_ITEMS = 50
MAX_LIST_ITEMS = 200
"""The most entries a format may allow in one list (all entries: `check.MAX_NODES`)."""
MAX_CHOICES = 50
MAX_CHOICE_CHARS = 100
MAX_SPEC_FIELDS = 30
"""Fields of one format and of one object in a list."""
MAX_NUMBER = 1e12
"""Bounds of a `number` field: values far beyond are typos or tricks, not measurements."""
VERDICTS = ("accept", "minor", "major", "reject")
"""Verdicts of a `review`, from mild to strict (as in `runs/topologies/review.py`)."""
OBJECTION_STATUSES = ("offen", "behoben", "strittig")
"""Status of an objection a review refers to (docs/10 §4.8)."""

RESERVED_NAMES = frozenset({"submissions", "results", "artifacts", "review"})
"""Names a submission cannot have: folders below `output/` that hold something else (unnamed
submissions, the results and versions of the old topologies). Files there (`summary.md`, the
JSON files of the finalizer) have a dot, which no name has."""

FormatName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
"""Name of a format: a slug like the names of `models.Slug`."""


class FieldSpec(StrictModel):
    """One field of a format (docs/10 §4.10 `formats`).

    `choice`: `choices` (filled from the assignment for the `choice` of a `decision`);
    `number`: `min`, `max`, `integer`; `text`: `max_chars`; `list`: `min_items`, `max_items`
    and either `item` (a single field per entry, never a list) or `fields` (an object per
    entry, without lists); `scores` (internal): names in `choices`, values from `min` to `max`.
    """

    type: FieldKind
    required: bool = True
    description: str = Field(default="", max_length=500)
    choices: list[Annotated[str, Field(min_length=1, max_length=MAX_CHOICE_CHARS)]] | None = Field(
        default=None, max_length=MAX_CHOICES
    )
    min: float | None = Field(default=None, ge=-MAX_NUMBER, le=MAX_NUMBER)
    max: float | None = Field(default=None, ge=-MAX_NUMBER, le=MAX_NUMBER)
    integer: bool = False
    max_chars: int | None = Field(default=None, ge=1, le=MAX_TEXT_CHARS)
    min_items: int = Field(default=0, ge=0, le=MAX_LIST_ITEMS)
    max_items: int | None = Field(default=None, ge=1, le=MAX_LIST_ITEMS)
    item: "FieldSpec | None" = None
    fields: dict[str, "FieldSpec"] | None = Field(default=None, max_length=MAX_SPEC_FIELDS)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        kind = self.type
        allowed = {
            FieldKind.CHOICE: {"choices"},
            FieldKind.NUMBER: {"min", "max", "integer"},
            FieldKind.TEXT: {"max_chars"},
            FieldKind.LIST: {"min_items", "max_items", "item", "fields"},
            FieldKind.SCORES: {"choices", "min", "max"},
        }[kind]
        used = {
            name
            for name in ("choices", "min", "max", "integer", "max_chars", "item", "fields")
            if getattr(self, name) not in (None, False)
        }
        if self.min_items:
            used.add("min_items")
        if self.max_items is not None:
            used.add("max_items")
        wrong = sorted(used - allowed)
        if wrong:
            raise ValueError(f"„{', '.join(wrong)}“ passt nicht zu einem Feld vom Typ „{kind}“.")
        if self.choices is not None and len(set(self.choices)) != len(self.choices):
            raise ValueError("„choices“ enthält einen Wert doppelt.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"„min“ {self.min:g} ist größer als „max“ {self.max:g}.")
        if kind is FieldKind.LIST:
            if (self.item is None) == (self.fields is None):
                raise ValueError("Eine Liste braucht genau eins von „item“ und „fields“.")
            inner = [self.item] if self.item is not None else list((self.fields or {}).values())
            if any(f.type is FieldKind.LIST for f in inner):
                raise ValueError("Eine Liste enthält keine Liste.")
            if self.max_items is not None and self.min_items > self.max_items:
                raise ValueError("„min_items“ ist größer als „max_items“.")
        return self

    @property
    def text_limit(self) -> int:
        """`max_chars`, or `DEFAULT_TEXT_CHARS`."""
        return self.max_chars or DEFAULT_TEXT_CHARS

    @property
    def item_limit(self) -> int:
        """`max_items`, or `DEFAULT_LIST_ITEMS`."""
        return self.max_items or DEFAULT_LIST_ITEMS


class FormatSpec(StrictModel):
    """A format: its fields and the rules on the head that belong to it.

    `content`: a `done` needs `body` or `files` (`text`); `needs_files`: a `done` needs at
    least one file (`artifact`). Own formats (`formats:`, `spec` of an assignment) only have
    `fields`, built from the public kinds.
    """

    fields: dict[FormatName, FieldSpec] = Field(default_factory=dict, max_length=MAX_SPEC_FIELDS)
    content: bool = False
    needs_files: bool = False

    def kinds(self) -> set[FieldKind]:
        """Every kind used in this format, nested ones included."""
        found: set[FieldKind] = set()
        stack = list(self.fields.values())
        while stack:
            spec = stack.pop()
            found.add(spec.type)
            stack += [spec.item] if spec.item is not None else []
            stack += list((spec.fields or {}).values())
        return found


def _text(
    max_chars: int | None = None, *, required: bool = True, description: str = ""
) -> FieldSpec:
    return FieldSpec(
        type=FieldKind.TEXT, max_chars=max_chars, required=required, description=description
    )


def _choice(
    choices: list[str] | None, *, required: bool = True, description: str = ""
) -> FieldSpec:
    return FieldSpec(
        type=FieldKind.CHOICE, choices=choices, required=required, description=description
    )


BUILTIN: dict[str, FormatSpec] = {
    "text": FormatSpec(content=True),
    "artifact": FormatSpec(needs_files=True),
    "review": FormatSpec(
        fields={
            "verdict": _choice(list(VERDICTS), description="Urteil, von mild bis streng"),
            "objections": FieldSpec(
                type=FieldKind.LIST,
                max_items=100,
                description="Einwände: neue nur mit „text“, Bezüge mit „ref“ und „status“",
                fields={
                    "text": _text(),
                    "ref": _choice(None, required=False, description="ID eines bekannten Einwands"),
                    "status": _choice(list(OBJECTION_STATUSES), required=False),
                },
            ),
        }
    ),
    "rating": FormatSpec(
        fields={
            "ratings": FieldSpec(
                type=FieldKind.LIST,
                min_items=1,
                max_items=100,
                description="je Abgabe aus „rate“ genau ein Eintrag",
                fields={
                    "ref": FieldSpec(type=FieldKind.NUMBER, integer=True, min=1),
                    "score": FieldSpec(type=FieldKind.NUMBER),
                    "criteria": FieldSpec(type=FieldKind.SCORES, required=False),
                    "comment": _text(required=False),
                },
            )
        }
    ),
    "list": FormatSpec(
        fields={
            "items": FieldSpec(
                type=FieldKind.LIST,
                min_items=1,
                max_items=DEFAULT_LIST_ITEMS,
                fields={"title": _text(200), "text": _text()},
            )
        }
    ),
    "decision": FormatSpec(fields={"choice": _choice(None)}),
}
"""The built-in formats (docs/10 §4.3). Choices left `None` come from the assignment."""


class Expectation(StrictModel):
    """The expected submission of an assignment: `context["submission"]` (W4b-01 §6).

    `format` names a built-in format or an own one; an own one brings its fields in `spec`
    (the room server does not know the `group.yaml`). `name` makes versions; `action` tells
    the actions of one agent in a phase apart; `choices` belong to a `decision`, `rate` (IDs of
    submissions) and `criteria` to a `rating`, `known_objections` (IDs) to a `review`.
    """

    format: FormatName
    spec: FormatSpec | None = None
    name: Slug | None = None
    action: str | None = Field(default=None, min_length=1, max_length=64)
    choices: list[Annotated[str, Field(min_length=1, max_length=MAX_CHOICE_CHARS)]] | None = Field(
        default=None, min_length=1, max_length=MAX_CHOICES
    )
    rate: list[Annotated[int, Field(ge=1)]] | None = Field(default=None, max_length=100)
    criteria: list[Annotated[str, Field(min_length=1, max_length=MAX_CHOICE_CHARS)]] | None = Field(
        default=None, max_length=MAX_CHOICES
    )
    known_objections: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=500
    )

    @model_validator(mode="before")
    @classmethod
    def _ids_as_text(cls, data: Any) -> Any:
        """Objection IDs may come as numbers; they are compared as text."""
        if isinstance(data, dict) and isinstance(data.get("known_objections"), list):
            data = {
                **data,
                "known_objections": [
                    str(v) if isinstance(v, int) and not isinstance(v, bool) else v
                    for v in data["known_objections"]
                ],
            }
        return data

    @model_validator(mode="after")
    def _fits_format(self) -> Self:
        builtin = self.format in BUILTIN
        if builtin and self.spec is not None:
            raise ValueError(f"„{self.format}“ ist ein eingebautes Format und hat keine „spec“.")
        if not builtin and self.spec is None:
            known = ", ".join(BUILTIN)
            raise ValueError(
                f"Das Format „{self.format}“ ist nicht eingebaut ({known}) und braucht „spec“."
            )
        if self.spec is not None:
            if self.spec.content or self.spec.needs_files:
                raise ValueError("Ein eigenes Format hat nur „fields“.")
            internal = self.spec.kinds() - set(PUBLIC_KINDS)
            if internal:
                raise ValueError("Ein eigenes Format nutzt nur choice, number, text und list.")
            if any(f.type is FieldKind.CHOICE and not f.choices for f in _all_fields(self.spec)):
                raise ValueError("Ein Feld vom Typ „choice“ braucht „choices“.")
        if self.name in RESERVED_NAMES:
            raise ValueError(f"Der Name „{self.name}“ ist in output/ schon belegt.")
        if self.format == "decision" and not self.choices:
            raise ValueError("Eine „decision“ braucht „choices“.")
        if self.format == "rating" and not self.rate:
            raise ValueError("Ein „rating“ braucht „rate“ mit den IDs der Abgaben.")
        if self.rate is not None and len(set(self.rate)) != len(self.rate):
            raise ValueError("„rate“ nennt eine Abgabe doppelt.")
        return self


def _all_fields(spec: FormatSpec) -> list[FieldSpec]:
    found, stack = [], list(spec.fields.values())
    while stack:
        field = stack.pop()
        found.append(field)
        stack += [field.item] if field.item is not None else []
        stack += list((field.fields or {}).values())
    return found


def format_for(expectation: Expectation, rubric: Rubric | None = None) -> FormatSpec:
    """The concrete format of an expectation: an own one as given, a built-in one with what
    the assignment adds (`choices`, `known_objections`) and the scale and criteria of a rating
    (`criteria` of the assignment, else those of the rubric; Entscheidung 5)."""
    if expectation.spec is not None:
        return expectation.spec
    base = BUILTIN[expectation.format]
    if expectation.format == "decision":
        return base.model_copy(update={"fields": {"choice": _choice(expectation.choices)}})
    if expectation.format == "review":
        objections = base.fields["objections"]
        assert objections.fields is not None
        inner = dict(objections.fields)
        inner["ref"] = inner["ref"].model_copy(
            update={"choices": list(expectation.known_objections or [])}
        )
        fields = dict(base.fields)
        fields["objections"] = objections.model_copy(update={"fields": inner})
        return base.model_copy(update={"fields": fields})
    if expectation.format == "rating":
        rubric = rubric or Rubric()
        low, high = rubric.scale
        criteria = (
            expectation.criteria if expectation.criteria is not None else list(rubric.criteria)
        )
        ratings = base.fields["ratings"]
        assert ratings.fields is not None
        inner = dict(ratings.fields)
        inner["score"] = FieldSpec(type=FieldKind.NUMBER, min=low, max=high)
        inner["criteria"] = FieldSpec(
            type=FieldKind.SCORES, required=False, choices=criteria, min=low, max=high
        )
        items = ratings.model_copy(update={"fields": inner})
        return base.model_copy(update={"fields": {"ratings": items}})
    return base

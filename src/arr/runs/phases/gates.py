"""What a gate computes, without a model, from the newest submissions of a pass (W4b-02b §6.6,
§6.10; W4b-02a §6.5; docs/10 §4.7, §4.8).

- `signal(field, rule, values)`: the values of one field, one per action (None: no value).
  `all`/`any` (only `status`), `majority` (the most frequent; on a tie the stricter verdict,
  else `on_tie`), `unanimous` (verdicts like `review.aggregate` of the old topology, other
  fields the common value, else `on_tie`), no rule (one action: its value).
- `judge_mean`, `judge_rank`: ratings → `pass`/`fail` against `pass_score`, or `ranked` with the
  order by the mean per submission (a tie: the newer first).
- `ObjectionList`: one list per run from the `review` submissions, by the rule of
  `review._take_objections`: new objections get an ID, known ones `offen`, `behoben` or
  `strittig`; submissions that disagree make it `strittig`.

A gate that cannot form a result returns `result=None` with a reason (Entscheidung 51): the
runner then asks the user. `status` always has a result: a missing submission counts as
`not_solved`.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from arr.models import GateRule
from arr.runs.phases.text import MAX_OBJECTION_CHARS, MAX_OBJECTIONS, block
from arr.submissions.formats import OBJECTION_STATUSES, VERDICTS

__all__ = [
    "NO_RATING",
    "NO_VALUE",
    "GateOutcome",
    "Objection",
    "ObjectionList",
    "judge_mean",
    "judge_rank",
    "signal",
    "verdicts",
]

NO_VALUE = "Keine Abgabe trägt einen Wert für „{field}“."
NO_RATING = "Kein Rater hat bewertet."


@dataclass
class GateOutcome:
    """The result of a gate: the value its routes are keyed by, or a tie (`on_tie`), or none.

    `score` is the mean of a gate `judge` with `mean`; `scores` the mean per rated submission;
    `ranking` the submissions by mean (rule `rank`); `counts` the values a rule counted."""

    result: str | None = None
    tie: bool = False
    reason: str = ""
    score: float | None = None
    scores: dict[int, float] = field(default_factory=dict)
    ranking: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def verdicts(values: Sequence[str | None], rule: str) -> str | None:
    """Verdicts like `review.aggregate` (Entscheidung 16 of W4-03): `majority` the most frequent,
    on a tie the stricter; `unanimous` `accept` only if every one is `accept`, else the
    strictest given, at least `minor` when one is missing. None without any verdict."""
    given = [v for v in values if v is not None]
    if not given:
        return None
    if rule == GateRule.UNANIMOUS:
        if len(given) == len(values) and all(v == "accept" for v in given):
            return "accept"
        return max([*given, "minor"], key=VERDICTS.index)
    counts = Counter(given)
    top = max(counts.values())
    return max((v for v, n in counts.items() if n == top), key=VERDICTS.index)


def _plain(value: Any) -> str | None:
    if value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def signal(field_name: str, rule: GateRule | None, values: Sequence[Any]) -> GateOutcome:
    """The result of a gate `signal` over `values` (one per action, None without a value)."""
    plain = [_plain(v) for v in values]
    given = [v for v in plain if v is not None]
    counts = dict(Counter(given))
    if field_name == "verdict":
        known = [v if v in VERDICTS else None for v in plain]
        rule_name = rule.value if rule is not None else GateRule.MAJORITY.value
        if rule is GateRule.UNANIMOUS:
            result = verdicts(known, rule_name)
        else:
            result = verdicts(known, GateRule.MAJORITY.value)
        if result is None:
            return GateOutcome(reason=NO_VALUE.format(field=field_name), counts=counts)
        return GateOutcome(result, counts=counts)
    if not given:
        return GateOutcome(reason=NO_VALUE.format(field=field_name), counts=counts)
    if rule in (GateRule.ALL, GateRule.ANY):
        test = all if rule is GateRule.ALL else any
        solved = test(v == "done" for v in plain)
        return GateOutcome("done" if solved else "not_solved", counts=counts)
    if rule is GateRule.UNANIMOUS:
        if len(given) == len(plain) and len(set(given)) == 1:
            return GateOutcome(given[0], counts=counts)
        return GateOutcome(tie=True, counts=counts)
    if rule is None and len(plain) == 1:
        return GateOutcome(given[0], counts=counts)
    top = max(counts.values())
    leaders = [v for v, n in counts.items() if n == top]
    if len(leaders) == 1:
        return GateOutcome(leaders[0], counts=counts)
    return GateOutcome(tie=True, counts=counts)


def judge_mean(ratings: Mapping[int, Sequence[float]], pass_score: float) -> GateOutcome:
    """`pass` or `fail`: the mean of every rating of the rated submission (Entscheidung 41: a
    pass has one submission; with more, the newest rated one counts)."""
    scores = {ref: fmean(values) for ref, values in ratings.items() if values}
    if not scores:
        return GateOutcome(reason=NO_RATING)
    ref = max(scores)
    score = scores[ref]
    return GateOutcome("pass" if score >= pass_score else "fail", score=score, scores=scores)


def judge_rank(ratings: Mapping[int, Sequence[float]], refs: Iterable[int]) -> GateOutcome:
    """`ranked`: the submissions by their mean, a tie the newer first; unrated ones last."""
    scores = {ref: fmean(values) for ref, values in ratings.items() if values}
    if not scores:
        return GateOutcome(reason=NO_RATING)
    rated = sorted(scores, key=lambda r: (-scores[r], -r))
    rest = [r for r in refs if r not in scores]
    return GateOutcome("ranked", scores=scores, ranking=rated + rest)


@dataclass
class Objection:
    """One objection of the run: ID, text, status, who raised it and where."""

    id: str
    text: str
    status: str
    by: str
    phase: str
    attempt: int


@dataclass
class ObjectionList:
    """The run's list of objections from the `review` submissions (docs/10 §4.8)."""

    items: list[Objection] = field(default_factory=list)

    def ids(self) -> list[str]:
        """IDs of all objections, for `known_objections`."""
        return [o.id for o in self.items]

    def take(
        self, reviews: Sequence[tuple[str, Mapping[str, Any]]], phase: str, attempt: int
    ) -> None:
        """Update the list from the reviews of one pass: (agent, `fields`) each."""
        known = {o.id: o for o in self.items}
        updates: dict[str, set[str]] = {}
        for agent, fields in reviews:
            objections = fields.get("objections")
            if not isinstance(objections, list):
                continue
            for entry in objections:
                if not isinstance(entry, Mapping):
                    continue
                ref = entry.get("ref")
                if isinstance(ref, str) and ref in known:
                    status = entry.get("status")
                    updates.setdefault(ref, set()).add(
                        status if status in OBJECTION_STATUSES else "offen"
                    )
                    continue
                text = entry.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                if len(self.items) >= MAX_OBJECTIONS:
                    continue
                objection = Objection(
                    f"E{len(self.items) + 1}",
                    block(text, MAX_OBJECTION_CHARS),
                    "offen",
                    agent,
                    phase,
                    attempt,
                )
                self.items.append(objection)
        for ref, statuses in updates.items():
            known[ref].status = statuses.pop() if len(statuses) == 1 else "strittig"

    def as_data(self) -> list[dict[str, Any]]:
        """The list for a context package: ID, status, text (never who raised it)."""
        return [{"id": o.id, "status": o.status, "text": o.text} for o in self.items]

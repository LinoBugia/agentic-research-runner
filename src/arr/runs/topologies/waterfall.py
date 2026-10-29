"""Topology `waterfall`: staged work with ratings and rework.

For each stage of `stages`, in order (the checkpoint `plan` lists their names):

- **Attempt:** the stage's agent gets a short prompt and the context package (`context`):
  `task`, `stage`, `attempt`, `produces`, the accepted results of earlier stages (`accepted`:
  ID, stage, title, path relative to the run folder, the body only if there is no file) and,
  on rework, the ratings of the last attempt (`rework`: result, score, per rater the score,
  the scores per criterion and the comment). `context.attempt` becomes the `attempt` of the
  results. Without a result (also after a turn with status `error`) the topology asks once
  more; then the attempt fails without rating.
- **Ratings:** all `raters` except the author rate in parallel (`ctx.turns`); missing ones
  are asked once more. The score is the mean of the ratings that exist; without any rating
  the attempt fails.
- **Decision:** score ≥ `pass_score` (`params.pass_score`, else `rubric.pass_score`) → the
  result is accepted. Otherwise the same agent reworks it, up to `max_rework` times. When all
  `1 + max_rework` attempts failed, `on_fail` decides: `continue` accepts the attempt with the
  best score, `stop` ends the run regularly (reason `stage_failed`), `escalate` asks the user:
  `retry` (one more attempt, repeatable), `continue` or `end` (like `stop`).

Checkpoints in `orchestrator.jsonl`: one per stage and attempt (`attempt`, `agent`, `result`,
`score`, `ratings`, `decision` = `passed` | `rework` | `failed` | `escalated`, `reason` =
`no_result` | `no_rating` if so) and one at the end of each stage (`phase: "stage_end"`,
`outcome` = `passed` | `best_accepted` | `stage_failed`, `accepted`, `score`, `attempts`).
See docs/03-skills-und-research-groups.md §4.4.

Parameters: `pass_score`, `max_rework` (2), `on_fail` (`continue`).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from arr.backends.base import TurnOutcome, TurnStatus
from arr.errors import ConfigIssue
from arr.models import ResolvedRun, Rubric, StageSpec
from arr.permissions.types import EscalationOption
from arr.room.types import Rating, Result
from arr.runs.context import RunContext, TurnJob
from arr.runs.topologies import describe, ensure_valid, int_param, require_task

PLAN = "plan"
MAX_REWORK = 2
ON_FAIL = ("continue", "stop", "escalate")
ESCALATION_OPTIONS = (EscalationOption.RETRY, EscalationOption.CONTINUE, EscalationOption.END)


def _rubric(resolved: ResolvedRun) -> Rubric:
    return resolved.rubric or Rubric()


def _pass_score(params: Mapping[str, Any], rubric: Rubric) -> Any:
    """`params.pass_score`, else `rubric.pass_score` (None if neither is set)."""
    value = params.get("pass_score")
    return rubric.pass_score if value is None else value


def check(resolved: ResolvedRun) -> list[ConfigIssue]:
    """Problems of the `waterfall` parameters, stages and raters (see `check_topology`)."""
    issues: list[ConfigIssue] = []
    params = resolved.params
    int_param(params, "max_rework", MAX_REWORK, issues, minimum=0)
    on_fail = params.get("on_fail") or "continue"
    if on_fail not in ON_FAIL:
        issues.append(
            ConfigIssue(
                "params.on_fail",
                f"Parameter „on_fail“ ist „{on_fail}“. Erlaubt sind: {', '.join(ON_FAIL)}.",
            )
        )
    rubric = _rubric(resolved)
    low, high = rubric.scale
    pass_score = _pass_score(params, rubric)
    if pass_score is None:
        issues.append(
            ConfigIssue(
                "params.pass_score",
                "„waterfall“ braucht eine Bestehensgrenze. Setze den Parameter „pass_score“ "
                "oder „rubric.pass_score“.",
            )
        )
    elif (
        isinstance(pass_score, bool)
        or not isinstance(pass_score, int | float)
        or not math.isfinite(pass_score)
    ):
        issues.append(
            ConfigIssue(
                "params.pass_score",
                f"Parameter „pass_score“ muss eine Zahl sein, gefunden {describe(pass_score)}.",
            )
        )
    elif not low <= pass_score <= high:
        issues.append(
            ConfigIssue(
                "params.pass_score",
                f"Parameter „pass_score“ = {pass_score:g} liegt außerhalb der Skala "
                f"[{low:g}, {high:g}] der Rubrik.",
            )
        )
    if not resolved.stages:
        issues.append(
            ConfigIssue(
                "stages", "„waterfall“ braucht Stufen. Lege sie unter „stages“ an (Name, Agent)."
            )
        )
    seen: set[str] = set()
    for index, stage in enumerate(resolved.stages):
        if stage.name == PLAN:
            issues.append(
                ConfigIssue(
                    f"stages[{index}].name",
                    f"Der Stufenname „{PLAN}“ ist für arr reserviert. Wähle einen anderen.",
                )
            )
        if stage.name in seen:
            issues.append(
                ConfigIssue(
                    f"stages[{index}].name",
                    f"Die Stufe „{stage.name}“ gibt es mehrfach. Benenne eine der Stufen um.",
                )
            )
        seen.add(stage.name)
        if not [r for r in resolved.raters if r != stage.agent]:
            issues.append(
                ConfigIssue(
                    "raters" if not resolved.raters else f"stages[{index}].agent",
                    f"Für die Stufe „{stage.name}“ gibt es keinen Rater außer dem Autor "
                    f"„{stage.agent}“. Ergänze unter „raters“ einen anderen Agenten.",
                )
            )
    return issues


# --------------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------------


class _Stopped(Exception):
    """The run stops (user stop or limit): the topology returns."""


@dataclass(slots=True)
class _Attempt:
    number: int
    result: Result | None = None
    ratings: list[Rating] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        return fmean(r.score for r in self.ratings) if self.ratings else None


def _result_ref(result: Result) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "id": result.id,
        "stage": result.stage,
        "title": result.title,
        "path": result.path,
    }
    if result.path is None:
        ref["body"] = result.body
    return ref


class _Waterfall:
    def __init__(self, ctx: RunContext) -> None:
        ensure_valid(ctx, check)
        self.ctx = ctx
        params = ctx.params
        rubric = _rubric(ctx.config)
        self.rubric = rubric
        self.pass_score = float(_pass_score(params, rubric))
        self.max_rework = int_param(params, "max_rework", MAX_REWORK, [])
        self.on_fail = str(params.get("on_fail") or "continue")
        self.task = require_task(ctx)
        self.accepted: list[Result] = []

    def _check_stop(self) -> None:
        if self.ctx.stop_requested:
            raise _Stopped

    async def run(self) -> None:
        stages = self.ctx.config.stages
        await self.ctx.checkpoint(PLAN, stages=[s.name for s in stages])
        try:
            for stage in stages:
                if not await self._stage(stage):
                    return
        except _Stopped:
            return

    # -- one stage --------------------------------------------------------------------------

    async def _stage(self, stage: StageSpec) -> bool:
        """Work through one stage; False if the run should end (`stop`, `end`)."""
        attempts: list[_Attempt] = []
        allowed = 1 + self.max_rework
        while True:
            attempt = await self._attempt(stage, len(attempts) + 1, attempts[-1:])
            attempts.append(attempt)
            score = attempt.score
            passed = score is not None and score >= self.pass_score
            info = self._attempt_info(stage, attempt)
            if passed:
                await self.ctx.checkpoint(stage.name, **info, decision="passed")
                return await self._accept(stage, attempt, attempts, "passed")
            if len(attempts) < allowed:
                await self.ctx.checkpoint(stage.name, **info, decision="rework")
                continue
            if self.on_fail != "escalate":
                await self.ctx.checkpoint(stage.name, **info, decision="failed")
                if self.on_fail == "continue":
                    return await self._accept_best(stage, attempts)
                return await self._fail(stage, attempts)
            await self.ctx.checkpoint(stage.name, **info, decision="escalated")
            option = await self.ctx.escalate(
                stage.name, self._escalation_summary(stage, attempts), ESCALATION_OPTIONS
            )
            self._check_stop()
            if option is EscalationOption.RETRY:
                allowed += 1
                continue
            if option is EscalationOption.CONTINUE:
                return await self._accept_best(stage, attempts)
            return await self._fail(stage, attempts)

    @staticmethod
    def _attempt_info(stage: StageSpec, attempt: _Attempt) -> dict[str, Any]:
        info: dict[str, Any] = {
            "attempt": attempt.number,
            "agent": stage.agent,
            "result": attempt.result.id if attempt.result else None,
            "score": attempt.score,
            "ratings": len(attempt.ratings),
        }
        if attempt.result is None:
            info["reason"] = "no_result"
        elif not attempt.ratings:
            info["reason"] = "no_rating"
        return info

    async def _accept(
        self, stage: StageSpec, attempt: _Attempt, attempts: list[_Attempt], outcome: str
    ) -> bool:
        assert attempt.result is not None
        self.accepted.append(attempt.result)
        await self.ctx.checkpoint(
            stage.name,
            phase="stage_end",
            outcome=outcome,
            accepted=attempt.result.id,
            attempt=attempt.number,
            score=attempt.score,
            attempts=len(attempts),
        )
        return True

    async def _accept_best(self, stage: StageSpec, attempts: list[_Attempt]) -> bool:
        """`continue`: the attempt with the best score counts as accepted (if there is one)."""
        rated = [a for a in attempts if a.score is not None]
        if rated:
            best = max(rated, key=lambda a: (a.score, a.number))
            return await self._accept(stage, best, attempts, "best_accepted")
        await self.ctx.checkpoint(
            stage.name,
            phase="stage_end",
            outcome="best_accepted",
            accepted=None,
            score=None,
            attempts=len(attempts),
        )
        return True

    async def _fail(self, stage: StageSpec, attempts: list[_Attempt]) -> bool:
        """`stop` / `end`: the run ends regularly with reason `stage_failed`."""
        await self.ctx.checkpoint(
            stage.name,
            phase="stage_end",
            outcome="stage_failed",
            reason="stage_failed",
            accepted=None,
            score=max((a.score for a in attempts if a.score is not None), default=None),
            attempts=len(attempts),
        )
        return False

    def _escalation_summary(self, stage: StageSpec, attempts: list[_Attempt]) -> str:
        scores = ", ".join(
            f"{a.score:.1f}".replace(".", ",") if a.score is not None else "–" for a in attempts
        )
        return (
            f"Stufe „{stage.name}“ ({stage.agent}): {len(attempts)} Versuche nicht bestanden "
            f"(Scores: {scores}; Bestehensgrenze {self.pass_score:g}). Noch ein Versuch, "
            "trotzdem weiter mit dem besten Versuch oder den Run beenden?"
        )

    # -- one attempt ------------------------------------------------------------------------

    def _context(self, stage: StageSpec, number: int, last: list[_Attempt]) -> dict[str, Any]:
        context: dict[str, Any] = {
            "task": self.task,
            "stage": stage.name,
            "attempt": number,
            "produces": stage.produces,
            "accepted": [_result_ref(r) for r in self.accepted],
        }
        if last:
            previous = last[0]
            context["rework"] = {
                "result": previous.result.id if previous.result else None,
                "score": previous.score,
                "pass_score": self.pass_score,
                "ratings": [
                    {
                        "rater": r.rater,
                        "score": r.score,
                        "criteria": r.criteria,
                        "comment": r.comment,
                    }
                    for r in previous.ratings
                ],
            }
        return context

    async def _attempt(self, stage: StageSpec, number: int, last: list[_Attempt]) -> _Attempt:
        attempt = _Attempt(number)
        context = self._context(stage, number, last)
        what = f"Stufe „{stage.name}“, Versuch {number}"
        if stage.produces:
            what += f", erwartet: {stage.produces}"
        prompt = (
            f"{what}. Den Auftrag mit Aufgabe und Ergebnissen der Vorstufen findest du mit "
            "`get_assignment()`."
        )
        if last:
            prompt += " Das ist eine Nacharbeit: Die Ratings deines letzten Versuchs stehen dort "
            prompt += "unter `rework`."
        prompt += " Gib dein Ergebnis mit `submit_result` ab."
        outcome = await self.ctx.turn(stage.agent, prompt, stage=stage.name, context=context)
        self._check_stop()
        attempt.result = _last_result(outcome)
        if attempt.result is None:
            outcome = await self.ctx.turn(
                stage.agent,
                f"Du hast für {what} noch kein Ergebnis abgegeben. Gib es jetzt mit "
                "`submit_result` ab.",
                stage=stage.name,
                context=context,
            )
            self._check_stop()
            attempt.result = _last_result(outcome)
        if attempt.result is not None:
            attempt.ratings = await self._ratings(stage, attempt.result, context)
        return attempt

    async def _ratings(
        self, stage: StageSpec, result: Result, context: dict[str, Any]
    ) -> list[Rating]:
        raters = [r for r in self.ctx.config.raters if r != result.agent]
        rate_context = {
            "task": self.task,
            "stage": stage.name,
            "attempt": context["attempt"],
            "result_id": result.id,
            "result_ids": [result.id],
            "result": _result_ref(result),
            "accepted": context["accepted"],
            "scale": list(self.rubric.scale),
            "criteria": self.rubric.criteria,
        }
        prompt = (
            f"Bewerte das Ergebnis #{result.id} der Stufe „{stage.name}“ mit `submit_rating` "
            "(Score und Kommentar, Scores je Kriterium der Rubrik). Ergebnis, Aufgabe und "
            "Rubrik findest du mit `get_assignment()`."
        )
        await self.ctx.turns([TurnJob(r, prompt, rate_context) for r in raters], stage=stage.name)
        self._check_stop()
        missing = [r for r in raters if r not in self._raters_of(result)]
        if missing:
            again = (
                f"Deine Bewertung von Ergebnis #{result.id} fehlt noch. Gib sie jetzt mit "
                "`submit_rating` ab."
            )
            await self.ctx.turns(
                [TurnJob(r, again, rate_context) for r in missing], stage=stage.name
            )
            self._check_stop()
        return [r for r in self.ctx.ratings(result.id) if r.rater in raters]

    def _raters_of(self, result: Result) -> set[str]:
        return {r.rater for r in self.ctx.ratings(result.id)}


def _last_result(outcome: TurnOutcome) -> Result | None:
    """The last result of a turn; a turn with status `error` counts as one without result."""
    if outcome.status is TurnStatus.ERROR or not outcome.results:
        return None
    return outcome.results[-1]


async def run(ctx: RunContext) -> None:
    """Run `waterfall` (docs/03 §4.4)."""
    await _Waterfall(ctx).run()

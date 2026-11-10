"""Topology `exploration`: ideas are given or generated, explored in parallel and ranked.
See docs/03-skills-und-research-groups.md §4.3 and W4-03 F8.

Phases (`PHASES`, the plan of the run; `open_from` must name one of them, F2):

0. `ideen`: the ideas of `ideas`, or the `ideator` submits `n_ideas` of them
   (`submit_result(kind="idea")`, asked once more if fewer came).
1. `exploration`: explorer `e` gets the ideas `e`, `e + E`, `e + 2E`, … (round robin, one after
   the other). The explorers work in parallel, in lockstep: `turns_per_idea` turns per idea,
   each ending with a state `submit_result(kind="exploration")`; the latest one counts. A
   silent explorer is asked once more after its last turn, then the idea stays without result.
   `exchange`: `none` (isolated: the results of the phase are sealed until the ranking, F4),
   `channel` (the explorers talk in their channel, `channel` names it if there are several),
   `digest` (sealed as well; arr puts title and start of the other explorers' latest results
   into every package, without a model).
2. `bewertung`: the `rankers` rate every result with `submit_rating` (scores per criterion of
   `criteria`), in parallel, a missing rating is asked once more; nobody rates their own.
   The ranking is the mean of the rankers' scores, ties and ideas without score stay in the
   order of the ideas. No rankers: no ranking scores.
3. `zusammenfassung`: arr writes the ranking and an overview into the checkpoint (and so
   into `summary.md`); with `summarizer` that agent writes a summary (F9).

Checkpoints: `ideen` (`ideas`, `source`), `exploration` per slot and turn and at the end
(`results` per idea, `missing`), `bewertung` (`ratings`, `missing`), `zusammenfassung`
(`ranking`: rank, idea, title, result, agent, score, ratings; `overview`).

Parameters: `ideas` (list of texts or `{title, text}`; empty: generate), `n_ideas` (3),
`ideator`, `explorers` (ID, pattern or list; default `explorer*`), `rankers` (same, may be
empty; default `ranker*`), `turns_per_idea` (1), `exchange` (`none`), `channel`, `criteria`
(default: those of the rubric), `summarizer`.
"""

import asyncio
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from arr.backends.base import TurnStatus
from arr.errors import ConfigIssue
from arr.models import ResolvedRun
from arr.room.types import Rating, Result
from arr.runs import topologies
from arr.runs.context import RunContext
from arr.runs.topologies import (
    agent_param,
    agents_param,
    choice_param,
    describe,
    ensure_valid,
    int_param,
    last_result,
    phase_issues,
    require_task,
    suggest,
)
from arr.workfolder import read_template

PHASES = ("ideen", "exploration", "bewertung", "zusammenfassung")
IDEEN, EXPLORATION, BEWERTUNG, ZUSAMMENFASSUNG = PHASES
EXCHANGES = ("none", "channel", "digest")
N_IDEAS = 3
TURNS_PER_IDEA = 1
IDEA_KIND = "idea"
RESULT_KIND = "exploration"
DIGEST_CHARS = 500
"""Start of another explorer's result in a digest (F8)."""
EXCERPT_CHARS = 1500
TEXT_BYTES = 64 * 1024


# --------------------------------------------------------------------------------------------
# Check
# --------------------------------------------------------------------------------------------


def _short(text: str, limit: int = 80) -> str:
    """A title of at most `limit` characters, cut at a word with „…“."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"


def _ideas(value: Any) -> list[dict[str, str]] | None:
    """Given ideas as `{title, text}`; None if the value is no list of texts or sections."""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    ideas = []
    for index, item in enumerate(value, 1):
        if isinstance(item, str) and item.strip():
            title = _short(item.strip().splitlines()[0])
            ideas.append({"title": title, "text": item.strip()})
        elif isinstance(item, dict) and isinstance(item.get("title"), str):
            text = item.get("text")
            ideas.append(
                {"title": item["title"], "text": text if isinstance(text, str) else item["title"]}
            )
        else:
            return None
        ideas[-1]["id"] = f"I{index}"
    return ideas


def _explorers(resolved: ResolvedRun, issues: list[ConfigIssue]) -> list[str]:
    return agents_param(resolved, "explorers", issues, default=["explorer*"])


def _rankers(resolved: ResolvedRun, issues: list[ConfigIssue]) -> list[str]:
    return agents_param(resolved, "rankers", issues, default=["ranker*"])


def _channel(resolved: ResolvedRun, explorers: list[str]) -> str | None:
    value = resolved.params.get("channel")
    if isinstance(value, str):
        return value
    shared = [c.name for c in resolved.groupchats if all(e in c.members for e in explorers)]
    return shared[0] if len(shared) == 1 else None


def check(resolved: ResolvedRun) -> list[ConfigIssue]:
    """Problems of the `exploration` parameters and roles (see `check_topology`)."""
    issues: list[ConfigIssue] = []
    params = resolved.params
    ideas = _ideas(params.get("ideas"))
    if ideas is None:
        issues.append(
            ConfigIssue(
                "params.ideas",
                "Parameter „ideas“ muss eine Liste von Ideen sein (Text oder {title, text}), "
                f"gefunden {describe(params.get('ideas'))}.",
            )
        )
    int_param(params, "n_ideas", N_IDEAS, issues, minimum=1)
    int_param(params, "turns_per_idea", TURNS_PER_IDEA, issues, minimum=1)
    exchange = choice_param(params, "exchange", EXCHANGES, "none", issues)
    ideator = agent_param(resolved, "ideator", issues)
    if not ideas and ideas is not None and ideator is None:
        issues.append(
            ConfigIssue(
                "params.ideator",
                "Ohne vorgegebene Ideen („ideas“) braucht „exploration“ einen „ideator“, der "
                "sie erzeugt.",
            )
        )
    explorers = _explorers(resolved, issues)
    if not explorers:
        issues.append(
            ConfigIssue(
                "params.explorers",
                "„exploration“ braucht Explorer. Nenne sie im Parameter „explorers“ (z. B. "
                "„explorer-*“) oder lege Agenten „explorer“ an.",
            )
        )
    _rankers(resolved, issues)
    agent_param(resolved, "summarizer", issues)
    names = [c.name for c in resolved.groupchats]
    if exchange == "channel" and explorers:
        name = _channel(resolved, explorers)
        if name is None or name not in names:
            text = (
                f"Parameter „channel“ nennt „{name}“, den Kanal gibt es nicht."
                f"{suggest(name, names)}"
                if name is not None
                else "„exchange: channel“ braucht einen Kanal mit allen Explorern; nenne ihn "
                "im Parameter „channel“."
            )
            issues.append(ConfigIssue("params.channel", text))
        else:
            outside = [e for e in explorers if e not in resolved.channel(name).members]
            if outside:
                issues.append(
                    ConfigIssue(
                        "params.channel",
                        f"Die Explorer {', '.join(outside)} sind nicht Mitglied im Kanal „{name}“.",
                    )
                )
    criteria = params.get("criteria")
    known = (resolved.rubric.criteria if resolved.rubric else None) or []
    if criteria is not None:
        if not isinstance(criteria, list) or not all(isinstance(c, str) for c in criteria):
            issues.append(
                ConfigIssue(
                    "params.criteria",
                    f"Parameter „criteria“ muss eine Liste von Namen sein, gefunden "
                    f"{describe(criteria)}.",
                )
            )
        elif known and set(criteria) - set(known):
            unknown = ", ".join(sorted(set(criteria) - set(known)))
            issues.append(
                ConfigIssue(
                    "params.criteria",
                    f"Die Kriterien {unknown} stehen nicht in der Rubrik ({', '.join(known)}); "
                    "Ratings nach ihnen würden abgelehnt.",
                )
            )
    issues += phase_issues(resolved, PHASES)
    return issues


# --------------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------------


class _Stopped(Exception):
    """The run stops (user stop or limit): the topology returns."""


@dataclass(slots=True)
class _Idea:
    id: str
    title: str
    text: str
    result: Result | None = None
    explorer: str | None = None
    ratings: list[Rating] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        return fmean(r.score for r in self.ratings) if self.ratings else None


class _Exploration:
    def __init__(self, ctx: RunContext) -> None:
        ensure_valid(ctx, check)
        self.ctx = ctx
        params = ctx.params
        config = ctx.config
        self.task = require_task(ctx)
        self.given = _ideas(params.get("ideas")) or []
        self.n_ideas = int_param(params, "n_ideas", N_IDEAS, [])
        self.turns = int_param(params, "turns_per_idea", TURNS_PER_IDEA, [])
        self.exchange = str(params.get("exchange") or "none")
        self.ideator = params.get("ideator")
        self.explorers = _explorers(config, [])
        self.rankers = _rankers(config, [])
        self.channel = _channel(config, self.explorers)
        self.summarizer = params.get("summarizer")
        rubric = config.rubric
        self.criteria = list(params.get("criteria") or (rubric.criteria if rubric else []))
        self.scale = list(rubric.scale) if rubric else None
        self.guide = read_template("prompts", "exploration_explorer.md")
        self.ideas: list[_Idea] = []

    def _check_stop(self) -> None:
        if self.ctx.stop_requested:
            raise _Stopped

    async def run(self) -> None:
        try:
            await self._ideas()
            if self.ideas:
                await self._explore()
                await self._rank()
            await self._finish()
        except _Stopped:
            return

    # -- (0) ideas ------------------------------------------------------------------------

    async def _ideas(self) -> None:
        if self.given:
            self.ideas = [_Idea(i["id"], i["title"], i["text"]) for i in self.given]
            await self.ctx.checkpoint(IDEEN, ideas=len(self.ideas), source="vorgegeben")
            return
        assert isinstance(self.ideator, str), "checked by ensure_valid"
        n = self.n_ideas
        mark = max((r.id for r in self.ctx.results()), default=0)
        prompt = (
            f"## Aufgabe\n\n{self.task}\n\nFinde {n} möglichst verschiedene Ansätze für diese "
            f'Aufgabe. Gib jeden einzeln mit `submit_result(kind="{IDEA_KIND}")` ab: Titel und '
            "eine kurze Beschreibung, wie man vorgehen würde. Explorer arbeiten die Ansätze "
            "danach parallel aus."
        )
        context = {"task": self.task, "n_ideas": n}
        await self.ctx.turn(self.ideator, prompt, stage=IDEEN, context=context)
        self._check_stop()
        found = self._new_ideas(mark)
        if len(found) < n:
            await self.ctx.turn(
                self.ideator,
                f"Bisher hast du {len(found)} von {n} Ansätzen abgegeben. Gib die übrigen jetzt "
                f'mit `submit_result(kind="{IDEA_KIND}")` ab, jeden einzeln.',
                stage=IDEEN,
                context=context,
            )
            self._check_stop()
            found = self._new_ideas(mark)
        for index, result in enumerate(found[:n], 1):
            text = self.ctx.result_text(result.id, TEXT_BYTES) or result.title
            self.ideas.append(_Idea(f"I{index}", result.title, text))
        await self.ctx.checkpoint(
            IDEEN,
            ideas=len(self.ideas),
            source=self.ideator,
            results=[r.id for r in found[:n]],
            missing=max(0, n - len(found)),
        )

    def _new_ideas(self, mark: int) -> list[Result]:
        return [r for r in self.ctx.results(agent=self.ideator, stage=IDEEN) if r.id > mark]

    # -- (1) exploration ------------------------------------------------------------------

    async def _explore(self) -> None:
        sealed = self.exchange in ("none", "digest")
        if sealed:  # isolated explorers do not read each other's files (F4 as a variable)
            self.ctx.seal_results(EXPLORATION, None)
        explorers = self.explorers
        slots = [
            self.ideas[start : start + len(explorers)]
            for start in range(0, len(self.ideas), len(explorers))
        ]
        try:
            for slot_number, slot in enumerate(slots, 1):
                pairs = list(zip(explorers, slot, strict=False))
                for idea_explorer, idea in pairs:
                    idea.explorer = idea_explorer
                for turn in range(1, self.turns + 1):
                    await self.ctx.checkpoint(
                        EXPLORATION,
                        slot=slot_number,
                        turn=turn,
                        of=self.turns,
                        ideas={i.id: i.explorer for _, i in pairs},
                    )
                    await asyncio.gather(*(self._work(e, idea, turn) for e, idea in pairs))
                    self._check_stop()
                await asyncio.gather(*(self._ask_silent(e, idea) for e, idea in pairs))
                self._check_stop()
        finally:
            if sealed:
                self.ctx.release_results(EXPLORATION)
        await self.ctx.checkpoint(
            EXPLORATION,
            phase="end",
            results={i.id: i.result.id if i.result else None for i in self.ideas},
            missing=[i.id for i in self.ideas if i.result is None],
        )

    def _package(self, explorer: str, idea: _Idea, turn: int) -> dict[str, Any]:
        package: dict[str, Any] = {
            "task": self.task,
            "idea": {"id": idea.id, "title": idea.title, "text": idea.text},
            "turn": turn,
            "turns": self.turns,
            "exchange": self.exchange,
        }
        if self.exchange == "channel" and self.channel:
            package["channel"] = self.channel
        if self.exchange == "digest":
            package["digest"] = self._digest(explorer)
        if idea.result is not None:
            package["previous"] = {"result": idea.result.id, "title": idea.result.title}
        return package

    def _digest(self, explorer: str) -> list[dict[str, Any]]:
        """F8: title and start of the other explorers' latest results, built by arr."""
        digest = []
        for idea in self.ideas:
            result = idea.result
            if result is None or idea.explorer == explorer:
                continue
            text = self.ctx.result_text(result.id, TEXT_BYTES)
            digest.append(
                {
                    "idea": idea.title,
                    "by": self.ctx.shown_name(explorer, result.agent),
                    "title": result.title,
                    "start": text[:DIGEST_CHARS] + (" …" if len(text) > DIGEST_CHARS else ""),
                }
            )
        return digest

    async def _work(self, explorer: str, idea: _Idea, turn: int) -> None:
        last = turn == self.turns
        prompt = (
            f"Idee {idea.id} „{idea.title}“, Arbeitsrunde {turn} von {self.turns}. Aufgabe, Idee"
            + (" und den Auszug der anderen" if self.exchange == "digest" else "")
            + " stehen in `get_assignment()`."
        )
        if self.exchange == "channel" and self.channel:
            prompt += f" Tauscht euch in #{self.channel} aus."
        prompt += f' Gib am Ende deinen Stand mit `submit_result(kind="{RESULT_KIND}")` ab' + (
            " – das ist deine letzte Runde zu dieser Idee." if last else "."
        )
        if turn == 1:
            prompt += "\n\n" + self.guide
        outcome = await self.ctx.turn(
            explorer, prompt, stage=EXPLORATION, context=self._package(explorer, idea, turn)
        )
        result = last_result(outcome)
        if result is not None:
            idea.result = result

    async def _ask_silent(self, explorer: str, idea: _Idea) -> None:
        """An explorer without any result for its idea is asked once more (F8, §3.5)."""
        asked = 0
        while idea.result is None and asked < topologies.ASK_AGAIN and not self.ctx.stop_requested:
            outcome = await self.ctx.turn(
                explorer,
                f"Zu Idee {idea.id} „{idea.title}“ hast du noch keinen Stand abgegeben. Gib ihn "
                f'jetzt mit `submit_result(kind="{RESULT_KIND}")` ab, auch wenn die Idee nicht '
                "trägt.",
                stage=EXPLORATION,
                context=self._package(explorer, idea, self.turns),
            )
            idea.result = last_result(outcome)
            asked += 1
            if outcome.status is TurnStatus.LIMIT:
                break

    # -- (2) ranking ----------------------------------------------------------------------

    def _rate_package(self, ranker: str, todo: list[_Idea]) -> dict[str, Any]:
        results = []
        for idea in todo:
            assert idea.result is not None
            result = idea.result
            text = self.ctx.result_text(result.id, TEXT_BYTES)
            entry: dict[str, Any] = {
                "result_id": result.id,
                "idea": idea.title,
                "title": result.title,
                "by": self.ctx.shown_name(ranker, result.agent),
                "excerpt": text[:EXCERPT_CHARS] + (" …" if len(text) > EXCERPT_CHARS else ""),
            }
            if result.path:
                entry["path"] = str(self.ctx.run_folder / result.path)
            results.append(entry)
        return {
            "task": self.task,
            "result_ids": [i.result.id for i in todo if i.result],
            "results": results,
            "criteria": self.criteria,
            "scale": self.scale,
        }

    def _todo(self, ranker: str) -> list[_Idea]:
        return [
            i
            for i in self.ideas
            if i.result is not None
            and i.result.agent != ranker
            and not any(r.rater == ranker for r in self.ctx.ratings(i.result.id))
        ]

    async def _rank(self) -> None:
        rated = [i for i in self.ideas if i.result is not None]
        if not self.rankers or not rated:
            await self.ctx.checkpoint(
                BEWERTUNG, ratings=0, reason="no_rankers" if rated else "no_results"
            )
            return
        await self.ctx.checkpoint(BEWERTUNG, rankers=self.rankers, results=len(rated))
        await asyncio.gather(*(self._ranker(r) for r in self.rankers))
        self._check_stop()
        for idea in rated:
            assert idea.result is not None
            idea.ratings = [r for r in self.ctx.ratings(idea.result.id) if r.rater in self.rankers]
        await self.ctx.checkpoint(
            BEWERTUNG,
            phase="end",
            ratings=sum(len(i.ratings) for i in rated),
            missing={r: [i.id for i in self._todo(r)] for r in self.rankers if self._todo(r)},
        )

    async def _ranker(self, ranker: str) -> None:
        todo = self._todo(ranker)
        if not todo:
            return
        criteria = f" nach den Kriterien {', '.join(self.criteria)}" if self.criteria else ""
        prompt = (
            f"Bewerte die {len(todo)} Ergebnisse der Exploration{criteria} mit `submit_rating`, "
            "je Ergebnis einmal (Score, Kommentar, Scores je Kriterium). Die Ergebnisse mit "
            "Auszug und Pfad stehen in `get_assignment()` unter `results`."
        )
        outcome = await self.ctx.turn(
            ranker, prompt, stage=BEWERTUNG, context=self._rate_package(ranker, todo)
        )
        self._check_stop()
        asked = 0
        # „Ranker fehlt“: asked again (`ASK_AGAIN`), then the ranking goes on without
        while (
            (todo := self._todo(ranker))
            and asked < topologies.ASK_AGAIN
            and outcome.status is not TurnStatus.LIMIT
        ):
            self._check_stop()
            asked += 1
            ids = ", ".join(f"#{i.result.id}" for i in todo if i.result)
            outcome = await self.ctx.turn(
                ranker,
                f"Deine Bewertung fehlt noch für {ids}. Gib sie jetzt mit `submit_rating` ab.",
                stage=BEWERTUNG,
                context=self._rate_package(ranker, todo),
            )

    # -- (3) summary ----------------------------------------------------------------------

    def _ranking(self) -> list[dict[str, Any]]:
        """F8: mean score of the rankers; ties and ideas without score keep the idea order."""
        order = sorted(
            range(len(self.ideas)),
            key=lambda n: (self.ideas[n].score is None, -(self.ideas[n].score or 0.0), n),
        )
        ranking = []
        for rank, index in enumerate(order, 1):
            idea = self.ideas[index]
            ranking.append(
                {
                    "rank": rank,
                    "idea": idea.id,
                    "title": idea.title,
                    "result": idea.result.id if idea.result else None,
                    "agent": idea.explorer,
                    "score": idea.score,
                    "ratings": len(idea.ratings),
                }
            )
        return ranking

    def _overview(self, ranking: list[dict[str, Any]], name: Any) -> str:
        if not ranking:
            return "Keine Ideen: Es gab nichts zu erkunden."
        lines = ["| Rang | Idee | Explorer | Ergebnis | Score |", "|---|---|---|---|---|"]
        for row in ranking:
            score = "–" if row["score"] is None else f"{row['score']:.1f}".replace(".", ",")
            result = f"#{row['result']}" if row["result"] else "keins"
            who = name(row["agent"]) if row["agent"] else "–"
            lines.append(f"| {row['rank']} | {row['title']} | {who} | {result} | {score} |")
        if not self.rankers:
            lines += ["", "Ohne Ranker: Die Reihenfolge ist die der Ideen."]
        return "\n".join(lines)

    async def _finish(self) -> None:
        ranking = self._ranking()
        await self.ctx.checkpoint(
            ZUSAMMENFASSUNG,
            ranking=ranking,
            overview=self._overview(ranking, lambda a: a),
            exchange=self.exchange,
        )
        if not self.summarizer or self.ctx.stop_requested:
            return
        viewer = self.summarizer
        await self.ctx.turn(
            self.summarizer,
            "Die Exploration ist beendet. Fasse sie zusammen: die Ideen, was jede ergab, die "
            "Rangliste und offene Punkte. Rangliste und Ergebnisse stehen in "
            '`get_assignment()`. Gib die Zusammenfassung mit `submit_result(kind="summary")` ab.',
            stage=ZUSAMMENFASSUNG,
            context={
                "task": self.task,
                "overview": self._overview(ranking, lambda a: self.ctx.shown_name(viewer, a)),
                "results": [
                    {"idea": i.title, "result": i.result.id, "title": i.result.title}
                    for i in self.ideas
                    if i.result is not None
                ],
            },
        )


async def run(ctx: RunContext) -> None:
    """Run `exploration` (docs/03 §4.3)."""
    await _Exploration(ctx).run()

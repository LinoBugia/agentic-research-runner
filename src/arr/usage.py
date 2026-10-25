"""Usage accounting per run, agent, model and auth profile.

The `turn_end` events of the agents' `transcript.jsonl` are the source: each carries the
tokens of the turn, its cost and `cost_kind` (docs/02-workfolder-und-runs.md §6). `usage`
events (per model request) are informational and not summed, so nothing is counted twice.
`usage.json` is written by the finalizer; the index in `.arr/arr.sqlite` can be rebuilt
from the same records (docs/02 §8).

`cost_kind` says whether a cost is `billed` (API key) or `estimated` (subscription, shown with
„≈“, docs/08-ux-konzept.md §8.7); `None` means unknown, e.g. Codex reports no cost. When
parts are summed, `Usage.__add__` decides the kind of the sum. Because a mixed sum cannot say
how much was billed and how much estimated (docs/08 §5.9), every `UsageTotal` also keeps the
amounts apart: `cost_billed_usd`, `cost_estimated_usd` and the number of turns without a known
cost (`cost_unknown_turns`).

Only finished turns count: a turn without `turn_end` (crash) adds nothing to `usage.json`,
even if `usage` events with tokens were logged. Such usage is not made up afterwards.
"""

import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from arr.backends.base import CostKind, Usage
from arr.models import AgentConfig, Record
from arr.runs.eventlog import TRANSCRIPT_FILE, read_events

log = logging.getLogger(__name__)

USAGE_FILE = "usage.json"
UNKNOWN = "unknown"
"""Key in `by_model` / `by_profile` for turns whose model or profile is not known."""

_TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning")


class UsageRecord(Record):
    """Usage of one finished turn (one `turn_end`), the row type of the index (docs/02 §8)."""

    agent: str
    turn: int
    ts: float
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    usage: Usage


class UsageTotal(Record):
    """Summed usage with the number of turns it covers.

    `usage` is the sum by `Usage.__add__` (one cost, one kind). The three fields below keep
    the cost apart by kind: `cost_billed_usd` (API key) and `cost_estimated_usd`
    (subscription, „≈“) are exact; `cost_unknown_turns` counts turns without a known cost
    (no `cost_usd`, e.g. Codex) or with a cost of unknown kind.
    """

    usage: Usage = Field(default_factory=Usage)
    turns: int = 0
    cost_billed_usd: float = Field(default=0, ge=0)
    cost_estimated_usd: float = Field(default=0, ge=0)
    cost_unknown_turns: int = Field(default=0, ge=0)

    def add(self, usage: Usage) -> None:
        """Add the usage of one turn."""
        self.usage = self.usage + usage
        self.turns += 1
        if usage.cost_usd is not None and usage.cost_kind is CostKind.BILLED:
            self.cost_billed_usd += usage.cost_usd
        elif usage.cost_usd is not None and usage.cost_kind is CostKind.ESTIMATED:
            self.cost_estimated_usd += usage.cost_usd
        else:
            self.cost_unknown_turns += 1


class AgentUsage(UsageTotal):
    """Totals of one agent, with the profile and provider it ran on."""

    profile: str | None = None
    provider: str | None = None
    models: dict[str, UsageTotal] = Field(default_factory=dict)


class RunUsage(Record):
    """Content of `usage.json`: sums per run, agent, model and auth profile (docs/02 §4, §8)."""

    version: int = 1
    run: str | None = None
    total: UsageTotal = Field(default_factory=UsageTotal)
    by_agent: dict[str, AgentUsage] = Field(default_factory=dict)
    by_model: dict[str, UsageTotal] = Field(default_factory=dict)
    by_profile: dict[str, UsageTotal] = Field(default_factory=dict)


def _cost_kind(value: Any) -> CostKind | None:
    try:
        return CostKind(value) if value is not None else None
    except ValueError:
        return None


def usage_records(
    events: Iterable[Mapping[str, Any]],
    config: AgentConfig | None = None,
    *,
    agent: str | None = None,
) -> list[UsageRecord]:
    """Extract one `UsageRecord` per `turn_end` from normalized events.

    The model is `turn_end.data.usage.model`, else the model of `config`. Records with
    invalid numbers are skipped with a warning (a damaged log must not break the summary).
    """
    records: list[UsageRecord] = []
    for event in events:
        if event.get("kind") != "turn_end":
            continue
        data = event.get("data") or {}
        tokens = data.get("usage") or {}
        try:
            records.append(
                UsageRecord(
                    agent=agent or str(event.get("agent", config.id if config else UNKNOWN)),
                    turn=int(event.get("turn", 0)),
                    ts=float(event.get("ts", 0)),
                    profile=config.auth if config else None,
                    provider=str(config.provider) if config else None,
                    model=tokens.get("model") or (config.model if config else None),
                    usage=Usage(
                        **{name: tokens.get(name) for name in _TOKEN_FIELDS},
                        cost_usd=data.get("cost_usd"),
                        cost_kind=_cost_kind(data.get("cost_kind")),
                    ),
                )
            )
        except (ValidationError, AttributeError, TypeError, ValueError):
            log.warning("turn_end mit ungültiger Usage übersprungen (seq %s)", event.get("seq"))
    return records


def aggregate_usage(
    records: Iterable[UsageRecord],
    *,
    run_id: str | None = None,
    agents: Iterable[str] = (),
) -> RunUsage:
    """Sum records per agent, model, profile and for the whole run.

    `agents` lists agents that should appear even without a finished turn (zero rows).
    """
    result = RunUsage(run=run_id)
    for agent in agents:
        result.by_agent.setdefault(agent, AgentUsage())
    for record in records:
        agent = result.by_agent.setdefault(record.agent, AgentUsage())
        agent.profile = agent.profile or record.profile
        agent.provider = agent.provider or record.provider
        model = record.model or UNKNOWN
        profile = record.profile or UNKNOWN
        result.total.add(record.usage)
        agent.add(record.usage)
        agent.models.setdefault(model, UsageTotal()).add(record.usage)
        result.by_model.setdefault(model, UsageTotal()).add(record.usage)
        result.by_profile.setdefault(profile, UsageTotal()).add(record.usage)
    return result


def compute_run_usage(
    run_dir: str | Path,
    configs: Mapping[str, AgentConfig] | Iterable[AgentConfig],
    *,
    run_id: str | None = None,
) -> RunUsage:
    """Read `agents/<id>/transcript.jsonl` of a run folder and aggregate its usage."""
    run_dir = Path(run_dir)
    by_id = configs if isinstance(configs, Mapping) else {c.id: c for c in configs}
    agents_dir = run_dir / "agents"
    on_disk = [p.name for p in agents_dir.iterdir() if p.is_dir()] if agents_dir.is_dir() else []
    ids = sorted({*by_id, *on_disk})
    records: list[UsageRecord] = []
    for agent_id in ids:
        events = read_events(agents_dir / agent_id / TRANSCRIPT_FILE)
        records += usage_records(events, by_id.get(agent_id), agent=agent_id)
    return aggregate_usage(records, run_id=run_id or run_dir.name, agents=ids)


def write_usage_json(
    run_dir: str | Path,
    configs: Mapping[str, AgentConfig] | Iterable[AgentConfig],
    *,
    run_id: str | None = None,
) -> Path:
    """Compute the usage of a run folder and write `usage.json` (atomically); return its path."""
    run_dir = Path(run_dir)
    usage = compute_run_usage(run_dir, configs, run_id=run_id)
    target = run_dir / USAGE_FILE
    temp = target.with_suffix(".json.tmp")
    temp.write_text(usage.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return target

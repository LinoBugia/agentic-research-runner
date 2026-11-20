"""W4b-09 A1: the rule „which request does the user decide because of `decider: user`“ as a
pure function (`arr.permissions.tightening.tightened_by`)."""

from typing import Any

import pytest
from decider_kit import Where, cfg, phase

from arr.permissions.tightening import tightened_by
from arr.permissions.types import TightenedBy, TightenedSource

AGENT, PHASE, UNKNOWN = TightenedSource.AGENT, TightenedSource.PHASE, TightenedSource.UNKNOWN

PLAIN = [phase("exploration", ["a", "b"]), phase("pruefen", ["a", "b"], index=1)]
TIGHT = [phase("exploration", ["a", "b"]), phase("beweis", ["a"], "user", 1)]

CASES: list[tuple[str, list[Any], list[Any], str, dict[str, tuple[str, int]], Any]] = [
    # (id, agents, phases, asking agent, where they are, expected)
    ("keins", [cfg("a"), cfg("b")], PLAIN, "a", {"a": ("pruefen", 1)}, None),
    ("agent", [cfg("a", "user"), cfg("b")], PLAIN, "a", {"a": ("pruefen", 1)}, AGENT),
    ("agent-ohne-phase", [cfg("a", "user"), cfg("b")], PLAIN, "a", {}, AGENT),
    ("agent-in-alter-topologie", [cfg("a", "user")], [], "a", {}, AGENT),
    ("anderer-agent", [cfg("a", "user"), cfg("b")], PLAIN, "b", {}, None),
    ("phase", [cfg("a"), cfg("b")], TIGHT, "a", {"a": ("beweis", 2)}, ("beweis", 2)),
    ("phase-ohne-user", [cfg("a"), cfg("b")], TIGHT, "a", {"a": ("exploration", 1)}, None),
    ("beides", [cfg("a", "user"), cfg("b")], TIGHT, "a", {"a": ("beweis", 1)}, AGENT),
    ("keine-phase-betreten", [cfg("a"), cfg("b")], TIGHT, "a", {}, UNKNOWN),
    ("keine-phase-ohne-user-phase", [cfg("a", "user"), cfg("b")], PLAIN, "b", {}, None),
    ("fremder-phasenname", [cfg("a"), cfg("b")], TIGHT, "a", {"a": ("gibtsnicht", 1)}, UNKNOWN),
    ("unbekannter-agent", [cfg("a"), cfg("b")], TIGHT, "x", {"x": ("exploration", 1)}, UNKNOWN),
    ("unbekannter-agent-nur-agent", [cfg("a", "user")], PLAIN, "x", {}, UNKNOWN),
    ("unbekannter-agent-ohne-decider", [cfg("a"), cfg("b")], PLAIN, "x", {}, None),
]


def expect(value: Any) -> TightenedBy | None:
    if value is None:
        return None
    if isinstance(value, tuple):
        return TightenedBy(source=PHASE, stage=value[0], attempt=value[1])
    return TightenedBy(source=value)


@pytest.mark.parametrize(
    ("agents", "phases", "agent", "where", "expected"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_a1_the_rule_in_every_case(
    agents: list[Any], phases: list[Any], agent: str, where: dict[str, Any], expected: Any
) -> None:
    assert tightened_by(agents, phases, agent, Where(dict(where))) == expect(expected)


def test_a1_without_any_decider_nothing_is_looked_up() -> None:
    where = Where(broken=True)  # a lookup would raise and count
    for agent in ("a", "b", "unbekannt"):
        assert tightened_by([cfg("a"), cfg("b")], PLAIN, agent, where) is None
    assert where.lookups == 0


def test_a1_an_agent_setting_needs_no_lookup() -> None:
    where = Where(broken=True)
    found = tightened_by([cfg("a", "user")], TIGHT, "a", where)
    assert found == TightenedBy(source=AGENT) and where.lookups == 0


def test_a1_only_tightened_agents_look_up_nothing_either() -> None:
    where = Where(broken=True)
    assert tightened_by([cfg("a", "user"), cfg("b")], PLAIN, "b", where) is None
    assert where.lookups == 0


def test_a1_a_lookup_that_raises_is_unknown() -> None:
    where = Where({"a": ("exploration", 1)}, broken=True)
    assert tightened_by([cfg("a")], TIGHT, "a", where) == TightenedBy(source=UNKNOWN)
    assert where.lookups == 1


def test_a1_no_lookup_at_all_with_a_tightened_phase_is_unknown() -> None:
    assert tightened_by([cfg("a")], TIGHT, "a", None) == TightenedBy(source=UNKNOWN)
    assert tightened_by([cfg("a", "user")], TIGHT, "a", None) == TightenedBy(source=AGENT)
    assert tightened_by([cfg("a")], PLAIN, "a", None) is None


def test_a1_the_phase_is_read_at_every_call() -> None:
    where = Where({"a": ("exploration", 1)})
    agents = [cfg("a"), cfg("b")]
    assert tightened_by(agents, TIGHT, "a", where) is None
    where.enter("a", "beweis", 1)
    assert tightened_by(agents, TIGHT, "a", where) == expect(("beweis", 1))
    where.enter("a", "exploration", 2)
    assert tightened_by(agents, TIGHT, "a", where) is None
    where.enter("a", "beweis", 2)
    assert tightened_by(agents, TIGHT, "a", where) == expect(("beweis", 2))


def test_a1_the_record_only_takes_valid_combinations() -> None:
    with pytest.raises(ValueError):
        TightenedBy(source=PHASE)
    with pytest.raises(ValueError):
        TightenedBy(source=AGENT, stage="beweis", attempt=1)
    with pytest.raises(ValueError):
        TightenedBy(source=PHASE, stage="beweis")
    with pytest.raises(ValueError):
        TightenedBy(source=PHASE, stage="beweis", attempt=0)
    with pytest.raises(ValueError):
        TightenedBy.model_validate({"source": "lockerer"})

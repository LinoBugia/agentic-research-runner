"""W4b-05b.1 (G7): the FakeBackend's fill level, mutations M42 and M43."""

from typing import Any

from arr.backends.base import EventKind
from arr.backends.fake import FakeBackend
from arr.models import AgentConfig


def backend(*turns: list[dict[str, Any]]) -> FakeBackend:
    config = AgentConfig(id="a", base_id="a", provider="claude", model="fake-model", auth="p")
    return FakeBackend(config, {"turns": [{"steps": steps} for steps in turns]})


async def end_of(fake: FakeBackend) -> dict[str, Any]:
    events = [e async for e in fake.run_turn("x")]
    assert events[-1].kind is EventKind.TURN_END
    return events[-1].data


async def test_m42_reset_forgets_the_fill_level() -> None:
    fake = backend([{"do": "usage", "input": 700}], [], [])
    await fake.start()
    assert (await end_of(fake))["context_tokens"] == 700
    assert (await end_of(fake))["context_tokens"] == 700  # counter-check: no reset, it stays
    await fake.reset()
    assert (await end_of(fake))["context_tokens"] is None


async def test_m43_requests_of_another_model_do_not_count() -> None:
    other = {"do": "usage", "input": 900_000, "model": "anderes-modell"}
    fake = backend([{"do": "usage", "input": 700}, other], [other])
    await fake.start()
    assert (await end_of(fake))["context_tokens"] == 700
    assert (await end_of(fake))["context_tokens"] == 700
    # counter-check: the agent's own model, named explicitly, counts
    own = backend([{"do": "usage", "input": 50, "model": "fake-model"}])
    await own.start()
    assert (await end_of(own))["context_tokens"] == 50

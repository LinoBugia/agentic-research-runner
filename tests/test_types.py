"""Contract checks for the interface types of W1-01 §3 points 3-6 (A4)."""

import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import (
    AgentBackend,
    AgentEvent,
    CostKind,
    EventKind,
    TurnOutcome,
    TurnStatus,
    Usage,
)
from arr.models import AgentConfig, AgentStatus
from arr.permissions.types import (
    ApprovalItem,
    ApprovalStatus,
    ApprovalType,
    Change,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    PermissionRequest,
    Risk,
    RiskLevel,
)
from arr.room.types import (
    Assignment,
    ChannelInfo,
    Message,
    MessageKind,
    MessageVia,
    Rating,
    Result,
    RoomAPI,
    Vote,
)
from arr.runs.context import AgentHandle, RunContext

ROOM_TOOLS = [  # docs/06-gruppenchats.md §9
    "inbox",
    "list_channels",
    "read_messages",
    "post_message",
    "get_attachment",
    "vote",
    "set_status",
    "submit_result",
    "submit_rating",
    "get_assignment",
    "give_floor",
    "call_vote",
    "pin",
    "close_discussion",
    "wait_until",  # docs/09 §4.3
]


def fields(model: Any) -> set[str]:
    return set(model.model_fields)


def test_backend_types() -> None:
    assert [k.value for k in EventKind] == [
        "turn_start",
        "text",
        "thinking",
        "tool_call",
        "tool_result",
        "usage",
        "rate_limit",
        "error",
        "turn_end",
        "hint",
    ]
    assert {"input", "output", "cache_read", "cache_write", "reasoning", "cost_usd"} <= fields(
        Usage
    )
    assert {c.value for c in CostKind} == {"billed", "estimated"}
    assert Usage().cost_kind is None
    assert {"agent_id", "turn", "status", "usage", "results"} <= fields(TurnOutcome)
    assert AgentBackend._is_protocol  # type: ignore[attr-defined]
    for method in ("start", "run_turn", "interrupt", "deliver_hint", "session_ref", "close"):
        assert hasattr(AgentBackend, method)


def test_agent_event_record_matches_transcript_format() -> None:
    event = AgentEvent("run-1", "explorer-1", 1, EventKind.TEXT, {"text": "hi"}, seq=3, ts=1.5)
    assert event.to_record() == {
        "seq": 3,
        "ts": 1.5,
        "run": "run-1",
        "agent": "explorer-1",
        "turn": 1,
        "kind": "text",
        "data": {"text": "hi"},
    }


def test_usage_addition() -> None:
    a = Usage(input=10, output=5, cost_usd=0.1, cost_kind=CostKind.BILLED)
    b = Usage(input=1, reasoning=7, cost_usd=0.2, cost_kind=CostKind.BILLED)
    total = a + b
    assert (total.input, total.output, total.reasoning) == (11, 5, 7)
    assert total.cost_usd == pytest.approx(0.3)
    assert total.cost_kind is CostKind.BILLED
    mixed = a + Usage(cost_usd=1, cost_kind=CostKind.ESTIMATED)
    assert mixed.cost_kind is CostKind.ESTIMATED
    assert (Usage() + Usage()).input is None
    assert (Usage() + a).cost_kind is CostKind.BILLED
    assert a.tokens()["input"] == 10


def test_run_context_is_abstract() -> None:
    assert inspect.isabstract(RunContext)
    expected = {"turn", "turns", "post", "results", "ratings", "checkpoint", "escalate"}
    expected |= {"run_id", "config", "params", "agents", "stop_requested"}
    assert expected <= RunContext.__abstractmethods__
    with pytest.raises(TypeError):
        RunContext()  # type: ignore[abstract]


def test_agent_handle() -> None:
    config = AgentConfig(id="a-1", base_id="a", provider="claude", model="m", auth="p")
    handle = AgentHandle(config)
    assert (handle.id, handle.turns_used, handle.status) == ("a-1", 0, AgentStatus.IDLE)


def test_room_types() -> None:
    assert len(MessageKind) == 12
    assert MessageKind.PASS == "pass"
    assert {v.value for v in MessageVia} == {"gui", "user_agent"}
    assert {"id", "ts", "sender", "channel", "kind", "body", "to", "reply_to", "refs"} <= fields(
        Message
    )
    assert {"pinned", "open", "via", "delivery", "sender_alias"} <= fields(Message)
    assert {"name", "members", "mode", "open", "anonymous"} <= fields(ChannelInfo)
    assert {"agent", "stage", "kind", "title", "body", "path", "attempt"} <= fields(Result)
    assert {"rater", "result_id", "score", "criteria", "comment"} <= fields(Rating)
    assert {"proposal_id", "agent", "choice", "reason"} <= fields(Vote)
    assert {"agent", "turn", "stage", "prompt", "context"} <= fields(Assignment)
    assert RoomAPI._is_protocol  # type: ignore[attr-defined]
    for tool in ROOM_TOOLS:
        assert callable(getattr(RoomAPI, tool)), tool


def test_room_api_is_runtime_checkable() -> None:
    def tool(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    room_cls = type("Room", (), {"agent_id": "a", **dict.fromkeys(ROOM_TOOLS, tool)})
    assert isinstance(room_cls(), RoomAPI)

    class Incomplete:
        agent_id = "a"

        def inbox(self) -> None: ...

    assert not isinstance(Incomplete(), RoomAPI)


class _Backend:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    async def start(self) -> None: ...

    async def run_turn(self, prompt: str, *, stage: str | None = None) -> AsyncIterator[AgentEvent]:
        yield AgentEvent("r", self.config.id, 1, EventKind.TURN_START, {"prompt": prompt})

    async def interrupt(self) -> None: ...

    async def deliver_hint(self, text: str) -> bool:
        return False

    def session_ref(self) -> dict[str, str]:
        return {}

    async def close(self) -> None: ...


async def test_agent_backend_is_runtime_checkable() -> None:
    config = AgentConfig(id="a", base_id="a", provider="codex", model="m", auth="p")
    backend = _Backend(config)
    assert isinstance(backend, AgentBackend)
    events = [event async for event in backend.run_turn("go")]
    assert events[0].kind is EventKind.TURN_START


def test_permission_types() -> None:
    assert {
        "agent_id",
        "run_id",
        "tool",
        "command",
        "paths",
        "urls",
        "reason",
        "categories",
    } <= fields(PermissionRequest)
    assert {r.value for r in RiskLevel} == {"low", "medium", "high"}
    assert {a.value for a in DecisionAction} >= {"allow", "deny", "ask", "adjust"}
    assert {s.value for s in DecisionScope} == {"once", "agent", "run", "always"}
    assert {d.value for d in DecidedBy} == {"user", "gatekeeper", "mandate_rule", "fixed_rule"}
    assert {"action", "scope", "decided_by", "reason", "changes"} <= fields(Decision)
    assert {t.value for t in ApprovalType} == {"run_start", "permission", "escalation", "question"}
    assert {s.value for s in ApprovalStatus} == {
        "open",
        "decided",
        "expired",
        "revoked",
        "obsolete",
    }
    assert Risk(level="high", text="Liest außerhalb.").level is RiskLevel.HIGH


def test_decision_rules() -> None:
    deny = Decision(action="deny", decided_by="fixed_rule", reason="Nicht erlaubt.")
    assert deny.scope is DecisionScope.ONCE and not deny.revocable
    assert Decision(action="allow", decided_by="gatekeeper").revocable
    assert Decision(action="allow", decided_by="user", scope="run").revocable
    assert not Decision(action="allow", decided_by="user", scope="always").revocable
    with pytest.raises(ValueError, match="always"):
        Decision(action="allow", decided_by="gatekeeper", scope="always")
    with pytest.raises(ValueError, match="changes"):
        Decision(action="adjust", decided_by="user")
    adjust = Decision(
        action="adjust", decided_by="user", changes=[{"path": "judges", "from": 3, "to": 2}]
    )
    assert adjust.changes[0] == Change(path="judges", from_=3, to=2)
    assert adjust.model_dump(by_alias=True)["changes"][0]["from"] == 3
    with pytest.raises(ValueError, match="option"):
        Decision(action="option", decided_by="user")
    with pytest.raises(ValueError, match="nur der User"):
        Decision(action="option", option="retry", decided_by="gatekeeper")


def _request() -> PermissionRequest:
    return PermissionRequest(
        run_id="r",
        agent_id="explorer-2",
        tool="Bash",
        command="pip install python-flint",
        categories=["network", "install"],
        reason="Brauche flint für Zetafunktionen.",
    )


def test_approval_item_rules() -> None:
    item = ApprovalItem(id="apr_0001", type="permission", run_id="r", request=_request())
    assert item.status is ApprovalStatus.OPEN and not item.revocable

    decided = item.model_copy(
        update={
            "status": ApprovalStatus.DECIDED,
            "decision": Decision(action="allow", decided_by="gatekeeper"),
        }
    )
    assert ApprovalItem.model_validate(decided.model_dump()).revocable

    with pytest.raises(ValueError, match="request"):
        ApprovalItem(id="x", type="permission", run_id="r")
    with pytest.raises(ValueError, match="keine Frist"):
        ApprovalItem(id="x", type="run_start", run_id="r", deadline=1.0)
    with pytest.raises(ValueError, match="Option"):
        ApprovalItem(id="x", type="escalation", run_id="r")
    with pytest.raises(ValueError, match="braucht „decision“"):
        ApprovalItem(id="x", type="run_start", run_id="r", status="decided")
    with pytest.raises(ValueError, match="passt nicht"):
        ApprovalItem(
            id="x",
            type="permission",
            run_id="r",
            request=_request(),
            status="decided",
            decision=Decision(action="ask", decided_by="fixed_rule"),
        )
    escalation = ApprovalItem(
        id="x",
        type="escalation",
        run_id="r",
        stage="beweis",
        options=[EscalationOption.RETRY, EscalationOption.END],
        status="decided",
        decision=Decision(action="option", option="end", decided_by="user"),
    )
    assert escalation.decision is not None and escalation.decision.option is EscalationOption.END
    with pytest.raises(ValueError, match="nicht angeboten"):
        escalation.model_validate({**escalation.model_dump(), "options": [EscalationOption.RETRY]})


def test_turn_status_values() -> None:
    assert TurnStatus.OK == "ok"
    outcome = TurnOutcome(agent_id="a", turn=1, status="interrupted")
    assert outcome.usage == Usage() and outcome.results == []


def test_get_attachment_returns_path() -> None:
    hints = inspect.get_annotations(RoomAPI.get_attachment, eval_str=True)
    assert hints["return"] is Path

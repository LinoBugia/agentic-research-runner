"""Types of the permission layer: normalized requests, risk, decisions, approval inbox items.

Both adapters turn tool calls that leave the sandbox into a `PermissionRequest`; the
PolicyEngine (W1-05) answers with a `Decision`; requests in the grey zone become an
`ApprovalItem` in the inbox. See docs/07-berechtigungen-und-freigaben.md §3, §4, §5 and the
field list in docs/08-ux-konzept.md §5.0.
"""

import time
from enum import StrEnum
from typing import Any, Self

from pydantic import Field, model_validator

from arr.models import Provider, Record


class RequestCategory(StrEnum):
    """Why a request needs a decision (docs/07 §3 ②, docs/08 §5.0 `categories[]`)."""

    NETWORK = "network"
    INSTALL = "install"
    READ_OUTSIDE = "read_outside"
    WRITE_OUTSIDE = "write_outside"
    PRIVILEGED = "privileged"
    OTHER = "other"


class RiskLevel(StrEnum):
    """Risk class of a request (docs/07 §3 ③, Codeplan §8 W1-05)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionAction(StrEnum):
    """Outcome of a decision (docs/07 §3, §5; docs/08 §5.0).

    `ask` is only a PolicyEngine result (goes to the inbox), never a final decision.
    `adjust` is for run starts changed by the user, `option` for escalations.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    ADJUST = "adjust"
    OPTION = "option"


class DecisionScope(StrEnum):
    """Reach of a decision (docs/07 §3 ④); `always` becomes a fixed rule, set only by the user."""

    ONCE = "once"
    AGENT = "agent"
    RUN = "run"
    ALWAYS = "always"


class DecidedBy(StrEnum):
    """Who made a decision (docs/07 §4); UI words in docs/08 §8.3."""

    USER = "user"
    GATEKEEPER = "gatekeeper"
    MANDATE_RULE = "mandate_rule"
    FIXED_RULE = "fixed_rule"


class ApprovalType(StrEnum):
    """Kind of an inbox item (docs/07-berechtigungen-und-freigaben.md §6)."""

    RUN_START = "run_start"
    PERMISSION = "permission"
    ESCALATION = "escalation"


class ApprovalStatus(StrEnum):
    """State of an inbox item (docs/08-ux-konzept.md §5.0, §8.3).

    `expired`: timeout without decision (counts as deny, docs/07 §3 ③); `obsolete`: no longer
    needed, e.g. the run was stopped; `revoked`: a decision was withdrawn (docs/07 §4).
    """

    OPEN = "open"
    DECIDED = "decided"
    EXPIRED = "expired"
    REVOKED = "revoked"
    OBSOLETE = "obsolete"


class EscalationOption(StrEnum):
    """Choices of an escalation (docs/03-skills-und-research-groups.md §4.4).

    `retry`: one more attempt (raises `max_rework` by 1), `continue`: go on anyway,
    `end`: stop the run.
    """

    RETRY = "retry"
    CONTINUE = "continue"
    END = "end"


class PermissionRequest(Record):
    """Normalized request of an agent for an action outside the sandbox (docs/07 §3 ③, §7).

    Built by the adapters from the SDK callbacks; `reason` is the agent's own justification.
    Paths are as given by the agent; the PolicyEngine normalizes them. Never contains secrets.
    """

    run_id: str
    agent_id: str
    role: str | None = None
    provider: Provider | None = None
    tool: str = Field(description="Tool name as reported by the SDK, e.g. Bash, Read")
    tool_call_id: str | None = None
    command: str | None = None
    paths: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list, description="e.g. SandboxNetworkAccess host")
    work_dir: str | None = None
    reason: str | None = None
    categories: list[RequestCategory] = Field(default_factory=list)
    ts: float = Field(default_factory=time.time)


class Risk(Record):
    """Risk class with a one-sentence German justification (docs/07 §3 ③, docs/08 §5.0)."""

    level: RiskLevel
    text: str


class Change(Record):
    """One change made with „Anpassen“, e.g. `judges: 3 → 2` (docs/07 §5, docs/04 §2)."""

    path: str
    from_: Any = Field(default=None, alias="from")
    to: Any = None


class Decision(Record):
    """A decision on a request, run start or escalation (docs/07 §3 ④, §4, §5).

    `reason` is the German justification; for `deny` it is shown to the agent together with
    an alternative. `comment` is an optional note by the user. Logged to `approvals.jsonl`.
    """

    action: DecisionAction
    scope: DecisionScope = DecisionScope.ONCE
    decided_by: DecidedBy
    reason: str = ""
    comment: str | None = None
    changes: list[Change] = Field(default_factory=list)
    option: EscalationOption | None = None
    risk: Risk | None = None
    decided_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.scope is DecisionScope.ALWAYS and self.decided_by is not DecidedBy.USER:
            raise ValueError("Die Reichweite „always“ darf nur der User festlegen (07 §3 ④).")
        if (self.action is DecisionAction.ADJUST) != bool(self.changes):
            raise ValueError("„changes“ gehört genau zu „adjust“ und darf dort nicht leer sein.")
        if (self.action is DecisionAction.OPTION) != (self.option is not None):
            raise ValueError("„option“ gehört genau zu „action: option“.")
        user_only = (DecisionAction.ADJUST, DecisionAction.OPTION)
        if self.action in user_only and self.decided_by is not DecidedBy.USER:
            raise ValueError("Anpassen und Eskalationen entscheidet in v1 nur der User (07 §6).")
        return self

    @property
    def revocable(self) -> bool:
        """True for decisions the user can withdraw: gatekeeper, or user with scope agent/run."""
        if self.action not in (DecisionAction.ALLOW, DecisionAction.DENY):
            return False
        if self.decided_by is DecidedBy.GATEKEEPER:
            return True
        return self.decided_by is DecidedBy.USER and self.scope in (
            DecisionScope.AGENT,
            DecisionScope.RUN,
        )


class EscalatedBy(Record):
    """Who passed a permission request on to the user, and why (docs/08 §5.0)."""

    decider: DecidedBy
    note: str = ""

    @model_validator(mode="after")
    def _only_automatic_deciders(self) -> Self:
        if self.decider not in (DecidedBy.MANDATE_RULE, DecidedBy.GATEKEEPER):
            raise ValueError("Weitergeben können nur „mandate_rule“ und „gatekeeper“.")
        return self


_ALLOWED_ACTIONS = {
    ApprovalType.RUN_START: {DecisionAction.ALLOW, DecisionAction.DENY, DecisionAction.ADJUST},
    ApprovalType.PERMISSION: {DecisionAction.ALLOW, DecisionAction.DENY},
    ApprovalType.ESCALATION: {DecisionAction.OPTION},
}
_USER_ONLY_TYPES = {ApprovalType.RUN_START, ApprovalType.ESCALATION}


class ApprovalItem(Record):
    """An entry of the approval inbox: run start, permission request or escalation (docs/07 §6).

    Run starts have no deadline (docs/07 §5). Run starts and escalations are decided only by
    the user, with scope `once` (E15, docs/07 §6). The GUI builds its own view model with the same
    enums (docs/08 §5.0); the details of a run start come from the resolved run.
    """

    id: str = Field(description="e.g. apr_0042, used in deep links")
    type: ApprovalType
    status: ApprovalStatus = ApprovalStatus.OPEN
    created_at: float = Field(default_factory=time.time)
    run_id: str
    agent_id: str | None = None
    request: PermissionRequest | None = None
    risk: Risk | None = None
    deadline: float | None = None
    escalated_by: EscalatedBy | None = None
    stage: str | None = Field(default=None, description="Escalation: stage that failed")
    summary: str | None = None
    options: list[EscalationOption] = Field(default_factory=list)
    decision: Decision | None = None
    count: int = Field(default=1, ge=1, description="Equal requests answered by this item")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.type is ApprovalType.PERMISSION and self.request is None:
            raise ValueError("Eine Berechtigungsanfrage braucht „request“.")
        if self.type is ApprovalType.RUN_START and self.deadline is not None:
            raise ValueError("Run-Start-Freigaben haben keine Frist (07 §5).")
        if self.type is ApprovalType.ESCALATION and not self.options:
            raise ValueError("Eine Eskalation braucht mindestens eine Option.")
        if (self.status is ApprovalStatus.DECIDED) and self.decision is None:
            raise ValueError("Ein entschiedener Eintrag braucht „decision“.")
        if self.status is ApprovalStatus.OPEN and self.decision is not None:
            raise ValueError("Ein offener Eintrag hat noch keine „decision“.")
        if self.decision is not None:
            if self.type in _USER_ONLY_TYPES and (
                self.decision.decided_by is not DecidedBy.USER
                or self.decision.scope is not DecisionScope.ONCE
            ):
                raise ValueError(
                    "Run-Starts und Eskalationen entscheidet in v1 nur der User in der GUI, "
                    "jeweils einmalig (E15, 07 §6). Erlaubt ist nur „decided_by: user“ mit "
                    "„scope: once“."
                )
            if self.decision.action not in _ALLOWED_ACTIONS[self.type]:
                raise ValueError(
                    f"„{self.decision.action}“ passt nicht zu einem Eintrag vom Typ „{self.type}“."
                )
            if self.decision.option is not None and self.decision.option not in self.options:
                raise ValueError(f"Die Option „{self.decision.option}“ wurde nicht angeboten.")
        return self

    @property
    def revocable(self) -> bool:
        """True if the decision can be withdrawn in the GUI history (docs/08 §5.0, docs/07 §4)."""
        return (
            self.status is ApprovalStatus.DECIDED
            and self.decision is not None
            and self.decision.revocable
        )

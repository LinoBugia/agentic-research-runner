"""Pydantic models for configuration files and shared enums.

User files (`run.yaml`, `group.yaml`, `arr.yaml`, `project.yaml`, `profiles.yaml`) are parsed
into strict models: unknown fields are rejected with a German message. The resolved form of a
run (`ResolvedRun`, `AgentConfig`) is produced by `arr.resolve`.

See docs/01-architektur.md §3, docs/02-workfolder-und-runs.md §1.1, §2.2, §3, §7,
docs/03-skills-und-research-groups.md §3 and docs/06-gruppenchats.md §2.
"""

import difflib
import re
from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

# --------------------------------------------------------------------------------------------
# Base classes
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


class Record(BaseModel):
    """Base for runtime records produced by arr itself (room, events, approvals).

    Unknown fields are rejected to catch programming errors early. See docs/02 §5-§6.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------------
# Small value types
# --------------------------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_SENDERS = frozenset({"user", "orchestrator"})
"""Sender names reserved for arr itself in group chats (docs/02 §5); never agent IDs."""

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _check_slug(value: Any, what: str, example: str) -> Any:
    if isinstance(value, str) and not _SLUG_RE.fullmatch(value):
        raise ValueError(
            f"Ungültige {what} „{value}“. Erlaubt sind Kleinbuchstaben, Ziffern, „-“ und „_“ "
            f"(höchstens 64 Zeichen), z. B. „{example}“."
        )
    return value


def _check_agent_id(value: Any) -> Any:
    value = _check_slug(value, "Agent-ID", "judge")
    if value in RESERVED_SENDERS:
        raise ValueError(
            f"Die Agent-ID „{value}“ ist für arr reserviert. Wähle einen anderen Namen."
        )
    return value


AgentId = Annotated[str, BeforeValidator(_check_agent_id)]
"""Agent ID: lowercase slug, not `user` or `orchestrator` (docs/02 §4, §5)."""

Slug = Annotated[str, BeforeValidator(lambda v: _check_slug(v, "Bezeichnung", "main"))]
"""Name of a channel, profile, shared folder, …: lowercase slug (docs/06 §2)."""


def parse_duration(value: Any) -> Any:
    """Parse `2h`, `90m`, `1h30m`, `45s`, `1d` or a number of seconds into a `timedelta`."""
    if isinstance(value, timedelta) or value is None:
        return value
    if isinstance(value, bool):
        raise ValueError(_DURATION_HELP)
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(_DURATION_HELP)
        return timedelta(seconds=value)
    if isinstance(value, str):
        text = value.strip().replace(" ", "")
        match = _DURATION_RE.fullmatch(text)
        if text and match:
            d, h, m, s = (int(g) if g else 0 for g in match.groups())
            return timedelta(days=d, hours=h, minutes=m, seconds=s)
    raise ValueError(_DURATION_HELP)


_DURATION_HELP = (
    "Ungültige Dauer. Schreibe sie z. B. als „2h“, „90m“, „1h30m“ oder „45s“ "
    "oder als Zahl in Sekunden."
)


def format_duration(value: timedelta) -> str:
    """Format a `timedelta` compactly, e.g. `2h`, `1h30m`, `45s` (inverse of `parse_duration`)."""
    total = int(value.total_seconds())
    if total == 0:
        return "0s"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        amount, total = divmod(total, size)
        if amount:
            parts.append(f"{amount}{unit}")
    return "".join(parts)


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str, when_used="json"),
]
"""A duration such as `wall_time: 2h`; JSON/YAML output is a compact string (docs/02 §1.1)."""


def _check_read_dir(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("run:"):
        rest = value.removeprefix("run:")
        name = rest.removeprefix("shared/")
        if rest != "output" and not (rest.startswith("shared/") and _SLUG_RE.fullmatch(name)):
            raise ValueError(
                f"Unbekannter Run-Ordner „{value}“. Erlaubt sind „run:output“ und "
                "„run:shared/<name>“."
            )
    return value


ReadDir = Annotated[str, BeforeValidator(_check_read_dir)]
"""Read-only folder: a path, `run:output` or `run:shared/<name>` (docs/02 §3.1, E19)."""


# --------------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------------


class Provider(StrEnum):
    """Research backend of an agent (E2, docs/01-architektur.md §4)."""

    CLAUDE = "claude"
    CODEX = "codex"

    @property
    def vendor(self) -> str:
        """Company that receives the agent's context: `anthropic` or `openai` (E19, docs/08)."""
        return "anthropic" if self is Provider.CLAUDE else "openai"


class Effort(StrEnum):
    """Reasoning effort. Union of the values of both SDKs; adapters map or reject per model.

    Claude: low … max. Codex: none … ultra, depending on the model.
    See docs/spikes/S2_codex.md §2 and docs/mathe-agent-harness.md.
    """

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class WorkspaceMode(StrEnum):
    """How an agent's working folder is created (docs/02-workfolder-und-runs.md §3.1)."""

    COPY = "copy"
    SHARED = "shared"
    EMPTY = "empty"


class TopologyKind(StrEnum):
    """Built-in orchestration policies (E8, docs/03-skills-und-research-groups.md §4)."""

    SOLO = "solo"
    ROUNDTABLE = "roundtable"
    EXPLORATION = "exploration"
    WATERFALL = "waterfall"
    REVIEW = "review"


class ChannelMode(StrEnum):
    """Turn-taking in a group chat (docs/06-gruppenchats.md §5)."""

    ROUNDS = "rounds"
    EVENT = "event"
    MODERATED = "moderated"


class RoundOrder(StrEnum):
    """Order of turns within a round, only for `mode: rounds` (docs/06 §2)."""

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class WakeOn(StrEnum):
    """Events that wake an idle agent in `mode: event` (docs/06-gruppenchats.md §5.2)."""

    MENTION = "mention"
    QUESTION = "question"
    ANY = "any"


class RunStatus(StrEnum):
    """Lifecycle of a run (docs/02-workfolder-und-runs.md §7, UI words in docs/08 §8.3)."""

    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    FINALIZING = "finalizing"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def is_active(self) -> bool:
        """True for the statuses counted as „Aktiv“ (docs/08-ux-konzept.md §8.3)."""
        return self in ACTIVE_RUN_STATUSES

    @property
    def is_final(self) -> bool:
        """True once the run has ended: done, stopped, failed or interrupted (docs/02 §7)."""
        return self in FINAL_RUN_STATUSES


ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.AWAITING_APPROVAL,
        RunStatus.QUEUED,
        RunStatus.PREPARING,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.FINALIZING,
    }
)
FINAL_RUN_STATUSES = frozenset(
    {RunStatus.DONE, RunStatus.STOPPED, RunStatus.FAILED, RunStatus.INTERRUPTED}
)


class AgentStatus(StrEnum):
    """State of a research agent as stored in `room.sqlite agent_state` (docs/06 §5.2).

    The GUI derives further display states (`waiting_user`, `error`) itself (docs/08 §5.0).
    """

    WORKING = "working"
    IDLE = "idle"
    DONE = "done"
    BLOCKED = "blocked"


class Starter(StrEnum):
    """Who started a run, stored in `meta.json` (docs/02-workfolder-und-runs.md §4)."""

    GUI = "gui"
    CLI = "cli"
    USER_AGENT = "user_agent"


class Decider(StrEnum):
    """Who decides permission requests (docs/07-berechtigungen-und-freigaben.md §3 ④)."""

    USER = "user"
    GATEKEEPER = "gatekeeper"
    GATEKEEPER_THEN_USER = "gatekeeper_then_user"


class AuthKind(StrEnum):
    """Kind of an auth profile (docs/04-service-gui-und-schnittstellen.md §5)."""

    CLAUDE_OAUTH = "claude_oauth"
    CLAUDE_API_KEY = "claude_api_key"
    CODEX_CHATGPT = "codex_chatgpt"
    OPENAI_API_KEY = "openai_api_key"


class RunVenvMode(StrEnum):
    """Whether a run gets its own venv on top of the base venv (docs/07 §2)."""

    OVERLAY = "overlay"
    OFF = "off"


# --------------------------------------------------------------------------------------------
# Agent configuration (run.yaml / group.yaml)
# --------------------------------------------------------------------------------------------


class Workspace(StrictModel):
    """Working folder of an agent: `copy`, `shared` + `name`, or `empty` (docs/02 §3.1)."""

    mode: WorkspaceMode = WorkspaceMode.COPY
    name: Slug | None = Field(default=None, description="Name of the shared copy (shared only)")

    @model_validator(mode="after")
    def _name_only_for_shared(self) -> Self:
        if self.mode is WorkspaceMode.SHARED and not self.name:
            raise ValueError(
                "Für „mode: shared“ fehlt „name“. Gib der geteilten Kopie einen Namen, "
                "z. B. „name: beweis“."
            )
        if self.mode is not WorkspaceMode.SHARED and self.name:
            raise ValueError("„name“ gibt es nur bei „mode: shared“. Entferne „name“.")
        return self


class Tools(StrictModel):
    """Optional tools of an agent: terminal and web access (docs/07 §2, docs/01 §8)."""

    shell: bool = True
    web: bool = False


class Limits(StrictModel):
    """Run limits, given field by field (docs/02 §1.1, docs/01 §6).

    Resolution merges field by field: `BUILTIN_LIMITS` < `arr.yaml` < `group.yaml` <
    `run.yaml`. A field that is not given keeps the value of the layer below; an explicit
    `null` (e.g. `budget_usd: null`) switches that limit off.
    """

    wall_time: Duration | None = None
    turns_per_agent: int | None = Field(default=None, ge=1)
    budget_usd: float | None = Field(default=None, ge=0)


BUILTIN_LIMITS = Limits(wall_time=timedelta(hours=2), turns_per_agent=40, budget_usd=20)
"""Lowest limits layer: the standard limits from docs/02 §1.1 (2h, 40 turns, 20 $)."""


class AgentSettings(StrictModel):
    """Fields an agent definition, `defaults` and a `mix` entry have in common (docs/02 §3).

    All fields are optional here; `arr.resolve` fills them from the layers
    built-in < `arr.yaml` < `group.yaml defaults` < `run.yaml defaults` < agent < `mix` entry.
    """

    provider: Provider | None = None
    model: str | None = None
    auth: Slug | None = Field(default=None, description="Name of the auth profile")
    effort: Effort | None = None
    role: str | None = Field(default=None, description="Role file, relative to skill or project")
    skills: list[str] | None = None
    workspace: Workspace | None = None
    tools: Tools | None = None
    read_dirs: list[ReadDir] | None = None


class AgentDefaults(AgentSettings):
    """`defaults:` section of `run.yaml` / `group.yaml`, applies to all agents (docs/02 §3)."""


class MixEntry(AgentSettings):
    """One entry of `mix:`; replicas take the entries round-robin (docs/03 §3)."""


class WhenCondition(StrictModel):
    """`when: {param, equals | in}`: include an agent only if a parameter matches (docs/03 §3)."""

    param: str
    equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _exactly_one_test(self) -> Self:
        has_equals = "equals" in self.model_fields_set
        has_in = "in_" in self.model_fields_set
        if has_equals == has_in:
            raise ValueError(
                "„when“ braucht genau eine Bedingung: „equals: <wert>“ oder „in: [<werte>]“."
            )
        return self

    def matches(self, value: Any) -> bool:
        """Return whether the parameter value satisfies the condition."""
        if "equals" in self.model_fields_set:
            return value == self.equals
        return value in (self.in_ or [])


PLACEHOLDER_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")
"""Matches `{{ … }}`; only `{{ params.<name> }}` is valid (docs/03 §3)."""


MAX_COUNT = 50
"""Upper limit for `count`, independent of `params.max` (protects against typos)."""


def _check_count(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, bool):
        raise ValueError("„count“ muss eine ganze Zahl ≥ 0 oder „{{ params.<name> }}“ sein.")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("„count“ muss eine ganze Zahl ≥ 0 sein.")
        if value > MAX_COUNT:
            raise ValueError(
                f"„count“ darf höchstens {MAX_COUNT} sein, gefunden {value}. "
                "Verringere die Zahl oder teile die Gruppe auf mehrere Runs auf."
            )
        return value
    if isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value.strip()):
        return value.strip()
    raise ValueError("„count“ muss eine ganze Zahl ≥ 0 oder „{{ params.<name> }}“ sein.")


class AgentSpec(AgentSettings):
    """One entry of `agents:` in `run.yaml` / `group.yaml`, before resolution (docs/02 §3).

    `count: n` expands to `<id>-1 … <id>-n` (also for `n = 1`), `count: 0` drops the agent.
    `mix` needs `count`. `when` includes the agent only if the condition holds (docs/03 §3).
    String fields and `count` may contain `{{ params.<name> }}`.
    """

    id: AgentId
    count: Annotated[int | str | None, BeforeValidator(_check_count)] = None
    mix: list[MixEntry] | None = Field(default=None, min_length=1)
    when: WhenCondition | None = None

    @model_validator(mode="after")
    def _mix_needs_count(self) -> Self:
        if self.mix is not None and self.count is None:
            raise ValueError(
                f"„mix“ bei „{self.id}“ braucht „count“. Setze z. B. „count: 2“, damit die "
                "Replikate abwechselnd verteilt werden können."
            )
        return self


class AgentConfig(StrictModel):
    """Fully resolved configuration of one agent, stored as `agents/<id>/agent.json` (docs/01 §3).

    Contains only the auth profile's name, never a secret (docs/01 §8).
    """

    id: AgentId
    base_id: AgentId = Field(description="ID in the config before `count` expansion")
    replica: int | None = Field(default=None, ge=1, description="1-based index from `count`")
    provider: Provider
    model: str
    auth: Slug
    effort: Effort | None = None
    role: str | None = None
    skills: list[str] = Field(default_factory=list)
    workspace: Workspace = Field(default_factory=Workspace)
    tools: Tools = Field(default_factory=Tools)
    read_dirs: list[ReadDir] = Field(default_factory=list)
    channels: list[Slug] = Field(default_factory=list, description="Channels the agent is in")


# --------------------------------------------------------------------------------------------
# Topology, group chats, group specs
# --------------------------------------------------------------------------------------------


class TopologyConfig(StrictModel):
    """`topology:` of a run: kind plus parameters (docs/02 §3, docs/03 §4).

    Accepts the short form `topology: review` as well. In a resolved run, `params` holds the
    final parameter values (declared defaults + overrides).
    """

    kind: TopologyKind
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _short_form(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"kind": data}
        return data


class GroupChatConfig(StrictModel):
    """A group chat channel with members and rules (docs/06-gruppenchats.md §2).

    In the input, `members` may contain patterns (`explorer-*`); after resolution it holds
    concrete agent IDs in a stable order.
    """

    name: Slug
    purpose: str | None = None
    members: list[str] = Field(min_length=1)
    mode: ChannelMode = ChannelMode.EVENT
    order: RoundOrder = RoundOrder.PARALLEL
    moderator: str | None = None
    open_from: str | None = Field(default=None, description="Stage from which it is open")
    anonymous: bool = False
    wake_on: list[WakeOn] = Field(default_factory=lambda: [WakeOn.MENTION, WakeOn.QUESTION])
    debounce_s: float = Field(default=20, ge=0)
    max_wakeups_per_agent: int = Field(default=12, ge=0)
    quiet_after_s: float = Field(default=300, gt=0)
    max_message_chars: int = Field(default=3000, ge=1)
    summary_every: int = Field(default=15, ge=0, description="0 turns the summary off")

    @model_validator(mode="after")
    def _moderator_only_when_moderated(self) -> Self:
        if self.mode is ChannelMode.MODERATED and not self.moderator:
            raise ValueError(
                f"Kanal „{self.name}“ ist moderiert, aber „moderator“ fehlt. "
                "Trage die Agent-ID des Moderators ein."
            )
        if self.mode is not ChannelMode.MODERATED and self.moderator:
            raise ValueError(
                f"„moderator“ gilt nur bei „mode: moderated“ (Kanal „{self.name}“). "
                "Entferne „moderator“ oder setze den Modus."
            )
        return self


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _type_label(value: Any) -> str:
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, int):
        return "ganze Zahl"
    if isinstance(value, float):
        return "Zahl"
    if isinstance(value, str):
        return "Text"
    if isinstance(value, list):
        return "Liste"
    if isinstance(value, dict):
        return "Abschnitt"
    return type(value).__name__


def _same_kind(default: Any, value: Any) -> bool:
    # Numbers are one kind: `pass_score: {default: 7}` accepts 7.5. Where a whole number is
    # required (`count`), the field itself checks it. Booleans are never numbers.
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if _is_number(default):
        return _is_number(value)
    for kind in (str, list, dict):
        if isinstance(default, kind):
            return isinstance(value, kind)
    return True


class ParamSpec(StrictModel):
    """A declared parameter in `group.yaml` with default and limits (docs/03 §3)."""

    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: list[Any] | None = Field(default=None, min_length=1)
    doc: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"„min“ ({self.min:g}) ist größer als „max“ ({self.max:g}).")
        if self.default is not None:
            problem = self.problem(self.default)
            if problem:
                raise ValueError(f"Der Standardwert passt nicht zur Deklaration: {problem}")
        return self

    def problem(self, value: Any) -> str | None:
        """Return a German description of why `value` is invalid, or `None` if it is valid.

        Checks type (against `default` and `min`/`max`), range and `choices`.
        """
        if value is None:
            return None if self.default is None else "Es fehlt ein Wert."
        if self.default is not None and not _same_kind(self.default, value):
            return (
                f"Falscher Typ: erwartet {_type_label(self.default)}, "
                f"gefunden {_type_label(value)} ({value!r})."
            )
        if self.min is not None or self.max is not None:
            if not _is_number(value):
                return f"Falscher Typ: erwartet eine Zahl, gefunden {_type_label(value)}."
            if self.min is not None and value < self.min:
                return f"Der Wert {value!r} ist kleiner als das Minimum {self.min:g}."
            if self.max is not None and value > self.max:
                return f"Der Wert {value!r} ist größer als das Maximum {self.max:g}."
        if self.choices is not None and value not in self.choices:
            allowed = ", ".join(str(c) for c in self.choices)
            return f"„{value}“ ist nicht erlaubt. Erlaubt sind: {allowed}."
        return None


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


class StageSpec(StrictModel):
    """One stage of the `waterfall` topology (docs/03 §4.4)."""

    name: Slug
    agent: str = Field(description="Agent ID (after count expansion) that works on the stage")
    produces: str | None = Field(default=None, description="Expected file, e.g. proof.md")


class GroupSpec(StrictModel):
    """Content of a group skill's `group.yaml`: a saved research group setup (docs/03 §3).

    Same structure as `run.yaml` without `project` and `task`, plus declared `params`.
    `topology` accepts `review` and `{kind: review}`; parameters belong under `params:`.
    """

    kind: Literal["research-group"] = "research-group"
    version: Literal[1] = 1
    description: str | None = None
    topology: TopologyKind
    params: dict[str, ParamSpec] = Field(default_factory=dict)

    @field_validator("topology", mode="before")
    @classmethod
    def _long_form(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "params" in value:
            raise ValueError(
                "In group.yaml stehen Parameter unter „params:“ (mit default, min, max …), "
                "nicht unter „topology.params“. Verschiebe sie dorthin."
            )
        unknown = sorted(str(k) for k in value if k != "kind")
        if unknown or "kind" not in value:
            raise ValueError(
                "„topology“ ist entweder ein Name wie „review“ oder „{kind: review}“"
                + (f", unbekannt: {', '.join(unknown)}." if unknown else ".")
            )
        return value["kind"]

    rubric: Rubric | None = None
    limits: Limits | None = None
    defaults: AgentDefaults | None = None
    agents: list[AgentSpec] = Field(min_length=1)
    groupchats: list[GroupChatConfig] = Field(default_factory=list)
    stages: list[StageSpec] = Field(default_factory=list)
    raters: list[str] = Field(default_factory=list, description="Agent IDs or patterns")


# --------------------------------------------------------------------------------------------
# Approvals settings, run input, resolved run
# --------------------------------------------------------------------------------------------


class GatekeeperSettings(StrictModel):
    """Supervising agent that decides within the user's mandate (docs/07 §3 ④, §4)."""

    model: str
    mandate: str = Field(description="Mandate file, relative to the workfolder")


class ApprovalSettings(StrictModel):
    """`approvals:` in `arr.yaml` or `run.yaml`: who decides, timeout (docs/07 §3 ④)."""

    decider: Decider = Decider.USER
    timeout: Duration = timedelta(minutes=20)
    gatekeeper: GatekeeperSettings | None = None

    @model_validator(mode="after")
    def _gatekeeper_present(self) -> Self:
        if self.decider is not Decider.USER and self.gatekeeper is None:
            raise ValueError(
                f"Für „decider: {self.decider}“ fehlt der Abschnitt „gatekeeper“ "
                "(Modell und Vollmacht). Ergänze ihn oder setze „decider: user“."
            )
        return self


class ApprovalOverride(StrictModel):
    """`approvals:` in `run.yaml`: a partial layer over `arr.yaml approvals` (docs/07 §3 ④).

    Checked only after merging, so `decider: gatekeeper` is valid when `arr.yaml` provides
    the `gatekeeper` section.
    """

    decider: Decider | None = None
    timeout: Duration | None = None
    gatekeeper: GatekeeperSettings | None = None


class RunInput(StrictModel):
    """Content of a `run.yaml` as passed in, stored as `run.input.yaml` (docs/02 §3).

    Exactly one of `task` and `task_file` is required. `topology` may be omitted when the run
    is based on a group skill; `topology.params` then override the skill's parameters.
    Non-empty `agents`, `groupchats`, `stages` and `raters` and a given `rubric` replace those
    of the skill.
    """

    version: Literal[1] = 1
    name: str | None = None
    project: str
    skill: str | None = None
    task: str | None = None
    task_file: str | None = None
    topology: TopologyConfig | None = None
    rubric: Rubric | None = None
    limits: Limits | None = None
    approvals: ApprovalOverride | None = None
    defaults: AgentDefaults | None = None
    agents: list[AgentSpec] = Field(default_factory=list)
    groupchats: list[GroupChatConfig] = Field(default_factory=list)
    stages: list[StageSpec] = Field(default_factory=list)
    raters: list[str] = Field(default_factory=list, description="Agent IDs or patterns")

    @model_validator(mode="after")
    def _one_task(self) -> Self:
        if (self.task is None) == (self.task_file is None):
            raise ValueError(
                "Gib die Aufgabe genau einmal an: entweder „task_file: task.md“ oder "
                "„task: |“ mit dem Text."
            )
        return self


class ResolvedRun(StrictModel):
    """Fully resolved run configuration, stored as `run.yaml` in the run folder (docs/02 §3.3).

    Produced by `arr.resolve.resolve_run`. Agents and channel members are concrete IDs,
    parameters are final, limits and approvals are merged with `arr.yaml`.
    """

    version: Literal[1] = 1
    name: str | None = None
    project: str
    skill: str | None = None
    task: str | None = None
    task_file: str | None = None
    topology: TopologyConfig
    rubric: Rubric | None = None
    stages: list[StageSpec] = Field(default_factory=list)
    raters: list[AgentId] = Field(default_factory=list)
    limits: Limits
    approvals: ApprovalSettings
    agents: list[AgentConfig]
    groupchats: list[GroupChatConfig] = Field(default_factory=list)

    @property
    def params(self) -> dict[str, Any]:
        """Final parameter values (same as `topology.params`), see docs/01 §6."""
        return self.topology.params

    def agent(self, agent_id: str) -> AgentConfig:
        """Return the agent with this ID; `KeyError` if there is none."""
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        raise KeyError(agent_id)

    def channel(self, name: str) -> GroupChatConfig:
        """Return the channel with this name; `KeyError` if there is none."""
        for channel in self.groupchats:
            if channel.name == name:
                return channel
        raise KeyError(name)


# --------------------------------------------------------------------------------------------
# Workfolder, project and app settings
# --------------------------------------------------------------------------------------------


class ProviderDefaults(StrictModel):
    """Per-provider defaults in `arr.yaml` (`defaults.claude`, `defaults.codex`, docs/02 §1.1)."""

    auth: Slug | None = None
    model: str | None = None
    effort: Effort | None = None


class UserAgentRunSettings(StrictModel):
    """`user_agent_runs:` in `arr.yaml`; approval is fixed (E15) and cannot be switched off.

    See docs/07-berechtigungen-und-freigaben.md §5.
    """

    require_approval: Literal[True] = True

    @field_validator("require_approval", mode="before")
    @classmethod
    def _always_true(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError(
                "Runs des User-Agents brauchen immer deine Freigabe (E15). „require_approval“ "
                "lässt sich nicht abschalten, entferne die Zeile oder setze true."
            )
        return value


class Toolchain(StrictModel):
    """Python environment and PATH of the research agents (docs/07 §2)."""

    python: str | None = Field(default=None, description="Base venv, e.g. ~/.venvs/mathe")
    extra_path: list[str] = Field(default_factory=list)
    run_venv: RunVenvMode = RunVenvMode.OVERLAY


class RetentionSettings(StrictModel):
    """Automatic slimming/archiving rules; arr never deletes automatically (docs/02 §9)."""

    slim_after_days: int | None = Field(default=14, ge=1)
    archive_after_days: int | None = Field(default=90, ge=1)
    max_runs_size_gb: float | None = Field(default=50, gt=0)


class WorkfolderSettings(StrictModel):
    """Content of `<workfolder>/arr.yaml` (docs/02-workfolder-und-runs.md §1.1). No secrets.

    `limits` is a partial layer above `BUILTIN_LIMITS`. `permissions` holds deviations from
    the fixed rules; its schema belongs to the PolicyEngine (docs/07 §3 ②) and is kept as
    plain data here.
    """

    version: Literal[1] = 1
    defaults: dict[Provider, ProviderDefaults] = Field(default_factory=dict)
    concurrency: dict[Slug, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    user_agent_runs: UserAgentRunSettings = Field(default_factory=UserAgentRunSettings)
    toolchain: Toolchain = Field(default_factory=Toolchain)
    approvals: ApprovalSettings = Field(default_factory=ApprovalSettings)
    permissions: dict[str, Any] = Field(default_factory=dict)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)


class ProjectSettings(StrictModel):
    """Content of the optional `projects/<p>/project.yaml` (docs/02 §2.2)."""

    skills: Literal["all"] | list[str] = "all"
    copy_exclude: list[str] = Field(default_factory=list)
    read_dirs: list[ReadDir] = Field(default_factory=list)

    @field_validator("skills", mode="before")
    @classmethod
    def _all_or_list(cls, value: Any) -> Any:
        if value == "all" or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            return value
        raise ValueError("„skills“ ist entweder „all“ oder eine Liste von Skill-Namen.")


class KeychainRef(StrictModel):
    """Reference to a macOS keychain item (`keyring` service + account), never the secret.

    See docs/01-architektur.md §8.
    """

    service: str = "arr"
    account: str


class AuthProfile(StrictModel):
    """An auth profile from `~/.arr/profiles.yaml` (docs/04 §5, docs/01 §3).

    Holds only the name, the kind and where the secret lives: a keychain item for tokens and
    API keys, a login folder for `codex_chatgpt`. There is deliberately no field for a secret.
    Without `keychain`, `arr.auth` uses service `arr` and the profile name as account.
    """

    name: Slug
    kind: AuthKind
    keychain: KeychainRef | None = None
    login_dir: str | None = Field(default=None, description="Codex login folder (CODEX_HOME)")

    @model_validator(mode="after")
    def _storage_matches_kind(self) -> Self:
        if self.kind is AuthKind.CODEX_CHATGPT and self.keychain is not None:
            raise ValueError(
                "Ein ChatGPT-Profil speichert seinen Login im Login-Ordner, nicht im "
                "Schlüsselbund. Entferne „keychain“."
            )
        if self.kind is not AuthKind.CODEX_CHATGPT and self.login_dir is not None:
            raise ValueError(
                "„login_dir“ gibt es nur bei Profilen der Art „codex_chatgpt“. Entferne die Zeile."
            )
        return self


class ProfilesFile(StrictModel):
    """Content of `~/.arr/profiles.yaml`: the list of auth profiles (docs/02 §1)."""

    version: Literal[1] = 1
    profiles: list[AuthProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        seen: set[str] = set()
        for profile in self.profiles:
            if profile.name in seen:
                raise ValueError(f"Das Profil „{profile.name}“ ist doppelt. Benenne eines um.")
            seen.add(profile.name)
        return self

"""Pydantic models for configuration files and shared enums.

User files (`run.yaml`, `group.yaml`, `arr.yaml`, `project.yaml`, `profiles.yaml`) are parsed
into strict models: unknown fields are rejected with a German message. The resolved form of a
run (`ResolvedRun`, `AgentConfig`) is produced by `arr.resolve`.

Since W4b-02a the phase toolkit (`topology: phases`, docs/10 §4.10) is part of the same tree:
`PhaseSpec` and its parts in the input (`group.yaml`, `run.yaml`, `config` of Claude Code),
`ResolvedPhase` and its parts in the resolved `run.yaml` of a run folder. Checks, JSON schema,
estimate and reading helpers live in `arr.plan`. The base types `StrictModel`, `Slug` and
`Rubric` come from `arr.submissions.formats` (see there) and are re-exported here.

See docs/01-architektur.md §3, docs/02-workfolder-und-runs.md §1.1, §2.2, §3, §7,
docs/03-skills-und-research-groups.md §3, docs/06-gruppenchats.md §2 and docs/10 §4.
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
    GetCoreSchemaHandler,
    GetJsonSchemaHandler,
    ModelWrapValidatorHandler,
    PlainSerializer,
    PrivateAttr,
    field_validator,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import PydanticCustomError, core_schema

from arr.submissions.formats import (
    _SLUG_RE,
    MAX_CHOICE_CHARS,
    MAX_CHOICES,
    FormatName,
    FormatSpec,
    Rubric,
    Slug,
    StrictModel,
    _check_slug,
)

# --------------------------------------------------------------------------------------------
# Base classes
# --------------------------------------------------------------------------------------------


class Record(BaseModel):
    """Base for runtime records produced by arr itself (room, events, approvals).

    Unknown fields are rejected to catch programming errors early. See docs/02 §5-§6.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------------
# Small value types
# --------------------------------------------------------------------------------------------

RESERVED_SENDERS = frozenset({"user", "orchestrator"})
"""Sender names reserved for arr itself in group chats (docs/02 §5); never agent IDs."""

_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def _check_agent_id(value: Any) -> Any:
    value = _check_slug(value, "Agent-ID", "judge")
    if value in RESERVED_SENDERS:
        raise ValueError(
            f"Die Agent-ID „{value}“ ist für arr reserviert. Wähle einen anderen Namen."
        )
    return value


AgentId = Annotated[str, BeforeValidator(_check_agent_id)]
"""Agent ID: lowercase slug, not `user` or `orchestrator` (docs/02 §4, §5)."""


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
    PHASES = "phases"
    """The phase toolkit (docs/10 §4.10); not startable until its runner exists (W4b-02b)."""


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
    WAITING = "waiting"  # turn ended with `wait_until`; arr wakes it (docs/09 §4.3)


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


DEFAULT_COMPACT = "60%"
"""Built-in threshold of auto-compact (P12 in docs/10 §8): 60 % of the context window."""
MAX_COMPACT_TOKENS = 10_000_000
"""Largest threshold given as tokens; context windows are far smaller (typo guard)."""
_PERCENT_RE = re.compile(r"^\s*(\d{1,3})\s*%\s*$")
_COMPACT_HELP = (
    "„at“ ist ein Anteil des Kontextfensters wie „60%“ (1 bis 100) oder eine Zahl von Token "
    f"(1 bis {MAX_COMPACT_TOKENS:,})."
).replace(",", ".")


def _check_threshold(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(_COMPACT_HELP)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        if not 1 <= value <= MAX_COMPACT_TOKENS:
            raise ValueError(_COMPACT_HELP)
        return value
    if isinstance(value, str):
        match = _PERCENT_RE.fullmatch(value)
        if match and 1 <= int(match.group(1)) <= 100:
            return f"{int(match.group(1))}%"
    raise ValueError(_COMPACT_HELP)


class Compact(StrictModel):
    """`compact: { at: 60% }`: when arr compacts an agent's session between two turns
    (docs/10 §4.6, P12). `at` is a share of the context window (`"60%"`) or a number of
    tokens. There is no own switch (Entscheidung 41); `at: 100%` turns arr's compaction off,
    then only the engine compacts (Entscheidung 49)."""

    at: Annotated[str | int, BeforeValidator(_check_threshold)] = DEFAULT_COMPACT

    @property
    def share(self) -> float | None:
        """The threshold as a share of the window (0.6 for `60%`), None if given in tokens."""
        return int(self.at[:-1]) / 100 if isinstance(self.at, str) else None

    @property
    def tokens(self) -> int | None:
        """The threshold in tokens, None if given as a share."""
        return self.at if isinstance(self.at, int) else None


UserDecider = Literal["user"]
"""`decider: user` at an agent or phase: only tightens the decider (P14, docs/10 §4.11)."""


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
    compact: Compact | None = None
    decider: UserDecider | None = None


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


def is_placeholder(value: Any) -> bool:
    """True if `value` is a text that is exactly one `{{ … }}` (spaces around allowed)."""
    return isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value.strip()) is not None


def has_placeholder(value: Any, budget: int = 10_000) -> bool:
    """True if a whole value somewhere in `value` (sections and lists) is a placeholder.

    Looks at no more than `budget` values; the loader already bounds files (W4-02.1)."""
    stack = [value]
    while stack and budget > 0:
        budget -= 1
        item = stack.pop()
        if is_placeholder(item):
            return True
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


PLACEHOLDER_PATTERN = r"^\s*\{\{\s*params\.[A-Za-z_][A-Za-z0-9_-]*\s*\}\}\s*$"
"""JSON schema pattern of a whole-value placeholder (W4b-02a)."""


class _OrParam:
    """Annotation: in the input, the value may also be a whole `{{ params.<name> }}`.

    Phases, formats and seed need placeholders in numbers, choices and names (`rounds`,
    `max_visits`, `rule`, …; W4b-02a §6.2). The placeholder is kept as text until
    `arr.resolve` inserts the value and checks it with the same model. The JSON schema says
    `anyOf: [<type>, placeholder]`.
    """

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        inner = handler(source)

        def keep(value: Any, validate: core_schema.ValidatorFunctionWrapHandler) -> Any:
            return value.strip() if is_placeholder(value) else validate(value)

        def dump(value: Any, nxt: core_schema.SerializerFunctionWrapHandler) -> Any:
            return value if is_placeholder(value) else nxt(value)

        return core_schema.no_info_wrap_validator_function(
            keep,
            inner,
            serialization=core_schema.wrap_serializer_function_ser_schema(dump, schema=inner),
        )

    def __get_pydantic_json_schema__(
        self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"anyOf": [handler(schema), {"type": "string", "pattern": PLACEHOLDER_PATTERN}]}


OR_PARAM = _OrParam()
"""Marker for `Annotated[…, OR_PARAM]`: the value may be a whole placeholder in the input."""


def _no_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(
            f"Falscher Typ: erwartet eine Zahl, gefunden {'true' if value else 'false'}. "
            "Wahrheitswerte sind keine Zahlen."
        )
    return value


def _only_bool(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if not isinstance(value, bool):
        shown = value if isinstance(value, int | float) else type(value).__name__
        if isinstance(value, str):
            shown = f"„{value[:20]}“"
        raise ValueError(f"Falscher Typ: erwartet true oder false, gefunden {shown}.")
    return value


NO_BOOL = BeforeValidator(_no_bool)
"""Numbers of phases take no `true`/`false` (Entscheidung 44, like submissions); numbers as
text (`"2"`) stay allowed, because parameters may arrive so."""
ONLY_BOOL = BeforeValidator(_only_bool)
"""Switches of phases take `true`/`false` (also as text), no `1` or `"yes"`."""


class _RawIfTemplated:
    """Annotation for a section that may hold placeholders anywhere (an own format): with a
    placeholder inside, the section stays plain data until `arr.resolve` inserts the values
    and validates it; without one, it is validated right away."""

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        inner = handler(source)

        def keep(value: Any, validate: core_schema.ValidatorFunctionWrapHandler) -> Any:
            if isinstance(value, dict) and has_placeholder(value):
                return value
            return validate(value)

        def dump(value: Any, nxt: core_schema.SerializerFunctionWrapHandler) -> Any:
            return value if isinstance(value, dict) else nxt(value)

        return core_schema.no_info_wrap_validator_function(
            keep,
            inner,
            serialization=core_schema.wrap_serializer_function_ser_schema(dump, schema=inner),
        )


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
    """Fully resolved configuration of one agent (docs/01 §3).

    Stored as `config` in `agents/<id>/agent.json` (`AgentFile` in `arr.runs.prepare`).

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
    compact: Compact = Field(default_factory=Compact, description="Auto-compact threshold")
    decider: UserDecider | None = Field(
        default=None, description="`user`: the user decides every request of this agent"
    )


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
    idle_after_s: float = Field(
        default=30, gt=0, description="event: idle time before a round (docs/09 §4.4)"
    )
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


class StageSpec(StrictModel):
    """One stage of the `waterfall` topology (docs/03 §4.4)."""

    name: Slug
    agent: str = Field(description="Agent ID (after count expansion) that works on the stage")
    produces: str | None = Field(default=None, description="Expected file, e.g. proof.md")


# --------------------------------------------------------------------------------------------
# Phase toolkit: `topology: phases` (docs/10 §4.10, W4b-02a)
# --------------------------------------------------------------------------------------------

MAX_PHASES = 30
"""Phases of one group: a long research plan has about ten; more is a typo or a generator."""
MAX_ACTIONS = MAX_COUNT
"""Entries in `actions` of one phase: at most one per agent of the largest `count`."""
MAX_NAMES = 2 * MAX_COUNT
"""Entries in a list of agents or channels of one phase (`standby`, `raters`, `channels`)."""
MAX_INPUTS = 20
"""Entries in `inputs` of one phase."""
MAX_ROUTES = MAX_CHOICES
"""Routes of one gate: one per value of the field, and a choice has at most this many."""
MAX_FORMATS = 20
"""Own formats of one group."""
MAX_SEEDS = 20
"""Entries in `seed` of one group."""
MAX_TURNS = 20
"""`turns` of one action: continuations without a submission, before arr asks once."""
MAX_ROUNDS = 20
"""`rounds` of a phase `chat`."""
MAX_REPEAT = 10
"""`repeat` of a phase: lockstep passes of every action (as `turns_per_idea` today)."""
MAX_VISITS = 10
"""`max_visits` of a phase: passes in a loop."""
MAX_ASSIGNMENTS = 50
"""`max_assignments` of a phase `moderated`."""
MAX_REF_CHARS = 200
"""Length of an agent entry, a route word or a path in a phase (names are slugs of 64)."""
DEFAULT_TURNS = 3
"""`turns` of an action without one: arr continues a turn without a submission up to three
times, as `solo` does today, then asks once (Entscheidung 46)."""
DEFAULT_CHAT_ROUNDS = 3
"""`rounds` of a phase `chat` without one: the default of a discussion today."""


class PhaseTopology(StrEnum):
    """How the participants of a phase work (docs/10 §4.5)."""

    SOLO = "solo"
    PARALLEL = "parallel"
    CHAT = "chat"
    MODERATED = "moderated"


class ContextMode(StrEnum):
    """What happens to an agent's session at the start of an action (docs/10 §4.6)."""

    KEEP = "keep"
    RESET = "reset"
    COMPACT = "compact"


class FolderMode(StrEnum):
    """Whether an agent keeps its working folder at the start of an action (docs/10 §4.12)."""

    KEEP = "keep"
    FRESH = "fresh"


class GateType(StrEnum):
    """Who decides when a phase ends and where the run goes (docs/10 §4.7)."""

    DONE = "done"
    SIGNAL = "signal"
    JUDGE = "judge"
    USER = "user"


class GateRule(StrEnum):
    """How a gate counts several submissions (docs/10 §4.7, W4b-02a §6.5).

    `all`, `any`: only for `status` at a gate `signal`; `majority`, `unanimous`: any field with
    fixed values (gate `signal`) or verdicts of reviews (gate `judge`); `mean`, `rank`: ratings
    of a gate `judge`."""

    ALL = "all"
    ANY = "any"
    MAJORITY = "majority"
    UNANIMOUS = "unanimous"
    MEAN = "mean"
    RANK = "rank"


class RouteKeyword(StrEnum):
    """A route without a target phase (docs/10 §4.7)."""

    NEXT = "next"
    RETRY = "retry"
    STOP = "stop"
    ESCALATE = "escalate"


class Pick(StrEnum):
    """Which submissions of a source an input takes (docs/10 §4.10 `inputs`).

    `latest`: the newest per agent and action (`RoomStore.submissions(latest=True)`); `all`:
    every version; `best`: the best rated (gate `judge` with `mean` or `rank`)."""

    BEST = "best"
    LATEST = "latest"
    ALL = "all"


class InputContent(StrEnum):
    """How much of a submission an input shows: summary and path, or the text (docs/10 §4.10)."""

    SUMMARY = "summary"
    FULL = "full"


Ref = Annotated[str, Field(min_length=1, max_length=MAX_REF_CHARS)]
"""An agent ID or pattern, a phase, channel or format name, a path: checked by name later."""
NameList = Annotated[list[Ref], Field(max_length=MAX_NAMES), OR_PARAM]
"""A list of agents (IDs, patterns) or channels; also one whole placeholder."""


class Goto(StrictModel):
    """`{ goto: <phase> }`: a route to a named phase, forward or back (docs/10 §4.7)."""

    goto: Annotated[Slug, OR_PARAM]


ROUTE_HELP = "Eine Route ist next, retry, stop, escalate oder „{ goto: <phase> }“."


def _route_input(value: Any) -> Any:
    """A route as written: a word (checked against the keywords by `arr.resolve`, G1) or
    `{ goto: <phase> }`."""
    if isinstance(value, Goto):
        return value
    if isinstance(value, str):
        if not value.strip() or len(value) > MAX_REF_CHARS:
            raise ValueError(ROUTE_HELP)
        return value.strip()
    if isinstance(value, dict) and set(value) == {"goto"}:
        target = value["goto"]
        if isinstance(target, str) and (is_placeholder(target) or _SLUG_RE.fullmatch(target)):
            return Goto(goto=target)
        raise ValueError(
            "„goto“ nennt den Namen einer Phase (Kleinbuchstaben, Ziffern, „-“, „_“). " + ROUTE_HELP
        )
    raise ValueError(ROUTE_HELP)


RouteSpec = Annotated[str | Goto, BeforeValidator(_route_input)]
"""A route in the input: a word or `{ goto: <phase> }`; `arr.resolve` checks the word."""

Route = RouteKeyword | Goto
"""A route in the resolved run: a keyword or `{ goto: <existing phase> }`."""


class SubmissionSpec(StrictModel):
    """`submission` of an action (docs/10 §4.10): `format` is required, `name` makes versions,
    `choices` only for `decision`."""

    format: Annotated[str, Field(min_length=1, max_length=64), OR_PARAM]
    name: Annotated[str, Field(min_length=1, max_length=MAX_REF_CHARS), OR_PARAM] | None = None
    choices: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=MAX_CHOICE_CHARS)]],
            Field(max_length=MAX_CHOICES),
            OR_PARAM,
        ]
        | None
    ) = None


class ActionSpec(StrictModel):
    """An action: assignment (`task`, a file in the skill or project) and submission, plus
    `turns`, how often arr continues a turn without a submission (docs/10 §4.3)."""

    task: Annotated[str, Field(min_length=1, max_length=MAX_REF_CHARS), OR_PARAM]
    submission: SubmissionSpec
    turns: Annotated[int, Field(ge=1, le=MAX_TURNS), NO_BOOL, OR_PARAM] | None = None


class GateSpec(StrictModel):
    """`gate` of a phase (docs/10 §4.7, §4.10). Which keys fit which `type` is checked by
    `arr.plan.check` (G2–G5): `done` has `routes.done`; `signal` `field`, `rule`, `routes`,
    `on_tie`; `judge` `raters`, `action`, `rule`, `pass_score`, `criteria`, `routes`,
    `on_tie`; `user` nothing else."""

    type: Annotated[GateType, OR_PARAM]
    field: Annotated[str, Field(min_length=1, max_length=64), OR_PARAM] | None = None
    rule: Annotated[GateRule, OR_PARAM] | None = None
    routes: (
        dict[Annotated[str, Field(min_length=1, max_length=MAX_CHOICE_CHARS)], RouteSpec] | None
    ) = Field(default=None, max_length=MAX_ROUTES)
    on_tie: RouteSpec | None = None
    raters: NameList | None = None
    action: ActionSpec | None = None
    pass_score: Annotated[float, Field(allow_inf_nan=False), NO_BOOL, OR_PARAM] | None = None
    criteria: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=MAX_CHOICE_CHARS)]],
            Field(max_length=MAX_CHOICES),
            OR_PARAM,
        ]
        | None
    ) = None


class InputSpec(StrictModel):
    """One entry of `inputs` (docs/10 §4.10): exactly one source (`from` a phase, `name` of a
    submission or seed, `channel`, `peers: summary`), with `pick` and `content`."""

    from_: Annotated[str, Field(min_length=1, max_length=64), OR_PARAM] | None = Field(
        default=None, alias="from"
    )
    name: Annotated[str, Field(min_length=1, max_length=64), OR_PARAM] | None = None
    channel: Annotated[str, Field(min_length=1, max_length=64), OR_PARAM] | None = None
    peers: Annotated[Literal["summary"], OR_PARAM] | None = None
    pick: Annotated[Pick, OR_PARAM] | None = None
    content: Annotated[InputContent, OR_PARAM] | None = None


ContextSetting = Annotated[ContextMode, OR_PARAM] | dict[Ref, Annotated[ContextMode, OR_PARAM]]
"""`context` of a phase: one value for all participants or a mapping agent → value."""
FolderSetting = Annotated[FolderMode, OR_PARAM] | dict[Ref, Annotated[FolderMode, OR_PARAM]]
"""`folder` of a phase: one value for all participants or a mapping agent → value."""


class PhaseSpec(StrictModel):
    """One entry of `phases` as written (docs/10 §4.9, §4.10). Values may be whole
    placeholders; `arr.resolve` inserts them and turns the phase into a `ResolvedPhase`.

    Keys of `actions` are agent IDs or patterns (`judge-*`). Which keys fit which `topology`
    and everything that names something else is checked by `arr.resolve` and `arr.plan`.
    """

    name: Slug
    topology: Annotated[PhaseTopology, OR_PARAM]
    when: WhenCondition | None = None
    actions: dict[Ref, ActionSpec] = Field(min_length=1, max_length=MAX_ACTIONS)
    standby: NameList | None = None
    channels: NameList | None = None
    rounds: Annotated[int, Field(ge=1, le=MAX_ROUNDS), NO_BOOL, OR_PARAM] | None = None
    isolated: Annotated[bool, ONLY_BOOL, OR_PARAM] | None = None
    over: Annotated[str, Field(min_length=1, max_length=64), OR_PARAM] | None = None
    repeat: Annotated[int, Field(ge=1, le=MAX_REPEAT), NO_BOOL, OR_PARAM] | None = None
    context: ContextSetting | None = None
    folder: FolderSetting | None = None
    inputs: list[InputSpec] | None = Field(default=None, max_length=MAX_INPUTS)
    max_visits: Annotated[int, Field(ge=0, le=MAX_VISITS), NO_BOOL, OR_PARAM] | None = None
    on_exhausted: RouteSpec | None = None
    max_assignments: Annotated[int, Field(ge=1, le=MAX_ASSIGNMENTS), NO_BOOL, OR_PARAM] | None = (
        None
    )
    decider: Annotated[UserDecider, OR_PARAM] | None = None
    gate: GateSpec | None = None


class SeedSpec(StrictModel):
    """One entry of `seed` (docs/10 §4.10): `file` (a path in the project, version 1 of an
    `artifact`) or `param` (a parameter whose value is a list, a `list`). An empty value
    creates nothing."""

    file: Annotated[str, Field(max_length=MAX_REF_CHARS)] | None = None
    param: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        given = {"file", "param"} & self.model_fields_set
        if len(given) != 1:
            raise ValueError(
                "Ein Eintrag in „seed“ hat genau eins von „file: <Pfad im Projekt>“ und "
                "„param: <Parameter mit einer Liste>“."
            )
        return self


OwnFormat = Annotated[FormatSpec, _RawIfTemplated()]
"""An own format in `formats:`; with a placeholder inside it stays data until resolution."""


# -- resolved form (run.yaml of a run folder; the contract for W4b-02b, 07 and 09) ----------


class ResolvedSubmission(StrictModel):
    """`submission` of an action after resolution (docs/10 §4.3)."""

    format: str
    name: str | None = None
    choices: list[str] | None = None


class ResolvedTask(StrictModel):
    """An action without its agent: assignment, submission, `turns` written out (docs/10 §4.3)."""

    task: str
    submission: ResolvedSubmission
    turns: int = Field(default=DEFAULT_TURNS, ge=1)


class ResolvedAction(ResolvedTask):
    """The action of one agent in a phase (docs/10 §4.3). `key` is the entry of `actions` it
    comes from (an ID or a pattern), for paths in messages."""

    agent: AgentId
    key: str


class ResolvedGate(StrictModel):
    """`gate` after resolution (docs/10 §4.7): every default written out (`done` with
    `routes.done: next`), raters as concrete IDs, `pass_score` of `mean` from the rubric if the
    gate has none."""

    type: GateType = GateType.DONE
    field: str | None = None
    rule: GateRule | None = None
    routes: dict[str, Route] = Field(default_factory=dict)
    on_tie: Route | None = None
    raters: list[AgentId] = Field(default_factory=list)
    action: ResolvedTask | None = None
    pass_score: float | None = None
    criteria: list[str] | None = None


class ResolvedInput(StrictModel):
    """One input after resolution (docs/10 §4.10): `content` and `pick` written out."""

    from_: str | None = Field(default=None, alias="from")
    name: str | None = None
    channel: str | None = None
    peers: Literal["summary"] | None = None
    pick: Pick | None = None
    content: InputContent = InputContent.SUMMARY


class ResolvedPhase(StrictModel):
    """A phase after resolution (docs/10 §4.4, W4b-02a §6.3).

    Agents are concrete IDs in a stable order, every default is written out (`context: keep`
    and the `folder` that follows it per participant, `repeat: 1`, `rounds` of a `chat`, the
    gate), placeholders are inserted. Phases dropped by `when` or with no agent left are not
    in the list; routes to them lead to the next phase that is (or `stop`). `index` is the
    position in the `phases` of the input, for paths in messages.
    """

    name: Slug
    index: int = Field(ge=0)
    topology: PhaseTopology
    actions: list[ResolvedAction]
    standby: list[AgentId] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    rounds: int | None = None
    isolated: bool = False
    over: str | None = None
    repeat: int = Field(default=1, ge=1)
    context: dict[str, ContextMode] = Field(default_factory=dict)
    folder: dict[str, FolderMode] = Field(default_factory=dict)
    inputs: list[ResolvedInput] | None = None
    max_visits: int | None = Field(default=None, ge=0)
    on_exhausted: Route | None = None
    max_assignments: int | None = None
    decider: UserDecider | None = None
    gate: ResolvedGate = Field(default_factory=lambda: ResolvedGate(routes={"done": "next"}))

    @property
    def agents(self) -> list[str]:
        """Agents with an action, in order, each once."""
        return list(dict.fromkeys(a.agent for a in self.actions))

    @property
    def participants(self) -> list[str]:
        """Agents with an action, on standby and raters of the gate, each once."""
        return list(dict.fromkeys([*self.agents, *self.standby, *self.gate.raters]))


class ResolvedSeed(StrictModel):
    """A seed after resolution (docs/10 §4.10): `file` (path in the project) or `items` (the
    list from the parameter `param`). Empty seeds are not in the resolved run."""

    file: str | None = None
    param: str | None = None
    items: list[Any] | None = None


class _BothChannelKeys:
    """Marker: the input has `channels` and `groupchats` (W4b-02a §6.2)."""


_BOTH_CHANNEL_KEYS = _BothChannelKeys()
CHANNELS_KEYS = ("channels", "groupchats")
"""`channels` (docs/10 §4.10) and the older spelling `groupchats`, equal in the input."""


def _rename_channels(data: Any) -> tuple[Any, str]:
    """`channels` → `groupchats` (the attribute in code and the key of a resolved run), and
    which key the input used; both together leave a marker the field check turns into an
    error."""
    if not isinstance(data, dict) or "channels" not in data:
        return data, "groupchats"
    data = dict(data)
    channels = data.pop("channels")
    if "groupchats" in data:
        data["groupchats"] = _BOTH_CHANNEL_KEYS
        return data, "groupchats"
    data["groupchats"] = channels
    return data, "channels"


def _not_both_channel_keys(value: Any) -> Any:
    if value is _BOTH_CHANNEL_KEYS:
        raise ValueError(
            "„channels“ und „groupchats“ sind zwei Schreibweisen derselben Liste. Schreibe "
            "nur „channels“ und entferne „groupchats“."
        )
    return value


class _PlanFields(StrictModel):
    """`channels` (= `groupchats`), `phases`, `formats` and `seed`, shared by `GroupSpec` and
    `RunInput` (docs/10 §4.10). `channels_key` tells which spelling the input used, so a
    message names the key that is in the file."""

    _channels_key: str = PrivateAttr(default="groupchats")

    @model_validator(mode="wrap")
    @classmethod
    def _channels_alias(cls, data: Any, handler: ModelWrapValidatorHandler[Self]) -> Self:
        cls._unknown_with_channels(data)
        data, key = _rename_channels(data)
        model = handler(data)
        model._channels_key = key
        return model

    @classmethod
    def _unknown_with_channels(cls, data: Any) -> None:
        """An unknown key is reported as by `StrictModel`, but the allowed keys name the new
        spelling `channels` (a typo like `chanels` gets the suggestion `channels`)."""
        if not isinstance(data, dict):
            return
        public = sorted(
            {
                "channels" if name == "groupchats" else info.alias or name
                for name, info in cls.model_fields.items()
            }
        )
        known = {*public, "groupchats", *cls.model_fields}
        for key in data:
            if key not in known:
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

    @property
    def channels_key(self) -> str:
        """`channels` or `groupchats`: the key of the channel list in the input."""
        return self._channels_key


class GroupSpec(_PlanFields):
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
    groupchats: Annotated[list[GroupChatConfig], BeforeValidator(_not_both_channel_keys)] = Field(
        default_factory=list, description="Channels; in the file `channels` or `groupchats`"
    )
    stages: list[StageSpec] = Field(default_factory=list)
    raters: list[str] = Field(default_factory=list, description="Agent IDs or patterns")
    phases: list[PhaseSpec] = Field(default_factory=list, max_length=MAX_PHASES)
    formats: dict[FormatName, OwnFormat] = Field(default_factory=dict, max_length=MAX_FORMATS)
    seed: dict[Slug, SeedSpec] = Field(default_factory=dict, max_length=MAX_SEEDS)


# --------------------------------------------------------------------------------------------
# Approvals settings, run input, resolved run
# --------------------------------------------------------------------------------------------


class GatekeeperSettings(StrictModel):
    """Supervising agent that decides within the user's mandate (docs/07 §3 ④, §4).

    `auth` is the profile of its calls (W4-04); without it the Claude default of `arr.yaml`
    (`defaults.claude.auth`) is used.
    """

    model: str
    mandate: str = Field(description="Mandate file, relative to the workfolder")
    auth: Slug | None = Field(default=None, description="Auth profile; default: Claude default")


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


class RunInput(_PlanFields):
    """Content of a `run.yaml` as passed in, stored as `run.input.yaml` (docs/02 §3).

    Exactly one of `task` and `task_file` is required. `topology` may be omitted when the run
    is based on a group skill; `topology.params` then override the skill's parameters.
    Non-empty `agents`, `groupchats` (or `channels`), `stages`, `raters`, `phases`, `formats`
    and `seed` and a given `rubric` replace those of the skill.
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
    groupchats: Annotated[list[GroupChatConfig], BeforeValidator(_not_both_channel_keys)] = Field(
        default_factory=list, description="Channels; in the file `channels` or `groupchats`"
    )
    stages: list[StageSpec] = Field(default_factory=list)
    raters: list[str] = Field(default_factory=list, description="Agent IDs or patterns")
    phases: list[PhaseSpec] = Field(default_factory=list, max_length=MAX_PHASES)
    formats: dict[FormatName, OwnFormat] = Field(default_factory=dict, max_length=MAX_FORMATS)
    seed: dict[Slug, SeedSpec] = Field(default_factory=dict, max_length=MAX_SEEDS)

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
    parameters are final, limits and approvals are merged with `arr.yaml`. A run with
    `topology: phases` has its `phases`, own `formats` and non-empty `seed` (W4b-02a §6.3);
    the key of the channels stays `groupchats`, so older run folders read unchanged.
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
    phases: list[ResolvedPhase] = Field(default_factory=list)
    formats: dict[str, FormatSpec] = Field(default_factory=dict)
    seed: dict[str, ResolvedSeed] = Field(default_factory=dict)

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

    def phase(self, name: str) -> ResolvedPhase:
        """Return the phase with this name; `KeyError` if there is none (or it was dropped)."""
        for phase in self.phases:
            if phase.name == name:
                return phase
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

    python: str | None = Field(
        default=None,
        description=(
            "Base venv or interpreter, e.g. /Users/Shared/arr-<user>/venvs/mathe; outside ~ "
            "for runs with Codex agents (W3-00, arr.venvs)"
        ),
    )
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
    compact: Compact = Field(default_factory=Compact, description="Default for every agent")


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
    disabled: bool = Field(default=False, description="Not-Aus: nothing starts with this profile")

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

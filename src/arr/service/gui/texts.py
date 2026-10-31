"""German words of the GUI for statuses, kinds and deciders (docs/08-ux-konzept.md §8.2, §8.3).

Every status has symbol + word + colour (§7.1); symbols are Material Icons names (§7.3).
"""

from dataclasses import dataclass

from arr.models import Decider, RunStatus, Starter, TopologyKind
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    DecisionAction,
    DecisionScope,
    RequestCategory,
    RiskLevel,
)
from arr.service.api_models import AgentDisplayState, ApprovalItem, RunSummary, Vendor


@dataclass(frozen=True)
class Status:
    """How a status looks: CSS class (colour), Material icon, German word (§7.1, K1)."""

    css: str
    icon: str
    word: str
    pulse: bool = False


RUN_STATUS = {
    RunStatus.DRAFT: Status("st-neutral", "edit", "Entwurf"),
    RunStatus.AWAITING_APPROVAL: Status("st-wait", "front_hand", "Wartet auf Freigabe"),
    RunStatus.QUEUED: Status("st-neutral", "schedule", "In Warteschlange"),
    RunStatus.PREPARING: Status("st-run", "autorenew", "Wird vorbereitet"),
    RunStatus.RUNNING: Status("st-run", "circle", "Läuft", pulse=True),
    RunStatus.PAUSED: Status("st-neutral", "pause", "Pausiert"),
    RunStatus.FINALIZING: Status("st-run", "autorenew", "Wird abgeschlossen"),
    RunStatus.DONE: Status("st-ok", "check", "Fertig"),
    RunStatus.STOPPED: Status("st-neutral", "stop", "Gestoppt"),
    RunStatus.FAILED: Status("st-err", "close", "Fehlgeschlagen"),
    RunStatus.INTERRUPTED: Status("st-err", "bolt", "Abgebrochen"),
}
WAITING_FOR_YOU = Status("st-wait", "front_hand", "Wartet auf dich")

AGENT_STATE = {
    AgentDisplayState.WORKING: Status("st-run", "circle", "arbeitet", pulse=True),
    AgentDisplayState.IDLE: Status("st-neutral", "radio_button_unchecked", "wartet"),
    AgentDisplayState.WAITING_USER: Status("st-wait", "front_hand", "wartet auf dich"),
    AgentDisplayState.BLOCKED: Status("st-block", "hourglass_top", "blockiert"),
    AgentDisplayState.DONE: Status("st-ok", "check", "fertig"),
    AgentDisplayState.ERROR: Status("st-err", "close", "Fehler"),
    AgentDisplayState.NOT_STARTED: Status("st-neutral", "remove", "nicht gestartet"),
}


def run_status(run: RunSummary) -> Status:
    """Main status of a run; a run paused by an escalation shows „Wartet auf dich“ (§7.1)."""
    if run.open_escalation:
        return WAITING_FOR_YOU
    return RUN_STATUS[run.status]


APPROVAL_TYPE = {
    ApprovalType.PERMISSION: "Berechtigung",
    ApprovalType.ESCALATION: "Eskalation",
    ApprovalType.RUN_START: "Run-Start",
}

CATEGORY = {
    RequestCategory.NETWORK: "Netzwerk",
    RequestCategory.INSTALL: "Installation",
    RequestCategory.READ_OUTSIDE: "Lesen außerhalb",
    RequestCategory.WRITE_OUTSIDE: "Schreiben außerhalb",
    RequestCategory.PRIVILEGED: "Erhöhte Rechte",
    RequestCategory.OTHER: "Sonstiges",
}

RISK = {
    RiskLevel.LOW: ("●○○", "niedrig"),
    RiskLevel.MEDIUM: ("●●○", "mittel"),
    RiskLevel.HIGH: ("●●●", "hoch"),
}

DECIDED_BY = {
    DecidedBy.USER: "du",
    DecidedBy.GATEKEEPER: "Aufsichts-Agent",
    DecidedBy.MANDATE_RULE: "Vollmacht (von arr geprüft)",
    DecidedBy.FIXED_RULE: "Feste Regel",
}

ESCALATED_BY = {
    DecidedBy.MANDATE_RULE: "von deiner Vollmacht (von arr geprüft) an dich weitergegeben",
    DecidedBy.GATEKEEPER: "vom Aufsichts-Agenten an dich weitergegeben",
}

DECIDER = {
    Decider.USER: "Du entscheidest jede Anfrage",
    Decider.GATEKEEPER: "Aufsichts-Agent entscheidet",
    Decider.GATEKEEPER_THEN_USER: "Aufsichts-Agent entscheidet, bei Zweifel du",
}

STARTER = {
    Starter.GUI: "du (GUI)",
    Starter.CLI: "CLI",
    Starter.USER_AGENT: "Claude Code",
}

STARTER_PHRASE = {
    Starter.GUI: "von dir (GUI)",
    Starter.CLI: "über die CLI",
    Starter.USER_AGENT: "von Claude Code",
}

STARTER_SLUGS = {"gui": Starter.GUI, "cli": Starter.CLI, "claude-code": Starter.USER_AGENT}

VENDOR = {Vendor.ANTHROPIC: "Anthropic", Vendor.OPENAI: "OpenAI"}

TOPOLOGY = {
    TopologyKind.SOLO: "solo",
    TopologyKind.ROUNDTABLE: "roundtable",
    TopologyKind.EXPLORATION: "exploration",
    TopologyKind.WATERFALL: "waterfall",
    TopologyKind.REVIEW: "review",
}


TOPOLOGY_LABEL = {
    TopologyKind.SOLO: "Solo",
    TopologyKind.ROUNDTABLE: "Roundtable",
    TopologyKind.EXPLORATION: "Exploration",
    TopologyKind.WATERFALL: "Wasserfall",
    TopologyKind.REVIEW: "Review",
}

# Dative forms for running text: „abgelehnt von dir“, „erlaubt vom Aufsichts-Agenten“ (§8.1)
DECIDED_BY_PHRASE = {
    DecidedBy.USER: "von dir",
    DecidedBy.GATEKEEPER: "vom Aufsichts-Agenten",
    DecidedBy.MANDATE_RULE: "von der Vollmacht-Prüfung",
    DecidedBy.FIXED_RULE: "von einer festen Regel",
}

_OPTION_ICONS = {"retry": "replay", "continue": "arrow_forward", "end": "stop"}


def scope_label(scope: DecisionScope, agent_id: str | None) -> str:
    """Reach of a decision: „einmalig“, „für explorer-1 in diesem Run“, … (§5.2)."""
    return {
        DecisionScope.ONCE: "einmalig",
        DecisionScope.AGENT: f"für {agent_id or 'den Agenten'} in diesem Run",
        DecisionScope.RUN: "für diesen Run",
        DecisionScope.ALWAYS: "immer",
    }[scope]


def outcome(item: ApprovalItem) -> Status:
    """Badge of an item in the history: erlaubt, abgelehnt, freigegeben, … (§8.3)."""
    if item.status is ApprovalStatus.EXPIRED:
        return Status("st-neutral", "timer_off", "abgelaufen")
    if item.status is ApprovalStatus.OBSOLETE:
        return Status("st-neutral", "block", "nicht mehr nötig")
    if item.status is ApprovalStatus.OPEN or item.decision is None:
        return Status("st-wait", "front_hand", "offen")
    revoked = item.status is ApprovalStatus.REVOKED
    action = item.decision.action
    if action is DecisionAction.DENY:
        status = Status("st-err", "close", "abgelehnt")
    elif action is DecisionAction.ADJUST:
        status = Status("st-ok", "check", "angepasst")
    elif action is DecisionAction.OPTION:
        # A chosen way forward, not a success: neutral, never the colour of „fertig“ (§7.1)
        key = item.decision.option_key
        label = next((o.label for o in item.options if o.key is key), str(key))
        status = Status("st-neutral", _OPTION_ICONS.get(str(key), "alt_route"), label)
    elif item.type is ApprovalType.RUN_START:
        status = Status("st-ok", "check", "freigegeben")
    else:
        status = Status("st-ok", "check", "erlaubt")
    if revoked:
        return Status("st-neutral", "undo", f"{status.word}, zurückgezogen")
    return status


RUN_FILTER_SLUGS = {
    "alle": "all",
    "aktiv": "active",
    "wartet": "waiting",
    "fertig": "done",
    "fehler": "error",
    "archiviert": "archived",
}
RUN_FILTER_LABELS = {
    "alle": "Alle",
    "aktiv": "Aktiv",
    "wartet": "Wartet auf dich",
    "fertig": "Fertig",
    "fehler": "Fehler",
    "archiviert": "Archiviert",
}

APPROVAL_FILTERS = {
    "alle": ("Alle", None),
    "run-starts": ("Run-Starts", ApprovalType.RUN_START),
    "berechtigungen": ("Berechtigungen", ApprovalType.PERMISSION),
    "eskalationen": ("Eskalationen", ApprovalType.ESCALATION),
}

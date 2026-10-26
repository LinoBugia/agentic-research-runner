"""Simulated macOS notifications of the prototype (docs/08 §4.2, §9.2: „als Toast mit Deep-Link“).

The texts follow the journeys R2, R5, R8 and R4 („arr · Run wartet auf Freigabe – …“,
„explorer-1 möchte … · läuft in 20 min ab“). Real notifications come with W3-03.
"""

from arr.models import RunStatus, Starter
from arr.permissions.types import ApprovalType
from arr.service.api_models import ApprovalItem, RunSummary
from arr.service.gui import format as fmt
from arr.service.gui.texts import STARTER_PHRASE

FINAL_WORD = {
    RunStatus.DONE: "Run fertig",
    RunStatus.STOPPED: "Run gestoppt",
    RunStatus.FAILED: "Run fehlgeschlagen",
    RunStatus.INTERRUPTED: "Run abgebrochen",
}


def approval_notification(item: ApprovalItem, now: float) -> tuple[str, tuple[str, str]]:
    """Text and deep link of the notification for a new inbox item."""
    link = ("Ansehen", f"/freigaben/{item.id}")
    if item.type is ApprovalType.RUN_START:
        starter = STARTER_PHRASE.get(item.requested_by or Starter.USER_AGENT, "")
        return (
            f"arr · Run wartet auf Freigabe – {item.skill or 'Run'} in {item.run.project} "
            f"({starter}): {item.run.name}",
            link,
        )
    if item.type is ApprovalType.ESCALATION:
        return f"arr · {item.run.name}: {item.title}. Run pausiert.", link
    deadline = ""
    if item.deadline:
        minutes = max(0, round((item.deadline - now) / 60))
        deadline = f" · läuft in {minutes} min ab ({fmt.clock(item.deadline)})"
    return f"arr · {item.title} · {item.run.name}{deadline}", link


def run_notification(run: RunSummary) -> tuple[str, tuple[str, str]] | None:
    """Text and deep link when a run has ended (R4: „Run fertig: … · 3 von 3 Stufen …“)."""
    word = FINAL_WORD.get(run.status)
    if word is None:
        return None
    info = f" · {run.info}" if run.info else ""
    return f"arr · {word}: {run.name}{info}", ("Zum Run", f"/runs/{run.id}")

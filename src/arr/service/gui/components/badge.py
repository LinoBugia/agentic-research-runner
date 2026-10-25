"""K1 status badge: symbol + word + colour, never colour alone (docs/08-ux-konzept.md §7.1, K1)."""

from arr.service.api_models import AgentDisplayState, RunSummary
from arr.service.gui.components.base import esc, icon
from arr.service.gui.texts import AGENT_STATE, Status, run_status


def badge_html(
    status: Status, *, word: str | None = None, large: bool = False, href: str | None = None
) -> str:
    """A status pill; with `href` a link (underlined on hover, K1)."""
    cls = f"badge {status.css}" + (" badge-lg" if large else "")
    inner = icon(status.icon, small=True, pulse=status.pulse) + esc(word or status.word)
    if href:
        return f'<a class="{cls}" href="{esc(href)}">{inner}</a>'
    return f'<span class="{cls}">{inner}</span>'


def run_badges_html(run: RunSummary, approval_href: str | None = None) -> str:
    """Lifecycle badge plus the attention chip „✋ n Freigabe(n)“ (§7.1 precedence).

    A run waiting for its start approval or paused by an escalation links its main badge to
    the inbox item; other runs with open items get the second chip.
    """
    status = run_status(run)
    waiting_main = run.open_escalation or run.status.value == "awaiting_approval"
    main = badge_html(status, href=approval_href if waiting_main else None)
    if run.open_approvals and not waiting_main:
        word = f"{run.open_approvals} Freigabe" + ("n" if run.open_approvals > 1 else "")
        chip = badge_html(Status("st-wait", "front_hand", word), href=approval_href)
        return f'<span class="stack-badges">{main}{chip}</span>'
    return main


def agent_badge_html(state: AgentDisplayState) -> str:
    """Badge of an agent state (K1, K3)."""
    return badge_html(AGENT_STATE[state])

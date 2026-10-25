"""K11 copy field: monospace text, click copies, toast „Kopiert“ (docs/08-ux-konzept.md K11)."""

from arr.service.gui.components.base import esc


def copy_html(value: str, *, label: str | None = None, shown: str | None = None) -> str:
    """A button that copies `value` (handled in arr.js); `shown` is the visible text."""
    aria = label or f"{value} kopieren"
    return (
        f'<button type="button" class="copy" data-copy="{esc(value)}" aria-label="{esc(aria)}" '
        f'title="Kopieren">{esc(shown or value)}</button>'
    )

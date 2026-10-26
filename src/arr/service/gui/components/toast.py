"""K12 toast: confirmation after actions, 6 s; errors stay; at most 3 (docs/08-ux-konzept.md K12).

The container is `aria-live="polite"` (§7.7) and is created by the page frame.
"""

from nicegui import ui

from arr.service.gui.components.base import button, esc, raw

MAX_TOASTS = 3
TOAST_SECONDS = 6.0


class Toasts:
    """Stack of toasts at the bottom right of one page."""

    def __init__(self) -> None:
        self.container = ui.element("div").classes("toasts").props('aria-live="polite"')

    def show(
        self,
        message: str,
        link: tuple[str, str] | None = None,
        *,
        error: bool = False,
        sticky: bool = False,
    ) -> None:
        """Show a toast; `link` is `(label, href)`, e.g. („Zum Run“, „/runs/…“).

        `sticky` toasts (simulated macOS notifications) stay until closed, like errors.
        """
        children = list(self.container.default_slot.children)
        for old in children[: max(0, len(children) - MAX_TOASTS + 1)]:
            old.delete()
        with self.container:
            css = "toast err" if error else "toast note" if sticky else "toast"
            box = ui.element("div").classes(css)
            if sticky:  # like a macOS notification: light card with the „wartet“ edge
                box.style(
                    "background:var(--surface);color:var(--text);border:1px solid "
                    "var(--border-strong);border-left:4px solid var(--wait-strong)"
                )
            with box:
                markup = esc(message)
                if link:
                    color = ' style="color:var(--text)"' if sticky else ""
                    markup += f' <a href="{esc(link[1])}"{color}>{esc(link[0])}</a>'
                raw(markup, tag="span")
                if error or sticky:
                    button("×", lambda: _remove(box), classes="x", title="Schließen")
                else:
                    ui.timer(TOAST_SECONDS, lambda: _remove(box), once=True)


def _remove(box: ui.element) -> None:
    if not box.is_deleted:
        box.delete()

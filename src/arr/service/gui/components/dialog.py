"""K8 confirmation dialog (docs/08-ux-konzept.md G6, K8, §8.6).

A native `<dialog>`: title as a question with the object's name, consequences as a list
(including what is kept), optional alternative and text input, „Abbrechen“ + verb button.
Focus starts on the heading, Esc cancels, ⌘↩ confirms, focus returns to the trigger
(handled in arr.js).
"""

import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from arr.service.gui.components.base import button, esc, raw

_ids = itertools.count(1)

ConfirmHandler = Callable[[str], Any]


class ConfirmDialog:
    """A confirmation dialog; build once per page, fill and open it with `show()`."""

    def __init__(self) -> None:
        self.id = f"dlg-{next(_ids)}"
        self._uses = itertools.count(1)
        self._on_confirm: ConfirmHandler | None = None
        self._on_alternative: Callable[[], Any] | None = None
        self._value = ""
        self.element = (
            ui.element("dialog").classes("dlg").props(f'id="{self.id}" aria-modal="true"')
        )
        self.element.mark("dialog")
        with self.element:
            self.body = ui.element("div")
            with ui.element("div").classes("df"):
                button("Abbrechen", js=f"() => arr.closeDialog('{self.id}')").mark("dialog-cancel")
                self.alt_button = button("", self._alternative).style("display:none")
                self.alt_button.mark("dialog-alternative")
                self.confirm_button = button("", self._confirm, primary=True)
                self.confirm_button.mark("dialog-confirm")

    def show(
        self,
        title: str,
        consequences: list[str],
        confirm_label: str,
        on_confirm: ConfirmHandler,
        *,
        danger: bool = True,
        grant: bool = False,
        intro: str | None = None,
        note: str | None = None,
        input_label: str | None = None,
        input_value: str = "",
        alternative: tuple[str, Callable[[], Any]] | None = None,
    ) -> None:
        """Fill the dialog and open it; `on_confirm` receives the text input (else "").

        `grant=True` for confirmations that grant something (e.g. „immer erlauben“): the
        button then ignores Space/Enter like the card buttons (G2), ⌘↩ still works.
        """
        self._on_confirm = on_confirm
        self._on_alternative = alternative[1] if alternative else None
        self._value = input_value
        heading_id = f"{self.id}-h{next(self._uses)}"
        self.element.props(f'aria-labelledby="{heading_id}"')
        self.body.clear()
        with self.body:
            raw(f'<div class="dh"><h2 id="{heading_id}" tabindex="-1">{esc(title)}</h2></div>')
            with ui.element("div").classes("db"):
                if intro:
                    raw(f"<p>{esc(intro)}</p>")
                if consequences:
                    items = "".join(f"<li>{esc(c)}</li>" for c in consequences)
                    raw(f"<ul>{items}</ul>")
                if note:
                    raw(f'<p class="small muted">{esc(note)}</p>')
                if input_label is not None:
                    with ui.element("div").classes("field"):
                        raw(
                            f'<label for="{heading_id}-in">{esc(input_label)} '
                            '<span class="opt">optional</span></label>'
                        )
                        area = ui.element("textarea").props(f'id="{heading_id}-in" rows="3"')
                        area._props["value"] = input_value
                        area.on("input", self._set_value, js_handler="(e) => emit(e.target.value)")
                        area.mark("dialog-input")
        self.confirm_button.clear()
        with self.confirm_button:
            raw(esc(confirm_label), tag="span")
        self.confirm_button.classes(replace="btn danger solid" if danger else "btn primary")
        if grant:
            self.confirm_button.props("data-grant")
        else:
            self.confirm_button.props(remove="data-grant")
        self.alt_button.clear()
        if alternative:
            with self.alt_button:
                raw(esc(alternative[0]), tag="span")
            self.alt_button.style(remove="display:none")
        else:
            self.alt_button.style("display:none")
        ui.run_javascript(f"arr.openDialog('{self.id}', '{heading_id}')")

    def close(self) -> None:
        """Close the dialog; arr.js gives the focus back to the trigger."""
        ui.run_javascript(f"arr.closeDialog('{self.id}')")

    def _set_value(self, event: Any) -> None:
        self._value = str(event.args or "")

    async def _confirm(self) -> None:
        handler, self._on_confirm = self._on_confirm, None
        self.close()
        if handler is not None:
            result = handler(self._value)
            if isinstance(result, Awaitable):
                await result

    async def _alternative(self) -> None:
        handler, self._on_alternative = self._on_alternative, None
        self.close()
        if handler is not None:
            result = handler()
            if isinstance(result, Awaitable):
                await result

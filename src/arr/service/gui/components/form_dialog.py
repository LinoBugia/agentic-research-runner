"""Dialog with form fields, e.g. „Neues Projekt“, „Neues Auth-Profil“ (docs/08 §5.6, §5.8, K8).

Same native `<dialog>` and focus rules as `ConfirmDialog` (focus on the heading, Esc cancels,
⌘↩ confirms, focus back to the trigger; arr.js). Unlike a confirmation it stays open when
the action fails, so the error can stand at the field.
"""

import itertools
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from arr.service.gui.components.base import button, esc, raw

_ids = itertools.count(1)

SubmitHandler = Callable[[], Awaitable[str | bool | None] | str | bool | None]
"""Returns None when done (the dialog closes), an error message (the dialog stays open and
shows it) or False (the dialog stays open, e.g. while a login runs)."""


class FormDialog:
    """A form dialog; build once per page, fill it with `open()` and a body builder."""

    def __init__(self, *, wide: bool = False) -> None:
        self.id = f"form-dlg-{next(_ids)}"
        self._uses = itertools.count(1)
        self._submit: SubmitHandler | None = None
        self.element = (
            ui.element("dialog")
            .classes("dlg wide" if wide else "dlg")
            .props(f'id="{self.id}" aria-modal="true"')
        )
        self.element.mark("form-dialog")
        with self.element:
            self.head = ui.element("div").classes("dh")
            self.body = ui.element("div").classes("db")
            self.error = raw("", tag="div").props('aria-live="assertive"')
            with ui.element("div").classes("df"):
                button("Abbrechen", js=f"() => arr.closeDialog('{self.id}')").mark("form-cancel")
                self.confirm_button = button("", self._confirm, primary=True)
                self.confirm_button.mark("form-confirm")

    def open(
        self,
        title: str,
        confirm_label: str,
        build: Callable[[], Any],
        on_submit: SubmitHandler,
        *,
        danger: bool = False,
    ) -> None:
        """Fill the dialog with `build()` (inside the body) and open it."""
        self._submit = on_submit
        heading_id = f"{self.id}-h{next(self._uses)}"
        self.element.props(f'aria-labelledby="{heading_id}"')
        self.head.clear()
        with self.head:
            raw(f'<h2 id="{heading_id}" tabindex="-1">{esc(title)}</h2>')
        self.body.clear()
        with self.body:
            build()
        self.error.set_content("")
        self.confirm_button.clear()
        with self.confirm_button:
            raw(esc(confirm_label), tag="span")
        self.confirm_button.classes(replace="btn danger solid" if danger else "btn primary")
        ui.run_javascript(f"arr.openDialog('{self.id}', '{heading_id}')")

    def close(self) -> None:
        """Close the dialog; arr.js gives the focus back to the trigger."""
        ui.run_javascript(f"arr.closeDialog('{self.id}')")

    def set_confirm(self, label: str, *, visible: bool = True) -> None:
        """Change the label of the confirm button or hide it (e.g. during a login)."""
        self.confirm_button.clear()
        with self.confirm_button:
            raw(esc(label), tag="span")
        self.confirm_button.style("display:none" if not visible else "")
        if visible:
            self.confirm_button.style(remove="display:none")

    def show_error(self, message: str) -> None:
        """An error that belongs to no single field (§8.4)."""
        self.error.set_content(
            f'<div class="banner err banner-sm" role="alert" style="margin:0 20px">'
            f'<div class="txt"><b>{esc(message)}</b></div></div>'
        )

    async def _confirm(self) -> None:
        if self._submit is None:
            return
        result = self._submit()
        if isinstance(result, Awaitable):
            result = await result
        if result is False:
            return
        if result:
            self.show_error(str(result))
            return
        self.close()

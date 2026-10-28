"""K7 form field with help text (docs/08-ux-konzept.md K7, §7.7).

Label, optional mark, native input, help text, change mark „● geändert (Standard 6) ·
zurücksetzen“, error text, locked state with a reason. Help and error are linked with
`aria-describedby`. Values reach Python as strings on every input (pages debounce their
checks, §5.4 „entprellt 500 ms“) and on leaving the field. A field counts as `touched` once
you left it (K7 „Prüfung beim Verlassen“); pages show its error from then on. A field the
page focused by itself (deep link, marked `data-auto-focus`) is not touched by leaving it,
only after you typed in it or put the focus there yourself.
"""

import itertools
from collections.abc import Callable
from typing import Any

from nicegui import ui

from arr.service.gui.components.base import button, esc, raw, text

_ids = itertools.count(1)

ChangeHandler = Callable[[str], Any]


def _on_value(element: ui.element, handler: ChangeHandler, *, checkbox: bool = False) -> None:
    """Call `handler(value)` on input and change; keep the element's `value` prop in sync, so a
    later reset from Python reaches the browser."""
    prop = "checked" if checkbox else "value"
    js = "(e) => emit(e.target.checked)" if checkbox else "(e) => emit(e.target.value)"

    def handle(event: Any) -> Any:
        value = event.args
        if checkbox:
            if value:
                element._props["checked"] = True
            else:
                element._props.pop("checked", None)
            value = "true" if value else ""
        else:
            element._props[prop] = value
        return handler(str(value))

    for kind in ("change",) if checkbox else ("input", "change"):
        element.on(kind, handle, js_handler=js)


# blur: tell Python whether the focus had been set by the page and was never taken by you
_BLUR_JS = (
    "(e) => { const auto = e.target.dataset.autoFocus === '1'; "
    "delete e.target.dataset.autoFocus; emit(auto); }"
)


def auto_focus_js(selector: str) -> str:
    """JavaScript that focuses a field for you (deep link) without making it `touched`; a
    click into it counts as your own focus."""
    return (
        f"arr.whenEl({selector!r}, (el) => {{ el.scrollIntoView({{block: 'center'}}); "
        "el.dataset.autoFocus = '1'; "
        "el.addEventListener('pointerdown', () => delete el.dataset.autoFocus, {once: true}); "
        "el.focus({preventScroll: true}); })"
    )


class Field:
    """One labelled form field; `value` is always the current text of the input."""

    def __init__(
        self,
        label: str,
        *,
        name: str,
        value: str = "",
        kind: str = "text",
        options: list[tuple[str, str]] | None = None,
        help_text: str | None = None,
        optional: str | None = None,
        default: str | None = None,
        default_text: str | None = None,
        default_word: str = "Standard",
        on_change: ChangeHandler | None = None,
        width: str | None = None,
        rows: int = 8,
        locked: str | None = None,
        attrs: str = "",
        key: str | None = None,
        on_leave: Callable[[], Any] | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.touched = False
        self._typed = False
        self.default = default
        self.default_text = default_text or default
        self.default_word = default_word
        self._on_change = on_change
        self._on_leave = on_leave
        uid = next(_ids)
        self.input_id = f"in-{name}-{uid}".replace(".", "-")
        help_id = f"{self.input_id}-help"
        error_id = f"{self.input_id}-err"
        self.box = ui.element("div").classes("field").props(f'id="f-{esc(name)}"')
        if locked:
            self.box.classes("locked")
        with self.box:
            opt = f' <span class="opt">{esc(optional)}</span>' if optional else ""
            lock = ' <span class="ico ico-sm" aria-hidden="true">lock</span>' if locked else ""
            # the technical name small and in monospace (docs/08 §8.1), e.g. turns_per_idea
            code = f' <span class="opt mono">{esc(key)}</span>' if key else ""
            raw(f'<label for="{self.input_id}">{esc(label)}{code}{opt}{lock}</label>', tag="span")
            described = f'aria-describedby="{help_id} {error_id}"'
            if kind == "select":
                self.input = ui.element("select").props(f'id="{self.input_id}" {described}')
                with self.input:
                    for opt_value, opt_label in options or []:
                        option = text("option", opt_label).props(f'value="{esc(opt_value)}"')
                        if opt_value == value:
                            option.props("selected")
                self.input._props["value"] = value
            elif kind == "textarea":
                self.input = ui.element("textarea").props(
                    f'id="{self.input_id}" rows="{rows}" {described} spellcheck="false"'
                )
                self.input._props["value"] = value
            else:
                self.input = ui.element("input").props(
                    f'id="{self.input_id}" type="{kind}" {described} {attrs}'
                )
                self.input._props["value"] = value
            self.input.mark(f"field-{name}")
            if width:
                self.input.style(f"width:{width}")
            if locked:
                self.input.props('disabled aria-disabled="true"')
            _on_value(self.input, self._changed)
            self.input.on("blur", self._left, js_handler=_BLUR_JS)
            self.error = raw("", tag="span", classes="error").props(
                f'id="{error_id}" aria-live="polite"'
            )
            self.mark = ui.element("span").classes("changed")
            hint = locked or help_text or ""
            self.help = raw(esc(hint), tag="span", classes="help").props(f'id="{help_id}"')
        self._render_mark()

    def _changed(self, value: str) -> Any:
        self.value = value
        self._typed = True
        self._render_mark()
        if self._on_change is not None:
            return self._on_change(value)
        return None

    def _left(self, event: Any) -> Any:
        if self.touched:
            return None
        if event.args is True and not self._typed:
            return None  # the page put the focus here and you only moved on (W1-08.2 N2)
        self.touched = True
        if self._on_leave is not None:
            return self._on_leave()
        return None

    def set_value(self, value: str) -> None:
        """Set the value from Python (e.g. „zurücksetzen“) and tell the page."""
        self.value = value
        self.input.props(f'value="{esc(value)}"')
        self._render_mark()
        if self._on_change is not None:
            self._on_change(value)

    def set_help(self, help_text: str) -> None:
        """Replace the help text (e.g. a hint that depends on another choice)."""
        self.help.set_content(esc(help_text))

    def set_error(self, message: str | None) -> None:
        """Show or clear the error text (K7 „Fehler“)."""
        self.error.set_content(esc(message or ""))
        if message:
            self.box.classes("invalid")
            self.input.props('aria-invalid="true"')
        else:
            self.box.classes(remove="invalid")
            self.input.props(remove="aria-invalid")

    def _render_mark(self) -> None:
        self.mark.clear()
        if self.default is None or self.value == self.default:
            return
        with self.mark:
            raw(f"● geändert ({esc(self.default_word)} {esc(self.default_text)}) · ", tag="span")
            button(
                "zurücksetzen",
                lambda: self.set_value(self.default or ""),
                classes="link-btn small",
                title=f"Zurücksetzen auf {self.default_text}",
            )


def checkbox(
    label: str,
    *,
    checked: bool,
    on_change: ChangeHandler,
    attrs: str = "",
    marker: str | None = None,
) -> ui.element:
    """A native checkbox with a visible label; `on_change` gets "true" or ""."""
    with ui.element("label").classes("inline") as box:
        element = ui.element("input").props(f'type="checkbox" {attrs}')
        if marker:
            element.mark(marker)
        if checked:
            element.props("checked")
        _on_value(element, on_change, checkbox=True)
        raw(esc(label), tag="span")
    return box


def radio(
    group: str, value: str, *, checked: bool, on_change: ChangeHandler, attrs: str = ""
) -> ui.element:
    """A native radio button of `group`; `on_change` gets `value` when it is chosen."""
    element = ui.element("input").props(
        f'type="radio" name="{esc(group)}" value="{esc(value)}" {attrs}'
    )
    element.mark(f"radio-{group}-{value}")
    if checked:
        element.props("checked")
    element.on("change", lambda _e: on_change(value))
    return element

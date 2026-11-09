"""Editor for one text file of a skill (W4-02, docs/08-ux-konzept.md §5.7, K7, K8, §8.6).

A native `textarea` whose text belongs to the browser (`NativeInput`, W3-08.1 U1), with
„Speichern“ (⌘↩, no confirmation) and „Verwerfen“ (confirmation). Saving also saves an invalid
file; the check of the skill follows right below. A file changed outside since it was opened
is not overwritten: „… wurde außerhalb geändert. Neu laden“, also while you type (polled).

Unsaved changes never get lost silently: the browser knows the saved text and marks the editor
as changed; a click on a link while it is changed opens the dialog „Änderungen verwerfen?“
instead (`EDITOR_JS`), closing or reloading the tab asks through the browser (`beforeunload`).
"""

import itertools
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import ui

from arr.service.api_client import ApiClient
from arr.service.api_models import (
    ApiException,
    ErrorCode,
    SaveSkillFileRequest,
    SkillFile,
    SkillInfo,
    SkillIssue,
)
from arr.service.gui import format as fmt
from arr.service.gui.components.base import button, esc, icon, raw, set_disabled
from arr.service.gui.components.dialog import ConfirmDialog
from arr.service.gui.components.form import NativeInput, on_value

SNAPSHOT_HINT = "Laufende Runs nutzen ihren Schnappschuss, Änderungen gelten ab dem nächsten Run."
POLL_S = 3.0
"""How often the editor looks whether the file changed outside (08 §5.7 „live“)."""

SavedHandler = Callable[[SkillInfo], Awaitable[None] | None]
_ids = itertools.count(1)

# Browser side: the saved text per editor, the „changed“ mark, the guard for links and the tab,
# jumping to a line and the cursor position. Installed once per page (`install_editor_js`).
EDITOR_JS = """
<script>
(function () {
  "use strict";
  const arr = (window.arr = window.arr || {});
  if (arr.skillEditor) return;
  const saved = new Map();
  function root(id) { return document.getElementById(id); }
  function area(id) { const r = root(id); return r && r.querySelector("textarea"); }
  function mark(id) {
    const r = root(id), a = area(id);
    if (!r || !a || !saved.has(id)) return;
    const changed = a.value !== saved.get(id);
    r.dataset.dirty = changed ? "1" : "0";
    const flag = r.querySelector("[data-dirty-flag]");
    if (flag) flag.hidden = !changed;
  }
  function dirtyRoot() { return document.querySelector("[data-skill-editor][data-dirty='1']"); }
  arr.skillEditor = {
    saved(id, text) {
      arr.whenEl("#" + CSS.escape(id) + " textarea", () => {
        saved.set(id, text);
        mark(id);
      });
    },
    clean() {
      document.querySelectorAll("[data-skill-editor]").forEach((r) => {
        r.dataset.dirty = "0";
        saved.delete(r.id);
      });
    },
    dirty() { return !!dirtyRoot(); },
    go(href) { arr.skillEditor.clean(); window.location.href = href; },
    gotoLine(id, line) {
      const a = area(id);
      if (!a) return;
      const lines = a.value.split("\\n");
      let pos = 0;
      for (let i = 0; i < Math.min(line - 1, lines.length); i++) pos += lines[i].length + 1;
      a.focus();
      a.setSelectionRange(pos, pos + (lines[line - 1] || "").length);
      const lh = parseFloat(getComputedStyle(a).lineHeight) || 18;
      a.scrollTop = Math.max(0, (line - 4) * lh);
      cursor(a);
    },
  };
  function cursor(a) {
    const r = a.closest("[data-skill-editor]");
    const out = r && r.querySelector("[data-cursor]");
    if (!out) return;
    const before = a.value.slice(0, a.selectionStart);
    const line = before.split("\\n").length;
    const col = before.length - before.lastIndexOf("\\n");
    out.textContent = "Zeile " + line + ", Spalte " + col;
  }
  document.addEventListener("input", (e) => {
    const r = e.target.closest && e.target.closest("[data-skill-editor]");
    if (r) { mark(r.id); cursor(e.target); }
  });
  ["keyup", "click", "focusin"].forEach((kind) => document.addEventListener(kind, (e) => {
    if (e.target.matches && e.target.matches("[data-skill-editor] textarea")) cursor(e.target);
  }));
  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest("a[href]");
    const r = dirtyRoot();
    if (!link || !r || link.closest("dialog") || link.target === "_blank") return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
    const href = link.getAttribute("href") || "";
    if (href.startsWith("#") || link.hasAttribute("data-jump")) return;
    if (link.hasAttribute("data-copy")) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    r.dispatchEvent(new CustomEvent("arr-leave", { detail: href }));
  }, true);
  window.addEventListener("beforeunload", (e) => {
    if (!dirtyRoot()) return;
    e.preventDefault();
    e.returnValue = "";
  });
})();
</script>
"""


def install_editor_js() -> None:
    """Add the editor's browser side to the page (once per page)."""
    ui.add_head_html(EDITOR_JS)


def safe_href(href: str) -> str | None:
    """A link of this app (`/…`), else None: the dialog never sends you elsewhere."""
    if not isinstance(href, str) or not href.startswith("/") or href.startswith("//"):
        return None
    if any(ch in href for ch in "\\\n\r\t"):
        return None
    return href


def issue_where(issue: SkillIssue) -> str:
    """„group.yaml, Zeile 31“ (with the field in brackets when known)."""
    where = issue.file + (f", Zeile {issue.line}" if issue.line else "")
    return where + (f" ({issue.field})" if issue.field else "")


class SkillEditor:
    """The editor of one file; build it inside the page's content with `build()`."""

    def __init__(
        self,
        api: ApiClient,
        *,
        skill: str,
        path: str,
        on_saved: SavedHandler,
        toast: Callable[[str], Any],
        new_file: bool = False,
    ) -> None:
        self.api = api
        self.skill = skill
        self.path = path
        self.on_saved = on_saved
        self.toast = toast
        self.new_file = new_file
        self.saved = ""
        self.text = ""
        self.version: str | None = None
        self.outside: str | None = None  # version found on disk that differs from ours
        self.dom_id = f"skill-editor-{next(_ids)}"
        self.timer: ui.timer | None = None

    @property
    def dirty(self) -> bool:
        """Python's view of „changed“ (the browser keeps its own, see `EDITOR_JS`)."""
        return self.text != self.saved

    async def build(self) -> bool:
        """Load the file and build the editor; False (with a message) if it cannot be edited."""
        try:
            file = await self.api.get_skill_file(self.skill, self.path)
        except ApiException as exc:
            raw(
                f'<div class="banner err banner-sm" role="alert">{icon("error")}<div class="txt">'
                f"<b>{esc(exc.error.message)}</b></div></div>"
            ).mark("editor-refused")
            return False
        self.dialog = ConfirmDialog()
        self.root = (
            ui.element("section")
            .classes("skill-editor")
            .props(f'id="{self.dom_id}" data-skill-editor data-form-scope')
        )
        self.root.mark("skill-editor")
        self.root.on("arr-leave", self._leave, ["detail"])
        with self.root:
            with ui.element("div").classes("card-head"):
                raw(
                    f'<span class="mono small">{esc(self.skill)}/{esc(self.path)}</span>'
                    '<span class="badge st-warn" data-dirty-flag hidden>● ungespeichert</span>',
                    tag="span",
                )
                self.meta = raw("", tag="span", classes="faint small")
            raw(f'<p class="small muted ed-hint">{esc(SNAPSHOT_HINT)}</p>')
            self.outside_box = ui.element("div")
            self.area = NativeInput("textarea").props(
                f'id="{self.dom_id}-text" rows="24" spellcheck="false" autocomplete="off" '
                f'aria-label="Inhalt von {esc(self.path)}" aria-describedby="{self.dom_id}-status"'
            )
            self.area.classes("ed-area")
            self.area.mark("editor-text")
            on_value(self.area, self._typed)
            with ui.element("div").classes("ed-bar"):
                self.save_button = button(
                    "Speichern", self._save, classes="btn primary", key_hint="⌘↩"
                ).props("data-form-submit")
                self.save_button.mark("editor-save")
                self.discard_button = button("Verwerfen", self._ask_discard)
                self.discard_button.mark("editor-discard")
                raw('<span class="faint small mono" data-cursor></span>', tag="span")
            self.status = (
                raw("", tag="div", classes="small ed-status")
                .props(f'id="{self.dom_id}-status" role="status" aria-live="polite"')
                .mark("editor-status")
            )
            self.checks = ui.element("div").classes("ed-checks")
            self.checks.mark("editor-checks")
        self._load(file)
        self.timer = ui.timer(POLL_S, self._poll)
        return True

    # -- state -------------------------------------------------------------------------------

    def _load(self, file: SkillFile) -> None:
        self.saved = self.text = file.content
        self.version = file.version
        self.outside = None
        self.area.set_value(file.content)
        self.area.update()
        self._render_meta(file.size)
        self.outside_box.clear()
        ui.run_javascript(
            f"arr.skillEditor.saved({json.dumps(self.dom_id)}, {json.dumps(file.content)})"
        )

    def _render_meta(self, size: int) -> None:
        self.meta.set_content(esc(f"{fmt.size(size)} · UTF-8"))

    def _typed(self, value: str) -> None:
        self.text = value

    # -- saving ------------------------------------------------------------------------------

    async def _save(self) -> None:
        content = self.text
        set_disabled(self.save_button, True)
        try:
            result = await self.api.save_skill_file(
                self.skill, self.path, SaveSkillFileRequest(content=content, version=self.version)
            )
        except ApiException as exc:
            if exc.error.code is ErrorCode.CONFLICT and self.version is not None:
                self._show_outside(exc.error.message)
                self.status.set_content(
                    '<span class="error"><b>Nicht gespeichert.</b> Die Datei wurde außerhalb '
                    "geändert, dein Text steht noch im Editor.</span>"
                )
            else:
                self.status.set_content(
                    f'<span class="error"><b>Nicht gespeichert.</b> {esc(exc.error.message)}</span>'
                )
            return
        finally:
            set_disabled(self.save_button, False)
        self.saved = content
        self.version = result.file.version
        self.outside = None
        self.outside_box.clear()
        self._render_meta(result.file.size)
        ui.run_javascript(
            f"arr.skillEditor.saved({json.dumps(self.dom_id)}, {json.dumps(content)})"
        )
        skill = result.skill
        clock = time.strftime("%H:%M", time.localtime(self.api.now()))
        if skill.valid:
            text = f"Gespeichert um {clock}. Prüfung: keine Fehler."
            css = "ok-text"
        else:
            n = len(skill.issues)
            text = (
                f"Gespeichert um {clock}, aber ungültig: {n} Fehler. Runs mit diesem Skill lassen "
                "sich erst nach der Korrektur starten."
            )
            css = "warn-text"
        self.status.set_content(f'<span class="{css}">{esc(text)}</span>')
        self.render_checks(skill)
        result_cb = self.on_saved(skill)
        if isinstance(result_cb, Awaitable):
            await result_cb

    def render_checks(self, skill: SkillInfo) -> None:
        """The check of the skill under the editor; issues of this file jump to their line."""
        self.checks.clear()
        if skill.valid:
            return
        with self.checks:
            raw(f"<h3>Prüfung · {len(skill.issues)} Fehler</h3>")
            for issue in skill.issues:
                with ui.element("div").classes("check"):
                    raw('<span class="st bad" aria-hidden="true">✕</span>', tag="span")
                    with ui.element("span").classes("txt"):
                        if issue.file == self.path and issue.line:
                            line = issue.line
                            button(
                                issue_where(issue),
                                classes="link-btn small b",
                                js=f"() => arr.skillEditor.gotoLine({json.dumps(self.dom_id)}, "
                                f"{line})",
                                title=f"Zu Zeile {line} springen",
                            ).mark(f"goto-line-{line}")
                        else:
                            raw(f"<b>{esc(issue_where(issue))}</b>", tag="span")
                        raw(f"<small>{esc(issue.message)}</small>", tag="span")

    # -- changes outside ---------------------------------------------------------------------

    async def _poll(self) -> None:
        if self.version is None or self.outside is not None:
            return
        try:
            file = await self.api.get_skill_file(self.skill, self.path)
        except ApiException:
            return  # gone or no longer readable: saving says so
        if file.version != self.version:
            self._show_outside(f"{self.path} wurde außerhalb geändert.")
            self.outside = file.version

    def _show_outside(self, message: str) -> None:
        self.outside_box.clear()
        with (
            self.outside_box,
            ui.element("div")
            .classes("banner warn banner-sm ed-outside")
            .props('role="status"')
            .mark("editor-outside"),
        ):
            raw(icon("sync_problem"), tag="span")
            with ui.element("div").classes("txt"):
                raw(
                    f"<b>{esc(message)}</b>"
                    "<span>Neu laden zeigt die Datei, wie sie jetzt auf der Platte steht. "
                    "Ungespeicherte Änderungen hier gehen dabei verloren.</span>",
                    tag="span",
                )
            button("Neu laden", self._ask_reload, classes="btn").mark("editor-reload")

    async def _reload(self) -> None:
        try:
            file = await self.api.get_skill_file(self.skill, self.path)
        except ApiException as exc:
            self.status.set_content(f'<span class="error">{esc(exc.error.message)}</span>')
            return
        self._load(file)
        self.status.set_content(esc("Neu geladen. Du siehst den Stand auf der Platte."))

    async def _ask_reload(self) -> None:
        if not self.dirty:
            await self._reload()
            return
        self.dialog.show(
            f"Änderungen an {self.path} verwerfen und neu laden?",
            [
                "Deine Änderungen seit dem letzten Speichern gehen verloren.",
                "Du siehst danach die Datei, wie sie jetzt auf der Platte steht.",
            ],
            "Verwerfen und neu laden",
            lambda _v: self._reload(),
        )

    # -- discarding and leaving ----------------------------------------------------------------

    def _ask_discard(self) -> None:
        if not self.dirty:
            self.status.set_content(esc("Nichts zu verwerfen: keine ungespeicherten Änderungen."))
            return
        self.dialog.show(
            f"Änderungen an {self.path} verwerfen?",
            [
                "Deine Änderungen seit dem letzten Speichern gehen verloren.",
                "Die Datei auf der Platte bleibt, wie sie zuletzt gespeichert wurde.",
            ],
            "Änderungen verwerfen",
            lambda _v: self._discard(),
        )

    def _discard(self) -> None:
        self.text = self.saved
        self.area.set_value(self.saved)
        self.area.update()
        ui.run_javascript(
            f"arr.skillEditor.saved({json.dumps(self.dom_id)}, {json.dumps(self.saved)})"
        )
        self.status.set_content(esc("Änderungen verworfen."))

    def _leave(self, event: Any) -> None:
        detail = event.args.get("detail") if isinstance(event.args, dict) else event.args
        href = safe_href(str(detail or ""))
        if href is None:
            return
        self.ask_leave(href)

    def ask_leave(self, href: str) -> None:
        """Ask before leaving with unsaved changes (link, other file, other skill)."""
        go = f"arr.skillEditor.go({json.dumps(href)})"

        def leave() -> None:
            ui.run_javascript(go)  # not awaited: the page goes away

        async def save_and_go() -> None:
            await self._save()
            if not self.dirty:
                leave()

        self.dialog.show(
            f"Änderungen an {self.path} verwerfen?",
            [
                "Du verlässt den Editor mit ungespeicherten Änderungen.",
                "Deine Änderungen seit dem letzten Speichern gehen verloren.",
                "Die Datei auf der Platte bleibt, wie sie zuletzt gespeichert wurde.",
            ],
            "Verwerfen und weiter",
            lambda _v: leave(),
            alternative=("Speichern und weiter", save_and_go),
        )

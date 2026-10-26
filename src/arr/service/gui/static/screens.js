// arr GUI – client-side behaviour of the W1-08 pages (docs/08-ux-konzept.md §5, §7.6):
// - Cmd+Enter submits the form section that holds the focus ([data-form-scope]), e.g. „Run
//   starten“ or „Speichern“; approval cards and dialogs keep their own rules (arr.js)
// - jump links of check lists move the focus to the field (§5.4 „Sprunglink zum Feld“)
(function () {
  "use strict";
  const arr = (window.arr = window.arr || {});

  document.addEventListener("keydown", (e) => {
    if (!(e.key === "Enter" && (e.metaKey || e.ctrlKey))) return;
    if (document.querySelector("dialog.dlg[open]")) return; // arr.js handles dialogs
    const active = document.activeElement;
    if (!active || !active.closest) return;
    if (active.closest("[data-primary-scope]")) return; // approval cards: arr.js
    const scope = active.closest("[data-form-scope]");
    if (!scope) return;
    e.preventDefault();
    if (e.repeat) return;
    const btn = scope.querySelector("[data-form-submit]");
    if (btn && !btn.disabled && btn.getAttribute("aria-disabled") !== "true") btn.click();
  });

  // check lists: „Budget zu knapp“ → the budget field, scrolled into view and focused
  arr.jumpTo = function (id) {
    const target = document.getElementById(id);
    if (!target) return;
    const field = target.matches("input, select, textarea, button")
      ? target
      : target.querySelector("input, select, textarea, button, h2");
    target.scrollIntoView({ block: "center" });
    if (field) {
      if (field.tagName === "H2") field.setAttribute("tabindex", "-1");
      field.focus({ preventScroll: true });
    }
  };
  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest("a[data-jump]");
    if (!link) return;
    e.preventDefault();
    arr.jumpTo(link.getAttribute("data-jump"));
  });
})();

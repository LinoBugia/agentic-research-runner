"""Client-side rules of static/*.js in Node, with a small stand-in for the browser: a refused
copy never says „Kopiert“ (review W3-02 M3), a chosen addressee presets the kind (G7). The rest
of the browser behaviour is in tests/test_gui_run_detail_browser.py.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import arr.service.gui

STATIC = Path(arr.service.gui.__file__).parent / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node fehlt")

BROWSER = """
const handlers = {}, toasts = [], dialogs = [];
const element = (tag) => ({
  tag, style: {}, children: [], listeners: {},
  append(...kids) { this.children.push(...kids); },
  addEventListener(type, fn) { this.listeners[type] = fn; },
  showModal() { this.open = true; dialogs.push(this); },
  close() { this.open = false; }, remove() {}, focus() {},
  select() { this.selected = true; },
});
globalThis.window = globalThis;
window.addEventListener = () => {};
globalThis.location = { search: "", href: "http://test/", pathname: "/" };
globalThis.history = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.requestAnimationFrame = () => 0;
globalThis.setInterval = () => 0;
globalThis.getComputedStyle = () => ({});
globalThis.ResizeObserver = globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.document = {
  documentElement: { setAttribute() {}, getAttribute: () => null, removeAttribute() {} },
  addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  createElement: element, body: { appendChild() {} },
};
const click = async (target) => {
  for (const fn of handlers.click) fn({ target, preventDefault() {} });
  await new Promise((done) => setImmediate(done));
};
"""


def run(script: str, name: str, scenario: str) -> Any:
    code = f"""{BROWSER}
{(STATIC / script).read_text("utf-8")}
(async () => {{ {scenario} }})().then((out) => {{
  console.log(JSON.stringify(out));
  process.exit(0);
}});
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-"],  # type: ignore[list-item]
        input=code,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


COPY = """
arr.toast = (text) => toasts.push(text);
Object.defineProperty(globalThis, "isSecureContext", { value: %(secure)s, configurable: true });
Object.defineProperty(globalThis, "navigator", {
  value: { clipboard: { writeText: () => %(answer)s } }, configurable: true,
});
const button = { getAttribute: () => "der Text" };
await click({ closest: (sel) => (sel === "[data-copy]" ? button : null) });
const area = dialogs.length ? dialogs[0].children.find((c) => c.tag === "textarea") : null;
return {
  toasts,
  value: area && area.value,
  selected: !!(area && area.selected),
  words: dialogs.length ? dialogs[0].children.map((c) => c.textContent || "").join(" ") : "",
};
"""


def test_copy_says_copied_only_when_the_browser_accepted() -> None:
    out = run("arr.js", "ok", COPY % {"secure": "true", "answer": "Promise.resolve()"})
    assert out["toasts"] == ["Kopiert"] and out["value"] is None


@pytest.mark.parametrize(
    "case",
    [
        {"secure": "true", "answer": 'Promise.reject(new Error("NotAllowedError"))'},
        {"secure": "false", "answer": "Promise.resolve()"},
    ],
    ids=["abgelehnt", "kein sicherer Kontext"],
)
def test_refused_copy_marks_the_text_for_the_shortcut(case: dict[str, str]) -> None:
    out = run("arr.js", "refused", COPY % case)
    assert out["toasts"] == []
    assert out["value"] == "der Text" and out["selected"]
    assert "nicht kopiert" in out["words"] and "⌘C" in out["words"]


KIND = """
const kind = { name: "kind", value: "note", dataset: {} };
const to = { name: "to", value: "" };
const form = { kind, to };
kind.closest = to.closest = (sel) => (sel === "form.composer" ? form : null);
const change = (target) => handlers.change.forEach((fn) => fn({ target }));
const seen = [];
to.value = "a"; change(to); seen.push(kind.value);
to.value = ""; change(to); seen.push(kind.value);
kind.value = "idea"; change(kind);
to.value = "b"; change(to); seen.push(kind.value);
return seen;
"""


def test_addressee_presets_question_until_the_kind_was_chosen() -> None:
    assert run("run_detail.js", "kind", KIND) == ["question", "note", "idea"]

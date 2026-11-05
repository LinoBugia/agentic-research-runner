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


SUGGEST = """
const suggest = %(js)s;
const note = { textContent: "" };
const opt = (value, textContent) => ({ value, textContent });
const options = [opt("", "alle in #main"), opt("diskutant-1", "diskutant-1"),
                 opt("diskutant-2", "diskutant-2")];
const kind = { name: "kind", value: "note", dataset: {} };
const to = { name: "to", value: "", options,
             get selectedIndex() { return options.findIndex((o) => o.value === this.value); } };
const body = { name: "body", value: "" };
const form = { kind, to, body, querySelector: (sel) => (sel === ".cmp-suggest" ? note : null) };
for (const el of [kind, to, body]) {
  el.form = form;
  el.closest = (sel) => (sel === "form.composer" ? form : null);
}
const tick = () => new Promise((done) => setTimeout(done, 0));
// the form's own listener first, then run_detail.js on the document (bubbling)
const fire = async (target, type) => {
  suggest({ target });
  if (type === "change") handlers.change.forEach((fn) => fn({ target }));
  await tick();
  return [kind.value, note.textContent];
};
const type = (text) => { body.value = text; return fire(body, "input"); };
const pick = (who) => { to.value = who; return fire(to, "change"); };
const seen = [];
seen.push(await type("Alles klar bei euch?"));
seen.push(await type("Mein Vorschlag. Frage an @diskutant-2: passt das?"));
seen.push(await type("Mein Vorschlag. Frage an diskutant-2: passt das?"));
seen.push(await type("@diskutant-10 und mail@diskutant-1.de"));
seen.push(await type("@alle, bitte kurz melden."));
seen.push(await type(""));
seen.push(await pick("diskutant-1"));
seen.push(await pick(""));
kind.value = "idea";
seen.push(await fire(kind, "change"));
seen.push(await type("Idee für @diskutant-1"));
// W3-08.1: after sending, the emptied field takes the suggestion back
delete kind.dataset.chosen; kind.value = "note";
seen.push(await type("Frage an @diskutant-2?"));
seen.push(await type(""));  // SENT_JS: rd.sent() empties the field, then an input event
return seen;
"""


def test_w3_08_u6_mention_or_single_addressee_suggests_question() -> None:
    """U6: `@name` of a member anywhere in the text (also mid-sentence, U5) or one addressee
    suggests „Frage“ and says why; without them it falls back to „Notiz“; a kind you picked
    yourself stays."""
    from arr.service.gui.pages.run_detail_channel import SUGGEST_QUESTION_JS

    out = run("run_detail.js", "suggest", SUGGEST % {"js": SUGGEST_QUESTION_JS})
    why = "Art „Frage“ vorgeschlagen: "
    assert out == [
        ["note", ""],
        ["question", why + "du sprichst @diskutant-2 an."],
        ["note", ""],
        ["note", ""],  # no member: @diskutant-10, an e-mail address
        ["question", why + "du sprichst @alle an."],
        ["note", ""],
        ["question", why + "du schreibst an diskutant-1."],
        ["note", ""],
        ["idea", ""],
        ["idea", ""],
        ["question", why + "du sprichst @diskutant-2 an."],
        ["note", ""],
    ]

"""W3-08.1 U1: typed text survives re-renders, in Node with the real Vue of NiceGUI and NiceGUI's
own `renderRecursively` (static/nicegui.js) on a small stand-in DOM.

NiceGUI renders the whole page in one Vue render function, and Vue sets `value` of a native
field on every render from the value stored on the client. At 61568ba nothing updated that
store while you typed, so the next update of any element put the template back. The browser
check of the lead (W3-08 Abschlussbericht, „Prüfung von Hand“) saw exactly that. Here: the old
handler loses the text (counter-check), `STORE_JS` keeps it without a render, an update of the
field itself keeps the newer browser text through `preserved_props` (review finding 7), and a
value set from Python (`set_value`) still arrives.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import nicegui
import pytest
from test_gui import gui_user
from test_gui_screens import _walk, see

from arr.service.gui.components.form import STORE_JS, NativeInput
from arr.service.mock_api import MockApi

NICEGUI = Path(nicegui.__file__).parent / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node fehlt")

OLD_JS = "(e) => emit(e.target.value)"
"""The input handler of `form.py` at 61568ba: the text goes to Python, the client store stays."""

HARNESS = r"""
// --- a stand-in DOM, enough for Vue's runtime-dom -------------------------------------------
class FakeNode {
  constructor(tag) {
    this.tagName = tag ? tag.toUpperCase() : undefined;
    this.nodeType = tag ? 1 : 3;
    this.childNodes = []; this.parentNode = null; this.attrs = {}; this.listeners = {};
    this.style = { setProperty() {}, removeProperty() {} };
    if (tag === "textarea" || tag === "input") this.value = "";
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const kids = this.parentNode.childNodes;
    return kids[kids.indexOf(this) + 1] || null;
  }
  insertBefore(child, anchor) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const i = anchor ? this.childNodes.indexOf(anchor) : -1;
    if (i < 0) this.childNodes.push(child); else this.childNodes.splice(i, 0, child);
    child.parentNode = this;
  }
  removeChild(child) {
    this.childNodes.splice(this.childNodes.indexOf(child), 1);
    child.parentNode = null;
  }
  set textContent(text) { this.childNodes = []; this._text = text; }
  get textContent() { return this._text || ""; }
  set innerHTML(_html) { this.childNodes = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  hasAttribute(k) { return k in this.attrs; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  removeEventListener(type) { delete this.listeners[type]; }
}
globalThis.Node = globalThis.Element = FakeNode;
globalThis.SVGElement = globalThis.MathMLElement = globalThis.ShadowRoot = class {};
globalThis.Window = class {};
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (text) => Object.assign(new FakeNode(), { nodeValue: text }),
  createComment: (text) => Object.assign(new FakeNode(), { nodeValue: text, nodeType: 8 }),
  querySelector: () => null,
};
globalThis.window = globalThis;
const sent = [];
window.socket = { emit: (_kind, msg) => sent.push(msg) };
window.did_handshake = true;
globalThis.False = false;
const Vue = await import(%(vue)s);  // after the DOM: Vue looks for `document` when loaded
globalThis.Vue = Vue;
// --- NiceGUI's own client code ---------------------------------------------------------------
const source = %(nicegui)s;
const part = source.slice(source.indexOf("function stringifyEventArgs"),
                          source.indexOf("function runJavascript"));
const renderRecursively = new Function(part + "; return renderRecursively;")();
// the update handler of nicegui.js for one element (preserved_props, then replace)
const update = (vm, id, element) => {
  for (const prop of element.preserved_props ?? []) {
    const now = vm.elements[id];
    if (now && prop in now.props) element.props[prop] = now.props[prop];
  }
  delete element.preserved_props;
  vm.elements[id] = element;
};
// --- the page: a box with the textarea and the change mark beside it ------------------------
const TEMPLATE = "# Aufgabe\n\n";
const listener = (js) => ({ listener_id: "l1", type: "input", specials: [], modifiers: [], keys: [],
                            args: null, throttle: 0, leading_events: true, trailing_events: true,
                            js_handler: js });
const el = (tag, props = {}, children = [], events = [], text = null) =>
  ({ tag, class: [], style: {}, props, slots: {}, children, events, text });
const page = (js) => ({
  0: el("div", {}, [1, 2]),
  1: el("textarea", { value: TEMPLATE }, [], [listener(js)]),
  2: el("span", {}, [], [], ""),
});
const run = async (js, steps) => {
  sent.length = 0;
  let renders = 0;
  const container = new FakeNode("div");
  const vueApp = Vue.createApp({
    data: () => ({ elements: page(js) }),
    render() { renders += 1; return renderRecursively(this.elements, 0); },
  });
  globalThis.app = vueApp;
  const native = (t) => ["div", "span", "textarea"].includes(t);
  if (!vueApp.config.isNativeTag) vueApp.config.isNativeTag = native;
  const vm = vueApp.mount(container);
  const area = container.childNodes[0].childNodes[0];
  const out = [];
  const type = async (text) => {
    area.value = text;  // what the browser does when you type
    area.listeners.input({ type: "input", target: area });
    await Vue.nextTick();
  };
  const mark = async (text) => {
    update(vm, "2", el("span", {}, [], [], text));
    await Vue.nextTick();
  };
  const own = async (value, preserved) => {  // an update of the field itself from Python
    const next = el("textarea", { value, "aria-invalid": "true" }, [], [listener(js)]);
    if (preserved) next.preserved_props = ["value"];
    update(vm, "1", next);
    await Vue.nextTick();
  };
  for (const [what, arg, flag] of steps) {
    const before = renders;
    if (what === "type") await type(arg);
    if (what === "mark") await mark(arg);
    if (what === "own") await own(arg, flag);
    out.push({ value: area.value, rendered: renders - before });
  }
  return { out, sent: sent.map((m) => JSON.parse(m.args[0])) };
};
const LONG = TEMPLATE + "Gibt es unendlich viele Primzahlzwillinge? ".repeat(40);
return {
  old: await run(%(old)s, [["type", LONG], ["mark", "● geändert"]]),
  fixed: await run(%(fixed)s, [
    ["type", LONG], ["mark", "● geändert"], ["mark", ""],
    ["type", LONG + "Weiter."],
    ["own", LONG, true],             // Python is one keystroke behind, sends aria-invalid
    ["own", "zurückgesetzt", false],  // set_value from Python
  ]),
  long: LONG.length,
};
"""


def _run_harness() -> dict[str, Any]:
    code = HARNESS % {
        "vue": json.dumps((NICEGUI / "vue.esm-browser.prod.js").as_uri()),
        "nicegui": json.dumps((NICEGUI / "nicegui.js").read_text("utf-8")),
        "old": json.dumps(OLD_JS),
        "fixed": json.dumps(STORE_JS.format(prop="value")),
    }
    script = (
        f"const result = await (async () => {{ {code} }})();\nconsole.log(JSON.stringify(result));"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-"],  # type: ignore[list-item]
        input=script,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_w3_08_1_u1_typed_text_survives_every_re_render_in_vue() -> None:
    result = _run_harness()
    long = "# Aufgabe\n\n" + "Gibt es unendlich viele Primzahlzwillinge? " * 40
    assert result["long"] == len(long)
    # counter-check, the state of 61568ba: any update puts the template back
    typed, after_mark = result["old"]["out"]
    assert typed["value"] == long
    assert after_mark["value"] == "# Aufgabe\n\n", "the bug of the browser check is reproduced"
    # fixed: the text stays through updates of other elements, typing renders nothing
    steps = result["fixed"]["out"]
    assert [s["value"] for s in steps[:4]] == [long, long, long, long + "Weiter."]
    assert steps[0]["rendered"] == 0 and steps[3]["rendered"] == 0
    assert steps[1]["rendered"] == 1  # the mark changed: a real render, and still no loss
    # review finding 7: Python sends the field one keystroke behind (aria-invalid) → kept
    assert steps[4]["value"] == long + "Weiter."
    # „zurücksetzen“: a value from Python replaces the text
    assert steps[5]["value"] == "zurückgesetzt"
    assert result["fixed"]["sent"] == [long, long + "Weiter."], "every text reached Python"


async def test_w3_08_1_u1_python_marks_the_browser_text_as_preserved() -> None:
    """`NativeInput._to_dict`: after typing, an update of the field (here `set_error` with
    `aria-invalid`, review finding 7) keeps the browser's text; after „zurücksetzen“ (Python
    sets the value) it does not."""
    async with gui_user(MockApi()) as user:
        await user.open("/neuer-run?skill=exploration&projekt=collatz")
        await see(user, "Die Aufgabe fehlt.")
        budget = user.find(marker="field-limits.budget_usd")
        [element] = budget.elements
        assert isinstance(element, NativeInput)
        assert "preserved_props" not in element._to_dict()  # nothing typed yet
        budget.trigger("input", "10 $")
        assert element._to_dict()["preserved_props"] == ["value"]
        assert element._to_dict()["props"]["value"] == "10 $"
        budget.trigger("change", "10 $")
        budget.trigger("blur", False)
        await see(user, "Zu knapp")
        assert element.props.get("aria-invalid") == "true"
        assert element._to_dict()["preserved_props"] == ["value"]
        with user.client:
            [reset] = [
                node
                for node in _walk(user.client.layout)
                if str(node.props.get("title", "")).startswith("Zurücksetzen auf")
            ]
        reset.mark("reset-budget")
        user.find(marker="reset-budget").click()
        data = element._to_dict()
        assert "preserved_props" not in data and data["props"]["value"] != "10 $"


async def test_w3_08_1_dialog_comment_survives_live_updates() -> None:
    """The comment in a confirmation dialog (components/dialog.py) is a `NativeInput` with the
    handler of the Node test above: live updates of the page while the dialog is open keep
    the typed text, an update of the field keeps it through `preserved_props`."""
    from test_gui import _within

    api = MockApi()
    async with gui_user(api) as user:
        await user.open("/freigaben/apr_0041")
        _within(user, "card-apr_0041", "deny").click()
        await user.should_see(content="Run-Start ablehnen?")
        area = user.find(marker="dialog-input")
        [element] = area.elements
        assert isinstance(element, NativeInput)
        handlers = {(lis.type, lis.js_handler) for lis in element._event_listeners.values()}
        assert ("input", STORE_JS.format(prop="value")) in handlers
        area.trigger("input", "Bitte ohne Web")
        assert element._to_dict()["preserved_props"] == ["value"]
        user.find(marker="dialog-confirm").click()
        await user.should_see(marker="decided-apr_0041")
        assert (await api.get_approval("apr_0041")).decision.comment == "Bitte ohne Web"

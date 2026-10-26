// arr GUI – typeset formulas (docs/08-ux-konzept.md G7, §7.2, §7.7).
// markdown_html renders `$…$` as <span class="math" data-tex="…"> with readable Unicode text.
// This script loads KaTeX (shipped with NiceGUI, URL in data-katex) and replaces the text with
// MathML, which the browser typesets with its math font; the LaTeX source stays in the MathML
// annotation for screen readers and copying. Without KaTeX the Unicode text stays.
(function () {
  "use strict";
  const script = document.currentScript;
  const url = script && script.dataset.katex;
  if (!url) return;
  let katex = null;
  let queued = false;

  function typeset() {
    queued = false;
    document.querySelectorAll(".math[data-tex]:not([data-typeset])").forEach((el) => {
      el.setAttribute("data-typeset", "1");
      try {
        el.innerHTML = katex.renderToString(el.getAttribute("data-tex"), {
          output: "mathml",
          displayMode: el.classList.contains("math-display"),
          throwOnError: false,
          strict: "ignore",
        });
        el.removeAttribute("title");
      } catch (e) {
        /* keep the Unicode text */
      }
    });
  }
  function queue() {
    if (!queued && katex) {
      queued = true;
      requestAnimationFrame(typeset);
    }
  }
  import(url)
    .then((module) => {
      katex = module.default || module;
      queue();
      new MutationObserver(queue).observe(document.body, { childList: true, subtree: true });
    })
    .catch(() => {
      /* NiceGUI without KaTeX: formulas stay as text */
    });
})();

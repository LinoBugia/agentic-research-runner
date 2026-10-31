// arr GUI – client-side behaviour that must not wait for the server (docs/08-ux-konzept.md):
// - deep links scroll the approval card into view and focus its heading, never a button (G2, K5)
// - Cmd+Enter acts only on the card (or dialog) that holds the focus; Space/Enter on a grant
//   button does nothing (G2)
// - dialogs: focus on the heading, Esc cancels, focus returns to the trigger (K8)
// - deadline countdown, copy fields (refused: text marked for ⌘C), clickable table rows, filter
//   controls
// - connection banner instead of NiceGUI's own note (§4.2, §8.4)
(function () {
  "use strict";
  const arr = (window.arr = window.arr || {});
  const root = document.documentElement;
  const params = new URLSearchParams(location.search);
  const theme = params.get("theme");
  if (theme === "dark" || theme === "light") root.setAttribute("data-theme", theme);
  // Appearance from the app settings: the page head sets it; if the page was slow and the
  // head script is missing (W1-08.2), the frame's data-theme and the browser's memory do it.
  const isTheme = (t) => t === "dark" || t === "light";
  try {
    const remembered = localStorage.getItem("arr-theme");
    if (!theme && isTheme(remembered) && !root.getAttribute("data-theme")) root.setAttribute("data-theme", remembered);
  } catch (e) {}
  arr.applyTheme = function (t) {
    if (theme) return; // ?theme= of the URL wins
    if (isTheme(t)) {
      root.setAttribute("data-theme", t);
      try { localStorage.setItem("arr-theme", t); } catch (e) {}
    } else {
      root.removeAttribute("data-theme");
      try { localStorage.removeItem("arr-theme"); } catch (e) {}
    }
  };
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  // ---- helpers ------------------------------------------------------------------------------
  function whenEl(selector, fn, timeoutMs) {
    const found = document.querySelector(selector);
    if (found) return fn(found);
    const limit = Date.now() + (timeoutMs || 8000);
    const obs = new MutationObserver(() => {
      const el = document.querySelector(selector);
      if (el) {
        obs.disconnect();
        fn(el);
      } else if (Date.now() > limit) {
        obs.disconnect();
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }
  arr.whenEl = whenEl;
  arr.offset = 0; // mock clock minus browser clock, in seconds
  arr.now = () => Date.now() / 1000 + arr.offset;
  const pad = (n) => String(n).padStart(2, "0");
  function hhmm(ts) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function hhmmss(ts) {
    const d = new Date(ts * 1000);
    return hhmm(ts) + ":" + pad(d.getSeconds());
  }
  // Same wording as arr.service.gui.format.deadline (08 §8.7 „läuft in 14 min ab (15:36)“)
  arr.deadlineText = function (deadline, now) {
    const left = deadline - now;
    const at = hhmm(deadline);
    if (left <= 0) return "Frist abgelaufen (" + at + ")";
    if (left < 120) {
      const m = Math.floor(left / 60);
      const s = Math.floor(left % 60);
      return "läuft in " + (m ? m + " min " : "") + s + " s ab (" + at + ")";
    }
    if (left < 3600) return "läuft in " + Math.ceil(left / 60) + " min ab (" + at + ")";
    const mins = Math.ceil(left / 60);
    return "läuft in " + Math.floor(mins / 60) + ":" + pad(mins % 60) + " h ab (" + at + ")";
  };
  arr.setText = function (id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  arr.setHtml = function (id, markup) {
    const el = document.getElementById(id);
    if (el && el.innerHTML !== markup) el.innerHTML = markup;
  };
  arr.toast = function (text) {
    const box = document.querySelector(".toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = text;
    box.appendChild(t);
    setTimeout(() => t.remove(), 6000);
  };

  // ---- focus after deep links (G2, K5, §4.3) ------------------------------------------------
  function settle(go) {
    // Re-align a few times while layout settles (fonts, late updates), unless the user acts.
    let acted = false;
    const stop = () => (acted = true);
    ["wheel", "touchstart", "keydown", "mousedown"].forEach((ev) =>
      window.addEventListener(ev, stop, { once: true, capture: true }),
    );
    go();
    requestAnimationFrame(() => requestAnimationFrame(() => acted || go()));
    setTimeout(() => acted || go(), 250);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => acted || go());
  }
  arr.focusCard = function (id) {
    whenEl("#" + CSS.escape(id) + " h3", (heading) => {
      const card = heading.closest(".appr");
      document.querySelectorAll(".appr.focus").forEach((c) => c.classList.remove("focus"));
      if (card) card.classList.add("focus");
      heading.setAttribute("tabindex", "-1");
      settle(() => {
        (card || heading).scrollIntoView({ block: "start" });
        if (document.activeElement !== heading) heading.focus({ preventScroll: true });
      });
    });
  };
  arr.focusHeading = function (selector) {
    whenEl(selector, (h) => {
      h.setAttribute("tabindex", "-1");
      settle(() => {
        if (document.activeElement !== h) h.focus({ preventScroll: true });
      });
    });
  };
  arr.focusRowLink = function (rowId) {
    whenEl("#" + CSS.escape(rowId) + " a", (link) => link.focus());
  };
  arr.focusAfterRemoval = function (selector, removedId) {
    // After a card disappeared: all cards are disarmed. If the focus was in that card, it moves
    // to the next card's heading (else the page heading); focus elsewhere stays where it is.
    arr.disarmAll();
    const limit = Date.now() + 1000;
    const settleFocus = () => {
      if (removedId && document.getElementById(removedId) && Date.now() < limit) {
        requestAnimationFrame(settleFocus);
        return;
      }
      arr.disarmAll();
      const active = document.activeElement;
      if (active && active !== document.body && document.contains(active)) return;
      const next = selector && document.querySelector(selector);
      const target = next || document.querySelector("#inhalt h1");
      if (target) {
        target.setAttribute("tabindex", "-1");
        target.focus({ preventScroll: !next });
      }
    };
    settleFocus();
  };

  whenEl(".app", (app) => {
    if ("theme" in app.dataset) arr.applyTheme(app.dataset.theme);
    if (app.dataset.now) arr.offset = parseFloat(app.dataset.now) - Date.now() / 1000;
    const hashId = location.hash.slice(1);
    const card = app.dataset.focusCard || params.get("focus") || (hashId.startsWith("apr_") ? hashId : "");
    if (card) arr.focusCard(card);
    else if (app.dataset.focus) arr.focusHeading(app.dataset.focus);
    else if (hashId) {
      // other anchors (e.g. /#warnungen): the section appears after Vue mounted, so scroll here
      whenEl("#" + CSS.escape(hashId), (section) => {
        const h = section.querySelector("h2, h3") || section;
        h.setAttribute("tabindex", "-1");
        settle(() => {
          section.scrollIntoView({ block: "start" });
          if (document.activeElement !== h) h.focus({ preventScroll: true });
        });
      });
    }
  });

  // ---- arming: granting only on a card that was seen (G2, K5, W1-06.1 H1, W1-06.2 N1/N2) ---
  // Granting (Cmd+Enter or a click on a [data-grant] button) works only once the card has been
  // seen for ARM_MS without a break: its head (type, risk, deadline, title) and its row of
  // decision buttons lie in the visible area below the top bar and inside the visible part of
  // every scrolling column around the card (run detail, W1-07; cards taller than that area keep
  // the button row at the bottom edge, see .tall), and every command (.cmd) has been in view
  // from its start to its end at least once (W1-07.1; not necessarily together with the head).
  // Leaving the visible area starts the ARM_MS again, also for a card that was armed
  // (W1-07.1). Every decision, page load and deep link disarms all cards and forgets what was
  // seen. Cmd+Enter also needs a fresh key press (no auto-repeat). Dialogs accept granting
  // ARM_MS after they opened.
  const ARM_MS = 1500;
  const TALL_MARGIN = 48;
  function inView(r, top, bottom) {
    return r.height > 0 && r.top >= top - 1 && r.bottom <= bottom + 1;
  }
  function band(card) {
    // visible band: below the top bar, inside the window and every scrolling column
    const bar = document.querySelector(".topbar");
    let top = bar ? bar.getBoundingClientRect().bottom : 0;
    let bottom = window.innerHeight;
    for (let p = card.parentElement; p && p !== document.body; p = p.parentElement) {
      if (/(auto|scroll|hidden)/.test(getComputedStyle(p).overflowY)) {
        const r = p.getBoundingClientRect();
        top = Math.max(top, r.top);
        bottom = Math.min(bottom, r.bottom);
      }
    }
    return [top, bottom];
  }
  function seen(card, top, bottom) {
    const head = card.querySelector(".ah") || card;
    const title = card.querySelector("h3") || head;
    const buttons = card.querySelector(".af") || card;
    return (
      inView(head.getBoundingClientRect(), top, bottom) &&
      inView(title.getBoundingClientRect(), top, bottom) &&
      inView(buttons.getBoundingClientRect(), top, bottom)
    );
  }
  function noteCommands(card, top, bottom) {
    // a sticky button row (tall card) covers the lower edge of the visible band
    const buttons = card.querySelector(".af");
    const limit = card.classList.contains("tall") && buttons ? Math.min(bottom, buttons.getBoundingClientRect().top) : bottom;
    let all = true;
    card.querySelectorAll(".cmd").forEach((cmd) => {
      const r = cmd.getBoundingClientRect();
      if (r.height > 0 && r.top >= top - 1 && r.top < limit) cmd._arrStart = true;
      if (r.height > 0 && r.bottom <= limit + 1 && r.bottom > top) cmd._arrEnd = true;
      all = all && cmd._arrStart && cmd._arrEnd;
    });
    return all;
  }
  function trackVisibility() {
    const now = performance.now();
    const bar = document.querySelector(".topbar");
    const room = window.innerHeight - (bar ? bar.getBoundingClientRect().bottom : 0);
    document.querySelectorAll("[data-primary-scope]").forEach((card) => {
      // Only a card taller than the visible area keeps its decision row at the window's
      // bottom edge (CSS .tall); otherwise head and buttons could never be in view together.
      card.classList.toggle("tall", card.offsetHeight > room - 8);
      const [top, bottom] = band(card);
      if (card.classList.contains("compact")) {
        // A compact card taller than its column can never be seen whole: it says so and leaves
        // „Details“ instead of a button that stays locked (W1-07.1). The margin keeps the hint
        // from flickering, because it changes the height of the card a little.
        const column = bottom - top;
        const height = card.offsetHeight;
        if (height > column) card.classList.add("too-tall");
        else if (height + TALL_MARGIN < column) card.classList.remove("too-tall");
      }
      const commands = noteCommands(card, top, bottom);
      if (!seen(card, top, bottom)) {
        card._arrSince = null; // out of view: an armed card has to be seen again
        card._arrArmed = false;
        return;
      }
      if (card._arrArmed) return;
      if (!commands) card._arrSince = null;
      else if (card._arrSince == null) card._arrSince = now;
      else if (now - card._arrSince >= ARM_MS) card._arrArmed = true;
    });
  }
  setInterval(trackVisibility, 100);
  arr.disarmAll = function () {
    document.querySelectorAll("[data-primary-scope]").forEach((card) => {
      card._arrSince = null;
      card._arrArmed = false;
      card.querySelectorAll(".cmd").forEach((cmd) => (cmd._arrStart = cmd._arrEnd = false));
    });
  };
  arr.isArmed = function (scope) {
    if (scope.tagName === "DIALOG") {
      return scope._arrOpenedAt != null && performance.now() - scope._arrOpenedAt >= ARM_MS;
    }
    trackVisibility();
    return !!scope._arrArmed;
  };
  // Clicks on grant buttons obey the same rule; a locked click does nothing. „Ablehnen“ and
  // other buttons are not [data-grant] and stay free.
  document.addEventListener(
    "click",
    (e) => {
      const grant = e.target && e.target.closest && e.target.closest("[data-grant]");
      if (!grant) return;
      const scope = grant.closest("dialog.dlg[open], [data-primary-scope]");
      if (scope && !arr.isArmed(scope)) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    },
    true,
  );
  // A decided card shows a confirmation of the same height for a moment (see freigaben.py):
  // move the focus there, so nothing slides under pointer or focus.
  arr.showDecided = function (cardId) {
    arr.disarmAll();
    whenEl("#" + CSS.escape(cardId) + " .appr-done", (done) => {
      done.setAttribute("tabindex", "-1");
      done.focus({ preventScroll: true });
    });
  };

  // ---- keyboard: Cmd+Enter only in the focused card; no single-key grants (G2, §7.6) --------
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        const dialog = document.querySelector("dialog.dlg[open]");
        const active = document.activeElement;
        const scope = dialog || (active && active.closest && active.closest("[data-primary-scope]"));
        if (!scope) return; // focus outside a card: nothing happens
        e.preventDefault();
        if (e.repeat || !arr.isArmed(scope)) return; // held key or not seen long enough
        const btn = scope.querySelector("[data-primary]");
        if (btn && !btn.disabled && btn.getAttribute("aria-disabled") !== "true") btn.click();
        return;
      }
      const onControl = e.target && e.target.closest && e.target.closest("button, a, [data-grant]");
      if (e.repeat && onControl && (e.key === " " || e.key === "Enter")) {
        e.preventDefault(); // a held key never clicks a button twice
        e.stopPropagation();
        return;
      }
      const grant = e.target && e.target.closest && e.target.closest("[data-grant]");
      if (grant && (e.key === " " || e.key === "Enter") && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        e.stopPropagation();
        arr.toast("Per Tastatur erteilst du das mit ⌘↩, damit nichts aus Versehen passiert.");
      }
    },
    true,
  );
  document.addEventListener(
    "keyup",
    (e) => {
      const grant = e.target && e.target.closest && e.target.closest("[data-grant]");
      if (grant && e.key === " ") {
        e.preventDefault();
        e.stopPropagation();
      }
    },
    true,
  );

  // ---- dialogs (K8) -------------------------------------------------------------------------
  let lastPointer = null;
  document.addEventListener(
    "pointerdown",
    (e) => {
      const el = e.target.closest && e.target.closest("button, a, [tabindex], input, select");
      if (el) lastPointer = el;
    },
    true,
  );
  arr.openDialog = function (id, headingId) {
    const active = document.activeElement;
    const trigger = active && active !== document.body ? active : lastPointer;
    whenEl("#" + CSS.escape(headingId || id), () => {
      const dlg = document.getElementById(id);
      if (!dlg) return;
      if (!dlg.open) {
        dlg._arrTrigger = trigger;
        dlg._arrOpenedAt = performance.now();
        dlg.showModal();
      }
      const h = headingId ? document.getElementById(headingId) : dlg.querySelector("h2");
      if (h) {
        h.setAttribute("tabindex", "-1");
        h.focus();
      }
    });
  };
  arr.closeDialog = function (id) {
    const dlg = document.getElementById(id);
    if (dlg && dlg.open) dlg.close();
  };
  document.addEventListener(
    "close",
    (e) => {
      const dlg = e.target;
      if (!(dlg instanceof HTMLDialogElement) || !dlg.classList.contains("dlg")) return;
      const trigger = dlg._arrTrigger;
      dlg._arrTrigger = null;
      if (trigger && document.contains(trigger)) trigger.focus();
      else {
        const h = document.querySelector("#inhalt h1");
        if (h) {
          h.setAttribute("tabindex", "-1");
          h.focus();
        }
      }
    },
    true,
  );

  // ---- copying refused: the text stands marked, ⌘C is enough (§8.4) ---------------------------
  function copyByHand(text) {
    const box = document.createElement("dialog");
    const title = document.createElement("b");
    title.textContent = "Der Text wurde nicht kopiert.";
    const why = document.createElement("p");
    why.textContent = "Der Browser hat den Zugriff auf die Zwischenablage abgelehnt. Der Text ist markiert: Drücke ⌘C.";
    const area = document.createElement("textarea");
    area.readOnly = true;
    area.rows = 6;
    area.style.width = "min(560px, 80vw)";
    area.value = text;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn";
    close.textContent = "Schließen";
    close.addEventListener("click", () => box.close());
    box.addEventListener("close", () => box.remove());
    box.append(title, why, area, close);
    document.body.appendChild(box);
    box.showModal();
    area.focus();
    area.select();
  }

  // ---- clicks: table rows, copy fields ------------------------------------------------------
  document.addEventListener("click", (e) => {
    const copy = e.target.closest && e.target.closest("[data-copy]");
    if (copy) {
      e.preventDefault();
      const text = copy.getAttribute("data-copy");
      const refused = () => copyByHand(text);
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(() => arr.toast("Kopiert"), refused);
      else refused();
      return;
    }
    const row = e.target.closest && e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a, button, input, select, label, [data-copy]")) {
      location.href = row.getAttribute("data-href");
    }
  });

  // ---- filter controls that change the URL (runs, approvals) --------------------------------
  function navigateWith(name, value) {
    const url = new URL(location.href);
    if (value) url.searchParams.set(name, value);
    else url.searchParams.delete(name);
    url.searchParams.delete("n");
    location.href = url.pathname + url.search;
  }
  document.addEventListener("change", (e) => {
    const sel = e.target.closest && e.target.closest("select[data-param]");
    if (sel) navigateWith(sel.getAttribute("data-param"), sel.value);
  });
  document.addEventListener("submit", (e) => {
    const form = e.target.closest && e.target.closest("form[data-param]");
    if (!form) return;
    e.preventDefault();
    const input = form.querySelector("input");
    navigateWith(form.getAttribute("data-param"), input ? input.value.trim() : "");
  });

  // ---- deadline countdown (minutely, every second in the last 2 min, 08 §5.2) ---------------
  setInterval(() => {
    const now = arr.now();
    document.querySelectorAll("[data-deadline]").forEach((el) => {
      const text = arr.deadlineText(parseFloat(el.getAttribute("data-deadline")), now);
      if (el.textContent !== text) el.textContent = text;
    });
  }, 1000);

  // ---- connection banner (§4.2, §8.4) -------------------------------------------------------
  whenEl("#popup", (popup) => {
    const update = () => {
      const banner = document.querySelector(".conn-banner");
      if (!banner) return;
      const lost = popup.getAttribute("aria-hidden") === "false";
      if (lost && !banner.classList.contains("show")) {
        const since = banner.querySelector("[data-since]");
        if (since) since.textContent = hhmmss(arr.now());
      }
      banner.classList.toggle("show", lost);
    };
    new MutationObserver(update).observe(popup, { attributes: true, attributeFilter: ["aria-hidden"] });
    update();
  });
})();

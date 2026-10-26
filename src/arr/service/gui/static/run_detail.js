// arr GUI – run detail behaviour that must not wait for the server (docs/08-ux-konzept.md §6):
// - "Live folgen": on at the end of a list, off after scrolling up; a channel opens at the
//   separator "neu" with following off; "▼ n neue …" brings you to the end (§6.6)
// - jumps to a message or transcript entry (overview, "Im Transkript zeigen", "↪ zu #5")
// - agent cards compact by space, not by window width: full → two lines → one line (§6.5)
// - finished stages collapse to "✓ 1–3 erledigt" when the progress bar does not fit (K9)
// - running durations ("· 2 min"), "gelesen bis" per channel in localStorage (ui_seen, §5.0)
// - ⌘↩ sends from the input bar; the ⋯ menu closes with Esc or a click outside
(function () {
  "use strict";
  const arr = (window.arr = window.arr || {});
  const rd = (arr.rd = arr.rd || {});
  const END_SLACK = 24; // px before the end that still count as "at the end"
  const now = () => (arr.now ? arr.now() : Date.now() / 1000);

  // ---- "gelesen bis" (ui_seen) ------------------------------------------------------------
  const seenKey = (run) => "arr.seen." + run;
  function loadSeen(run) {
    try {
      return JSON.parse(localStorage.getItem(seenKey(run)) || "null");
    } catch (e) {
      return null;
    }
  }
  rd.markSeen = function (run, channel, id) {
    const state = loadSeen(run) || {};
    state.channels = state.channels || {};
    state.at = state.at || {};
    if ((state.channels[channel] || 0) >= id) return;
    state.channels[channel] = id;
    state.at[channel] = now();
    try {
      localStorage.setItem(seenKey(run), JSON.stringify(state));
    } catch (e) {
      /* private mode: nothing is remembered */
    }
  };
  // The stored state is read once at load, before this page marks anything, and sent to the
  // server when the socket is up. Afterwards the page reports what you actually saw: the last
  // message of the open channel whose top lay in the visible area (W1-07.1).
  let rdRoot = null;
  let seenSent = false;
  const unsent = {};
  function reportSeen(root) {
    const snapshot = loadSeen(root.dataset.rd) || {};
    const limit = Date.now() + 8000;
    const send = () => {
      if (window.socket && window.socket.connected) {
        root.dispatchEvent(new CustomEvent("arrseen", { detail: snapshot }));
        seenSent = true;
        Object.keys(unsent).forEach((ch) => sendRead(ch, unsent[ch]));
      } else if (Date.now() < limit) setTimeout(send, 50);
    };
    send();
  }
  function sendRead(channel, id) {
    if (!seenSent || !rdRoot) {
      unsent[channel] = Math.max(unsent[channel] || 0, id);
      return;
    }
    delete unsent[channel];
    rdRoot.dispatchEvent(new CustomEvent("arrread", { detail: { channel: channel, id: id } }));
  }
  function markVisible(box) {
    // "gelesen bis" moves only as far as you have seen
    if (!rdRoot || document.visibilityState !== "visible") return;
    const rows = box.querySelectorAll(".rd-list [id^='m-']");
    if (!rows.length) return;
    const b = box.getBoundingClientRect();
    let last = 0;
    let channel = null;
    rows.forEach((m) => {
      const r = m.getBoundingClientRect();
      if (r.height > 0 && r.top >= b.top - 1 && r.top < b.bottom - 8) {
        const match = /^m-(.+)-(\d+)$/.exec(m.id);
        if (match && +match[2] > last) {
          channel = match[1];
          last = +match[2];
        }
      }
    });
    if (!channel || box._rdRead >= last) return;
    box._rdRead = last;
    rd.markSeen(rdRoot.dataset.rd, channel, last);
    sendRead(channel, last);
  }

  // ---- URL without reload -------------------------------------------------------------------
  rd.pushUrl = function (query) {
    const url = location.pathname + (query ? "?" + query : "");
    if (url !== location.pathname + location.search) history.pushState({ rd: 1 }, "", url);
  };
  window.addEventListener("popstate", () => location.reload());

  // ---- scrolling views: "Live folgen" (§6.6) ----------------------------------------------
  function settle(go) {
    // layout may still change (fonts, formulas): align a few times unless the user acts
    let acted = false;
    const stop = () => (acted = true);
    ["wheel", "touchstart", "keydown", "mousedown"].forEach((ev) =>
      window.addEventListener(ev, stop, { once: true, capture: true }),
    );
    go();
    requestAnimationFrame(() => acted || go());
    setTimeout(() => acted || go(), 200);
    setTimeout(() => acted || go(), 600);
  }
  function flash(el) {
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 2500);
  }
  function scrollToItem(box, el, above) {
    const top = el.getBoundingClientRect().top - box.getBoundingClientRect().top;
    box._rdAuto = true;
    box.scrollTop += top - above;
    requestAnimationFrame(() => (box._rdAuto = false));
  }
  function setupScroll(box) {
    if (box._rdReady) return;
    box._rdReady = true;
    const view = box.closest(".rd-view") || box.parentElement;
    const jump = view.querySelector(".jump");
    const messages = (box.getAttribute("aria-label") || "").startsWith("Nachrichten");
    let follow = box.dataset.follow === "1";
    let fresh = 0;
    let lastTop = box.scrollTop;
    const atEnd = () => box.scrollTop + box.clientHeight >= box.scrollHeight - END_SLACK;
    const show = () => {
      if (messages) markVisible(box);
      const toggle = view.querySelector(".follow-toggle"); // the head may be re-rendered
      if (toggle) toggle.setAttribute("aria-pressed", follow ? "true" : "false");
      if (!jump) return;
      jump.hidden = follow || (!fresh && atEnd());
      const text = jump.querySelector(".jump-text");
      if (!text) return;
      if (!fresh) text.textContent = "Ans Ende · Live folgen";
      else if (messages) text.textContent = fresh + (fresh === 1 ? " neue Nachricht" : " neue Nachrichten");
      else text.textContent = fresh + (fresh === 1 ? " neues Ereignis" : " neue Ereignisse");
    };
    const toEnd = () => {
      box._rdAuto = true;
      box.scrollTop = box.scrollHeight;
      requestAnimationFrame(() => {
        box.scrollTop = box.scrollHeight;
        box._rdAuto = false;
        lastTop = box.scrollTop;
      });
    };
    box._rdFollowing = () => follow;
    box._rdFollow = (on) => {
      follow = on;
      if (on) {
        fresh = 0;
        toEnd();
      }
      show();
    };
    // only your own scrolling changes "Live folgen"; layout shifts (formulas, fonts) do not
    let userAt = -1e9;
    let dragging = false;
    const mark = () => (userAt = performance.now());
    ["wheel", "touchmove", "keydown"].forEach((ev) => box.addEventListener(ev, mark, { passive: true }));
    box.addEventListener("mousedown", () => (dragging = true));
    window.addEventListener("mouseup", () => (dragging = false));
    box.addEventListener(
      "scroll",
      () => {
        if (box._rdAuto || (!dragging && performance.now() - userAt > 800)) {
          lastTop = box.scrollTop;
          show();
          return;
        }
        if (atEnd()) {
          if (!follow) {
            follow = true;
            fresh = 0;
          }
        } else if (box.scrollTop < lastTop - 2) follow = false;
        lastTop = box.scrollTop;
        show();
      },
      { passive: true },
    );
    const list = box.querySelector(".rd-list");
    if (list) {
      new MutationObserver((mutations) => {
        let added = 0;
        mutations.forEach((m) => m.addedNodes.forEach((n) => n.nodeType === 1 && added++));
        if (!added) return;
        if (follow || box.scrollHeight <= box.clientHeight + END_SLACK) {
          follow = true;
          toEnd();
        } else fresh += added;
        show();
      }).observe(list, { childList: true });
    }
    const target = box.dataset.openAt && document.getElementById(box.dataset.openAt);
    if (target) {
      if (target.tagName === "DETAILS") target.open = true;
      const isSeparator = target.classList.contains("newline");
      // the separator "neu" with one or two older messages above it (§6.5)
      const above = () => {
        if (!isSeparator) return box.clientHeight / 4;
        const row = target.closest(".sep-row") || target;
        let room = 0;
        let prev = row.previousElementSibling;
        for (let i = 0; i < 2 && prev; i++, prev = prev.previousElementSibling) {
          room += prev.getBoundingClientRect().height;
        }
        return Math.min(room, box.clientHeight * 0.35);
      };
      settle(() => scrollToItem(box, target, above()));
      if (!isSeparator) flash(target);
    } else if (follow) settle(toEnd);
    show();
    if (jump) jump.onclick = () => box._rdFollow(true);
  }
  document.addEventListener("visibilitychange", () => {
    const box = currentBox();
    if (box && (box.getAttribute("aria-label") || "").startsWith("Nachrichten")) markVisible(box);
  });
  function currentBox() {
    return document.querySelector(".rd-view .rd-scroll");
  }
  rd.focusView = function (kind, target) {
    // after changing the view (tab, "Im Transkript zeigen", a question): focus its start
    let selector = null;
    if (kind === "ansicht") selector = '.rd-center .tabs a.active[data-sel="ansicht:' + target + '"]';
    else if (kind === "ergebnis") selector = "#e" + String(target).padStart(3, "0") + "-h";
    else if (kind === "seq") selector = "#seq-" + target.split(":").pop();
    else if (kind === "msg") selector = "#m-" + target.replace(/:(\d+)$/, "-$1");
    if (!selector) return;
    const limit = Date.now() + 3000;
    const go = () => {
      const el = document.querySelector(selector);
      if (!el) {
        if (Date.now() < limit) setTimeout(go, 50);
        return;
      }
      const target = el.tagName === "DETAILS" ? el.querySelector("summary") : el;
      if (!target.matches("a[href], button, summary")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
    };
    setTimeout(go, 60);
  };
  rd.toEnd = function () {
    const box = currentBox();
    if (box && box._rdFollow) box._rdFollow(true);
  };
  rd.sent = function () {
    const body = document.getElementById("cmp-body");
    if (body) {
      body.value = "";
      body.focus();
    }
  };

  document.addEventListener("click", (e) => {
    if (!e.target.closest || !e.target.closest(".follow-toggle")) return;
    const box = currentBox();
    if (box && box._rdFollow) box._rdFollow(!box._rdFollowing());
  });

  // ---- keep the focus when a live update redraws a row (W1-07.3) -----------------------------
  // The server replaces the inner HTML of a transcript entry or message when it changes (a tool
  // call finishes). If the focus was inside, the browser drops it to <body>; give it back to
  // the same row (same id; its summary if the row is collapsible).
  let kept = null;
  document.addEventListener("focusin", (e) => {
    const host = e.target.closest && e.target.closest(".rd-view [id^='seq-'], .rd-view [id^='m-']");
    kept = host ? { id: host.id, summary: e.target.tagName === "SUMMARY" } : null;
  });
  document.addEventListener("pointerdown", (e) => {
    // you put the focus elsewhere yourself: nothing to keep
    if (kept && !(e.target.closest && e.target.closest("#" + CSS.escape(kept.id)))) kept = null;
  }, true);
  function restoreFocus() {
    if (!kept) return;
    const active = document.activeElement;
    if (active && active !== document.body && document.contains(active)) return;
    const host = document.getElementById(kept.id);
    if (!host) return;
    // a collapsible row is focused on its summary (the row of a decided tool call, too)
    const summary = host.tagName === "DETAILS" || kept.summary ? host.querySelector("summary") : null;
    const target = summary || host;
    if (!target.matches("a[href], button, summary")) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }

  // ---- jumps inside the shown list --------------------------------------------------------
  document.addEventListener("click", (e) => {
    const link = e.target.closest && e.target.closest("[data-jump], [data-jump-first]");
    if (!link) return;
    const box = currentBox();
    const target = link.dataset.jump
      ? document.getElementById(link.dataset.jump)
      : box && box.querySelector(link.dataset.jumpFirst);
    e.preventDefault();
    if (!box || !target || !box.contains(target)) {
      if (arr.toast) arr.toast("Diese Nachricht liegt vor den geladenen Nachrichten.");
      return;
    }
    if (box._rdFollow) box._rdFollow(false);
    scrollToItem(box, target, box.clientHeight / 4);
    flash(target);
  });

  // ---- agent cards by space (§6.5) and the progress bar (K9) -------------------------------
  const LEVELS = ["lvl-full", "lvl-2", "lvl-1"];
  function compactAgents() {
    const col = document.querySelector(".rd-left");
    if (!col) return;
    for (const level of LEVELS) {
      col.classList.remove(...LEVELS);
      col.classList.add(level);
      if (col.scrollHeight <= col.clientHeight + 1) break;
    }
  }
  function collapseStages() {
    document.querySelectorAll(".stagebar").forEach((bar) => {
      bar.classList.remove("collapsed");
      if (bar.querySelector(".sum") && bar.scrollWidth > bar.clientWidth + 1) bar.classList.add("collapsed");
    });
  }
  let layoutQueued = false;
  function layout() {
    if (layoutQueued) return;
    layoutQueued = true;
    requestAnimationFrame(() => {
      layoutQueued = false;
      compactAgents();
      collapseStages();
    });
  }
  window.addEventListener("resize", layout);

  // ---- quick tooltip of the agent cards (one-line cards show "jetzt" only here, §6.5) -------
  // The native title appears late, never on keyboard focus and not in screenshots: show the
  // same text after a short hover or at once on focus; the title is lent while it shows.
  let tip = null;
  let tipCard = null;
  let tipTimer = null;
  function placeTip(card) {
    const r = card.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    const top = Math.min(Math.max(8, r.top), window.innerHeight - t.height - 8);
    tip.style.left = Math.round(r.right + 8) + "px";
    tip.style.top = Math.round(top) + "px";
  }
  function showTip(card) {
    hideTip();
    const text = card.getAttribute("title") || card.dataset.tip;
    if (!text) return;
    card.dataset.tip = text;
    card.removeAttribute("title");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "rd-tip";
      tip.id = "rd-tip";
      tip.setAttribute("role", "tooltip");
    }
    tip.textContent = text;
    document.body.appendChild(tip);
    placeTip(card);
    card.setAttribute("aria-describedby", "rd-tip");
    tipCard = card;
  }
  function hideTip() {
    clearTimeout(tipTimer);
    if (tip) tip.remove();
    if (tipCard) {
      if (!tipCard.getAttribute("title") && tipCard.dataset.tip) tipCard.setAttribute("title", tipCard.dataset.tip);
      tipCard.removeAttribute("aria-describedby");
      tipCard = null;
    }
  }
  document.addEventListener("mouseover", (e) => {
    const card = e.target.closest && e.target.closest(".rd-left .agent");
    if (!card || card === tipCard) return;
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => showTip(card), 300);
  });
  document.addEventListener("mouseout", (e) => {
    const card = e.target.closest && e.target.closest(".rd-left .agent");
    if (card && !card.contains(e.relatedTarget)) hideTip();
  });
  document.addEventListener("focusin", (e) => {
    const card = e.target.closest && e.target.closest(".rd-left .agent");
    if (card) showTip(card);
    else if (tipCard) hideTip();
  });
  document.addEventListener("scroll", hideTip, true);
  document.addEventListener("keydown", (e) => e.key === "Escape" && hideTip());

  // ---- durations of running tools and turns ------------------------------------------------
  function duration(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s >= 3600) {
      const m = Math.floor(s / 60);
      return Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0") + " h";
    }
    if (s >= 600) return Math.floor(s / 60) + " min";
    if (s >= 60) return Math.floor(s / 60) + " min" + (s % 60 ? " " + (s % 60) + " s" : "");
    return s + " s";
  }
  function tick() {
    const t = now();
    document.querySelectorAll(".rd [data-since]").forEach((el) => {
      const text = duration(t - parseFloat(el.getAttribute("data-since")));
      if (el.textContent !== text) el.textContent = text;
    });
  }
  setInterval(tick, 1000);

  // ---- input bar and menu ------------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.closest && e.target.closest(".composer")) {
      e.preventDefault();
      if (e.repeat) return;
      const send = e.target.closest(".composer").querySelector("[data-send]");
      if (send) send.click();
      return;
    }
    if (e.key === "Escape") {
      const open = document.querySelector("details.menu[open]");
      if (open) {
        open.open = false;
        const summary = open.querySelector("summary");
        if (summary) summary.focus();
      }
    }
  });
  document.addEventListener("click", (e) => {
    document.querySelectorAll("details.menu[open]").forEach((menu) => {
      if (!menu.contains(e.target) || e.target.closest("[role=menuitem]")) menu.open = false;
    });
  });

  // ---- start ------------------------------------------------------------------------------
  function start(root) {
    rdRoot = root;
    reportSeen(root);
    const view = root.querySelector(".rd-view");
    const setup = () => {
      document.querySelectorAll(".rd-view .rd-scroll").forEach(setupScroll);
      tick();
    };
    setup();
    if (view) new MutationObserver(setup).observe(view, { childList: true });
    if (view) new MutationObserver(restoreFocus).observe(view, { childList: true, subtree: true });
    const agents = root.querySelector(".agents");
    if (agents) new MutationObserver(layout).observe(agents, { childList: true });
    const stages = root.querySelector(".stagebar-slot");
    if (stages) new MutationObserver(layout).observe(stages, { childList: true, subtree: true });
    if (window.ResizeObserver) {
      const left = root.querySelector(".rd-left");
      if (left) new ResizeObserver(layout).observe(left);
    }
    layout();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(layout);
  }
  function waitForRoot() {
    const root = document.querySelector("[data-rd]");
    if (root) return start(root);
    const obs = new MutationObserver(() => {
      const found = document.querySelector("[data-rd]");
      if (found) {
        obs.disconnect();
        start(found);
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }
  waitForRoot();
})();

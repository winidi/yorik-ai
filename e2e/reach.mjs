// Phone check: can a finger get to everything on the screen?
//
// Three ways a page fails on a phone without crashing, all found by hand
// on 2026-10-06 (diagnostics question, calendar import, photo viewer,
// timetable):
//
//   unreachable  a control or a piece of text lies above or below the
//                screen, and no scroll container that accepts a finger can
//                bring it in. Yorik's <body> has `touch-action: none`, so
//                the document itself never scrolls by touch: a page needs
//                its own `overflow-y-auto` container.
//   clipped      the same, but the content is cut off by a screen-sized
//                `overflow: hidden` box.
//   covered      a control is on the screen, but something else lies on
//                top of it and takes the tap.
//   misplaced    a full-screen layer (a dialog's `fixed inset-0`) does not
//                fill the screen, because a parent that is moved or
//                transformed holds it: a dialog opened from inside a
//                slid-away drawer shows only an edge.
//
// `reachFindings(page)` looks at the page as it is right now and returns
// [{ kind, what, detail }]. It scrolls only containers a finger could
// scroll, and puts every one back.

export async function reachFindings(page) {
  return page.evaluate(() => {
    const VW = window.innerWidth, VH = window.innerHeight;
    const out = [];
    const seen = new Set();
    const style = (el) => getComputedStyle(el);

    const label = (el) => {
      const own = (el.getAttribute?.("aria-label") || el.getAttribute?.("title") || el.innerText || el.getAttribute?.("placeholder") || el.value || "")
        .toString().replace(/\s+/g, " ").trim().slice(0, 50);
      const cls = (el.className && el.className.toString ? el.className.toString() : "").split(/\s+/).slice(0, 3).join(".");
      return `${el.tagName.toLowerCase()}${own ? ` "${own}"` : ""}${!own && cls ? `.${cls}` : ""}`;
    };

    // The layer the person is looking at: a full-screen fixed overlay if
    // one is open (a dialog), otherwise the page.
    const layer = (() => {
      let el = document.elementFromPoint(VW / 2, VH / 2);
      let found = null;
      while (el && el !== document.body) {
        const s = style(el);
        if (s.position === "fixed") {
          const r = el.getBoundingClientRect();
          if (r.width >= VW * 0.9 && r.height >= VH * 0.9) found = el;
        }
        el = el.parentElement;
      }
      return found || document.body;
    })();

    const visible = (el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return false;
      // A closed <details> keeps the geometry of what it hides.
      const fold = el.closest("details:not([open])");
      if (fold && !el.closest("summary")) return false;
      if (el.checkVisibility && !el.checkVisibility({ contentVisibilityAuto: false })) return false;
      for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
        const s = style(n);
        if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
        if (n.getAttribute("aria-hidden") === "true" || n.inert) return false;
      }
      return true;
    };

    const pannable = (ta) => ta === "auto" || ta === "manipulation" || /pan-y/.test(ta);

    // Scroll containers above `el` that a finger starting on `el` may move.
    // Per the pointer-events spec the touch-action of every element from
    // the touched one up to the scroller counts.
    const fingerScrollers = (el) => {
      const list = [];
      let allowed = true;
      for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
        const s = style(n);
        if (!pannable(s.touchAction)) allowed = false;
        const isRoot = n === document.documentElement;
        const scrolls = isRoot
          ? document.scrollingElement && document.scrollingElement.scrollHeight > VH + 1
          : /(auto|scroll)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 1;
        if (scrolls && allowed) list.push(isRoot ? document.scrollingElement : n);
        if (s.position === "fixed") {
          // A fixed box does not move with anything above it.
          break;
        }
      }
      return list;
    };

    // Is `el` cut off by a box that does not scroll? Looked at after
    // the finger-scrollable containers have done what they can.
    const clipper = (el) => {
      const r = el.getBoundingClientRect();
      for (let n = el.parentElement; n && n !== document.documentElement; n = n.parentElement) {
        const s = style(n);
        if (/(hidden|clip)/.test(s.overflowY)) {
          const c = n.getBoundingClientRect();
          if (r.top >= c.bottom - 1 || r.bottom <= c.top + 1) return n;
        }
        if (s.position === "fixed") break;
      }
      return null;
    };

    // Is `el` off to the side inside a row that scrolls or cuts sideways
    // (a toolbar, a row of chips)? Then a sideways swipe gets to it, or
    // it is truncated on purpose; either way not this check's business.
    const sideways = (el) => {
      const r = el.getBoundingClientRect(), cx = r.left + r.width / 2;
      for (let n = el.parentElement; n && n !== document.documentElement; n = n.parentElement) {
        if (style(n).overflowX === "visible") continue;
        const c = n.getBoundingClientRect();
        if (cx < c.left || cx > c.right) return true;
      }
      return false;
    };

    // Bring `el` towards the middle of the screen using only the given
    // scrollers; returns a function that undoes it.
    const bringIn = (el, scrollers) => {
      const before = scrollers.map((s) => [s, s.scrollTop, s.style.scrollBehavior]);
      for (const s of scrollers) {
        const r = el.getBoundingClientRect();
        const want = r.top + r.height / 2 - VH / 2;
        if (Math.abs(want) < 1) break;
        s.style.scrollBehavior = "auto";
        s.scrollTop += want;
      }
      return () => { for (const [s, top, behaviour] of before) { s.scrollTop = top; s.style.scrollBehavior = behaviour; } };
    };

    // The stacking layer an element lives in: the nearest positioned
    // ancestor with a z-index of its own (a dialog, a drawer, a popover,
    // the dock), or null for the page itself.
    const stack = (el) => {
      for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
        const s = style(n);
        if (s.position !== "static" && s.zIndex !== "auto") return n;
      }
      return null;
    };

    // What takes a tap aimed at `el`: its middle, and a quarter in from
    // either side, so a button half hidden under a floating one counts.
    // Returns the first thing found on top, or null when `el` gets all three.
    const coverer = (el) => {
      const r = el.getBoundingClientRect();
      const cy = r.top + r.height / 2;
      const lab = el.closest("label");
      for (const f of [0.5, 0.25, 0.75]) {
        const cx = Math.min(VW - 1, Math.max(0, r.left + r.width * f));
        const top = document.elementFromPoint(cx, cy);
        if (!top || top === el || el.contains(top) || top.contains(el)) continue;
        const tl = top.closest("label");
        if (lab && tl && lab === tl) continue;
        return top;
      }
      return null;
    };

    // Does it matter that `top` lies on `el`? Yes when both are in the
    // same layer (an image over the arrows of its own viewer), or when
    // `top` is permanent furniture (the dock, a floating button) that the
    // control cannot be scrolled out from under. A popover, a drawer's
    // backdrop, a toast or the first-visit hint over the page is by design.
    const blocks = (el, top) => {
      if (top.closest("[data-app-hint]")) return false;
      if (stack(top) === stack(el)) return true;
      if (top.closest('nav[data-tour="dock"]')) return true;
      // a floating button: the button itself or the box right around it is fixed
      const fab = top.closest("button");
      // ...but not the red pill of a running recording: the person started
      // it and stops it with that very button
      if (fab && /stop recording|aufnahme (beenden|stoppen)/i.test(`${fab.getAttribute("aria-label") || ""} ${fab.title || ""} ${fab.innerText || ""}`)) return false;
      return !!fab && (style(fab).position === "fixed" || (fab.parentElement && style(fab.parentElement).position === "fixed"));
    };

    const add = (kind, el, detail) => {
      const key = `${kind}|${label(el)}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ kind, what: label(el), detail });
    };

    for (const el of document.querySelectorAll("div, section, aside")) {
      const s = style(el);
      if (s.position !== "fixed" || s.top !== "0px" || s.left !== "0px" || s.right !== "0px" || s.bottom !== "0px") continue;
      if (s.display === "none" || s.visibility === "hidden") continue;
      const r = el.getBoundingClientRect();
      if (Math.abs(r.left) > 2 || Math.abs(r.top) > 2 || Math.abs(r.width - VW) > 2 || Math.abs(r.height - VH) > 2)
        add("misplaced", el, `a full-screen layer sits at ${Math.round(r.left)},${Math.round(r.top)} and is ${Math.round(r.width)}x${Math.round(r.height)} on a ${VW}x${VH} screen: a moved parent holds it`);
    }

    const CONTROL = "button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=tab], [role=switch], [role=checkbox]";
    const controls = [...layer.querySelectorAll(CONTROL)].filter((el) => !el.disabled && visible(el));
    // Leaves with their own text, so a page of plain content counts too.
    const texts = [];
    const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t && texts.length < 600; t = walker.nextNode()) {
      if ((t.nodeValue || "").trim().length < 3) continue;
      const el = t.parentElement;
      if (el && !texts.includes(el) && !el.closest(CONTROL) && visible(el)) texts.push(el);
    }

    for (const [el, isControl] of [...controls.map((c) => [c, true]), ...texts.map((t) => [t, false])]) {
      let r = el.getBoundingClientRect();
      // Off to the side: a drawer or a carousel, not this check's business.
      if (r.left + r.width / 2 < 0 || r.left + r.width / 2 > VW) continue;
      // On the screen and takes its tap: nothing to do (the common case).
      let cy = r.top + r.height / 2;
      if (cy >= 0 && cy <= VH && !clipper(el) && !(isControl && coverer(el))) continue;

      const scrollers = fingerScrollers(el);
      const undo = bringIn(el, scrollers);
      r = el.getBoundingClientRect();
      cy = r.top + r.height / 2;
      const clip = clipper(el);
      if (clip) {
        // Small boxes that cut their content (accordions, truncated cards)
        // do it on purpose; a screen-sized one cuts the page off.
        const c = clip.getBoundingClientRect();
        if (clip.clientHeight >= VH * 0.5 && r.top >= c.bottom - 1)
          add("clipped", el, `cut off by ${label(clip)} (overflow hidden, ${Math.round(c.height)}px high)`);
      } else if (cy < 0 || cy > VH) {
        const how = cy > VH ? `${Math.round(r.bottom - VH)}px below the screen` : `${Math.round(-r.top)}px above the screen`;
        add("unreachable", el, `${how}; ${scrollers.length ? "the scroll area ends before it" : "nothing here scrolls by finger"}`);
      } else if (isControl && !sideways(el)) {
        const top = coverer(el);
        if (top && blocks(el, top)) add("covered", el, `${label(top)} lies on top of it`);
      }
      undo();
    }
    return out;
  }).catch(() => []);
}

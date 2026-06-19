// Apple-style minimal list view. Runs inside a sandboxed iframe with the
// `window.yorik` bridge already injected. Self-contained: own CSS, no exports.

(function() {
  // ─── inline styles ────────────────────────────────────────────────────
  const css = `
    .controls { display:flex; justify-content:space-between; align-items:center;
                margin:8px 16px 12px; gap:12px; flex-wrap:wrap; }
    .header { font-size:28px; font-weight:200; letter-spacing:-0.5px; }
    button { background: var(--card); color: var(--text);
             border: 1px solid var(--card-border); border-radius: 8px;
             padding: 6px 10px; font-size: 12px; cursor: pointer; }
    button:hover { background: rgba(129, 140, 248, 0.15); }
    .acal { display:flex; flex-direction:column; gap:12px; padding: 0 16px 24px; }
    .acal-day { display:flex; gap:16px; padding:14px 16px;
                background: rgba(255, 255, 255, 0.04); border-radius: 14px;
                border-left: 3px solid var(--accent); cursor: pointer; }
    .acal-date { min-width:60px; font-size:28px; font-weight:300;
                 text-align:center; color: var(--accent); }
    .acal-date small { display:block; font-size:11px; color: var(--text-dim);
                       text-transform: uppercase; }
    .acal-events { flex:1; display:flex; flex-direction:column; gap:6px; }
    .acal-event { background: rgba(129, 140, 248, 0.12); border-radius:10px;
                  padding:8px 12px; font-size:13px; cursor:pointer; }
    .acal-event.highlight { background: rgba(245, 158, 11, 0.25);
                            outline: 1px solid var(--warn); }
    .acal-event .due { color: var(--text-dim); font-size: 11px; margin-top: 2px; }
    .acal-empty { padding: 40px; text-align:center; color: var(--text-dim); }
  `;
  const styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  const root = document.getElementById("root");

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function fmtMonth(d) {
    return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  }
  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  function render() {
    const { events, opts, highlightIds } = window.yorik;
    const now = new Date();
    const year = opts.year ?? now.getFullYear();
    const monthIdx = (opts.month ?? now.getMonth() + 1) - 1;
    const first = new Date(year, monthIdx, 1);

    const byDay = new Map();
    for (const ev of events) {
      const k = (ev.starts_at || "").slice(0, 10);
      if (!k) continue;
      if (!byDay.has(k)) byDay.set(k, []);
      byDay.get(k).push(ev);
    }
    const sortedDays = [...byDay.keys()].sort();

    const dayBlocks = sortedDays.map(iso => {
      const d = new Date(iso + "T12:00:00");
      const items = byDay.get(iso).sort((a, b) =>
        (a.starts_at || "").localeCompare(b.starts_at || "")
      );
      return `
        <div class="acal-day" data-iso="${iso}">
          <div class="acal-date">${d.getDate()}
            <small>${d.toLocaleDateString(undefined, { weekday: "short" })}</small>
          </div>
          <div class="acal-events">
            ${items.map(e => `
              <div class="acal-event ${highlightIds.has(Number(e.id)) ? "highlight" : ""}" data-eid="${e.id}">
                <strong>${escapeHtml(e.title)}</strong>
                <div class="due">${fmtTime(e.starts_at)}${e.ends_at ? " – " + fmtTime(e.ends_at) : ""}${e.person ? "  ·  " + escapeHtml(e.person) : ""}</div>
              </div>
            `).join("")}
          </div>
        </div>`;
    }).join("");

    root.innerHTML = `
      <div class="controls">
        <div class="header">${fmtMonth(first)}</div>
        <div>
          <button data-nav="prev" title="Previous month">‹</button>
          <button data-nav="today" title="Today">Today</button>
          <button data-nav="next" title="Next month">›</button>
        </div>
      </div>
      <div class="acal">
        ${dayBlocks || `<div class="acal-empty">No events this month.</div>`}
      </div>
    `;

    root.querySelectorAll(".acal-event[data-eid]").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = Number(el.getAttribute("data-eid"));
        const ev = events.find(x => x.id === id);
        if (ev) window.yorik.selectEvent(ev);
      });
    });

    root.querySelectorAll(".acal-day[data-iso]").forEach(el => {
      el.addEventListener("click", (e) => {
        if (e.target.closest(".acal-event")) return;
        const iso = el.getAttribute("data-iso");
        window.yorik.selectDay(iso, byDay.get(iso) || []);
      });
    });

    root.querySelectorAll("[data-nav]").forEach(btn => {
      btn.addEventListener("click", () => {
        const nav = btn.getAttribute("data-nav");
        let m = monthIdx + 1, y = year;
        if (nav === "prev") { m -= 1; if (m === 0) { m = 12; y -= 1; } }
        else if (nav === "next") { m += 1; if (m === 13) { m = 1; y += 1; } }
        else { const n = new Date(); m = n.getMonth() + 1; y = n.getFullYear(); }
        window.yorik.navigate({ month: m, year: y });
      });
    });

    // tell parent how tall we are so the iframe doesn't scroll-on-scroll
    window.yorik.setHeight(Math.max(400, document.body.scrollHeight + 24));
  }

  window.yorik.onUpdate(render);
})();

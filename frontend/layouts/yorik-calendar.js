// Google-calendar-style layout: month grid + week view with hourly grid,
// overlap packing, drag-to-create, now-line, highlight pulse. Runs inside a
// sandboxed iframe with `window.yorik` injected. Self-contained: own CSS,
// no exports.

(function () {
  // ─── inline styles ──────────────────────────────────────────────────────
  const css = `
    /* ── top toolbar ─────────────────────────────────────────────── */
    .controls {
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
      margin: 0 0 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--card-border);
    }
    .ctl-title {
      font-size: 18px; font-weight: 600; color: var(--text);
      flex: 1; letter-spacing: -0.2px;
    }
    .ctl-spacer { flex: 1; }
    .ctl-grp { display: inline-flex; gap: 4px; align-items: center;
               background: var(--card-2); border: 1px solid var(--card-border);
               border-radius: 999px; padding: 3px; }
    .ctl-grp button {
      background: transparent !important; border: none !important;
      padding: 5px 11px; font-size: 12px; color: var(--text-dim);
      border-radius: 999px;
    }
    .ctl-grp button:hover { background: var(--card) !important; color: var(--text); }
    .ctl-grp button.primary { background: var(--accent) !important; color: #0a0d14; font-weight: 600; }
    button.add-event {
      background: var(--accent); color: #0a0d14; border: 1px solid var(--accent);
      border-radius: 999px; padding: 6px 14px; font-size: 12px; font-weight: 600;
      cursor: pointer; font-family: inherit;
    }
    button.add-event:hover { background: var(--accent-2); }
    button { font-family: inherit; }

    /* ── Month grid — pills, not dots ──────────────────────────── */
    .month {
      display: grid; grid-template-columns: repeat(7, 1fr);
      gap: 1px;
      background: var(--card-border);
      border-radius: 12px; overflow: hidden;
      border: 1px solid var(--card-border);
    }
    .weekday {
      background: var(--bg-2);
      padding: 9px 8px;
      font-size: 11px;
      color: var(--text-faint);
      text-align: center;
      font-weight: 500;
      letter-spacing: 0;
    }
    .day {
      background: var(--bg-2);
      padding: 6px 6px 8px;
      min-height: 110px;
      cursor: pointer;
      position: relative;
      display: flex; flex-direction: column; gap: 3px;
      transition: background 100ms ease;
    }
    .day:hover { background: var(--bg-3); }
    .day.other-month .num,
    .day.other-month .ev,
    .day.other-month .more { opacity: 0.32; }
    .day.today .num {
      background: var(--accent);
      color: #0a0d14;
      width: 22px; height: 22px;
      border-radius: 50%;
      display: inline-flex; align-items: center; justify-content: center;
      font-weight: 700;
    }
    .day.highlight {
      background: rgba(245, 158, 11, 0.18);
      outline: 2px solid var(--warn); outline-offset: -2px;
    }
    .day .num {
      font-weight: 500;
      font-size: 13px;
      color: var(--text);
      display: inline-block;
      width: 22px; height: 22px;
      text-align: center; line-height: 22px;
    }
    .ev {
      font-size: 11px; line-height: 1.3;
      padding: 2px 6px;
      border-radius: 4px;
      background: var(--card-2);
      border-left: 3px solid currentColor;
      color: var(--text);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .more {
      font-size: 10px; color: var(--text-faint);
      padding: 0 6px;
      letter-spacing: 0.2px;
    }

    /* ── Week grid ──────────────────────────────────────────────── */
    .gweek {
      display: grid; grid-template-columns: 56px repeat(7, 1fr);
      grid-auto-rows: auto; gap: 0; font-size: 12px;
      border: 1px solid var(--card-border); border-radius: 12px; overflow: hidden;
    }
    .gweek > * { background: var(--bg-2); }
    .gweek-corner { padding: 8px; border-bottom: 1px solid var(--card-border); border-right: 1px solid var(--card-border); }
    .dayhead { text-align: center; padding: 10px 6px; cursor: pointer; border-bottom: 1px solid var(--card-border); border-right: 1px solid var(--card-border); }
    .dayhead:last-child { border-right: none; }
    .dayhead.today .daynum {
      background: var(--accent); color: #0a0d14;
      border-radius: 50%; width: 26px; height: 26px;
      display: inline-flex; align-items: center; justify-content: center;
      font-weight: 700;
    }
    .dayname { color: var(--text-faint); font-size: 10px; font-weight: 500; }
    .daynum { font-size: 17px; font-weight: 500; margin-top: 4px; display: inline-block; min-width: 26px; }
    .allday-label {
      display: flex; align-items: center; justify-content: flex-end;
      padding: 6px 10px; color: var(--text-faint); font-size: 10px;
      border-bottom: 1px solid var(--card-border); border-right: 1px solid var(--card-border);
    }
    .allday-cell { padding: 4px; min-height: 28px; cursor: pointer;
                   border-bottom: 1px solid var(--card-border); border-right: 1px solid var(--card-border); }
    .allday-cell:last-child { border-right: none; }
    .allday-event {
      font-size: 11px; padding: 2px 6px; border-radius: 4px;
      color: #0a0d14; font-weight: 600; margin-bottom: 2px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .hourgutter { position: relative; border-right: 1px solid var(--card-border); }
    .hourlabel {
      position: relative;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      padding: 2px 8px 0;
      text-align: right;
      color: var(--text-faint); font-size: 10px;
      box-sizing: border-box;
    }
    .hourlabel:first-child { border-top: none; }
    .daycol {
      position: relative; cursor: crosshair; user-select: none;
      border-right: 1px solid var(--card-border);
    }
    .daycol:last-child { border-right: none; }
    .hourrow { border-top: 1px solid rgba(255, 255, 255, 0.05); box-sizing: border-box; }
    .hourrow:first-child { border-top: none; }
    .gw-event {
      position: absolute; border-radius: 6px; border-left: 3px solid;
      padding: 4px 6px; overflow: hidden; color: #0a0d14;
      background: rgba(129, 140, 248, 0.85); cursor: pointer; z-index: 2;
      transition: filter 120ms ease, transform 120ms ease;
    }
    .gw-event:hover { filter: brightness(1.08); transform: translateY(-1px); }
    .gw-event.highlight {
      box-shadow: 0 0 0 2px var(--warn), 0 0 18px 4px rgba(245, 158, 11, 0.55);
      z-index: 4; animation: hi-pulse 1.6s ease-out 2;
    }
    @keyframes hi-pulse {
      0%   { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.9); }
      100% { box-shadow: 0 0 0 2px var(--warn), 0 0 24px 8px rgba(245, 158, 11, 0); }
    }
    .gw-event .t { font-size: 12px; font-weight: 600; white-space: nowrap;
                   overflow: hidden; text-overflow: ellipsis; }
    .gw-event .when { font-size: 10px; opacity: 0.85; }
    .gw-now { position: absolute; left: 0; right: 0; height: 2px;
              background: var(--danger); z-index: 3; pointer-events: none; }
    .gw-now::before { content: ""; position: absolute; left: -5px; top: -4px;
                      width: 10px; height: 10px; background: var(--danger);
                      border-radius: 50%; }
    .ghost { position: absolute; left: 2px; right: 2px;
             background: rgba(129, 140, 248, 0.35);
             border: 1px dashed var(--accent); border-radius: 6px;
             z-index: 5; pointer-events: none; }
  `;
  const styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const WEEK_START_HOUR = 6;
  const WEEK_END_HOUR = 23;
  const WEEK_HOUR_PX = 48;

  function hashColor(s) {
    let h = 0;
    for (let i = 0; i < (s || "").length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    const p = ["#818cf8","#22c55e","#f59e0b","#ef4444","#3b82f6","#ec4899","#14b8a6"];
    return p[Math.abs(h) % p.length];
  }
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => (
      { "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]
    ));
  }
  // Layout switcher — only show when the user has multiple bundled layouts
  // available. Yorik posts the list via the bridge; we fall back to the
  // standard pair otherwise.
  function layoutSwitcherHtml() {
    const opts = (window.yorik.opts || {});
    const available = opts.availableLayouts || ["yorik-calendar", "apple"];
    const current = opts.layoutId || "yorik-calendar";
    if (available.length <= 1) return "";
    const labels = { "yorik-calendar": "Yorik", "apple": "Apple" };
    return `<div class="ctl-grp">
      ${available.map(id =>
        `<button data-set-layout="${id}" ${id === current ? 'class="primary"' : ""}>${labels[id] || id}</button>`
      ).join("")}
    </div>`;
  }

  // Wire toolbar buttons that are common to both views.
  function wireToolbar() {
    root.querySelectorAll("[data-new-event]").forEach(btn => {
      btn.addEventListener("click", () => {
        parent.postMessage({ _yorik: 1, type: "new_event" }, "*");
      });
    });
    root.querySelectorAll("[data-set-layout]").forEach(btn => {
      btn.addEventListener("click", () => {
        parent.postMessage({ _yorik: 1, type: "set_layout", layout: btn.getAttribute("data-set-layout") }, "*");
      });
    });
  }

  function isoDay(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }
  function fmtTime(d) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  function startOfWeek(d) {
    const out = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    out.setDate(out.getDate() - ((out.getDay() + 6) % 7));
    return out;
  }

  function eventBlockGeometry(ev, dayStart) {
    const start = new Date(ev.starts_at);
    const end = ev.ends_at ? new Date(ev.ends_at) : new Date(start.getTime() + 3600_000);
    const dayEnd = new Date(dayStart);
    dayEnd.setDate(dayEnd.getDate() + 1);
    const startInDay = new Date(Math.max(start.getTime(), dayStart.getTime()));
    const endInDay = new Date(Math.min(end.getTime(), dayEnd.getTime()));
    if (endInDay <= startInDay) return null;
    const startHourF = startInDay.getHours() + startInDay.getMinutes() / 60;
    const endHourF = endInDay.getHours() + endInDay.getMinutes() / 60;
    const vsStart = Math.max(startHourF, WEEK_START_HOUR);
    const vsEnd = Math.min(endHourF === 0 ? WEEK_END_HOUR : endHourF, WEEK_END_HOUR);
    if (vsEnd <= vsStart) return null;
    return {
      top: (vsStart - WEEK_START_HOUR) * WEEK_HOUR_PX,
      height: Math.max(18, (vsEnd - vsStart) * WEEK_HOUR_PX - 2),
    };
  }

  function packDayEvents(eventsForDay) {
    const sorted = [...eventsForDay].sort((a, b) => (a.starts_at || "").localeCompare(b.starts_at || ""));
    const columns = [];
    const placement = new Map();
    for (const ev of sorted) {
      const start = new Date(ev.starts_at).getTime();
      let placed = false;
      for (let c = 0; c < columns.length; c++) {
        const last = columns[c][columns[c].length - 1];
        const lastEnd = (last.ends_at ? new Date(last.ends_at) : new Date(new Date(last.starts_at).getTime() + 3600_000)).getTime();
        if (lastEnd <= start) {
          columns[c].push(ev);
          placement.set(ev, c);
          placed = true;
          break;
        }
      }
      if (!placed) {
        columns.push([ev]);
        placement.set(ev, columns.length - 1);
      }
    }
    return { columns: Math.max(1, columns.length), placement };
  }

  const root = document.getElementById("root");

  function renderMonth() {
    const { events, opts, highlightIds } = window.yorik;
    const now = new Date();
    const year = opts.year ?? now.getFullYear();
    const monthIdx = (opts.month ?? now.getMonth() + 1) - 1;
    const first = new Date(year, monthIdx, 1);
    const offset = (first.getDay() + 6) % 7;
    const gridStart = new Date(first);
    gridStart.setDate(first.getDate() - offset);

    const byDay = new Map();
    for (const ev of events) {
      const k = (ev.starts_at || "").slice(0, 10);
      if (!k) continue;
      if (!byDay.has(k)) byDay.set(k, []);
      byDay.get(k).push(ev);
    }

    const monthLabel = first.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    const todayIso = isoDay(new Date());

    root.innerHTML = `
      <div class="controls">
        <div class="ctl-title">${monthLabel}</div>
        <div class="ctl-grp">
          <button data-nav="prev" title="Previous month">‹</button>
          <button data-nav="today" title="Today">Today</button>
          <button data-nav="next" title="Next month">›</button>
        </div>
        <div class="ctl-grp">
          <button data-view="month" class="primary">Month</button>
          <button data-view="week">Week</button>
        </div>
        ${layoutSwitcherHtml()}
        <button class="add-event" data-new-event>+ Event</button>
      </div>
      <div class="month">
        ${WEEKDAYS.map(d => `<div class="weekday">${d}</div>`).join("")}
        ${Array.from({ length: 42 }).map((_, i) => {
          const d = new Date(gridStart);
          d.setDate(gridStart.getDate() + i);
          const iso = isoDay(d);
          const inMonth = d.getMonth() === monthIdx;
          const today = todayIso === iso;
          const evs = byDay.get(iso) || [];
          const hasHi = evs.some(e => highlightIds.has(Number(e.id)));
          const visible = evs.slice(0, 3);
          const extra = evs.length - visible.length;
          const pills = visible.map(e => {
            const c = e.color || hashColor(e.title);
            return `<div class="ev" style="color:${c}" title="${escapeHtml(e.title)}">${escapeHtml(e.title)}</div>`;
          }).join("");
          const more = extra > 0 ? `<div class="more">+${extra} more</div>` : "";
          return `
            <div class="day ${inMonth ? "" : "other-month"} ${today ? "today" : ""} ${hasHi ? "highlight" : ""}" data-iso="${iso}">
              <div class="num">${d.getDate()}</div>
              ${pills}
              ${more}
            </div>`;
        }).join("")}
      </div>
    `;

    root.querySelectorAll(".day").forEach(el => {
      el.addEventListener("click", () => {
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

    root.querySelectorAll("[data-view]").forEach(btn => {
      btn.addEventListener("click", () => window.yorik.setView(btn.getAttribute("data-view")));
    });
    wireToolbar();

    window.yorik.setHeight(Math.max(400, document.body.scrollHeight + 16));
  }

  function renderWeek() {
    const { events, opts, highlightIds } = window.yorik;
    const now = new Date();
    const anchorDate = opts.anchorIso ? new Date(opts.anchorIso + "T12:00:00") : now;
    const weekStart = startOfWeek(anchorDate);
    const days = Array.from({ length: 7 }, (_, i) => {
      const d = new Date(weekStart);
      d.setDate(weekStart.getDate() + i);
      return d;
    });
    const weekEnd = new Date(weekStart);
    weekEnd.setDate(weekStart.getDate() + 6);

    const byDay = new Map(days.map(d => [isoDay(d), []]));
    const allDayByDay = new Map(days.map(d => [isoDay(d), []]));
    for (const ev of events) {
      const k = (ev.starts_at || "").slice(0, 10);
      if (!byDay.has(k)) continue;
      (ev.all_day ? allDayByDay : byDay).get(k).push(ev);
    }

    const headerLabel = `${weekStart.toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${weekEnd.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;
    const hours = [];
    for (let h = WEEK_START_HOUR; h < WEEK_END_HOUR; h++) {
      hours.push({ h, label: new Date(2020, 0, 1, h).toLocaleTimeString(undefined, { hour: "numeric" }) });
    }
    const bodyHeight = (WEEK_END_HOUR - WEEK_START_HOUR) * WEEK_HOUR_PX;

    const todayIso = isoDay(new Date());
    const nowInWeek = days.some(d => isoDay(d) === todayIso);
    const nowHourF = now.getHours() + now.getMinutes() / 60;
    const nowTop = nowInWeek && nowHourF >= WEEK_START_HOUR && nowHourF < WEEK_END_HOUR
      ? (nowHourF - WEEK_START_HOUR) * WEEK_HOUR_PX : null;
    const nowColIdx = nowInWeek ? days.findIndex(d => isoDay(d) === todayIso) : -1;

    root.innerHTML = `
      <div class="controls">
        <div class="ctl-title">${headerLabel}</div>
        <div class="ctl-grp">
          <button data-wnav="prev" title="Previous week">‹</button>
          <button data-wnav="today" title="This week">Today</button>
          <button data-wnav="next" title="Next week">›</button>
        </div>
        <div class="ctl-grp">
          <button data-view="month">Month</button>
          <button data-view="week" class="primary">Week</button>
        </div>
        ${layoutSwitcherHtml()}
        <button class="add-event" data-new-event>+ Event</button>
      </div>
      <div class="gweek">
        <div class="gweek-corner"></div>
        ${days.map(d => `
          <div class="dayhead ${isoDay(d) === todayIso ? "today" : ""}" data-iso="${isoDay(d)}">
            <div class="dayname">${d.toLocaleDateString(undefined, { weekday: "short" })}</div>
            <div class="daynum">${d.getDate()}</div>
          </div>
        `).join("")}
        <div class="allday-label">all-day</div>
        ${days.map(d => {
          const iso = isoDay(d);
          const items = allDayByDay.get(iso) || [];
          return `<div class="allday-cell" data-iso="${iso}">
            ${items.map(e => `<div class="allday-event" style="background:${e.color || hashColor(e.title)}">${escapeHtml(e.title)}</div>`).join("")}
          </div>`;
        }).join("")}
        <div class="hourgutter" style="height:${bodyHeight}px">
          ${hours.map(({ label }) => `<div class="hourlabel" style="height:${WEEK_HOUR_PX}px">${label}</div>`).join("")}
        </div>
        ${days.map((d, colIdx) => {
          const iso = isoDay(d);
          const items = byDay.get(iso) || [];
          const { columns, placement } = packDayEvents(items);
          const blocks = items.map(ev => {
            const geom = eventBlockGeometry(ev, d);
            if (!geom) return "";
            const col = placement.get(ev);
            const widthPct = 100 / columns;
            const leftPct = widthPct * col;
            const color = ev.color || hashColor(ev.title);
            const start = new Date(ev.starts_at);
            const end = ev.ends_at ? new Date(ev.ends_at) : new Date(start.getTime() + 3600_000);
            const isHi = highlightIds.has(Number(ev.id));
            return `<div class="gw-event ${isHi ? "highlight" : ""}"
                style="top:${geom.top}px; height:${geom.height}px;
                       left:calc(${leftPct}% + 2px); width:calc(${widthPct}% - 4px);
                       background:${color}; border-left-color:${color}"
                data-eid="${ev.id}"
                title="${escapeHtml(ev.title)} · ${fmtTime(start)}–${fmtTime(end)}">
              <div class="t">${escapeHtml(ev.title)}</div>
              <div class="when">${fmtTime(start)}–${fmtTime(end)}</div>
            </div>`;
          }).join("");
          const nowLine = (colIdx === nowColIdx && nowTop !== null)
            ? `<div class="gw-now" style="top:${nowTop}px"></div>` : "";
          return `<div class="daycol" data-iso="${iso}" style="height:${bodyHeight}px">
            ${hours.map(() => `<div class="hourrow" style="height:${WEEK_HOUR_PX}px"></div>`).join("")}
            ${blocks}
            ${nowLine}
          </div>`;
        }).join("")}
      </div>
    `;

    // Drag-to-create on day columns
    const SNAP_PX = WEEK_HOUR_PX * (15 / 60);
    function pxToIso(iso, yPx) {
      const snapped = Math.max(0, Math.round(yPx / SNAP_PX) * SNAP_PX);
      const minutes = (snapped / WEEK_HOUR_PX) * 60;
      const h = WEEK_START_HOUR + Math.floor(minutes / 60);
      const m = Math.round(minutes % 60);
      return `${iso}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00`;
    }
    root.querySelectorAll(".daycol").forEach(col => {
      let dragging = null;
      col.addEventListener("mousedown", (e) => {
        if (e.target.closest(".gw-event")) return;
        if (e.button !== 0) return;
        e.preventDefault();
        const rect = col.getBoundingClientRect();
        const iso = col.getAttribute("data-iso");
        const startY = e.clientY - rect.top;
        const ghost = document.createElement("div");
        ghost.className = "ghost";
        ghost.style.top = `${Math.round(startY / SNAP_PX) * SNAP_PX}px`;
        ghost.style.height = `${SNAP_PX * 2}px`;
        col.appendChild(ghost);
        dragging = { iso, rect, startY, ghost, col };
        const onMove = (ev) => {
          if (!dragging) return;
          const y = Math.max(0, Math.min(bodyHeight, ev.clientY - dragging.rect.top));
          const a = Math.round(dragging.startY / SNAP_PX) * SNAP_PX;
          const b = Math.round(y / SNAP_PX) * SNAP_PX;
          dragging.ghost.style.top = `${Math.min(a, b)}px`;
          dragging.ghost.style.height = `${Math.max(SNAP_PX, Math.abs(b - a))}px`;
        };
        const onUp = (ev) => {
          if (!dragging) return;
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          const rectNow = dragging.col.getBoundingClientRect();
          const y = Math.max(0, Math.min(bodyHeight, ev.clientY - rectNow.top));
          const a = Math.round(dragging.startY / SNAP_PX) * SNAP_PX;
          const b = Math.round(y / SNAP_PX) * SNAP_PX;
          const top = Math.min(a, b);
          let height = Math.abs(b - a);
          if (height < SNAP_PX) height = SNAP_PX * 2;
          const iso = dragging.iso;
          const starts = pxToIso(iso, top);
          const ends = pxToIso(iso, top + height);
          dragging.ghost.remove();
          dragging = null;
          window.yorik.selectSlot(iso, starts, ends);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
    });

    root.querySelectorAll(".allday-cell, .dayhead").forEach(el => {
      el.addEventListener("click", (e) => {
        if (e.target.closest(".gw-event")) return;
        const iso = el.getAttribute("data-iso");
        const evs = [...(byDay.get(iso) || []), ...(allDayByDay.get(iso) || [])];
        window.yorik.selectDay(iso, evs);
      });
    });

    root.querySelectorAll(".gw-event").forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = Number(el.getAttribute("data-eid"));
        const ev = events.find(x => x.id === id);
        if (ev) window.yorik.selectEvent(ev);
      });
    });

    root.querySelectorAll("[data-wnav]").forEach(btn => {
      btn.addEventListener("click", () => {
        const nav = btn.getAttribute("data-wnav");
        let anchor;
        if (nav === "prev") { anchor = new Date(weekStart); anchor.setDate(anchor.getDate() - 7); }
        else if (nav === "next") { anchor = new Date(weekStart); anchor.setDate(anchor.getDate() + 7); }
        else { anchor = new Date(); }
        window.yorik.navigate({ month: anchor.getMonth() + 1, year: anchor.getFullYear(), anchorIso: isoDay(anchor) });
      });
    });

    root.querySelectorAll("[data-view]").forEach(btn => {
      btn.addEventListener("click", () => window.yorik.setView(btn.getAttribute("data-view")));
    });
    wireToolbar();

    window.yorik.setHeight(Math.max(500, bodyHeight + 160));
  }

  function render() {
    const view = window.yorik.opts.view || "month";
    if (view === "week") renderWeek(); else renderMonth();
  }

  window.yorik.onUpdate(render);
})();

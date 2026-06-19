// Notes — a reference Yorik app, designed to feel like a piece of
// considered writing software (Bear / Apple Notes / Things lineage).
//
// Runs in a sandboxed iframe; calls operations via window.yorik.callOperation.
// No build step, no imports, no external resources. All CSS is injected
// at runtime; all DOM is created via innerHTML.

(function () {
  var root = document.getElementById("app") || document.body;

  if (!window.yorik) {
    root.innerHTML =
      '<div style="padding:32px;font:14px/1.6 system-ui;color:#7a1f1f;">' +
      "This app must run inside the Yorik shell." +
      "</div>";
    return;
  }

  /* ─────────────────────────  STYLES  ───────────────────────── */

  var styles = document.createElement("style");
  styles.textContent = [
    ":root {",
    // Tokens — pulled from host with a refined paper-feel fallback
    "  --bg:         var(--yorik-bg,        #fbf8f4);",
    "  --fg:         var(--yorik-fg,        #1f1c19);",
    "  --fg-muted:   var(--yorik-fg-muted,  #8a847d);",
    "  --card:       var(--yorik-card,      #ffffff);",
    "  --border:     var(--yorik-border,    #e8e3d9);",
    "  --accent:     var(--yorik-accent,    #2c5f5d);",
    "  --accent-fg:  var(--yorik-accent-fg, #ffffff);",
    "  --radius:     var(--yorik-radius,    8px);",
    // Internal palette — soft, low-saturation mood cues
    "  --m-happy:      hsl(36 64% 56%);",
    "  --m-excited:    hsl(12 70% 60%);",
    "  --m-calm:       hsl(176 36% 48%);",
    "  --m-neutral:    hsl(218 10% 62%);",
    "  --m-sad:        hsl(214 28% 50%);",
    "  --m-anxious:    hsl(338 32% 60%);",
    "  --m-grateful:   hsl(46 56% 54%);",
    "  --m-tired:      hsl(258 18% 56%);",
    "  --m-frustrated: hsl(2 46% 56%);",
    // Type
    "  --serif: ui-serif, 'Iowan Old Style', 'Charter', 'Apple Garamond', Palatino, 'Times New Roman', serif;",
    "  --sans:  ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, system-ui, sans-serif;",
    "  --mono:  ui-monospace, 'SF Mono', Menlo, 'Cascadia Code', Consolas, monospace;",
    "  color-scheme: light dark;",
    "}",
    // Reset
    "*, *::before, *::after { box-sizing: border-box; }",
    "html, body { height: 100%; margin: 0; }",
    "body { font-family: var(--sans); color: var(--fg); background: var(--bg);" +
      " -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }",
    "button { font: inherit; color: inherit; background: transparent; border: 0;" +
      " cursor: pointer; padding: 0; }",
    "input, textarea {" +
      " font: inherit; color: inherit; background: transparent;" +
      " border: 0; padding: 0; outline: none; }",
    ":focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }",
    "::selection { background: color-mix(in oklab, var(--accent) 28%, transparent); }",
    // Layout shell
    ".app {" +
      " display: grid; grid-template-columns: 340px 1fr;" +
      " height: 100vh; overflow: hidden; }",
    // Sidebar
    ".sidebar {" +
      " background: color-mix(in oklab, var(--bg) 82%, var(--fg) 5%);" +
      " border-right: 1px solid var(--border);" +
      " display: flex; flex-direction: column; min-width: 0; }",
    ".sb-header {" +
      " padding: 22px 20px 12px;" +
      " display: flex; align-items: center; justify-content: space-between; gap: 10px; }",
    ".sb-title {" +
      " font-family: var(--serif); font-size: 26px; font-weight: 600;" +
      " letter-spacing: -0.012em; margin: 0; line-height: 1; }",
    ".sb-new {" +
      " width: 30px; height: 30px; border-radius: 50%;" +
      " background: var(--accent); color: var(--accent-fg);" +
      " display: inline-flex; align-items: center; justify-content: center;" +
      " font-size: 20px; line-height: 1; font-weight: 300;" +
      " transition: transform 140ms cubic-bezier(0.2,0.7,0.2,1.4), box-shadow 140ms ease;" +
      " box-shadow: 0 1px 2px color-mix(in oklab, var(--fg) 18%, transparent); }",
    ".sb-new:hover { transform: scale(1.08);" +
      " box-shadow: 0 2px 6px color-mix(in oklab, var(--fg) 22%, transparent); }",
    ".sb-new:active { transform: scale(0.96); }",
    ".sb-search-wrap { padding: 4px 14px 8px; position: relative; }",
    ".sb-search {" +
      " width: 100%; padding: 8px 12px 8px 30px;" +
      " background: var(--card); border: 1px solid var(--border);" +
      " border-radius: calc(var(--radius) - 2px);" +
      " font-size: 13px;" +
      " transition: border-color 140ms ease, background 140ms ease," +
      "             box-shadow 140ms ease; }",
    ".sb-search::placeholder { color: var(--fg-muted); opacity: 0.7; }",
    ".sb-search:focus {" +
      " border-color: color-mix(in oklab, var(--accent) 50%, var(--border));" +
      " box-shadow: 0 0 0 3px color-mix(in oklab, var(--accent) 14%, transparent); }",
    ".sb-search-icon {" +
      " position: absolute; left: 24px; top: 50%; transform: translateY(-50%);" +
      " width: 12px; height: 12px; border: 1.4px solid var(--fg-muted);" +
      " border-radius: 50%; opacity: 0.6; pointer-events: none; }",
    ".sb-search-icon::after {" +
      " content: ''; position: absolute; right: -4px; bottom: -3px;" +
      " width: 5px; height: 1.4px; background: var(--fg-muted);" +
      " transform: rotate(45deg); transform-origin: left center; }",
    ".sb-actions { padding: 2px 14px 6px; display: flex; flex-direction: column; gap: 2px; }",
    ".sb-action {" +
      " width: 100%; text-align: left;" +
      " padding: 8px 10px; border-radius: calc(var(--radius) - 2px);" +
      " font-size: 12.5px; color: var(--fg-muted);" +
      " display: flex; align-items: center; gap: 8px;" +
      " transition: background 120ms ease, color 120ms ease; }",
    ".sb-action:hover { background: color-mix(in oklab, var(--card) 84%, var(--fg) 8%); color: var(--fg); }",
    ".sb-action.active {" +
      " background: color-mix(in oklab, var(--card) 78%, var(--accent) 14%);" +
      " color: var(--fg); }",
    ".sb-action .dot { width: 6px; height: 6px; border-radius: 50%;" +
      " background: var(--accent); opacity: 0.8; flex: none; }",
    // Sidebar list
    ".sb-list { flex: 1; overflow-y: auto; padding: 6px 0 16px;" +
      " scrollbar-width: thin;" +
      " scrollbar-color: color-mix(in oklab, var(--fg) 18%, transparent) transparent; }",
    ".sb-list::-webkit-scrollbar { width: 10px; }",
    ".sb-list::-webkit-scrollbar-thumb {" +
      " background: color-mix(in oklab, var(--fg) 16%, transparent);" +
      " border: 3px solid transparent; background-clip: padding-box; border-radius: 6px; }",
    ".sb-section {" +
      " font-family: var(--sans); font-size: 10.5px; font-weight: 600;" +
      " letter-spacing: 0.09em; text-transform: uppercase;" +
      " color: var(--fg-muted);" +
      " padding: 16px 20px 6px; }",
    ".sb-item {" +
      " position: relative; cursor: pointer; user-select: none;" +
      " padding: 10px 16px 12px 24px;" +
      " transition: background 100ms ease;" +
      " border-bottom: 1px solid color-mix(in oklab, var(--border) 55%, transparent); }",
    ".sb-item:last-child { border-bottom: 0; }",
    ".sb-item:hover { background: color-mix(in oklab, var(--card) 84%, var(--fg) 6%); }",
    ".sb-item.active {" +
      " background: color-mix(in oklab, var(--card) 76%, var(--accent) 12%); }",
    ".sb-item::before {" +
      " content: ''; position: absolute; left: 12px; top: 14px; bottom: 14px;" +
      " width: 2.5px; border-radius: 2px;" +
      " background: var(--mood-color, var(--m-neutral));" +
      " opacity: 0.78; }",
    ".sb-item-title {" +
      " font-size: 14px; font-weight: 500; line-height: 1.35;" +
      " color: var(--fg); margin: 0 0 3px;" +
      " display: -webkit-box; -webkit-line-clamp: 1;" +
      " -webkit-box-orient: vertical; overflow: hidden; }",
    ".sb-item-preview {" +
      " font-size: 12.5px; line-height: 1.45;" +
      " color: var(--fg-muted);" +
      " display: -webkit-box; -webkit-line-clamp: 2;" +
      " -webkit-box-orient: vertical; overflow: hidden; }",
    ".sb-item-time {" +
      " font-family: var(--mono); font-size: 10.5px;" +
      " color: var(--fg-muted); opacity: 0.7;" +
      " letter-spacing: 0.02em; margin-top: 7px; }",
    // Skeleton loading
    ".sb-skel { padding: 12px 20px; }",
    ".sb-skel-row {" +
      " height: 11px; border-radius: 4px; margin: 8px 0;" +
      " background: color-mix(in oklab, var(--card) 60%, var(--border) 40%);" +
      " animation: shimmer 1.8s ease-in-out infinite; }",
    ".sb-skel-row.s2 { width: 78%; }",
    ".sb-skel-row.s3 { width: 52%; }",
    "@keyframes shimmer { 0%,100% { opacity: 0.55; } 50% { opacity: 1; } }",
    // Empty state in sidebar
    ".sb-empty {" +
      " padding: 30px 24px; text-align: center;" +
      " font-size: 13px; color: var(--fg-muted); line-height: 1.5; }",
    // Main pane
    ".pane { background: var(--bg); overflow-y: auto; position: relative; }",
    ".pane::-webkit-scrollbar { width: 10px; }",
    ".pane::-webkit-scrollbar-thumb {" +
      " background: color-mix(in oklab, var(--fg) 12%, transparent);" +
      " border: 3px solid transparent; background-clip: padding-box; border-radius: 6px; }",
    ".pane-inner {" +
      " max-width: 680px; margin: 0 auto; padding: 56px 56px 96px;" +
      " animation: rise 280ms cubic-bezier(0.2, 0.7, 0.2, 1) both; }",
    "@keyframes rise { from { opacity: 0; transform: translateY(6px); }" +
      " to { opacity: 1; transform: translateY(0); } }",
    ".pane-empty {" +
      " height: 100%; display: flex; flex-direction: column;" +
      " align-items: center; justify-content: center;" +
      " padding: 40px; text-align: center; }",
    ".pane-empty-text {" +
      " font-family: var(--serif); font-size: 18px; font-style: italic;" +
      " color: var(--fg-muted); margin: 0; }",
    ".pane-empty-hint {" +
      " font-family: var(--sans); font-size: 12.5px;" +
      " color: var(--fg-muted); opacity: 0.7;" +
      " margin: 14px 0 0; letter-spacing: 0.01em; }",
    // Meta header in reader / compose / summary
    ".meta {" +
      " font-family: var(--mono); font-size: 11.5px;" +
      " color: var(--fg-muted); letter-spacing: 0.04em;" +
      " display: flex; align-items: center; flex-wrap: wrap; gap: 10px;" +
      " margin: 0 0 6px; }",
    ".meta-sep { opacity: 0.5; }",
    ".mood-pill {" +
      " display: inline-flex; align-items: center; gap: 6px;" +
      " font-family: var(--sans); font-size: 11px; letter-spacing: 0;" +
      " padding: 3px 9px 3px 8px; border-radius: 999px;" +
      " background: color-mix(in oklab, var(--card) 60%, var(--mood-color) 16%);" +
      " color: var(--fg); text-transform: lowercase; }",
    ".mood-pill::before {" +
      " content: ''; width: 6px; height: 6px; border-radius: 50%;" +
      " background: var(--mood-color); }",
    // Reader body
    ".read-body {" +
      " font-family: var(--serif); font-size: 18px; line-height: 1.72;" +
      " color: var(--fg); white-space: pre-wrap; word-wrap: break-word;" +
      " margin: 16px 0 0; letter-spacing: 0.005em; }",
    // Compose
    ".compose-form {" +
      " display: flex; flex-direction: column; min-height: calc(100vh - 80px); }",
    ".compose-text {" +
      " flex: 1; width: 100%; min-height: 55vh;" +
      " resize: none;" +
      " font-family: var(--serif); font-size: 18px;" +
      " line-height: 1.72; letter-spacing: 0.005em;" +
      " color: var(--fg); background: transparent;" +
      " padding: 0; margin: 16px 0 0; }",
    ".compose-text::placeholder {" +
      " color: var(--fg-muted); opacity: 0.5; font-style: italic; }",
    ".compose-bar {" +
      " position: sticky; bottom: 0; margin-top: 16px;" +
      " padding: 16px 0 8px;" +
      " display: flex; align-items: center; gap: 14px;" +
      " background: linear-gradient(to top, var(--bg) 75%, transparent); }",
    ".compose-save {" +
      " padding: 9px 18px; background: var(--accent); color: var(--accent-fg);" +
      " border-radius: calc(var(--radius) - 2px);" +
      " font-size: 13px; font-weight: 500; letter-spacing: 0.015em;" +
      " transition: transform 120ms ease, box-shadow 140ms ease, opacity 140ms ease;" +
      " box-shadow: 0 1px 2px color-mix(in oklab, var(--fg) 16%, transparent); }",
    ".compose-save:hover {" +
      " box-shadow: 0 2px 8px color-mix(in oklab, var(--fg) 22%, transparent); }",
    ".compose-save:active { transform: translateY(1px); }",
    ".compose-save:disabled { opacity: 0.55; cursor: progress; box-shadow: none; }",
    ".compose-hint, .compose-count {" +
      " font-family: var(--mono); font-size: 11px;" +
      " color: var(--fg-muted); letter-spacing: 0.02em; }",
    ".compose-count { margin-left: auto; }",
    ".compose-error {" +
      " color: hsl(0 56% 50%); font-size: 12.5px;" +
      " font-family: var(--sans); letter-spacing: 0; }",
    // Summary
    ".summary-body {" +
      " font-family: var(--serif); font-size: 18px; line-height: 1.72;" +
      " color: var(--fg); margin: 16px 0 0; letter-spacing: 0.005em;" +
      " padding-left: 18px;" +
      " border-left: 2px solid color-mix(in oklab, var(--accent) 50%, var(--border)); }",
    ".summary-foot {" +
      " font-family: var(--mono); font-size: 11px;" +
      " color: var(--fg-muted); margin: 28px 0 0;" +
      " padding-top: 16px; border-top: 1px solid var(--border);" +
      " display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }",
    ".summary-refresh {" +
      " font-family: var(--sans); font-size: 12px; color: var(--accent);" +
      " padding: 5px 11px; border-radius: 999px;" +
      " border: 1px solid color-mix(in oklab, var(--accent) 28%, var(--border));" +
      " transition: background 120ms ease; }",
    ".summary-refresh:hover { background: color-mix(in oklab, var(--accent) 8%, transparent); }",
    // Back button (mobile)
    ".back-btn {" +
      " display: none; align-items: center; gap: 6px;" +
      " margin: 0 0 14px -8px; padding: 4px 10px;" +
      " font-size: 12.5px; color: var(--fg-muted);" +
      " border-radius: 4px;" +
      " transition: background 100ms ease, color 100ms ease; }",
    ".back-btn:hover {" +
      " background: color-mix(in oklab, var(--card) 84%, var(--fg) 6%);" +
      " color: var(--fg); }",
    ".back-btn .chev {" +
      " display: inline-block; width: 6px; height: 6px;" +
      " border-left: 1.6px solid currentColor;" +
      " border-bottom: 1.6px solid currentColor;" +
      " transform: rotate(45deg); margin-right: 3px; }",
    // Mobile
    "@media (max-width: 720px) {",
    "  .app { grid-template-columns: 1fr; }",
    "  .pane { display: none; }",
    "  .app.detail .sidebar { display: none; }",
    "  .app.detail .pane { display: block; }",
    "  .back-btn { display: inline-flex; }",
    "  .pane-inner { padding: 26px 22px 80px; }",
    "  .sb-header { padding: 18px 18px 10px; }",
    "}",
    // Honor reduced motion
    "@media (prefers-reduced-motion: reduce) {",
    "  *, *::before, *::after {" +
    "    animation-duration: 0.01ms !important;" +
    "    transition-duration: 0.01ms !important; }",
    "}",
  ].join("\n");
  document.head.appendChild(styles);

  /* ─────────────────────────  STATE  ───────────────────────── */

  var state = {
    notes: [],
    loading: true,
    error: null,
    selectedId: null,       // number | "new" | "summary" | null
    search: "",
    draft: "",
    saving: false,
    saveError: null,
    summary: null,          // { summary, note_count, loadedAt }
    summarizing: false,
    summaryError: null,
    mobileDetail: false,
  };

  /* ─────────────────────────  HELPERS  ───────────────────────── */

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function parseDate(iso) {
    if (!iso) return null;
    // SQLite stamp: "2026-06-08 12:27:11" (UTC, no Z)
    var d = new Date(String(iso).replace(" ", "T") + "Z");
    return isNaN(d.getTime()) ? null : d;
  }

  function startOfDay(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function formatListTime(date) {
    if (!date) return "";
    var today = startOfDay(new Date());
    var yesterday = new Date(today.getTime() - 86400000);
    var weekAgo = new Date(today.getTime() - 6 * 86400000);
    if (date >= today) {
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    if (date >= yesterday) return "Yesterday";
    if (date >= weekAgo) return date.toLocaleDateString([], { weekday: "short" });
    return date.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function formatFullStamp(date) {
    if (!date) return "";
    var dateStr = date.toLocaleDateString([], {
      weekday: "long", month: "long", day: "numeric",
    });
    var timeStr = date.toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit",
    });
    return dateStr + " · " + timeStr;
  }

  function bucketLabel(date) {
    if (!date) return "Undated";
    var today = startOfDay(new Date());
    var yesterday = new Date(today.getTime() - 86400000);
    var weekAgo = new Date(today.getTime() - 6 * 86400000);
    var monthAgo = new Date(today.getTime() - 30 * 86400000);
    if (date >= today) return "Today";
    if (date >= yesterday) return "Yesterday";
    if (date >= weekAgo) return "This Week";
    if (date >= monthAgo) return "This Month";
    return "Earlier";
  }

  var SECTION_ORDER = ["Today", "Yesterday", "This Week", "This Month", "Earlier", "Undated"];

  function firstLine(body) {
    var first = (body || "").split(/\r?\n/)[0].trim();
    return first || "Empty note";
  }

  function previewLines(body) {
    var lines = (body || "").split(/\r?\n/);
    return lines.slice(1).join(" ").trim();
  }

  var MOOD_VARS = {
    happy: "--m-happy",
    excited: "--m-excited",
    calm: "--m-calm",
    neutral: "--m-neutral",
    sad: "--m-sad",
    anxious: "--m-anxious",
    grateful: "--m-grateful",
    tired: "--m-tired",
    frustrated: "--m-frustrated",
  };

  function moodColorVar(mood) {
    if (!mood) return "var(--m-neutral)";
    var k = String(mood).toLowerCase();
    return MOOD_VARS[k] ? "var(" + MOOD_VARS[k] + ")" : "var(--m-neutral)";
  }

  function filteredNotes() {
    var q = state.search.trim().toLowerCase();
    if (!q) return state.notes;
    return state.notes.filter(function (n) {
      return (n.body || "").toLowerCase().indexOf(q) >= 0;
    });
  }

  /* ─────────────────────────  DOM SCAFFOLD  ───────────────────────── */

  root.innerHTML =
    '<div class="app">' +
      '<aside class="sidebar"></aside>' +
      '<section class="pane"></section>' +
    "</div>";
  var appEl = root.firstChild;
  var sidebarEl = appEl.querySelector(".sidebar");
  var paneEl = appEl.querySelector(".pane");

  /* ─────────────────────────  SIDEBAR  ───────────────────────── */

  function renderSidebar() {
    var listHtml;
    if (state.loading) {
      listHtml =
        '<div class="sb-skel">' +
          '<div class="sb-skel-row"></div><div class="sb-skel-row s2"></div><div class="sb-skel-row s3"></div>' +
          '<div class="sb-skel-row"></div><div class="sb-skel-row s2"></div><div class="sb-skel-row s3"></div>' +
          '<div class="sb-skel-row"></div><div class="sb-skel-row s2"></div><div class="sb-skel-row s3"></div>' +
        "</div>";
    } else if (state.error) {
      listHtml =
        '<div class="sb-empty" style="color:hsl(0 56% 45%);">' +
          esc(state.error) +
        "</div>";
    } else {
      var rows = filteredNotes();
      if (rows.length === 0) {
        if (state.search) {
          listHtml = '<div class="sb-empty">No notes match <em>"' +
            esc(state.search) + '"</em>.</div>';
        } else {
          listHtml = '<div class="sb-empty">No notes yet.<br>' +
            'Tap the <strong style="color:var(--fg);">+</strong> to begin.</div>';
        }
      } else {
        // Group into sections (notes already ordered newest first by backend)
        var sections = {};
        var ordered = [];
        rows.forEach(function (n) {
          var d = parseDate(n.created_at);
          var b = bucketLabel(d);
          if (!sections[b]) {
            sections[b] = { label: b, items: [] };
            ordered.push(sections[b]);
          }
          sections[b].items.push({ n: n, d: d });
        });
        ordered.sort(function (a, b) {
          return SECTION_ORDER.indexOf(a.label) - SECTION_ORDER.indexOf(b.label);
        });
        listHtml = ordered.map(function (sec) {
          return '<div class="sb-section">' + esc(sec.label) + "</div>" +
            sec.items.map(function (it) {
              var n = it.n;
              var active = n.id === state.selectedId;
              var title = firstLine(n.body);
              var preview = previewLines(n.body);
              var time = formatListTime(it.d);
              var mood = moodColorVar(n.mood);
              return '<div class="sb-item' + (active ? " active" : "") + '"' +
                ' data-id="' + n.id + '"' +
                ' style="--mood-color:' + mood + ';"' +
                ' tabindex="0" role="button" aria-label="Open note">' +
                '<div class="sb-item-title">' + esc(title) + "</div>" +
                (preview ? '<div class="sb-item-preview">' + esc(preview) + "</div>" : "") +
                '<div class="sb-item-time">' + esc(time) + "</div>" +
              "</div>";
            }).join("");
        }).join("");
      }
    }

    sidebarEl.innerHTML =
      '<header class="sb-header">' +
        '<h1 class="sb-title">Notes</h1>' +
        '<button class="sb-new" id="btn-new" title="New note" aria-label="New note">+</button>' +
      "</header>" +
      '<div class="sb-search-wrap">' +
        '<span class="sb-search-icon" aria-hidden="true"></span>' +
        '<input type="search" class="sb-search" id="search-input"' +
          ' placeholder="Search" autocomplete="off"' +
          ' value="' + esc(state.search) + '" />' +
      "</div>" +
      '<div class="sb-actions">' +
        '<button class="sb-action' + (state.selectedId === "summary" ? " active" : "") + '"' +
          ' id="btn-summary">' +
          '<span class="dot"></span>Summarize today' +
        "</button>" +
      "</div>" +
      '<div class="sb-list">' + listHtml + "</div>";

    sidebarEl.querySelector("#btn-new").addEventListener("click", openCompose);
    sidebarEl.querySelector("#btn-summary").addEventListener("click", openSummary);

    var searchInput = sidebarEl.querySelector("#search-input");
    searchInput.addEventListener("input", function (e) {
      state.search = e.target.value;
      renderSidebar();
      var ni = sidebarEl.querySelector("#search-input");
      if (ni) {
        ni.focus();
        var p = ni.value.length;
        try { ni.setSelectionRange(p, p); } catch (_) { /* noop */ }
      }
    });

    sidebarEl.querySelectorAll(".sb-item").forEach(function (el) {
      el.addEventListener("click", function () {
        openNote(parseInt(el.dataset.id, 10));
      });
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openNote(parseInt(el.dataset.id, 10));
        }
      });
    });
  }

  /* ─────────────────────────  MAIN PANE  ───────────────────────── */

  function renderPane() {
    if (state.selectedId === "new") return renderCompose();
    if (state.selectedId === "summary") return renderSummary();
    if (typeof state.selectedId === "number") {
      var note = state.notes.find(function (n) { return n.id === state.selectedId; });
      if (note) return renderReader(note);
    }
    renderEmpty();
  }

  function backBtnHtml() {
    return '<button class="back-btn" id="back-btn" type="button" aria-label="Back to list">' +
      '<span class="chev" aria-hidden="true"></span>Notes</button>';
  }

  function bindBackBtn() {
    var b = paneEl.querySelector("#back-btn");
    if (b) b.addEventListener("click", function () {
      state.mobileDetail = false;
      updateMobileClass();
    });
  }

  function updateMobileClass() {
    if (state.mobileDetail) appEl.classList.add("detail");
    else appEl.classList.remove("detail");
  }

  function renderEmpty() {
    if (state.loading) {
      paneEl.innerHTML = '<div class="pane-empty">' +
        '<p class="pane-empty-text">Loading…</p></div>';
      return;
    }
    if (state.notes.length === 0) {
      paneEl.innerHTML =
        '<div class="pane-empty">' +
          '<p class="pane-empty-text">A clean page.</p>' +
          '<p class="pane-empty-hint">Tap + in the sidebar to write your first note.</p>' +
        "</div>";
    } else {
      paneEl.innerHTML =
        '<div class="pane-empty">' +
          '<p class="pane-empty-text">Select a note.</p>' +
          '<p class="pane-empty-hint">Or compose a new one.</p>' +
        "</div>";
    }
  }

  function renderReader(note) {
    var d = parseDate(note.created_at);
    var stamp = formatFullStamp(d);
    var hasMood = !!note.mood;
    paneEl.innerHTML =
      '<article class="pane-inner">' +
        backBtnHtml() +
        '<div class="meta">' +
          "<span>" + esc(stamp) + "</span>" +
          (hasMood
            ? '<span class="meta-sep">·</span>' +
              '<span class="mood-pill" style="--mood-color:' + moodColorVar(note.mood) + ';">' +
                esc(note.mood) +
              "</span>"
            : "") +
        "</div>" +
        '<div class="read-body">' + esc(note.body) + "</div>" +
      "</article>";
    bindBackBtn();
  }

  function renderCompose() {
    var saveLabel = state.saving ? "Saving…" : "Save note";
    paneEl.innerHTML =
      '<form class="pane-inner compose-form" id="compose-form" novalidate>' +
        backBtnHtml() +
        '<div class="meta">' +
          "<span>New note</span>" +
          '<span class="meta-sep">·</span>' +
          "<span>" + esc(formatFullStamp(new Date())) + "</span>" +
        "</div>" +
        '<textarea class="compose-text" id="compose-text"' +
          ' placeholder="Begin writing…" autocomplete="off"' +
          ' spellcheck="true">' + esc(state.draft) + "</textarea>" +
        '<div class="compose-bar">' +
          '<button type="submit" class="compose-save" id="compose-save"' +
            (state.saving ? " disabled" : "") + ">" + saveLabel + "</button>" +
          '<span class="compose-hint">⌘ + Enter to save</span>' +
          (state.saveError ? '<span class="compose-error">' +
            esc(state.saveError) + "</span>" : "") +
          '<span class="compose-count" id="compose-count">' +
            state.draft.length + " chars</span>" +
        "</div>" +
      "</form>";

    var form = paneEl.querySelector("#compose-form");
    var ta = paneEl.querySelector("#compose-text");
    var counter = paneEl.querySelector("#compose-count");
    bindBackBtn();

    setTimeout(function () {
      if (ta && !state.saving) { ta.focus(); ta.selectionStart = ta.value.length; }
    }, 40);

    ta.addEventListener("input", function (e) {
      state.draft = e.target.value;
      if (counter) counter.textContent = state.draft.length + " chars";
    });
    ta.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        doSave();
      }
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      doSave();
    });

    function doSave() {
      var body = (ta.value || "").trim();
      if (!body || state.saving) return;
      state.saving = true;
      state.saveError = null;
      state.draft = ta.value;
      renderCompose();
      window.yorik.callOperation("yorik.notes.add_note", { body: body }).then(function (result) {
        state.saving = false;
        state.draft = "";
        return loadNotes().then(function () {
          if (result && result.id) openNote(result.id);
          else {
            renderSidebar();
            renderPane();
          }
        });
      }).catch(function (err) {
        state.saving = false;
        state.saveError = (err && err.message) ? err.message : "Save failed";
        renderCompose();
      });
    }
  }

  function renderSummary() {
    var inner;
    if (state.summarizing) {
      inner =
        '<div class="meta"><span>Summary of today</span></div>' +
        '<div class="summary-body" style="opacity:0.6;font-style:italic;">' +
          "Composing summary…" +
        "</div>";
    } else if (state.summaryError) {
      inner =
        '<div class="meta"><span>Summary of today</span></div>' +
        '<div class="summary-body" style="color:hsl(0 56% 45%);font-style:italic;">' +
          esc(state.summaryError) +
        "</div>";
    } else if (state.summary) {
      var loaded = state.summary.loadedAt;
      inner =
        '<div class="meta">' +
          "<span>Summary of today</span>" +
          '<span class="meta-sep">·</span>' +
          "<span>composed " + esc(loaded.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })) + "</span>" +
        "</div>" +
        '<div class="summary-body">' + esc(state.summary.summary || "(no summary returned)") + "</div>" +
        '<div class="summary-foot">' +
          "<span>Based on " + state.summary.note_count +
            " note" + (state.summary.note_count === 1 ? "" : "s") + " from today.</span>" +
          '<button class="summary-refresh" id="summary-refresh" type="button">Regenerate</button>' +
        "</div>";
    } else {
      inner =
        '<div class="meta"><span>Summary of today</span></div>' +
        '<div class="summary-body" style="font-style:italic;color:var(--fg-muted);">' +
          "Nothing here yet." +
        "</div>";
    }
    paneEl.innerHTML =
      '<article class="pane-inner">' + backBtnHtml() + inner + "</article>";
    bindBackBtn();
    var rb = paneEl.querySelector("#summary-refresh");
    if (rb) rb.addEventListener("click", runSummary);
  }

  /* ─────────────────────────  ACTIONS  ───────────────────────── */

  function openNote(id) {
    state.selectedId = id;
    state.mobileDetail = true;
    renderSidebar();
    renderPane();
    updateMobileClass();
  }

  function openCompose() {
    state.selectedId = "new";
    state.saveError = null;
    state.mobileDetail = true;
    renderSidebar();
    renderPane();
    updateMobileClass();
  }

  function openSummary() {
    state.selectedId = "summary";
    state.mobileDetail = true;
    renderSidebar();
    renderPane();
    updateMobileClass();
    if (!state.summary && !state.summarizing) runSummary();
  }

  function runSummary() {
    state.summarizing = true;
    state.summaryError = null;
    if (state.selectedId === "summary") renderSummary();
    window.yorik.callOperation("yorik.notes.summarize_today").then(function (result) {
      state.summarizing = false;
      state.summary = {
        summary: (result && result.summary) || "",
        note_count: (result && result.note_count) || 0,
        loadedAt: new Date(),
      };
      if (state.selectedId === "summary") renderSummary();
    }).catch(function (err) {
      state.summarizing = false;
      state.summaryError = (err && err.message) ? err.message : "Could not generate summary";
      if (state.selectedId === "summary") renderSummary();
    });
  }

  function loadNotes() {
    return window.yorik.callOperation("yorik.notes.list_notes", { limit: 100 }).then(function (result) {
      state.loading = false;
      state.error = null;
      state.notes = (result && result.notes) || [];
      renderSidebar();
      renderPane();
    }).catch(function (err) {
      state.loading = false;
      state.error = (err && err.message) ? err.message : "Could not load notes";
      renderSidebar();
      renderPane();
    });
  }

  /* ─────────────────────────  INIT  ───────────────────────── */

  renderSidebar();
  renderPane();
  loadNotes();
})();

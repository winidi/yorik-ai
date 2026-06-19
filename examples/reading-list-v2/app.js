// Reading list — full-surface manifest v2 reference iframe UI.
// Same minimal vanilla JS style as habits-v2; keeps the example
// approachable for app authors who don't want to bring React.

(function () {
  "use strict";
  var root = document.getElementById("app") || document.body;
  var state = { items: [], filter: "unread", loading: true, error: null };

  var style = document.createElement("style");
  style.textContent = [
    "body{font-family:ui-serif,Georgia,serif;background:#fafaf7;color:#1a1a1a;",
    "  margin:0;padding:32px 24px;max-width:720px;margin:0 auto;}",
    "h1{font-size:22px;font-weight:600;margin:0 0 4px;}",
    ".sub{color:#666;font-size:13px;margin-bottom:20px;}",
    ".add{display:flex;gap:8px;margin-bottom:16px;}",
    ".add input{flex:1;padding:8px 12px;border:1px solid #ddd;border-radius:8px;",
    "  background:#fff;font:inherit;font-size:14px;}",
    ".add button{padding:8px 16px;background:#1a1a1a;color:#fafaf7;border:0;",
    "  border-radius:8px;font:inherit;font-size:14px;cursor:pointer;}",
    ".filters{display:flex;gap:4px;margin-bottom:16px;font-size:13px;}",
    ".filters button{padding:4px 10px;border:1px solid transparent;",
    "  background:transparent;color:#666;border-radius:6px;cursor:pointer;font:inherit;}",
    ".filters button.active{background:#1a1a1a;color:#fafaf7;}",
    ".item{display:flex;align-items:center;gap:12px;padding:12px 0;",
    "  border-bottom:1px solid #eee;}",
    ".item-main{flex:1;min-width:0;}",
    ".item-title{font-weight:500;}",
    ".item-url{font-size:11px;color:#999;font-family:ui-monospace,monospace;",
    "  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}",
    ".status{font-size:11px;color:#666;padding:2px 8px;border:1px solid #ddd;",
    "  border-radius:4px;background:transparent;cursor:pointer;font:inherit;}",
    ".empty{text-align:center;color:#888;padding:32px 0;font-style:italic;}",
    ".error{color:#c00;font-size:13px;margin-bottom:12px;}",
  ].join("");
  document.head.appendChild(style);

  function callOp(name, params) {
    return window.yorik.callOperation("yorik.reading-list." + name, params || {});
  }

  function render() {
    root.innerHTML = "";

    var h1 = document.createElement("h1");
    h1.textContent = "Reading list";
    root.appendChild(h1);

    var sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = "Save links to read later. Recommended-by support coming.";
    root.appendChild(sub);

    if (state.error) {
      var err = document.createElement("div");
      err.className = "error";
      err.textContent = state.error;
      root.appendChild(err);
    }

    var form = document.createElement("form");
    form.className = "add";
    form.innerHTML =
      "<input id='url' type='url' placeholder='https://…' required>" +
      "<input id='title' type='text' placeholder='Title (optional)'>" +
      "<button type='submit'>Add</button>";
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var url = form.querySelector("#url").value.trim();
      var title = form.querySelector("#title").value.trim() || null;
      if (!url) return;
      callOp("add_item", { url: url, title: title })
        .then(load)
        .catch(function (er) { state.error = String(er && er.message || er); render(); });
    });
    root.appendChild(form);

    var fs = document.createElement("div");
    fs.className = "filters";
    ["unread", "reading", "read", "all"].forEach(function (s) {
      var btn = document.createElement("button");
      btn.textContent = s;
      if (state.filter === s) btn.className = "active";
      btn.addEventListener("click", function () {
        state.filter = s; load();
      });
      fs.appendChild(btn);
    });
    root.appendChild(fs);

    if (state.loading) {
      var l = document.createElement("div"); l.className = "empty";
      l.textContent = "Loading…"; root.appendChild(l); return;
    }

    if (state.items.length === 0) {
      var em = document.createElement("div"); em.className = "empty";
      em.textContent = "Nothing here. Paste a link above to start.";
      root.appendChild(em); return;
    }

    state.items.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "item";

      var main = document.createElement("div");
      main.className = "item-main";
      var t = document.createElement("div");
      t.className = "item-title";
      t.textContent = item.title || item.url;
      var u = document.createElement("div");
      u.className = "item-url";
      u.textContent = item.url;
      main.appendChild(t); main.appendChild(u);
      row.appendChild(main);

      var next = nextStatus(item.status);
      if (next) {
        var btn = document.createElement("button");
        btn.className = "status";
        btn.textContent = "→ " + next;
        btn.title = "Currently: " + item.status;
        btn.addEventListener("click", function () {
          callOp("mark_status", { item_id: item.id, status: next }).then(load);
        });
        row.appendChild(btn);
      } else {
        var s = document.createElement("span");
        s.className = "status";
        s.textContent = item.status;
        row.appendChild(s);
      }

      root.appendChild(row);
    });
  }

  function nextStatus(s) {
    if (s === "unread") return "reading";
    if (s === "reading") return "read";
    return null;
  }

  function load() {
    state.loading = true; state.error = null; render();
    callOp("list_items", { status: state.filter }).then(function (r) {
      state.items = (r && r.items) || [];
      state.loading = false; render();
    }).catch(function (er) {
      state.error = String(er && er.message || er);
      state.loading = false; render();
    });
  }

  load();
})();

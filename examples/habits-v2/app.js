// Habits — minimum-viable reference iframe UI.
//
// Runs in a sandboxed iframe. Talks to its own operations via
// window.yorik.callOperation. Deliberately keeps DOM construction
// hand-rolled (no React) so the example stays approachable.

(function () {
  "use strict";

  var root = document.getElementById("app") || document.body;
  var state = { habits: [], loading: true, error: null };

  // ─── styles ───
  var style = document.createElement("style");
  style.textContent = [
    "body{font-family:ui-serif,Georgia,serif;background:#fafaf7;color:#1a1a1a;",
    "  margin:0;padding:32px 24px;max-width:640px;margin:0 auto;}",
    "h1{font-size:22px;font-weight:600;margin:0 0 16px;}",
    ".add{display:flex;gap:8px;margin-bottom:24px;}",
    ".add input{flex:1;padding:8px 12px;border:1px solid #ddd;border-radius:8px;",
    "  background:#fff;font:inherit;font-size:14px;}",
    ".add input[type=number]{flex:0 0 80px;}",
    ".add button{padding:8px 16px;background:#1a1a1a;color:#fafaf7;border:0;",
    "  border-radius:8px;font:inherit;font-size:14px;cursor:pointer;}",
    ".habit{display:flex;align-items:center;gap:12px;padding:12px 0;",
    "  border-bottom:1px solid #eee;}",
    ".habit-name{flex:1;font-weight:500;}",
    ".habit-progress{font-size:12px;color:#666;font-variant-numeric:tabular-nums;}",
    ".bar{display:inline-block;width:60px;height:4px;background:#eee;",
    "  border-radius:2px;overflow:hidden;margin:0 8px;vertical-align:middle;}",
    ".bar-fill{height:100%;background:#1a1a1a;}",
    ".log{padding:6px 10px;border:1px solid #ccc;background:transparent;",
    "  border-radius:6px;font:inherit;font-size:12px;cursor:pointer;}",
    ".log:hover{background:#eee;}",
    ".empty{text-align:center;color:#888;padding:32px 0;font-style:italic;}",
    ".error{color:#c00;font-size:13px;margin-bottom:12px;}",
  ].join("");
  document.head.appendChild(style);

  function callOp(name, params) {
    return window.yorik.callOperation("yorik.habits." + name, params || {});
  }

  function render() {
    root.innerHTML = "";

    var h1 = document.createElement("h1");
    h1.textContent = "Habits";
    root.appendChild(h1);

    if (state.error) {
      var err = document.createElement("div");
      err.className = "error";
      err.textContent = state.error;
      root.appendChild(err);
    }

    // Add form
    var form = document.createElement("form");
    form.className = "add";
    form.innerHTML =
      "<input id='hname' type='text' placeholder='New habit…' required>" +
      "<input id='htarget' type='number' min='1' max='21' value='3'>" +
      "<button type='submit'>Add</button>";
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var name = form.querySelector("#hname").value.trim();
      var target = parseInt(form.querySelector("#htarget").value, 10) || 1;
      if (!name) return;
      callOp("add_habit", { name: name, target_per_week: target })
        .then(load)
        .catch(function (err) { state.error = String(err && err.message || err); render(); });
    });
    root.appendChild(form);

    if (state.loading) {
      var l = document.createElement("div");
      l.className = "empty";
      l.textContent = "Loading…";
      root.appendChild(l);
      return;
    }

    if (state.habits.length === 0) {
      var em = document.createElement("div");
      em.className = "empty";
      em.textContent = "No habits yet — add one above.";
      root.appendChild(em);
      return;
    }

    state.habits.forEach(function (h) {
      var row = document.createElement("div");
      row.className = "habit";

      var name = document.createElement("div");
      name.className = "habit-name";
      name.textContent = h.name;
      row.appendChild(name);

      var ratio = Math.min(1, h.completions_last_7d / Math.max(1, h.target_per_week));
      var prog = document.createElement("div");
      prog.className = "habit-progress";
      prog.innerHTML =
        h.completions_last_7d + " / " + h.target_per_week +
        "<span class='bar'><span class='bar-fill' style='width:" +
        Math.round(ratio * 100) + "%'></span></span> last 7d";
      row.appendChild(prog);

      var btn = document.createElement("button");
      btn.className = "log";
      btn.textContent = "Log";
      btn.addEventListener("click", function () {
        callOp("log_completion", { habit_id: h.id }).then(load);
      });
      row.appendChild(btn);

      root.appendChild(row);
    });
  }

  function load() {
    state.loading = true;
    state.error = null;
    render();
    callOp("list_habits", {}).then(function (r) {
      state.habits = (r && r.habits) || [];
      state.loading = false;
      render();
    }).catch(function (err) {
      state.error = String(err && err.message || err);
      state.loading = false;
      render();
    });
  }

  load();
})();

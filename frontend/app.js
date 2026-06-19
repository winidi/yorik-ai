// HomeOS — controller for the browser dashboard.
// Layouts render inside sandboxed iframes; this file is the host/orchestrator.

// Known bundled layouts. Marketplace-installed ones get added at runtime.
// Apple is temporarily disabled — toolbar wiring went into yorik-calendar
// only, so switching to apple leaves the user without controls. Re-enable
// when apple.js gains the same in-iframe toolbar.
const BUNDLED_LAYOUTS = new Set(["yorik-calendar"]);
const _legacyLayout = localStorage.getItem("homeos_layout");
if (_legacyLayout && !BUNDLED_LAYOUTS.has(_legacyLayout)) {
  // Old "google", "google-classic", or "apple" → migrate to the default.
  localStorage.setItem("homeos_layout", "yorik-calendar");
}

const state = {
  role: localStorage.getItem("homeos_role") || "admin",
  layout: localStorage.getItem("homeos_layout") || "yorik-calendar",
  view: localStorage.getItem("homeos_gcal_view") || "month", // "month" | "week"
  month: new Date().getMonth() + 1,
  year: new Date().getFullYear(),
  anchorIso: null,           // for week view navigation across week boundaries
  highlightIds: new Set(),
  conversationId: sessionStorage.getItem("homeos_conversation_id") || null,
  events: [],
  tasks: [],
  recorder: null,
  voiceMaxSeconds: 60,       // overwritten by /api/health on load

  // Wave 6: top-level app routing. 'home' = home screen. 'calendar' / 'chat' /
  // 'docs' = bundled apps. Community apps will land here too with their own ids.
  app: localStorage.getItem("yorik_app") || "calendar",
  apps: [],                  // populated from /api/apps
  chat: {
    messages: [],            // messages in the *active* conversation
    history: [],             // list of past conversations from /api/conversations
    activeConvId: null,
  },
  docs: {
    list: [],                // documents from /api/documents
    selectedId: null,
    searchResults: null,     // null = not searching; [] = no hits; [...] = hits
  },
};

const RESET_CONV_PATTERNS = [
  /^\s*(start over|new conversation|new chat|reset|forget (it|that|this)|begin again)\b/i,
];

function maybeResetConversation(userMessage) {
  if (RESET_CONV_PATTERNS.some(re => re.test(userMessage))) {
    state.conversationId = null;
    sessionStorage.removeItem("homeos_conversation_id");
    return true;
  }
  return false;
}

function persistConversationId(id) {
  if (!id || id === state.conversationId) return;
  state.conversationId = id;
  sessionStorage.setItem("homeos_conversation_id", id);
}

const $ = (sel) => document.querySelector(sel);

async function fetchJSON(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

// Local-date ISO formatter — uses local components, NOT toISOString (UTC).
function fmtIso(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Compute the visible date range so we fetch only what the calendar grid actually shows.
// Returns [startIso, endIso] half-open. Week view: 7 days centered on anchorIso.
// Month view: the 6-week grid that contains the month (Mon-anchored).
function visibleRange() {
  if (state.view === "week" && state.layout === "yorik-calendar") {
    const anchor = state.anchorIso
      ? new Date(state.anchorIso + "T12:00:00")
      : new Date();
    const start = new Date(anchor);
    const off = (start.getDay() + 6) % 7;       // Monday-anchored
    start.setDate(start.getDate() - off);
    const end = new Date(start);
    end.setDate(end.getDate() + 7);
    return [fmtIso(start), fmtIso(end)];
  }
  // Default = month grid: 42-day Monday-anchored window that contains the month.
  const first = new Date(state.year, state.month - 1, 1);
  const off = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - off);
  const end = new Date(start);
  end.setDate(start.getDate() + 42);
  return [fmtIso(start), fmtIso(end)];
}

async function loadAll() {
  try {
    const [s, e] = visibleRange();
    const [events, tasks, cats] = await Promise.all([
      fetchJSON(`/api/events?role=${state.role}&start_date=${s}&end_date=${e}`),
      fetchJSON(`/api/tasks?role=${state.role}`),
      fetchJSON(`/api/task-categories`).catch(() => []),
    ]);
    state.events = events;
    state.tasks = tasks;
    state.taskCategories = cats;
    renderAll();
  } catch (e) {
    showResponse(`Failed to load: ${e.message}`, true);
  }
}

function renderAll() {
  // Calendar render = post current state into the iframe; sidebar stays in parent.
  postStateToLayout();
  renderTasks();
}

function renderTasks() {
  const root = $("#tasks");
  if (!root) return;
  const cats = state.taskCategories || [];
  // Build a lookup so we can colour each section by its category.
  const catByName = Object.fromEntries(cats.map(c => [c.name, c]));

  // Group tasks by category. Preserve user-defined category order; tasks
  // without a category land in "Uncategorised" at the end.
  const buckets = new Map();
  for (const c of cats) buckets.set(c.name, []);
  buckets.set(null, []);
  for (const t of state.tasks) {
    const key = t.category && buckets.has(t.category) ? t.category : null;
    buckets.get(key).push(t);
  }

  const header = `
    <div class="tasks-header">
      <h2>Tasks <span class="tasks-role">${escapeHtml(state.role)}</span></h2>
      <div class="tasks-header-actions">
        <button id="manage-cats" class="subtle" title="Manage categories">⚙</button>
        <button id="add-task" class="primary">+ Task</button>
      </div>
    </div>`;

  if (!state.tasks.length) {
    root.innerHTML = header + `
      <div class="tasks-empty">No tasks yet for <em>${escapeHtml(state.role)}</em>. Click <strong>+ Task</strong>.</div>`;
    root.querySelector("#add-task")?.addEventListener("click", () => openTaskModal({}));
    root.querySelector("#manage-cats")?.addEventListener("click", openCategoriesModal);
    return;
  }

  const sectionsHtml = [];
  for (const [catName, tasks] of buckets.entries()) {
    if (!tasks.length) continue;
    const cat = catName ? catByName[catName] : null;
    const color = cat?.color || "var(--text-faint)";
    const label = catName || "Uncategorised";
    const open = tasks.filter(t => !t.done).length;
    sectionsHtml.push(`
      <section class="task-cat">
        <header class="task-cat-h">
          <span class="task-cat-dot" style="background:${color}"></span>
          <span class="task-cat-name">${escapeHtml(label)}</span>
          <span class="task-cat-count">${open}/${tasks.length}</span>
        </header>
        <div class="task-cat-list">
          ${tasks.map(t => `
            <div class="task ${t.done ? "done" : ""}" data-tid="${t.id}" title="click to edit"
                 style="border-left-color:${cat?.color || "transparent"}">
              <label class="task-check" data-stop>
                <input type="checkbox" data-toggle="${t.id}" ${t.done ? "checked" : ""}>
                <span class="checkmark"></span>
              </label>
              <div class="task-body">
                <div class="task-title">${escapeHtml(t.title)}</div>
                ${t.due_date || t.person ? `
                  <div class="task-meta">
                    ${t.due_date ? `<span class="due">${escapeHtml(t.due_date)}</span>` : ""}
                    ${t.person ? `<span class="person">${escapeHtml(t.person)}</span>` : ""}
                  </div>` : ""}
              </div>
            </div>
          `).join("")}
        </div>
      </section>
    `);
  }

  root.innerHTML = header + `<div class="tasks-body">${sectionsHtml.join("")}</div>`;

  root.querySelector("#add-task")?.addEventListener("click", () => openTaskModal({}));
  root.querySelector("#manage-cats")?.addEventListener("click", openCategoriesModal);
  root.querySelectorAll(".task").forEach(el => {
    el.addEventListener("click", async (e) => {
      const toggle = e.target.closest("[data-toggle]");
      if (toggle || e.target.closest("[data-stop]")) {
        if (!toggle) return;
        e.stopPropagation();
        const id = Number(toggle.getAttribute("data-toggle"));
        const wasChecked = toggle.checked;
        try {
          await fetchJSON(`/api/tasks/${id}?role=${state.role}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ done: wasChecked }),
          });
          loadAll();
        } catch (err) {
          toggle.checked = !wasChecked;
          showResponse(`Could not update task: ${err.message}`, true);
        }
        return;
      }
      const id = Number(el.getAttribute("data-tid"));
      const t = state.tasks.find(x => x.id === id);
      if (t) openTaskModal({ task: t });
    });
  });
}

async function openCategoriesModal() {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" style="max-width:440px">
      <h3>Task categories</h3>
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:12px">
        Add, rename, or delete categories. Deleting one un-categorises its tasks.
      </div>
      <div id="cat-list" style="display:flex; flex-direction:column; gap:6px; margin-bottom:14px"></div>
      <div style="display:flex; gap:6px">
        <input id="cat-name" placeholder="New category name" style="flex:1">
        <input id="cat-color" type="color" value="#818cf8" style="width:48px; padding:2px">
        <button id="cat-add" class="primary">Add</button>
      </div>
      <div class="actions" style="margin-top:14px">
        <button id="cat-close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#cat-close").onclick = close;

  async function refresh() {
    const cats = await fetchJSON("/api/task-categories");
    state.taskCategories = cats;
    const list = back.querySelector("#cat-list");
    list.innerHTML = cats.map(c => `
      <div style="display:flex; gap:8px; align-items:center; padding:6px 10px; background:var(--card-2); border-radius:8px">
        <span style="width:14px;height:14px;border-radius:50%;background:${c.color};flex-shrink:0"></span>
        <span style="flex:1; font-size:13px">${escapeHtml(c.name)}</span>
        <button class="danger" data-del="${c.id}" style="padding:3px 9px; font-size:11px">Delete</button>
      </div>
    `).join("") || `<div style="color:var(--text-dim); font-size:12px; padding:8px">No categories yet.</div>`;
    list.querySelectorAll("[data-del]").forEach(btn => {
      btn.onclick = async () => {
        if (!confirm("Delete this category? Tasks using it become uncategorised.")) return;
        await fetch(`/api/task-categories/${btn.getAttribute("data-del")}?role=${state.role}`, { method: "DELETE" });
        await refresh();
        renderTasks();
      };
    });
  }
  back.querySelector("#cat-add").onclick = async () => {
    const name = back.querySelector("#cat-name").value.trim();
    if (!name) return;
    const color = back.querySelector("#cat-color").value || "#818cf8";
    const r = await fetch(`/api/task-categories?role=${state.role}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, color }),
    });
    if (!r.ok) { const t = await r.text(); alert("Failed: " + t); return; }
    back.querySelector("#cat-name").value = "";
    await refresh();
    renderTasks();
  };
  await refresh();
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showResponse(text, isError = false, sqlUsed = null) {
  const panel = $("#response");
  if (!panel) return;
  panel.innerHTML = `
    <div style="color:${isError ? "var(--danger)" : "var(--text)"}">${escapeHtml(text)}</div>
    ${sqlUsed ? `<details style="margin-top:8px"><summary style="cursor:pointer;color:var(--text-dim)">SQL used</summary><pre>${escapeHtml(sqlUsed)}</pre></details>` : ""}
  `;
}

async function askText(msg) {
  if (!msg) return;
  const wasReset = maybeResetConversation(msg);
  if (wasReset) {
    showResponse("New conversation — earlier context cleared.");
    return;
  }
  showResponse("Asking the LLM...");
  try {
    const r = await fetchJSON("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, role: state.role, conversation_id: state.conversationId }),
    });
    persistConversationId(r.conversation_id);
    const prefix = r.from_cache ? "⚡ answered from cache · " : "";
    showResponse(prefix + (r.response || "(no response)"), r.error === true, r.sql_used);
    // Apply any UI actions the LLM emitted — these reload events as needed.
    const applied = await applyUiActions(r.ui_actions || []);
    // If the LLM didn't take a view-changing action, just refresh in case it wrote rows.
    if (!applied) loadAll();
  } catch (e) {
    showResponse(`Ask failed: ${e.message}`, true);
  }
}

async function askVoice() {
  // If TTS is currently playing, the mic acts as a stop button — interrupting
  // a long voice answer is a one-tap action with no risk of an accidental
  // new recording.
  if (_audioPlaying) {
    resetTTSQueue();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    showResponse("Browser does not support audio recording — type instead.", true);
    return;
  }
  const btn = $("#voice");

  // Toggle: second click stops recording immediately. No fixed time cap below
  // the configured ceiling — the user controls when to stop.
  if (state.recorder) {
    state.recorder.stop();
    return;
  }
  // Starting a fresh recording while a previous answer is still queued —
  // cut it off so the user isn't fighting the speaker.
  resetTTSQueue();

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const rec = new MediaRecorder(stream);
    const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);

    const startedAt = Date.now();
    let tickId = null;
    const maxSec = Math.max(5, state.voiceMaxSeconds || 60);
    const showTimer = () => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      const remaining = maxSec - elapsed;
      setVoiceBtn("■", `REC ${elapsed}s`);
      btn.title = `Recording — click to stop (auto-stop in ${remaining}s)`;
    };
    showTimer();
    tickId = setInterval(showTimer, 1000);

    rec.onstop = async () => {
      btn.classList.remove("recording");
      setVoiceBtn("mic", "Talk");
      btn.title = "Tap to talk to Yorik";
      if (tickId) clearInterval(tickId);
      state.recorder = null;
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
      const fd = new FormData();
      fd.append("audio", blob, "voice.webm");
      showResponse(`Transcribing (${Math.round(blob.size / 1024)} KB)…`);
      resetTTSQueue();
      try {
        const url = `/api/ask-voice/stream?role=${state.role}${state.conversationId ? `&conversation_id=${encodeURIComponent(state.conversationId)}` : ""}`;
        const r = await fetch(url, { method: "POST", body: fd });
        if (!r.ok || !r.body) {
          const j = await r.json().catch(() => ({}));
          throw new Error(j.error || `HTTP ${r.status}`);
        }
        // Read NDJSON line-by-line. Server emits events in this order:
        //   transcript → identification → audio* (per sentence) → done | error
        const reader = r.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";
        let transcript = "";
        let idLine = "";
        let resetConv = false;
        // Streaming-into-chat state: the assistant bubble we grow as
        // sentence-chunks arrive. Indexed so we can update its content
        // in-place without re-querying state.chat.messages.
        let assistantBubbleIdx = -1;

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let nl;
          while ((nl = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, nl).trim();
            buf = buf.slice(nl + 1);
            if (!line) continue;
            let ev;
            try { ev = JSON.parse(line); } catch { continue; }
            if (ev.type === "transcript") {
              transcript = ev.text || "";
              if (maybeResetConversation(transcript)) {
                showResponse(`"${transcript}"\n\nNew conversation — earlier context cleared.`);
                resetConv = true;
                break;
              }
              showResponse(`🎤 "${transcript}"\n\nThinking…`);
              // Push the user bubble immediately so the conversation feels
              // live even while the LLM is still thinking.
              if (state.app === "chat") {
                pushChatMessage("user", transcript);
              }
            } else if (ev.type === "identification") {
              idLine = ev.identified
                ? `🎤 Identified as ${ev.identified.name} (sim ${ev.identified.similarity}) · lang ${ev.language}\n`
                : `🎤 Voice not recognized → using role "${ev.effective_role || state.role}" · lang ${ev.language || "en"}\n`;
            } else if (ev.type === "audio") {
              enqueueTTS(ev.url);
              // Stream-into-chat: append this sentence to the growing
              // assistant bubble (creating it on the first chunk).
              if (state.app === "chat") {
                if (assistantBubbleIdx < 0) {
                  pushChatMessage("assistant", ev.text || "");
                  assistantBubbleIdx = state.chat.messages.length - 1;
                } else {
                  const prev = state.chat.messages[assistantBubbleIdx].content;
                  state.chat.messages[assistantBubbleIdx].content = (prev + " " + (ev.text || "")).trim();
                  renderChatMessages();
                }
              }
            } else if (ev.type === "done") {
              persistConversationId(ev.conversation_id);
              const prefix = ev.from_cache ? "⚡ answered from cache · " : "";
              showResponse(`${idLine}"${transcript}"\n\n${prefix}${ev.response || ""}`, false, ev.sql_used);
              // Reconcile chat bubble with the full server response — some
              // TTS chunks may have been dropped (emoji-only sentences etc.),
              // so prefer the authoritative text from the done event.
              if (state.app === "chat") {
                if (assistantBubbleIdx < 0 && (ev.response || "")) {
                  pushChatMessage("assistant", ev.response);
                } else if (assistantBubbleIdx >= 0 && (ev.response || "").trim() !== state.chat.messages[assistantBubbleIdx].content.trim()) {
                  state.chat.messages[assistantBubbleIdx].content = ev.response;
                  renderChatMessages();
                }
                loadConversationHistory();
              }
              const applied = await applyUiActions(ev.ui_actions || []);
              if (!applied) loadAll();
            } else if (ev.type === "error") {
              throw new Error(ev.error || "unknown stream error");
            }
          }
          if (resetConv) break;
        }
      } catch (e) {
        showResponse(`Voice ask failed: ${e.message}`, true);
      }
    };
    rec.start();
    state.recorder = rec;
    btn.classList.add("recording");
    btn.classList.remove("playing");
    // Safety ceiling only — under normal use the user clicks again to stop.
    setTimeout(() => { if (state.recorder === rec) rec.stop(); }, maxSec * 1000);
  } catch (e) {
    showResponse(`Mic error: ${e.message}`, true);
  }
}

// `datetime-local` requires "YYYY-MM-DDTHH:MM" — strip seconds / TZ if present.
function toDtLocal(iso) {
  if (!iso) return "";
  return String(iso).slice(0, 16);
}

function todayDtLocal(date9am = true) {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}T${date9am ? "09:00" : "12:00"}`;
}

function openEventModal({ event = null, prefillDate = null, prefillStart = null, prefillEnd = null } = {}) {
  const editing = !!event;
  const back = document.createElement("div");
  back.className = "modal-back";
  const starts = editing
    ? toDtLocal(event.starts_at)
    : (prefillStart ? toDtLocal(prefillStart) : (prefillDate ? `${prefillDate}T09:00` : todayDtLocal()));
  const ends = editing
    ? toDtLocal(event.ends_at)
    : (prefillEnd ? toDtLocal(prefillEnd) : "");
  back.innerHTML = `
    <div class="modal">
      <h3>${editing ? "Edit event" : "Add event"}</h3>
      <label>Title <input id="ev-title" required value="${escapeHtml(editing ? event.title : "")}"></label>
      <label>Starts at <input id="ev-start" type="datetime-local" value="${starts}"></label>
      <label>Ends at <input id="ev-end" type="datetime-local" value="${ends}"></label>
      <label>Person <input id="ev-person" placeholder="optional" value="${escapeHtml(editing ? (event.person || "") : "")}"></label>
      <label>Color <input id="ev-color" type="color" value="${editing ? (event.color || "#818cf8") : "#818cf8"}"></label>
      <label>Notes <input id="ev-notes" placeholder="optional" value="${escapeHtml(editing ? (event.notes || "") : "")}"></label>
      <label>Visible to (comma-separated roles) <input id="ev-roles" value="${escapeHtml(editing ? (event.allowed_roles || "admin,member") : "admin,member")}"></label>
      <div class="actions">
        ${editing ? `<button id="ev-delete" class="danger" style="margin-right:auto">Delete</button>` : ""}
        <button id="ev-cancel">Cancel</button>
        <button id="ev-save" class="primary">Save</button>
      </div>
    </div>
  `;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#ev-cancel").onclick = close;

  back.querySelector("#ev-save").onclick = async () => {
    // datetime-local returns "YYYY-MM-DDTHH:MM" without seconds — normalize.
    const pad = (s) => (s && s.length === 16 ? `${s}:00` : s);
    const startsAt = pad(back.querySelector("#ev-start").value);
    const endsAt = pad(back.querySelector("#ev-end").value);
    const body = {
      title: back.querySelector("#ev-title").value.trim(),
      starts_at: startsAt,
      ends_at: endsAt || null,
      person: back.querySelector("#ev-person").value || null,
      color: back.querySelector("#ev-color").value,
      notes: back.querySelector("#ev-notes").value || null,
      allowed_roles: back.querySelector("#ev-roles").value || "admin,member",
    };
    if (!body.title || !body.starts_at) return;
    try {
      const url = editing ? `/api/events/${event.id}?role=${state.role}` : `/api/events?role=${state.role}`;
      const r = await fetch(url, {
        method: editing ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const t = await r.text();
        showResponse(`${editing ? "Save" : "Add"} failed: ${t}`, true);
        return;
      }
      // If the saved date is outside the currently visible window, navigate to it
      // so the user actually sees the change they just made.
      const savedDate = startsAt.slice(0, 10);
      const [winStart, winEnd] = visibleRange();
      if (savedDate < winStart || savedDate >= winEnd) {
        const d = new Date(savedDate + "T12:00:00");
        state.year = d.getFullYear();
        state.month = d.getMonth() + 1;
        state.anchorIso = savedDate;
      }
      close();
      await loadAll();
    } catch (e) {
      showResponse(`Network error: ${e.message}`, true);
    }
  };

  if (editing) {
    back.querySelector("#ev-delete").onclick = async () => {
      if (!confirm(`Delete "${event.title}"?`)) return;
      try {
        const r = await fetch(`/api/events/${event.id}?role=${state.role}`, { method: "DELETE" });
        if (!r.ok) {
          const t = await r.text();
          showResponse(`Delete failed: ${t}`, true);
          return;
        }
        close();
        loadAll();
      } catch (e) {
        showResponse(`Network error: ${e.message}`, true);
      }
    };
  }

  setTimeout(() => back.querySelector("#ev-title").focus(), 50);
}

function openTaskModal({ task = null, prefillDate = null } = {}) {
  const editing = !!task;
  const back = document.createElement("div");
  back.className = "modal-back";
  const due = editing ? (task.due_date || "") : (prefillDate || "");
  back.innerHTML = `
    <div class="modal">
      <h3>${editing ? "Edit task" : "Add task"}</h3>
      <label>Title <input id="tk-title" required value="${escapeHtml(editing ? task.title : "")}"></label>
      <label>Due date <input id="tk-due" type="date" value="${due}"></label>
      <label>Category
        <select id="tk-category">
          <option value="">— uncategorised —</option>
          ${(state.taskCategories || []).map(c =>
            `<option value="${escapeHtml(c.name)}" ${editing && task.category === c.name ? "selected" : ""}>${escapeHtml(c.name)}</option>`
          ).join("")}
        </select>
      </label>
      <label>Person <input id="tk-person" placeholder="optional" value="${escapeHtml(editing ? (task.person || "") : "")}"></label>
      <label>Notes <input id="tk-notes" placeholder="optional" value="${escapeHtml(editing ? (task.notes || "") : "")}"></label>
      <label>Visible to (comma-separated roles) <input id="tk-roles" value="${escapeHtml(editing ? (task.allowed_roles || "admin,member") : "admin,member")}"></label>
      ${editing ? `<label style="flex-direction:row;align-items:center;gap:8px"><input id="tk-done" type="checkbox" ${task.done ? "checked" : ""}> Done</label>` : ""}
      <div class="actions">
        ${editing ? `<button id="tk-delete" class="danger" style="margin-right:auto">Delete</button>` : ""}
        <button id="tk-cancel">Cancel</button>
        <button id="tk-save" class="primary">Save</button>
      </div>
    </div>
  `;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#tk-cancel").onclick = close;

  back.querySelector("#tk-save").onclick = async () => {
    const body = {
      title: back.querySelector("#tk-title").value.trim(),
      due_date: back.querySelector("#tk-due").value || null,
      person: back.querySelector("#tk-person").value || null,
      category: back.querySelector("#tk-category").value || null,
      notes: back.querySelector("#tk-notes").value || null,
      allowed_roles: back.querySelector("#tk-roles").value || "admin,member",
    };
    if (editing) body.done = back.querySelector("#tk-done").checked;
    if (!body.title) return;
    try {
      const url = editing ? `/api/tasks/${task.id}?role=${state.role}` : `/api/tasks?role=${state.role}`;
      const r = await fetch(url, {
        method: editing ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const t = await r.text();
        showResponse(`${editing ? "Save" : "Add"} task failed: ${t}`, true);
        return;
      }
      close();
      loadAll();
    } catch (e) {
      showResponse(`Network error: ${e.message}`, true);
    }
  };

  if (editing) {
    back.querySelector("#tk-delete").onclick = async () => {
      if (!confirm(`Delete task "${task.title}"?`)) return;
      try {
        const r = await fetch(`/api/tasks/${task.id}?role=${state.role}`, { method: "DELETE" });
        if (!r.ok) {
          const t = await r.text();
          showResponse(`Delete failed: ${t}`, true);
          return;
        }
        close();
        loadAll();
      } catch (e) {
        showResponse(`Network error: ${e.message}`, true);
      }
    };
  }

  setTimeout(() => back.querySelector("#tk-title").focus(), 50);
}

function wire() {
  // The role dropdown is gone — role now comes from the logged-in user.
  // The user-pill in the header just shows the current user; click for
  // Settings → Account (wave 2).
  $("#user-pill")?.addEventListener("click", () => openSettingsModal({ initialTab: "account" }));
  $("#logout-btn")?.addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    location.reload();
  });

  // (#layout dropdown + #add-event button moved INTO the calendar iframe —
  // each calendar layout draws its own toolbar with these. Yorik's header
  // is now app-agnostic.)

  $("#ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#ask-input");
    askText(input.value.trim());
    input.value = "";
  });

  $("#voice").addEventListener("click", askVoice);
  // (Voices used to be its own header button; now it's a Settings tab.)
  $("#settings").addEventListener("click", () => openSettingsModal({}));

  // Light/dark toggle. The initial theme was already applied by the inline
  // script in <head> before first paint so there's no flash. Here we just
  // wire the user-facing flip and persist it, then push the new vars into
  // every mounted iframe so calendar / community apps re-skin in sync.
  const _syncThemeIcon = () => {
    const t = document.documentElement.getAttribute("data-theme") || "dark";
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.innerHTML = `<svg class="icon"><use href="#i-${t === "dark" ? "moon" : "sun"}"/></svg>`;
  };
  // Initial paint — the inline-head script set data-theme before render,
  // but the icon defaults to "moon"; sync once on wire().
  setTimeout(_syncThemeIcon, 50);

  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("yorik_theme", next); } catch (e) {}
    _syncThemeIcon();
    // Tell every iframe to pull fresh CSS vars (theme propagation).
    document.querySelectorAll("iframe[data-kind]").forEach(f => {
      try { f.contentWindow.postMessage({ _yorik: 1, type: "theme", theme: next, vars: themeVarsString() }, "*"); } catch (e) {}
    });
  });

  document.addEventListener("homeos:day-selected", (e) => {
    const { date, events } = e.detail;
    const panel = $("#response");
    if (!panel) return;
    if (!events.length) {
      panel.innerHTML = `
        <div><strong>${escapeHtml(date)}</strong> · nothing scheduled.</div>
        <div style="margin-top:8px"><button class="primary" id="day-add">+ Add event on this day</button></div>
      `;
      panel.querySelector("#day-add").onclick = () => openEventModal({ prefillDate: date });
      return;
    }
    panel.innerHTML = `
      <div><strong>${escapeHtml(date)}</strong> · ${events.length} event${events.length > 1 ? "s" : ""} <span style="color:var(--text-dim)">(click any to edit)</span></div>
      <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px">
        ${events.map(ev => `
          <button class="day-evt" data-eid="${ev.id}" style="text-align:left;display:flex;justify-content:space-between;gap:8px">
            <span>${escapeHtml(ev.title)}</span>
            <span style="color:var(--text-dim);font-size:12px">${(ev.starts_at || "").slice(11, 16) || "all day"}</span>
          </button>
        `).join("")}
      </div>
      <div style="margin-top:8px"><button class="primary" id="day-add">+ Add event on this day</button></div>
    `;
    panel.querySelector("#day-add").onclick = () => openEventModal({ prefillDate: date });
    panel.querySelectorAll(".day-evt").forEach(btn => {
      btn.onclick = () => {
        const id = Number(btn.getAttribute("data-eid"));
        const ev = state.events.find(x => x.id === id) || events.find(x => x.id === id);
        if (ev) openEventModal({ event: ev });
      };
    });
  });

  document.addEventListener("homeos:month-change", (e) => {
    state.month = e.detail.month;
    state.year = e.detail.year;
    state.anchorIso = null;
    loadAll();
  });

  document.addEventListener("homeos:week-change", (e) => {
    const { month, year, anchorIso } = e.detail;
    state.anchorIso = anchorIso;
    state.month = month;
    state.year = year;
    // ALWAYS reload — visibleRange() in week view is just the 7-day window
    // around anchorIso, so within-month navigation still needs a fresh fetch.
    // Otherwise the user sees an empty week until the 30s polling tick.
    loadAll();
  });

  document.addEventListener("homeos:event-selected", (e) => {
    const ev = e.detail.event;
    if (!ev) return;
    openEventModal({ event: ev });
  });

  document.addEventListener("homeos:slot-selected", (e) => {
    const { starts_at, ends_at } = e.detail;
    openEventModal({ prefillStart: starts_at, prefillEnd: ends_at });
  });
}

// ---------------------------------------------------------------------------
// LLM-driven UI actions — the agent emits these in /api/ask responses.
// ---------------------------------------------------------------------------

async function applyUiActions(actions) {
  if (!actions || actions.length === 0) return false;
  // Server-side auto-emitted show_calendar (reason === "just modified") is
  // authoritative — it has the correct anchor + just-inserted row id. The LLM
  // sometimes emits a competing show_calendar with stale or hallucinated ids.
  // When the authoritative one is present, drop the others.
  const authoritative = actions.find(a => a.type === "show_calendar" && a.reason === "just modified");
  if (authoritative) {
    actions = actions.filter(a => a.type !== "show_calendar" || a === authoritative);
  }
  let appliedAny = false;
  for (const a of actions) {
    if (a.type === "show_calendar") {
      const anchor = a.anchor_date;       // YYYY-MM-DD
      if (!anchor) continue;
      const d = new Date(anchor + "T12:00:00"); // mid-day to avoid TZ edge
      const newMonth = d.getMonth() + 1;
      const newYear = d.getFullYear();
      const needReload = newMonth !== state.month || newYear !== state.year;
      state.month = newMonth;
      state.year = newYear;
      state.anchorIso = anchor;
      state.view = a.view === "day" ? "week" : (a.view || "week"); // day view not implemented; fall back to week
      localStorage.setItem("homeos_gcal_view", state.view);
      state.highlightIds = new Set((a.highlight_event_ids || []).map(Number));
      if (needReload) await loadAll();
      else renderAll();
      appliedAny = true;
    } else if (a.type === "open_layout_picker") {
      openLayoutPicker(a.layouts || []);
      appliedAny = true;
    } else if (a.type === "open_n8n_install") {
      const url = (a.n8n_base_url || "http://127.0.0.1:5678") + "/";
      showResponse(`Open n8n at ${url} to finish installing "${a.connector_name}". ${a.install_hint || ""}`);
      window.open(url, "_blank", "noopener");
      appliedAny = true;
    } else if (a.type === "refresh_data") {
      // Server-side hint after a write to events/tasks/bills: reload now so
      // the user sees the change while the agent is still speaking.
      await loadAll();
      appliedAny = true;
    }
  }
  return appliedAny;
}

// ---------------------------------------------------------------------------
// Layout picker modal — opened by the LLM via list_calendar_layouts.
// ---------------------------------------------------------------------------

function openLayoutPicker(layouts) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" style="max-width:520px">
      <h3>Calendar layouts</h3>
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:8px">
        Pick a style. Choices ship with HomeOS; the marketplace will grow with community uploads.
      </div>
      <div id="layout-list" style="display:flex; flex-direction:column; gap:8px; max-height:60vh; overflow:auto"></div>
      <div class="actions">
        <button id="layout-close">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(back);
  const list = back.querySelector("#layout-list");
  for (const lay of layouts) {
    const row = document.createElement("div");
    row.className = "panel";
    row.style.padding = "12px";
    row.innerHTML = `
      <div style="display:flex; justify-content:space-between; gap:12px; align-items:center">
        <div>
          <div style="font-weight:600">${escapeHtml(lay.name)} <span class="tag">${escapeHtml(lay.id)}</span></div>
          <div style="color:var(--text-dim); font-size:12px">${escapeHtml(lay.description || "")}</div>
          <div style="color:var(--text-dim); font-size:11px; margin-top:4px">by ${escapeHtml(lay.author || "—")}${lay.rating ? ` · ★${lay.rating}` : ""}</div>
        </div>
        <button class="primary" data-pick="${escapeHtml(lay.id)}">${state.layout === lay.id ? "In use" : "Use"}</button>
      </div>
    `;
    list.appendChild(row);
  }
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#layout-close").onclick = close;
  list.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-pick]");
    if (!btn) return;
    const id = btn.getAttribute("data-pick");
    if (BUNDLED_LAYOUTS.has(id)) {
      state.layout = id;
      localStorage.setItem("homeos_layout", id);
      const sel = $("#layout");
      if (sel) {
        if (![...sel.options].some(o => o.value === id)) {
          const o = document.createElement("option");
          o.value = id; o.textContent = id;
          sel.appendChild(o);
        }
        sel.value = id;
      }
      close();
      await loadAll();
      await mountLayout(id);
    } else {
      showResponse(`Layout "${id}" isn't bundled yet — marketplace install lands in Wave 3.`, true);
    }
  });
}

// ---------------------------------------------------------------------------
// TTS playback — server returns a one-shot /api/tts-audio/{token} URL.
// Autoplay works because askVoice was triggered by a user gesture (button click).
// ---------------------------------------------------------------------------

let _ttsPlayer = null;
function playTTS(url) {
  try {
    if (_ttsPlayer) { _ttsPlayer.pause(); _ttsPlayer = null; }
    const a = new Audio(url);
    a.autoplay = true;
    _ttsPlayer = a;
    a.play().catch(err => console.warn("TTS autoplay blocked:", err));
  } catch (e) {
    console.warn("TTS playback failed:", e);
  }
}

// FIFO audio queue for the streaming voice-ask flow. Each sentence's TTS
// arrives separately; we play them strictly in arrival order so the response
// stays coherent even if a later sentence finishes synthesizing first.
const _audioQueue = [];
let _audioPlaying = false;
function enqueueTTS(url) {
  _audioQueue.push(url);
  if (!_audioPlaying) _playNextInQueue();
  _updateVoiceBtn();
}
function _playNextInQueue() {
  if (!_audioQueue.length) { _audioPlaying = false; _updateVoiceBtn(); return; }
  _audioPlaying = true;
  _updateVoiceBtn();
  const url = _audioQueue.shift();
  if (_ttsPlayer) { _ttsPlayer.pause(); }
  const a = new Audio(url);
  _ttsPlayer = a;
  a.onended = _playNextInQueue;
  a.onerror = _playNextInQueue;
  a.play().catch(err => { console.warn("TTS chunk autoplay blocked:", err); _playNextInQueue(); });
}
function resetTTSQueue() {
  _audioQueue.length = 0;
  if (_ttsPlayer) { _ttsPlayer.pause(); _ttsPlayer = null; }
  _audioPlaying = false;
  _updateVoiceBtn();
}

// The floating mic has three visual states:
//   idle      — neutral dark pill: 🎙 + "Talk"
//   recording — red pulsing pill:  ■ + "REC Ns"
//   playing   — accent pill:       ⏹ + "STOP"
// setVoiceBtn() updates the icon+label inside the new FAB markup;
// _updateVoiceBtn() switches between idle and playing (askVoice owns the
// recording state).
function setVoiceBtn(iconName, label) {
  const btn = document.getElementById("voice");
  if (!btn) return;
  const ic = btn.querySelector(".voice-ic");
  const lb = btn.querySelector(".voice-lbl");
  // iconName may be a Lucide symbol id ("mic", "stop") or a literal string
  // ("■") for the in-recording timer display. Detect via known set.
  if (ic) {
    const knownIcons = new Set(["mic", "stop"]);
    ic.innerHTML = knownIcons.has(iconName)
      ? `<svg class="icon"><use href="#i-${iconName}"/></svg>`
      : iconName;
  }
  if (lb) lb.textContent = label;
}
function _updateVoiceBtn() {
  const btn = document.getElementById("voice");
  if (!btn) return;
  if (state.recorder) return;  // recording state owned by askVoice's tick
  if (_audioPlaying) {
    setVoiceBtn("stop", "STOP");
    btn.title = "Stop voice playback";
    btn.classList.add("playing");
    btn.classList.remove("recording");
  } else {
    setVoiceBtn("mic", "Talk");
    btn.title = "Tap to talk to Yorik";
    btn.classList.remove("playing");
  }
}

// Esc key also stops voice playback — useful on desktops/keyboards.
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && _audioPlaying) {
    resetTTSQueue();
  }
});

// ---------------------------------------------------------------------------
// Voice profiles modal — list profiles, enroll/re-enroll voices, set language.
// ---------------------------------------------------------------------------

const SUPPORTED_LANGS = [
  { code: "en", label: "English" },
  { code: "de", label: "Deutsch" },
  { code: "fr", label: "Français" },
  { code: "es", label: "Español" },
  { code: "it", label: "Italiano" },
];

async function openVoiceProfilesModal() {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" style="max-width:600px">
      <h3>🎤 Voice profiles</h3>
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:8px">
        Enroll a 15-second voice sample so the system recognizes you and answers in your language automatically.
        Recording works only when you've granted microphone access.
      </div>
      <label style="display:flex; align-items:center; gap:8px; padding:8px 12px; background:rgba(255,255,255,0.04); border-radius:8px; margin-bottom:10px; cursor:pointer">
        <input type="checkbox" id="vp-id-toggle">
        <span style="font-size:13px">
          Identify speaker on every voice command
          <div style="color:var(--text-dim); font-size:11px; margin-top:2px">
            Off = skip the ECAPA speaker-ID step entirely (faster, but voice commands always use the role selected in the header).
          </div>
        </span>
      </label>
      <div id="vp-list" style="display:flex; flex-direction:column; gap:8px; max-height:55vh; overflow:auto"></div>
      <div class="actions">
        <button id="vp-close">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(back);
  const close = () => { back.remove(); };
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#vp-close").onclick = close;

  // Voice-ID toggle: read current value, wire the change.
  const toggle = back.querySelector("#vp-id-toggle");
  try {
    const s = await fetchJSON("/api/settings/voice_id_enabled");
    toggle.checked = (s.value === null || s.value === "1");
  } catch {
    toggle.checked = true;
  }
  toggle.addEventListener("change", async () => {
    try {
      await fetch(`/api/settings/voice_id_enabled?role=${state.role}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: toggle.checked ? "1" : "0" }),
      });
    } catch (e) {
      alert(`Failed to save: ${e.message}`);
    }
  });

  await refreshVoiceProfilesList(back);
}

async function refreshVoiceProfilesList(back) {
  const list = back.querySelector("#vp-list");
  list.innerHTML = `<div style="color:var(--text-dim)">Loading…</div>`;
  let profiles;
  try {
    profiles = await fetchJSON("/api/voice-profiles");
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">Failed to load: ${escapeHtml(e.message)}</div>`;
    return;
  }
  list.innerHTML = "";
  for (const p of profiles) {
    const langOpts = SUPPORTED_LANGS.map(L =>
      `<option value="${L.code}" ${p.language === L.code ? "selected" : ""}>${L.label}</option>`
    ).join("");
    const row = document.createElement("div");
    row.className = "panel";
    row.style.padding = "12px";
    row.innerHTML = `
      <div style="display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:8px">
        <div>
          <div style="font-weight:600">${escapeHtml(p.name)} <span class="tag">${escapeHtml(p.role)}</span></div>
          <div style="color:var(--text-dim); font-size:12px">
            ${p.enrolled ? "✓ voice enrolled" : "○ not enrolled"}
            ${p.email ? ` · ${escapeHtml(p.email)}` : ""}
          </div>
        </div>
        <select data-lang="${p.id}" style="font-size:12px">${langOpts}</select>
      </div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
        <button class="primary" data-record="${p.id}">${p.enrolled ? "Re-record voice" : "Record voice (15s)"}</button>
        ${p.enrolled ? `<button class="danger" data-clear="${p.id}">Clear</button>` : ""}
        <span class="vp-status" data-status="${p.id}" style="color:var(--text-dim); font-size:12px"></span>
      </div>
    `;
    list.appendChild(row);
  }
  list.addEventListener("click", (e) => handleVPClick(e, back), { once: true });
  list.addEventListener("change", (e) => handleVPLangChange(e, back), { once: true });
}

async function handleVPClick(e, back) {
  const rec = e.target.closest("[data-record]");
  const clr = e.target.closest("[data-clear]");
  if (rec) {
    const id = Number(rec.getAttribute("data-record"));
    await enrollVoice(id, back);
  } else if (clr) {
    const id = Number(clr.getAttribute("data-clear"));
    if (!confirm("Clear this voice enrollment?")) {
      // re-attach the once-listener
      back.querySelector("#vp-list").addEventListener("click", (e) => handleVPClick(e, back), { once: true });
      return;
    }
    await fetch(`/api/voice-profile/${id}/enrollment?role=${state.role}`, { method: "DELETE" });
    await refreshVoiceProfilesList(back);
  } else {
    // not a recognized button — re-attach listener
    back.querySelector("#vp-list").addEventListener("click", (e) => handleVPClick(e, back), { once: true });
  }
}

async function handleVPLangChange(e, back) {
  const sel = e.target.closest("[data-lang]");
  if (!sel) {
    back.querySelector("#vp-list").addEventListener("change", (e) => handleVPLangChange(e, back), { once: true });
    return;
  }
  const id = Number(sel.getAttribute("data-lang"));
  const lang = sel.value;
  try {
    const r = await fetch(`/api/voice-profile/${id}/language?role=${state.role}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: lang }),
    });
    if (!r.ok) {
      const t = await r.text();
      alert(`Failed to set language: ${t}`);
    }
  } finally {
    await refreshVoiceProfilesList(back);
  }
}

async function enrollVoice(profileId, back) {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    alert("Browser does not support audio recording.");
    return;
  }
  const statusEl = back.querySelector(`[data-status="${profileId}"]`);
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    statusEl.textContent = `Mic error: ${e.message}`;
    return;
  }
  const rec = new MediaRecorder(stream);
  const chunks = [];
  rec.ondataavailable = (e) => chunks.push(e.data);
  const SECONDS = 15;
  let remaining = SECONDS;
  statusEl.style.color = "var(--danger)";
  statusEl.textContent = `● Recording… ${remaining}s left`;
  const tick = setInterval(() => {
    remaining -= 1;
    if (remaining > 0) statusEl.textContent = `● Recording… ${remaining}s left`;
  }, 1000);

  rec.onstop = async () => {
    clearInterval(tick);
    stream.getTracks().forEach(t => t.stop());
    statusEl.style.color = "var(--text-dim)";
    statusEl.textContent = "Uploading…";
    const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
    const fd = new FormData();
    fd.append("audio", blob, "enroll.webm");
    try {
      const r = await fetch(`/api/voice-profile/${profileId}/enroll?role=${state.role}`, { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) {
        statusEl.style.color = "var(--danger)";
        statusEl.textContent = `✗ ${j.detail || r.status}`;
        return;
      }
      statusEl.style.color = "var(--ok)";
      statusEl.textContent = `✓ Enrolled (${j.enrolled_seconds}s, dim ${j.embedding_dim})`;
      await refreshVoiceProfilesList(back);
    } catch (e) {
      statusEl.style.color = "var(--danger)";
      statusEl.textContent = `✗ ${e.message}`;
    }
  };

  rec.start();
  setTimeout(() => { if (rec.state === "recording") rec.stop(); }, SECONDS * 1000);
}

// ---------------------------------------------------------------------------
// Settings modal — installed connectors, permission grants, n8n status.
// ---------------------------------------------------------------------------

async function openSettingsModal({ initialTab = "account" } = {}) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" style="max-width:720px">
      <h3>⚙ Settings</h3>
      <div style="display:flex; gap:4px; margin-bottom:12px; flex-wrap:wrap">
        <button data-tab="account" class="primary">Account</button>
        <button data-tab="users">Users</button>
        <button data-tab="backup">Backup</button>
        <!-- Connectors moved to /r/settings (React shell) — removed
             from the legacy modal. -->
        <button data-tab="skills">Skills</button>
        <button data-tab="documents">Documents</button>
        <button data-tab="apps">Apps</button>
        <button data-tab="templates">Templates</button>
        <button data-tab="extensions">Extensions</button>
        <button data-tab="voices">Voices</button>
        <button data-tab="permissions">Permissions</button>
        <button data-tab="n8n">n8n</button>
      </div>
      <div id="settings-body" style="max-height:60vh; overflow:auto"></div>
      <div class="actions"><button id="settings-close">Close</button></div>
    </div>
  `;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#settings-close").onclick = close;
  const body = back.querySelector("#settings-body");
  const buttons = back.querySelectorAll("[data-tab]");
  function setActive(tab) {
    buttons.forEach(b => b.classList.toggle("primary", b.dataset.tab === tab));
  }
  async function showTab(tab) {
    setActive(tab);
    // Skeleton rows while the tab body renders — much smoother than the
    // "Loading…" text jump-cut to the real content.
    body.innerHTML = `
      <div class="skeleton-stack">
        <div class="skeleton title"></div>
        ${Array.from({ length: 4 }).map(() => `
          <div class="skeleton-row">
            <div class="skeleton avatar"></div>
            <div class="col">
              <div class="skeleton" style="width: 60%"></div>
              <div class="skeleton" style="width: 90%; height: 10px"></div>
            </div>
            <div class="skeleton pill"></div>
          </div>`).join("")}
      </div>`;
    if (tab === "account") body.innerHTML = await renderAccountTab();
    else if (tab === "users") body.innerHTML = await renderUsersTab();
    else if (tab === "backup") body.innerHTML = await renderBackupTab();
    else if (tab === "skills") body.innerHTML = await renderSkillsTab();
    else if (tab === "documents") body.innerHTML = await renderDocumentsTab();
    else if (tab === "apps") body.innerHTML = await renderAppsTab();
    else if (tab === "templates") body.innerHTML = await renderTemplatesTab();
    else if (tab === "extensions") body.innerHTML = await renderExtensionsTab();
    else if (tab === "voices") body.innerHTML = await renderVoicesTab();
    else if (tab === "permissions") body.innerHTML = await renderPermissionsTab();
    else if (tab === "n8n") body.innerHTML = await renderN8nTab();
    wireSettingsBody(body, back, tab);
  }
  buttons.forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
  showTab(initialTab);
}

function _fmtSize(n) {
  if (n == null) return "?";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

async function renderAccountTab() {
  // Self-service profile + password change. Works for every role.
  let me;
  try {
    me = await fetchJSON("/api/auth/me");
  } catch {
    return `<div class="error">Could not load your profile.</div>`;
  }
  const u = me.user || {};
  return `
    <div class="account-grid">
      <div class="account-section">
        <h4>Your profile</h4>
        <label class="auth-field">
          <span>Display name</span>
          <input id="account-name" type="text" value="${escapeHtml(u.name || "")}">
        </label>
        <label class="auth-field">
          <span>Email</span>
          <input type="email" value="${escapeHtml(u.email || "")}" disabled>
          <small class="muted">Email is the login id and cannot be changed here (admin can reset via Users).</small>
        </label>
        <label class="auth-field">
          <span>Role</span>
          <input type="text" value="${escapeHtml(u.role || "")}" disabled>
        </label>
        <label class="auth-field">
          <span>Language (for TTS + LLM replies)</span>
          <select id="account-language">
            <option value="en" ${u.language === "en" ? "selected" : ""}>English</option>
            <option value="de" ${u.language === "de" ? "selected" : ""}>Deutsch</option>
          </select>
        </label>
        <button id="account-save" class="primary">Save profile</button>
        <div id="account-status" class="muted" style="margin-top:8px"></div>
      </div>

      <div class="account-section">
        <h4>Change password</h4>
        <label class="auth-field">
          <span>Current password</span>
          <input id="pw-current" type="password" autocomplete="current-password">
        </label>
        <label class="auth-field">
          <span>New password</span>
          <input id="pw-new" type="password" autocomplete="new-password" minlength="8">
          <small class="muted">Minimum 8 characters. Changing the password kicks all your other sessions.</small>
        </label>
        <button id="pw-save" class="primary">Update password</button>
        <div id="pw-status" class="muted" style="margin-top:8px"></div>
      </div>
    </div>
  `;
}

async function renderBackupTab() {
  let s;
  try {
    s = await fetchJSON("/api/backup/status");
  } catch (e) {
    if (String(e.message).includes("403") || String(e.message).includes("401")) {
      return `<div class="empty-state"><div class="icon">🔒</div><div class="title">Admin only</div></div>`;
    }
    return `<div class="error">Failed to load backup status: ${escapeHtml(e.message)}</div>`;
  }
  const cfg = s.config || {};
  const target = s.target || {};
  const hist = s.history || [];
  const snaps = s.snapshots || [];

  const targetBanner = !cfg.passphrase_set
    ? `<div class="bk-banner bk-warn">⚠ Set a backup passphrase first. Without it backups can't run. Lose this passphrase and your backups are unrecoverable — save it somewhere safe (password manager / paper).</div>`
    : !target.available
      ? `<div class="bk-banner bk-warn">⚠ Target unavailable: ${escapeHtml(target.reason || "")}. ${target.is_external ? "Plug your backup drive in." : ""}</div>`
      : `<div class="bk-banner bk-ok">✓ Target ready · ${_fmtBytes(target.free_bytes)} free of ${_fmtBytes(target.total_bytes)}${target.is_external ? " · external drive" : ""}</div>`;

  return `
    ${targetBanner}
    <div class="bk-grid">
      <section class="bk-section">
        <h4>Target</h4>
        <label class="auth-field">
          <span>Where to write snapshots</span>
          <input id="bk-target" type="text" value="${escapeHtml(cfg.target_path || "")}" placeholder="/media/yorik/MyUsbStick/yorik-backups">
          <small class="muted">Recommended: an external SSD or USB stick. Path can be anywhere writable. Cloud-target option coming later.</small>
        </label>
        <button id="bk-save-target" class="primary">Save target</button>
      </section>

      <section class="bk-section">
        <h4>Passphrase</h4>
        <p class="muted small">
          The snapshot is encrypted with this passphrase using <code>age</code>.
          <strong>Yorik never shows it again</strong> — save it somewhere safe.
          Lose it → your backups are unreadable.
        </p>
        <label class="auth-field">
          <span>${cfg.passphrase_set ? "Replace passphrase" : "Set passphrase"} (min 8 chars)</span>
          <input id="bk-passphrase" type="password" autocomplete="new-password" minlength="8">
        </label>
        <button id="bk-save-pass">${cfg.passphrase_set ? "Replace" : "Set passphrase"}</button>
      </section>

      <section class="bk-section">
        <h4>Schedule</h4>
        <label class="auth-field">
          <span>Daily backup time (24h, empty = manual only)</span>
          <input id="bk-schedule" type="time" value="${escapeHtml(cfg.schedule || "")}">
          <small class="muted">Recommended: 03:00 — low-activity window.</small>
        </label>
        <label class="auth-field">
          <span>Keep this many most-recent snapshots (older are auto-deleted)</span>
          <input id="bk-retain" type="number" min="1" max="365" value="${cfg.retain_count || 30}">
        </label>
        <button id="bk-save-sched" class="primary">Save schedule</button>
      </section>

      <section class="bk-section">
        <h4>What to include</h4>
        <label class="bk-check">
          <input type="checkbox" id="bk-incl-photos" ${cfg.include_photos ? "checked" : ""}>
          <span>Photo library (Immich) — can be gigabytes, slow</span>
        </label>
        <label class="bk-check">
          <input type="checkbox" id="bk-incl-paperless" ${cfg.include_paperless ? "checked" : ""}>
          <span>Filed documents (Paperless data + media) — can be large</span>
        </label>
        <p class="muted small" style="margin:6px 0">
          Always included: SQLite databases, uploaded documents, briefings,
          and the credential key (don't share that one).
        </p>
        <button id="bk-save-incl">Save</button>
      </section>

      <section class="bk-section bk-run">
        <h4>Run now</h4>
        <button id="bk-run" class="primary" ${cfg.passphrase_set && target.available ? "" : "disabled"}>↗ Backup now</button>
        <span id="bk-run-status" class="muted small"></span>
      </section>
    </div>

    <h4 style="margin-top:24px">Recent backups</h4>
    ${hist.length === 0 ? `<div class="muted">No backups yet.</div>` : `
      <table class="users-table">
        <thead><tr><th>When</th><th>Status</th><th>Size</th><th>Took</th><th>Includes</th><th>File</th></tr></thead>
        <tbody>
          ${hist.map(h => `
            <tr ${h.status === "failed" ? 'style="opacity:.7"' : ""}>
              <td class="small">${escapeHtml((h.started_at || "").replace("T"," ").slice(0,16))}</td>
              <td>${h.status === "ok" ? `<span style="color:var(--ok)">✓ ok</span>`
                      : `<span style="color:var(--danger)" title="${escapeHtml(h.error || "")}">✕ failed</span>`}</td>
              <td class="small">${_fmtBytes(h.size_bytes)}</td>
              <td class="small">${h.duration_s ? h.duration_s.toFixed(1) + "s" : "—"}</td>
              <td class="small muted">${(h.includes || []).join(", ")}</td>
              <td class="small muted" title="${escapeHtml(h.filename || "")}">${escapeHtml((h.filename || "").slice(0,40))}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `}

    ${snaps.length > 0 ? `
      <h4 style="margin-top:18px">Snapshots on target (${snaps.length})</h4>
      <p class="muted small">To restore from one of these, run on the server:<br>
        <code>bash scripts/restore.sh /path/to/snapshot.tar.gz.age</code>
      </p>
      <table class="users-table">
        <thead><tr><th>File</th><th>Size</th><th>Created</th></tr></thead>
        <tbody>
          ${snaps.slice(0, 20).map(p => `
            <tr><td class="small">${escapeHtml(p.filename)}</td>
                <td class="small">${_fmtBytes(p.size_bytes)}</td>
                <td class="small">${escapeHtml((p.mtime || "").replace("T"," ").slice(0,16))}</td></tr>
          `).join("")}
        </tbody>
      </table>` : ""}
  `;
}

function _fmtBytes(n) {
  if (!n && n !== 0) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024**2) return (n/1024).toFixed(1) + " KB";
  if (n < 1024**3) return (n/1024/1024).toFixed(1) + " MB";
  return (n/1024**3).toFixed(2) + " GB";
}

async function renderUsersTab() {
  let users, externals;
  try {
    users = await fetchJSON("/api/users");
    // Fetch provisioning state for every user in parallel; pre-fold
    // into a map so the row renderer is cheap.
    externals = Object.fromEntries(await Promise.all(
      users.map(async u => {
        try {
          const x = await fetchJSON(`/api/users/${u.id}/externals`);
          return [u.id, x];
        } catch { return [u.id, null]; }
      })
    ));
  } catch (e) {
    if (String(e.message).includes("403") || String(e.message).includes("401")) {
      return `<div class="empty-state">
        <div class="icon">🔒</div>
        <div class="title">Admin only</div>
        <div class="subtitle">Ask an admin to manage users.</div>
      </div>`;
    }
    return `<div class="error">Failed to load users: ${escapeHtml(e.message)}</div>`;
  }
  const rows = users.map(u => {
    const ex = externals[u.id] || {};
    const pp = (ex.paperless || {}).linked ? '<span class="ext-badge ok">📄 Paperless</span>'
                                            : '<span class="ext-badge">📄 —</span>';
    const im = (ex.immich || {}).linked    ? '<span class="ext-badge ok">📷 Immich</span>'
                                            : '<span class="ext-badge">📷 —</span>';
    return `
    <tr data-uid="${u.id}" ${u.disabled ? 'style="opacity:.55"' : ""}>
      <td>
        <div class="user-row-name">${escapeHtml(u.name || "—")}</div>
        <div class="muted small">${escapeHtml(u.email || "(no email)")}</div>
        <div class="ext-badges">${pp} ${im}</div>
      </td>
      <td>
        <select class="user-role-sel" data-uid="${u.id}">
          ${["admin","member","child","employee","viewer"].map(r =>
            `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
        </select>
      </td>
      <td class="small muted">
        ${u.last_login_at ? escapeHtml(u.last_login_at.slice(0,16).replace("T"," ")) : "never"}
        <br>${u.active_sessions} active session${u.active_sessions === 1 ? "" : "s"}
      </td>
      <td>${u.has_password ? "✓ set" : "<em>none</em>"}</td>
      <td>
        <button class="user-act" data-act="provision-paperless" data-uid="${u.id}" title="Link Paperless">📄+</button>
        <button class="user-act" data-act="provision-immich" data-uid="${u.id}" title="Link Immich">📷+</button>
        <button class="user-act" data-act="reset" data-uid="${u.id}" title="Reset password">↻ pw</button>
        <button class="user-act" data-act="toggle" data-uid="${u.id}" title="${u.disabled ? "Enable" : "Disable"}">
          ${u.disabled ? "↑ enable" : "✕ disable"}
        </button>
        <button class="user-act" data-act="delete" data-uid="${u.id}" title="Delete user">🗑</button>
      </td>
    </tr>
  `;
  }).join("");
  return `
    <div class="users-toolbar">
      <button id="users-add" class="primary">+ Add user</button>
      <span class="muted small">Sessions are revoked when a user is disabled, has their password reset, or is deleted.</span>
    </div>
    <table class="users-table">
      <thead><tr><th>User</th><th>Role</th><th>Activity</th><th>Password</th><th>Actions</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <!-- Inline add-user panel — collapsed by default; shown when "+ Add user" clicked. -->
    <div id="users-add-form" hidden class="users-add-form">
      <h4>Create user</h4>
      <label class="auth-field"><span>Email</span><input id="new-email" type="email"></label>
      <label class="auth-field"><span>Name</span><input id="new-name" type="text"></label>
      <label class="auth-field"><span>Role</span>
        <select id="new-role">
          <option value="member">member</option>
          <option value="admin">admin</option>
          <option value="child">child</option>
          <option value="employee">employee</option>
          <option value="viewer">viewer</option>
        </select>
      </label>
      <label class="auth-field"><span>Initial password (≥8 chars)</span><input id="new-pw" type="password" minlength="8"></label>
      <label class="auth-field">
        <span>Auto-provision external accounts</span>
        <div class="auto-provision-checks">
          <label><input type="checkbox" id="new-paperless" checked> Paperless</label>
          <label><input type="checkbox" id="new-immich" checked> Immich</label>
        </div>
        <small class="muted">Creates matching accounts in Paperless + Immich with the same password, captures per-user API tokens, so Yorik scopes their searches to their own data.</small>
      </label>
      <div id="new-status" class="muted"></div>
      <button id="new-submit" class="primary">Create</button>
    </div>
  `;
}

async function renderSkillsTab() {
  // Catalogue of every loaded skill the current role can call. Each
  // card shows description + when-to-use + inputs schema; click a card
  // to expand the full markdown body (procedural instructions).
  try {
    const skills = await fetchJSON(`/api/skills?role=${state.role}`);
    if (!skills.length) {
      return `<div class="empty-state">
        <div class="icon">🧰</div>
        <div class="title">No skills loaded</div>
        <div class="subtitle">Drop a skill into <code>backend/skills/&lt;name&gt;/</code> with a skill.md + skill.py and restart.</div>
      </div>`;
    }
    const byTag = {};
    for (const s of skills) {
      const tags = s.tags?.length ? s.tags : ["other"];
      for (const t of tags) {
        (byTag[t] ||= []).push(s);
      }
    }
    let html = `<div class="skills-intro">Yorik can do <b>${skills.length}</b> things for your role. The chat agent picks the right skill automatically; click any card to see the procedural body the LLM follows.</div>`;
    html += `<div class="skills-grid">`;
    for (const s of skills.sort((a, b) => a.name.localeCompare(b.name))) {
      const inputsList = Object.entries(s.inputs || {})
        .map(([k, v]) => {
          const req = v.required ? "" : " <span class=\"skill-opt\">(opt)</span>";
          return `<code>${escapeHtml(k)}</code>${req}`;
        }).join(", ") || "<span class=\"skill-opt\">no args</span>";
      const tagsHtml = (s.tags || []).map(t => `<span class="skill-tag">${escapeHtml(t)}</span>`).join("");
      html += `
        <div class="skill-card" data-skill="${escapeHtml(s.name)}">
          <div class="skill-card-h">
            <code class="skill-name">${escapeHtml(s.name)}</code>
            ${tagsHtml}
          </div>
          <div class="skill-desc">${escapeHtml(s.description)}</div>
          <div class="skill-inputs"><b>inputs:</b> ${inputsList}</div>
          <details class="skill-details"><summary>when to use / cost / body</summary>
            <pre class="skill-when">${escapeHtml(s.when_to_use || "(no guidance)")}</pre>
            <div><b>cost:</b> ${escapeHtml(s.cost || "n/a")}</div>
            <div><b>permissions:</b> ${escapeHtml((s.permissions || []).join(", "))}</div>
            <div><b>side effects:</b> ${escapeHtml(s.side_effects || "none")}</div>
            <div class="skill-body-mount" data-mount="${escapeHtml(s.name)}">
              <a href="#" class="skill-load-body">Load procedural body…</a>
            </div>
          </details>
        </div>`;
    }
    html += `</div>`;
    return html;
  } catch (e) {
    return `<div class="error">Failed to load skills: ${escapeHtml(e.message)}</div>`;
  }
}

async function renderDocumentsTab() {
  try {
    const docs = await fetchJSON(`/api/documents?role=${state.role}`);
    const rows = docs.length === 0
      ? `<tr><td colspan="5">
           <div class="empty-state">
             <div class="icon">📄</div>
             <div class="title">No documents yet</div>
             <div class="subtitle">Drag a PDF, Word doc, or text file onto the zone below to upload + index.</div>
           </div>
         </td></tr>`
      : docs.map(d => `
          <tr style="border-bottom:1px solid rgba(255,255,255,0.04)">
            <td style="padding:8px"><strong>${escapeHtml(d.title)}</strong>
              <div style="color:var(--text-dim);font-size:11px">${escapeHtml(d.mime_type || "?")}</div></td>
            <td style="padding:8px">${_fmtSize(d.bytes)}</td>
            <td style="padding:8px">${d.chunk_count ?? 0} chunks</td>
            <td style="padding:8px"><span class="tag">${escapeHtml(d.allowed_roles || "")}</span></td>
            <td style="padding:8px; text-align:right; white-space:nowrap">
              <button data-doc-reindex="${d.id}">Reindex</button>
              <button data-doc-delete="${d.id}" class="danger">Delete</button>
            </td>
          </tr>`).join("");
    return `
      <div id="doc-drop" style="border:2px dashed var(--card-border); border-radius:12px; padding:24px; text-align:center; color:var(--text-dim); margin-bottom:12px; cursor:pointer">
        <div style="font-size:14px">📄  Drop a file here or click to upload</div>
        <div style="font-size:11px; margin-top:4px">PDF, DOCX, Markdown, plain text. Encrypted at rest if you back up data/.</div>
        <input type="file" id="doc-file-input" accept=".pdf,.docx,.md,.markdown,.txt" style="display:none">
        <div style="display:flex; gap:8px; justify-content:center; margin-top:10px; font-size:12px">
          <label>Visible to: <input id="doc-roles" value="admin" style="width:120px"></label>
          <label>Tags: <input id="doc-tags" placeholder="optional, comma-sep" style="width:160px"></label>
        </div>
      </div>
      <div id="doc-status" style="font-size:12px; margin-bottom:8px; min-height:18px"></div>
      <table style="width:100%; font-size:13px; border-collapse:collapse">
        <tr style="text-align:left; color:var(--text-dim); border-bottom:1px solid var(--card-border)">
          <th style="padding:6px">Title</th>
          <th style="padding:6px">Size</th>
          <th style="padding:6px">Index</th>
          <th style="padding:6px">Visible to</th>
          <th></th>
        </tr>
        ${rows}
      </table>
      <div style="margin-top:14px; padding-top:10px; border-top:1px solid var(--card-border)">
        <div style="font-size:12px; color:var(--text-dim); margin-bottom:6px">Try a search:</div>
        <div style="display:flex; gap:8px">
          <input id="doc-search-q" placeholder="what does the readme say about CSP?" style="flex:1">
          <button id="doc-search-btn" class="primary">Search</button>
        </div>
        <div id="doc-search-results" style="margin-top:10px; font-size:12px"></div>
      </div>
    `;
  } catch (e) {
    return `<div style="color:var(--danger); padding:12px">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

async function uploadDocument(file, allowedRoles, tags, statusEl) {
  statusEl.style.color = "var(--text-dim)";
  statusEl.textContent = `Uploading ${file.name} (${_fmtSize(file.size)})…`;
  const fd = new FormData();
  fd.append("file", file);
  const params = new URLSearchParams({
    role: state.role,
    title: file.name.replace(/\.[^.]+$/, ""),
    allowed_roles: allowedRoles || "admin",
    tags: tags || "",
  });
  try {
    const r = await fetch(`/api/documents/upload?${params}`, { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) {
      statusEl.style.color = "var(--danger)";
      statusEl.textContent = `✗ ${j.detail || ("HTTP " + r.status)}`;
      return false;
    }
    const idx = j.index_result || {};
    statusEl.style.color = "var(--ok)";
    statusEl.textContent = `✓ Indexed ${file.name} into ${idx.chunk_count || 0} chunks (${idx.embed_failed_count || 0} embed failures)`;
    return true;
  } catch (e) {
    statusEl.style.color = "var(--danger)";
    statusEl.textContent = `✗ ${e.message}`;
    return false;
  }
}

async function renderAppsTab() {
  try {
    const [apps, grants] = await Promise.all([
      fetchJSON(`/api/apps?role=${state.role}`),
      fetchJSON(`/api/app-grants`).catch(() => []),
    ]);
    const community = apps.filter(a => !a.bundled);
    const grantsByApp = {};
    for (const g of grants) (grantsByApp[g.app_id] ||= []).push(g);

    const builtinsBlurb = `
      <div style="color:var(--text-dim); font-size:11px; padding:6px 4px 14px">
        Bundled apps (Calendar, Chat, Documents) are part of Yorik core and don't appear here.
      </div>`;
    if (!community.length) {
      return `
        <div style="padding:8px 4px">
          <div class="empty-state">
            <div class="icon">🧩</div>
            <div class="title">No community apps installed</div>
            <div class="subtitle">Drop a folder into <code>apps/</code> and restart, or install one by path below.</div>
          </div>
          <div style="display:flex; gap:6px">
            <input id="app-install-path" placeholder="/absolute/path/to/your-app/" style="flex:1">
            <button id="app-install-btn" class="primary">Install</button>
          </div>
          <div id="app-install-status" style="font-size:11px; margin-top:6px; min-height:14px"></div>
          ${builtinsBlurb}
        </div>`;
    }
    return `
      <div style="padding:4px">
        ${community.map(a => {
          const gs = grantsByApp[a.id] || [];
          const grantsHtml = gs.length
            ? gs.map(g => `
                <span class="tag" style="margin-right:4px; font-size:10px">
                  ${escapeHtml(g.resource_type)}: ${escapeHtml(g.resource_db ? g.resource_db + "." : "")}${escapeHtml(g.resource_name)} (${escapeHtml(g.access)})
                  <a href="#" data-revoke-app-grant="${g.id}" style="color:var(--danger); margin-left:4px">×</a>
                </span>`).join("")
            : `<span style="color:var(--text-dim); font-size:11px">no extra permissions</span>`;
          return `
            <div style="border:1px solid var(--card-border); border-radius:10px; padding:12px; margin-bottom:10px">
              <div style="display:flex; align-items:center; gap:10px">
                <div style="font-size:24px">${escapeHtml(a.icon)}</div>
                <div style="flex:1">
                  <div style="font-weight:600">${escapeHtml(a.name)}</div>
                  <div style="color:var(--text-dim); font-size:11px">${escapeHtml(a.id)} · v${escapeHtml(a.version || "?")} · by ${escapeHtml(a.author || "?")}</div>
                </div>
                <button data-app-import="${escapeHtml(a.id)}">Import CSV…</button>
                <button data-app-uninstall="${escapeHtml(a.id)}" class="danger">Uninstall</button>
              </div>
              <div style="color:var(--text-dim); font-size:12px; margin-top:6px">${escapeHtml(a.description || "")}</div>
              <div style="margin-top:8px">${grantsHtml}</div>
            </div>`;
        }).join("")}
        <div style="padding:8px 4px; border-top:1px solid var(--card-border); margin-top:8px">
          <div style="color:var(--text-dim); font-size:11px; margin-bottom:4px">Install from local path:</div>
          <div style="display:flex; gap:6px">
            <input id="app-install-path" placeholder="/absolute/path/to/your-app/" style="flex:1">
            <button id="app-install-btn" class="primary">Install</button>
          </div>
          <div id="app-install-status" style="font-size:11px; margin-top:6px; min-height:14px"></div>
        </div>
        ${builtinsBlurb}
      </div>`;
  } catch (e) {
    return `<div style="color:var(--danger); padding:12px">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

async function openCsvImportWizard(appId) {
  // Pull manifest to discover the app's tables (best-effort) and any
  // bundled importer presets. The /import endpoint validates against the
  // actual schema, so we don't need to be perfect here.
  let manifest = {};
  try { manifest = await fetchJSON(`/api/apps/${appId}/manifest?role=${state.role}`); } catch {}
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" style="max-width:680px">
      <h3>Import CSV → ${escapeHtml(manifest.name || appId)}</h3>
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:12px">
        Upload a CSV, map columns to one of the app's tables, preview with dry-run, then commit.
      </div>
      <div style="display:flex; gap:8px; align-items:end; flex-wrap:wrap; margin-bottom:10px">
        <label style="flex:2; display:flex; flex-direction:column; gap:4px">
          <span style="font-size:11px; color:var(--text-dim)">CSV file</span>
          <input type="file" id="csv-file" accept=".csv,.tsv,.txt">
        </label>
        <label style="flex:1; display:flex; flex-direction:column; gap:4px">
          <span style="font-size:11px; color:var(--text-dim)">Target table</span>
          <input id="csv-table" placeholder="customers" value="customers">
        </label>
        <label style="display:flex; flex-direction:column; gap:4px">
          <span style="font-size:11px; color:var(--text-dim)">Delimiter</span>
          <select id="csv-delim"><option value=",">,</option><option value=";">;</option><option value="	">tab</option></select>
        </label>
        <label style="display:flex; flex-direction:column; gap:4px">
          <span style="font-size:11px; color:var(--text-dim)">On duplicate</span>
          <select id="csv-on-dup"><option value="skip">skip</option><option value="update">update</option><option value="error">error</option></select>
        </label>
      </div>
      <div style="margin-bottom:8px">
        <span style="font-size:11px; color:var(--text-dim)">Column mapping (CSV column → app column), one per line as <code>csv_col=app_col</code>:</span>
        <textarea id="csv-mapping" rows="6" style="width:100%; font-family:monospace; font-size:12px; margin-top:4px" placeholder="name=name&#10;email=email&#10;phone=phone"></textarea>
        <div style="font-size:11px; color:var(--text-dim); margin-top:4px">
          ${manifest.id ? `Hint: app "${escapeHtml(manifest.id)}" has its tables in <code>data/apps/${escapeHtml(manifest.id)}/data.db</code>.` : ""}
        </div>
      </div>
      <div class="actions">
        <button id="csv-dryrun" style="margin-right:auto">Preview (dry-run)</button>
        <button id="csv-cancel">Cancel</button>
        <button id="csv-commit" class="primary" disabled>Import</button>
      </div>
      <div id="csv-result" style="margin-top:10px; font-size:12px; max-height:200px; overflow:auto"></div>
    </div>`;
  document.body.appendChild(back);
  const close = () => back.remove();
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  back.querySelector("#csv-cancel").onclick = close;

  function parseMapping() {
    const lines = back.querySelector("#csv-mapping").value.trim().split(/\n/);
    const out = {};
    for (const line of lines) {
      if (!line.trim()) continue;
      const [k, v] = line.split("=").map(s => s.trim());
      if (k && v) out[k] = v;
    }
    return out;
  }

  async function runImport(dryRun) {
    const file = back.querySelector("#csv-file").files[0];
    const resultEl = back.querySelector("#csv-result");
    if (!file) {
      resultEl.innerHTML = `<span style="color:var(--danger)">Pick a CSV file first.</span>`;
      return;
    }
    const cols = parseMapping();
    if (!Object.keys(cols).length) {
      resultEl.innerHTML = `<span style="color:var(--danger)">Add at least one mapping line.</span>`;
      return;
    }
    const mapping = {
      table: back.querySelector("#csv-table").value.trim(),
      columns: cols,
      skip_first_row: true,
      on_duplicate: back.querySelector("#csv-on-dup").value,
      dry_run: dryRun,
      delimiter: back.querySelector("#csv-delim").value,
    };
    const fd = new FormData();
    fd.append("file", file);
    resultEl.innerHTML = `<span style="color:var(--text-dim)">${dryRun ? "Previewing" : "Importing"}…</span>`;
    const params = new URLSearchParams({
      mapping: JSON.stringify(mapping),
      role: state.role,
    });
    try {
      const r = await fetch(`/api/apps/${encodeURIComponent(appId)}/import?${params}`, { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) {
        resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${escapeHtml(j.detail || ("HTTP " + r.status))}</span>`;
        return;
      }
      const previewHtml = (j.preview || []).map(p =>
        `<div style="font-family:monospace; padding:2px 0; color:var(--text-dim)">${escapeHtml(JSON.stringify(p))}</div>`
      ).join("");
      const errorsHtml = (j.errors || []).map(e =>
        `<div style="color:var(--danger); font-size:11px">row ${e.row}: ${escapeHtml(e.error)}</div>`
      ).join("");
      resultEl.innerHTML = `
        <div style="color:${j.errors_count ? "var(--warn)" : "var(--ok)"}; font-weight:600">
          ${j.dry_run ? "Dry-run:" : "✓ Imported:"} ${j.imported} of ${j.rows_in_file} rows · ${j.errors_count} error(s)
        </div>
        ${previewHtml ? `<div style="margin-top:6px"><strong>Preview (first 5):</strong>${previewHtml}</div>` : ""}
        ${errorsHtml ? `<div style="margin-top:6px"><strong>Errors:</strong>${errorsHtml}</div>` : ""}
      `;
      if (j.dry_run && !j.errors_count) {
        back.querySelector("#csv-commit").disabled = false;
      }
      if (!j.dry_run) {
        back.querySelector("#csv-commit").disabled = true;
      }
    } catch (e) {
      resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${escapeHtml(e.message)}</span>`;
    }
  }

  back.querySelector("#csv-dryrun").onclick = () => runImport(true);
  back.querySelector("#csv-commit").onclick = () => runImport(false);
}

async function renderExtensionsTab() {
  let list = [];
  try { list = await fetchJSON("/api/extensions"); } catch (e) {}
  const rows = list.length === 0
    ? `<div class="empty-state">
         <div class="icon">🔌</div>
         <div class="title">No extensions on disk</div>
         <div class="subtitle">Extensions live in <code>extensions/</code> and add optional, regional, or domain-specific features (e.g. German e-invoice format, regional bank protocols).</div>
       </div>`
    : list.map(e => {
        const ok = e.loaded;
        const depsOk = e.deps?.all_met;
        const status = ok ? `<span class="tag" style="background:rgba(52,211,153,0.15);color:var(--ok)">active</span>`
                          : depsOk ? `<span class="tag" style="background:rgba(245,158,11,0.15);color:var(--warn)">deps ok, not yet loaded</span>`
                                   : `<span class="tag" style="background:rgba(245,158,11,0.15);color:var(--warn)">deps missing</span>`;
        const reqs = (e.python_requirements || []).map(escapeHtml).join(", ");
        const missing = (e.deps?.missing || []).map(escapeHtml).join(", ");
        return `
          <div style="padding:12px 14px; background:var(--card-2); border-radius:10px; margin-bottom:8px">
            <div style="display:flex; gap:10px; align-items:flex-start">
              <div style="flex:1; min-width:0">
                <div style="display:flex; gap:8px; align-items:baseline; flex-wrap:wrap">
                  <div style="font-weight:600; font-size:14px">${escapeHtml(e.name)}</div>
                  ${e.country ? `<span class="tag" style="font-size:10px">${escapeHtml(e.country)}</span>` : ""}
                  ${status}
                </div>
                <div style="font-size:12px; color:var(--text-dim); margin-top:4px; line-height:1.5">${escapeHtml(e.description || "")}</div>
                <div style="font-size:10px; color:var(--text-faint); margin-top:6px">
                  v${escapeHtml(e.version || "?")} · by ${escapeHtml(e.author || "?")}
                  ${reqs ? ` · requires: <code>${reqs}</code>` : ""}
                  ${missing ? `<br><span style="color:var(--warn)">missing: <code>${missing}</code></span>` : ""}
                  ${e.docs_url ? ` · <a href="${escapeHtml(e.docs_url)}" target="_blank" rel="noopener">docs</a>` : ""}
                </div>
              </div>
              ${depsOk
                ? ""
                : `<button class="primary" data-ext-install="${escapeHtml(e.id)}" style="padding:6px 12px; font-size:12px; flex-shrink:0">Install deps</button>`}
            </div>
          </div>
        `;
      }).join("");
  return `
    <div style="padding:4px">
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:12px">
        Extensions add optional regional or domain-specific capabilities (German ZUGFeRD e-invoices, future Swiss QR-Bill, etc.). Install only what you need — keeps your Yorik install lean.
      </div>
      ${rows}
      <div id="ext-install-status" style="font-size:12px; margin-top:10px; min-height:14px; white-space:pre-wrap; font-family:ui-monospace, monospace"></div>
    </div>`;
}

async function renderTemplatesTab() {
  let list = [];
  try {
    list = await fetchJSON("/api/compose/templates");
  } catch (e) {}
  const rows = list.length === 0
    ? `<div class="empty-state">
         <div class="icon">📋</div>
         <div class="title">No templates installed</div>
         <div class="subtitle">Templates ship as small JSON files in <code>templates/</code>. Install one below — or browse the community marketplace at <a href="https://github.com/winidi/yorik-community/tree/main/templates" target="_blank" rel="noopener">yorik-community</a>.</div>
       </div>`
    : list.map(t => `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:11px 12px; background:var(--card-2); border-radius:8px; margin-bottom:6px">
          <div style="flex:1; min-width:0">
            <div style="font-weight:600; font-size:13px">${escapeHtml(t.name)} <span class="tag" style="font-size:10px; margin-left:6px">${escapeHtml(t.id)}</span></div>
            <div style="font-size:11px; color:var(--text-dim); margin-top:3px">${escapeHtml(t.description || "(no description)")}</div>
            <div style="font-size:10px; color:var(--text-faint); margin-top:3px">v${escapeHtml(t.version || "?")} · by ${escapeHtml(t.author || "?")}${t.vertical ? " · vertical: " + escapeHtml(t.vertical) : ""}${(t.needs_apps || []).length ? " · needs: " + t.needs_apps.map(escapeHtml).join(", ") : ""}</div>
          </div>
          <button class="danger" data-tpl-del="${escapeHtml(t.id)}" style="padding:5px 10px; font-size:11px; flex-shrink:0">Delete</button>
        </div>
      `).join("");
  return `
    <div style="padding:4px">
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:12px">
        Compose templates are JSON files in <code>templates/</code> that the Compose app + LLM use to draft invoices, quotes, letters with your real data filled in.
      </div>
      ${rows}
      <div style="margin-top:18px; padding-top:14px; border-top:1px solid var(--card-border)">
        <div style="color:var(--text-dim); font-size:11px; margin-bottom:6px">Install from local file path:</div>
        <div style="display:flex; gap:6px; margin-bottom:8px">
          <input id="tpl-install-path" placeholder="/absolute/path/to/template.json" style="flex:1; padding:7px 11px; font-size:12px">
          <button id="tpl-install-btn" class="primary" style="padding:7px 14px; font-size:12px">Install</button>
        </div>
        <div style="color:var(--text-dim); font-size:11px; margin-bottom:6px">Or paste JSON directly:</div>
        <textarea id="tpl-install-json" placeholder='{"id":"my-template","name":"My template","version":"1.0","body_html":"<p>Hello</p>"}' style="width:100%; min-height:120px; font-family:ui-monospace, monospace; font-size:11px; padding:8px 10px; background:var(--card-2); border:1px solid var(--card-border); border-radius:8px; color:var(--text); resize:vertical"></textarea>
        <div style="display:flex; justify-content:flex-end; margin-top:6px">
          <button id="tpl-install-json-btn" class="primary" style="padding:7px 14px; font-size:12px">Install from JSON</button>
        </div>
        <div id="tpl-install-status" style="font-size:11px; margin-top:8px; min-height:14px"></div>
      </div>
    </div>`;
}

async function renderVoicesTab() {
  return `
    <div style="padding:4px">
      <div style="color:var(--text-dim); font-size:12px; margin-bottom:10px">
        Enroll a 15-second voice sample so Yorik recognizes you and answers in your language automatically. Recording needs microphone access.
      </div>
      <label style="display:flex; align-items:center; gap:8px; padding:8px 12px; background:rgba(255,255,255,0.04); border-radius:8px; margin-bottom:12px; cursor:pointer">
        <input type="checkbox" id="vp-id-toggle">
        <span style="font-size:13px">
          Identify speaker on every voice command
          <div style="color:var(--text-dim); font-size:11px; margin-top:2px">
            Off = skip the ECAPA speaker-ID step entirely (faster, but voice commands always use the role selected in the header).
          </div>
        </span>
      </label>
      <div id="vp-list" style="display:flex; flex-direction:column; gap:8px; max-height:55vh; overflow:auto"></div>
    </div>`;
}

async function renderPermissionsTab() {
  try {
    const list = await fetchJSON("/api/permissions");
    if (!list.length) {
      return `<div style="color:var(--text-dim); padding:12px">No layout-to-connector grants yet. Bundled layouts (Google, Apple) don't need them.</div>`;
    }
    return `
      <table style="width:100%; font-size:13px; border-collapse:collapse">
        <tr style="text-align:left; color:var(--text-dim); border-bottom:1px solid var(--card-border)">
          <th style="padding:6px">Layout</th>
          <th style="padding:6px">Connector</th>
          <th style="padding:6px">Granted</th>
          <th></th>
        </tr>
        ${list.map(g => `
          <tr style="border-bottom:1px solid rgba(255,255,255,0.04)">
            <td style="padding:8px"><code>${escapeHtml(g.layout_id)}</code></td>
            <td style="padding:8px"><strong>${escapeHtml(g.connector_name)}</strong></td>
            <td style="padding:8px; color:var(--text-dim)">${escapeHtml(g.granted_at)} by ${escapeHtml(g.granted_by_role)}</td>
            <td style="padding:8px; text-align:right">
              <button data-revoke="${escapeHtml(g.layout_id)}|${escapeHtml(g.connector_name)}" class="danger">Revoke</button>
            </td>
          </tr>`).join("")}
      </table>
    `;
  } catch (e) {
    return `<div style="color:var(--danger); padding:12px">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

async function renderN8nTab() {
  try {
    const s = await fetchJSON("/api/n8n/status");
    if (s.ok) {
      return `
        <div style="padding:12px">
          <div style="color:var(--ok)">✓ n8n reachable at <code>${escapeHtml(s.base_url)}</code></div>
          <div style="color:var(--text-dim); margin-top:8px; font-size:12px">
            OAuth-heavy connectors (Gmail, Slack, Twilio, etc.) will install themselves as
            n8n workflows. Power users can edit them visually at the link above.
          </div>
        </div>`;
    }
    return `
      <div style="padding:12px">
        <div style="color:var(--warn)">⚠ ${escapeHtml(s.error)}</div>
        <div style="color:var(--text-dim); margin-top:12px; font-size:12px; line-height:1.6">
          To enable n8n-backed connectors:
          <ol style="padding-left:20px">
            <li>Open <a href="http://localhost:5678" target="_blank">n8n</a></li>
            <li>Complete the first-run owner setup if you haven't</li>
            <li>Go to Settings → API → Create API Key</li>
            <li>Paste the key into <code>config.env</code> as <code>HOMEOS_N8N_API_KEY=…</code></li>
            <li>Restart Yorik (<code>bash start.sh</code>)</li>
          </ol>
        </div>
      </div>`;
  } catch (e) {
    return `<div style="color:var(--danger); padding:12px">Failed: ${escapeHtml(e.message)}</div>`;
  }
}

function wireSettingsBody(body, back, currentTab) {
  if (currentTab === "backup") {
    const refresh = () => { back.remove(); openSettingsModal({ initialTab: "backup" }); };
    const patch = async (payload) => {
      const r = await fetch("/api/backup/config", {
        method: "PATCH", headers: {"content-type": "application/json"},
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || `HTTP ${r.status}`);
      }
      return r.json();
    };
    body.querySelector("#bk-save-target")?.addEventListener("click", async () => {
      try {
        await patch({ target_path: body.querySelector("#bk-target").value.trim() });
        refresh();
      } catch (e) { alert("Save failed: " + e.message); }
    });
    body.querySelector("#bk-save-pass")?.addEventListener("click", async () => {
      const pw = body.querySelector("#bk-passphrase").value;
      if (pw.length < 8) { alert("Passphrase must be at least 8 characters."); return; }
      if (!confirm("Have you saved this passphrase somewhere safe?\n\nIf you lose it, your backups become unrecoverable. Yorik does NOT store it in a way you can recover.")) return;
      try {
        await patch({ passphrase: pw });
        body.querySelector("#bk-passphrase").value = "";
        refresh();
      } catch (e) { alert("Save failed: " + e.message); }
    });
    body.querySelector("#bk-save-sched")?.addEventListener("click", async () => {
      try {
        await patch({
          schedule: body.querySelector("#bk-schedule").value,
          retain_count: parseInt(body.querySelector("#bk-retain").value, 10) || 30,
        });
        refresh();
      } catch (e) { alert("Save failed: " + e.message); }
    });
    body.querySelector("#bk-save-incl")?.addEventListener("click", async () => {
      try {
        await patch({
          include_photos:    body.querySelector("#bk-incl-photos").checked,
          include_paperless: body.querySelector("#bk-incl-paperless").checked,
        });
        refresh();
      } catch (e) { alert("Save failed: " + e.message); }
    });
    body.querySelector("#bk-run")?.addEventListener("click", async () => {
      const btn = body.querySelector("#bk-run");
      const status = body.querySelector("#bk-run-status");
      btn.disabled = true;
      status.textContent = "Backing up — this can take a minute…";
      try {
        const r = await fetch("/api/backup/run", { method: "POST" });
        const d = await r.json();
        if (d.ok) {
          status.textContent = `✓ Done. ${_fmtBytes(d.size_bytes)} in ${d.duration_s.toFixed(1)}s`;
          setTimeout(refresh, 1500);
        } else {
          status.textContent = `✕ Failed: ${d.error || d.detail || "unknown"}`;
          btn.disabled = false;
        }
      } catch (e) {
        status.textContent = `✕ Failed: ${e.message}`;
        btn.disabled = false;
      }
    });
  }
  if (currentTab === "account") {
    // Save profile
    body.querySelector("#account-save")?.addEventListener("click", async () => {
      const name = body.querySelector("#account-name").value.trim();
      const language = body.querySelector("#account-language").value;
      const status = body.querySelector("#account-status");
      status.textContent = "Saving…";
      try {
        await fetchJSON("/api/profile", {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ name, language }),
        });
        status.textContent = "Saved. The header will refresh on next reload.";
        // Update the in-memory state + header so the change is visible without reload.
        const pillName = document.getElementById("user-pill-name");
        if (pillName && name) pillName.textContent = name;
      } catch (e) {
        status.textContent = "Failed: " + e.message;
      }
    });
    // Change password
    body.querySelector("#pw-save")?.addEventListener("click", async () => {
      const current_password = body.querySelector("#pw-current").value;
      const new_password = body.querySelector("#pw-new").value;
      const status = body.querySelector("#pw-status");
      if (!new_password || new_password.length < 8) {
        status.textContent = "New password must be at least 8 characters.";
        return;
      }
      status.textContent = "Updating…";
      try {
        await fetchJSON("/api/auth/change-password", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ current_password, new_password }),
        });
        status.textContent = "Password updated. You stayed logged in here; other sessions were kicked.";
        body.querySelector("#pw-current").value = "";
        body.querySelector("#pw-new").value = "";
      } catch (e) {
        status.textContent = "Failed: " + e.message;
      }
    });
  }
  if (currentTab === "users") {
    // Role change
    body.querySelectorAll(".user-role-sel").forEach(sel => {
      sel.addEventListener("change", async () => {
        const uid = sel.dataset.uid;
        try {
          await fetchJSON(`/api/users/${uid}`, {
            method: "PATCH", headers: {"content-type": "application/json"},
            body: JSON.stringify({ role: sel.value }),
          });
        } catch (e) {
          alert("Role change failed: " + e.message);
        }
      });
    });
    // Action buttons
    body.querySelectorAll(".user-act").forEach(btn => {
      btn.addEventListener("click", async () => {
        const uid = btn.dataset.uid;
        const act = btn.dataset.act;
        try {
          if (act === "reset") {
            const pw = prompt("New password for this user (min 8 chars):");
            if (!pw || pw.length < 8) return;
            await fetchJSON(`/api/users/${uid}/reset-password`, {
              method: "POST", headers: {"content-type":"application/json"},
              body: JSON.stringify({ new_password: pw }),
            });
            alert("Password reset. The user has been logged out everywhere.");
          } else if (act === "provision-paperless" || act === "provision-immich") {
            const service = act === "provision-paperless" ? "paperless" : "immich";
            const pw = prompt(`This will create / re-use an account for this user in ${service}, log in as them once, and store an API token. Enter the password they should use in ${service} (typically the same as their Yorik password):`);
            if (!pw || pw.length < 8) return;
            try {
              const r = await fetchJSON(`/api/users/${uid}/provision/${service}`, {
                method: "POST", headers: {"content-type":"application/json"},
                body: JSON.stringify({ password: pw }),
              });
              alert(`${service} linked. New user_id in ${service}: ${r[service+"_user_id"] || "?"}`);
              back.remove();
              openSettingsModal({ initialTab: "users" });
            } catch (e) {
              alert(`${service} provisioning failed: ${e.message}`);
            }
          } else if (act === "toggle") {
            const wasDisabled = btn.textContent.includes("enable");
            await fetchJSON(`/api/users/${uid}`, {
              method: "PATCH", headers: {"content-type":"application/json"},
              body: JSON.stringify({ disabled: !wasDisabled }),
            });
            back.remove();
            openSettingsModal({ initialTab: "users" });
          } else if (act === "delete") {
            if (!confirm("Permanently delete this user? Their sessions are revoked. WhatsApp / docs they owned remain (we'll handle reassignment in wave 3).")) return;
            await fetchJSON(`/api/users/${uid}`, { method: "DELETE" });
            back.remove();
            openSettingsModal({ initialTab: "users" });
          }
        } catch (e) {
          alert("Action failed: " + e.message);
        }
      });
    });
    // Add user toggle + submit
    body.querySelector("#users-add")?.addEventListener("click", () => {
      body.querySelector("#users-add-form").hidden = false;
    });
    body.querySelector("#new-submit")?.addEventListener("click", async () => {
      const email = body.querySelector("#new-email").value.trim();
      const name  = body.querySelector("#new-name").value.trim();
      const role  = body.querySelector("#new-role").value;
      const password = body.querySelector("#new-pw").value;
      const status = body.querySelector("#new-status");
      const auto_provision = [];
      if (body.querySelector("#new-paperless")?.checked) auto_provision.push("paperless");
      if (body.querySelector("#new-immich")?.checked)    auto_provision.push("immich");
      if (!email || !name || password.length < 8) {
        status.textContent = "Email, name and ≥8-char password are all required.";
        return;
      }
      status.textContent = auto_provision.length
        ? "Creating Yorik user + " + auto_provision.join(" + ") + " accounts…"
        : "Creating user…";
      try {
        const r = await fetchJSON("/api/users", {
          method: "POST", headers: {"content-type":"application/json"},
          body: JSON.stringify({ email, name, role, password, auto_provision }),
        });
        const prov = r.provisioning || {};
        const issues = Object.entries(prov).filter(([_k, v]) => v && v.ok === false);
        if (issues.length) {
          // Surface partial failures so the admin can fix and retry
          // via the per-service /provision endpoint.
          alert("User created, but some provisioning failed:\n\n" +
                issues.map(([k, v]) => `${k}: ${v.error}`).join("\n"));
        }
        back.remove();
        openSettingsModal({ initialTab: "users" });
      } catch (e) {
        status.textContent = "Failed: " + e.message;
      }
    });
  }
  if (currentTab === "skills") {
    // Lazy-load each skill's procedural body only when its <details> is
    // expanded and the "Load…" link is clicked — keeps the initial tab
    // render cheap even with dozens of skills.
    body.querySelectorAll(".skill-load-body").forEach(a => {
      a.addEventListener("click", async (e) => {
        e.preventDefault();
        const mount = a.closest(".skill-body-mount");
        const name = mount.dataset.mount;
        mount.innerHTML = `<div class="muted">loading…</div>`;
        try {
          const full = await fetchJSON(`/api/skills/${encodeURIComponent(name)}`);
          mount.innerHTML = `<pre class="skill-body">${escapeHtml(full.body || "(no body)")}</pre>`;
        } catch (err) {
          mount.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
        }
      });
    });
  }
  // Documents tab: drag-drop upload, reindex, delete, search.
  if (currentTab === "documents") {
    const dropZone = body.querySelector("#doc-drop");
    const fileInput = body.querySelector("#doc-file-input");
    const statusEl = body.querySelector("#doc-status");
    const rolesEl = body.querySelector("#doc-roles");
    const tagsEl = body.querySelector("#doc-tags");
    const refreshDocs = async () => {
      back.remove();
      openSettingsModal({ initialTab: "documents" });
    };
    if (dropZone) {
      dropZone.onclick = () => fileInput.click();
      dropZone.ondragover = (e) => { e.preventDefault(); dropZone.style.borderColor = "var(--accent)"; };
      dropZone.ondragleave = () => { dropZone.style.borderColor = "var(--card-border)"; };
      dropZone.ondrop = async (e) => {
        e.preventDefault();
        dropZone.style.borderColor = "var(--card-border)";
        for (const f of e.dataTransfer.files) {
          await uploadDocument(f, rolesEl?.value, tagsEl?.value, statusEl);
        }
        await refreshDocs();
      };
      fileInput.onchange = async (e) => {
        for (const f of e.target.files) {
          await uploadDocument(f, rolesEl?.value, tagsEl?.value, statusEl);
        }
        await refreshDocs();
      };
    }
    body.querySelectorAll("[data-doc-reindex]").forEach(btn => {
      btn.onclick = async () => {
        const id = btn.getAttribute("data-doc-reindex");
        statusEl.textContent = `Reindexing ${id}…`;
        const r = await fetch(`/api/documents/${id}/reindex?role=${state.role}`, { method: "POST" });
        const j = await r.json();
        statusEl.textContent = r.ok ? `✓ ${id}: ${j.chunk_count} chunks` : `✗ ${JSON.stringify(j)}`;
        statusEl.style.color = r.ok ? "var(--ok)" : "var(--danger)";
        await refreshDocs();
      };
    });
    body.querySelectorAll("[data-doc-delete]").forEach(btn => {
      btn.onclick = async () => {
        const id = btn.getAttribute("data-doc-delete");
        if (!confirm(`Delete document ${id} and its index?`)) return;
        await fetch(`/api/documents/${id}?role=${state.role}`, { method: "DELETE" });
        await refreshDocs();
      };
    });
    const searchBtn = body.querySelector("#doc-search-btn");
    const searchQ = body.querySelector("#doc-search-q");
    const searchRes = body.querySelector("#doc-search-results");
    const runSearch = async () => {
      const q = (searchQ?.value || "").trim();
      if (!q) return;
      searchRes.innerHTML = `<div style="color:var(--text-dim)">Searching…</div>`;
      const r = await fetch(`/api/documents/search?role=${state.role}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, k: 5 }),
      });
      const hits = await r.json();
      if (!Array.isArray(hits) || hits.length === 0) {
        searchRes.innerHTML = `<div style="color:var(--text-dim)">No matches.</div>`;
        return;
      }
      searchRes.innerHTML = hits.map(h => `
        <div style="padding:8px 0; border-top:1px solid rgba(255,255,255,0.05)">
          <div><strong>${escapeHtml(h.doc_title)}</strong>
               <span style="color:var(--text-dim); font-size:11px">chunk ${h.chunk_index} · dist ${h.distance}</span></div>
          <div style="color:var(--text-dim); margin-top:4px; white-space:pre-wrap">${escapeHtml((h.chunk_text || "").slice(0, 400))}…</div>
        </div>`).join("");
    };
    if (searchBtn) searchBtn.onclick = runSearch;
    if (searchQ) searchQ.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  }

  // Extensions tab wiring — one button per extension that runs the pip
  // install on demand. Output is shown in the status area for transparency
  // (some Python wheels can take a minute on first install).
  if (currentTab === "extensions") {
    const status = body.querySelector("#ext-install-status");
    body.querySelectorAll("[data-ext-install]").forEach(btn => {
      btn.onclick = async () => {
        const id = btn.getAttribute("data-ext-install");
        btn.disabled = true;
        const origLabel = btn.textContent;
        btn.textContent = "Installing…";
        status.style.color = "var(--text-dim)";
        status.textContent = `Running pip install for "${id}" — first run may take 30-90s…`;
        try {
          const r = await fetch(`/api/extensions/${encodeURIComponent(id)}/install?role=${state.role}`, { method: "POST" });
          const j = await r.json();
          if (j.ok) {
            status.style.color = "var(--ok)";
            status.textContent = `✓ Installed. The extension is now active.\n\n` + (j.stdout || "").slice(-800);
            setTimeout(() => { back.remove(); openSettingsModal({ initialTab: "extensions" }); }, 1400);
          } else {
            status.style.color = "var(--danger)";
            status.textContent = `✗ Install failed.\n\n${j.error || ""}\n\n${j.stderr || j.stdout || ""}`;
            btn.disabled = false;
            btn.textContent = origLabel;
          }
        } catch (e) {
          status.style.color = "var(--danger)";
          status.textContent = `✗ Network error: ${e.message}`;
          btn.disabled = false;
          btn.textContent = origLabel;
        }
      };
    });
  }

  // Templates tab wiring — install by path / inline JSON, delete by id.
  if (currentTab === "templates") {
    const status = body.querySelector("#tpl-install-status");
    const setStatus = (msg, ok = true) => {
      if (!status) return;
      status.style.color = ok ? "var(--ok)" : "var(--danger)";
      status.textContent = msg;
    };
    const reload = async () => {
      back.remove();
      openSettingsModal({ initialTab: "templates" });
    };
    body.querySelector("#tpl-install-btn")?.addEventListener("click", async () => {
      const path = body.querySelector("#tpl-install-path").value.trim();
      if (!path) return;
      const r = await fetch(`/api/compose/templates/install?role=${state.role}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const j = await r.json();
      if (!r.ok) { setStatus(`✗ ${j.detail || r.status}`, false); return; }
      setStatus(`✓ installed: ${j.id}`);
      setTimeout(reload, 700);
    });
    body.querySelector("#tpl-install-json-btn")?.addEventListener("click", async () => {
      const raw = body.querySelector("#tpl-install-json").value.trim();
      if (!raw) return;
      let parsed;
      try { parsed = JSON.parse(raw); }
      catch (e) { setStatus(`✗ JSON parse: ${e.message}`, false); return; }
      const r = await fetch(`/api/compose/templates/install?role=${state.role}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: parsed }),
      });
      const j = await r.json();
      if (!r.ok) { setStatus(`✗ ${j.detail || r.status}`, false); return; }
      setStatus(`✓ installed: ${j.id}`);
      setTimeout(reload, 700);
    });
    body.querySelectorAll("[data-tpl-del]").forEach(btn => {
      btn.onclick = async () => {
        const id = btn.getAttribute("data-tpl-del");
        if (!confirm(`Delete template "${id}"?`)) return;
        await fetch(`/api/compose/templates/${encodeURIComponent(id)}?role=${state.role}`, { method: "DELETE" });
        reload();
      };
    });
  }

  // Voices tab wiring — reuses the same helpers as the (now-removed)
  // standalone modal; they accept any container as scope.
  if (currentTab === "voices") {
    const toggle = body.querySelector("#vp-id-toggle");
    if (toggle) {
      (async () => {
        try {
          const s = await fetchJSON("/api/settings/voice_id_enabled");
          toggle.checked = (s.value === null || s.value === "1");
        } catch { toggle.checked = true; }
      })();
      toggle.addEventListener("change", async () => {
        try {
          await fetch(`/api/settings/voice_id_enabled?role=${state.role}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: toggle.checked ? "1" : "0" }),
          });
        } catch (e) { alert(`Failed to save: ${e.message}`); }
      });
    }
    refreshVoiceProfilesList(body);
  }

  // Apps tab wiring
  if (currentTab === "apps") {
    body.querySelectorAll("[data-app-import]").forEach(btn => {
      btn.onclick = () => openCsvImportWizard(btn.getAttribute("data-app-import"));
    });
    body.querySelectorAll("[data-app-uninstall]").forEach(btn => {
      btn.onclick = async () => {
        const appId = btn.getAttribute("data-app-uninstall");
        if (!confirm(`Uninstall "${appId}" and wipe its data?\n\nThis removes data/apps/${appId}/data.db. The source under apps/${appId}/ stays on disk.`)) return;
        const r = await fetch(`/api/apps/${encodeURIComponent(appId)}?role=${state.role}&wipe_data=true`, { method: "DELETE" });
        if (r.ok || r.status === 204) {
          await loadAppRegistry();
          if (state.app === appId) openApp("home");
          back.remove();
          openSettingsModal({ initialTab: "apps" });
        } else {
          alert(`Failed: HTTP ${r.status}`);
        }
      };
    });
    body.querySelectorAll("[data-revoke-app-grant]").forEach(a => {
      a.onclick = async (e) => {
        e.preventDefault();
        const gid = a.getAttribute("data-revoke-app-grant");
        if (!confirm(`Revoke grant #${gid}? The app may stop working until re-granted.`)) return;
        await fetch(`/api/app-grants/${gid}?role=${state.role}`, { method: "DELETE" });
        back.remove();
        openSettingsModal({ initialTab: "apps" });
      };
    });
    const installBtn = body.querySelector("#app-install-btn");
    const installInput = body.querySelector("#app-install-path");
    const installStatus = body.querySelector("#app-install-status");
    if (installBtn) {
      installBtn.onclick = async () => {
        const path = installInput.value.trim();
        if (!path) return;
        installStatus.style.color = "var(--text-dim)";
        installStatus.textContent = "Installing…";
        const r = await fetch(`/api/apps/install?source_dir=${encodeURIComponent(path)}&role=${state.role}`, { method: "POST" });
        const j = await r.json().catch(() => ({}));
        if (r.ok) {
          installStatus.style.color = "var(--ok)";
          installStatus.textContent = `✓ Installed "${j.app_id}" with ${j.operations.length} operation(s)`;
          await loadAppRegistry();
          renderHomeView();
          setTimeout(() => { back.remove(); openSettingsModal({ initialTab: "apps" }); }, 900);
        } else {
          installStatus.style.color = "var(--danger)";
          installStatus.textContent = `✗ ${j.detail || ("HTTP " + r.status)}`;
        }
      };
    }
  }

  body.querySelectorAll("[data-revoke]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const [lid, cname] = btn.getAttribute("data-revoke").split("|");
      if (!confirm(`Revoke grant: ${lid} → ${cname}?`)) return;
      await fetch(`/api/permissions/${encodeURIComponent(lid)}/${encodeURIComponent(cname)}?role=${state.role}`, { method: "DELETE" });
      back.remove();
      openSettingsModal({ initialTab: "permissions" });
    });
  });
}


async function loadHealthConfig() {
  try {
    const h = await fetchJSON("/api/health");
    if (h.voice_max_seconds) state.voiceMaxSeconds = Number(h.voice_max_seconds);
  } catch { /* keep defaults */ }
}

// ---------------------------------------------------------------------------
// Layout iframe host — bridge between sandboxed layouts and the parent.
//
// Each layout runs in an iframe with sandbox="allow-scripts" (no allow-same-
// origin → null origin), with a meta-CSP that blocks fetch/XHR. The only way
// in/out is postMessage. The bridge below is inlined into every iframe's
// srcdoc; it exposes `window.yorik` to the layout code.
//
// Why iframes: even with our parent CSP, a community-uploaded layout running
// in the same realm could read localStorage, scrape the DOM, or call our API
// as the user. The iframe sandbox eliminates all of that. Bundled layouts go
// through the same path so we dogfood the sandbox from day one.
// ---------------------------------------------------------------------------

const LAYOUT_BRIDGE_JS = `
'use strict';
window.yorik = {
  events: [],
  tasks: [],
  opts: {},
  highlightIds: new Set(),
  _onUpdate: null,
  _waiters: new Map(),
  _nextReqId: 1,

  onUpdate(cb) {
    this._onUpdate = cb;
    if (this.opts && Object.keys(this.opts).length) {
      try { cb(); } catch (e) { console.error('[layout]', e); }
    }
  },

  selectEvent(ev) { parent.postMessage({_yorik:1, type:'event_clicked', event: ev}, '*'); },
  selectDay(date, events) { parent.postMessage({_yorik:1, type:'day_clicked', date: date, events: events || []}, '*'); },
  selectSlot(date, starts_at, ends_at) { parent.postMessage({_yorik:1, type:'slot_selected', date: date, starts_at: starts_at, ends_at: ends_at}, '*'); },
  navigate(detail) { parent.postMessage(Object.assign({_yorik:1, type:'nav'}, detail), '*'); },
  setHeight(px) { parent.postMessage({_yorik:1, type:'request_height', height: px}, '*'); },
  setView(view) { parent.postMessage({_yorik:1, type:'set_view', view: view}, '*'); },

  connector(name) {
    const self = this;
    return {
      get(params) {
        const id = self._nextReqId++;
        return new Promise(function(resolve, reject) {
          self._waiters.set(id, {resolve: resolve, reject: reject});
          parent.postMessage({_yorik:1, type:'connector_request', id: id, name: name, params: params || {}}, '*');
          setTimeout(function() {
            if (self._waiters.has(id)) {
              self._waiters.delete(id);
              reject(new Error('connector ' + name + ' timeout'));
            }
          }, 10000);
        });
      }
    };
  },
};

window.addEventListener('message', function(e) {
  const msg = e.data;
  if (!msg || msg._yorik !== 1) return;
  if (msg.type === 'state') {
    if (msg.events) window.yorik.events = msg.events;
    if (msg.tasks) window.yorik.tasks = msg.tasks;
    if (msg.opts) window.yorik.opts = msg.opts;
    if (msg.highlightIds) window.yorik.highlightIds = new Set(msg.highlightIds);
    if (window.yorik._onUpdate) {
      try { window.yorik._onUpdate(); } catch (e) { console.error('[layout]', e); }
    }
  } else if (msg.type === 'connector_response') {
    const w = window.yorik._waiters.get(msg.id);
    if (w) {
      window.yorik._waiters.delete(msg.id);
      if (msg.error) w.reject(new Error(msg.error));
      else w.resolve(msg.result);
    }
  } else if (msg.type === 'theme') {
    // Parent flipped the theme. Rewrite our :root with the new CSS vars
    // so calendar / community-app contents re-skin in lockstep, no reload.
    if (msg.vars) {
      let styleEl = document.getElementById('__yorik_theme__');
      if (!styleEl) {
        styleEl = document.createElement('style');
        styleEl.id = '__yorik_theme__';
        document.head.appendChild(styleEl);
      }
      styleEl.textContent = ':root { ' + msg.vars + ' }';
    }
  }
});

parent.postMessage({_yorik:1, type:'ready'}, '*');
`;

function themeVarsString() {
  const cs = getComputedStyle(document.documentElement);
  const vars = [
    '--bg','--bg-2','--bg-3',
    '--card','--card-2','--card-border','--card-border-strong',
    '--accent','--accent-2','--accent-dim','--accent-soft',
    '--text','--text-dim','--text-faint',
    '--danger','--ok','--warn','--info',
  ];
  return vars.map(v => `${v}: ${cs.getPropertyValue(v).trim()};`).join(' ');
}

function buildLayoutSrcdoc(layoutId, layoutJs) {
  // CSP inside the iframe: NO outbound network. NO eval. Inline styles+scripts
  // allowed only because that's how we ship the bridge + layout code together.
  const csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; font-src data:";
  return `<!doctype html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<title>${layoutId}</title>
<style>
:root { ${themeVarsString()} }
html, body { margin: 0; padding: 0; min-height: 100vh; background: transparent; color: var(--text);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', Roboto, sans-serif; }
*, *::before, *::after { box-sizing: border-box; }
button, select, input { font-family: inherit; }
</style>
</head>
<body>
<div id="root"></div>
<script>${LAYOUT_BRIDGE_JS}</script>
<script>${layoutJs}</script>
</body></html>`;
}

let _pendingConnectorRequests = 0;  // for debug

async function mountLayout(layoutId) {
  const container = $("#calendar");
  if (!container) return;
  let js;
  try {
    const r = await fetch(`/layouts/${encodeURIComponent(layoutId)}.js`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    js = await r.text();
  } catch (e) {
    container.innerHTML = `<div style="padding:20px;color:var(--danger)">Failed to load layout "${escapeHtml(layoutId)}": ${escapeHtml(e.message)}</div>`;
    return;
  }
  container.innerHTML = "";
  const iframe = document.createElement("iframe");
  iframe.id = "layout-iframe";
  iframe.dataset.layoutId = layoutId;
  iframe.style.width = "100%";
  iframe.style.height = "780px";
  iframe.style.border = "none";
  iframe.style.background = "transparent";
  iframe.setAttribute("sandbox", "allow-scripts");
  iframe.srcdoc = buildLayoutSrcdoc(layoutId, js);
  container.appendChild(iframe);
  state.layoutReady = false;
}

function postStateToLayout() {
  const iframe = document.getElementById("layout-iframe");
  if (!iframe?.contentWindow) return;
  iframe.contentWindow.postMessage({
    _yorik: 1,
    type: "state",
    events: state.events,
    tasks: state.tasks,
    opts: {
      month: state.month,
      year: state.year,
      anchorIso: state.anchorIso,
      view: state.view,
      role: state.role,
      layoutId: state.layout,
      availableLayouts: Array.from(BUNDLED_LAYOUTS),
    },
    highlightIds: Array.from(state.highlightIds || []),
  }, "*");
}

// Permission flow (Wave 3): the SERVER enforces grants via the
// connector_grants table. Client-side, we just forward the layout_id with
// every invoke; on 403 we open a modal asking the user, persist the grant,
// and retry. Bundled layouts are server-side exempted (see BUNDLED_LAYOUT_IDS
// in backend/main.py) so this only kicks in for marketplace layouts.

async function promptForConnectorGrant(layoutId, connectorName, description) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "modal-back";
    back.innerHTML = `
      <div class="modal" style="max-width:480px">
        <h3>Layout requests access</h3>
        <div style="color:var(--text-dim); font-size:13px; margin-bottom:12px">
          <strong>${escapeHtml(layoutId)}</strong> wants to use the
          <strong>${escapeHtml(connectorName)}</strong> connector.
        </div>
        <div style="background: rgba(255,255,255,0.04); padding:12px; border-radius:8px; font-size:12px; color:var(--text-dim); margin-bottom:12px">
          ${escapeHtml(description || "(no description provided)")}
        </div>
        <div style="font-size:11px; color:var(--text-dim); margin-bottom:8px">
          You'll only see this prompt once per layout + connector pair.
          Revoke later in <em>Permissions</em>.
        </div>
        <div class="actions">
          <button id="grant-deny">Deny</button>
          <button id="grant-allow" class="primary">Allow</button>
        </div>
      </div>
    `;
    document.body.appendChild(back);
    const close = (decision) => { back.remove(); resolve(decision); };
    back.addEventListener("click", (e) => { if (e.target === back) close(false); });
    back.querySelector("#grant-deny").onclick = () => close(false);
    back.querySelector("#grant-allow").onclick = async () => {
      try {
        await fetch(`/api/permissions?role=${state.role}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ layout_id: layoutId, connector_name: connectorName }),
        });
        close(true);
      } catch (e) {
        console.warn("grant failed:", e);
        close(false);
      }
    };
  });
}

// Find which of our sandboxed iframes a postMessage came from. Returns null
// for unknown sources so we ignore stray frames.
function _findSourceIframe(source) {
  for (const f of document.querySelectorAll("iframe[data-kind]")) {
    if (f.contentWindow === source) return f;
  }
  // Backwards compat: the calendar layout iframe doesn't yet have data-kind.
  const layoutIframe = document.getElementById("layout-iframe");
  if (layoutIframe && layoutIframe.contentWindow === source) return layoutIframe;
  return null;
}

window.addEventListener("message", async (e) => {
  const msg = e.data;
  if (!msg || msg._yorik !== 1) return;
  const iframe = _findSourceIframe(e.source);
  if (!iframe) return;
  const isLayout = iframe.id === "layout-iframe";
  const isApp = iframe.dataset.kind === "community-app";
  // For layout iframes, grants check uses layout_id. For app iframes, the
  // app's UI is trusted code the user installed — we pass __system__ to
  // bypass the per-layout connector_grants table (cross-app DB protections
  // still apply via app_grants in the SDK).
  const grantId = isApp ? "__system__" : (iframe.dataset.layoutId || "");
  switch (msg.type) {
    case "ready":
      if (isLayout) {
        state.layoutReady = true;
        postStateToLayout();
      }
      break;
    case "event_clicked":
      if (isLayout && msg.event) openEventModal({ event: msg.event });
      break;
    case "day_clicked":
      if (isLayout) document.dispatchEvent(new CustomEvent("homeos:day-selected", { detail: { date: msg.date, events: msg.events || [] } }));
      break;
    case "slot_selected":
      if (isLayout) openEventModal({ prefillStart: msg.starts_at, prefillEnd: msg.ends_at });
      break;
    case "new_event":
      if (isLayout) openEventModal({});
      break;
    case "set_layout":
      if (isLayout && msg.layout && BUNDLED_LAYOUTS.has(msg.layout) && msg.layout !== state.layout) {
        state.layout = msg.layout;
        localStorage.setItem("homeos_layout", state.layout);
        mountLayout(state.layout);  // re-mount the iframe with the new layout
      }
      break;
    case "nav":
      if (!isLayout) break;
      if (msg.anchorIso !== undefined) {
        document.dispatchEvent(new CustomEvent("homeos:week-change", { detail: { month: msg.month, year: msg.year, anchorIso: msg.anchorIso } }));
      } else if (msg.month !== undefined) {
        document.dispatchEvent(new CustomEvent("homeos:month-change", { detail: { month: msg.month, year: msg.year } }));
      }
      break;
    case "set_view":
      if (isLayout && msg.view && msg.view !== state.view) {
        state.view = msg.view;
        localStorage.setItem("homeos_gcal_view", state.view);
        loadAll();
      }
      break;
    case "request_height":
      // Fullscreen apps are sized by CSS (full viewport) — ignore their
      // self-sizing requests so the app can't accidentally shrink itself.
      if (iframe.dataset.chrome === "fullscreen") break;
      if (typeof msg.height === "number" && msg.height > 200 && msg.height < 4000) {
        iframe.style.height = msg.height + "px";
      }
      break;
    case "connector_request": {
      _pendingConnectorRequests++;
      const callConnector = async () => {
        const url = `/api/connectors/${encodeURIComponent(msg.name)}/invoke?role=${state.role}&layout_id=${encodeURIComponent(grantId)}`;
        const r = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ params: msg.params || {} }),
        });
        return { ok: r.ok, status: r.status, body: await r.json().catch(() => ({})) };
      };
      try {
        let r = await callConnector();
        if (r.status === 403 && r.body?.detail?.error === "connector_not_granted") {
          const granted = await promptForConnectorGrant(
            grantId, msg.name, r.body.detail.connector_description
          );
          if (!granted) {
            iframe.contentWindow.postMessage({ _yorik: 1, type: "connector_response", id: msg.id, error: "permission denied by user" }, "*");
            break;
          }
          r = await callConnector();
        }
        if (!r.ok) {
          const err = r.body?.detail || r.body?.error || `HTTP ${r.status}`;
          iframe.contentWindow.postMessage({ _yorik: 1, type: "connector_response", id: msg.id, error: typeof err === "string" ? err : JSON.stringify(err) }, "*");
        } else {
          iframe.contentWindow.postMessage({ _yorik: 1, type: "connector_response", id: msg.id, result: r.body }, "*");
        }
      } catch (err) {
        iframe.contentWindow.postMessage({ _yorik: 1, type: "connector_response", id: msg.id, error: err.message }, "*");
      } finally {
        _pendingConnectorRequests--;
      }
      break;
    }
  }
});

// ---------------------------------------------------------------------------
// APP ROUTER — top-level switching between Calendar / Chat / Docs / Home.
// ---------------------------------------------------------------------------

async function loadAppRegistry() {
  try {
    state.apps = await fetchJSON(`/api/apps?role=${state.role}`);
  } catch (e) {
    console.warn("apps registry failed:", e);
    state.apps = [];
  }
  renderDock();
}

const BUILTIN_APP_IDS = new Set(["home", "calendar", "chat", "docs", "compose", "whatsapp"]);

function setActiveApp(appId) {
  // Tear down per-app polling/overlays when leaving an app that owns
  // background timers (currently just WhatsApp).
  if (state.app === "whatsapp" && appId !== "whatsapp") {
    try { _waTeardown(); } catch (_) {}
  }
  state.app = appId;
  localStorage.setItem("yorik_app", appId);
  const isBuiltin = BUILTIN_APP_IDS.has(appId);
  // Toggle which view is visible. Builtins each have a dedicated section;
  // community apps share #community-app-view.
  const views = {
    home:     "#home-view",
    calendar: "#calendar-view",
    chat:     "#chat-view",
    docs:     "#docs-view",
    compose:  "#compose-view",
    whatsapp: "#whatsapp-view",
  };
  for (const [id, sel] of Object.entries(views)) {
    const el = document.querySelector(sel);
    if (el) el.hidden = (id !== appId);
  }
  const communityView = document.getElementById("community-app-view");
  if (communityView) communityView.hidden = isBuiltin;
  // Ask bar lives only on Chat. Move it into chat-main so it docks at the
  // bottom of the messages panel (proper chat-window layout); move it back
  // to its original sibling-before-stage spot otherwise so layout doesn't
  // jump if we later choose to show it elsewhere.
  const askForm = document.getElementById("ask-form");
  const responsePanel = document.getElementById("response");
  const stage = document.getElementById("app-stage");
  const chatMain = document.querySelector("#chat-view .chat-main");
  if (askForm) {
    askForm.hidden = (appId !== "chat");
    askForm.classList.toggle("ask--in-chat", appId === "chat");
    if (appId === "chat" && chatMain && askForm.parentElement !== chatMain) {
      chatMain.appendChild(askForm);
    } else if (appId !== "chat" && stage && askForm.parentElement !== document.body) {
      document.body.insertBefore(askForm, stage);
    }
  }
  // #response auto-hides when empty (CSS), so it can stay un-hidden — it'll
  // only show on non-chat pages when the user voice-asks something there.
  if (responsePanel) {
    responsePanel.hidden = (appId === "chat");  // chat-messages owns conversation display
  }
  // (Calendar-specific controls used to live in the Yorik header; they're
  // now drawn by each calendar layout inside its own iframe.)
  // Page title hint
  const app = state.apps.find(a => a.id === appId);
  document.title = appId === "home" ? "Yorik" : `Yorik · ${app?.name || appId}`;
  // chrome: fullscreen → hide Yorik's top header, give the iframe the
  // whole viewport. Bottom dock always stays as the platform's spine.
  const fullscreen = app?.chrome === "fullscreen";
  document.body.classList.toggle("chrome-fullscreen", !!fullscreen);
  // Reflect active app in the dock.
  renderDock();
}

// ─── Bottom dock ────────────────────────────────────────────────────────
// Mac-style fixed dock with one tile per installed app. Builtins first
// (Home / Calendar / Chat / Docs), divider, then community apps in
// registry order. Re-rendered whenever state.apps or state.app changes.

// Preferred display order for known bundled apps. Any bundled app not
// listed here still appears in the dock — appended after these.
const _DOCK_ORDER = ["home", "calendar", "chat", "docs", "compose", "photos", "whatsapp", "email", "briefing"];

// "home" is a virtual destination — not in /api/apps — so we inject it as
// a synthetic dock entry on every render so the user always has a way back.
const _HOME_TILE = { id: "home", name: "Home", icon: "🏠", bundled: true };

function renderDock() {
  const dock = document.getElementById("dock");
  if (!dock) return;
  if (!state.apps?.length) { dock.innerHTML = ""; return; }
  const byId = Object.fromEntries([_HOME_TILE, ...state.apps].map(a => [a.id, a]));
  const builtinsOrdered = _DOCK_ORDER.map(id => byId[id]).filter(Boolean);
  const builtinsExtra = state.apps.filter(a => a.bundled && !_DOCK_ORDER.includes(a.id));
  const builtins = [...builtinsOrdered, ...builtinsExtra];
  const community = state.apps.filter(a => !a.bundled && !_DOCK_ORDER.includes(a.id));
  const tile = (a) => `
    <button class="dock-tile ${state.app === a.id ? "active" : ""}" data-app="${escapeHtml(a.id)}" aria-label="${escapeHtml(a.name)}">
      <span class="dock-icon">${escapeHtml(a.icon || "▣")}</span>
      <span class="dock-tooltip">${escapeHtml(a.name)}</span>
    </button>`;
  dock.innerHTML =
    builtins.map(tile).join("") +
    (community.length ? `<span class="dock-divider"></span>` + community.map(tile).join("") : "");
  dock.querySelectorAll("[data-app]").forEach(el => {
    el.addEventListener("click", () => openApp(el.getAttribute("data-app")));
  });
}

// ─── URL routing ────────────────────────────────────────────────────────
// Each app gets a slug-based URL: / → home, /calendar, /chat, /documents,
// /<community-app-id>. openApp() pushes history state; popstate handles
// browser back/forward; initial load reads location.pathname.
//
// Special case: the "docs" app id is served at /documents because FastAPI's
// auto Swagger UI lives at /docs and would shadow the SPA route.
const _APP_URL_OVERRIDES = { docs: "/documents" };
const _APP_URL_REVERSE = Object.fromEntries(
  Object.entries(_APP_URL_OVERRIDES).map(([id, path]) => [path.replace(/^\//, ""), id])
);

function _appIdFromPath(pathname) {
  // "/" → home, "/chat" → "chat", "/documents" → "docs"
  const slug = (pathname || "/").replace(/^\/+|\/+$/g, "");
  if (slug === "") return "home";
  return _APP_URL_REVERSE[slug] || slug;
}

function _pathForAppId(appId) {
  if (appId === "home") return "/";
  return _APP_URL_OVERRIDES[appId] || "/" + appId;
}

async function openApp(appId, { push = true } = {}) {
  // Apps that live in the React shell (frontend-react) need a hard
  // navigate to /r/<route>. Anything not in this map is handled
  // inline as before.
  const REACT_APPS = { home: "/r/home", email: "/r/email", whatsapp: "/r/whatsapp", calendar: "/r/calendar", chat: "/r/chat", docs: "/r/documents", compose: "/r/compose", photos: "/r/photos", briefing: "/r/briefing", settings: "/r/settings" };
  if (REACT_APPS[appId]) {
    window.location.href = REACT_APPS[appId];
    return;
  }
  // Lazy-mount each app's content. mountApp is called fresh because
  // app state may have changed since the last visit.
  const mount = async () => {
    if (appId === "home") {
      renderHomeView();
      setActiveApp("home");
      return true;
    }
    if (appId === "calendar") {
      setActiveApp("calendar");
      if (!document.querySelector("#layout-iframe")) {
        await mountLayout(state.layout);
      }
      return true;
    }
    if (appId === "chat") {
      setActiveApp("chat");
      await mountChatApp();
      return true;
    }
    if (appId === "docs") {
      setActiveApp("docs");
      await mountDocsApp();
      return true;
    }
    if (appId === "compose") {
      setActiveApp("compose");
      await mountComposeApp();
      return true;
    }
    if (appId === "whatsapp") {
      setActiveApp("whatsapp");
      await mountWhatsAppApp();
      return true;
    }
    // External-iframe apps (Photos → Immich, future Grafana etc.) point at
    // a non-Yorik origin. Everything else is a Wave-6b community app that
    // uses the window.yorik bridge.
    const app = state.apps.find(a => a.id === appId);
    if (!app) {
      showResponse(`No app "${appId}" installed.`, true);
      return false;
    }
    setActiveApp(appId);
    if (app.view_kind === "external_iframe") {
      mountExternalApp(app);
    } else {
      await mountCommunityApp(appId);
    }
    return true;
  };
  const ok = await mount();
  if (ok && push) {
    const path = _pathForAppId(appId);
    if (path !== location.pathname) {
      history.pushState({ app: appId }, "", path);
    }
  }
}

window.addEventListener("popstate", (e) => {
  // Browser back/forward — switch view WITHOUT re-pushing.
  const appId = e.state?.app || _appIdFromPath(location.pathname);
  openApp(appId, { push: false });
});

// External-iframe apps point at a separate origin (Immich, Grafana, etc.).
// The URL is derived per-host from the manifest `entry` field — we don't
// trust manifests to ship raw URLs because they're per-deployment (localhost
// uses raw ports, Tailscale uses :8443).
function _externalAppUrl(entry) {
  const proto = location.protocol;       // "http:" or "https:"
  const host = location.hostname;        // "localhost" or "<tailnet>.ts.net"
  switch (entry) {
    case "immich":
      // Immich runs on port 2283 (host-direct) or 8443 (Tailscale-served).
      // Same hostname, different port = cross-origin but iframe-allowed.
      return proto === "https:"
        ? `https://${host}:8443/`
        : `http://${host}:2283/`;
    case "paperless":
      // Paperless runs on port 8010 (host-direct) or 8444 (Tailscale-served).
      return proto === "https:"
        ? `https://${host}:8444/`
        : `http://${host}:8010/`;
    default:
      return null;
  }
}

function mountExternalApp(app) {
  const host = document.getElementById("community-app-host");
  if (!host) return;
  const url = _externalAppUrl(app.entry);
  if (!url) {
    host.innerHTML = `<div style="padding:24px;color:var(--danger)">No URL configured for external app "${escapeHtml(app.entry)}"</div>`;
    return;
  }
  host.innerHTML = "";
  const iframe = document.createElement("iframe");
  iframe.id = `app-iframe-${app.id}`;
  iframe.className = "app-iframe";
  iframe.dataset.appId = app.id;
  iframe.dataset.kind = "external-app";
  iframe.dataset.chrome = app.chrome || "embedded";
  iframe.style.width = "100%";
  iframe.style.border = "none";
  iframe.style.background = "transparent";
  // Height is driven by CSS now (.community-app-host fills viewport - chrome).
  // No sandbox — Immich/Paperless need cookies, popups, downloads. The
  // cross-origin boundary itself isolates them from Yorik's DOM.
  iframe.src = url;
  iframe.referrerPolicy = "no-referrer-when-downgrade";
  iframe.allow = "camera; microphone; clipboard-read; clipboard-write";
  host.appendChild(iframe);
}

async function mountCommunityApp(appId) {
  const host = document.getElementById("community-app-host");
  if (!host) return;
  host.innerHTML = `<div style="padding:24px;color:var(--text-dim)">Loading ${escapeHtml(appId)}…</div>`;
  let js;
  try {
    const r = await fetch(`/api/apps/${encodeURIComponent(appId)}/ui?role=${state.role}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    js = await r.text();
  } catch (e) {
    host.innerHTML = `<div style="padding:24px;color:var(--danger)">Failed to load app "${escapeHtml(appId)}": ${escapeHtml(e.message)}</div>`;
    return;
  }
  host.innerHTML = "";
  const iframe = document.createElement("iframe");
  iframe.id = `app-iframe-${appId}`;
  iframe.className = "app-iframe";
  iframe.dataset.appId = appId;
  iframe.dataset.kind = "community-app";
  iframe.style.width = "100%";
  iframe.style.border = "none";
  iframe.style.background = "transparent";
  // Fullscreen apps get sized by CSS (full viewport minus dock). Embedded
  // apps start at 720px and may request more via window.yorik.setHeight().
  const app = state.apps.find(a => a.id === appId);
  iframe.dataset.chrome = app?.chrome || "embedded";
  if (iframe.dataset.chrome !== "fullscreen") {
    iframe.style.height = "720px";
  }
  iframe.setAttribute("sandbox", "allow-scripts");
  iframe.srcdoc = buildLayoutSrcdoc(appId, js);
  host.appendChild(iframe);
}

// ---------------------------------------------------------------------------
// HOME app — the icon grid.
// ---------------------------------------------------------------------------

function renderHomeView() {
  const grid = document.getElementById("home-grid");
  const greet = document.getElementById("home-greeting");
  if (greet) {
    const h = new Date().getHours();
    const part = h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    greet.textContent = `${part}. What would you like to do?`;
  }
  if (!grid) return;
  grid.innerHTML = state.apps.map(a => `
    <div class="home-tile ${a.bundled ? "" : "community-tile"}" data-app="${escapeHtml(a.id)}">
      <div class="tile-icon">${escapeHtml(a.icon)}</div>
      <div class="tile-name">${escapeHtml(a.name)}</div>
      <div class="tile-desc">${escapeHtml(a.description)}</div>
    </div>
  `).join("");
  grid.querySelectorAll("[data-app]").forEach(el => {
    el.addEventListener("click", () => openApp(el.getAttribute("data-app")));
  });
}

// ---------------------------------------------------------------------------
// CHAT app — full-screen conversation view.
// ---------------------------------------------------------------------------

function _truncate(s, n = 80) { return s.length > n ? s.slice(0, n) + "…" : s; }

async function loadConversationHistory() {
  try {
    state.chat.history = await fetchJSON(`/api/conversations?role=${state.role}&limit=50`);
  } catch (e) {
    state.chat.history = [];
  }
  renderChatSidebar();
}

async function loadConversation(convId) {
  if (!convId) {
    state.chat.messages = [];
    state.chat.activeConvId = null;
    renderChatMessages();
    return;
  }
  try {
    const c = await fetchJSON(`/api/conversations/${encodeURIComponent(convId)}?role=${state.role}`);
    state.chat.messages = c.messages || [];
    state.chat.activeConvId = c.id;
    state.conversationId = c.id;
    sessionStorage.setItem("homeos_conversation_id", c.id);
  } catch (e) {
    state.chat.messages = [];
    state.chat.activeConvId = null;
  }
  renderChatMessages();
  renderChatSidebar();
}

function renderChatSidebar() {
  const list = document.getElementById("chat-history");
  if (!list) return;
  if (!state.chat.history.length) {
    list.innerHTML = `<div style="font-size:11px;color:var(--text-dim);padding:8px">No past conversations yet — start asking.</div>`;
    return;
  }
  list.innerHTML = state.chat.history.map(c => `
    <div class="conv ${c.id === state.chat.activeConvId ? "active" : ""}" data-conv="${escapeHtml(c.id)}">
      <div>${escapeHtml(_truncate(c.preview || "(empty)", 60))}</div>
      <div class="meta">${c.message_count} msg · ${escapeHtml(c.updated_at || "")}</div>
    </div>
  `).join("");
  list.querySelectorAll("[data-conv]").forEach(el => {
    el.addEventListener("click", () => loadConversation(el.getAttribute("data-conv")));
  });
}

function renderChatMessages() {
  const root = document.getElementById("chat-messages");
  if (!root) return;
  if (!state.chat.messages.length) {
    root.innerHTML = `
      <div class="chat-empty">
        <div class="empty-state">
          <div class="icon">💬</div>
          <div class="title">Talk to Yorik</div>
          <div class="subtitle">Ask anything — calendar, documents, weather, the system itself. The response is spoken back if your role has a voice profile.</div>
        </div>
      </div>
    `;
    return;
  }
  root.innerHTML = state.chat.messages.map(m => {
    const role = m.role === "user" ? "user" : (m.role === "system" ? "system" : "assistant");
    const ts = m.timestamp ? new Date(m.timestamp).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}) : "";
    const userInitial = (state.role || "Y")[0].toUpperCase();
    const avatar = role === "user" ? userInitial : role === "system" ? "!" : "Y";
    const who = role === "user" ? "You" : role === "system" ? "System" : "Yorik";
    return `
      <div class="chat-msg ${role}">
        <div class="avatar">${avatar}</div>
        <div class="body">
          <div class="who">${who}</div>
          <div class="content">${escapeHtml(m.content || "")}</div>
          ${ts ? `<div class="meta">${ts}</div>` : ""}
        </div>
      </div>`;
  }).join("");
  root.scrollTop = root.scrollHeight;
}

async function mountChatApp() {
  const newBtn = document.getElementById("chat-new");
  if (newBtn && !newBtn._wired) {
    newBtn.addEventListener("click", () => {
      // Start fresh: clear conversation_id, empty messages, load history list.
      state.conversationId = null;
      sessionStorage.removeItem("homeos_conversation_id");
      state.chat.messages = [];
      state.chat.activeConvId = null;
      renderChatMessages();
      renderChatSidebar();
    });
    newBtn._wired = true;
  }
  await loadConversationHistory();
  if (state.conversationId) await loadConversation(state.conversationId);
  else renderChatMessages();
}

function pushChatMessage(role, content) {
  state.chat.messages.push({
    role,
    content,
    timestamp: new Date().toISOString(),
  });
  if (state.app === "chat") renderChatMessages();
}

// ---------------------------------------------------------------------------
// DOCS app — full-screen documents browser.
// ---------------------------------------------------------------------------

async function mountDocsApp() {
  await loadDocsList();
  renderDocsView();
  wireDocsApp();
}

// ─── COMPOSE app ──────────────────────────────────────────────────────────
// AI-first document composer. TipTap editor in the middle, template picker
// left, source-data + action-buttons right. Wave 1 = editor mount + template
// list stub; Wave 2 (next sessions) = real templates, LLM draft, save/send.

let _composeEditor = null;
let _composeTemplates = [];
let _composeActiveTemplate = null;

// ─── WhatsApp app ───────────────────────────────────────────────────────
// Talks to /api/whatsapp/* (which proxies the Baileys bridge). State:
//   _waState.activeJid     — selected chat
//   _waState.statusTimer   — polling timer for QR/connected
//   _waState.chatsTimer    — polling timer for new messages (will switch
//                            to SSE/WS in phase 2)
const _waState = {
  activeJid: null,
  qrOverlay: null,
  // True once the user clicks outside the QR modal — suppress re-show
  // until they re-enter the WhatsApp app or the bridge reports a
  // connection status change. Prevents the modal from re-popping after
  // explicit dismissal.
  qrDismissed: false,
  // Phase 3: single WebSocket connection replaces the 3s/4s polling
  // pair. ws holds the active connection; reconnectAttempts drives
  // exponential backoff up to 10s; reconnectTimer is the scheduled
  // retry so we can cancel on teardown.
  ws: null,
  reconnectAttempts: 0,
  reconnectTimer: null,
};

async function mountWhatsAppApp() {
  // Wire one-time event handlers.
  if (!mountWhatsAppApp._wired) {
    mountWhatsAppApp._wired = true;
    document.getElementById("wa-refresh")?.addEventListener("click", _waRefreshChats);
    document.getElementById("wa-sync")?.addEventListener("click", _waSyncFromBridge);
    document.getElementById("wa-clear")?.addEventListener("click", _waClear);
    document.getElementById("wa-import")?.addEventListener("click", () => {
      document.getElementById("wa-import-file")?.click();
    });
    document.getElementById("wa-import-file")?.addEventListener("change", _waImport);
    document.getElementById("wa-backfill")?.addEventListener("click", _waBackfill);
    document.getElementById("wa-briefing")?.addEventListener("click", _waOpenBriefing);
    document.getElementById("wa-draft-regen")?.addEventListener("click", _waRegenerateDrafts);
    document.getElementById("wa-draft-discard")?.addEventListener("click", _waDiscardDrafts);
    document.getElementById("wa-send-form")?.addEventListener("submit", _waSend);
    document.getElementById("wa-draft-use")?.addEventListener("click", _waUseDraft);
  }

  // Fresh entry into the app — allow the QR modal to show again even if
  // it was dismissed in the previous session.
  _waState.qrDismissed = false;

  // Phase 3: open the WebSocket. The "hello" event delivers initial
  // connected/QR state inline so we don't need a separate status fetch.
  // Live message/chat events flow over the same channel — no more polling.
  _waOpenWs();

  // One-time chat list load. After this, updates arrive via WS events.
  await _waRefreshChats();
}

// Called by setActiveApp when navigating AWAY from WhatsApp — close the
// WS and tear down the QR overlay so neither lingers behind the next
// app the user opens.
function _waTeardown() {
  if (_waState.reconnectTimer) { clearTimeout(_waState.reconnectTimer); _waState.reconnectTimer = null; }
  if (_waState.ws) {
    try { _waState.ws.close(1000, "app left"); } catch (_) {}
    _waState.ws = null;
  }
  _waHideQrOverlay();
  _waState.qrDismissed = false;
  _waState.reconnectAttempts = 0;
}

// Open the WS to /api/whatsapp/ws. Reconnects with exponential backoff
// up to 10 seconds. Translates incoming events into local UI updates:
//   hello / connection_status / ready / disconnected / qr → status logic
//   message  → ingest landed; refresh chat list + active thread
//   chat     → contact-name backfill or chat ordering change
function _waOpenWs() {
  if (_waState.ws) {
    try { _waState.ws.close(); } catch (_) {}
    _waState.ws = null;
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/api/whatsapp/ws`;
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    _waScheduleReconnect();
    return;
  }
  _waState.ws = ws;

  ws.addEventListener("open", () => {
    _waState.reconnectAttempts = 0;
  });

  ws.addEventListener("message", (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch (_) { return; }
    _waHandleWsEvent(evt);
  });

  ws.addEventListener("close", () => {
    _waState.ws = null;
    // Only reconnect if the WhatsApp app is still active — _waTeardown
    // closes the socket cleanly on app exit and resets state.
    if (state.app === "whatsapp") _waScheduleReconnect();
  });

  ws.addEventListener("error", () => {
    // close handler does reconnect; nothing to do here.
  });
}

function _waScheduleReconnect() {
  if (_waState.reconnectTimer) return;
  const delay = Math.min(500 * Math.pow(1.7, _waState.reconnectAttempts), 10_000);
  _waState.reconnectAttempts += 1;
  _waState.reconnectTimer = setTimeout(() => {
    _waState.reconnectTimer = null;
    if (state.app === "whatsapp") _waOpenWs();
  }, delay);
}

function _waHandleWsEvent(evt) {
  const t = evt.type;
  const p = evt.payload || {};
  if (t === "hello") {
    _waApplyStatus(p);
    return;
  }
  if (t === "ready") {
    _waApplyStatus({ connected: true, me: p.me });
    return;
  }
  if (t === "disconnected") {
    _waApplyStatus({ connected: false, hasQr: false });
    return;
  }
  if (t === "qr") {
    _waApplyStatus({ connected: false, hasQr: true });
    return;
  }
  if (t === "message") {
    // A new message landed. Refresh the chat list so the row jumps to
    // the top with the new preview, and refresh the active thread if
    // this is the visible chat.
    _waRefreshChats();
    return;
  }
  if (t === "chat") {
    _waRefreshChats();
    return;
  }
  if (t === "drafts_updated") {
    // Auto-draft set was created or discarded — refresh chat-list badges
    // and the variant panel if we're currently viewing this chat.
    _waRefreshChats();
    if (p.chat_jid === _waState.activeJid) {
      _waLoadVariants(p.chat_jid);
    }
    return;
  }
}

// Translate a bridge-status snapshot into UI: show/hide QR overlay,
// keep behaviour parity with the old polling code path.
async function _waApplyStatus(s) {
  if (s.connected) {
    _waState.qrDismissed = false;
    _waHideQrOverlay();
    return;
  }
  if (_waState.qrDismissed) return;
  if (s.bridge_unreachable) {
    _waShowQrOverlay({ error: "WhatsApp bridge container isn't running. Start it with: docker compose up -d whatsapp-bridge" });
    return;
  }
  if (s.hasQr) {
    try {
      const qr = await fetch("/api/whatsapp/qr").then(r => r.json());
      _waShowQrOverlay({ qrPng: qr.qrPng });
    } catch (e) {
      _waShowQrOverlay({ error: "Could not fetch QR — check bridge logs." });
    }
  } else {
    _waShowQrOverlay({ pending: true });
  }
}

function _waShowQrOverlay({ qrPng, error, pending } = {}) {
  if (!_waState.qrOverlay) {
    const ov = document.createElement("div");
    ov.className = "wa-qr-overlay";
    ov.innerHTML = `
      <div class="wa-qr-card">
        <button type="button" class="wa-qr-close" aria-label="Close">×</button>
        <h2>Link your WhatsApp</h2>
        <p class="wa-qr-sub"></p>
        <div class="wa-qr-content"></div>
        <ol class="wa-qr-steps">
          <li>Open WhatsApp on your phone</li>
          <li>Tap <b>Settings → Linked Devices → Link a Device</b></li>
          <li>Scan the QR above</li>
        </ol>
      </div>`;
    // Dismiss on outside-click (clicking the overlay backdrop, not the
    // card) or on the × button. ESC also closes.
    const dismiss = () => { _waState.qrDismissed = true; _waHideQrOverlay(); };
    ov.addEventListener("click", (e) => { if (e.target === ov) dismiss(); });
    ov.querySelector(".wa-qr-close").addEventListener("click", dismiss);
    ov._escHandler = (e) => { if (e.key === "Escape") dismiss(); };
    document.addEventListener("keydown", ov._escHandler);
    document.body.appendChild(ov);
    _waState.qrOverlay = ov;
  }
  const sub = _waState.qrOverlay.querySelector(".wa-qr-sub");
  const content = _waState.qrOverlay.querySelector(".wa-qr-content");
  if (error) {
    sub.textContent = "Setup needed";
    content.innerHTML = `<div style="color:var(--danger);padding:20px;font-size:13px">${escapeHtml(error)}</div>`;
  } else if (pending) {
    sub.textContent = "Waiting for QR from bridge…";
    content.innerHTML = `<div style="padding:40px;color:var(--text-dim)">⏳</div>`;
  } else {
    sub.textContent = "Scan to connect — Yorik becomes a linked device on your WhatsApp account.";
    content.innerHTML = `<img src="${qrPng}" alt="WhatsApp pairing QR">`;
  }
}

function _waHideQrOverlay() {
  if (_waState.qrOverlay) {
    if (_waState.qrOverlay._escHandler) {
      document.removeEventListener("keydown", _waState.qrOverlay._escHandler);
    }
    _waState.qrOverlay.remove();
    _waState.qrOverlay = null;
  }
}

async function _waRefreshChats() {
  let chats, pending;
  try {
    [chats, pending] = await Promise.all([
      fetch("/api/whatsapp/chats").then(r => r.json()),
      fetch("/api/whatsapp/drafts/pending-counts").then(r => r.ok ? r.json() : {}),
    ]);
  } catch (e) {
    return;
  }
  pending = pending || {};
  const list = document.getElementById("wa-chats-list");
  if (!list) return;
  if (!chats.length) {
    list.innerHTML = `<div class="wa-empty">No conversations yet. Once you scan the QR and someone messages you, they'll appear here.</div>`;
    return;
  }
  list.innerHTML = chats.map(c => {
    const ts = c.last_message_ts ? new Date(c.last_message_ts * 1000) : null;
    const tsLabel = ts ? _waFmtTime(ts) : "";
    const name = c.name || c.jid.split("@")[0];
    const preview = c.last_message_text || "";
    const active = _waState.activeJid === c.jid ? "active" : "";
    const draftCount = pending[c.jid] || 0;
    const draftBadge = draftCount ? `<span class="wa-pending-badge" title="${draftCount} pending AI drafts">✨ ${draftCount}</span>` : "";
    return `
      <div class="wa-chat-item ${active}" data-jid="${escapeHtml(c.jid)}">
        ${_waAvatarHtml(c.jid, name)}
        <div>
          <div class="wa-chat-name">${escapeHtml(name)}</div>
          <div class="wa-chat-preview">${escapeHtml(preview)}</div>
        </div>
        <div class="wa-chat-meta" style="grid-column:2">
          <span class="wa-chat-ts">${tsLabel}</span>
          ${draftBadge}
          ${c.unread_count ? `<span class="wa-unread">${c.unread_count}</span>` : ""}
        </div>
      </div>`;
  }).join("");
  list.querySelectorAll("[data-jid]").forEach(el => {
    el.addEventListener("click", () => _waOpenChat(el.getAttribute("data-jid")));
  });
  // If the currently-open chat has new messages, refresh thread too.
  if (_waState.activeJid) _waLoadThread(_waState.activeJid);
}

async function _waOpenChat(jid) {
  _waState.activeJid = jid;
  // Track the draft variant the user picked (if any) so Send can mark
  // it as used and discard its siblings.
  _waState.activeDraftId = null;
  // Highlight in list
  document.querySelectorAll(".wa-chat-item").forEach(el => {
    el.classList.toggle("active", el.getAttribute("data-jid") === jid);
  });
  // Enable inputs
  document.getElementById("wa-send-input").disabled = false;
  document.getElementById("wa-send-btn").disabled = false;
  document.getElementById("wa-draft-text").disabled = false;
  // Update header — avatar + name
  const name = document.querySelector(`.wa-chat-item[data-jid="${CSS.escape(jid)}"] .wa-chat-name`)?.textContent || jid;
  const header = document.getElementById("wa-thread-h");
  if (header) {
    header.innerHTML = `${_waAvatarHtml(jid, name, "lg")}<span class="wa-thread-name">${escapeHtml(name)}</span>`;
  }
  await Promise.all([_waLoadThread(jid), _waLoadVariants(jid)]);
}

// Load pending auto-draft variants for the active chat and render them
// in the right pane. No-op if there are none.
async function _waLoadVariants(jid) {
  let data;
  try {
    data = await fetch(`/api/whatsapp/drafts/${encodeURIComponent(jid)}/pending`).then(r => r.json());
  } catch (e) {
    return;
  }
  const host = document.getElementById("wa-variants");
  const regenBtn = document.getElementById("wa-draft-regen");
  const discardBtn = document.getElementById("wa-draft-discard");
  if (regenBtn) regenBtn.disabled = false;
  if (!data || !data.variants?.length) {
    host.innerHTML = `<div class="wa-empty-sm">No pending drafts. Click ↻ to generate now, or wait for a new incoming message.</div>`;
    if (discardBtn) discardBtn.disabled = true;
    return;
  }
  if (discardBtn) discardBtn.disabled = false;
  host.innerHTML = data.variants.map(v => `
    <div class="wa-variant" data-draft-id="${v.id}">
      <div class="wa-variant-label">${escapeHtml(v.label)}</div>
      <div class="wa-variant-text">${escapeHtml(v.text)}</div>
    </div>
  `).join("");
  host.querySelectorAll("[data-draft-id]").forEach(el => {
    el.addEventListener("click", () => _waPickVariant(el, parseInt(el.dataset.draftId, 10)));
  });
  // Render sources panel.
  const src = document.getElementById("wa-draft-sources");
  if (data.sources?.length) {
    src.innerHTML = data.sources.map(s =>
      `<div class="wa-draft-src">${escapeHtml(s.snippet || s.kind)}</div>`
    ).join("");
  } else {
    src.innerHTML = `<div class="wa-empty-sm">No additional context used.</div>`;
  }
}

function _waPickVariant(el, draftId) {
  document.querySelectorAll(".wa-variant").forEach(v => v.classList.remove("active"));
  el.classList.add("active");
  const text = el.querySelector(".wa-variant-text")?.textContent || "";
  document.getElementById("wa-draft-text").value = text;
  document.getElementById("wa-send-input").value = text;
  document.getElementById("wa-draft-use").disabled = false;
  _waState.activeDraftId = draftId;
}

async function _waRegenerateDrafts() {
  if (!_waState.activeJid) return;
  const btn = document.getElementById("wa-draft-regen");
  btn.disabled = true;
  document.getElementById("wa-variants").innerHTML =
    `<div class="wa-empty-sm">Generating fresh variants…</div>`;
  try {
    const r = await fetch(`/api/whatsapp/drafts/${encodeURIComponent(_waState.activeJid)}/regenerate`, {
      method: "POST",
    });
    if (!r.ok) throw new Error(await r.text());
    // The autodraft module will broadcast drafts_updated → _waLoadVariants
    // is called via the WS handler. As a fallback, also call directly.
    await _waLoadVariants(_waState.activeJid);
  } catch (e) {
    showResponse(`Regenerate failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

async function _waDiscardDrafts() {
  if (!_waState.activeJid) return;
  try {
    await fetch(`/api/whatsapp/drafts/${encodeURIComponent(_waState.activeJid)}/discard`, {
      method: "POST",
    });
    await _waLoadVariants(_waState.activeJid);
  } catch (e) {
    showResponse(`Discard failed: ${e.message}`, true);
  }
}

// Build an <span class="wa-avatar"> with an <img> that gracefully falls
// back to initials if /api/whatsapp/avatar/:jid returns 404 (contact has
// no picture, hides it via privacy settings, etc.). Initials computed
// from the display name — first two leading letters of first two words.
function _waAvatarHtml(jid, name, size) {
  const initials = (name || jid || "?")
    .split(/[\s+()-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map(s => s[0] || "")
    .join("")
    .toUpperCase()
    .slice(0, 2);
  const cls = size === "lg" ? "wa-avatar wa-avatar--lg" : "wa-avatar";
  const url = `/api/whatsapp/avatar/${encodeURIComponent(jid)}`;
  return `<span class="${cls}" data-initials="${escapeHtml(initials)}">` +
         `<img src="${url}" alt="" onerror="this.parentElement.textContent=this.parentElement.dataset.initials">` +
         `</span>`;
}

async function _waLoadThread(jid) {
  let msgs;
  try {
    msgs = await fetch(`/api/whatsapp/chats/${encodeURIComponent(jid)}/messages?limit=50`).then(r => r.json());
  } catch (e) {
    return;
  }
  const host = document.getElementById("wa-thread-msgs");
  if (!host) return;
  if (!msgs.length) {
    host.innerHTML = `<div class="wa-empty">No messages in this chat yet.</div>`;
    return;
  }
  // Preserve scroll-at-bottom behavior — only autoscroll if user was already at bottom.
  const wasAtBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
  host.innerHTML = msgs.map(m => {
    const side = m.from_me ? "out" : "in";
    const ts = new Date(m.timestamp * 1000);
    const tsLabel = _waFmtTime(ts);
    let body = "";
    if (m.text) body = escapeHtml(m.text);
    else if (m.media_kind === "document") body = `<span class="wa-msg-media">📄 ${escapeHtml(m.filename || "Document")}</span>`;
    else if (m.media_kind === "image") body = `<span class="wa-msg-media">📷 Photo</span>`;
    else if (m.media_kind === "video") body = `<span class="wa-msg-media">🎥 Video</span>`;
    else if (m.media_kind === "audio") body = `<span class="wa-msg-media">🎙️ Voice message</span>${m.transcript ? `<div class="wa-msg-transcript">${escapeHtml(m.transcript)}</div>` : ""}`;
    else body = `<span class="wa-msg-media">[${escapeHtml(m.media_kind || "media")}]</span>`;
    // Auto-routing status badge: tells the user this media was filed
    // into Paperless/Immich and gives them a click-through.
    let badge = "";
    if (m.media_paperless_id) {
      badge = `<a class="wa-msg-badge" href="/documents" target="_blank" title="Filed to Paperless">📁 Filed</a>`;
    } else if (m.media_immich_id) {
      badge = `<a class="wa-msg-badge" href="/photos" target="_blank" title="Added to Photos">🖼️ In Photos</a>`;
    }
    const author = (!m.from_me && m.push_name) ? `<div class="wa-msg-author">${escapeHtml(m.push_name)}</div>` : "";
    return `<div class="wa-msg ${side}">${author}${body}${badge}<div class="wa-msg-ts">${tsLabel}</div></div>`;
  }).join("");
  if (wasAtBottom) host.scrollTop = host.scrollHeight;
}

async function _waSend(e) {
  e.preventDefault();
  const input = document.getElementById("wa-send-input");
  const text = input.value.trim();
  if (!text || !_waState.activeJid) return;
  const btn = document.getElementById("wa-send-btn");
  btn.disabled = true;
  try {
    const body = { text };
    // Pass draft_id only if the user explicitly picked a variant. If
    // they typed from scratch, leave it null so siblings aren't
    // affected (unrelated send).
    if (_waState.activeDraftId) body.draft_id = _waState.activeDraftId;
    await fetch(`/api/whatsapp/chats/${encodeURIComponent(_waState.activeJid)}/send`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(r => {
      if (!r.ok) throw new Error("send failed");
      return r.json();
    });
    input.value = "";
    document.getElementById("wa-draft-text").value = "";
    _waState.activeDraftId = null;
    await Promise.all([_waLoadThread(_waState.activeJid), _waLoadVariants(_waState.activeJid)]);
  } catch (err) {
    showResponse(`Send failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

async function _waGenerateDraft() {
  if (!_waState.activeJid) return;
  const btn = document.getElementById("wa-draft-gen");
  const ta = document.getElementById("wa-draft-text");
  btn.disabled = true;
  btn.textContent = "Thinking…";
  ta.value = "";
  try {
    const r = await fetch("/api/whatsapp/draft", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_jid: _waState.activeJid }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    ta.value = data.draft || "";
    document.getElementById("wa-draft-use").disabled = !data.draft;
    const srcHost = document.getElementById("wa-draft-sources");
    if (data.sources?.length) {
      srcHost.innerHTML = data.sources.map(s =>
        `<div class="wa-draft-src">${escapeHtml(s.snippet || s.kind)}</div>`
      ).join("");
    } else {
      srcHost.innerHTML = `<div class="wa-empty-sm">No additional context used.</div>`;
    }
  } catch (err) {
    showResponse(`Draft failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg class="icon"><use href="#i-sparkles"/></svg> Generate draft`;
  }
}

async function _waOpenBriefing() {
  // Modal overlay with the daily briefing. Generated on open, cached
  // per-session in _waState.lastBriefing to avoid re-spending tokens
  // if the user re-opens within a minute.
  let modal = document.getElementById("wa-briefing-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "wa-briefing-modal";
    modal.className = "wa-qr-overlay"; // reuse the same backdrop style
    modal.innerHTML = `
      <div class="wa-qr-card wa-briefing-card">
        <button type="button" class="wa-qr-close" aria-label="Close">×</button>
        <h2>📋 Inbox briefing</h2>
        <div class="wa-briefing-controls">
          <label>Last
            <select id="wa-briefing-window">
              <option value="6">6 hours</option>
              <option value="24" selected>24 hours</option>
              <option value="72">3 days</option>
              <option value="168">1 week</option>
            </select>
          </label>
          <button id="wa-briefing-refresh" class="primary">Refresh</button>
        </div>
        <div class="wa-briefing-body" id="wa-briefing-body">
          <div class="wa-empty-sm">Generating briefing…</div>
        </div>
      </div>`;
    const close = () => modal.remove();
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    modal.querySelector(".wa-qr-close").addEventListener("click", close);
    modal.querySelector("#wa-briefing-refresh").addEventListener("click", _waLoadBriefing);
    modal.querySelector("#wa-briefing-window").addEventListener("change", _waLoadBriefing);
    document.body.appendChild(modal);
  }
  await _waLoadBriefing();
}

async function _waLoadBriefing() {
  const body = document.getElementById("wa-briefing-body");
  const hours = parseInt(document.getElementById("wa-briefing-window").value, 10) || 24;
  body.innerHTML = `<div class="wa-empty-sm">Generating briefing — qwen3 is reading your inbox…</div>`;
  try {
    const r = await fetch(`/api/whatsapp/briefing?hours=${hours}`);
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    const s = data.stats || {};
    const statsLine = `${s.chats_with_new_msgs || 0} chats · ${s.chats_with_pending_drafts || 0} pending drafts · ${s.media_auto_filed || 0} attachments filed`;
    // Render markdown-ish: bold **text**, line breaks, bullets.
    const html = _waRenderBriefing(data.summary || "");
    body.innerHTML = `
      <div class="wa-briefing-stats">${escapeHtml(statsLine)}</div>
      <div class="wa-briefing-content">${html}</div>
      <div class="wa-briefing-foot">Generated ${escapeHtml(data.generated_at || "")}</div>
    `;
  } catch (e) {
    body.innerHTML = `<div class="wa-briefing-error">Briefing failed: ${escapeHtml(e.message)}</div>`;
  }
}

function _waRenderBriefing(md) {
  // Minimal markdown: **bold**, line breaks, leading "- " bullets, "## " headers.
  let h = escapeHtml(md);
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  // Convert "## Title" lines to <h3>.
  h = h.replace(/^##\s+(.+)$/gm, "<h3>$1</h3>");
  // Convert lines starting with "- " to list items.
  const lines = h.split("\n");
  const out = [];
  let inList = false;
  for (const ln of lines) {
    if (/^\s*-\s+/.test(ln)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + ln.replace(/^\s*-\s+/, "") + "</li>");
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      if (ln.trim()) out.push("<p>" + ln + "</p>");
    }
  }
  if (inList) out.push("</ul>");
  return out.join("\n");
}

async function _waBackfill() {
  const btn = document.getElementById("wa-backfill");
  if (!btn) return;
  btn.disabled = true;
  btn.title = "Indexing… (one-time, ~50-200 ms per message)";
  showResponse("Building semantic index — this can take a minute on a big inbox…");
  try {
    const r = await fetch("/api/whatsapp/backfill-embeddings", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    showResponse(`Semantic index: +${data.indexed} new, ${data.skipped} skipped, ${data.errors} errors (total candidates: ${data.total}).`);
  } catch (e) {
    showResponse(`Backfill failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.title = "Build / refresh semantic search index";
  }
}

async function _waImport(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  const btn = document.getElementById("wa-import");
  if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/whatsapp/import", { method: "POST", body: form });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    showResponse(`Imported ${data.messages_inserted} messages from "${file.name}" → chat "${data.contact_name || data.chat_jid}".`);
    await _waRefreshChats();
  } catch (err) {
    showResponse(`Import failed: ${err.message}`, true);
  } finally {
    if (btn) btn.disabled = false;
    e.target.value = ""; // allow re-importing same file
  }
}

async function _waClear() {
  if (!confirm("Wipe all ingested WhatsApp chats, messages and drafts? Your phone's WhatsApp is not affected — this only clears Yorik's local copy.")) return;
  try {
    const r = await fetch("/api/whatsapp/clear", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    showResponse(`Cleared ${data.chats_cleared} chats, ${data.messages_cleared} messages, ${data.drafts_cleared} drafts.`);
    _waState.activeJid = null;
    document.querySelector(".wa-thread-name").textContent = "Select a conversation";
    document.getElementById("wa-thread-msgs").innerHTML = `<div class="wa-empty">Pick a chat on the left to see messages.</div>`;
    document.getElementById("wa-send-input").disabled = true;
    document.getElementById("wa-send-btn").disabled = true;
    document.getElementById("wa-draft-gen").disabled = true;
    document.getElementById("wa-draft-text").disabled = true;
    await _waRefreshChats();
  } catch (e) {
    showResponse(`Clear failed: ${e.message}`, true);
  }
}

async function _waSyncFromBridge() {
  const btn = document.getElementById("wa-sync");
  if (!btn) return;
  btn.disabled = true;
  btn.title = "Syncing…";
  try {
    const r = await fetch("/api/whatsapp/sync", { method: "POST" });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    showResponse(`Synced ${data.messages_ingested} messages across ${data.chats} chats.`);
    await _waRefreshChats();
  } catch (e) {
    showResponse(`Sync failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
    btn.title = "Sync history from phone";
  }
}

function _waUseDraft() {
  const draft = document.getElementById("wa-draft-text").value.trim();
  if (!draft) return;
  document.getElementById("wa-send-input").value = draft;
  document.getElementById("wa-send-input").focus();
}

function _waFmtTime(d) {
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { day: "2-digit", month: "short" });
}

async function mountComposeApp() {
  // Idempotent — only re-mount if the host element is empty.
  const host = document.getElementById("compose-editor-host");
  if (host && !_composeEditor) {
    if (!window.Tiptap) {
      host.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div><div class="title">TipTap bundle not loaded</div><div class="subtitle">Run <code>bash scripts/build-tiptap-vendor.sh</code> and hard-reload.</div></div>`;
      return;
    }
    const { Editor, StarterKit, Underline, Link, Placeholder, Mention, Table, TableRow, TableHeader, TableCell } = window.Tiptap;
    _composeEditor = new Editor({
      element: host,
      extensions: [
        StarterKit,
        Underline,
        Link.configure({ openOnClick: false }),
        Placeholder.configure({
          placeholder: "Pick a template on the left, or just start typing… (Tip: ask Yorik to draft something via voice)",
        }),
        Mention.configure({
          HTMLAttributes: { class: "tiptap-mention" },
          // suggestion plugin will be filled in once we wire LLM-suggested vars
          suggestion: { items: () => [], render: () => ({}) },
        }),
        Table.configure({ resizable: true, HTMLAttributes: { class: "tiptap-table" } }),
        TableRow, TableHeader, TableCell,
      ],
      content: "",
      autofocus: false,
    });
    // Highlight-and-ask: show a "✨ Ask Yorik" pill anchored to the
    // selection. Click → mini panel asks for an instruction → LLM call →
    // suggestion cards → click to accept/reject.
    _composeEditor.on("selectionUpdate", ({ editor }) => {
      const { from, to } = editor.state.selection;
      if (to > from) {
        _showComposePillForSelection(editor, from, to);
      } else {
        _hideComposePill();
      }
    });
    _composeEditor.on("blur", () => { /* keep pill open while user clicks it */ });
  }

  await _loadComposeTemplates();
  _renderComposeTemplateList();
  _renderComposeToolbar();

  // Save-to-Paperless wiring
  const sav = document.getElementById("compose-save");
  if (sav && !sav._wired) {
    sav.onclick = async () => {
      if (!_composeEditor) return;
      const title = (prompt("Title for Paperless:", _composeActiveTemplate || "document") || "").trim();
      if (!title) return;
      const body_html = _composeEditor.getHTML();
      sav.disabled = true;
      const orig = sav.textContent;
      sav.textContent = "Saving…";
      try {
        const r = await fetch(`/api/compose/save?role=${state.role}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body_html, title, tags: ["compose", _composeActiveTemplate].filter(Boolean) }),
        });
        const j = await r.json();
        if (!r.ok) { alert("Save failed: " + (j.detail || r.status)); return; }
        sav.textContent = "✓ Saved";
        setTimeout(() => { sav.textContent = orig; sav.disabled = false; }, 1800);
      } catch (e) {
        alert("Save error: " + e.message);
        sav.disabled = false;
        sav.textContent = orig;
      }
    };
    sav._wired = true;
  }

  // Send-via-email wiring (asks for recipient + subject inline)
  const snd = document.getElementById("compose-send");
  if (snd && !snd._wired) {
    snd.onclick = async () => {
      if (!_composeEditor) return;
      const to = (prompt("Recipient email:") || "").trim();
      if (!to) return;
      const subject = (prompt("Subject:", _composeActiveTemplate || "Document") || "").trim();
      if (!subject) return;
      const body_html = _composeEditor.getHTML();
      snd.disabled = true;
      const orig = snd.textContent;
      snd.textContent = "Sending…";
      try {
        const r = await fetch(`/api/compose/send-email?role=${state.role}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body_html, to, subject, title: subject,
            tags: ["compose", _composeActiveTemplate].filter(Boolean) }),
        });
        const j = await r.json();
        if (!r.ok) { alert("Send failed: " + (j.detail || r.status)); return; }
        snd.textContent = "✓ Sent";
        setTimeout(() => { snd.textContent = orig; snd.disabled = false; }, 1800);
      } catch (e) {
        alert("Send error: " + e.message);
        snd.disabled = false;
        snd.textContent = orig;
      }
    };
    snd._wired = true;
  }

  // Export-PDF wiring
  const exp = document.getElementById("compose-export");
  if (exp && !exp._wired) {
    exp.onclick = async () => {
      if (!_composeEditor) return;
      const body_html = _composeEditor.getHTML();
      const tpl = _composeTemplates.find(t => t.id === _composeActiveTemplate);
      const r = await fetch(`/api/compose/render-pdf?role=${state.role}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body_html,
          filename: `${_composeActiveTemplate || "document"}.pdf`,
          template_id: _composeActiveTemplate || null,
          args: tpl?.default_args || {},
        }),
      });
      if (!r.ok) {
        alert("PDF render failed: HTTP " + r.status);
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${_composeActiveTemplate || "document"}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    };
    exp._wired = true;
  }
}

async function _loadComposeTemplates() {
  // Show skeleton rows while the fetch is in flight (templates is fast
  // locally, but adds polish when /api/compose/templates is cold).
  const list = document.getElementById("compose-templates-list");
  if (list && !_composeTemplates) {
    list.innerHTML = Array.from({ length: 3 }).map(() => `
      <div class="skeleton-stack" style="padding:9px 11px; gap:6px">
        <div class="skeleton" style="width:70%; height:13px"></div>
        <div class="skeleton" style="width:90%; height:10px"></div>
      </div>`).join("");
  }
  try {
    _composeTemplates = await fetchJSON("/api/compose/templates");
  } catch (e) {
    _composeTemplates = [];
  }
}

function _renderComposeTemplateList() {
  const list = document.getElementById("compose-templates-list");
  if (!list) return;
  if (!_composeTemplates.length) {
    list.innerHTML = `<div class="compose-empty">No templates yet — the registry endpoint is wired in task #113.</div>`;
    return;
  }
  list.innerHTML = _composeTemplates.map(t => `
    <div class="compose-tpl ${_composeActiveTemplate === t.id ? "active" : ""}" data-tpl="${escapeHtml(t.id)}">
      <div class="compose-tpl-name">${escapeHtml(t.name)}</div>
      <div class="compose-tpl-desc">${escapeHtml(t.description || "")}</div>
    </div>
  `).join("");
  list.querySelectorAll("[data-tpl]").forEach(el => {
    el.onclick = () => _selectComposeTemplate(el.getAttribute("data-tpl"));
  });
}

async function _selectComposeTemplate(tplId) {
  _composeActiveTemplate = tplId;
  _renderComposeTemplateList();
  if (!_composeEditor) return;
  // Skeleton draft that roughly mirrors an invoice/letter layout — header
  // line + meta block + a couple of body paragraphs + a fake table —
  // so the page doesn't jolt when the real content lands.
  _composeEditor.commands.setContent(`
    <div class="skeleton title"></div>
    <div class="skeleton line" style="width: 80%"></div>
    <div class="skeleton line" style="width: 65%"></div>
    <div class="skeleton block"></div>
    <div class="skeleton line" style="width: 90%"></div>
    <div class="skeleton line" style="width: 70%"></div>
  `);
  const tpl = _composeTemplates.find(t => t.id === tplId);
  const args = { ...(tpl?.default_args || {}) };
  try {
    const r = await fetch(`/api/compose/draft?role=${state.role}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template_id: tplId, args }),
    });
    const j = await r.json();
    if (!r.ok) {
      _composeEditor.commands.setContent(
        `<p style="color:var(--danger)">Draft failed: ${escapeHtml(j.detail || r.status)}</p>`
      );
      return;
    }
    _composeEditor.commands.setContent(j.html || "");
    _renderComposeSourceData(j.template, j.data, j.args);
    _enableComposeActions(true);
  } catch (e) {
    _composeEditor.commands.setContent(
      `<p style="color:var(--danger)">Draft network error: ${escapeHtml(e.message)}</p>`
    );
  }
}

function _renderComposeSourceData(template, data, args) {
  const el = document.getElementById("compose-ai-data");
  if (!el) return;
  const argRows = Object.entries(args || {}).map(([k, v]) =>
    `<div class="ai-row"><span class="ai-k">${escapeHtml(k)}</span><span class="ai-v">${escapeHtml(JSON.stringify(v))}</span></div>`
  ).join("");
  const dataKeys = Object.keys(data || {});
  const dataRows = dataKeys.map(k => {
    const v = data[k];
    const preview = v == null ? "—" : (
      Array.isArray(v) ? `${v.length} item${v.length === 1 ? "" : "s"}`
        : (typeof v === "object" ? Object.keys(v).slice(0, 3).join(", ") + (Object.keys(v).length > 3 ? "…" : "")
        : String(v))
    );
    return `<div class="ai-row"><span class="ai-k">${escapeHtml(k)}</span><span class="ai-v">${escapeHtml(preview)}</span></div>`;
  }).join("");
  el.innerHTML = `
    <div class="ai-section"><div class="ai-section-h">Template</div><div class="ai-template">${escapeHtml(template.name)}</div></div>
    ${argRows ? `<div class="ai-section"><div class="ai-section-h">Arguments</div>${argRows}</div>` : ""}
    ${dataRows ? `<div class="ai-section"><div class="ai-section-h">Pulled data</div>${dataRows}</div>` : `<div class="compose-empty">(no data_query in this template)</div>`}
  `;
}

function _enableComposeActions(on) {
  for (const id of ["compose-save", "compose-send", "compose-export"]) {
    const b = document.getElementById(id);
    if (b) b.disabled = !on;
  }
}

// ─── Highlight-and-ask: floating pill + suggestion panel ─────────────────

let _composePill = null;
let _composePanel = null;
let _composeSelection = null;  // {from, to, text}

function _showComposePillForSelection(editor, from, to) {
  const text = editor.state.doc.textBetween(from, to, " ");
  if (!text || text.length < 2) { _hideComposePill(); return; }
  _composeSelection = { from, to, text };
  // Place the pill just above the selection's bounding rect.
  const rect = editor.view.coordsAtPos(from);
  const rect2 = editor.view.coordsAtPos(to);
  const top = Math.min(rect.top, rect2.top) - 38;
  const left = (rect.left + rect2.right) / 2;
  if (!_composePill) {
    _composePill = document.createElement("button");
    _composePill.className = "compose-pill";
    _composePill.innerHTML = "✨ Ask Yorik";
    _composePill.onclick = _openComposeAskPanel;
    document.body.appendChild(_composePill);
  }
  _composePill.style.top = `${Math.max(8, top + window.scrollY)}px`;
  _composePill.style.left = `${left + window.scrollX}px`;
  _composePill.style.display = "inline-flex";
}

function _hideComposePill() {
  if (_composePill) _composePill.style.display = "none";
  if (_composePanel) { _composePanel.remove(); _composePanel = null; }
}

function _openComposeAskPanel() {
  if (!_composeSelection || !_composeEditor) return;
  const sel = _composeSelection;
  // Position the panel under the pill.
  const rect = _composePill.getBoundingClientRect();
  _composePanel?.remove();
  _composePanel = document.createElement("div");
  _composePanel.className = "compose-ask-panel";
  _composePanel.style.top = `${rect.bottom + 6 + window.scrollY}px`;
  _composePanel.style.left = `${rect.left + window.scrollX - 120}px`;
  _composePanel.innerHTML = `
    <div class="compose-ask-h">Revise selection</div>
    <div class="compose-ask-sel">${escapeHtml(_truncate(sel.text, 120))}</div>
    <div class="compose-ask-row">
      <input class="compose-ask-input" placeholder="e.g. make it more formal, shorter, German" autofocus>
      <button class="primary compose-ask-go">↵</button>
    </div>
    <div class="compose-ask-results"></div>
    <div class="compose-ask-foot">
      <button class="compose-ask-cancel">Cancel</button>
    </div>
  `;
  document.body.appendChild(_composePanel);
  const input = _composePanel.querySelector(".compose-ask-input");
  const go = _composePanel.querySelector(".compose-ask-go");
  const cancel = _composePanel.querySelector(".compose-ask-cancel");
  const results = _composePanel.querySelector(".compose-ask-results");
  setTimeout(() => input.focus(), 20);

  const fire = async () => {
    const instruction = input.value.trim();
    if (!instruction) return;
    results.innerHTML = `<div class="compose-ask-loading">Asking Yorik…</div>`;
    go.disabled = true;
    const before = _composeEditor.state.doc.textBetween(Math.max(0, sel.from - 200), sel.from, " ");
    const after  = _composeEditor.state.doc.textBetween(sel.to, Math.min(_composeEditor.state.doc.content.size, sel.to + 200), " ");
    try {
      const r = await fetch(`/api/connectors/compose/invoke?role=${state.role}&layout_id=__system__`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ params: {
          op: "revise",
          selected_text: sel.text,
          instruction,
          context_before: before,
          context_after: after,
        }}),
      });
      const j = await r.json();
      const suggestions = (j.suggestions || []);
      if (!suggestions.length) {
        results.innerHTML = `<div class="compose-ask-loading">No suggestions came back — try a different prompt.</div>`;
      } else {
        results.innerHTML = suggestions.map((s, i) => `
          <div class="compose-suggestion" data-idx="${i}">
            <div class="compose-suggestion-text">${escapeHtml(s.text)}</div>
            <div class="compose-suggestion-actions">
              <button class="primary compose-accept" data-idx="${i}">Accept</button>
            </div>
          </div>
        `).join("");
        results.querySelectorAll(".compose-accept").forEach(btn => {
          btn.onclick = () => {
            const idx = parseInt(btn.getAttribute("data-idx"));
            const repl = suggestions[idx].text;
            // Replace the selected range with the new text. Keep simple
            // text replacement for v1; rich-formatted replacement comes
            // when we add the suggestion-mark workflow.
            _composeEditor.chain().focus()
              .insertContentAt({ from: sel.from, to: sel.to }, repl)
              .run();
            _hideComposePill();
          };
        });
      }
    } catch (e) {
      results.innerHTML = `<div class="compose-ask-loading" style="color:var(--danger)">Error: ${escapeHtml(e.message)}</div>`;
    } finally {
      go.disabled = false;
    }
  };
  go.onclick = fire;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") fire(); if (e.key === "Escape") _hideComposePill(); });
  cancel.onclick = _hideComposePill;
}

function _renderComposeToolbar() {
  const tb = document.getElementById("compose-toolbar");
  if (!tb || !_composeEditor) return;
  const cmd = (action, body, opts) => () => {
    const chain = _composeEditor.chain().focus();
    chain[action](opts).run();
  };
  const btn = (label, onclick, isActive = false) =>
    `<button class="${isActive ? "active" : ""}">${label}</button>`;
  tb.innerHTML = `
    <button data-act="bold" title="Bold">${window.icon("bold")}</button>
    <button data-act="italic" title="Italic">${window.icon("italic")}</button>
    <button data-act="underline" title="Underline">${window.icon("underline")}</button>
    <span class="tb-sep"></span>
    <button data-act="h1" title="Heading 1">${window.icon("heading-1")}</button>
    <button data-act="h2" title="Heading 2">${window.icon("heading-2")}</button>
    <button data-act="paragraph" title="Paragraph">${window.icon("paragraph")}</button>
    <span class="tb-sep"></span>
    <button data-act="bullet" title="Bulleted list">${window.icon("list")}</button>
    <button data-act="ordered" title="Numbered list">${window.icon("list-ordered")}</button>
    <span class="tb-sep"></span>
    <button data-act="table" title="Insert table">${window.icon("table")}</button>
    <button data-act="hr" title="Horizontal rule">${window.icon("minus")}</button>
  `;
  const actions = {
    bold:      () => _composeEditor.chain().focus().toggleBold().run(),
    italic:    () => _composeEditor.chain().focus().toggleItalic().run(),
    underline: () => _composeEditor.chain().focus().toggleUnderline().run(),
    h1:        () => _composeEditor.chain().focus().toggleHeading({ level: 1 }).run(),
    h2:        () => _composeEditor.chain().focus().toggleHeading({ level: 2 }).run(),
    paragraph: () => _composeEditor.chain().focus().setParagraph().run(),
    bullet:    () => _composeEditor.chain().focus().toggleBulletList().run(),
    ordered:   () => _composeEditor.chain().focus().toggleOrderedList().run(),
    table:     () => _composeEditor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(),
    hr:        () => _composeEditor.chain().focus().setHorizontalRule().run(),
  };
  tb.querySelectorAll("[data-act]").forEach(b => {
    b.onclick = actions[b.getAttribute("data-act")];
  });
}

async function loadDocsList() {
  try {
    state.docs.list = await fetchJSON(`/api/documents?role=${state.role}`);
  } catch (e) {
    state.docs.list = [];
  }
}

// (note: _fmtBytes is defined once near the top of the file; see
// renderBackupTab. Both call sites here use the same helper.)

function renderDocsView() {
  const body = document.getElementById("docs-body");
  if (!body) return;

  if (state.docs.searchResults !== null) {
    // Search-results mode
    const hits = state.docs.searchResults;
    body.innerHTML = `
      <div></div>
      <div class="docs-results">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px">
          ${hits.length} match${hits.length !== 1 ? "es" : ""} · click "Clear" to go back to the doc list
          <button id="docs-clear-search" style="margin-left:12px">Clear</button>
        </div>
        ${hits.length === 0
          ? `<div style="padding:24px;text-align:center;color:var(--text-dim)">Nothing matches your query.</div>`
          : hits.map(h => `
              <div class="docs-result">
                <div class="doc-name">${escapeHtml(h.doc_title)} <span class="score">match ${(1 - h.distance).toFixed(2)}</span></div>
                <div class="chunk">${escapeHtml(_truncate(h.chunk_text || "", 600))}</div>
              </div>
            `).join("")
        }
      </div>
    `;
    body.querySelector("#docs-clear-search")?.addEventListener("click", () => {
      state.docs.searchResults = null;
      document.getElementById("docs-search-input").value = "";
      renderDocsView();
    });
    return;
  }

  // Doc-list + detail mode
  const list = state.docs.list;
  const selected = list.find(d => d.id === state.docs.selectedId);
  body.innerHTML = `
    <aside class="docs-list">
      ${list.length === 0
        ? `<div class="empty-state">
             <div class="icon">📄</div>
             <div class="title">No documents yet</div>
             <div class="subtitle">Drop a PDF or Word doc onto this area, or click <strong>+ Upload</strong> above.</div>
           </div>`
        : list.map(d => {
            const ext = (d.title.split(".").pop() || "").toLowerCase();
            const kind = ["pdf","docx","md","txt"].includes(ext) ? ext : "txt";
            const ic = { pdf: "PDF", docx: "DOC", md: "MD", txt: "TXT" }[kind];
            return `
              <div class="doc ${d.id === state.docs.selectedId ? "active" : ""}" data-doc-id="${d.id}">
                <div class="file-icon ${kind}">${ic}</div>
                <div class="meta-block">
                  <div class="title">${escapeHtml(d.title)}</div>
                  <div class="meta">
                    <span>${_fmtBytes(d.bytes)}</span>
                    <span class="dot"></span>
                    <span>${d.chunk_count || 0} chunks</span>
                    <span class="dot"></span>
                    <span>${escapeHtml(d.allowed_roles || "—")}</span>
                  </div>
                </div>
              </div>
            `;
          }).join("")
      }
    </aside>
    <section class="docs-detail">
      ${selected
        ? `
          <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px">
            <div>
              <div style="font-size:18px;font-weight:600">${escapeHtml(selected.title)}</div>
              <div style="color:var(--text-dim);font-size:12px">${escapeHtml(selected.mime_type || "")} · ${_fmtBytes(selected.bytes)} · ${selected.chunk_count || 0} chunks · indexed ${escapeHtml(selected.indexed_at || "never")}</div>
            </div>
            <div style="display:flex;gap:6px">
              <a class="button" href="/api/documents/${selected.id}/raw?role=${encodeURIComponent(state.role)}&download=1" download>⬇ Download</a>
              <button data-doc-reindex="${selected.id}">Reindex</button>
              <button data-doc-delete="${selected.id}" class="danger">Delete</button>
            </div>
          </div>
          <div style="color:var(--text-dim);font-size:12px;margin-bottom:14px">
            Visible to roles: <code>${escapeHtml(selected.allowed_roles)}</code>
          </div>
          <div id="docs-preview" class="docs-preview" data-doc-id="${selected.id}" data-mime="${escapeHtml(selected.mime_type || "")}">
            <div style="color:var(--text-dim);text-align:center;padding:24px">Loading preview…</div>
          </div>
          <div style="margin-top:14px;font-size:12px;color:var(--text-dim);line-height:1.6">
            Ask Yorik in the chat about this document — the answer will be cited.
          </div>
        `
        : `<div style="color:var(--text-dim);text-align:center;padding:36px">
             Select a document on the left, or upload a new one.
           </div>`
      }
    </section>
  `;

  // Doc click → show detail
  body.querySelectorAll("[data-doc-id]").forEach(el => {
    el.addEventListener("click", () => {
      state.docs.selectedId = Number(el.getAttribute("data-doc-id"));
      renderDocsView();
    });
  });
  body.querySelectorAll("[data-doc-reindex]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-doc-reindex");
      btn.textContent = "Reindexing…";
      await fetch(`/api/documents/${id}/reindex?role=${state.role}`, { method: "POST" });
      await loadDocsList();
      renderDocsView();
    });
  });
  body.querySelectorAll("[data-doc-delete]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-doc-delete");
      if (!confirm(`Delete document ${id}?`)) return;
      await fetch(`/api/documents/${id}?role=${state.role}`, { method: "DELETE" });
      state.docs.selectedId = null;
      await loadDocsList();
      renderDocsView();
    });
  });

  // Inline preview: text/markdown fetched and rendered, PDF via <iframe>,
  // images via <img>, anything else falls back to a download CTA.
  const preview = body.querySelector("#docs-preview");
  if (preview) _renderDocPreview(preview);
}

async function _renderDocPreview(host) {
  const id = host.getAttribute("data-doc-id");
  const mime = (host.getAttribute("data-mime") || "").toLowerCase();
  const src = `/api/documents/${id}/raw?role=${encodeURIComponent(state.role)}`;
  const dlSrc = src + "&download=1";

  const isPdf   = mime.includes("pdf");
  const isImage = mime.startsWith("image/");
  const isText  = mime.startsWith("text/") || mime.includes("json") || mime.includes("markdown");

  if (isPdf) {
    host.innerHTML = `<iframe src="${src}" class="docs-preview-frame" title="Document preview"></iframe>`;
    return;
  }
  if (isImage) {
    host.innerHTML = `
      <div class="docs-preview-image">
        <img src="${src}" alt="Document preview" />
      </div>`;
    return;
  }
  if (isText) {
    try {
      const txt = await fetch(src).then(r => r.text());
      host.innerHTML = `<pre class="docs-preview-text">${escapeHtml(txt)}</pre>`;
    } catch {
      host.innerHTML = `
        <div style="color:var(--text-dim);text-align:center;padding:24px">
          Could not load preview.
          <div style="margin-top:8px"><a class="button" href="${dlSrc}" download>⬇ Download instead</a></div>
        </div>`;
    }
    return;
  }
  host.innerHTML = `
    <div style="color:var(--text-dim);text-align:center;padding:24px">
      No inline preview for <code>${escapeHtml(mime || "this file type")}</code>.
      <div style="margin-top:10px"><a class="button" href="${dlSrc}" download>⬇ Download</a></div>
    </div>`;
}

function wireDocsApp() {
  const searchInput = document.getElementById("docs-search-input");
  const searchBtn = document.getElementById("docs-search-btn");
  const uploadBtn = document.getElementById("docs-upload-btn");
  const fileInput = document.getElementById("docs-file-input");
  if (searchBtn && !searchBtn._wired) {
    const runSearch = async () => {
      const q = (searchInput?.value || "").trim();
      if (!q) { state.docs.searchResults = null; renderDocsView(); return; }
      try {
        const r = await fetch(`/api/documents/search?role=${state.role}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: q, k: 8 }),
        });
        const hits = await r.json();
        state.docs.searchResults = Array.isArray(hits) ? hits : [];
      } catch (e) {
        state.docs.searchResults = [];
      }
      renderDocsView();
    };
    searchBtn.addEventListener("click", runSearch);
    searchInput?.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
    searchBtn._wired = true;
  }
  if (uploadBtn && !uploadBtn._wired) {
    uploadBtn.addEventListener("click", () => fileInput?.click());
    fileInput?.addEventListener("change", async (e) => {
      for (const f of e.target.files) {
        const fd = new FormData();
        fd.append("file", f);
        const params = new URLSearchParams({
          role: state.role,
          title: f.name.replace(/\.[^.]+$/, ""),
          allowed_roles: "admin,member",
        });
        await fetch(`/api/documents/upload?${params}`, { method: "POST", body: fd });
      }
      await loadDocsList();
      renderDocsView();
    });
    uploadBtn._wired = true;
  }
}

// ---------------------------------------------------------------------------
// Hook the app router into existing flows.
// ---------------------------------------------------------------------------

// Existing askText / askVoice push to the chat history when the chat app is
// the active view, so the conversation feels continuous.
const _origAskText = askText;
askText = async function(msg) {
  // If we're not on the chat app, asking still works (response panel shows it).
  // If we ARE on the chat app, also push the bubbles into the chat view.
  if (state.app === "chat") {
    pushChatMessage("user", msg);
  }
  await _origAskText(msg);
  // After ask completes, the response is in #response. Push it into chat too.
  if (state.app === "chat") {
    const panel = document.getElementById("response");
    const text = panel?.querySelector("div")?.textContent || "";
    if (text && text !== "Asking the LLM...") pushChatMessage("assistant", text);
    // Refresh history sidebar so the new conversation shows up.
    loadConversationHistory();
  }
};

// applyUiActions extension — handle open_app + open_compose_draft from the LLM.
const _origApplyUiActions = applyUiActions;
applyUiActions = async function(actions) {
  if (!actions || actions.length === 0) return false;
  // Pull out open_app actions first so we switch view BEFORE running anything else.
  const openActs = actions.filter(a => a.type === "open_app");
  if (openActs.length) {
    const a = openActs[openActs.length - 1];  // last wins
    await openApp(a.app_id || "home");
  }
  // Compose-draft action from the LLM's compose.draft connector — switches
  // to the Compose app and loads the rendered draft into the editor.
  const composeActs = actions.filter(a => a.type === "open_compose_draft");
  if (composeActs.length) {
    const a = composeActs[composeActs.length - 1];
    await openApp("compose");
    // Give mountComposeApp a moment to finish creating the editor.
    await new Promise(r => setTimeout(r, 80));
    if (_composeEditor) {
      _composeActiveTemplate = a.template_id || null;
      _renderComposeTemplateList();
      _composeEditor.commands.setContent(a.html || "");
      _renderComposeSourceData(
        { name: a.template_name || a.template_id || "Document" },
        a.data || {},
        a.args || {},
      );
      _enableComposeActions(true);
    }
  }
  const remaining = actions.filter(a => a.type !== "open_app" && a.type !== "open_compose_draft");
  return await _origApplyUiActions(remaining);
};

// (The 🏠 home button moved out of the header — the bottom dock now owns
// navigation, including back-to-home via its first tile.)

// When the user types in the ask bar, also push to chat history if we end up
// in the chat app. The ask form handler already exists in wire(); we don't
// override it. Chat-app integration happens through askText's wrapper above.

// Load the Lucide sprite into a hidden div so every <use href="#i-..."/>
// resolves locally with no network round-trip. We do this before wire()
// so any startup-time icon() calls find their symbols.
(async function _loadIconSprite() {
  try {
    const r = await fetch("/vendor/lucide-sprite.svg");
    if (r.ok) {
      const mount = document.getElementById("icon-sprite-mount");
      if (mount) mount.innerHTML = await r.text();
    }
  } catch (e) { console.warn("icon sprite load failed:", e); }
})();

// Helper for generating an icon <svg> from a sprite symbol. Exposed on
// window so it's usable from inline-event handlers + template strings.
window.icon = function (name, cls = "") {
  return `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
};

// Auth-gated bootstrap. Until we know whether the user is logged in,
// nothing else mounts — no point fetching data we'd just discard, and
// it avoids a flicker of authenticated-looking UI behind the modal.
async function _bootstrap() {
  let me;
  try {
    me = await fetch("/api/auth/me", { credentials: "include" }).then(r => r.json());
  } catch (e) {
    me = { logged_in: false };
  }
  if (!me.logged_in) {
    _showAuthOverlay(me.setup_required ? "setup" : "login");
    return;
  }
  // Logged in — hide overlay, populate header, boot the rest of the app.
  state.role = me.user?.role || state.role;
  document.getElementById("auth-overlay")?.setAttribute("hidden", "");
  const namePill = document.getElementById("user-pill-name");
  const rolePill = document.getElementById("user-pill-role");
  if (namePill) namePill.textContent = me.user?.name || me.user?.email || "user";
  if (rolePill) rolePill.textContent = me.user?.role || "";
  wire();
  loadHealthConfig();
  loadAll();
  // App-registry + initial route only after auth; pathname='/' resolves
  // to 'home' which hard-navigates to /r/home (React shell). On an
  // unconfigured tenant that redirect was firing before _bootstrap
  // could show the setup overlay — the operator landed on a logged-out
  // /r/home instead of the wizard.
  await loadAppRegistry();
  const initial = _appIdFromPath(location.pathname);
  await openApp(initial, { push: false });
  history.replaceState({ app: initial }, "", location.pathname);
}

function _showAuthOverlay(mode) {
  const overlay = document.getElementById("auth-overlay");
  if (!overlay) return;
  overlay.removeAttribute("hidden");
  const isSetup = mode === "setup";
  document.getElementById("auth-tagline").textContent = isSetup
    ? "First-run setup — create the initial admin account"
    : "Sign in to continue";
  document.getElementById("auth-name-field").hidden = !isSetup;
  document.getElementById("auth-submit").textContent = isSetup ? "Create admin & sign in" : "Sign in";
  const form = document.getElementById("auth-form");
  const err = document.getElementById("auth-error");
  err.hidden = true;
  form.onsubmit = async (e) => {
    e.preventDefault();
    err.hidden = true;
    const email = document.getElementById("auth-email").value.trim();
    const password = document.getElementById("auth-password").value;
    const name = document.getElementById("auth-name").value.trim();
    const url = isSetup ? "/api/auth/setup" : "/api/auth/login";
    // Tenant mode requires the invite token issued by the host. It
    // arrives as ?invite=<token> on the URL the operator hands over.
    const inviteToken = new URLSearchParams(location.search).get("invite");
    const body = isSetup
      ? { email, password, name, invite_token: inviteToken || undefined }
      : { email, password };
    try {
      const r = await fetch(url, {
        method: "POST", credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const detail = await r.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${r.status}`);
      }
      // Cookie is set; reload so the gated bootstrap runs again clean.
      location.reload();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  };
  setTimeout(() => document.getElementById("auth-email").focus(), 50);
}

_bootstrap();

setInterval(loadAll, 30000);

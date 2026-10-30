// The page's logic: polls /api/state, renders the check-in banner, prep cards, week and dashboard,
// drives the demo clock, and sets up notifications (web push when VAPID keys exist, else local ones).

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const button = (label, cls, onClick) => {
  const b = el("button", cls, label);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
};

const POLL_MS = 10_000;
let state = null;
let serverOffset = 0; // the server's clock (maybe simulated) minus this device's, in ms
let cards = [];
let lastSyncSeen;
let renderedKeys = { checkin: "", week: "" };
let movedIds = new Set();
let notified = new Set(); // "blockId:status" already announced
let firstPoll = true;
let pushSubscribed = false;
let courses = [];
const swReady = "serviceWorker" in navigator
  ? navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).catch(() => null)
  : Promise.resolve(null);

// ---- helpers -----------------------------------------------------------------------------------

async function api(path, opts = {}) {
  const headers = typeof opts.body === "string" ? { "Content-Type": "application/json" } : {};
  const res = await fetch(path, { ...opts, headers });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {}
    throw new Error(msg);
  }
  return res.json();
}
const post = (path, body) => api(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {}
  },
};

const pad = (n) => String(n).padStart(2, "0");
const toLocalInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const now = () => new Date(Date.now() + serverOffset);
const fmtTime = (d) => d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtDay = (d) => d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
const fmtDayTime = (d) => `${d.toLocaleDateString([], { weekday: "short" })} ${fmtTime(d)}`;
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const label = (s) => (s || "").replaceAll("_", " ");

function dueText(iso) {
  if (!iso) return "No due date";
  const d = new Date(iso);
  const n = now();
  const days = Math.round((startOfDay(d) - startOfDay(n)) / 86_400_000);
  if (d < n) return `Was due ${fmtDayTime(d)}`;
  if (days === 0) return `Due today ${fmtTime(d)}`;
  if (days === 1) return `Due tomorrow ${fmtTime(d)}`;
  return `Due ${fmtDayTime(d)} · in ${days} days`;
}

let toastTimer;
function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

function flash(node) {
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "center" });
  node.classList.remove("flash");
  void node.offsetWidth; // restart the animation
  node.classList.add("flash");
}

// ---- state polling -----------------------------------------------------------------------------

async function refresh(reloadCards = false) {
  let s;
  try {
    s = await api("/api/state");
  } catch {
    $("#sync-status").textContent = "Can't reach the server";
    return;
  }
  serverOffset = Date.parse(s.now) - Date.now();
  state = s;
  renderClock();
  renderSync();
  renderCheckin();
  renderWeek();
  notifyNew();
  updateNotifyBtn();
  if (reloadCards || s.sync.last !== lastSyncSeen) {
    lastSyncSeen = s.sync.last;
    await loadCards();
  }
}

function renderClock() {
  if (!state) return;
  $("#clock-time").textContent = now().toLocaleString([], {
    weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
  });
  $("#clock-badge").hidden = !state.simulated;
}

let syncTimer = null; // fast polling while a sync runs
let syncDismissed = localGet("syncDismissed");

function localGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function localSet(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}

// "Building card 3 of 5: Lab: Relative Velocity"
function progressText(p) {
  if (!p || !p.phase) return "Starting sync…";
  const count = p.total ? ` ${Math.min(p.done + 1, p.total)} of ${p.total}` : "";
  const now = p.current.length ? `: ${p.current.join(", ")}` : "";
  if (p.phase === "cards") {
    // cards build in parallel, so name everything in flight
    if (p.total === 1) return `Building card${now}`;
    return `Building cards (${p.done} of ${p.total} done)${now}`;
  }
  if (p.phase === "feedback") return `Fetching teacher feedback${count}`;
  if (p.phase === "assignments") return `Fetching assignments from ${p.total} courses (${p.done} done)`;
  return `${p.label}…`;
}

function renderSync() {
  const s = state.sync;
  const out = $("#sync-status");
  out.title = s.error || "";
  if (s.running) out.textContent = "Syncing…";
  else if (s.error) out.textContent = "Last sync failed";
  else if (!state.config.canvas) out.textContent = "Canvas not connected";
  else if (s.last) {
    const mins = Math.round((Date.now() - Date.parse(s.last)) / 60_000);
    out.textContent = mins < 1 ? "Synced just now" : `Synced ${mins} min ago`;
  } else out.textContent = "";
  $("#sync-btn").disabled = s.running;
  renderSyncPanel(s);
  if (s.running && !syncTimer) syncTimer = setInterval(refresh, 1000);
  if (!s.running && syncTimer) {
    clearInterval(syncTimer);
    syncTimer = null;
  }
}

function renderSyncPanel(s) {
  const panel = $("#sync-panel");
  const p = s.running ? s.progress : s.report;
  const failures = (p && p.failures) || [];
  const showReport = !s.running && p && (failures.length || s.error) && syncDismissed !== p.started_at;
  panel.hidden = !(s.running || showReport);
  if (panel.hidden) return;
  panel.classList.toggle("has-failures", failures.length > 0 || !!s.error);
  const fill = $("#sync-fill");
  if (s.running) {
    $("#sync-text").textContent = progressText(p);
    const share = p && p.total ? p.done / p.total : 0;
    fill.style.width = `${Math.round(share * 100)}%`;
    fill.parentElement.hidden = !(p && p.total);
  } else {
    const secs = p.timings && p.timings.total ? ` in ${Math.round(p.timings.total)} s` : "";
    const cards = p.cards !== undefined ? ` · ${p.cards} new card${p.cards === 1 ? "" : "s"}` : "";
    $("#sync-text").textContent = s.error
      ? `Last sync failed: ${s.error}`
      : `Last sync finished${secs}${cards} · ${failures.length} didn't work:`;
    fill.parentElement.hidden = true;
  }
  $("#sync-dismiss").hidden = s.running;
  $("#sync-failures").replaceChildren(
    ...failures.map((f) => {
      const li = el("li");
      li.append(el("strong", "", f.name), document.createTextNode(` (${f.phase}): ${f.error}`));
      return li;
    }),
  );
}

$("#sync-dismiss").addEventListener("click", () => {
  const r = state && state.sync.report;
  if (r) {
    syncDismissed = r.started_at;
    localSet("syncDismissed", syncDismissed);
  }
  $("#sync-panel").hidden = true;
});

// ---- check-in ----------------------------------------------------------------------------------

function suggestedTime() {
  const d = now();
  const t = new Date(d);
  if (d.getHours() < 19) t.setHours(19, 0, 0, 0);
  else t.setHours(d.getHours() + 1, 0, 0, 0);
  return t;
}

function renderCheckin() {
  const key = state.checkin.map((b) => `${b.id}:${b.status}`).join(",");
  if (key === renderedKeys.checkin) return; // don't wipe a time the student is typing
  renderedKeys.checkin = key;
  $("#checkin").replaceChildren(...state.checkin.map(checkinBox));
}

function checkinBox(b) {
  const missed = b.status === "missed";
  const box = el("div", `checkin${missed ? " missed" : ""}`);
  box.id = `checkin-${b.id}`;
  box.append(el("h2", null, missed ? `You missed the start of ${b.name}` : `Time to start: ${b.name}`));
  box.append(
    el(
      "p",
      null,
      missed
        ? `It was planned for ${fmtDayTime(new Date(b.start))}. No problem: when will you start?`
        : b.first_step
          ? `First step: ${b.first_step}`
          : "Open the prep card for your first step.",
    ),
  );
  const pickInput = el("input");
  pickInput.type = "datetime-local";
  pickInput.value = toLocalInput(suggestedTime());
  pickInput.setAttribute("aria-label", "Start time");
  const pick = el("span", "pick");
  pick.append(
    pickInput,
    button("Pick this time", null, () => {
      if (!pickInput.value) return toast("Choose a time first");
      answer(b.id, "pick_time", new Date(pickInput.value).toISOString());
    }),
  );
  const actions = el("div", "actions");
  actions.append(
    button("Start now", "primary", () => answer(b.id, "start_now")),
    button("+15 min", null, () => answer(b.id, "plus_15")),
    pick,
  );
  box.append(actions);
  return box;
}

async function answer(blockId, ans, pickedTime) {
  try {
    const r = await post(`/api/blocks/${blockId}/checkin`, { answer: ans, picked_time: pickedTime ?? null });
    movedIds = new Set([...r.moved, blockId]);
    renderedKeys.week = "";
    if (ans === "start_now") toast(r.block.first_step ? `Go: ${r.block.first_step}` : "Go!", 6000);
    else if (ans === "plus_15") toast("OK, I'll ping you again in 15 minutes.");
    else {
      const shifted = r.moved.length ? ` ${r.moved.length} other block${r.moved.length > 1 ? "s" : ""} moved to make room.` : "";
      toast(`Moved to ${fmtDayTime(new Date(r.block.start))}.${shifted}`, 5000);
    }
    await refresh(true);
    if (ans === "start_now") {
      showTab("cards");
      flash(document.getElementById(`card-${r.block.assignment_id}`));
    } else if (ans === "pick_time") showTab("week");
  } catch (e) {
    toast(e.message);
  }
}

// ---- week --------------------------------------------------------------------------------------

function renderWeek() {
  const key = JSON.stringify([state.week_start, state.blocks.map((b) => [b.id, b.start, b.status, b.at_risk])]);
  if (key === renderedKeys.week) return;
  renderedKeys.week = key;
  const first = new Date(state.week_start);
  const days = [...Array(7)].map((_, i) => new Date(first.getFullYear(), first.getMonth(), first.getDate() + i));
  const byDay = days.map(() => []);
  for (const b of state.blocks) {
    const s = new Date(b.start);
    const i = days.findIndex((d, k) => s >= d && (k === 6 || s < days[k + 1]));
    if (i >= 0) byDay[i].push(b);
  }
  $("#week").replaceChildren(
    ...days.map((d, i) => {
      const col = el("div", `day${i === 0 ? " today" : ""}`);
      col.append(el("h3", null, i === 0 ? `Today · ${fmtDay(d)}` : fmtDay(d)));
      byDay[i].forEach((b) => col.append(blockEl(b)));
      if (!byDay[i].length) col.append(el("p", "muted small", "Free"));
      return col;
    }),
  );
  movedIds = new Set(); // animate a reflow once, not on every poll
}

function blockEl(b) {
  const classes = ["block", b.status];
  if (b.at_risk) classes.push("risk");
  if (movedIds.has(b.id)) classes.push("moved");
  const n = el("div", classes.join(" "));
  n.append(el("div", "t", `${fmtTime(new Date(b.start))}–${fmtTime(new Date(b.end))}`), el("div", "n", b.name));
  const meta = [b.course, b.status !== "planned" ? b.status : "", b.at_risk ? "ends after due date" : ""].filter(Boolean);
  if (meta.length) n.append(el("div", "muted", meta.join(" · ")));
  n.title = b.first_step || b.name;
  n.addEventListener("click", () => {
    showTab("cards");
    flash(document.getElementById(`card-${b.assignment_id}`));
  });
  return n;
}

// ---- cards -------------------------------------------------------------------------------------

async function loadCards() {
  try {
    cards = await api("/api/cards");
  } catch (e) {
    toast(e.message);
    return;
  }
  renderCards();
}

function renderCards() {
  const root = $("#cards");
  if (!cards.length) {
    root.replaceChildren(
      el(
        "p",
        "empty",
        state?.config.canvas
          ? "No open work due in the next two weeks."
          : "No cards yet. Put your Canvas token in .env and press Sync, or run `python -m app.demo` for sample data.",
      ),
    );
    return;
  }
  root.replaceChildren(...cards.map(renderCard));
}

function renderCard(c) {
  const card = el("article", `card${c.done ? " done" : ""}`);
  card.id = `card-${c.assignment_id}`;

  const meta = el("div", "card-meta");
  meta.append(el("span", "badge", label(c.kind)));
  if (c.course) meta.append(el("span", null, c.course));
  meta.append(el("span", null, `· ${dueText(c.due_at)}`));
  card.append(meta, el("h2", null, c.name || `Assignment ${c.assignment_id}`));
  if (c.summary) card.append(el("p", "summary", c.summary));

  const first = el("div", "first-step");
  first.append(el("div", "label", "First step · about 2 minutes"), el("p", null, c.first_step));
  const firstActions = el("div", "actions");
  if (!c.done) firstActions.append(button("Start now", "primary", () => startAssignment(c.assignment_id)));
  if (c.next_block) firstActions.append(el("span", "muted small", `Planned for ${fmtDayTime(new Date(c.next_block))}`));
  first.append(firstActions);
  card.append(first);

  if (c.watch_out) {
    const w = el("p", "watch");
    w.append(el("strong", null, "Watch out: "), document.createTextNode(c.watch_out));
    card.append(w);
  }

  if (c.kind === "test" && (c.practice.length || c.focus)) card.append(renderPractice(c));

  if (c.steps.length) {
    const details = el("details");
    details.open = c.kind !== "test";
    details.append(el("summary", null, `Steps (${c.steps.length})`));
    const key = `steps-${c.assignment_id}`;
    const checked = new Set(store.get(key, []));
    const ol = el("ol", "steps");
    c.steps.forEach((s, i) => {
      const li = el("li");
      const lab = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.checked = checked.has(i);
      box.addEventListener("change", () => {
        box.checked ? checked.add(i) : checked.delete(i);
        store.set(key, [...checked]);
      });
      lab.append(box, el("span", null, s));
      li.append(lab);
      ol.append(li);
    });
    details.append(ol);
    card.append(details);
  }

  const safeLinks = c.links.filter((l) => /^https?:\/\//i.test(l.url));
  if (safeLinks.length) {
    const links = el("div", "links");
    safeLinks.forEach((l) => {
      const a = el("a", null, l.label);
      a.href = l.url;
      a.target = "_blank";
      a.rel = "noopener";
      links.append(a);
    });
    card.append(links);
  }

  const foot = el("div", "card-foot");
  foot.append(el("span", "badge", `${c.effort} effort`));
  if (c.generated_by !== "model") foot.append(el("span", "badge", c.generated_by === "demo" ? "demo card" : "template card"));
  foot.append(el("span", "spacer"));
  const regen = button("Regenerate", "ghost", async () => {
    regen.disabled = true;
    regen.textContent = "Regenerating…";
    try {
      await post(`/api/cards/${c.assignment_id}/regenerate`);
      await loadCards();
      toast("Card updated");
    } catch (e) {
      toast(e.message);
      regen.disabled = false;
      regen.textContent = "Regenerate";
    }
  });
  foot.append(regen);
  if (!c.done) {
    foot.append(
      button("Mark done", "ghost", async () => {
        try {
          await post(`/api/assignments/${c.assignment_id}/done`);
          await refresh(true);
        } catch (e) {
          toast(e.message);
        }
      }),
    );
  }
  card.append(foot);
  return card;
}

function renderPractice(c) {
  const box = el("div", "practice");
  box.append(el("h3", null, c.practice.length ? `Practice set (${c.practice.length})` : "Practice set"));
  if (c.focus) box.append(el("p", "focus", c.focus));
  if (!c.practice.length) {
    box.append(el("p", "muted small", "Set META_API_KEY to get questions from this unit's documents. Add graded tests on the Dashboard to aim them at your weak spots."));
    return box;
  }
  const ol = el("ol");
  c.practice.forEach((q) => {
    const li = el("li");
    li.append(el("div", "q", q.prompt));
    if (q.choices.length) {
      const choices = el("ol", "choices");
      q.choices.forEach((ch) => choices.append(el("li", null, ch)));
      li.append(choices);
    }
    const meta = el("div", "q-meta");
    meta.append(
      el("span", "badge", label(q.topic)),
      el("span", "badge", label(q.qtype)),
      el("span", `badge ${q.verified ? "ok" : "warn"}`, q.verified ? "answer double-checked" : "answer unchecked"),
    );
    li.append(meta);
    if (q.source) {
      const from = el("div", "q-source", "From ");
      if (/^https?:\/\//i.test(q.source_url || "")) {
        const a = el("a", null, q.source);
        a.href = q.source_url;
        a.target = "_blank";
        a.rel = "noopener";
        from.append(a);
      } else {
        from.append(q.source);
      }
      li.append(from);
    }
    const det = el("details");
    det.append(el("summary", null, "Show answer"), el("p", null, q.answer));
    if (q.explanation) det.append(el("p", "muted small", q.explanation));
    li.append(det);
    ol.append(li);
  });
  box.append(ol);
  return box;
}

async function startAssignment(id) {
  try {
    const r = await post(`/api/assignments/${id}/start`);
    toast(r.block.first_step ? `Go: ${r.block.first_step}` : "Go!", 6000);
    await refresh(true);
  } catch (e) {
    toast(e.message);
  }
}

// ---- dashboard ---------------------------------------------------------------------------------

async function loadCourses() {
  try {
    courses = await api("/api/courses");
  } catch {
    return;
  }
  const sel = $("#course-select");
  const saved = store.get("course", null);
  sel.replaceChildren(
    ...courses.map((c) => {
      const o = el("option", null, c.name);
      o.value = c.id;
      return o;
    }),
  );
  if (saved && courses.some((c) => String(c.id) === String(saved))) sel.value = saved;
}

async function loadDashboard() {
  if (!courses.length) await loadCourses();
  const id = $("#course-select").value;
  if (!id) {
    $("#grid").replaceChildren(el("p", "empty", "No courses yet. Sync with Canvas first."));
    return;
  }
  try {
    const [d, s] = await Promise.all([api(`/api/dashboard?course_id=${id}`), api("/api/stats")]);
    renderGrid(d.grid);
    renderWeakest(d.weakest);
    renderRows(d.rows);
    renderEvidence(d.evidence);
    renderStats(s);
  } catch (e) {
    toast(e.message);
  }
}

function renderGrid(grid) {
  const root = $("#grid");
  if (!grid.cells.length) {
    root.replaceChildren(el("p", "empty", "No graded tests for this course yet. Add a photo below."));
    return;
  }
  const byKey = new Map(grid.cells.map((c) => [`${c.topic}|${c.qtype}`, c]));
  const table = el("table");
  const head = el("tr");
  head.append(el("th", null, "Topic \\ type"), ...grid.qtypes.map((q) => el("th", null, label(q))));
  table.append(head);
  for (const topic of grid.topics) {
    const tr = el("tr");
    tr.append(el("th", null, label(topic)));
    for (const qtype of grid.qtypes) {
      const c = byKey.get(`${topic}|${qtype}`);
      const td = el("td", "cell");
      if (c) {
        const pct = Math.round(c.fraction * 100);
        td.append(document.createTextNode(`${pct}% lost`), el("small", null, `${c.lost}/${c.possible} marks · ${c.count} q`));
        td.style.background = `color-mix(in srgb, var(--accent) ${Math.max(6, pct)}%, transparent)`;
        if (pct >= 50) td.classList.add("hot");
      } else td.textContent = "–";
      tr.append(td);
    }
    table.append(tr);
  }
  root.replaceChildren(table);
}

function renderWeakest(cells) {
  const root = $("#weakest");
  if (!cells.length) return root.replaceChildren();
  const ul = el("ul", "weak-list");
  cells.forEach((c) => {
    const li = el("li");
    li.append(el("strong", null, `${label(c.topic)} · ${label(c.qtype)}`), document.createTextNode(` (${Math.round(c.fraction * 100)}% lost)`));
    c.examples.forEach((e) => li.append(el("div", "muted small", `${e.question} → “${e.student_answer}”${e.teacher_note ? ` (${e.teacher_note})` : ""}`)));
    ul.append(li);
  });
  root.replaceChildren(el("h3", null, "Weakest spots (the practice sets aim here)"), ul);
}

function renderRows(rows) {
  const root = $("#rows");
  if (!rows.length) return root.replaceChildren();
  const table = el("table");
  const head = el("tr");
  ["Test", "Question", "Answer", "Marks", "Teacher note", "Topic", "Type", ""].forEach((h) => head.append(el("th", null, h)));
  table.append(head);
  rows.forEach((r) => {
    const tr = el("tr");
    const marks = r.marks_possible != null ? `${r.marks ?? "?"}/${r.marks_possible}` : "–";
    [r.source, r.question, r.student_answer, marks, r.teacher_note, label(r.topic), label(r.qtype)].forEach((v) => tr.append(el("td", null, v ?? "")));
    const td = el("td");
    td.append(
      button("Delete", "ghost", async () => {
        if (!confirm("Delete this row?")) return;
        try {
          await api(`/api/tests/rows/${r.id}`, { method: "DELETE" });
          loadDashboard();
        } catch (e) {
          toast(e.message);
        }
      }),
    );
    tr.append(td);
    table.append(tr);
  });
  root.replaceChildren(el("p", "muted small", "Fix a misread row at /docs (PUT /api/tests/rows/{id}) or delete it here."), table);
}

function renderEvidence(items) {
  const root = $("#evidence");
  if (!items.length) return root.replaceChildren(el("li", "muted", "Nothing yet: graded work with comments or rubric scores shows up here."));
  root.replaceChildren(
    ...items.map((i) => {
      if (i.source === "rubric") {
        return el("li", null, `${i.assignment}: lost ${i.points_lost}/${i.points_possible} on “${i.criterion}”${i.teacher_comment ? `: “${i.teacher_comment}”` : ""}`);
      }
      if (i.source === "comment") return el("li", null, `${i.author || "Teacher"} on ${i.assignment}: “${i.text}”`);
      return el("li", null, `${i.test}: ${i.question}${i.teacher_note ? ` (“${i.teacher_note}”)` : ""}`);
    }),
  );
}

function renderStats(s) {
  const stat = (n, text) => {
    const d = el("div", "stat");
    d.append(el("b", null, n ?? "–"), el("span", "muted small", text));
    return d;
  };
  $("#stats").replaceChildren(
    stat(s.pings, "pings sent"),
    stat(s.starts, "starts"),
    stat(s.avg_minutes_to_start, "avg minutes from ping to start"),
    stat(s.answers.plus_15 ?? 0, "+15 min snoozes"),
    stat(s.blocks.missed ?? 0, "missed blocks"),
  );
}

$("#course-select").addEventListener("change", (e) => {
  store.set("course", e.target.value);
  loadDashboard();
});

$("#upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("#upload-file").files[0];
  const courseId = $("#course-select").value;
  if (!file || !courseId) return toast("Pick a course and a photo");
  const form = new FormData();
  form.append("course_id", courseId);
  form.append("file", file);
  const status = $("#upload-status");
  status.textContent = "Reading the photo… this takes a little while.";
  try {
    const r = await api("/api/tests/upload", { method: "POST", body: form });
    status.textContent = `Saved ${r.saved} question${r.saved === 1 ? "" : "s"}.`;
    $("#upload-form").reset();
    loadDashboard();
  } catch (err) {
    status.textContent = err.message;
  }
});

// ---- tabs, clock, sync -------------------------------------------------------------------------

function showTab(name) {
  document.querySelectorAll("[role=tab]").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  store.set("tab", name);
  if (name === "dashboard") loadDashboard();
}
document.querySelectorAll("[role=tab]").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

document.querySelectorAll("[data-clock]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const v = btn.dataset.clock;
    let body;
    if (v === "set") {
      const val = $("#clock-input").value;
      if (!val) return toast("Choose a date and time first");
      body = { action: "set", when: new Date(val).toISOString() };
    } else if (v === "reset") body = { action: "reset" };
    else body = { action: "advance", minutes: Number(v) };
    try {
      const r = await post("/api/clock", body);
      const n = r.events.pinged.length;
      if (n) toast(`${n} ping${n > 1 ? "s" : ""} sent`);
      else if (r.events.missed.length) toast("A start was missed");
      await refresh(true);
    } catch (e) {
      toast(e.message);
    }
  }),
);

$("#sync-btn").addEventListener("click", async () => {
  $("#sync-btn").disabled = true;
  $("#sync-status").textContent = "Syncing…";
  try {
    const r = await post("/api/sync");
    toast(`Synced ${r.assignments} assignments · ${r.cards} new cards · ${r.blocks} new blocks`);
  } catch (e) {
    toast(e.message, 6000);
  }
  courses = [];
  await refresh(true);
});

// ---- notifications -----------------------------------------------------------------------------

const canNotify = () => typeof Notification !== "undefined";

function b64ToBytes(s) {
  const padded = s + "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

function updateNotifyBtn() {
  const btn = $("#notify-btn");
  if (!canNotify()) return (btn.hidden = true);
  const granted = Notification.permission === "granted";
  const pushAvailable = state?.config.push;
  btn.textContent = granted ? (pushSubscribed ? "Push on" : "Notifications on") : "Turn on notifications";
  btn.disabled = granted && (pushSubscribed || !pushAvailable);
}

async function enableNotifications() {
  const perm = await Notification.requestPermission();
  if (perm !== "granted") return toast("Notifications are blocked in this browser's settings");
  const reg = await swReady;
  if (state?.config.push && state.config.push_key && reg?.pushManager) {
    try {
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(state.config.push_key) });
      await post("/api/push/subscribe", sub.toJSON());
      pushSubscribed = true;
      toast("Push notifications on, even with this page closed");
    } catch (e) {
      toast(`Push didn't work (${e.message}); you'll still get notifications while this page is open`, 6000);
    }
  } else toast("Notifications on while this page is open");
  updateNotifyBtn();
}
$("#notify-btn").addEventListener("click", enableNotifications);

async function showLocalNotification(title, body, blockId) {
  const opts = { body, tag: `block-${blockId}`, icon: "/static/icons/icon-192.png", data: { url: `/?block=${blockId}` } };
  const reg = await swReady;
  if (reg) return reg.showNotification(title, opts);
  new Notification(title, opts);
}

// Pings found by polling become notifications here unless push already delivered them.
function notifyNew() {
  const keys = state.checkin.map((b) => `${b.id}:${b.status}`);
  if (firstPoll) {
    keys.forEach((k) => notified.add(k));
    firstPoll = false;
    return;
  }
  for (const b of state.checkin) {
    const key = `${b.id}:${b.status}`;
    if (notified.has(key)) continue;
    notified.add(key);
    if (pushSubscribed || !canNotify() || Notification.permission !== "granted") continue;
    const missed = b.status === "missed";
    showLocalNotification(
      missed ? `Missed start: ${b.name}` : `Time to start: ${b.name}`,
      missed ? "When will you start? Tap to pick a time." : b.first_step || "Open your prep card.",
      b.id,
    );
  }
  if (!syncTimer) syncTimer = setInterval(refresh, 1000); // show progress while the POST runs
}

// ---- boot --------------------------------------------------------------------------------------

async function boot() {
  const reg = await swReady;
  if (reg?.pushManager) {
    try {
      pushSubscribed = !!(await reg.pushManager.getSubscription());
    } catch {}
  }
  showTab(store.get("tab", "cards"));
  await refresh(true);
  $("#clock-input").value = toLocalInput(now());

  // Opened from a notification (/?block=ID): jump to that check-in.
  const params = new URLSearchParams(location.search);
  const blockId = params.get("block");
  if (blockId) {
    history.replaceState(null, "", "/");
    const box = document.getElementById(`checkin-${blockId}`);
    if (box) flash(box);
  }

  setInterval(renderClock, 1000);
  setInterval(refresh, POLL_MS);
  document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && refresh());
}

boot();

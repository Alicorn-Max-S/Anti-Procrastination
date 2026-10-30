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
let pushState = "off"; // "on": this browser is subscribed and the server has it; "failed": subscribing threw
let pushError = "";
let clockJustMoved = false; // the clock bar already says what a clock change did, so no extra toast
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
  updateNotify();
  updateTitle();
  closeAnswered();
  scheduleNextStart();
  if (reloadCards || s.sync.last !== lastSyncSeen) {
    lastSyncSeen = s.sync.last;
    await loadCards();
  }
}

// "early morning" next to 4:30 AM makes a wrong AM/PM obvious at a glance.
function partOfDay(d) {
  const h = d.getHours();
  if (h < 4) return "night";
  if (h < 9) return "early morning";
  if (h < 12) return "morning";
  if (h < 17) return "afternoon";
  if (h < 21) return "evening";
  return "night";
}

// "in 25 min", "in 3 h", "in 2 days"
function inText(ms) {
  const min = Math.round(ms / 60_000);
  if (min <= 0) return "now";
  if (min < 60) return `in ${min} min`;
  const h = Math.round(min / 60);
  return h < 36 ? `in ${h} h` : `in ${Math.round(h / 24)} days`;
}

function renderClock() {
  if (!state) return;
  const n = now();
  $("#clock-day").textContent = n.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  $("#clock-hm").textContent = n.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  $("#clock-part").textContent = partOfDay(n);
  $("#clock-badge").hidden = !state.simulated;
  const next = state.next_start;
  $("#clock-next").textContent = next
    ? `Next start: ${next.name} · ${fmtDayTime(new Date(next.start))} (${inText(new Date(next.start) - n)})`
    : "Nothing planned ahead";
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

// Pings stay in the list above the tabs; missed starts go in a modal that sits over the page
// until each one is answered.
function renderCheckin() {
  // "In 15 min" moves with the clock: relabel it in place rather than redraw (which would close an
  // open picker).
  const in15 = `In 15 min · ${fmtDayTime(new Date(now().getTime() + 15 * 60_000))}`;
  document.querySelectorAll("button[data-in15]").forEach((btn) => (btn.textContent = in15));
  const key = [
    notifyPermission(),
    ...state.checkin.map((b) => `${b.id}:${b.status}:${b.replacement?.start ?? ""}:${(b.choices || []).map((c) => (c.key === "in_15" ? c.key : c.start)).join("|")}`),
  ].join(",");
  if (key === renderedKeys.checkin) return; // don't wipe a time the student is typing
  renderedKeys.checkin = key;
  const pings = state.checkin.filter((b) => b.status !== "missed");
  const misses = state.checkin.filter((b) => b.status === "missed");
  const notice = state.checkin.length ? notifyNotice() : null;
  const listNotice = notice && !misses.length ? [notice] : [];
  $("#checkin").replaceChildren(...listNotice, ...pings.map(checkinBox));
  const modal = $("#miss-modal");
  const wasOpen = !modal.hidden;
  modal.hidden = !misses.length;
  document.body.classList.toggle("modal-open", misses.length > 0);
  $("#miss-modal-title").textContent = misses.length > 1 ? `${misses.length} missed starts` : "Missed start";
  $("#miss-modal-list").replaceChildren(...(notice && misses.length ? [notice] : []), ...misses.map(checkinBox));
  if (misses.length && !wasOpen) modal.querySelector("button.primary")?.focus();
}

// The check-in's buttons, on the page and on notifications (sw.js posts `answer` and
// `picked_time`): Start now; "In 15 min" and/or "Next work time" from the server's `choices`, each
// with its day and time; Pick a time…. A missed block is already planned at its next work time,
// so that button keeps it there rather than moving it.
function startOptions(b) {
  const out = [{ action: "start_now", title: "Start now", answer: "start_now" }];
  for (const c of b.choices || []) {
    const d = new Date(c.start);
    if (c.key === "in_15") out.push({ action: "in_15", title: `In 15 min · ${fmtDayTime(d)}`, answer: "plus_15" });
    if (c.key === "next") {
      const keep = b.replacement && new Date(b.replacement.start).getTime() === d.getTime();
      out.push({ action: "next", title: `Next work time · ${fmtDayTime(d)}`, answer: keep ? "keep" : "pick_time", picked_time: keep ? null : c.start });
    }
  }
  out.push({ action: "pick", title: "Pick a time…" });
  return out;
}

// The buttons row, each acting at once, plus the picker that "Pick a time…" opens.
function startButtons(b) {
  const row = el("div", "actions");
  const picker = pickControl(b);
  for (const o of startOptions(b)) {
    if (o.action === "pick") {
      const toggle = button(o.title, null, () => {
        picker.hidden = !picker.hidden;
        toggle.setAttribute("aria-expanded", String(!picker.hidden));
        if (!picker.hidden) picker.querySelector("input").focus();
      });
      toggle.setAttribute("aria-expanded", "false");
      row.append(toggle);
      continue;
    }
    const btn = button(o.title, o.action === "start_now" ? "primary" : null, () => answer(b.id, o.answer, o.picked_time));
    if (o.action === "in_15") btn.dataset.in15 = "";
    row.append(btn);
  }
  return [row, picker];
}

// "Pick a time…": a time (the next work time to start with) and one button that moves the block
// there, with what else that shifts ("Lab: Relative Velocity shifts to Sat 6:15 PM.").
function pickControl(b) {
  const wrap = el("div", "pick");
  wrap.hidden = true;
  const input = el("input");
  input.type = "datetime-local";
  input.setAttribute("aria-label", "Start time");
  const move = button("Move", null, () => {
    if (!input.value) return toast("Choose a time first");
    answer(b.id, "pick_time", new Date(input.value).toISOString());
  });
  const effect = el("p", "effect muted small");
  effect.setAttribute("aria-live", "polite");
  let asked = 0;
  const update = async () => {
    if (!input.value) return;
    const d = new Date(input.value);
    move.textContent = `Move to ${fmtDayTime(d)}`;
    const mine = ++asked;
    try {
      const r = await post(`/api/blocks/${b.id}/preview`, { answer: "pick_time", picked_time: d.toISOString() });
      if (mine === asked) effect.textContent = previewText(r);
    } catch (e) {
      if (mine === asked) effect.textContent = e.message;
    }
  };
  input.addEventListener("input", update);
  const start = (b.choices || []).find((c) => c.key === "next") || b.choices?.[0];
  input.value = toLocalInput(start ? new Date(start.start) : new Date(Math.ceil((now().getTime() + 15 * 60_000) / 900_000) * 900_000));
  update();
  const row = el("div", "actions");
  row.append(input, move);
  wrap.append(row, effect);
  return wrap;
}

// What else a picked time moves: "Lab: Relative Velocity shifts to Sat 6:15 PM." The button
// already says where this block goes.
function previewText(r) {
  const parts = r.moved.map((m) => `${m.name} shifts to ${fmtDayTime(new Date(m.start))}${m.at_risk ? " (after it's due)" : ""}.`);
  if (!parts.length) parts.push("Nothing else moves.");
  if (r.block.at_risk) parts.unshift("That ends after it's due.");
  return parts.join(" ");
}

function checkinBox(b) {
  const missed = b.status === "missed";
  const box = el("div", `checkin${missed ? " missed" : ""}`);
  box.id = `checkin-${b.id}`;
  if (missed) box.append(el("h2", null, `You missed ${b.name} (planned ${fmtDayTime(new Date(b.start))}). When will you start?`));
  else {
    box.append(el("h2", null, `Time to start: ${b.name}`));
    box.append(el("p", null, b.first_step ? `First step: ${b.first_step}` : "Open the prep card for your first step."));
  }
  box.append(...startButtons(b));
  return box;
}

// What a card shows when its start was missed or its due date passed (start-loop owns this;
// renderCard puts it above the first step).
function cardAlert(c) {
  if (c.done) return null;
  if (c.past_due) {
    const box = el("div", "card-alert late");
    box.append(
      el("strong", null, `Past due since ${fmtDayTime(new Date(c.due_at))}`),
      el("p", null, "Canvas doesn't show it turned in. Do it now and turn it in late, or mark it done if you handed it in on paper."),
    );
    const actions = el("div", "actions");
    if (/^https?:\/\//i.test(c.html_url || "")) {
      const a = el("a", "button primary", "Open in Canvas");
      a.href = c.html_url;
      a.target = "_blank";
      a.rel = "noopener";
      actions.append(a);
    }
    actions.append(
      button("Start now", null, () => startAssignment(c.assignment_id)),
      button("I turned it in", "ghost", async () => {
        try {
          await post(`/api/assignments/${c.assignment_id}/done`);
          renderedKeys.week = "";
          await refresh(true);
        } catch (e) {
          toast(e.message);
        }
      }),
    );
    box.append(actions);
    return box;
  }
  if (c.missed) {
    const box = el("div", "card-alert missed");
    box.append(el("strong", null, `Missed the start planned for ${fmtDayTime(new Date(c.missed.start))}. When will you start?`));
    const entry = state?.checkin.find((b) => b.id === c.missed.block_id) || { id: c.missed.block_id, choices: [] };
    box.append(...startButtons(entry));
    return box;
  }
  return null;
}

// This browser's push endpoint, so the server tells only the student's other devices to clear
// their notification (this page closes its own).
async function pushEndpoint() {
  try {
    return (await (await swReady)?.pushManager?.getSubscription())?.endpoint || "";
  } catch {
    return "";
  }
}

async function answer(blockId, ans, pickedTime) {
  try {
    const r = await post(`/api/blocks/${blockId}/checkin`, { answer: ans, picked_time: pickedTime ?? null, endpoint: await pushEndpoint() });
    movedIds = new Set([...r.moved, r.block.id]); // r.block is the new block when a miss was answered
    renderedKeys.week = "";
    if (ans === "start_now") toast(r.block.first_step ? `Go: ${r.block.first_step}` : "Go!", 6000);
    else if (ans === "plus_15") toast("OK, I'll ping you again in 15 minutes.");
    else {
      const shifted = r.moved.length ? ` ${r.moved.length} other block${r.moved.length > 1 ? "s" : ""} moved to make room.` : "";
      toast(`Set for ${fmtDayTime(new Date(r.block.start))}. I'll ping you then.${shifted}`, 5000);
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
  // Ask where notes live once per course: on its first open card that has the student write.
  const asking = new Set();
  const askedCourses = new Set();
  for (const c of cards) {
    if (c.done || !c.writes || c.notes_medium || !c.course_id || askedCourses.has(c.course_id)) continue;
    if (store.get(`notesAskSkipped:${c.course_id}`, false)) continue;
    askedCourses.add(c.course_id);
    asking.add(c.assignment_id);
  }
  root.replaceChildren(...cards.map((c) => renderCard(c, asking.has(c.assignment_id))));
}

function renderCard(c, askNotes = false) {
  const card = el("article", `card${c.done ? " done" : ""}`);
  card.id = `card-${c.assignment_id}`;

  const meta = el("div", "card-meta");
  meta.append(el("span", "badge", label(c.kind)));
  if (c.course) meta.append(el("span", null, c.course));
  meta.append(el("span", null, `· ${dueText(c.due_at)}`));
  card.append(meta, el("h2", null, c.name || `Assignment ${c.assignment_id}`));
  if (c.summary) card.append(el("p", "summary", c.summary));
  if (c.reading) card.append(el("p", "reading-length", readingText(c.reading)));
  if (!c.instructions_seen) {
    card.append(el("p", "note", "Start Helper couldn't read the instructions for this one, so the steps are general. Find the handout first."));
  }
  if (c.generated_by === "fallback" && c.model_error) {
    card.append(el("p", "muted small", `Template card: the model call failed (${c.model_error}). Regenerate to try again.`));
  }

  const alert = cardAlert(c); // missed start / past due (start-loop)
  if (alert) card.append(alert);

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
  if (c.sources && c.sources.length) card.append(renderSources(c.sources));
  if (askNotes) card.append(notesAsk(c));
  if (c.stale) card.append(el("p", "muted small", "This card will be rebuilt on the next sync."));

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
  foot.append(
    button(c.done ? "Undo done" : "Mark done", "ghost", async () => {
      try {
        await post(`/api/assignments/${c.assignment_id}/${c.done ? "undone" : "done"}`);
        renderedKeys.week = "";
        await refresh(true);
        if (c.done) toast(`${c.name} is back on your list.`);
      } catch (e) {
        toast(e.message);
      }
    }),
  );
  card.append(foot);
  return card;
}

// "35 pages (pp. 177–211) · about 62 min of reading · 3 sittings"
function readingText(r) {
  const pages = r.first_page === r.last_page ? `p. ${r.first_page}` : `pp. ${r.first_page}–${r.last_page}`;
  const sittings = r.sittings.length > 1 ? ` · ${r.sittings.length} sittings` : "";
  return `${r.pages} page${r.pages === 1 ? "" : "s"} (${pages}) · about ${r.minutes} min of reading${sittings}`;
}

// What the card was built from: "Read: The Unbreakable Chain (35 pages) · Couldn't open: KEY (shared only inside the school)"
function renderSources(sources) {
  const box = el("div", "sources small");
  const read = sources.filter((s) => s.read);
  const unread = sources.filter((s) => !s.read);
  const link = (text, url, cls = null) => {
    const a = el("a", cls, text);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    return a;
  };
  const item = (s, why) => {
    const span = el("span", "source");
    // A Google file the server couldn't fetch: opening it in Chrome lets the extension save it for the next sync.
    const openDoc = why && /^https:\/\/(docs|drive)\.google\.com\//i.test(s.url || "");
    const web = s.url && /^https?:\/\//i.test(s.url) && !openDoc;
    span.append(web ? link(s.name, s.url) : el("span", null, s.name));
    // A read file's reason is a note on how it was read, e.g. "read from the page view: downloading is turned off".
    const pages = !why && s.pages ? `${s.pages} page${s.pages === 1 ? "" : "s"}` : "";
    const extra = [pages, s.reason].filter(Boolean).join(", ");
    if (extra) span.append(document.createTextNode(` (${extra})`));
    if (openDoc) span.append(document.createTextNode(" "), link("Open this doc", s.url, "open-doc"));
    return span;
  };
  const line = (label, list, why) => {
    const p = el("p", why ? "unread" : null);
    p.append(el("strong", null, label));
    list.forEach((s, i) => {
      if (i) p.append(document.createTextNode(" · "));
      p.append(item(s, why));
    });
    return p;
  };
  if (read.length) box.append(line("Read: ", read, false));
  if (unread.length) box.append(line("Couldn't open: ", unread, true));
  return box;
}

// ---- where each course's notes live -----------------------------------------------------------

// Save it; the server marks that course's upcoming cards stale, so the next sync rebuilds them.
async function setNotes(courseId, courseName, medium) {
  try {
    const r = await api(`/api/courses/${courseId}/notes`, { method: "PUT", body: JSON.stringify({ medium }) });
    cards.forEach((c) => {
      if (c.course_id !== courseId) return;
      c.notes_medium = medium;
      if (r.stale.includes(c.assignment_id)) c.stale = true;
    });
    courses.forEach((c) => {
      if (c.id === courseId) c.notes_medium = medium;
    });
    renderCards();
    renderNotesSettings();
    const n = r.stale.length;
    toast(n ? `Saved. ${n} ${courseName} card${n === 1 ? "" : "s"} will update on the next sync.` : `Saved for ${courseName}.`);
  } catch (e) {
    toast(e.message);
  }
}

// "Do you keep US History notes on paper or in a doc?", once per course, on a card with writing steps.
function notesAsk(c) {
  const box = el("div", "notes-ask");
  box.append(el("p", null, `Do you keep ${c.course || "this course's"} notes on paper or in a doc?`));
  const actions = el("div", "actions");
  actions.append(
    button("On paper", "ghost", () => setNotes(c.course_id, c.course, "paper")),
    button("In a doc", "ghost", () => setNotes(c.course_id, c.course, "doc")),
    button("Not now", "ghost", () => {
      store.set(`notesAskSkipped:${c.course_id}`, true);
      renderCards();
    }),
  );
  box.append(actions);
  return box;
}

// Dashboard: one choice per course that has cards (or already has a setting).
function renderNotesSettings() {
  const root = $("#notes-settings");
  if (!root) return;
  const withCards = new Set(cards.map((c) => c.course_id));
  const list = courses.filter((c) => withCards.has(c.id) || c.notes_medium);
  if (!list.length) {
    root.replaceChildren(el("p", "empty", "No courses with cards yet. Sync with Canvas first."));
    return;
  }
  root.replaceChildren(
    ...list.map((course) => {
      const row = el("div", "notes-row");
      const select = el("select", null);
      select.id = `notes-${course.id}`;
      [["", "Not set"], ["paper", "On paper"], ["doc", "In a doc"]].forEach(([value, text]) => {
        const o = el("option", null, text);
        o.value = value;
        select.append(o);
      });
      select.value = course.notes_medium || "";
      select.addEventListener("change", () => setNotes(course.id, course.name, select.value || null));
      const name = el("label", null, course.name);
      name.htmlFor = select.id;
      row.append(name, select);
      return row;
    }),
  );
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
  const up = $("#upload-course");
  up.replaceChildren(up.options[0], ...courses.map((c) => {
    const o = el("option", null, c.name);
    o.value = c.id;
    return o;
  }));
}

const courseName = (id) => courses.find((c) => c.id === id)?.name;
const num = (x) => (x == null ? "?" : String(Math.round(x * 100) / 100));

// One line for a graded test's score: "82.5% (80.5/100 + 2 bonus)".
function scoreText(t) {
  const parts = [];
  if (t.score != null) parts.push(t.score_possible === 100 ? `${num(t.score)}%` : `${num(t.score)}/${num(t.score_possible)}`);
  if (t.earned != null && t.possible) parts.push(`(${num(t.earned)}/${num(t.possible)}${t.bonus ? ` + ${num(t.bonus)} bonus` : ""})`);
  return parts.join(" ") || "no score read";
}

// A select that files a test under another course; the first option says where it is now.
function moveControl(t) {
  const sel = el("select");
  sel.setAttribute("aria-label", "Move this test to another course");
  const here = el("option", null, t.course_id ? "Move to…" : "Pick its course…");
  here.value = "";
  sel.append(here, ...courses.filter((c) => c.id !== t.course_id).map((c) => {
    const o = el("option", null, c.name);
    o.value = c.id;
    return o;
  }));
  sel.addEventListener("change", async () => {
    if (!sel.value) return;
    try {
      await api(`/api/tests/${t.id}/course`, { method: "PUT", body: JSON.stringify({ course_id: Number(sel.value) }) });
      toast(`Moved to ${courseName(Number(sel.value))}`);
      loadDashboard();
    } catch (e) {
      toast(e.message);
    }
  });
  return sel;
}

function testBox(t) {
  const box = el("div", `graded-test${t.needs_review ? " review" : ""}`);
  const head = el("div", "graded-head");
  head.append(el("strong", null, t.title || t.source), el("span", "graded-score", scoreText(t)));
  box.append(head);
  const where = el("div", "muted small graded-where");
  const filed = courseName(t.course_id);
  const detected = courseName(t.detected_course_id);
  where.append(document.createTextNode(
    filed
      ? `${filed}${t.detected_course_id === t.course_id ? " (detected from the test)" : detected ? ` (the test looks like ${detected})` : ""} · `
      : "Couldn't tell which course this is from · ",
  ));
  where.append(moveControl(t), button("Delete", "ghost small", async () => {
    if (!confirm("Delete this test and its questions?")) return;
    try {
      await api(`/api/tests/${t.id}`, { method: "DELETE" });
      loadDashboard();
    } catch (e) {
      toast(e.message);
    }
  }));
  box.append(where);
  const ul = el("ul", "graded-sections");
  t.sections.forEach((s) => {
    const score = s.bonus
      ? (s.earned != null ? `+${num(s.earned)} bonus` : "bonus")
      : `${num(s.earned)}/${num(s.possible)}${s.written ? "" : " (added up)"}`;
    const li = el("li");
    li.append(el("span", null, s.name), el("span", s.bonus ? "muted" : s.earned < s.possible ? "lost" : "full", score));
    if (s.questions) li.title = `${s.questions} questions, ${s.wrong} marked wrong · ${label(s.qtype)}`;
    ul.append(li);
  });
  box.append(ul);
  if (t.needs_review) {
    const warn = el("div", "graded-review");
    warn.append(el("strong", null, "Doesn't add up, check it: "));
    const list = el("ul");
    t.mismatches.forEach((m) => list.append(el("li", null, m)));
    warn.append(list);
    box.append(warn);
  }
  t.notes.forEach((n) => box.append(el("div", "muted small", n)));
  return box;
}

function renderTests(tests) {
  const root = $("#tests");
  root.replaceChildren(...tests.slice().reverse().map(testBox));
}

async function loadDashboard() {
  if (!courses.length) await loadCourses();
  renderNotesSettings();
  const id = $("#course-select").value;
  if (!id) {
    $("#grid").replaceChildren(el("p", "empty", "No courses yet. Sync with Canvas first."));
    return;
  }
  try {
    const [d, s] = await Promise.all([api(`/api/dashboard?course_id=${id}`), api("/api/stats")]);
    renderGrid(d.grid);
    renderWeakest(d.weakest);
    renderTests(d.tests);
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
        td.append(document.createTextNode(`${pct}% lost`), el("small", null, `${num(c.lost)}/${num(c.possible)} marks · ${c.count} q`));
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
  ["Section", "Question", "Answer", "Marks", "Teacher note", "Topic", "Type", ""].forEach((h) => head.append(el("th", null, h)));
  table.append(head);
  rows.forEach((r) => {
    const tr = el("tr");
    const mark = r.correct === true ? "✓ " : r.correct === false ? "✗ " : "";
    const marks = r.bonus
      ? `${mark}bonus`
      : r.marks_possible != null
        ? `${mark}${r.estimated ? "≈" : ""}${num(r.marks)}/${num(r.marks_possible)}`
        : `${mark}–`;
    [r.section || r.source, r.question, r.student_answer, marks, r.teacher_note, label(r.topic), label(r.qtype)].forEach((v) => tr.append(el("td", null, v ?? "")));
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
  root.replaceChildren(el("p", "muted small", "≈ marks are the section's lost points split across the questions marked wrong. Fix a misread row at /docs (PUT /api/tests/rows/{id}) or delete it here."), table);
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
  const courseId = $("#upload-course").value;
  if (!file) return toast("Pick a photo or scan");
  const form = new FormData();
  if (courseId) form.append("course_id", courseId);
  form.append("file", file);
  const status = $("#upload-status");
  status.textContent = "Reading it page by page… this takes a little while.";
  try {
    const r = await api("/api/tests/upload", { method: "POST", body: form });
    const t = r.test;
    const where = courseName(t.course_id);
    status.textContent = `${scoreText(t)} · ${r.saved} question${r.saved === 1 ? "" : "s"}${
      where ? ` · ${courseId ? "filed under" : "detected:"} ${where}` : " · couldn't tell the course, pick it below"
    }${t.needs_review ? " · doesn't add up, check it below" : ""}`;
    $("#upload-form").reset();
    if (t.course_id && String(t.course_id) !== $("#course-select").value) {
      $("#course-select").value = t.course_id;
      store.set("course", String(t.course_id));
    }
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

document.querySelectorAll("[data-clock]").forEach((btn) => btn.addEventListener("click", () => moveClock(btn.dataset.clock)));

async function moveClock(v) {
  let body;
  if (v === "set") {
    const val = $("#clock-input").value;
    if (!val) return clockMessage("Choose a date and time first.");
    body = { action: "set", when: new Date(val).toISOString() };
  } else if (v === "reset" || v === "next") body = { action: v };
  else body = { action: "advance", minutes: Number(v) };
  let r;
  try {
    r = await post("/api/clock", body);
  } catch (e) {
    return clockMessage(e.message);
  }
  serverOffset = Date.parse(r.now) - Date.now();
  clockMessage(clockSummary(r, v === "reset"), r);
  clockJustMoved = true;
  try {
    await refresh(true);
  } finally {
    clockJustMoved = false;
  }
  const first = r.pinged[0] || r.missed[0];
  if (first) flash(document.getElementById(`checkin-${first.id}`));
}

// "Clock set to Fri Oct 30, 4:30 AM (early morning). Nothing starts then. Next: Test #2, Fri 4:30 PM."
function clockSummary(r, reset) {
  const t = new Date(r.now);
  const when = `${fmtDay(t)}, ${fmtTime(t)} (${partOfDay(t)})`;
  const parts = [reset ? `Back to real time: ${when}.` : `Clock set to ${when}.`];
  const n = r.pinged.length;
  if (n) parts.push(`${n} ping${n > 1 ? "s" : ""} sent: ${r.pinged.map((b) => b.name).join(", ")}.`);
  r.missed.forEach((b) =>
    parts.push(
      b.replacement
        ? `Missed start: ${b.name}, moved to ${fmtDayTime(new Date(b.replacement.start))}.`
        : `Missed start: ${b.name}.`,
    ),
  );
  if (!n && !r.missed.length) {
    parts.push("Nothing starts then.");
    parts.push(r.next_start ? `Next: ${r.next_start.name}, ${fmtDayTime(new Date(r.next_start.start))}.` : "Nothing else is planned.");
  }
  return parts.join(" ");
}

// The clock bar's own line: stays until the next clock change, with the buttons that fit.
function clockMessage(text, r) {
  const box = $("#clock-msg");
  box.hidden = false;
  const actions = el("span", "actions");
  if (r && !r.pinged.length && !r.missed.length && r.next_start) {
    actions.append(button("Jump to next start", "primary", () => moveClock("next")));
  }
  if (r && (r.pinged.length || r.missed.length)) {
    const perm = notifyPermission();
    if (perm === "granted") actions.append(el("span", "muted small", "Notification sent."));
    else if (perm === "default") {
      actions.append(
        el("span", "warn-text", "Notifications are off, so it only shows on this page."),
        button("Turn on notifications", "primary", enableNotifications),
      );
    } else if (perm === "denied") {
      actions.append(el("span", "warn-text", "Notifications are blocked for this site, so it only shows on this page."));
    }
  }
  actions.append(button("×", "ghost close", () => (box.hidden = true)));
  box.replaceChildren(el("span", null, text), actions);
}

function renderClockPreview() {
  const v = $("#clock-input").value;
  const d = v ? new Date(v) : null;
  $("#clock-preview").textContent = d ? `${fmtDayTime(d)} · ${partOfDay(d)}` : "";
}
$("#clock-input").addEventListener("input", renderClockPreview);

$("#sync-btn").addEventListener("click", async () => {
  $("#sync-btn").disabled = true;
  $("#sync-status").textContent = "Syncing…";
  if (!syncTimer) syncTimer = setInterval(refresh, 1000); // show progress while the POST runs
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
const notifyPermission = () => (canNotify() ? Notification.permission : "unsupported");
const BLOCKED_HELP =
  "Notifications are blocked for this site. To allow them, click the icon left of the address bar, set Notifications to Allow, then reload.";

function b64ToBytes(s) {
  const padded = s + "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

function sameKey(sub, key) {
  const have = sub.options?.applicationServerKey;
  if (!have) return true; // can't tell: keep it
  const a = new Uint8Array(have);
  const b = b64ToBytes(key);
  return a.length === b.length && a.every((x, i) => x === b[i]);
}

// Push (works with the page closed): subscribe this browser and hand the subscription to the
// server. Runs on every load once permission is granted, so a new DB or new keys heal themselves.
async function syncPush() {
  const reg = await swReady;
  if (notifyPermission() !== "granted" || !reg?.pushManager) return;
  try {
    const { key } = await post("/api/push/setup");
    if (!key) return void (pushState = "off");
    let sub = await reg.pushManager.getSubscription();
    if (sub && !sameKey(sub, key)) {
      await sub.unsubscribe();
      sub = null;
    }
    sub = sub || (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) }));
    await post("/api/push/subscribe", sub.toJSON());
    pushState = "on";
    pushError = "";
  } catch (e) {
    pushState = "failed";
    pushError = `${e.name}: ${e.message}`;
    // so /api/health shows why push is off on this machine
    post("/api/push/error", { error: pushError, browser: navigator.userAgent }).catch(() => {});
  }
}

function updateNotify() {
  const btn = $("#notify-btn");
  const perm = notifyPermission();
  btn.hidden = perm === "unsupported";
  btn.disabled = false;
  if (perm === "granted") {
    btn.textContent = pushState === "on" ? "Notifications on · test" : "Notifications on (page open) · test";
    btn.title =
      pushState === "on"
        ? "Push is set up, so pings arrive even with this page closed. Click to send a test."
        : `Pings show while this page is open${pushError ? ` (push failed: ${pushError})` : ""}. Click to send a test.`;
  } else if (perm === "denied") {
    btn.textContent = "Notifications blocked";
    btn.title = BLOCKED_HELP;
  } else {
    btn.textContent = "Turn on notifications";
    btn.title = "Get a notification when it's time to start";
  }
  if (state) renderCheckin(); // its key includes the permission, so the notice follows it
}

// Shown above the check-ins when a ping can't reach the student outside this page.
function notifyNotice() {
  const perm = notifyPermission();
  if (perm === "granted") return null;
  const box = el("div", "notice");
  if (perm === "denied") box.append(el("span", null, BLOCKED_HELP));
  else if (perm === "unsupported") box.append(el("span", null, "This browser can't show notifications, so keep this page open to see pings."));
  else {
    box.append(
      el("span", null, "Notifications are off, so pings only show on this page."),
      button("Turn on notifications", "primary", enableNotifications),
    );
  }
  return box;
}

async function enableNotifications() {
  const perm = notifyPermission();
  if (perm === "unsupported") return toast("This browser can't show notifications.");
  if (perm === "denied") return toast(BLOCKED_HELP, 9000);
  if (perm === "default" && (await Notification.requestPermission()) !== "granted") {
    updateNotify();
    return toast(notifyPermission() === "denied" ? BLOCKED_HELP : "Notifications stay off for now.", 7000);
  }
  if (pushState !== "on") await syncPush();
  updateNotify();
  if (pushState === "failed") toast(`Push didn't work (${pushError}), so pings arrive while this page is open.`, 9000);
  await testNotification();
}
$("#notify-btn").addEventListener("click", enableNotifications);

// Push when this browser is subscribed (proves the whole path), else a local one.
async function testNotification() {
  const help = "Nothing popped up? Check Windows Settings › System › Notifications (Do not disturb, and your browser allowed).";
  try {
    if (pushState === "on") {
      const r = await post("/api/push/test");
      if (r.sent) return toast(`Test sent by push. ${help}`, 9000);
    }
    const shown = await showLocalNotification("Start Helper", { body: "Notifications work.", tag: "test", data: { url: "/" } });
    toast(shown ? `Test notification shown. ${help}` : "The browser refused to show a notification.", 9000);
  } catch (e) {
    toast(e.message);
  }
}

// The same options sw.js gives a pushed ping: the check-in's buttons (startOptions; a laptop shows
// the first two), and a missed start stays on screen until answered (and buzzes a phone).
function notifyOptions(b, renotify) {
  const missed = b.status === "missed";
  const actions = startOptions(b);
  return {
    body: missed ? `Planned for ${fmtDayTime(new Date(b.start))}. When will you start?` : b.first_step || "Open your prep card.",
    tag: `block-${b.id}`,
    data: { url: `/?block=${b.id}`, block_id: b.id, kind: missed ? "missed" : "ping", actions },
    requireInteraction: missed,
    renotify,
    vibrate: missed ? [300, 100, 300, 100, 300] : [200, 100, 200],
    actions: actions.map(({ action, title }) => ({ action, title })),
  };
}

// Through the service worker when there is one (needed on Android and for the buttons), else the
// page's own (no buttons there).
async function showLocalNotification(title, opts) {
  const full = { icon: "/static/icons/icon-192.png", badge: "/static/icons/icon-192.png", ...opts };
  try {
    const reg = await swReady;
    if (reg) {
      await reg.showNotification(title, full);
      return true;
    }
    const { actions, ...plain } = full;
    new Notification(title, plain);
    return true;
  } catch {
    return false;
  }
}

// New pings, misses and reminders found by polling: a notification (push may have shown it
// already: the same tag replaces it, and only alerts again when push isn't doing it), a toast or
// chime, and the title. A reminder is a miss whose `notified` count went up.
function notifyNew() {
  const keyOf = (b) => `${b.id}:${b.status}:${b.notified ?? 0}`;
  if (firstPoll) {
    state.checkin.forEach((b) => notified.add(keyOf(b)));
    firstPoll = false;
    return;
  }
  const fresh = state.checkin.filter((b) => !notified.has(keyOf(b)));
  for (const b of fresh) {
    notified.add(keyOf(b));
    if (notifyPermission() !== "granted") continue;
    const missed = b.status === "missed";
    const renotify = missed && (b.notified ?? 1) > 1 && pushState !== "on";
    showLocalNotification(missed ? `Missed start: ${b.name}` : `Time to start: ${b.name}`, notifyOptions(b, renotify));
  }
  if (fresh.some((b) => b.status === "missed")) chime();
  if (fresh.length && !clockJustMoved) {
    const b = fresh[0];
    if (b.status !== "missed") {
      toast(`Time to start: ${b.name}`, 6000);
      flash(document.getElementById(`checkin-${b.id}`));
    }
  }
}

// Answered somewhere else (another device, a notification button): its notification goes too.
async function closeAnswered() {
  const reg = await swReady;
  if (!reg?.getNotifications) return;
  const live = new Set(state.checkin.map((b) => `block-${b.id}`));
  try {
    (await reg.getNotifications()).forEach((n) => n.tag.startsWith("block-") && !live.has(n.tag) && n.close());
  } catch {}
}

// The tab title counts check-ins, and flashes the missed start's name while one is waiting.
let titleTimer = null;
let titleFlip = false;
function updateTitle() {
  const base = state.checkin.length ? `(${state.checkin.length}) Start Helper` : "Start Helper";
  const miss = state.checkin.find((b) => b.status === "missed");
  if (!miss) {
    clearInterval(titleTimer);
    titleTimer = null;
    document.title = base;
    return;
  }
  const alertTitle = `Missed start: ${miss.name}`;
  if (titleTimer) clearInterval(titleTimer);
  titleTimer = setInterval(() => {
    titleFlip = !titleFlip;
    document.title = titleFlip ? alertTitle : base;
  }, 1000);
}

// A short two-note chime. Browsers only allow sound after the student has clicked or typed on the
// page once, so the audio context is made (or resumed) on the first click or key press.
let audio = null;
function unlockAudio() {
  try {
    audio = audio || new AudioContext();
    if (audio.state === "suspended") audio.resume();
  } catch {}
}
document.addEventListener("pointerdown", unlockAudio);
document.addEventListener("keydown", unlockAudio);

function chime() {
  if (!audio || audio.state !== "running") return false;
  const t0 = audio.currentTime;
  [880, 660].forEach((freq, i) => {
    const t = t0 + i * 0.18;
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    osc.connect(gain).connect(audio.destination);
    osc.start(t);
    osc.stop(t + 0.17);
  });
  return true;
}

// Polling runs every 10 s; this lands a refresh just after the next start (and the next miss), so
// the page's own notification is on time too. Set from a fetch callback, not a timer, so Chrome's
// background-tab throttling of chained timers doesn't apply.
let startTimer = null;
function scheduleNextStart() {
  clearTimeout(startTimer);
  const times = [];
  if (state.next_start) times.push(new Date(state.next_start.start).getTime());
  state.checkin.filter((b) => b.status === "pinged").forEach((b) => times.push(new Date(b.start).getTime() + 15 * 60_000));
  const ms = Math.min(...times.map((t) => t - now().getTime()).filter((d) => d > -1000)) + 800;
  if (Number.isFinite(ms) && ms < 60 * 60_000) startTimer = setTimeout(() => refresh(), Math.max(ms, 0));
}

// A tap on a notification while the page is open: the service worker focuses it and says which block.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", async (e) => {
    if (e.data?.type === "answered") return refresh(true);
    if (e.data?.type !== "open-block") return;
    await refresh(true);
    focusBlock(e.data.blockId);
  });
}

function focusBlock(blockId) {
  const box = document.getElementById(`checkin-${blockId}`);
  if (box) return flash(box);
  const b = state?.blocks.find((x) => String(x.id) === String(blockId));
  if (b) {
    showTab("cards");
    flash(document.getElementById(`card-${b.assignment_id}`));
  }
}

// ---- boot --------------------------------------------------------------------------------------

async function boot() {
  await syncPush();
  showTab(store.get("tab", "cards"));
  await refresh(true);
  $("#clock-input").value = toLocalInput(now());
  renderClockPreview();

  // Opened from a notification (/?block=ID): jump to that check-in.
  const params = new URLSearchParams(location.search);
  const blockId = params.get("block");
  if (blockId) {
    history.replaceState(null, "", "/");
    focusBlock(blockId);
  }

  setInterval(renderClock, 1000);
  setInterval(refresh, POLL_MS);
  document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && refresh());
}

boot();

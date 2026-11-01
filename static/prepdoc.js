// Prep docs, built to the design handoff of Oct 31 (prepdoc.css has the look, theme.js the colors):
// the assignment list, one card as a page in four templates (Document, Checklist, Dashboard,
// Cornell), Focus mode, and the class color picker. Loaded before app.js and only defines things;
// the helpers it calls ($, el, api, post, store, now, fmt*, cardAlert, ...) live in app.js.
//
// Everything shown comes from the card the server built. Where the design wants something cards
// don't have yet (hint chips, worked examples, a list of problems), the section is left out, and a
// TODO says what was assumed.

const PD_TEMPLATES = ["document", "checklist", "dashboard", "cornell"];
const PD_TEMPLATE_NAMES = { document: "Document", checklist: "Checklist", dashboard: "Dashboard", cornell: "Cornell" };
// TODO(design): the spec names defaults for readings, problem sets, tests and essays only; labs and
// "other" get Checklist since their cards are a list of steps.
const PD_DEFAULT_TEMPLATE = { reading: "document", problem_set: "checklist", test: "dashboard", essay: "dashboard", lab: "checklist", other: "checklist" };
const PD_KIND_NAMES = { reading: "Reading", problem_set: "Problem set", test: "Test", essay: "Essay", lab: "Lab", other: "Assignment" };
const PD_EFFORT_MINUTES = { light: 30, medium: 60, heavy: 90 }; // scheduler.DURATION_MINUTES
const PD_QUICK_MINUTES = 10; // scheduler.QUICK_MINUTES

let pdOpen = null; // the assignment whose prep doc is showing, or null for the list
let pdLastKey = "";
let pdListView = "todo"; // the list shows "todo" (not done yet) or "done"
const pdMem = new Map(); // per assignment, this visit only: practice question, typed answer, regenerating

// ---- small pieces ------------------------------------------------------------------------------

const PD_ICONS = {
  "arrow-left": '<path d="M19 12H5"/><path d="m11 6-6 6 6 6"/>',
  "arrow-right": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
  "chevron-right": '<path d="m9 6 6 6-6 6"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/>',
  "triangle-alert": '<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18"/><path d="M8 3v4"/><path d="M16 3v4"/>',
  upload: '<path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/>',
  x: '<path d="M6 6l12 12"/><path d="M18 6 6 18"/>',
  "bell-off": '<path d="M8.7 3A6 6 0 0 1 18 8c0 2.7.6 4.5 1.3 5.6"/><path d="M17 17H3s3-2 3-9a4.7 4.7 0 0 1 .3-1.7"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/><path d="m2 2 20 20"/>',
  lightbulb: '<path d="M9 18h6"/><path d="M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/>',
  "panel-left": '<rect x="5" y="4" width="14" height="16" rx="2"/><path d="M9 4v16"/>',
};

function pdIcon(name, size = 16, width = 2) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const attrs = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": width, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };
  for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
  s.innerHTML = PD_ICONS[name];
  return s;
}

// A button or link with an optional icon before the label (after it for forward arrows).
function pdBtn(text, cls, onClick, iconName = null) {
  const b = el("button", `pd-btn ${cls}`);
  b.type = "button";
  if (iconName && iconName !== "arrow-right") b.append(pdIcon(iconName, iconName === "arrow-left" ? 18 : 16));
  b.append(document.createTextNode(text));
  if (iconName === "arrow-right") b.append(pdIcon("arrow-right", 16, 2.2));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

function pdLink(text, cls, href, iconName = null) {
  const a = el("a", `pd-btn ${cls}`);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener";
  if (iconName && iconName !== "arrow-right") a.append(pdIcon(iconName));
  a.append(document.createTextNode(text));
  if (iconName === "arrow-right") a.append(pdIcon("arrow-right", 16, 2.2));
  return a;
}

const pdOver = (text, tag = "h2", cls = "pd-over") => el(tag, cls, text);
const pdTwo = (i) => String(i + 1).padStart(2, "0");
const pdCap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const pdIsWeb = (url) => /^https?:\/\//i.test(url || "");

// A hairline section: a top line, then an overline header (h2) and its content.
function pdSection(title, cls = "", meta = null) {
  const sec = el("section", `pd-sec ${cls}`.trim());
  if (meta) {
    const head = el("div", "pd-sec-head");
    head.append(pdOver(title), meta);
    sec.append(head);
  } else sec.append(pdOver(title));
  return sec;
}

function pdSkeleton(widths) {
  const wrap = el("div");
  wrap.setAttribute("aria-hidden", "true");
  widths.forEach((w) => {
    const s = el("span", "pd-skel");
    s.style.width = w;
    wrap.append(s);
  });
  return wrap;
}

// ---- light / dark and the class color ----------------------------------------------------------

const pdDarkQuery = window.matchMedia("(prefers-color-scheme: dark)");

// "light" or "dark": the student's override, else the system's.
function pdMode() {
  const pick = store.get("pdMode", "system");
  if (pick === "light" || pick === "dark") return pick;
  return pdDarkQuery.matches ? "dark" : "light";
}

// The whole page follows the override too (style.css reads data-theme), so the app around a prep
// doc never shows the other mode.
function pdApplyMode() {
  const pick = store.get("pdMode", "system");
  if (pick === "light" || pick === "dark") document.documentElement.dataset.theme = pick;
  else delete document.documentElement.dataset.theme;
}

function pdSetMode(pick) {
  store.set("pdMode", pick);
  pdApplyMode();
  pdRender(true);
  pdRenderFocus();
  pdRepaintShell();
}

pdDarkQuery.addEventListener("change", () => {
  pdRender(true);
  pdRenderFocus();
  pdRepaintShell();
});

// The check-in box and the week's blocks carry class colors too (app.js); redraw them when a color
// or the mode changes.
function pdRepaintShell() {
  if (typeof state === "undefined" || !state) return;
  renderCheckin();
  renderWeek();
}

function pdPaint(node, hex) {
  for (const [k, v] of Object.entries(themeToCssVars(deriveTheme(hex || DEFAULT_SUBJECT, pdMode())))) node.style.setProperty(k, v);
}

const pdGraphic = (hex) => deriveTheme(hex || DEFAULT_SUBJECT, pdMode()).graphic;

// 8 round swatches; the pick re-themes everything for that class at once.
function pdSwatches(course) {
  const group = el("div", "pd-swatches");
  group.setAttribute("role", "radiogroup");
  group.setAttribute("aria-label", `Color for ${course.name}`);
  for (const c of SUBJECT_COLORS) {
    const b = el("button", "pd-swatch");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String((course.color || "").toUpperCase() === c.hex));
    b.setAttribute("aria-label", c.name);
    b.title = c.name;
    const dot = el("span");
    dot.style.background = c.muted;
    b.append(dot);
    b.addEventListener("click", () => pdSetColor(course.id, c.hex));
    group.append(b);
  }
  return group;
}

async function pdSetColor(courseId, hex) {
  try {
    const r = await api(`/api/courses/${courseId}/color`, { method: "PUT", body: JSON.stringify({ color: hex }) });
    cards.forEach((c) => c.course_id === courseId && (c.color = r.color));
    courses.forEach((c) => c.id === courseId && (c.color = r.color));
    pdRender(true);
    pdRenderFocus();
    renderNotesSettings();
    pdRepaintShell();
  } catch (e) {
    toast(e.message);
  }
}

// ---- what a card holds, shaped for the templates -----------------------------------------------

// The ordered steps: the first step, then the rest, each with minutes for its timer. The first step
// is the two-minute start; a reading's sittings (prep.py puts them right after) bring their own
// minutes; the rest share what's left of the effort's session length.
function pdSteps(c) {
  const texts = [c.first_step, ...(c.steps || [])];
  const sittings = c.kind === "reading" && c.reading ? c.reading.sittings : [];
  const mins = texts.map((t, i) => {
    if (i === 0) return 2;
    const s = sittings[i - 1];
    return s && /^read\b/i.test(t) ? s.minutes : null;
  });
  const fixed = mins.reduce((a, m) => a + (m || 0), 0);
  const open = mins.filter((m) => m === null).length;
  const each = open ? Math.max(5, Math.round(((PD_EFFORT_MINUTES[c.effort] || 60) - fixed) / open / 5) * 5) : 0;
  return texts.map((text, i) => ({
    text,
    minutes: mins[i] ?? each,
    sitting: mins[i] !== null && i > 0 ? sittings[i - 1] : null,
    lists: (c.step_lists || []).filter((l) => l.step === i - 1),
  }));
}

function pdChecked(c) {
  let v = store.get(`pd:steps:${c.assignment_id}`, null);
  if (v === null) v = store.get(`steps-${c.assignment_id}`, []).map((i) => i + 1); // the old card's ticks (no first step)
  return new Set(v);
}

function pdSetChecked(c, set) {
  store.set(`pd:steps:${c.assignment_id}`, [...set].sort((a, b) => a - b));
}

const pdCurrent = (steps, checked) => steps.findIndex((_, i) => !checked.has(i));

function pdTemplate(c) {
  const t = store.get(`pd:tpl:${c.assignment_id}`, null);
  return PD_TEMPLATES.includes(t) ? t : PD_DEFAULT_TEMPLATE[c.kind] || "checklist";
}

function pdState(id) {
  if (!pdMem.has(id)) pdMem.set(id, { q: null, answer: "", checked: false, hint: false, generating: false, failed: "" });
  return pdMem.get(id);
}

// Files: what the card read or tried to read, then any other link from Canvas. A Google file the
// server couldn't open links to "Open this doc", so the Chrome extension can save it.
function pdFiles(c) {
  const out = [];
  const seen = new Set();
  for (const s of c.sources || []) {
    const url = pdIsWeb(s.url) ? s.url : "";
    if (url) seen.add(url);
    const google = !s.read && /^https:\/\/(docs|drive)\.google\.com\//i.test(url);
    out.push({
      name: s.name,
      url: google ? "" : url,
      openDoc: google ? url : "",
      extra: s.read ? (s.pages ? `${s.pages} page${s.pages === 1 ? "" : "s"}` : "") : `couldn't open${s.reason ? `: ${s.reason}` : ""}`,
      read: s.read,
    });
  }
  for (const l of c.links || []) {
    if (!pdIsWeb(l.url) || seen.has(l.url)) continue;
    seen.add(l.url);
    out.push({ name: l.label, url: l.url, openDoc: "", extra: "", read: true });
  }
  return out;
}

// What "Open ..." in Start here opens: the first file it read, else the assignment on Canvas.
function pdSource(c) {
  const f = pdFiles(c).find((x) => x.url && x.read);
  if (f) return { label: `Open ${f.name.length > 36 ? f.name.slice(0, 34) + "…" : f.name}`, url: f.url };
  return pdIsWeb(c.html_url) ? { label: "Open in Canvas", url: c.html_url } : null;
}

function pdFileRow(f) {
  const row = f.url ? el("a", "pd-file") : el("div", "pd-file");
  if (f.url) {
    row.href = f.url;
    row.target = "_blank";
    row.rel = "noopener";
  }
  row.append(pdIcon("file", 18, 1.8), el("span", f.url ? "name" : null, f.name));
  if (f.extra) row.append(el("span", "extra", f.extra));
  if (!f.openDoc) return row;
  const wrap = el("div", "pd-file-row");
  wrap.append(row, pdLink("Open this doc", "soft", f.openDoc));
  return wrap;
}

function pdFilesSection(c) {
  const sec = pdSection("Files", "tight");
  const files = pdFiles(c);
  if (!files.length) sec.append(el("p", "pd-muted", "No files attached"));
  files.forEach((f) => sec.append(pdFileRow(f)));
  return sec;
}

// The red box. Max 3 items, and none at all when there's no feedback.
function pdWatchOut(c, numbered = false, cls = "") {
  // TODO(design): cards hold one watch-out line and not where it came from, so there is one item
  // and no source on the right.
  const items = (c.watch_out ? [c.watch_out] : []).slice(0, 3);
  if (!items.length) return null;
  const box = el("section", `pd-warn ${cls}`.trim());
  const head = el("div", "pd-warn-head");
  const h = el("h2");
  h.append(pdIcon("triangle-alert", 16, 2.2), document.createTextNode("Watch out"));
  head.append(h);
  const list = el(numbered ? "ol" : "ul");
  items.forEach((t, i) => {
    const li = el("li");
    li.append(numbered ? el("span", "n", pdTwo(i)) : el("span", "mark"), el("span", null, t));
    li.firstChild.setAttribute("aria-hidden", "true");
    list.append(li);
  });
  box.append(head, list);
  return box;
}

const pdDayLabel = (d) => (startOfDay(d).getTime() === startOfDay(now()).getTime() ? "Today" : d.toLocaleDateString([], { weekday: "short" }));
const pdDueLine = (c) => (c.due_at ? `Due ${fmtDay(new Date(c.due_at))} at ${fmtTime(new Date(c.due_at))}` : "No due date");

// ---- the page: the list, or one prep doc --------------------------------------------------------

function openDoc(id) {
  showTab("cards");
  if (location.hash !== `#/prep/${id}`) location.hash = `/prep/${id}`;
  else pdRoute();
  window.scrollTo(0, 0);
}

function closeDoc() {
  history.pushState(null, "", location.pathname + location.search);
  pdRoute();
}

function pdRoute(render = true) {
  const m = location.hash.match(/^#\/prep\/(\d+)$/);
  const was = pdOpen;
  pdOpen = m ? Number(m[1]) : null;
  if (!render) return;
  pdRender(true);
  if (pdOpen !== was) window.scrollTo(0, 0);
}
window.addEventListener("hashchange", pdRoute);
window.addEventListener("popstate", pdRoute);

// Draws the Cards tab. Skips the redraw when nothing it shows has changed, so a poll never wipes
// what the student is typing; `force` redraws anyway (their own clicks).
function pdRender(force = false) {
  const root = $("#cards");
  if (!root) return;
  const card = pdOpen !== null ? cards.find((c) => c.assignment_id === pdOpen) : null;
  const key = JSON.stringify([pdOpen, pdMode(), card || cards, card ? pdTemplate(card) : pdListView]);
  if (!force && key === pdLastKey) return;
  pdLastKey = key;
  const asking = notesAskIds();
  root.replaceChildren(card ? pdDoc(card, asking.has(card.assignment_id)) : pdList());
}

// To do / Done: plain words, one shown at a time.
function pdListSwitch(todo, done) {
  const group = el("div", "pd-switch");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Show");
  for (const [view, text] of [["todo", `To do · ${todo}`], ["done", `Done · ${done}`]]) {
    const b = button(text, null, () => {
      pdListView = view;
      pdRender(true);
    });
    b.setAttribute("aria-pressed", String(pdListView === view));
    group.append(b);
  }
  return group;
}

function pdList() {
  const root = el("div", "pd");
  pdPaint(root, DEFAULT_SUBJECT);
  const wrap = el("div", "pd-wrap");
  const head = el("header", "pd-list-head");
  const todo = cards.filter((c) => !c.done);
  const done = cards.filter((c) => c.done);
  const title = el("div", "pd-title");
  title.append(el("h1", "pd-h1", "Assignments"));
  if (cards.length) title.append(el("p", "pd-sub pd-text2", pdListView === "todo" ? `${todo.length} to do` : `${done.length} done`));
  head.append(title);
  if (cards.length) head.append(pdListSwitch(todo.length, done.length));
  wrap.append(head);
  const shown = pdListView === "todo" ? todo : done;
  if (cards.length && !shown.length) {
    wrap.append(el("p", "pd-sub pd-text2", pdListView === "todo" ? "Nothing left to do. Nice." : "Nothing marked done yet."));
  }
  if (!cards.length) {
    wrap.append(
      el(
        "p",
        "pd-sub pd-text2",
        state?.config.canvas
          ? "No open work due in the next two weeks."
          : "No cards yet. Put your Canvas token in .env and press Sync, or run `python -m app.demo` for sample data.",
      ),
    );
    root.append(wrap);
    return root;
  }
  const ul = el("ul", "pd-list");
  for (const c of shown) {
    const li = el("li", c.done ? "done" : "");
    const a = el("a");
    a.href = `#/prep/${c.assignment_id}`;
    a.id = `card-${c.assignment_id}`;
    const info = el("div", "info");
    const course = el("p", "course");
    const dot = el("span", "pd-list-dot");
    dot.style.background = pdGraphic(c.color);
    dot.setAttribute("aria-hidden", "true");
    course.append(dot, document.createTextNode(c.course || "No class"));
    const steps = pdSteps(c);
    const cur = pdCurrent(steps, pdChecked(c));
    info.append(course, el("p", "name", c.name || `Assignment ${c.assignment_id}`));
    const first = c.quick ? `Quick task: ${c.first_step}` : cur < 0 ? "All steps done. Turn it in." : `${cur === 0 ? "Start here" : `Step ${cur + 1}`}: ${steps[cur].text}`;
    if (!c.done) info.append(el("p", "first", first));
    if (!c.done && c.past_due) info.append(el("p", "flag", `Past due since ${fmtDayTime(new Date(c.due_at))}`));
    else if (!c.done && c.missed) info.append(el("p", "flag", `Missed the start planned for ${fmtDayTime(new Date(c.missed.start))}`));
    else if (!c.done && c.watch_out) {
      const w = el("p", "watch");
      w.append(pdIcon("triangle-alert", 15, 2.2), el("span", null, `Watch out: ${c.watch_out}`));
      info.append(w);
    }
    // The type tag takes the class's tint, like its prep doc.
    const meta = el("span", "meta");
    const tag = el("span", "pd-list-type", c.quick ? "Quick task" : PD_KIND_NAMES[c.kind] || "Assignment");
    pdPaint(tag, c.color);
    meta.append(tag, el("span", "when", c.done ? "Done" : dueText(c.due_at)), pdIcon("chevron-right", 18, 2));
    a.append(info, meta);
    li.append(a);
    ul.append(li);
  }
  if (shown.length) wrap.append(ul);
  // Class colors, for the classes on this list.
  const seen = new Map();
  cards.forEach((c) => c.course_id && !seen.has(c.course_id) && seen.set(c.course_id, { id: c.course_id, name: c.course, color: c.color }));
  if (seen.size) {
    const det = el("details", "pd-colors");
    det.append(el("summary", null, "Class colors"));
    for (const course of seen.values()) {
      const row = el("div", "pd-color-row");
      row.append(el("span", null, course.name), pdSwatches(course));
      det.append(row);
    }
    wrap.append(det);
  }
  root.append(wrap);
  return root;
}

function pdDoc(c, askNotes) {
  const tpl = pdTemplate(c);
  const st = pdState(c.assignment_id);
  const root = el("div", "pd");
  pdPaint(root, c.color);
  const wrap = el("div", "pd-wrap");
  wrap.append(pdHeader(c, tpl));
  const alert = cardAlert(c); // a missed start or a passed due date (the start loop)
  // TODO(design): not in the spec; shown as a grey tile above the template, the box kind the
  // design keeps for things that aren't the hero or a watch-out.
  if (alert) wrap.append(alert);
  if (st.failed) wrap.append(pdFailed(c, st.failed));
  wrap.append(st.generating ? pdGenerating(c) : c.quick ? pdQuick(c) : PD_RENDER[tpl](c));
  if (askNotes) wrap.append(notesAsk(c));
  wrap.append(pdFoot(c));
  root.append(wrap);
  return root;
}

function pdHeader(c, tpl) {
  const head = el("header", "pd-head");
  const controls = el("div", "pd-controls");
  const back = el("a", "pd-btn neutral ui back");
  back.href = location.pathname + location.search;
  back.append(pdIcon("arrow-left", 18), document.createTextNode("Assignments"));
  back.addEventListener("click", (e) => {
    e.preventDefault();
    closeDoc();
  });
  const right = el("div", "pd-controls-right");
  if (!c.quick) right.append(pdBtn("Start focus", "soft ui", () => startFocus(c), "target"), pdSwitcher(c, tpl));
  controls.append(back, right);
  const title = el("div", "pd-title");
  const kicker = el("p", "pd-kicker");
  const dot = el("span", "pd-dot");
  dot.setAttribute("aria-hidden", "true");
  const due = c.due_at ? `Due ${fmtDay(new Date(c.due_at))}` : "No due date";
  kicker.append(dot, document.createTextNode([c.course, PD_KIND_NAMES[c.kind], due].filter(Boolean).join(" · ")));
  title.append(kicker, el("h1", "pd-h1", c.name || `Assignment ${c.assignment_id}`));
  head.append(controls, title);
  return head;
}

// Document / Checklist / Dashboard / Cornell. The same card in another layout: nothing regenerates.
function pdSwitcher(c, tpl) {
  const group = el("div", "pd-switch");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Prep doc template");
  const pick = (t) => {
    store.set(`pd:tpl:${c.assignment_id}`, t);
    pdRender(true);
    $(`.pd-switch button[data-tpl="${t}"]`)?.focus();
  };
  PD_TEMPLATES.forEach((t, i) => {
    const b = el("button", null, PD_TEMPLATE_NAMES[t]);
    b.type = "button";
    b.dataset.tpl = t;
    b.setAttribute("aria-pressed", String(t === tpl));
    b.addEventListener("click", () => pick(t));
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      pick(PD_TEMPLATES[(i + (e.key === "ArrowRight" ? 1 : -1) + PD_TEMPLATES.length) % PD_TEMPLATES.length]);
    });
    group.append(b);
  });
  return group;
}

// Card details that aren't part of any template: effort, a template card's reason, and the
// buttons the old card had.
function pdFoot(c) {
  const foot = el("footer", "pd-foot");
  const notes = [c.quick ? "Quick task" : `${pdCap(c.effort)} effort`];
  if (c.generated_by === "demo") notes.push("Demo card.");
  if (c.generated_by === "fallback") notes.push(c.model_error ? `Template card: the model call failed (${c.model_error}).` : "Template card.");
  if (c.stale) notes.push("This card will be rebuilt on the next sync.");
  foot.append(el("p", null, notes.join(" · ")));
  const actions = el("div", "pd-actions");
  const regen = pdBtn("Regenerate", "neutral", () => pdRegenerate(c));
  regen.disabled = pdState(c.assignment_id).generating;
  actions.append(
    regen,
    pdBtn(c.done ? "Undo done" : "Mark done", "neutral", async () => {
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
  foot.append(actions);
  return foot;
}

async function pdRegenerate(c) {
  const st = pdState(c.assignment_id);
  st.generating = true;
  st.failed = "";
  pdRender(true);
  try {
    await post(`/api/cards/${c.assignment_id}/regenerate`);
    st.generating = false;
    await loadCards();
    toast("Card updated");
  } catch (e) {
    st.generating = false;
    st.failed = e.message;
  }
  pdRender(true);
}

// Generation failed: a watch-out-styled box with Try again; the rest of the old card stays.
function pdFailed(c, message) {
  const box = el("section", "pd-warn");
  box.setAttribute("role", "alert");
  const head = el("div", "pd-warn-head");
  const h = el("h2");
  h.append(pdIcon("triangle-alert", 16, 2.2), document.createTextNode("Couldn't prep this one."));
  head.append(h);
  const actions = el("div", "pd-actions");
  actions.append(pdBtn("Try again", "soft", () => pdRegenerate(c)));
  box.append(head, el("p", null, message), actions);
  return box;
}

// Generating: grey blocks where the text will go, hero first.
// TODO(design): one skeleton (Document's shape) stands in for all four templates.
function pdGenerating(c) {
  const row = el("div", "pd-row");
  const main = el("div", "pd-main");
  const hero = el("section", "pd-hero");
  hero.setAttribute("aria-busy", "true");
  hero.append(el("h2", "pd-over strong", "Start here"), pdSkeleton(["80%", "55%"]), pdSkeleton(["60%"]));
  const prompt = pdSection("The prompt, in plain words");
  prompt.append(pdSkeleton(["95%", "90%", "40%"]));
  main.append(hero, prompt);
  const side = el("aside", "pd-side");
  const plan = pdSection("Plan");
  plan.append(pdSkeleton(["90%", "70%", "85%"]));
  side.append(plan, pdFilesSection(c));
  row.append(main, side);
  const live = el("p", "sr-only", "Building this prep doc…");
  live.setAttribute("role", "status");
  row.append(live);
  return row;
}

// ---- Quick task (a form, a signature): the one action, no template -------------------------------

function pdQuick(c) {
  const main = el("div", "pd-main pd-quick");
  const hero = el("section", "pd-hero");
  if (c.done) {
    hero.append(el("h2", "pd-over strong", "Done"), el("p", "pd-hero-text", "This one is marked done."));
  } else {
    hero.append(el("h2", "pd-over strong", `Quick task · about ${PD_QUICK_MINUTES} min`), el("p", "pd-hero-text", c.first_step));
    if (c.next_block) hero.append(el("p", "pd-sub", `Planned for ${fmtDayTime(new Date(c.next_block))}.`));
    const actions = el("div", "pd-actions");
    const src = pdSource(c);
    if (src) {
      const open = pdLink(src.label, "hero", src.url, "arrow-right");
      open.addEventListener("click", () => pdStarted(c)); // opening the form is starting
      actions.append(open);
    }
    actions.append(pdBtn("Mark it done", "hero-outline", () => pdMarkDone(c)));
    hero.append(actions);
  }
  main.append(hero);
  const warn = pdWatchOut(c);
  if (warn) main.append(warn);
  if (pdFiles(c).length > 1) main.append(pdFilesSection(c));
  return main;
}

// ---- Document (readings) ------------------------------------------------------------------------

function pdStartHere(c, steps, checked) {
  const cur = pdCurrent(steps, checked);
  const hero = el("section", "pd-hero");
  const actions = el("div", "pd-actions");
  if (cur < 0) {
    hero.append(el("h2", "pd-over strong", "All steps done"), el("p", "pd-hero-text", c.done ? "This one is marked done." : "Turn it in, then mark it done."));
    if (pdIsWeb(c.html_url)) actions.append(pdLink("Open in Canvas", "hero", c.html_url, "arrow-right"));
    if (!c.done) actions.append(pdBtn("Mark it done", "hero-outline", () => pdMarkDone(c)));
    hero.append(actions);
    return hero;
  }
  // TODO(design): the label stays "Start here" for the first step only; later ones say which step.
  const what = cur === 0 ? "Start here" : `Step ${cur + 1} of ${steps.length}`;
  hero.append(el("h2", "pd-over strong", `${what} · about ${steps[cur].minutes} min`), el("p", "pd-hero-text", steps[cur].text));
  // The subline: why the steps are general, how long the reading is, or when it's planned.
  let sub = "";
  if (cur === 0 && !c.instructions_seen) sub = "Start Helper couldn't read the instructions for this one, so the steps are general. Find the handout first.";
  else if (cur === 0 && c.reading) sub = `${readingText(c.reading)}.`;
  else if (c.next_block) sub = `Planned for ${fmtDayTime(new Date(c.next_block))}.`;
  if (sub) hero.append(el("p", "pd-sub", sub));
  const src = pdSource(c);
  if (src) actions.append(pdLink(src.label, "hero", src.url, "arrow-right"));
  actions.append(
    pdBtn("Mark step done", "hero-outline", () => {
      checked.add(cur);
      pdSetChecked(c, checked);
      pdRender(true);
    }),
    pdBtn("Start focus", "hero-outline", () => startFocus(c), "target"),
  );
  hero.append(actions);
  return hero;
}

async function pdMarkDone(c) {
  try {
    await post(`/api/assignments/${c.assignment_id}/done`);
    renderedKeys.week = "";
    await refresh(true);
  } catch (e) {
    toast(e.message);
  }
}

// Starting (entering Focus, or opening a quick task's form) is recorded like "Start now", once.
async function pdStarted(c) {
  if (c.started || c.done) return;
  try {
    await post(`/api/assignments/${c.assignment_id}/start`);
    c.started = true;
    renderedKeys.week = "";
    refresh(true);
  } catch (e) {
    toast(e.message);
  }
}

function pdDocument(c) {
  const steps = pdSteps(c);
  const checked = pdChecked(c);
  const row = el("div", "pd-row");
  const main = el("div", "pd-main");
  main.append(pdStartHere(c, steps, checked));
  const narrowWarn = pdWatchOut(c, false, "pd-narrow-only");
  if (narrowWarn) main.append(narrowWarn);
  if (c.summary) {
    const sec = pdSection("The prompt, in plain words");
    sec.append(el("p", "pd-lead", c.summary));
    main.append(sec);
  }
  // The teacher's own lists (reading or discussion questions), word for word.
  // TODO(design): no hint chips: cards don't say where in the reading each answer is.
  (c.step_lists || []).forEach((list) => {
    const title = c.kind === "reading" ? "Reading questions" : pdCap(list.label);
    const sec = pdSection(title, "tight", c.kind === "reading" ? el("span", "pd-meta", pdCap(list.label.replace(/^the /, ""))) : null);
    const ol = el("ol", "pd-qs");
    list.items.forEach((t, i) => {
      const li = el("li");
      const n = el("span", "n", pdTwo(i));
      n.setAttribute("aria-hidden", "true");
      const body = el("div", "body");
      body.append(el("p", null, t));
      li.append(n, body);
      ol.append(li);
    });
    sec.append(ol);
    main.append(sec);
  });
  const side = el("aside", "pd-side");
  const warn = pdWatchOut(c, false, "pd-wide-only");
  if (warn) side.append(warn);
  side.append(pdPlan(c, steps, checked), pdFilesSection(c));
  row.append(main, side);
  return row;
}

// The plan: every step, with the planned session's day and time on the step it starts at.
// TODO(design): the scheduler plans one session per assignment, not one per step, so only the
// current step has a time; the rest follow it.
function pdPlan(c, steps, checked) {
  const sec = pdSection("Plan", "tight");
  const cur = pdCurrent(steps, checked);
  const session = (c.blocks || [])[0];
  if (!session) sec.append(el("p", "pd-muted", "Not scheduled yet"));
  steps.forEach((s, i) => {
    const row = el("div", `pd-plan-row${checked.has(i) ? " done" : ""}${i === cur ? "" : " later"}`);
    let day = el("span", "pd-day");
    let time = el("span", "pd-small");
    if (session && i === cur) {
      const d = new Date(session.start);
      const label = session.status === "started" ? "Now" : pdDayLabel(d);
      day = el("span", `pd-day${label === "Today" || label === "Now" ? " today" : ""}`, label);
      time = el("span", "pd-small", fmtTime(d));
    }
    row.append(day, el("span", "what", s.text), time, el("span", "pd-small", `${s.minutes} min`));
    sec.append(row);
  });
  const due = el("p", "pd-due");
  due.append(pdIcon("calendar", 16, 1.8), document.createTextNode(pdDueLine(c)));
  sec.append(due);
  return sec;
}

// ---- Checklist (problem sets) -------------------------------------------------------------------

function pdChecklist(c) {
  const steps = pdSteps(c);
  const checked = pdChecked(c);
  const row = el("div", "pd-row");
  const main = el("div", "pd-main checklist");
  // TODO(design): cards don't split a problem set into problems, so the statement is the whole
  // assignment's ("what you turn in") and the side column has no Problems list.
  if (c.summary) {
    const what = el("div", "pd-title");
    what.append(pdOver("What you turn in"), el("p", "pd-problem", c.summary));
    main.append(what);
  }
  const sec = pdSection("Steps", "tight");
  const ol = el("ol", "pd-steps");
  const side = el("aside", "pd-side");
  const progress = el("section", "pd-hero progress");
  const items = steps.map((s, i) => {
    const li = el("li");
    const line = el("div", "pd-step-line");
    const box = el("input");
    box.type = "checkbox";
    box.id = `pd-step-${c.assignment_id}-${i}`;
    box.checked = checked.has(i);
    box.addEventListener("change", () => {
      box.checked ? checked.add(i) : checked.delete(i);
      pdSetChecked(c, checked);
      update();
    });
    const n = el("span", "n", pdTwo(i));
    n.setAttribute("aria-hidden", "true");
    const lab = el("label", null, s.text);
    lab.htmlFor = box.id;
    const tag = el("span", "pd-tag", "Up next");
    line.append(box, n, lab, tag);
    li.append(line);
    const extra = el("div", "pd-step-extra");
    s.lists.forEach((list) => {
      const more = el("details", "pd-step-list");
      more.append(el("summary", null, `Show ${list.label}`));
      const items = el("ol");
      list.items.forEach((t) => items.append(el("li", null, t)));
      more.append(items);
      extra.append(more);
    });
    const pill = c.watch_out ? el("p", "pd-pill") : null;
    if (pill) {
      pill.append(pdIcon("triangle-alert", 16, 2.2), document.createTextNode(c.watch_out));
      extra.append(pill);
    }
    li.append(extra);
    ol.append(li);
    return { li, tag, extra, pill, hasLists: s.lists.length > 0 };
  });
  // Ticking a box moves "current" in place (no redraw), so the row tint slides to the next step.
  const update = () => {
    const cur = pdCurrent(steps, checked);
    items.forEach((it, i) => {
      it.li.className = [checked.has(i) ? "done" : "", i === cur ? "current" : "", i === cur - 1 ? "before-current" : ""].filter(Boolean).join(" ");
      it.tag.hidden = i !== cur;
      if (it.pill) it.pill.hidden = i !== cur;
      it.extra.hidden = !it.hasLists && !(it.pill && i === cur);
    });
    pdProgress(progress, c, steps, checked);
  };
  update();
  sec.append(ol);
  main.append(sec);
  side.append(progress, pdFilesSection(c));
  row.append(main, side);
  return row;
}

function pdProgress(box, c, steps, checked) {
  const done = steps.filter((_, i) => checked.has(i)).length;
  const left = steps.reduce((a, s, i) => a + (checked.has(i) ? 0 : s.minutes), 0);
  const stat = el("p", "pd-stat");
  stat.append(el("span", "big", `${done}/${steps.length}`), el("span", "unit", "steps done"));
  const segs = el("div", "pd-segs");
  segs.setAttribute("aria-hidden", "true");
  segs.style.gridTemplateColumns = `repeat(${steps.length}, minmax(0, 1fr))`;
  steps.forEach((_, i) => segs.append(el("div", i < done ? "on" : "")));
  const start = pdBtn("Start focus", "hero", () => startFocus(c), "target");
  start.style.alignSelf = "flex-start";
  box.replaceChildren(
    el("h2", "pd-over strong", "Progress"),
    stat,
    segs,
    el("p", null, left ? `About ${left} minutes left` : "Every step is done."),
    start,
  );
}

// ---- Dashboard (tests and essays) ---------------------------------------------------------------

function pdDashboard(c) {
  const wrap = el("div", "pd-dash");
  const isTest = c.kind === "test";

  // Row 1: days left, and today's practice question (or, with no practice set, the step to do).
  const row1 = el("div", "pd-row stretch");
  const hero = el("section", "pd-hero countdown");
  hero.append(el("h2", "pd-over strong", isTest ? "Test in" : "Due in"));
  const count = el("p", "pd-count");
  if (c.due_at) {
    const days = Math.round((startOfDay(new Date(c.due_at)) - startOfDay(now())) / 86_400_000);
    if (days > 0) count.append(el("span", "big", String(days)), el("span", "unit", days === 1 ? "day" : "days"));
    else count.append(el("span", "big word", days === 0 ? "Today" : "Past due"));
  } else count.append(el("span", "big word", "No date"));
  hero.append(count, el("p", null, pdSessionsLine(c)));
  hero.append(pdBtn("See the plan", "hero", () => showTab("week"), "arrow-right"));
  row1.append(hero, c.practice?.length ? pdPractice(c) : pdDashFirst(c));
  wrap.append(row1);

  // Row 2: what the student misses, from graded tests, and the watch-out.
  const row2 = el("div", "pd-row");
  const m = c.misses || { topics: [], qtypes: [] };
  if (m.topics.length || m.qtypes.length) {
    row2.append(pdBars("Topics you miss", m.topics), pdBars("Question types you miss", m.qtypes));
  } else {
    const sec = pdSection("Topics you miss", "tight pd-third");
    sec.append(el("p", "pd-muted", "No graded tests for this class yet. Upload one to see where the marks go."));
    row2.append(sec);
  }
  const warn = pdWatchOut(c, true, "pd-third");
  if (warn) row2.append(warn);
  wrap.append(row2);

  // Row 3: files, and the graded work the practice was built from.
  const row3 = el("div", "pd-row");
  const files = pdFilesSection(c);
  files.classList.add("pd-third");
  const built = pdSection("Built from", "pd-two-thirds");
  const list = el("div", "pd-built");
  // TODO(design): graded tests have no page to open here, so they're names, not links.
  (c.built_from || []).forEach((t) => {
    const item = el("span", "pd-file");
    item.append(pdIcon("file", 16, 1.8), el("span", null, t));
    list.append(item);
  });
  if (!(c.built_from || []).length) list.append(el("span", "pd-muted", "No graded work yet."));
  list.append(pdBtn("Upload graded work", "soft", () => pdGoUpload(c.course_id), "upload"));
  built.append(list);
  row3.append(files, built);
  wrap.append(row3);
  return wrap;
}

// "Three 20-minute practice sessions are on your calendar before Thursday."
function pdSessionsLine(c) {
  const blocks = c.blocks || [];
  const what = c.kind === "test" ? "practice session" : "work session";
  if (!blocks.length) return `No ${what} is on your calendar yet.`;
  const words = ["No", "One", "Two", "Three", "Four", "Five", "Six"];
  const mins = Math.round((new Date(blocks[0].end) - new Date(blocks[0].start)) / 60_000);
  const n = blocks.length;
  const due = c.due_at ? new Date(c.due_at) : null;
  const before = !due ? "" : startOfDay(due).getTime() === startOfDay(now()).getTime() ? " before it's due" : ` before ${due.toLocaleDateString([], { weekday: "long" })}`;
  return `${words[n] || n} ${mins}-minute ${what}${n > 1 ? "s are" : " is"} on your calendar${before}.`;
}

// TODO(design): an essay (or any card without practice questions) has nothing to practice, so the
// right of row 1 holds the step to do now instead.
function pdDashFirst(c) {
  const steps = pdSteps(c);
  const checked = pdChecked(c);
  const cur = pdCurrent(steps, checked);
  const sec = pdSection(c.kind === "test" ? "Today's practice question" : cur <= 0 ? "Start here" : `Step ${cur + 1} of ${steps.length}`, "pd-practice");
  if (c.kind === "test") {
    sec.append(el("p", "pd-lead pd-text2", c.focus || "No practice questions yet. Set META_API_KEY to get questions from this unit's documents, and add graded tests on the Dashboard to aim them at your weak spots."));
    return sec;
  }
  if (cur < 0) {
    sec.append(el("p", "pd-question", "Every step is done. Turn it in, then mark it done."));
    return sec;
  }
  sec.append(el("p", "pd-question", steps[cur].text));
  const actions = el("div", "pd-actions");
  actions.append(
    pdBtn("Mark step done", "soft", () => {
      checked.add(cur);
      pdSetChecked(c, checked);
      pdRender(true);
    }),
  );
  sec.append(actions);
  return sec;
}

const pdNorm = (s) => (s || "").toLowerCase().normalize("NFC").replace(/[^\p{L}\p{N}]+/gu, " ").trim();

// Whether a typed answer matches: for multiple choice the same choice (by letter, "B)", or its
// words), else the same words. Free answers that don't match are shown next to the real one, not
// marked wrong.
function pdMatches(q, given) {
  if (!pdNorm(given)) return false;
  if (q.choices.length) {
    const pick = (s) => {
      const m = (s || "").trim().match(/^\(?([a-h])\)?[.):]?(?:\s+|$)/i);
      return m ? "abcdefgh".indexOf(m[1].toLowerCase()) : q.choices.findIndex((ch) => pdNorm(ch) === pdNorm(s));
    };
    const a = pick(q.answer), g = pick(given);
    if (a >= 0 && g >= 0) return a === g;
  }
  return pdNorm(given) === pdNorm(q.answer);
}

function pdPractice(c) {
  const st = pdState(c.assignment_id);
  const qs = c.practice;
  if (st.q === null || st.q >= qs.length) st.q = Math.floor(startOfDay(now()).getTime() / 86_400_000) % qs.length; // "today's"
  const q = qs[st.q];
  const sec = pdSection("Today's practice question", "pd-practice", el("span", "pd-type", label(q.qtype)));
  sec.append(el("p", "pd-question", q.prompt));
  if (q.choices.length) {
    const ol = el("ol", "pd-choices");
    q.choices.forEach((ch) => ol.append(el("li", null, ch)));
    sec.append(ol);
  }
  const tile = el("div", "pd-tile");
  const lab = el("label", "pd-tile-label", "Your answer");
  const input = el("input", "pd-input answer");
  input.id = `pd-answer-${c.assignment_id}`;
  lab.htmlFor = input.id;
  input.placeholder = q.choices.length ? "Type the letter or the answer" : "Type your answer";
  input.value = st.answer;
  input.addEventListener("input", () => (st.answer = input.value));
  input.addEventListener("keydown", (e) => e.key === "Enter" && check());
  const result = el("div", "pd-result");
  result.setAttribute("aria-live", "polite");
  const answerRow = el("div", "pd-answer-row");
  const check = () => {
    st.checked = true;
    showResult();
  };
  const hint = pdBtn("Hint", "fill", () => {
    st.hint = true;
    showResult();
  });
  const showResult = () => {
    result.replaceChildren();
    if (st.hint) {
      // TODO(design): the hint is where the question came from (or its topic); cards hold no
      // separate hint text.
      result.append(el("span", "pd-chip", q.source ? `Look in ${q.source}` : `Topic: ${label(q.topic)}`));
    }
    if (!st.checked) return;
    const right = pdMatches(q, st.answer);
    const line = el("p");
    if (right) line.append(el("strong", null, "Right. "));
    else if (q.choices.length) line.append(el("strong", null, "Not quite. "), document.createTextNode(`The answer is ${q.answer}. `));
    else line.append(el("strong", null, "Check yours against the answer: "), document.createTextNode(`${q.answer}. `));
    if (q.explanation) line.append(document.createTextNode(q.explanation));
    result.append(line, el("p", "pd-muted", q.verified ? "Answer double-checked by a second solve." : "This answer wasn't double-checked."));
    if (qs.length > 1) {
      const next = pdBtn("Next question", "soft", () => {
        Object.assign(st, { q: (st.q + 1) % qs.length, answer: "", checked: false, hint: false });
        pdRender(true);
        $(`#pd-answer-${c.assignment_id}`)?.focus();
      }, "arrow-right");
      next.style.alignSelf = "flex-start";
      result.append(next);
    }
  };
  answerRow.append(input, pdBtn("Check", "soft", check), hint);
  tile.append(lab, answerRow, result);
  sec.append(tile);
  const meta = [`Question ${st.q + 1} of ${qs.length}`, q.source ? `from ${q.source}` : ""].filter(Boolean).join(" · ");
  sec.append(el("p", "pd-muted", meta));
  if (c.focus) sec.append(el("p", "pd-text2", c.focus));
  showResult();
  return sec;
}

// "Topics you miss": missed out of asked, worst first; the two weakest get the accent.
function pdBars(title, rows) {
  const sec = pdSection(title, "tight pd-third pd-bars");
  if (!rows.length) sec.append(el("p", "pd-muted", "Nothing missed yet."));
  rows.forEach((r, i) => {
    const row = el("div", `pd-bar-row${i < 2 && r.wrong > 0 ? " weak" : ""}`);
    const lab = el("div", "label");
    const bar = el("div", "pd-bar");
    bar.setAttribute("aria-hidden", "true");
    const fill = el("div");
    fill.style.width = `${Math.round((r.wrong / r.total) * 100)}%`;
    bar.append(fill);
    lab.append(el("span", null, pdCap(label(r.label))), bar);
    const score = el("span", "score", `${r.wrong}/${r.total}`);
    score.setAttribute("aria-label", `${r.wrong} of ${r.total} missed`);
    row.append(score, lab);
    sec.append(row);
  });
  return sec;
}

// "Upload graded work": the Dashboard tab's upload form, set to this class.
function pdGoUpload(courseId) {
  showTab("dashboard");
  const select = $("#upload-course");
  if (select && courseId) select.value = String(courseId);
  const file = $("#upload-file");
  file?.scrollIntoView({ block: "center" });
  file?.focus();
}

// ---- Cornell (opt-in, for readings) -------------------------------------------------------------

// Cues: the teacher's questions when there are any, else the steps.
// TODO(design): cards hold no cues or hint lines of their own, so there's no hint line.
function pdCues(c) {
  const items = (c.step_lists || []).flatMap((l) => l.items);
  return items.length ? items : pdSteps(c).map((s) => s.text);
}

function pdCornellNotes(c) {
  const v = store.get(`pd:cornell:${c.assignment_id}`, null);
  return v && typeof v === "object" ? { cue: 0, notes: {}, summary: "", ...v } : { cue: 0, notes: {}, summary: "" };
}

const pdSaveCornell = (c, v) => store.set(`pd:cornell:${c.assignment_id}`, v);

function pdCornell(c) {
  const cues = pdCues(c);
  const v = pdCornellNotes(c);
  if (v.cue >= cues.length) v.cue = 0;
  const row = el("div", "pd-row");
  const main = el("div", "pd-main cornell");
  const narrowWarn = pdWatchOut(c, false, "pd-narrow-only");
  if (narrowWarn) main.append(narrowWarn);
  const pair = el("div", "pd-cornell");
  const list = el("div", "pd-cues");
  const panel = el("div", "pd-note-panel");
  const select = (i, focusNotes = false) => {
    v.cue = i;
    pdSaveCornell(c, v);
    drawList();
    drawPanel();
    if (focusNotes) panel.querySelector("textarea")?.focus();
  };
  const drawList = () => {
    list.replaceChildren(el("h2", "pd-over", "Cues"));
    cues.forEach((cue, i) => {
      const b = el("button");
      b.type = "button";
      b.setAttribute("aria-pressed", String(i === v.cue));
      const n = el("span", "n", pdTwo(i));
      n.setAttribute("aria-hidden", "true");
      b.append(n, el("span", null, cue));
      b.addEventListener("click", () => select(i));
      list.append(b);
    });
  };
  const drawPanel = () => {
    const i = v.cue;
    const area = el("textarea", "pd-textarea");
    area.id = `pd-cue-${c.assignment_id}`;
    area.rows = 7;
    area.placeholder = "Your notes, in your own words";
    area.value = v.notes[i] || "";
    area.addEventListener("input", () => {
      v.notes[i] = area.value;
      pdSaveCornell(c, v);
    });
    const cue = el("label", "pd-cue", cues[i]);
    cue.htmlFor = area.id;
    const foot = el("div", "pd-note-foot");
    const prev = pdBtn("Previous", "quiet", () => select(i - 1, true));
    prev.disabled = i === 0;
    const last = i === cues.length - 1;
    foot.append(prev, pdBtn(last ? "Back to cue 1" : "Next cue", "soft", () => select(last ? 0 : i + 1, true)));
    panel.replaceChildren(el("p", "pd-over strong", `Cue ${i + 1} of ${cues.length}`), cue, area, foot);
  };
  drawList();
  drawPanel();
  pair.append(list, panel);
  const summary = el("section", "pd-hero summary");
  const area = el("textarea", "pd-textarea summary");
  area.id = `pd-summary-${c.assignment_id}`;
  area.rows = 3;
  area.value = v.summary;
  area.addEventListener("input", () => {
    v.summary = area.value;
    pdSaveCornell(c, v);
  });
  // TODO(design): cards hold no summary prompt, so it's a general one.
  const prompt = el("label", "pd-summary-q", "In two or three sentences, what is this reading mostly about?");
  prompt.htmlFor = area.id;
  summary.append(el("h2", "pd-over strong", "Summary"), prompt, area);
  main.append(pair, summary);
  const side = el("aside", "pd-side");
  side.append(pdFilesSection(c));
  const warn = pdWatchOut(c, false, "pd-wide-only");
  if (warn) side.append(warn);
  row.append(main, side);
  return row;
}

const PD_RENDER = { document: pdDocument, checklist: pdChecklist, dashboard: pdDashboard, cornell: pdCornell };

// ---- Focus mode ---------------------------------------------------------------------------------
//
// Full screen, one step at a time, with a timer. Opened from any template; Exit goes back to it with
// the ticks kept. "Do Not Disturb" holds this page's own pings, chimes and missed-start window while
// it's on (a browser can't switch the system's notifications off).
// TODO(design): pushes from the server still reach the student's other devices meanwhile.

let pdFocus = null;
let pdFocusTimer = null;

function pdFocusSteps(c, tpl) {
  if (tpl !== "cornell") return pdSteps(c);
  const cues = pdCues(c);
  const each = Math.max(5, Math.round((PD_EFFORT_MINUTES[c.effort] || 60) / cues.length / 5) * 5);
  return cues.map((text) => ({ text, minutes: each, sitting: null, lists: [] }));
}

async function startFocus(c) {
  const tpl = pdTemplate(c);
  const steps = pdFocusSteps(c, tpl);
  if (!steps.length) return;
  const cur = tpl === "cornell" ? pdCornellNotes(c).cue : pdCurrent(steps, pdChecked(c));
  pdFocus = { id: c.assignment_id, tpl, i: Math.max(0, Math.min(cur, steps.length - 1)), dnd: true, finished: false, opener: document.activeElement };
  pdFocusStep();
  document.body.classList.add("pd-focus-open", "pd-dnd-on");
  pdRenderFocus();
  clearInterval(pdFocusTimer);
  pdFocusTimer = setInterval(pdTick, 1000);
  await pdStarted(c);
}

// Reset the timer to the current step's estimate.
function pdFocusStep() {
  const c = cards.find((x) => x.assignment_id === pdFocus.id);
  const steps = pdFocusSteps(c, pdFocus.tpl);
  pdFocus.total = steps[pdFocus.i].minutes * 60;
  pdFocus.endsAt = Date.now() + pdFocus.total * 1000;
  pdFocus.left = pdFocus.total;
  pdFocus.paused = false;
  pdFocus.said = "";
}

const pdQuiet = () => !!(pdFocus && pdFocus.dnd && !pdFocus.finished);

function pdCloseFocus() {
  clearInterval(pdFocusTimer);
  const opener = pdFocus?.opener;
  pdFocus = null;
  $("#pd-focus")?.remove();
  document.body.classList.remove("pd-focus-open", "pd-dnd-on");
  pdRender(true);
  // Safari doesn't focus a clicked button, so the opener can be <body>.
  if (opener && opener !== document.body && document.contains(opener)) opener.focus();
  else $(".pd-controls .pd-btn.soft")?.focus();
}

document.addEventListener("keydown", (e) => {
  const modalShowing = document.body.classList.contains("modal-open") && !pdQuiet(); // DND hides it
  if (e.key === "Escape" && pdFocus && !modalShowing) pdCloseFocus();
});

const pdClockText = (s) => `${Math.floor(Math.max(0, s) / 60)}:${String(Math.max(0, s) % 60).padStart(2, "0")}`;

function pdTick() {
  if (!pdFocus || pdFocus.finished || pdFocus.paused) return;
  const left = Math.max(0, Math.ceil((pdFocus.endsAt - Date.now()) / 1000));
  if (left === pdFocus.left) return;
  const was = pdFocus.left;
  pdFocus.left = left;
  const ui = pdFocus.ui;
  if (!ui) return;
  if (left === 0) {
    pdRenderFocus();
    pdSay("Time's up");
    return;
  }
  ui.timer.textContent = pdClockText(left);
  ui.bar.style.width = `${Math.round((1 - left / pdFocus.total) * 100)}%`;
  if (was > 60 && left <= 60) pdSay("1 minute left");
}

// Only "1 minute left" and "Time's up" are announced, never each tick.
function pdSay(text) {
  if (pdFocus?.ui?.live) pdFocus.ui.live.textContent = text;
}

function pdRenderFocus() {
  if (!pdFocus) return;
  const c = cards.find((x) => x.assignment_id === pdFocus.id);
  if (!c) return pdCloseFocus();
  let root = $("#pd-focus");
  if (!root) {
    root = el("div", "pd-focus");
    root.id = "pd-focus";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", `Focus: ${c.name}`);
    document.body.append(root);
  }
  pdPaint(root, c.color);
  const steps = pdFocusSteps(c, pdFocus.tpl);
  const checked = pdChecked(c);
  const f = pdFocus;
  const wrap = el("div", "pd-focus-wrap");

  // Header: exit, the class and assignment, which step.
  const head = el("header", "pd-focus-head");
  const exit = pdBtn(`Exit to ${PD_TEMPLATE_NAMES[f.tpl]}`, "neutral ui back", pdCloseFocus, "x");
  const kicker = el("p", "pd-kicker");
  const dot = el("span", "pd-dot");
  dot.setAttribute("aria-hidden", "true");
  kicker.append(dot, document.createTextNode([c.course, c.name].filter(Boolean).join(" · ")));
  head.append(exit, kicker, el("p", "pd-over strong", f.finished ? "All done" : `Step ${f.i + 1} of ${steps.length}`));
  const bar = el("div", "pd-stepbar");
  bar.setAttribute("aria-hidden", "true");
  bar.style.gridTemplateColumns = `repeat(${steps.length}, minmax(0, 1fr))`;
  steps.forEach((_, j) => {
    const isDone = f.finished || (f.tpl === "cornell" ? j < f.i : checked.has(j));
    bar.append(el("div", j === f.i && !f.finished ? "current" : isDone ? "done" : ""));
  });
  wrap.append(head, bar);

  const main = el("main", "pd-focus-main");
  const live = el("p", "sr-only");
  live.setAttribute("aria-live", "polite");
  if (f.finished) {
    const back = pdBtn(`Back to ${PD_TEMPLATE_NAMES[f.tpl]}`, "soft tall", pdCloseFocus);
    back.style.alignSelf = "flex-start";
    main.append(el("h1", "pd-h1 pd-focus-title", "All done"), el("p", "pd-lead", `You finished every step of ${c.name}.`), back);
    wrap.append(main, live);
    root.replaceChildren(wrap);
    root.scrollTop = 0;
    f.ui = { live };
    back.focus();
    return;
  }
  // A redraw replaces every control: put the keyboard focus back on the same one afterwards.
  const active = root.contains(document.activeElement) ? document.activeElement : null;
  const was = active && (active.tagName === "TEXTAREA" ? { area: active.selectionStart } : { text: active.textContent });
  const step = steps[f.i];

  // Do Not Disturb, until the remaining steps' time is up.
  if (f.dnd) {
    const until = new Date(now().getTime() + (f.left + steps.slice(f.i + 1).reduce((a, s) => a + s.minutes * 60, 0)) * 1000);
    const strip = el("div", "pd-dnd");
    strip.setAttribute("role", "status");
    const p = el("p");
    const span = el("span");
    span.append(el("strong", null, "Do Not Disturb is on."), document.createTextNode(` Notifications are paused until ${fmtTime(until)}.`));
    p.append(pdIcon("bell-off", 18), span);
    strip.append(
      p,
      pdBtn("Turn off", "quiet", () => {
        f.dnd = false;
        document.body.classList.remove("pd-dnd-on");
        pdRenderFocus();
      }),
    );
    main.append(strip);
  }

  // The timer. At 0 it asks rather than moving on.
  const timerBox = el("section", "pd-hero timer");
  const leftCol = el("div", "pd-timer-left");
  const rightCol = el("div", "pd-timer-right");
  const track = el("div", "pd-timer-bar");
  track.setAttribute("aria-hidden", "true");
  const fill = el("div");
  fill.style.width = `${Math.round((1 - f.left / f.total) * 100)}%`;
  track.append(fill);
  const timer = el("p", "pd-timer", pdClockText(f.left));
  if (f.left > 0) {
    leftCol.append(el("h2", "pd-over strong", "Time left on this step"), timer);
    rightCol.append(
      track,
      pdBtn(f.paused ? "Resume timer" : "Pause timer", "hero-outline", () => {
        if (f.paused) f.endsAt = Date.now() + f.left * 1000;
        f.paused = !f.paused;
        pdRenderFocus();
      }),
    );
  } else {
    leftCol.append(el("h2", "pd-over strong", "Time left on this step"), el("p", "pd-timer-up", "Time's up. Keep going or move on?"));
    rightCol.append(
      track,
      pdBtn("Add 5 minutes", "hero-outline", () => {
        f.total = f.left = 300;
        f.endsAt = Date.now() + 300_000;
        pdRenderFocus();
      }),
    );
  }
  timerBox.append(leftCol, rightCol);
  main.append(timerBox, el("h1", "pd-h1 pd-focus-title", step.text));

  // Cornell: the cue's own notes box, the same notes as the template.
  if (f.tpl === "cornell") {
    const v = pdCornellNotes(c);
    const area = el("textarea", "pd-textarea");
    area.rows = 7;
    area.placeholder = "Your notes, in your own words";
    area.setAttribute("aria-label", `Notes for: ${step.text}`);
    area.value = v.notes[f.i] || "";
    area.addEventListener("input", () => {
      v.notes[f.i] = area.value;
      pdSaveCornell(c, v);
    });
    main.append(area);
  }

  // Files: on a reading sitting, the reading's row says which pages and opens to the first one.
  const files = pdSection("Files", "tight pd-files-focus");
  const list = pdFiles(c);
  if (!list.length) files.append(el("p", "pd-muted", "No files attached"));
  list.forEach((file) => {
    const row = el("div", "pd-file-row");
    const s = step.sitting;
    const isReading = s && c.reading && file.url && file.name === c.reading.file;
    const shown = isReading ? { ...file, extra: s.first_page === s.last_page ? `page ${s.first_page}` : `pages ${s.first_page}–${s.last_page}` } : file;
    const link = pdFileRow({ ...shown, openDoc: "" });
    row.append(link);
    if (isReading) {
      const page = (s.pdf_pages || "").match(/\d+/)?.[0] || s.first_page;
      row.append(pdLink(`Open to page ${s.first_page}`, "soft", `${file.url}#page=${page}`, "arrow-right"));
    } else if (file.openDoc) row.append(pdLink("Open this doc", "soft", file.openDoc));
    files.append(row);
  });
  if (step.sitting) {
    const stop = el("p", "pd-stop");
    stop.append(pdIcon("panel-left", 20, 1.8), document.createTextNode(`Stop at the end of page ${step.sitting.last_page}. Then mark this step done.`));
    files.append(stop);
  }
  main.append(files);

  if (c.watch_out) {
    const pill = el("p", "pd-pill focus");
    pill.append(pdIcon("triangle-alert", 17, 2.2), document.createTextNode(c.watch_out));
    main.append(pill);
  }

  // Up next, Back, and Done.
  const next = steps[f.i + 1];
  const upNext = el("section", "pd-next");
  const text = el("div", "pd-next-text");
  const nextLine = el("p");
  if (next) nextLine.append(document.createTextNode(next.text), el("span", "pd-muted", ` · about ${next.minutes} min`));
  else nextLine.append(document.createTextNode("Nothing. This is the last step."));
  text.append(el("h2", "pd-over", "Up next"), nextLine);
  const actions = el("div", "pd-actions");
  const back = pdBtn("Back", "neutral tall", () => {
    f.i -= 1;
    pdFocusStep();
    pdRenderFocus();
  }, "arrow-left");
  back.disabled = f.i === 0;
  const done = pdBtn(next ? "Done, next step" : "Done", "soft tall", () => {
    if (f.tpl !== "cornell") {
      checked.add(f.i);
      pdSetChecked(c, checked);
    }
    if (next) {
      f.i += 1;
      pdFocusStep();
    } else {
      f.finished = true;
      f.dnd = false;
      document.body.classList.remove("pd-dnd-on");
    }
    pdRenderFocus();
  }, "arrow-right");
  actions.append(back, done);
  upNext.append(text, actions);
  main.append(upNext);

  wrap.append(main, live);
  root.replaceChildren(wrap);
  f.ui = { timer, bar: fill, live };
  // A new step starts at the top, with the timer in view; on a phone "Done, next step" sits below
  // the fold, so focusing it must not scroll there.
  if (f.drawn !== f.i) root.scrollTop = 0;
  f.drawn = f.i;
  const still = { preventScroll: true };
  if (!f.shown) {
    f.shown = true;
    done.focus(still);
  } else if (was?.area !== undefined) {
    const area = root.querySelector("textarea");
    area?.focus(still);
    area?.setSelectionRange(was.area, was.area);
  } else if (was) {
    ([...root.querySelectorAll("button, a")].find((b) => b.textContent === was.text) || done).focus(still);
  }
}

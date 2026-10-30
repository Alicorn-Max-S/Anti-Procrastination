// Start Helper files: the service worker.
//
// Every 10 minutes (and on "Fetch now") it asks the Start Helper server which school Google
// files it still needs, downloads each one here with the browser's own Google sign-in, and posts
// the file to the server. Chrome attaches the Google cookies itself because of the host
// permissions; this code never reads or stores a cookie. When the student opens a doc, the
// content script reports it, and the server says whether to save it (a linked file it still
// lacks, or the student's Google Assignments copy).

import {
  MAX_BYTES, REASONS, classify, classifyView, filenameFrom, fromBase64, isCanvasAssignment, isFetchable,
  withAuthUser,
} from "./lib.js";

const ALARM = "poll";
const PERIOD_MINUTES = 10;
const PER_RUN = 10; // files per run, fetched one at a time
const TIMEOUT_MS = 120_000;

// ---- settings + server ------------------------------------------------------------------------

async function settings() {
  const { server = "", key = "", email = "" } = await chrome.storage.local.get(["server", "key", "email"]);
  return { server, key, email };
}

async function api(path, { json, form } = {}) {
  const { server, key } = await settings();
  if (!server || !key) throw new Error("Not set up: open the extension's options and add the server and key.");
  const init = { headers: { "X-Start-Helper-Key": key } };
  if (json) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    init.method = "POST";
    init.body = form;
  }
  const r = await fetch(server + path, init);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${r.status}: ${body.detail || r.statusText}`);
  return body;
}

async function record(update) {
  const { last = {} } = await chrome.storage.local.get("last");
  await chrome.storage.local.set({ last: { ...last, ...update, at: new Date().toISOString() } });
}

// ---- fetching from Google ---------------------------------------------------------------------

// Read a response body, giving up past MAX_BYTES (a missing Content-Length can't hide a huge file).
async function readCapped(response, controller) {
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_BYTES) {
      controller.abort();
      return null;
    }
    chunks.push(value);
  }
  return new Blob(chunks, { type: response.headers.get("content-type") || "application/octet-stream" });
}

// fetch() can't follow a redirect to a site the extension has no access to (Google's sign-in
// page), so it throws as if the network were down. A second request that doesn't follow
// redirects tells the two apart.
async function redirectsOffSite(url) {
  try {
    const r = await fetch(url, { credentials: "include", cache: "no-store", redirect: "manual" });
    return r.type === "opaqueredirect";
  } catch {
    return false;
  }
}

async function tryFetch(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(url, { credentials: "include", cache: "no-store", signal: controller.signal });
    const verdict = classify({
      status: r.status,
      finalUrl: r.url,
      contentType: r.headers.get("content-type") || "",
      length: Number(r.headers.get("content-length") || 0),
    });
    if (!verdict.ok) {
      controller.abort();
      return verdict;
    }
    const blob = await readCapped(r, controller);
    if (!blob) return { ok: false, reason: "too_big", detail: "over 100 MB" };
    return { ok: true, blob, filename: filenameFrom(r.headers.get("content-disposition")) };
  } catch (e) {
    if (!controller.signal.aborted && (await redirectsOffSite(url))) {
      return { ok: false, reason: "signin", detail: "Google sent it to the sign-in page" };
    }
    return { ok: false, reason: "network", detail: String(e).slice(0, 200) };
  } finally {
    clearTimeout(timer);
  }
}

// Load the doc's normal page once to tell "no access" from "downloading turned off".
async function probe(viewUrl, email) {
  if (!isFetchable(viewUrl)) return "error";
  const url = withAuthUser(viewUrl, email);
  try {
    const r = await fetch(url, { credentials: "include", cache: "no-store" });
    const text = r.ok ? (await r.text()).slice(0, 200_000) : "";
    return classifyView({ status: r.status, finalUrl: r.url, text });
  } catch {
    return (await redirectsOffSite(url)) ? "signin" : "network";
  }
}

// item: {fetch_url, view_url} from the server. Tries the school account first when its email is
// set, then the browser's default account.
async function fetchGoogle(item, email) {
  if (!isFetchable(item.fetch_url)) return { ok: false, reason: "error", detail: "not a Google download URL" };
  const urls = email ? [withAuthUser(item.fetch_url, email), item.fetch_url] : [item.fetch_url];
  let got;
  for (const url of urls) {
    got = await tryFetch(url);
    if (got.ok || !["signin", "denied", "page"].includes(got.reason)) break;
  }
  if (got.reason === "denied" || got.reason === "page") {
    got = { ok: false, reason: await probe(item.view_url, email), detail: got.detail };
  }
  return got;
}

async function upload(url, got, title, assignmentId) {
  const form = new FormData();
  form.append("url", url);
  form.append("title", title || "");
  if (assignmentId != null) form.append("assignment_id", String(assignmentId));
  form.append("file", got.blob, got.filename || "download");
  return api("/api/ext/files", { form });
}

async function reportFailure(url, got) {
  try {
    await api("/api/ext/failed", { json: { url, reason: got.reason in REASONS ? got.reason : "error", detail: got.detail || "" } });
  } catch (e) {
    console.warn("couldn't report failure", e);
  }
}

// ---- the polling run --------------------------------------------------------------------------

let running = null;

function runOnce(trigger) {
  if (!running) running = doRun(trigger).finally(() => (running = null));
  return running;
}

async function doRun(trigger) {
  const { email } = await settings();
  let needed;
  try {
    needed = await api("/api/ext/needed");
  } catch (e) {
    await record({ error: String(e.message || e), trigger });
    return { error: String(e.message || e) };
  }
  await chrome.storage.local.set({ watch: needed.watch || [], copies: needed.copies || [] });
  const saved = [];
  const failed = [];
  for (const item of needed.files.slice(0, PER_RUN)) {
    const got = await fetchGoogle(item, email);
    if (got.ok) {
      try {
        await upload(item.url, got, filenameTitle(got.filename) || item.name, null);
        saved.push(item.name);
        continue;
      } catch (e) {
        failed.push({ name: item.name, reason: "error", detail: String(e.message || e) });
        continue;
      }
    }
    await reportFailure(item.url, got);
    failed.push({ name: item.name, reason: got.reason, detail: got.detail || "" });
  }
  const result = {
    error: "", trigger, saved, failed,
    left: Math.max(0, needed.files.length - PER_RUN),
    copies: needed.copies || [],
  };
  await record(result);
  return result;
}

function filenameTitle(filename) {
  return filename ? filename.replace(/\.[A-Za-z0-9]{2,5}$/, "") : "";
}

// ---- docs the student opens -------------------------------------------------------------------

// The Canvas assignment page this tab was opened from, if any: Google Assignments opens the
// student's copy in a new tab from the Canvas page (or from a tool tab that page opened). Tab URLs
// are only visible for sites the extension may access, so this needs the Canvas permission from
// the options page.
async function canvasOpener(tab, frameId) {
  if (!tab) return "";
  if (frameId && isCanvasAssignment(tab.url)) return tab.url; // a doc framed inside the Canvas page
  let t = tab;
  for (let i = 0; i < 3 && t.openerTabId != null; i++) {
    try {
      t = await chrome.tabs.get(t.openerTabId);
    } catch {
      break;
    }
    if (isCanvasAssignment(t.url)) return t.url;
  }
  return "";
}

function fileId(url) {
  const m = url.match(/\/d\/([\w-]+)/) || url.match(/[?&]id=([\w-]+)/);
  return m ? m[1] : "";
}

async function badge(tabId, ok, title) {
  if (tabId == null) return;
  await chrome.action.setBadgeText({ tabId, text: ok ? "✓" : "!" });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: ok ? "#2e7d32" : "#c77700" });
  await chrome.action.setTitle({ tabId, title });
}

// Fetch the export from inside the open doc: a same-origin request with first-party cookies, for
// when the service worker's request didn't carry the sign-in (strict third-party-cookie settings).
async function fetchInPage(tab, frameId, fetchUrl) {
  try {
    const r = await chrome.tabs.sendMessage(tab.id, { type: "fetchHere", url: fetchUrl }, { frameId });
    if (r && r.ok) return { ok: true, blob: new Blob([fromBase64(r.data)], { type: r.type }), filename: r.filename };
  } catch (e) {
    console.warn("in-page fetch failed", e);
  }
  return null;
}

async function onOpened(msg, sender) {
  const { server, key, email } = await settings();
  if (!server || !key) return { wanted: false };
  const tab = sender.tab;
  const opener = await canvasOpener(tab, sender.frameId);
  // Only docs the server is watching for, or opened from a Canvas assignment, are reported:
  // the server doesn't need to hear about every doc the student opens.
  const { watch = [] } = await chrome.storage.local.get("watch");
  if (!opener && !watch.includes(fileId(msg.url))) return { wanted: false };
  let answer;
  try {
    answer = await api("/api/ext/opened", { json: { url: msg.url, opener_url: opener } });
  } catch (e) {
    return { wanted: false, error: String(e.message || e) };
  }
  if (!answer.wanted) return answer;
  return saveDoc(answer, msg.title, tab, sender.frameId, email);
}

async function saveDoc(answer, pageTitle, tab, frameId, email) {
  let got = await fetchGoogle(answer, email);
  if (!got.ok && tab && ["signin", "network"].includes(got.reason) && new URL(answer.fetch_url).origin === new URL(tab.url || answer.url).origin) {
    got = (await fetchInPage(tab, frameId ?? 0, answer.fetch_url)) || got;
  }
  if (got.ok) {
    try {
      await upload(answer.url, got, filenameTitle(got.filename) || pageTitle, answer.assignment_id);
      await badge(tab?.id, true, answer.assignment ? `Saved as your copy for ${answer.assignment}` : "Saved to Start Helper");
      return { wanted: true, saved: true };
    } catch (e) {
      await badge(tab?.id, false, `Start Helper didn't take it: ${e.message || e}`);
      return { wanted: true, saved: false, error: String(e.message || e) };
    }
  }
  await reportFailure(answer.url, got);
  await badge(tab?.id, false, `Couldn't save: ${REASONS[got.reason] || got.reason}`);
  return { wanted: true, saved: false, reason: got.reason };
}

// The popup's "This is my copy for …" on the open doc.
async function saveCopy({ tabId, url, title, assignmentId }) {
  const { email } = await settings();
  let answer;
  try {
    answer = await api("/api/ext/opened", { json: { url, opener_url: "", assignment_id: assignmentId } });
  } catch (e) {
    return { saved: false, error: String(e.message || e) };
  }
  if (!answer.wanted) return { saved: false, error: "Start Helper doesn't need this doc for that assignment." };
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const result = await saveDoc(answer, title, tab, 0, email);
  if (result.saved) await runOnce("copy"); // refresh the waiting-copies list
  return result;
}

// ---- wiring -----------------------------------------------------------------------------------

// Alarms usually survive a browser restart, but Chrome doesn't promise it.
async function ensureAlarm() {
  if (!(await chrome.alarms.get(ALARM))) {
    await chrome.alarms.create(ALARM, { periodInMinutes: PERIOD_MINUTES, delayInMinutes: 1 });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await ensureAlarm();
  const { server } = await settings();
  if (!server) chrome.runtime.openOptionsPage();
});

chrome.runtime.onStartup.addListener(async () => {
  await ensureAlarm();
  runOnce("startup");
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) runOnce("timer");
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  const handlers = {
    opened: () => onOpened(msg, sender),
    run: () => runOnce("button"),
    saveCopy: () => saveCopy(msg),
  };
  const handler = handlers[msg.type];
  if (!handler) return false;
  handler().then(sendResponse, (e) => sendResponse({ error: String(e.message || e) }));
  return true; // answer asynchronously
});

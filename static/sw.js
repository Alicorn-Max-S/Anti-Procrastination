// Service worker: shows start-time pushes with the check-in's buttons, answers from those buttons, opens the right check-in on tap, and keeps the page shell cached so the
// installed app opens without a network. Served at /sw.js for full scope.

const CACHE = "start-helper-v7";
const SHELL = ["/", "/static/app.js", "/static/prepdoc.js", "/static/theme.js", "/static/style.css", "/static/prepdoc.css", "/static/manifest.webmanifest", "/static/icons/icon-192.png"];
const ICON = "/static/icons/icon-192.png";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Network first for the shell (so edits show up right away), cache as the offline fallback.
// "no-cache" makes the browser check with the server instead of reusing its own HTTP-cached copy,
// which could pair a new page with an old stylesheet. API calls always go to the network.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(event.request, { cache: "no-cache" })
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(event.request, { ignoreSearch: true })),
  );
});

// The buttons when a push doesn't list its own (an older server). Each is {action, title} plus the
// check-in `answer` (and `picked_time`) it posts; one without an answer opens the check-in.
const ACTIONS = [
  { action: "start_now", title: "Start now", answer: "start_now" },
  { action: "in_15", title: "In 15 min", answer: "plus_15" },
  { action: "pick", title: "Pick a time…" },
];

// A ping or a missed start (the server sends `kind`, `block_id`, `renotify` and `actions`, the
// same buttons as the check-in on the page) gets the buttons and a buzz; Chrome on a laptop shows
// the first two. A missed start stays on screen until answered, and a reminder alerts again under
// the same tag. app.js's notifyOptions matches this for the notifications the page shows.
function options(data) {
  const opts = { body: data.body, tag: data.tag || undefined, icon: ICON, badge: ICON, data };
  if (!data.block_id) return opts;
  const missed = data.kind === "missed";
  return {
    ...opts,
    actions: (data.actions || ACTIONS).map(({ action, title }) => ({ action, title })),
    requireInteraction: missed,
    renotify: Boolean(data.renotify && data.tag),
    vibrate: missed ? [300, 100, 300, 100, 300] : [200, 100, 200],
  };
}

self.addEventListener("push", (event) => {
  let data = { title: "Time to start", body: "Open Start Helper.", url: "/" };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch {
    if (event.data) data.body = event.data.text();
  }
  event.waitUntil(data.kind === "answered" ? answeredElsewhere(data) : show(data));
});

// An open page stops waiting to show this one itself (app.js notifyNew).
async function show(data) {
  await self.registration.showNotification(data.title, options(data));
  if (data.tag) await tellPages({ type: "pushed", tag: data.tag });
}

// Answered on another device: swap the notification still up here for a quiet one. A push has to
// show something or Chrome shows its own, so it only skips that when nothing is up to replace.
async function answeredElsewhere(data) {
  const stale = (await self.registration.getNotifications()).filter((n) => (data.clear || []).includes(n.tag));
  stale.slice(1).forEach((n) => n.close());
  if (stale.length) {
    await self.registration.showNotification(data.title, { body: data.body, tag: stale[0].tag, silent: true, icon: ICON, badge: ICON, data: { url: "/" } });
  }
  await tellPages({ type: "answered" });
}

async function tellPages(message) {
  const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  wins.forEach((w) => w.postMessage(message));
}

// A tap focuses the open page and tells it which block (no reload); otherwise opens /?block=ID.
// A button with an answer answers without opening anything (Start now then opens the card).
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const url = data.url || "/";
  const blockId = data.block_id || new URL(url, location.origin).searchParams.get("block");
  const chosen = event.action ? (data.actions || ACTIONS).find((a) => a.action === event.action) : null;
  event.waitUntil(chosen?.answer && blockId ? answerFromNotification(blockId, chosen, url) : openPage(url, blockId));
});

async function openPage(url, blockId) {
  const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const open = wins.find((w) => new URL(w.url).origin === location.origin);
  if (!open) return self.clients.openWindow(url);
  await open.focus();
  if (blockId) open.postMessage({ type: "open-block", blockId });
}

// Posts the check-in like the page does. The subscription's endpoint tells the server which
// device answered, so the "answered" push goes only to the others. If it can't be sent, the page
// opens on the check-in instead.
async function answerFromNotification(blockId, chosen, url) {
  try {
    const sub = await self.registration.pushManager.getSubscription();
    const res = await fetch(`/api/blocks/${blockId}/checkin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer: chosen.answer, picked_time: chosen.picked_time || null, endpoint: sub?.endpoint || "" }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch {
    return openPage(url, blockId);
  }
  await tellPages({ type: "answered" });
  if (chosen.answer === "start_now") await openPage(url, blockId);
}

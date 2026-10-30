// Service worker: shows start-time pushes, opens the right check-in on tap, and keeps the page
// shell cached so the installed app opens without a network. Served at /sw.js for full scope.

const CACHE = "start-helper-v2";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/manifest.webmanifest", "/static/icons/icon-192.png"];

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
// API calls always go to the network.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(event.request)
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

self.addEventListener("push", (event) => {
  let data = { title: "Time to start", body: "Open Start Helper.", url: "/" };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch {
    if (event.data) data.body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: data.tag || undefined,
      icon: "/static/icons/icon-192.png",
      badge: "/static/icons/icon-192.png",
      data,
    }),
  );
});

// A tap focuses the open page and tells it which block (no reload); otherwise opens /?block=ID.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/";
  const blockId = new URL(url, location.origin).searchParams.get("block");
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (wins) => {
      const open = wins.find((w) => new URL(w.url).origin === location.origin);
      if (!open) return self.clients.openWindow(url);
      await open.focus();
      if (blockId) open.postMessage({ type: "open-block", blockId });
    }),
  );
});

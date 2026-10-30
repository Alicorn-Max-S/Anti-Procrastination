// Start Helper files: runs on Google Docs/Slides/Sheets/Drive file pages (and frames of them).
// Tells the service worker which file this page shows; the server decides whether to save it.
// When asked, fetches the file's export from here: a same-origin request, so it works even when
// the browser's cookie settings keep the sign-in off the service worker's request.
(() => {
  const DOC = /^https:\/\/(?:docs\.google\.com\/(?:document|presentation|spreadsheets)|drive\.google\.com\/file)\/(?:u\/\d+\/)?d\/([\w-]+)/;
  const MAX_BYTES = 100_000_000;
  const m = location.href.match(DOC);
  if (!m || m[1] === "e") return; // "/d/e/…" is a published (public) doc

  const title = () => document.title.replace(/ - Google (Docs|Slides|Sheets|Drive)$/, "").trim();

  function toBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  }

  async function fetchHere(url) {
    const u = new URL(url);
    if (u.origin !== location.origin || !u.pathname.includes("/export")) return { ok: false };
    const r = await fetch(u, { credentials: "same-origin", cache: "no-store" });
    const type = r.headers.get("content-type") || "";
    if (!r.ok || type.startsWith("text/html")) return { ok: false };
    const buffer = await r.arrayBuffer();
    if (buffer.byteLength > MAX_BYTES) return { ok: false };
    const disposition = r.headers.get("content-disposition") || "";
    const name = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    return { ok: true, data: toBase64(buffer), type, filename: name ? decodeURIComponent(name[1]) : "" };
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type !== "fetchHere") return false;
    fetchHere(msg.url).then(sendResponse, () => sendResponse({ ok: false }));
    return true;
  });

  chrome.runtime.sendMessage({ type: "opened", url: location.href, title: title() }).catch(() => {});
})();

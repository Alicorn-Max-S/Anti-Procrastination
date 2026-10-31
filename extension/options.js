import { normalizeServer, permissionPattern } from "./lib.js";

const $ = (id) => document.getElementById(id);

function show(el, text, cls = "") {
  el.textContent = text;
  el.className = cls;
}

async function load() {
  const { server = "http://localhost:8000", key = "", email = "", canvasUrl = "" } =
    await chrome.storage.local.get(["server", "key", "email", "canvasUrl"]);
  $("server").value = server;
  $("key").value = key;
  $("email").value = email;
  if (canvasUrl) await showCanvas(canvasUrl);
}

async function showCanvas(canvasUrl) {
  const pattern = permissionPattern(canvasUrl);
  if (!pattern) return;
  $("canvas").hidden = false;
  $("canvas-origin").textContent = new URL(canvasUrl).host;
  const granted = await chrome.permissions.contains({ origins: [pattern] });
  if (granted) show($("canvas-result"), "Allowed.", "ok");
  $("allow-canvas").disabled = granted;
}

$("save").addEventListener("click", async () => {
  const server = normalizeServer($("server").value);
  if (!server) return show($("result"), "That isn't a web address.", "warn");
  // Ask for a tunnel's site right away: Chrome only shows the prompt during the click.
  const pattern = permissionPattern(server);
  const allowed = pattern ? await chrome.permissions.request({ origins: [pattern] }) : true;
  if (!allowed) return show($("result"), `Chrome needs permission to reach ${new URL(server).host}.`, "warn");

  const key = $("key").value.trim();
  const email = $("email").value.trim();
  await chrome.storage.local.set({ server, key, email });
  show($("result"), "Testing…");
  try {
    const r = await fetch(`${server}/api/ext/hello`, { headers: { "X-Start-Helper-Key": key } });
    const body = await r.json().catch(() => ({}));
    if (r.status === 401) return show($("result"), "The server says the key is wrong.", "warn");
    if (!r.ok) return show($("result"), `Server answered ${r.status}: ${body.detail || r.statusText}`, "warn");
    await chrome.storage.local.set({ canvasUrl: body.canvas_url || "" });
    show($("result"), "Connected. Fetching files now…", "ok");
    if (body.canvas_url) await showCanvas(body.canvas_url);
    const run = await chrome.runtime.sendMessage({ type: "run" });
    if (run?.error) return show($("result"), `Connected, but: ${run.error}`, "warn");
    const failed = run?.failed?.length || 0;
    show($("result"), `Connected. Saved ${run?.saved?.length || 0} file(s)${failed ? `, ${failed} couldn't be fetched (see the toolbar popup)` : ""}.`, "ok");
  } catch (e) {
    show($("result"), `Couldn't reach ${server}. Is Start Helper running? (${e.message || e})`, "warn");
  }
});

$("allow-canvas").addEventListener("click", async () => {
  const { canvasUrl = "" } = await chrome.storage.local.get("canvasUrl");
  const pattern = permissionPattern(canvasUrl);
  if (!pattern) return;
  const granted = await chrome.permissions.request({ origins: [pattern] });
  show($("canvas-result"), granted ? "Allowed." : "Not allowed. Use the popup's \"This is my copy\" instead.", granted ? "ok" : "warn");
  $("allow-canvas").disabled = granted;
});

load();

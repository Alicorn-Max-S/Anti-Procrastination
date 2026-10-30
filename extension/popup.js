import { REASONS } from "./lib.js";

const $ = (id) => document.getElementById(id);
const DOC = /^https:\/\/(?:docs\.google\.com\/(?:document|presentation|spreadsheets)|drive\.google\.com\/file)\/(?:u\/\d+\/)?d\/([\w-]+)/;

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function when(iso) {
  if (!iso) return "never";
  const d = new Date(iso);
  return d.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

async function render() {
  const { server = "", key = "", last = {}, copies = [] } =
    await chrome.storage.local.get(["server", "key", "last", "copies"]);
  const box = $("last");
  box.replaceChildren();
  if (!server || !key) {
    $("server").replaceChildren(el("span", "Not set up yet. Open Settings and add the server and key.", "warn"));
    $("run").disabled = true;
    return;
  }
  $("server").replaceChildren(el("span", "Server: "), el("code", server));
  if (last.error) {
    box.append(el("p", `Last try (${when(last.at)}): ${last.error}`, "warn"));
  } else if (last.at) {
    const saved = last.saved || [];
    const failed = last.failed || [];
    box.append(el("p", `Last run ${when(last.at)}: saved ${saved.length}, couldn't get ${failed.length}` +
      (last.left ? `, ${last.left} more next time` : "") + "."));
    if (saved.length) {
      const ul = el("ul");
      saved.forEach((name) => ul.append(el("li", name, "ok")));
      box.append(ul);
    }
    if (failed.length) {
      const ul = el("ul");
      failed.forEach((f) => ul.append(el("li", `${f.name}: ${REASONS[f.reason] || f.detail || f.reason}`, "warn")));
      box.append(ul, el("p", "Open those from their cards; if downloading is allowed, opening one saves it.", "hint"));
    }
  } else {
    box.append(el("p", "Hasn't run yet."));
  }

  $("copies").hidden = copies.length === 0;
  const list = $("copies-list");
  list.replaceChildren();
  for (const c of copies) {
    const li = el("li");
    const a = el("a", c.name);
    a.href = c.html_url;
    a.target = "_blank";
    li.append(a, el("span", c.course ? ` (${c.course})` : "", "hint"));
    list.append(li);
  }
  await renderCopyPicker(copies);
}

// On an open Google doc, offer "This is my copy for …" when a Google Assignment is waiting.
async function renderCopyPicker(copies) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("copy").hidden = true;
  if (!tab?.url || !DOC.test(tab.url) || copies.length === 0) return;
  const select = $("copy-for");
  select.replaceChildren(...copies.map((c) => {
    const o = el("option", c.course ? `${c.name} (${c.course})` : c.name);
    o.value = String(c.assignment_id);
    return o;
  }));
  $("copy").hidden = false;
  $("save-copy").onclick = async () => {
    $("save-copy").disabled = true;
    $("copy-result").textContent = "Saving…";
    const r = await chrome.runtime.sendMessage({
      type: "saveCopy", tabId: tab.id, url: tab.url, title: tab.title || "", assignmentId: Number(select.value),
    });
    $("copy-result").textContent = r?.saved ? "Saved." : (r?.error || REASONS[r?.reason] || "Couldn't save it.");
    $("copy-result").className = r?.saved ? "ok" : "warn";
    $("save-copy").disabled = false;
    if (r?.saved) render();
  };
}

$("run").addEventListener("click", async () => {
  $("run").disabled = true;
  $("run").textContent = "Fetching…";
  await chrome.runtime.sendMessage({ type: "run" });
  $("run").disabled = false;
  $("run").textContent = "Fetch now";
  render();
});

$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());

render();

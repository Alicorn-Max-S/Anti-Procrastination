// Pure helpers shared by the service worker, popup and options page (and lib.test.mjs).
// Nothing here touches chrome.* so it runs under `node --test` too.

export const MAX_BYTES = 100_000_000; // the server's cap (app/browser_files.py)

// Only these hosts are ever fetched with the student's Google sign-in. Downloads may redirect
// to *.googleusercontent.com, which is checked on the final URL.
const FETCH_HOSTS = new Set(["docs.google.com", "drive.google.com", "drive.usercontent.google.com"]);

// Why a fetch failed: the server's REASONS keys, with the popup's wording.
export const REASONS = {
  signin: "Chrome isn't signed in to your school Google account",
  no_access: "your school Google account can't open it",
  restricted: "the teacher turned off downloading",
  too_big: "over 100 MB",
  not_found: "deleted, or the link is wrong",
  not_a_file: "Google sent a web page instead of the file",
  network: "couldn't reach Google",
  error: "the download failed",
};

const CANVAS_ASSIGNMENT = /\/courses\/\d+\/assignments\/\d+/;

export function isFetchable(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" && FETCH_HOSTS.has(u.hostname);
  } catch {
    return false;
  }
}

function isGoogleHost(hostname) {
  return hostname === "google.com" || hostname.endsWith(".google.com") || hostname.endsWith(".googleusercontent.com");
}

// Pick the school account when several Google accounts are signed in.
export function withAuthUser(url, email) {
  if (!email) return url;
  const u = new URL(url);
  u.searchParams.set("authuser", email);
  return u.toString();
}

export function isCanvasAssignment(url) {
  return typeof url === "string" && CANVAS_ASSIGNMENT.test(url);
}

// What a Google response means, judged from its headers before the body is read.
// "denied" and "page" aren't final: the caller loads the doc's normal page to tell
// "no access" from "downloading turned off".
export function classify({ status, finalUrl, contentType = "", length = 0 }) {
  let host = "";
  try {
    const u = new URL(finalUrl);
    host = u.hostname;
    if (host === "accounts.google.com" || u.pathname.includes("ServiceLogin")) return { ok: false, reason: "signin" };
  } catch {
    return { ok: false, reason: "error", detail: "no final URL" };
  }
  if (!isGoogleHost(host)) return { ok: false, reason: "error", detail: `redirected off Google (${host})` };
  if (status === 404) return { ok: false, reason: "denied", detail: "HTTP 404" };
  if (status === 401 || status === 403) return { ok: false, reason: "denied", detail: `HTTP ${status}` };
  if (status !== 200) return { ok: false, reason: "error", detail: `HTTP ${status}` };
  if (contentType.toLowerCase().startsWith("text/html")) return { ok: false, reason: "page", detail: "HTML instead of a file" };
  if (length > MAX_BYTES) return { ok: false, reason: "too_big", detail: `${Math.round(length / 1e6)} MB` };
  return { ok: true };
}

// After "denied" or "page": what loading the doc's normal page says about it.
export function classifyView({ status, finalUrl, text = "" }) {
  let u;
  try {
    u = new URL(finalUrl);
  } catch {
    return "error";
  }
  if (u.hostname === "accounts.google.com" || u.pathname.includes("ServiceLogin")) return "signin";
  if (status === 404) return "not_found";
  if (status !== 200) return "no_access";
  if (/You need access|Request access|You need permission|Ask for access/i.test(text)) return "no_access";
  return "restricted"; // the page opens for this account, but the export was refused
}

// "Lab 4 handout.pdf" from a Content-Disposition header, or "".
export function filenameFrom(disposition) {
  if (!disposition) return "";
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through to the plain name */
    }
  }
  const plain = disposition.match(/filename="([^"]+)"/i) || disposition.match(/filename=([^;]+)/i);
  return plain ? plain[1].trim() : "";
}

// The server's base URL, tidied, or null if it isn't one.
export function normalizeServer(text) {
  try {
    const u = new URL(String(text).trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.origin + u.pathname.replace(/\/+$/, "");
  } catch {
    return null;
  }
}

// The optional host permission a server or Canvas URL needs, or null when the manifest
// already covers it (localhost) or it isn't https.
export function permissionPattern(url) {
  try {
    const u = new URL(url);
    if (u.hostname === "localhost" || u.hostname === "127.0.0.1") return null;
    return u.protocol === "https:" ? `https://${u.hostname}/*` : null;
  } catch {
    return null;
  }
}

// Bytes <-> base64 for extension messages, which are JSON only.
export function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

export function fromBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

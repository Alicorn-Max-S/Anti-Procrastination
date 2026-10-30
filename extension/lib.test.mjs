// Tests for lib.js: run with `node --test extension/lib.test.mjs` (Node 18+). Chrome never loads this file.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_BYTES, classify, classifyView, filenameFrom, fromBase64, isCanvasAssignment, isFetchable, normalizeServer,
  permissionPattern, toBase64, withAuthUser,
} from "./lib.js";

const EXPORT = "https://docs.google.com/document/d/ID/export?format=pdf";

test("only Google download hosts are fetchable", () => {
  assert.ok(isFetchable(EXPORT));
  assert.ok(isFetchable("https://drive.usercontent.google.com/download?id=ID&export=download&confirm=t"));
  assert.ok(!isFetchable("http://docs.google.com/document/d/ID/export"));
  assert.ok(!isFetchable("https://docs.google.com.evil.test/x"));
  assert.ok(!isFetchable("https://accounts.google.com/"));
  assert.ok(!isFetchable("not a url"));
});

test("authuser picks the school account", () => {
  assert.equal(withAuthUser(EXPORT, ""), EXPORT);
  assert.equal(withAuthUser(EXPORT, "me@school.org"), `${EXPORT}&authuser=me%40school.org`);
});

test("classify reads Google's answer from the headers", () => {
  const ok = { status: 200, finalUrl: EXPORT, contentType: "application/pdf", length: 1000 };
  assert.deepEqual(classify(ok), { ok: true });
  assert.equal(classify({ ...ok, finalUrl: "https://accounts.google.com/v3/signin/identifier?continue=x" }).reason, "signin");
  assert.equal(classify({ ...ok, finalUrl: "https://www.google.com/a/x/ServiceLogin?x" }).reason, "signin");
  assert.equal(classify({ ...ok, contentType: "text/html; charset=utf-8" }).reason, "page");
  assert.equal(classify({ ...ok, status: 403 }).reason, "denied");
  assert.equal(classify({ ...ok, status: 404 }).reason, "denied");
  assert.equal(classify({ ...ok, status: 500 }).reason, "error");
  assert.equal(classify({ ...ok, length: MAX_BYTES + 1 }).reason, "too_big");
  assert.equal(classify({ ...ok, finalUrl: "https://doc-0s-docs.googleusercontent.com/docs/securesc/x" }).ok, true);
  assert.equal(classify({ ...ok, finalUrl: "https://evil.test/file.pdf" }).reason, "error");
});

test("classifyView tells no access from downloading turned off", () => {
  const view = "https://docs.google.com/document/d/ID/edit";
  assert.equal(classifyView({ status: 200, finalUrl: view, text: "<html>doc editor</html>" }), "restricted");
  assert.equal(classifyView({ status: 200, finalUrl: view, text: "You need access. Request access" }), "no_access");
  assert.equal(classifyView({ status: 401, finalUrl: view }), "no_access");
  assert.equal(classifyView({ status: 404, finalUrl: view }), "not_found");
  assert.equal(classifyView({ status: 200, finalUrl: "https://accounts.google.com/ServiceLogin" }), "signin");
});

test("file names come out of Content-Disposition", () => {
  assert.equal(filenameFrom(`attachment; filename="Lab 4.pdf"; filename*=UTF-8''Lab%204%20handout.pdf`), "Lab 4 handout.pdf");
  assert.equal(filenameFrom(`attachment; filename="Notes.pdf"`), "Notes.pdf");
  assert.equal(filenameFrom(""), "");
});

test("server URLs and the permissions they need", () => {
  assert.equal(normalizeServer(" http://localhost:8001/ "), "http://localhost:8001");
  assert.equal(normalizeServer("https://abc.trycloudflare.com"), "https://abc.trycloudflare.com");
  assert.equal(normalizeServer("ftp://x"), null);
  assert.equal(normalizeServer("localhost:8000"), null);
  assert.equal(permissionPattern("http://localhost:8001"), null);
  assert.equal(permissionPattern("https://abc.trycloudflare.com/x"), "https://abc.trycloudflare.com/*");
  assert.equal(permissionPattern("https://canvas.school.edu"), "https://canvas.school.edu/*");
});

test("Canvas assignment pages are recognised", () => {
  assert.ok(isCanvasAssignment("https://canvas.school.edu/courses/12/assignments/345"));
  assert.ok(isCanvasAssignment("https://canvas.school.edu/courses/12/assignments/345?module_item_id=9"));
  assert.ok(!isCanvasAssignment("https://canvas.school.edu/courses/12/pages/intro"));
  assert.ok(!isCanvasAssignment(undefined));
});

test("bytes survive the base64 trip", () => {
  const bytes = new Uint8Array(200_000).map((_, i) => i % 256);
  assert.deepEqual(fromBase64(toBase64(bytes.buffer)), bytes);
});

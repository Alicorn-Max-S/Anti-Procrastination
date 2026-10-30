# Chrome extension for school-only Google files: research and design

Most assignment instructions sit in Google Docs, Slides and Drive files shared only inside the school's Google domain. The server can't open them without a Google sign-in. Max can't grant OAuth on his school account, and we won't hand the server his Google cookies. So a Chrome extension running in his normal, signed-in Chrome fetches the files and uploads them to Start Helper.

Researched Fri Oct 30 2026. Things I couldn't verify without a school account are marked **unverified**. Max's first run settles them (checklist in `README.md`).

## 1. Credentialed fetches in Manifest V3

**Where to fetch from.** An extension service worker that has host permission for a site can make cross-origin requests to it, and CORS doesn't apply. Content scripts are different: Chrome's docs say "Cross-origin requests are always treated as such in content scripts", so they follow the page's rules ([network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)). Fetching in the service worker is the main path.

**Cookies.** With host permission, the browser's own cookies for that site go out on service-worker fetches. They aren't visible in the worker's DevTools, and the docs don't spell it out ([chromium-extensions thread, Jan 2023](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/RMUtNEhR0R8)). The extension never reads, copies or stores a cookie: it doesn't ask for the `cookies` permission, and Chrome attaches them itself.

**SameSite.** Chrome counts a request as same-site when the initiator is an extension that has access to the URL. That is the first condition in Chromium's own test ([extension_samesite_cookies_browsertest.cc](https://chromium.googlesource.com/chromium/src/+/0c45f14dd57a8edd6b43e5b51a1a08611fdb4aa6/chrome/browser/extensions/extension_samesite_cookies_browsertest.cc)). The docs say requests from an extension to a third party "are treated as same-site if the extension has host permissions", so `SameSite=Strict` cookies are sent ([storage and cookies](https://developer.chrome.com/docs/extensions/develop/concepts/storage-and-cookies)).

**Third-party-cookie blocking.** The same page says this same-site exception doesn't apply when third-party cookies are blocked. It also says third-party cookies are never blocked in frames whose top-level page is an extension page. A Chrome DevRel engineer described the blocked-3PC case as unsettled in Jan 2024 ([thread](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/ZF9-7s5BHSI)). Default Chrome doesn't block third-party cookies, but a school policy (`BlockThirdPartyCookies`) could. **Unverified for Max's profile.** The design has a fallback for it (below).

**`credentials: "include"`.** Set on every Google fetch. It is a no-op for a privileged request, but it states the intent and makes the fallback below behave the same way.

**Fallback: same-origin fetch from the open doc.** A content script running on `docs.google.com` that fetches `docs.google.com/.../export` makes a same-origin request with first-party cookies, so cookie policy can't break it. When the service-worker fetch fails for a doc Max has open, the background asks that tab's content script to fetch the export. Bytes come back base64-encoded, because Chrome's extension messaging is JSON-only and capped at 64 MiB ([messaging](https://developer.chrome.com/docs/extensions/develop/concepts/messaging)).

**Host permissions (least privilege).**

| Pattern | Why |
| --- | --- |
| `https://docs.google.com/*` | Docs/Slides/Sheets export; the content script that notices opened docs |
| `https://drive.google.com/*` | Drive file pages, and the content script there |
| `https://drive.usercontent.google.com/*` | Where Drive downloads are served from |
| `https://*.googleusercontent.com/*` | Export and download redirects can land on `doc-…-docs.googleusercontent.com` |
| `http://localhost/*`, `http://127.0.0.1/*` | The Start Helper server. A pattern without a port matches every port ([match patterns](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns)) |
| optional `https://*/*` | Requested at runtime for only two origins: a tunnel server URL, and Max's Canvas site (to spot Google Assignments copies). `permissions.request` needs a click ([permissions](https://developer.chrome.com/docs/extensions/reference/api/permissions)) |

No `cookies`, `tabs`, `downloads`, `scripting`, `webRequest` or `<all_urls>`.

## 2. Export endpoints

Without a sign-in, `…/export` redirects to Google's login page ([a scraper's notes](https://docs.rs/crate/nab/0.8.2/source/src/site/google/mod.rs)). With one, it returns the file.

| File | Canvas link shapes seen in `test_data/` | Fetch | Returns |
| --- | --- | --- | --- |
| Doc | `/document/d/ID/edit`, `/preview`, `/edit?usp=sharing`, `/u/N/d/ID` | `https://docs.google.com/document/d/ID/export?format=pdf` | PDF |
| Slides | `/presentation/d/ID/edit` | `https://docs.google.com/presentation/d/ID/export/pdf` | PDF |
| Sheets | `/spreadsheets/d/ID/edit` | `https://docs.google.com/spreadsheets/d/ID/export?format=pdf` | PDF of every sheet |
| Drive file | `drive.google.com/file/d/ID/view`, `/preview`, `open?id=ID` | `https://drive.usercontent.google.com/download?id=ID&export=download&confirm=t` | The file as uploaded (PDF, DOCX, image …) |
| Form | `/forms/d/…/viewform` | not fetched | Not a document |
| Published doc | `/document/d/e/2PACX…/pub` | not fetched | Already public, so the server reads it |

**Drive's virus-scan page.** For big files, `drive.google.com/uc?export=download` returns an HTML "can't scan for viruses" page instead of the file. Download tools skip it with `confirm=t` on `drive.usercontent.google.com/download` ([torchvision](https://github.com/pytorch/vision/pull/5645/files), [modde-sources](https://docs.rs/crate/modde-sources/0.2.1/source/src/gdrive/mod.rs)). Start Helper caps files at 25 MB anyway. The extension checks `Content-Length`, aborts anything bigger, and reports `too_big`.

**Which account.** With several Google accounts signed in, Google uses the default one (`/u/0`), and exports can fail with 403 ([U-M IT](https://teamdynamix.umich.edu/TDClient/30/Portal/KB/PrintArticle?ID=15355)). The options page takes an optional school email, and the extension adds `authuser=<email>` to export URLs. **Unverified**: Google honours `authuser` widely, but not documented for export URLs. If it fails, the extension retries without it, and the README says to sign out of other accounts or use a separate profile.

**Telling failures apart.** Without a sign-in, Google answers a school-only file with a 302 to `accounts.google.com/ServiceLogin` (checked with curl on Max's two AP Calc Drive files). The extension has no access to `accounts.google.com`, and shouldn't, so `fetch()` can't follow that redirect: it throws `TypeError: Failed to fetch`, exactly like a dropped network. The extension tells them apart by repeating the request with `redirect: "manual"`. An `opaqueredirect` response means "sent to sign-in"; another throw means the network really is down. HTML where a file should be also means not signed in or wrong account. For a 401/403/404, the extension loads the doc's normal view page once to separate "can view, but downloading is off" (`restricted`) from "no access" (`no_access`). Opening the view page is exactly what the "Open this doc" button would do.

## 3. Files with download/print/copy turned off

The owner can turn off download, print and copy for viewers and commenters. One switch covers all three, and it removes the download entry points in Drive, Docs, Sheets and Slides ([Google, 2015](https://workspaceupdates.googleblog.com/2015/07/disable-downloading-printing-and.html)). Teachers' files are usually shared with download on, which is the default.

The known ways around it are the Docs `/mobilebasic` HTML view and scraping the rendered page ([example tutorial](https://dev.to/remoteseoconsultant/how-to-copy-a-view-only-google-doc-2593)).

**Max's decision (Oct 30): read them anyway.** In his classes the setting is usually a teacher's mistake, not a choice. So when the export is refused and the view page says the doc opens (`restricted`), the extension fetches the doc's server-rendered page view and sends the HTML with `via=page`. The server keeps only the text, as a `.txt` that starts with "(Read from the page view: the teacher turned off downloading.)", and records `via = "page"` on the row (`browser_files.stored(url).via`), so the card can say where the text came from. The reader reads it like any other text file.

| Kind | Page view | Checked on a public file, Oct 30 |
| --- | --- | --- |
| Docs | `/document/d/ID/mobilebasic` | Plain HTML, full text in paragraphs and tables. |
| Slides | `/presentation/d/ID/htmlpresent` | Each slide is an SVG with a `<text>` per line; all slides' text is in the page. |
| Sheets | `/spreadsheets/d/ID/htmlview/sheet?headers=false&gid=0` | Plain `/htmlview` is now drawn by JavaScript and has no cells in the HTML. `/htmlview/sheet` has them, first tab only. |
| Drive files (PDF, Word, images) | none | Still reported as `restricted`; Max opens them. |

What isn't verified: no download-disabled file was available, so it's untested whether Google serves these views for one. The tutorial above says `/mobilebasic` works for view-only Docs. If a page view fails, or has under 40 characters of text, the file stays `restricted` and the card says "Open this doc", as before. When the doc is open in a tab and the service worker's request lacks the sign-in, the content script fetches the same page view from inside the doc (same-origin, like the export fallback in section 1). The same rules hold as for downloads: only Canvas-listed links (or a matched Google Assignments copy), only `docs.google.com`, only to the configured server, no cookies read.

## 4. Google Assignments

When a teacher attaches a file as "make a copy for each student", each student gets their own copy to edit. The copy is labelled with the student's name and saved in the student's Drive under `Assignments/<course>` ([Google](https://support.google.com/edu/assignments/answer/9069281), [Texas A&M](https://lms.tamu.edu/Menu/LTIs/Google-Assignments), [Brown](https://ithelp.brown.edu/kb/articles/using-google-assignments-in-canvas)). Guides disagree on whether the copy is made when the teacher creates the assignment or when the student first opens it. Either way its file id appears nowhere in Canvas, so the server can't ask for it.

**How the extension recognizes the copy without depending on Google's markup:**

1. A content script on `docs.google.com` / `drive.google.com` file pages tells the background "doc ID was opened".
2. The background walks the tab's `openerTabId` chain (up to 3 tabs). A Google Assignments file opens in a new tab from the Canvas assignment page, or from a tool tab that page opened. If one of those tabs is a Canvas URL `/courses/C/assignments/A`, its URL goes to the server. The tab URL is visible only with host permission for the Canvas origin, which is why Canvas is an optional permission, granted on the options page.
3. The server decides. If assignment A uses Google Assignments (`external_tool_url` on assignments.google.com), the doc isn't one of the teacher's linked files, and no copy is stored yet, it answers "wanted, for assignment A". The extension then fetches it like any other file.
4. Fallback: in the popup, on any open doc, Max can pick "This is my copy for …" from the list of Google Assignments assignments still missing a copy.

It captures the copy once, when it's first seen, so the card gets the template instructions. It never re-uploads the student's later work.

## 5. School-managed Chrome

- `ExtensionInstallBlocklist` set to `*` blocks every extension not allowlisted, and "All unpacked extensions are blocked" ([policy definition](https://chromium.googlesource.com/chromium/src/+/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionInstallBlocklist.yaml)).
- `ExtensionDeveloperModeSettings` (Chrome 128+) set to 1 hides Developer mode on `chrome://extensions`. Unset, Developer mode is allowed unless `DeveloperToolsAvailability` is 2 ([definition](https://chromium.googlesource.com/chromium/src/+/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionDeveloperModeSettings.yaml)).
- `ExtensionAllowedTypes` limits installs to listed types (`extension`, `theme`, `user_script`, `hosted_app`, `legacy_packaged_app`, `platform_app`) ([definition](https://chromium.googlesource.com/chromium/src/+/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionAllowedTypes.yaml)). If set without `extension`, this extension is blocked.
- School user policies follow the school account into a Chrome profile that is signed in to Chrome with it. A profile where the school account is only signed in on the web normally doesn't get them, unless the school forces a separate managed profile.

**How Max checks:** `chrome://management` says whether the browser or profile is managed. `chrome://policy` lists every policy in force. `chrome://extensions` shows whether the Developer mode toggle can be turned on. If it's blocked, use a separate Chrome profile that isn't signed in to Chrome, and sign in to the school account only on the web there.

## 6. Talking to the server

| Option | Verdict |
| --- | --- |
| Extension polls the server (`chrome.alarms`, every 10 min, plus on startup and a "Fetch now" button) | **Chosen.** Works with no Start Helper tab open, and keeps the logic on the server, where pytest covers it. |
| Page talks to the extension (`externally_connectable`) | Not needed. The match list must name exact sites, so every tunnel URL would need a manifest edit. |
| CORS for `chrome-extension://` | Not needed. With host permission for the server origin, the service worker bypasses CORS, and the server sends no CORS headers, so other web pages still can't read its responses. |

**Server URL** is set in options (default `http://localhost:8000`). A non-localhost URL, like a Cloudflare tunnel, triggers a permission request for that single origin.

**Shared key.** Every `/api/ext/*` call needs an `X-Start-Helper-Key` header. The server makes a random key on first use (kept in its DB) and prints it with `python -m app.browser_files key`. Max pastes it into options. A custom header also forces a CORS preflight, so a random web page can't post files to the server (the server answers no preflight).

## 7. Least privilege and account safety

- No `cookies` permission. Cookies are never read, exported, logged or sent anywhere. Chrome attaches them to Google requests itself.
- The extension fetches only what the server lists, or a doc Max opened that the server says it wants. Before fetching, it checks the URL is `https:` on `docs.google.com`, `drive.google.com` or `drive.usercontent.google.com`. Redirects stay inside the Google host permissions.
- It only ever uploads to the configured server URL, with the key.
- The server lists only Google links found in Canvas assignment descriptions for upcoming work, plus Google Assignments copies matched as above.
- Uploads are checked server-side: 100 MB max, real file signature (PDF, Office ZIP, image). An HTML sign-in page is rejected even if the extension mislabels it. HTML is accepted only as a page view (`via=page`) of a Docs, Slides or Sheets link, and only its text is kept.

## 8. Checked in a real Chrome (Oct 30)

Branded Chrome no longer honours `--load-extension` (silently ignored in 156, even with `--disable-features=DisableLoadExtensionCommandLineSwitch`). So the check loaded the extension through DevTools' `Extensions.loadUnpacked` (puppeteer-core `browser.installExtension`, over `--remote-debugging-pipe`), in a throwaway headless Chrome 156 profile with no Google sign-in. The server was a copy of Max's DB on :8005.

- It loads as MV3, and the service worker starts.
- The options page's "Save and test" reached `http://localhost:8005` from the service worker. Local Network Access didn't get in the way of an extension with host permission for localhost.
- Opening a public Google Doc linked from a test assignment ran the whole chain: content script, then `/api/ext/opened`, then the export fetch in the service worker, then the upload. The tab's badge showed ✓, and the server stored a real PDF.
- The polling run saved 4 PDFs: the Docs links in Max's upcoming work are actually exportable without sign-in. The 2 Drive files hit the sign-in redirect and were reported as `signin`, which is the right answer for an unsigned profile.

- Page views (later the same day): the service worker fetched a public Doc's `/mobilebasic` (200, `text/html`, still on `docs.google.com`) and posted it with `via=page`. The server stored 5.8k characters of clean text as `google-<id>.txt`, marked `via = "page"`.

What only Max's signed-in run can show: the cookies on service-worker requests, `authuser=<email>`, the `restricted`/`no_access` split, and whether Google serves page views of a download-disabled file. Those are the checklist in the final report.

## 9. Design summary

```
Canvas (token) ──> server: assignment descriptions ──> Google links it can't open
                                                           │
Chrome extension (Max's signed-in browser)                 │
  every 10 min: GET  /api/ext/needed  <────────────────────┘
  for each: fetch export with Max's sign-in ── ok ──> POST /api/ext/files (bytes)
                                          ├─ downloading off ─> page view ─> POST /api/ext/files (HTML, via=page)
                                          └─ fails ─> POST /api/ext/failed (reason)
  doc opened: POST /api/ext/opened {url, opener Canvas URL} -> wanted? -> fetch + upload
server: stores the file under its Google id, the reader uses it on the next card build,
        and the card's "Open this doc" button shows the reason until then.
```

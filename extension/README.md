# Start Helper files (Chrome extension)

Most assignments link to Google Docs, Slides and Drive files shared only inside your school's Google domain. The Start Helper server can't open those, because it isn't signed in to Google. This extension runs in your normal Chrome, where you're already signed in to your school account. It downloads those files and sends them to your Start Helper server, and cards get rebuilt with what's in them.

- It only fetches Google files that your upcoming Canvas assignments link to (the server gives it the list), plus your own Google Assignments copy.
- It only sends files to the Start Helper server you set, with your key.
- It never reads, copies or stores your Google cookies. Chrome adds your sign-in to Google requests by itself.
- When a teacher turned off downloading on a Doc, Slides or Sheet, it reads the doc's page view instead and sends that text. The card's source says it was read from the page view. Drive files (PDFs, Word files) with downloading off can't be read that way: the card says "the teacher turned off downloading", and you open them yourself.

`RESEARCH.md` explains why it works this way, with sources.

## Before you start: can this Chrome load it?

Use the Chrome profile where you're signed in to your school Google account.

1. Go to `chrome://extensions`. If the **Developer mode** switch (top right) won't turn on, your school blocks unpacked extensions in this profile. Make a new Chrome profile (profile icon → **Add** → **Continue without an account**), sign in to your school account on google.com in that profile only, and use that profile instead.
2. Optional: `chrome://policy` lists what your school controls. Look for `ExtensionInstallBlocklist`, `ExtensionDeveloperModeSettings` or `BlockThirdPartyCookies`.

## Install (once)

1. Start Start Helper as usual (for example `uvicorn app.main:app --reload --port 8001`).
2. In a second terminal, in the Start Helper folder with the venv active, print the extension key:
   ```
   python -m app.browser_files key
   ```
   It prints a line of random letters. Copy it. It's the same every time you run this.
3. In Chrome, go to `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick the `extension` folder inside the Start Helper folder.
4. A **Start Helper files** settings tab opens (if it doesn't: click the extension's **Details** → **Extension options**). Fill in:
   - **Start Helper server**: the address you open Start Helper at, e.g. `http://localhost:8001`.
   - **Key**: what step 2 printed.
   - **School Google account**: your school email, only if this Chrome is signed in to more than one Google account.
5. Click **Save and test**. You should see **Connected. Saved N file(s)…** within a minute or so. A "couldn't be fetched" count is fine; the popup says why for each one.
6. In the box under it, click **Allow Canvas tabs** and accept. This lets the extension spot your Google Assignments copies (below). It only reads the address of Canvas tabs.
7. Pin it: puzzle-piece icon in the toolbar → pin **Start Helper files**.

## Using it

- **Nothing to do day to day.** Every 10 minutes it asks the server what's missing and fetches it. **Fetch now** in the popup does it immediately.
- **Cards update by themselves.** A file that arrives marks its card for one rebuild on the next sync (within `SYNC_MINUTES`, 5 by default). Fetching the same file again never triggers another rebuild.
- **The popup** (toolbar icon) shows the last run: what it saved, and why anything failed.
- **Files it can't fetch** show under "Couldn't open" on the card, with the reason. Open the doc from there. The extension saves it as soon as the doc opens: the toolbar icon shows ✓ on that tab, or ! with the reason in its tooltip.
- **Google Assignments.** Your copy of the doc only exists once you open the assignment from Canvas, so the server can't ask for it. Open the assignment on Canvas, then open your file from it. The extension sees the doc came from that Canvas assignment and saves your copy once (✓ on the tab). It never re-saves your later work. If it didn't catch it: with the doc open, click the toolbar icon → pick the assignment under **Is this doc your copy…** → **This is my copy**. The popup lists the assignments still waiting for a copy.
- **Using a tunnel** (`https://….trycloudflare.com`): put that URL in settings and click **Save and test**. Chrome asks to allow that one site.

## If something's wrong

| You see | Do this |
| --- | --- |
| "Chrome isn't signed in to your school Google account" | Sign in to your school account in this Chrome profile. With several accounts, add your school email in settings. |
| "your school Google account can't open it" | The teacher shared it with someone else. Ask them, or open it from Canvas. |
| "the teacher turned off downloading" | A Drive file, or a doc whose page view had no text. Open it and read it there. |
| "Couldn't reach http://localhost:…" | Start Helper isn't running, or the port in settings is wrong. |
| "The server says the key is wrong" | Run `python -m app.browser_files key` again in the folder the server runs from, and paste that. |
| You changed files in `extension/` | Click the reload arrow on the extension's card in `chrome://extensions`. |

## For developers

- `background.js`: service worker. Polls `GET /api/ext/needed`, fetches with `credentials: "include"`, and posts to `/api/ext/files` or `/api/ext/failed`. When a doc is opened, it asks `/api/ext/opened` whether to save it.
- `content.js`: on Docs/Slides/Sheets/Drive file pages. Reports the opened doc, and can fetch the export same-origin when cookie settings keep the sign-in off the service worker's request.
- `lib.js`: pure helpers. Tests: `node --test extension/lib.test.mjs`.
- Server side: `app/browser_files.py`, `/api/ext/*` in `app/main.py`, tests in `tests/test_browser_files.py`.

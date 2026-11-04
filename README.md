# warmstart

**Less staring. More starting.** We do the warm-up and wake-up, so you can get to work.

Planners help you plan; this helps you start. It turns every Canvas assignment into a prep card with the first step already decided, pings you at start time, and re-plans when you miss it. When a quiz or test is coming, the card is a short practice set built from your graded tests, aimed at the topics and question types you miss.

Built for the Meta AI developer hackathon. See `CLAUDE.md` for the plan and `docs/brand.md` for the name, copy, colors and logo.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy .env.example .env          # then fill in your tokens (Windows)
python -m app.canvas            # check the Canvas token
uvicorn app.main:app --reload
```

Open http://localhost:8000. With a Canvas token set, the server syncs on start and every `SYNC_MINUTES` after that; the **Sync** button runs one now. One page, three tabs: Assignments (cards), Week (blocks), Dashboard (tests, stats, devices).

No Canvas token or model key yet? Load made-up sample data and everything else works (cards fall back to templates without a model key):

```
python -m app.demo            # four fake courses, graded history, cards, graded-test rows
python -m app.demo --usage    # the same, plus three made-up weeks of pings and starts
python -m app.demo --clear    # remove it again
```

Assignments linking Google files shared only inside your school? The server can't open those without your sign-in, so a small Chrome extension fetches them in your signed-in Chrome. Setup steps are in `extension/README.md`.

## Running

Needs Python 3.11 or newer. Day to day, from the repo root with the venv active:

```
.venv\Scripts\activate          # Windows; every terminal you run warmstart from
uvicorn app.main:app --reload   # http://localhost:8000 (add --port 8001 if 8000 is taken)
```

Useful knobs in `.env`: `BACKGROUND=0` stops the loop (then syncs and pings only happen when you trigger them), `SYNC_MINUTES` how often it syncs, `CARD_HORIZON_DAYS` how far ahead cards build, `MAX_CARDS_PER_SYNC` capping model cards per sync, `TIMEZONE` (blank means this computer's) and `WORK_START_HOUR` / `WORK_END_HOUR` for the daily window. LibreOffice is optional: without it, Office attachments fall back to description text.

Handy one-shot commands (venv active, repo root):

```
python -m app.canvas            # check the Canvas token; lists each course with assignment counts
python -m app.sync              # sync Canvas -> SQLite once, without starting the server
python -m app.grader PATH [--course ID] [--fresh]   # read a graded-test photo or scan into the DB
python -m app.push              # print a VAPID key pair for .env
python -m app.browser_files key # print the key for the Chrome extension's options page
pytest                          # the test suite
```

System One tools (separate from the app; the Layout table says what each is for):

```
python -m decider               # decision API on 127.0.0.1:8090; needs llama-server running Muse Glimmer
python glimmer_check.py         # pre-flight for that llama-server (127.0.0.1:8080, --jinja, -np 1)
python -m labeling              # hand-labelling site on 127.0.0.1:8100
python -m eval.run calibrate [--approve]   # score labels, propose decision lines (needs llama-server)
python -m eval.run compare                 # run the setups; needs pip install -e ".[eval]" for the chart
```

## How it works

1. **Sync** (`sync.py`, `canvas.py`): courses, assignments, rubric criteria, and, for graded work, teacher comments and rubric scores.
2. **Prep cards** (`prep.py`): for each assignment due in the next `CARD_HORIZON_DAYS`, Muse Spark gets the description, rubric, links and attachments (file text from Canvas files, public Google files, pages and web articles; school-only Google files arrive through the extension; Office files converted to PDF if LibreOffice is installed, scans transcribed by the model) and returns a card through **structured output**. It can call two **tools**, `past_feedback` and `weak_spots`, to ground the "watch out" line in what your teachers actually said. Links it returns are kept only if they really came from Canvas. Each card opens as a prep doc in one of four templates (Document, Checklist, Dashboard, Cornell), in the course's color, with a Focus mode that shows one step at a time (`static/prepdoc.js`). Drag the divider between a prep doc's two sides (or focus it and use the arrow keys) to make either wider; the width is kept, and Enter or a double-click resets it. Quote marks are paired by context (`prep.smart_quotes`), since the brand font draws straight quotes the same on both sides. When the whole task is one small action with nothing to prep (a form, a signature), the model marks it quick, and the card is just that action and a link.
3. **Test cards** (`practice.py`): practice questions aimed at your weakest topic x question-type cells, each answer checked by an independent second solve (wording differences get a third judging call; questions the check can't confirm are dropped, or shown unchecked).
4. **Graded tests** (`grader.py`): a photo or scan of a marked test goes to the model with **vision**, one call per page, and comes back as sections (subtotals, bonus) and one row per question (answer, marks, X or check, teacher note, topic, type). Code checks the teacher's arithmetic (questions to section subtotals, sections to page totals and the written score) and flags the test for review when it doesn't add up. The course is detected from the test, and question types come from a list kept per course. The Dashboard shows each test's score and sections and the resulting grid (`student_model.py`). `python -m app.grader PATH [--course ID] [--fresh]` reads a file from the command line.
5. **The start loop** (`scheduler.py`, `loop.py`, `checkin.py`): each card gets a block inside your daily window, worked back from the due date. A test gets short practice sessions instead (three 20-minute ones for a medium test, one on each day before it), and a quick task one 10-minute block. At the block's start you get a ping; 15 minutes without starting marks it missed and asks when you'll start. Picking a time moves that block and shifts only the blocks in its way. Every ping, answer and start is saved in `pings`. The Dashboard's starting stats count them and show how late starts come on each weekday (`habits.py`), and once a weekday has a pattern, its pings say so: "You usually start 25 min late on Tuesdays."
6. **Change the plan in your own words** (`availability.py`): on the Week tab, say what's going on: "I can't work Tuesday after 5, soccer", "I'm free Saturday morning", "I have math tutoring at 1 and I'll do all my STEM work during it, put my humanities around it", "move the history doc to Saturday morning", "I finished the reading". Muse Spark reads it against a calendar and your open work (id, class, title, due date), so "Tuesday" lands on the right date and "STEM" on the right classes. It makes a sensible assumption instead of refusing (tutoring with no end time gets an hour) and says what it assumed. The page shows the result with every time editable; nothing changes until you say yes. What it can return:
   - **Time off / open time**: planned blocks in time you can't work move to the closest free working time; new open time gives late blocks another try.
   - **A work session** (tutoring, study hall): the session's time is shared among its work by how long each usually takes, those assignments' other planned blocks go, and other work keeps out of it like time off. One notification at its start; at its end, "What did you finish?" (on the page and as a push with a *Finished them all* button). Finished work is marked done, the rest gets a new block; each answer is saved in `pings`.
   - **Plans**: work put in the free time of a day or window you name ("around it" means that day, outside the session). Those blocks are pinned, so nothing reorders them.
   - **Done**: work you say is finished.

   A form takes a span directly and works without a model key. Blocks you're already on (pinged or started) stay and are reported. New blocks and every check-in time keep to the same spans. Removing a span leaves moved blocks where they went; removing a session that hasn't started gives its work back to the planner. Spans live in the `availability` table.
7. **Earliest due first** (`scheduler.due_order`): after planning, after a check-in answer and after any change above, a planned block for work due later never sits ahead of one due sooner. The two trade places when the swap fits in working time and before both due dates; the later one goes right after the sooner one where it can. Blocks you placed yourself (a picked time, "In 15 min", a plan, a session) never move, and a test's practice session only trades within its own day.
8. **Adding work by hand** (`added.py`): sync stays automatic, and **Add work** on the Assignments page adds what it leaves out, only when you ask: a Canvas assignment that's past due and never turned in, has no due date, or is due beyond the two-week list; any item from a class's Modules (page, file, link, discussion, quiz, read live from Canvas); or your own task, class optional, with instructions and up to five files (PDF, Office, images, text). Each gets a finish-by time (default the end of tomorrow) that the planner treats as its due date; the card shows Canvas's own date beside it. A template card shows at once and the model's card follows. Change the finish-by time or take it off the list from its prep doc. Added items live in the `added` table; Modules items and your own get negative ids, and your files sit under `data/files/own/`.

## Demo controls

The clock bar under the header sets a **simulated clock** (`clock.py`). Jumping to a block's start fires its ping immediately; **Real time** goes back. The simulated time survives a server restart.

## Notifications

- **Desktop:** press *Turn on notifications*; pings show while the page is open.
- **Phone (push):** needs HTTPS. Generate keys with `python -m app.push` and paste them into `.env` (or skip it: the server makes its own pair when you first turn notifications on), restart, expose the server (for example `cloudflared tunnel --url http://localhost:8000`), open that URL on the phone, add it to the home screen, and press *Turn on notifications*. iPhone needs iOS 16.4+ and the home-screen app.
- **Android: use Chrome.** Pings go out at high urgency, so Chrome wakes a sleeping phone for them. Firefox on Android gets them through Mozilla's push server, which doesn't pass the urgency on, so they wait until the phone is next opened. Dashboard → *Notifications and extension* lists each device that gets pings.

## Fixing things by hand

`/docs` is FastAPI's interactive API page. It's the quickest way to fix a misread test row (`PUT /api/tests/rows/{id}`) or poke an endpoint during the demo.

## Layout

| Path | What it is |
| --- | --- |
| `app/main.py` | FastAPI app: the API, the page, and the background loop |
| `app/loop.py` | Sync job and the tick that fires pings and marks misses |
| `app/canvas.py`, `app/sync.py` | Canvas client and Canvas -> SQLite sync |
| `app/meta_client.py` | Meta Responses API: structured output, files, tool calling |
| `app/prep.py`, `app/practice.py` | Homework prep cards and test practice sets |
| `app/reader.py`, `app/materials.py`, `app/reading.py`, `app/gdocs.py`, `app/web.py`, `app/convert.py` | File text for cards: Canvas/Google files, pages, web articles, unit excerpts, reading plans, Office conversion |
| `app/grader.py`, `app/student_model.py` | Graded-test extraction and the weakness grid |
| `app/scheduler.py`, `app/checkin.py`, `app/clock.py` | Blocks, check-in reflow, simulated clock |
| `app/habits.py` | How late starts come by weekday, from `pings` |
| `app/availability.py` | What you say about your time and work: reads it into spans, work sessions, plans and finished work, and moves the week to fit |
| `app/added.py` | Work added by hand: old or later Canvas assignments, Modules items, your own task and files |
| `app/push.py` | Web push (VAPID) |
| `app/db.py`, `app/models.py`, `app/config.py` | SQLite layer, shared types, settings |
| `app/progress.py`, `app/colors.py` | Live sync progress for the sync bar; per-course colors |
| `app/demo.py` | Fabricated demo data (no real names or grades) |
| `static/` | The single web page: `app.js` (page logic), `prepdoc.js` (the prep docs), `theme.js`, styles, PWA manifest, service worker, icons, and the logo SVGs in `static/brand/` |
| `extension/`, `app/browser_files.py` | Chrome extension that fetches school-only Google files with your sign-in, and the server side it talks to |
| `decider/` | System One decider: a separate FastAPI process (`python -m decider`, port 8090) that answers the multiple-choice decisions with local Muse Glimmer through llama-server. Not wired into the app yet |
| `decisions/fields.py` | The decision fields and option order, shared by the labelling page and the decider |
| `eval/` | System One eval harness (`python -m eval.run calibrate` / `compare`, spec Part C). Per-item records stay in `data/eval/`; only aggregates (`results.csv`, `summary.md`, `compare.png`) land in `eval/`. Needs `pip install -e ".[eval]"` for the chart |
| `labeling/` | Throwaway hand-labelling site (`python -m labeling`, port 8100): labels synced assignments into `data/labeling/labels.jsonl` for the eval |
| `test_data/` | Censored snapshot of real Canvas responses backing the tests; see its README for what was changed |
| `glimmer_check.py` | Pre-flight checks that a local Muse Glimmer over llama-server behaves the way the decider assumes |
| `tests/` | Tests, run with `pytest`; they never touch Canvas, the model, push, school Google files, web articles or llama-server |

Real school data (`.env`, the DB, attachments, test photos) lives under `data/` and friends, which are gitignored. The one committed exception is `test_data/`, a censored snapshot (see its README). This repo is public.

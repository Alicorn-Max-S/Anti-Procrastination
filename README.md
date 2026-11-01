# Start Helper

Planners help you plan; this helps you start. It turns every Canvas assignment into a prep card with the first step already decided, pings you at start time, and re-plans when you miss it. When a quiz or test is coming, the card is a short practice set built from your graded tests, aimed at the topics and question types you miss.

Built for the Meta AI developer hackathon. See `CLAUDE.md` for the plan.

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
cp .env.example .env            # then fill in your tokens
uvicorn app.main:app --reload
```

Open http://localhost:8000. With a Canvas token set, the server syncs on start and every `SYNC_MINUTES` after that; the **Sync** button runs one now.

No Canvas token or model key yet? Load made-up sample data and everything else works (cards fall back to templates without a model key):

```
python -m app.demo            # four fake courses, graded history, cards, graded-test rows
python -m app.demo --usage    # the same, plus three made-up weeks of pings and starts
python -m app.demo --clear    # remove it again
```

Assignments linking Google files shared only inside your school? The server can't open those without your sign-in, so a small Chrome extension fetches them in your signed-in Chrome. Setup steps are in `extension/README.md`.

## How it works

1. **Sync** (`sync.py`, `canvas.py`): courses, assignments, rubric criteria, and, for graded work, teacher comments and rubric scores.
2. **Prep cards** (`prep.py`): for each assignment due in the next `CARD_HORIZON_DAYS`, Muse Spark gets the description, rubric, links and attachments (DOCX/PPTX converted to PDF if LibreOffice is installed) and returns a card through **structured output**. It can call two **tools**, `past_feedback` and `weak_spots`, to ground the "watch out" line in what your teachers actually said. Links it returns are kept only if they really came from Canvas. Each card opens as a prep doc in one of four templates (Document, Checklist, Dashboard, Cornell), in the course's color, with a Focus mode that shows one step at a time (`static/prepdoc.js`). When the whole task is one small action with nothing to prep (a form, a signature), the model marks it quick, and the card is just that action and a link.
3. **Test cards** (`practice.py`): practice questions aimed at your weakest topic x question-type cells, each answer checked by an independent second solve.
4. **Graded tests** (`grader.py`): a photo or scan of a marked test goes to the model with **vision**, one call per page, and comes back as sections (subtotals, bonus) and one row per question (answer, marks, X or check, teacher note, topic, type). Code checks the teacher's arithmetic (questions to section subtotals, sections to page totals and the written score) and flags the test for review when it doesn't add up. The course is detected from the test, and question types come from a list kept per course. The Dashboard shows each test's score and sections and the resulting grid (`student_model.py`). `python -m app.grader FILE` reads a file from the command line.
5. **The start loop** (`scheduler.py`, `loop.py`, `checkin.py`): each card gets a block inside your daily window, worked back from the due date. A test gets short practice sessions instead (three 20-minute ones for a medium test, one on each day before it), and a quick task one 10-minute block. At the block's start you get a ping; 15 minutes without starting marks it missed and asks when you'll start. Picking a time moves that block and shifts only the blocks in its way. Every ping, answer and start is saved in `pings`. The Dashboard's starting stats count them and show how late starts come on each weekday (`habits.py`), and once a weekday has a pattern, its pings say so: "You usually start 25 min late on Tuesdays."

## Demo controls

The clock bar under the header sets a **simulated clock** (`clock.py`). Jumping to a block's start fires its ping immediately; **Real time** goes back. The simulated time survives a server restart.

## Notifications

- **Desktop:** press *Turn on notifications*; pings show while the page is open.
- **Phone (push):** needs HTTPS. Generate keys with `python -m app.push`, paste them into `.env`, restart, expose the server (for example `cloudflared tunnel --url http://localhost:8000`), open that URL on the phone, add it to the home screen, and press *Turn on notifications*. iPhone needs iOS 16.4+ and the home-screen app.
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
| `app/grader.py`, `app/student_model.py` | Graded-test extraction and the weakness grid |
| `app/scheduler.py`, `app/checkin.py`, `app/clock.py` | Blocks, check-in reflow, simulated clock |
| `app/habits.py` | How late starts come by weekday, from `pings` |
| `app/push.py` | Web push (VAPID) |
| `app/db.py`, `app/models.py`, `app/config.py` | SQLite layer, shared types, settings |
| `app/demo.py` | Fabricated demo data (no real names or grades) |
| `static/` | The single web page (`prepdoc.js` draws the prep docs), PWA manifest, service worker, icons |
| `extension/`, `app/browser_files.py` | Chrome extension that fetches school-only Google files with your sign-in, and the server side it talks to |
| `decider/` | System One decider: a separate FastAPI process (`python -m decider`, port 8090) that answers the multiple-choice decisions with local Muse Glimmer through llama-server. Not wired into the app yet |
| `decisions/fields.py` | The decision fields and option order, shared by the labelling page and the decider |
| `tests/` | Tests, run with `pytest`; they never touch Canvas, the model, push or llama-server |

Real school data (the DB, attachments, test photos) lives under `data/`, which is gitignored. This repo is public.

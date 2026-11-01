# Start Helper

Planners help you plan; this helps you start. Start Helper turns every Canvas assignment into a prep card with the first step already decided, pings the student at start time, and re-plans when they miss it. When a quiz or test is coming, the card is a short practice set built from the student's uploaded graded tests, aimed at the topics and question types they miss.

One student model, built from Canvas comments, rubric scores and graded tests, feeds every card's "watch out for" line. That is what makes this one tool instead of a planner with a study app attached.

Built solo for the Meta AI developer hackathon. `README.md` has setup and the module layout; this file holds the plan and the rules.

## Commands

Windows, from the repo root, venv active.

```
pip install -e ".[dev]"          # once
uvicorn app.main:app --reload    # http://localhost:8000
pytest                           # never touches Canvas, the model or push
python -m app.demo               # fabricated sample data; --clear removes it
python -m app.canvas             # check the Canvas token
```

## What must work

The homework card, the test card, and the start-time ping with check-in and reflow never get cut. They are the product. Prep cards change shape by assignment type.

## Scope tiers

| Tier | What's in it |
| --- | --- |
| Core | Canvas sync with a personal token. Homework prep card (steps, links, first step). Test prep card from graded tests (topic by question type, practice in weak formats). Start-time ping plus check-in and reflow, with the simulated clock. One web page holding the cards. |
| Target | "Watch out for" lines on homework cards. Android push. Per-type card templates (lab, reading, problem set, test). Dashboard view. Second-solve answer check. |
| Stretch | Nothing here is scheduled. iPhone push. Semester findings and dry run on Muse Wonder. Quiz results updating the model. A "you usually start 25 min late on Tuesdays" stat. Meta AI Connector, if admitted. |
| Cut | Tutoring mode, learned start-time model, Muse Glimmer, Voice Transcribe, SAM, Muse Image. |

### Cut ladder

When behind, drop in this order. Rungs 1 to 5 are already cut.

1. Computer-use setup
2. Chrome extension ingest
3. App range in the demo (Notion, Google Docs copy link, printable sheet, tablet)
4. iPhone push
5. Classmate pilot
6. Dry-run pass
7. Semester findings with quote check
8. Quiz results feeding the model
9. Dashboard (fall back to the plain topic-by-type table)
10. Card templates beyond homework and test
11. Second-solve check
12. Android push (desktop notifications only)

## Schedule and freeze

- **MVP freeze: Sat Oct 31, 10 PM.** It can move earlier, never later. After it there are no new features, only fixes to bugs that show on camera. Hackathon deadline is Wed Nov 4, 6 PM PT.
- Build targets, not status. Thu Oct 29: start loop (blocks, simulated clock, notification, check-in with start now / +15 min / pick a time, reflow), then HTTPS deploy and Android push. Fri Oct 30: grader extraction (graded-test photos into per-question rows using a fixed type list per class) and the student model as a plain table. Sat Oct 31: test prep card (practice in the weakest cells, answers checked by a second solve), watch-out lines, templates for homework and test only.
- A missed checkpoint gets its cut that same night instead of borrowing tomorrow's hours. Two misses in a row means dropping the next rung of the ladder.
- The demo runs on the simulated clock. Make sure the start loop saves every ping, check-in answer and actual start to the database (`pings`), so real numbers exist without testers.

## Settled decisions

- Canvas ingest is token only.
- Cards live on the in-tool web page, one layout per card type unless there is slack.
- Demo data is censored real classes (`test_data/`), unless a Free-for-Teacher sandbox course is needed for a live "teacher posts an assignment" moment.

## Stack and APIs

| Piece | Choice | Notes |
| --- | --- | --- |
| Backend | Python, FastAPI, SQLite | A scheduler for start times; pywebpush with VAPID keys for push |
| Front end | One web app installed as a PWA (manifest plus service worker) | Cards, check-in and dashboard; same page on laptop and phone |
| Canvas | Personal access token, polled every few minutes | Client in `app/canvas.py` |
| Model | Muse Spark 1.3, standard tier | Standard tier doesn't train on your content; the contributor tier does |
| Model features | File input, structured output, tool calling | PDFs and images go in directly; DOCX and PPTX are converted to PDF first (LibreOffice headless) |
| Stretch model | Muse Wonder 1.0 (preview) | Only for semester findings and the dry run. $4 input, $20 output, $0.20 cached input per million tokens |
| Hosting | A small server or a Cloudflare tunnel | Phone push needs HTTPS. iPhone push needs the PWA on the home screen, iOS 16.4 or later; Android push works from the browser |

Cost: about 50k input and 2k output tokens per prep is roughly 7 cents at $1.25 and $4.25 per million, so the $150 credit covers around 2,000 preps. `MAX_CARDS_PER_SYNC` exists so a first sync can't spend it.

The Meta Model API is newer than most coding agents' training data. Read these instead of guessing: [docs home](https://dev.meta.ai/docs/), [structured output](https://dev.meta.ai/docs/structured-output), [tool calling](https://dev.meta.ai/docs/tool-calling), [file handling](https://dev.meta.ai/docs/file-handling), [computer use](https://dev.meta.ai/docs/computer-use).

## Data and privacy

- This repo is public. Never commit `.env`, the SQLite DB, attachments, graded-test photos or raw Canvas responses. Real data lives under `data/`, which is gitignored.
- `test_data/` is a censored snapshot of real Canvas responses (names replaced, IDs renumbered, student work dropped) and the only real-derived data in the repo. Its README says what was changed. Demo data in `app/demo.py` is fabricated.
- Use the Meta standard tier only. The contributor tier trains on content, so real school data stays off it.
- Tests never call Canvas, the model or push (see `tests/conftest.py`). Keep it that way.

## Semester findings and dry run (stretch)

Runs only if the core is green by Saturday noon, and it builds on the student model.

- One pass reads the whole semester as text and returns problems that repeat across classes. Every finding must quote the teacher's own words and name the item it came from. Plain code checks the quote appears in that item and drops any finding that fails. Show the page photo next to each finding: the check proves a quote is in the extracted text, not that the photo was read correctly. Never claim the list is complete; the check catches invented findings, not missed ones.
- A second pass has Muse Wonder attempt the assignment and log where it got stuck, backtracked or had to look something up. Those points become the watch-out lines, with a concrete first step and an effort level. The dry-run solution is never shown or stored. Effort is a level with reasons, not minutes. Cut it unless its traps match where the student struggled on three finished assignments.
- Keep the corpus as plain text and put it first in the prompt so it caches. Don't send photos to Wonder; one third-party writeup reports a 1 MiB request limit. Don't pitch this as a long-context win (Wonder scores 80.7% and Spark 83.0% on Artificial Analysis's long-context test); the pitch is findings a judge can verify.
- Cost is unmeasured. Count the corpus with `POST /v1/responses/input_tokens`, check usage on the first calls, and see whether cache writes cost extra. Reasoning tokens bill at the output rate.

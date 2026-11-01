# System One decision layer: spec

Muse Glimmer, running locally through llama.cpp, answers the app's multiple-choice decisions by reading the probabilities of the option letters in a single pass. When a field comes out unsure, Glimmer thinks in short paragraphs with a probe after each one, until it is confident or hits the cap. The decisions then route Spark prep, the scheduler and the topic grid. The split of work: Spark reads photos and writes cards. Glimmer makes every categorical judgment.

The work has three parts, built in this order:

- **Part A**: the decider service.
- **Part C**: the eval harness, which runs calibration first and the comparison later.
- **Part B**: wiring into the backend.

Part B only starts if the go/no-go check in Part C passes.

## Status (update between sessions)

- llama.cpp build: b12023 (f3586e66b), at `C:\Users\Max\llama.cpp\bin`. glimmer_check.py: all 5 checks pass (Oct 31).
- Measured on the 3090: prefill ~1,160 tok/s on a fresh 902-token prompt, decode ~34 tok/s (no drafter). Earlier numbers were ~990 and ~29.
- Fast-pass format (from glimmer_check.py check 3): `empty-thinking`. The letters held 0.998 of the probability with the empty thinking message and 0.880 without it. The decider's `FAST_PASS_FORMAT` defaults to it.
- Token ids: `<|eom|>` 200007, `<|eot|>` 200008, BOS `<|begin_of_text|>` 200000, `<|start|>` 200022, `<|message|>` 200023. Letters A–Z bare are 45–70 in order. With a leading space: A 352, B 410, C 354, D 401, E 427, F 433, G 477, H 465, I 372, J 605, K 590, L 432, M 376, N 448, O 530, P 375, Q 1902, R 423, S 333, T 350, U 574, V 577, W 484, X 1475, Y 939, Z 1214. Every one is a single token.
- Server behavior the decider relies on (measured):
  - With `return_tokens`, a `"\n\n"` stop returns the stop token in `tokens`, though `content` leaves it out. So the ids go back as-is, and the decider only appends `"\n\n"` if a server ever leaves it out.
  - `<|eom|>` is not an end-of-generation token. Glimmer writes `<|eom|>`, then goes on to `<|start|>assistant to=user<|message|>B<|eot|>` in the same call. The loop cuts at the first `<|eom|>`/`<|eot|>`.
  - The chat template appends `# Valid recipients: "self", "user".` to the system turn by itself.
  - The template field has 5 options in `decisions/fields.py` (document, checklist, dashboard, cornell, quick). The Fields table now matches; fields.py wins if they ever differ.
- Part A (decider/): built Oct 31, not committed yet. 31 unit tests pass against a fake llama-server. `python -m decider` serves on 127.0.0.1:8090. `python -m decider.live_check --test-db PATH --server-log PATH` reruns the live check.
- Live run, Oct 31. Default lines (0.85/0.85), skip_thinking empty, seed 7, 1 slot:
  - 3 labeled assignments took 0.5–3.9 s each, with 6–7 probes. type and template were instant on all 3 and matched the hand labels. effort matched 0 of 3: two were `thought`, and the quiz was instant at 0.947 for "one short sitting" where the hand label was multi-day.
  - The Spanish IV graded test (47 questions, 3 topics) took 180 s, 3.8 s per question: 330 probes, 96 thinking calls, 5,398 tokens generated. Tiers: format 32 instant / 15 thought, topic 20 / 27, credit 44 / 3. Nothing came out `guessed`, and nothing hit the cap (the longest thinking was 296 tokens). Credit matched the written marks on 47/47. Topic matched the grader's (Spark) topic on 37/47. Those are not hand labels: no test question is labeled yet.
  - Cache: in every call, each probe after the first processed only its tail. The worst was 1 token beyond it (the last generated token). Later probes processed 7–135 tokens, median 43. A loop's first thinking chunk processes its question's tail (up to ~130 tokens, since the slot held another question); every later chunk processes 1 token. With `--swa-full`, the log had 0 "forcing full prompt re-processing" lines.
- Topic naming differs from the spec. The spec's question, with the answer turn opened directly, made Glimmer copy back "None of the listed topics fit." as the name (3 of 3 real questions). In the first live run that became a topic, and about 15 later questions picked it. Now the naming question ends with "Reply with just the topic name.", uses the empty thinking turn (the fast-pass format), and a name starting "None of" is dropped. After the fix, all 7 naming calls in the run returned an existing topic, and new_topics stayed empty.
- Small additions beyond the spec:
  - the `FAST_PASS_FORMAT` env var
  - an optional `section` on test questions (the grader stores section instructions, and they settle most formats)
  - an optional `course` name on /decide/test
  - an optional `seed` on both endpoints
  - `min_p: 0` in the thinking sampler (llama-server's default is 0.05)
  - one system message shared by assignments and test questions
  - the reversed topic probe keeps "None of these fit" last
  - points lost is not computed in Part A; it's left for Part B.
- Template defaults for lab reports and projects: dashboard for both, the template Max picked most often when labeling them (lab reports 5 of 8, projects 5 of 13). Change `TEMPLATE_DEFAULTS` in `decider/prompts.py` to change it.
- Part C (eval/): built Oct 31 – Nov 1, not committed yet. `python -m eval.run calibrate|compare`, 21 tests in `tests/test_eval.py`. Per-item records stay in `data/eval/` (gitignored).
  - Every option in `decisions/fields.py` now has a definition (`Option.description`), written from how Max labeled: forms, surveys and signatures are "something else" and "quick"; reading checks (RC) and graded check-ins are "test or quiz"; Cornell is for learning from a video, lesson or reading; a verb in parentheses is conjugation; a blank filled from given words (por / para / X) is multiple choice; on a graded test no marks means full credit, and so does -1 then +1. Every model sees "Label: definition" in the lettered block, and the labeling page shows it as the button's tooltip. `PROMPT_VERSION` went to 2.
  - The first calibration (prompt version 1, no definitions) is kept in `data/eval/calibrate.v1.jsonl` and `calibrate-report.v1.txt`. Fast-pass error: type 22%, template 42%, effort 72%, format 49%, topic 4%, credit 59%. Thinking ended at ≥ 0.999 confidence on wrong answers as well as right ones, so no think line had zero errors on any field.
  - Test questions: Spark reads each uploaded test once with the grader's page prompt, cached in `data/eval/tests/<id>.json`. Marks as written combine marks_seen, the written points (earned/possible or -lost) and, for a question alone in its section, the section score. Calc and physics rows matched the labels one to one. Spanish IV was labeled per blank (45) where Spark reads per question (37): a `question_map` joins blanks to rows, and a row whose blanks got different credit counts as partial.
  - Spark misread 4 marks, checked against the page photos and fixed as `corrections` in the cache files: Spanish C4, C7 and C9 have -1 and then +1 (Spark missed the +1), and physics 6 says 7/7 (Spark read 4/7).
  - Calibrate runs on 4 llama-server slots (`-np 4 -c 32768`): 2.13 s per field against 4.15 s on 1 slot. Prompt caching works as in Part A. Batched decoding can change the last digits of a probability, so calibrate's numbers can differ slightly from the 1-slot decider's.
- Lines (decisions/lines.json), approved by Max Nov 1 from the prompt-version-2 calibration (744 labeled fields: 197 assignments, 51 test questions; the zero-error rule held as specced):

  | Field | Cost | Fast line | Think line | Instant on calibration | Fast-pass error |
  |---|---|---|---|---|---|
  | type | expensive | 0.999762 | never (1.01) | 12% | 16.2% |
  | template | expensive | 0.99941 | never | 9% | 31.0% |
  | effort | cheap | 0.98 | never | 1% | 67.0% |
  | format | cheap | 0.996 | never | 29% | 23.5% |
  | topic | expensive | 0.91 | never | 94% | 3.9% |
  | credit | expensive | 0.89 | never | 43% | 21.6% |

  - No think line is possible on any field. When the thinking closes, the probe is ≥ 0.999 on wrong answers as well as right ones (every wrong answer on every field), so no line accepts zero labeled errors, or 10% or less for the cheap fields. A field that isn't instant thinks to its end and comes out `guessed`. Nothing hit the cap (512 tokens); the median loop was about 100–190 tokens.
  - The reversed probe changed only credit's instant share, from 47% to 43%. It never lowered a line.
  - Type's zero-error line is set by one label (assignment 16004, a problem set the model calls something else at 0.99976). Allowing one labeled error would make type 35% instant.
- Cheap fields that skip thinking: none. Effort's fast-pass error is 67% and format's is 23.5%, both over the 10% rule.
- Compare, Nov 1 (`eval/summary.md`, `eval/results.csv`, `eval/compare.png`). Glimmer on 1 slot as in production, seed 7, prompt version 2. Spark used 182k input and 378k output tokens (372k of them reasoning, at its default effort) on 248 calls: $1.81.

  | Setup | Accuracy | Expensive | Cheap | Seconds per item | Cost per 1,000 items |
  |---|---|---|---|---|---|
  | All Spark | 77.8% | 82.9% | 67.7% | 5.2 | $7.30 |
  | Base Glimmer | 74.1% | 79.0% | 64.1% | 7.4 | $2.28 |
  | System One, fast pass only | 66.7% | 78.6% | 42.7% | 0.57 | $0.18 |
  | System One, full | 73.3% | 81.2% | 57.3% | 11.2 | $3.43 |

  - By field (Spark / base Glimmer / fast pass / System One): type 84 / 85 / 85 / 86, template 74 / 73 / 70 / 69, effort 61 / 58 / 34 / 49, format 94 / 88 / 76 / 88, topic 98 / 100 / 96 / 98, credit 96 / 57 / 73 / 94.
  - System One's tiers in compare: 18% instant, 0% thought, 82% guessed, 608 thinking loops, 0 at the cap. Without a think line every unsure field thinks to the end, one loop per field, which is why it is slower than base Glimmer's one call per item.
  - Thinking pays on credit (73 → 94), format (76 → 88) and effort (34 → 49). It adds about 1 point on type and topic and nothing on template.
  - The lines and the option definitions were both fitted on this same labeled set, so System One's numbers are in-sample and somewhat optimistic.
- Prompt versions since, Nov 1. The rules doc is "System One labeling rules" (Claude Docs).
  - 3: 20 worked examples (`Field.examples`) in the shared system turn. Each sourced example's label is held out of scoring.
  - 4: a new type, video or audio. Test corrections and "Receta" moved to something else, and tests and quizzes always get Dashboard.
  - 5: Max's Nov 1 rules ("System One v5 changes for Claude Code"). New type, template and effort definitions, `TEMPLATE_DEFAULTS` as one line per type, an effort rule line (`Field.rules`, printed above the examples), and 7 more examples. Its credit wording ("no points lost by the score") dropped credit from 96% to 84%: Glimmer answered "can't tell" on no-mark questions labeled full.
  - 6: v5 with the v2 credit wording. Only the credit question changed, so the other v5 records were carried over unchanged. The 51 credit fields and the 51 test questions for Spark and Glimmer were re-run.
  - 7 (current): System One asks type first, then template and effort with its own type answer in the prompt (`USES_TYPE`, `with_type` in `decider/prompts.py`). Before this the three were asked blind, unlike Spark and base Glimmer, which answer all three in one reply; reflection exit tickets got "two or three sessions" because the effort question couldn't tell them from mini quizzes. Calibrate records type for every assignment first (held-out ones too), works out System One's type answer under the type lines it proposes, then records template and effort with that answer (`type_answer` in each record). Also Max's second round of Nov 1 rules: exit tickets (reflection → quick, prep → dashboard), labs always dashboard, surveys and missing-work reminders → quick, audio → document, readings → document only in a humanities class on the class's subject, a type rule for project parts, and four effort rules (prep, essays, projects, links vs attachments). 18 relabels, listed in the rules doc; the pre-v7 labels are in `data/labeling/labels.backup-20261101-pre-v7.jsonl`.
  - Max relabeled on Nov 1 (931+ rows in `labels.jsonl`). 21 labels are held out as example sources.
- v6 results (`data/eval/calibrate-report.v6.txt`; `eval/summary.md` now holds the v6 Spark and Glimmer compare). v2 is its records re-scored on the current labels.

  | Field | System One v2 → v6 | System One instant | Base Glimmer v2 → v6 | Spark v2 → v6 |
  |---|---|---|---|---|
  | type | 88 → 88 | 9% → 26% | 86 → 88 | 84 → 89 |
  | template | 67 → 82 | 9% → 15% | 72 → 81 | 72 → 76 |
  | effort | 48 → 70 | 1% → 27% | 59 → 73 | 63 → 76 |
  | format | 90 → 92 | 29% → 94% | 88 → 96 | 94 → 98 |
  | topic | 98 → 96 | 94% → 92% | 100 → 100 | 98 → 96 |
  | credit | 96 → 96 | 43% → 43% | 57 → 86 | 96 → 96 |
  | all | 73 → 83 | | 74 → 84 | 78 → 84 |

  - System One's numbers here come from the calibration: the instant answer, otherwise the thinking answer. System One full wasn't re-run, because it needs the v6 lines.
  - The proposed v6 lines are in `data/eval/proposal.v6.json` (type 0.99964, template 0.9981, effort 0.981, format 0.74, topic 0.91, credit 0.95; think never). Format would now skip thinking (fast-pass error 7.8%). Not approved yet, so `decisions/lines.json` is still v2.
  - Spark cost for the v5 and v6 runs: $2.59. Base Glimmer now matches Spark overall (83.5% vs 83.8%) at $2.70 vs $8.72 per 1,000 items.
- Go/no-go: Max's call, not made yet. As specced, the expensive assignment fields are rarely instant (type 12%, template 9%), and the reversed check doesn't change that. The test-question fields are: topic 94%, credit 43% with zero labeled errors.

## Existing pieces this builds on

- `decisions/fields.py`: the field definitions shared with the labeling page, including option lists and letter order. This is the single source of truth. Do not copy the option lists anywhere else.
- `labels.jsonl`: Max's hand labels from the labeling page.
- `glimmer_check.py`: the server checks. Reuse its prompt-building and probability-reading code.
- Grader extraction (Spark): produces one row per test question. Part B lists the fields each row must contain.

## Architecture

```
Canvas sync ──> job queue (1 worker, due-date order) ──┐
                                                        ├──> Decider service ──> llama-server (Glimmer, 127.0.0.1:8080)
Graded test photo ──> Spark reads rows ────────────────┘          │
                                                                   v
                                                          decisions table
                              ┌────────────────┬───────────────────┼─────────────────────┐
                              v                v                   v                     v
                        Spark prep         scheduler          topic grid           clarify list
                     (type, template)      (effort)    (format, topic, credit)  (guessed expensive fields)
```

The decision loop runs inside the decider, on the same machine as llama-server. The backend makes one HTTP call per assignment, or one per test. Where the decider runs is just config (`DECIDER_URL`), so it can move to a VPS or Modal later without code changes.

## Serving

```
llama-server -m Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf -ngl 99 -c 16384 -np 1 --swa-full --jinja --host 127.0.0.1 --port 8080
```

- Load no mmproj, since Glimmer only sees text. Load no DFlash drafter.
- `--swa-full` is required. Without it, rolling the cache back after a probe fails on the sliding-window layers, and the log shows "forcing full prompt re-processing".
- Parallel thinking is optional and off by default. To enable it, run `-np N -c 16384*N`, and the decider pins each concurrent thinking loop to its own slot with `id_slot`. The setting is `PARALLEL_SLOTS`, default 1.
- Stop the ninfer Qwen server before starting this one. The two do not fit in 24 GB together.
- Never expose llama-server outside the machine.

## Fields

These come from `decisions/fields.py`. Each field's cost decides how the gate treats it.

Assignment fields. Each one is a separate question. Type is asked first; template and effort see System One's type answer after the item context (since prompt version 7):

| Field | Options, in letter order | Cost |
|---|---|---|
| type | test or quiz, essay or written response, reading, problem set or worksheet, lab report, project or presentation, video or audio, something else | expensive |
| template | document, checklist, dashboard, Cornell notes, quick | expensive |
| effort | one short sitting, one solid session, two or three sessions, multi-day | cheap |

The template question includes the defaults from the prep doc formats plan as a hint:

- Readings: document. Cornell notes is the alternative for readings.
- Problem sets: checklist.
- Tests and essays: dashboard, which is built from uploaded graded work.
- Lab reports, projects and "something else": whatever is set in the Status section. Videos and podcasts (added Nov 1): Cornell notes.

What is not a template decision:

- Focus mode is not a template option. It's a view the student turns on for any assignment, so Glimmer never picks it.
- Flashcards are a separate tool, and the timeline format was dropped. Neither one is a decision.

The prep doc plan labels the formats A to E (A document, B checklist, C dashboard, D focus mode, E Cornell notes). Those letters are display names only. Prompts use the format names, and the probe assigns its own letters from the order in `decisions/fields.py`.

Test-question fields:

| Field | Options, in letter order | Cost |
|---|---|---|
| format | multiple choice, true/false, matching, fill in the blank, short answer, free response, calculation, translation, conjugation, diagram | cheap |
| topic | the class's topic list in stored order, then "none of these fit" (always last) | expensive |
| credit | full, partial, none, can't tell | expensive |

- Points lost is computed in code. If the marks contain a number, parse it. If credit is "none" and the test prints a point value, all of that question's points are lost.
- A topic list is capped at 25 topics plus "none of these fit", so that every option is one letter from A to Z. If a class goes over 25, fail loudly.

## Prompt format

- Render the system and user turns with llama-server's `/apply-template`, which applies Glimmer's chat template. Append the assistant parts by hand. The server adds the BOS token, so never include `<|begin_of_text|>`.
- The system message must be byte-identical for every request: fixed instruction text plus `Reasoning strength: low.` Never put timestamps or any per-request data in it. Never change the reasoning strength, because that throws away the cached prefix.
- Order the user message from most shared to least shared:
  - For an assignment: course, title, due date, points, submission types, attachment names, rubric rows, and the first 1,200 characters of the description. Also include how many graded tests have been uploaded for this class, since the dashboard is built from them. The question block comes after.
  - For a test: the format list and the class topic list first, then the question row (question text, the student's answer, the marks as written, the teacher note, points), then the question block.
- The question block is the question, the lettered options (`A) …`), and the line `Reply with just the letter.`
- The assistant suffix:
  - The thinking turn is `<|start|>assistant to=self<|message|>{thinking}<|eom|>`. If the fast-pass format is `no-thinking` and the thinking is empty, leave this turn out entirely.
  - Then the answer turn opener: `<|start|>assistant to=user<|message|>`
- Send generated thinking back as the exact token ids llama-server returned (`return_tokens`), never as retyped text. Retokenized text can come out one token different at a boundary and miss the cache.

## Probe

`POST /completion` with `{prompt, n_predict: 1, n_probs: 20, temperature: 0, cache_prompt: true, id_slot}`. The prompt can be a string or a token array.

- From `completion_probabilities[0].top_logprobs`, add up the probability for each option letter. Count both the `"A"` and `" A"` token variants.
- `letter_share` is the sum of the probabilities on all option letters. `confidence` is the top letter's probability divided by `letter_share`.
- Record the prompt tokens processed, taken from the response timings, as `probe_tokens`.

## Fast pass and gate

1. Run a forward probe with empty thinking.
2. If `confidence >= lines[field].fast` and `letter_share >= 0.5`, run a reversed-order probe: list the options in reverse and reassign the letters. Act only if the reversed probe picks the same option and also has `letter_share >= 0.5`. The tier is `instant`.
3. Anything else is unsure. When the forward probe already fails, skip the reversed probe.
4. If the field is unsure and it is in `skip_thinking` (a cheap field whose fast-pass error was 10% or less on the labels), take the forward probe's top option. The tier is `guessed`.
5. Every other unsure field goes to the thinking loop.

## Thinking loop

- Generate thinking with `POST /completion` and `{n_predict: 96, stop: ["\n\n"], temperature: 1.0, top_p: 0.95, top_k: 64, return_tokens: true}`. Use a fixed seed in eval runs. Append the `"\n\n"` stop text back onto the thinking.
- If the returned tokens contain `<|eom|>` or `<|eot|>`, truncate there: Glimmer has finished thinking. Run one final probe and use its answer. The tier is `thought` if it clears the line, otherwise `guessed`.
- If a chunk is under 24 tokens, generate another chunk before probing.
- Probe after each paragraph, checking confidence and letter_share. Do not run the reversed probe inside the loop.
- Stop as soon as `confidence >= lines[field].think` and `letter_share >= 0.5`. The tier is `thought`.
- The cap: only start a new paragraph while the total thinking tokens are under 512. The paragraph already in progress always finishes, so the worst case is about 600 tokens.
- At the cap, if the field is still below the line, take the top option. The tier is `guessed`.
- Never stop because consecutive probes agree. Stop on confidence only.

## Topic naming

This applies when the topic field decides on "none of these fit" with tier `instant` or `thought`.

- Send a naming request. The question is: "None of the listed topics fit. Write a short topic name (6 words or fewer). If a listed topic is close, write that one exactly." Open the answer turn directly. Settings: `n_predict: 16, temperature: 0, stop: ["\n"]`.
- If the result matches an existing topic (case-insensitive), use that topic. Otherwise add it to the class list for the rest of this test, and return it in `new_topics`.

## Decider service API (Part A)

A FastAPI app in `decider/`, running as a separate process.

- `GET /health` returns `{ok, llama_ok, build}`.
- `POST /decide/assignment` takes `{assignment: {...}, fields?: [names]}` and returns `{decisions: {field: Decision}, timings}`. It runs type first (fast pass, then thinking if unsure), then template and effort with the type answer in their prompt. Type is decided even when only template or effort is asked for.
- `POST /decide/test` takes `{class_id, topics: [...], questions: [{id, text, answer, marks, note, points}], fields?}` and returns `{decisions: {question_id: {field: Decision}}, new_topics: [...], timings}`. It runs the questions in order: the fast pass for every field first, then the thinking loops for the unsure ones. Loops run in parallel only when `PARALLEL_SLOTS > 1`.

A `Decision` is:

```
{value, option_index, confidence, letter_share, flipped: bool|null,
 tier: "instant"|"thought"|"guessed", think_tokens, probe_tokens,
 trace: [{think_tokens, probs: {option: p}, letter_share}], prompt_version, model}
```

Config comes from environment variables: `GLIMMER_URL`, `PARALLEL_SLOTS=1`, `CAP_TOKENS=512`, `PARAGRAPH_MAX=96`, `MIN_PROBE_CHUNK=24`, `LETTER_SHARE_MIN=0.5`, `LINES_FILE=decisions/lines.json`, `PROMPT_VERSION`.

The lines file looks like `{"lines": {field: {"fast": x, "think": y}}, "skip_thinking": [fields]}`. Until calibration runs, every field uses fast 0.85 and think 0.85, and `skip_thinking` is empty.

## Part B: wiring into the backend

The decisions table:

| Column group | Columns |
|---|---|
| Identity | `id`, `item_kind` (assignment or test_question), `item_id`, `field` |
| Answer | `value`, `option_index`, `confidence`, `letter_share`, `flipped` |
| How it was decided | `tier` (instant, thought, guessed, rule or confirmed), `think_tokens`, `probe_tokens`, `trace` (JSON) |
| Versioning | `prompt_version`, `model` |
| Confirmation | `confirmed_value`, `confirmed_at` |
| Timestamps | `created_at`, `updated_at` |

`(item_kind, item_id, field)` is unique. A re-run overwrites the row unless its tier is `confirmed`.

Assignment flow:

- When the Canvas sync finds a new assignment, it creates the card in a "preparing" state and puts the assignment on the queue. A single worker drains the queue in due-date order.
- The worker calls `POST /decide/assignment` with a 90-second timeout and writes the rows. It then runs Spark prep in the decided format (document, checklist, dashboard or Cornell notes). Effort sets the scheduler's block size.
- Focus mode stays a student toggle on any card. Part B doesn't change how it works.
- When a student switches a card's format themselves, the template field's tier becomes `confirmed` with the format they picked.

Fallback:

- If the decider is unreachable, returns an error or times out, use the app's existing pre-Glimmer behavior. A submission type of `online_quiz` means test or quiz. The tier is `rule`, and the item goes on a re-decide list.
- A periodic task re-runs the re-decide list whenever `/health` reports ok.

Test flow:

- Spark extraction produces one row per question: the question text, the student's answer, the marks copied literally ("X", "✓", "-2", "half circled", never paraphrased as "incorrect"), the teacher note, and points.
- If the class has no topics yet, the same extraction call also returns 5 to 8 seed topics.
- The backend then calls `POST /decide/test` with a 600-second timeout, saves `new_topics` to the class's topic list, and updates the grid.

The card badge:

- `instant` shows "Decided instantly", `thought` shows "Thought it over", and `guessed` shows "Best guess". `rule` shows no badge.
- Tapping the badge opens a panel with the probability bars from the last trace entry. For fields with tier `thought` or `guessed`, it also shows the belief curve: the top option's probability against thinking tokens.
- A cheap field with tier `guessed` only gets a small flag on the card.

Clarify:

- The cards screen shows a count of guessed expensive fields ("2 to check"). Tapping it opens a list showing each field's top two options and a dismiss button. No banners, no popups, no push notifications.
- An item expires when its assignment's start time passes or the assignment is completed. A dismissed item never comes back.
- After a test upload, the same list appears inline at the bottom of the results.
- Answering sets the tier to `confirmed`. A changed type or template re-runs Spark prep. A changed topic or credit updates the grid. A changed effort reschedules the block.

## Part C: eval harness

`eval/run.py` has two subcommands.

`calibrate`:

- Runs System One over every labeled item. For every item and field, it records the forward fast-pass probe, the reversed probe, and a full thinking loop up to the cap, with its trace.
- Any label whose value isn't among the field's current options (for example, an old template label of "focus mode") is skipped and listed in the report.
- It reports, per field: confidence histograms for right versus wrong answers, both for the fast pass and after thinking; the fast-pass error rate; and the cap-hit rate.
- It proposes settings:
  - Expensive fields: the lowest line that accepts zero labeled errors.
  - Cheap fields: the lowest line that accepts no more than 10% errors.
  - `skip_thinking`: every cheap field whose fast-pass error is 10% or less.
- It prints the proposal and waits for Max's approval before writing `decisions/lines.json`.
- Go/no-go is Max's call, made from this report. If the expensive fields can't be decided instantly at any useful rate, and the reversed-order check doesn't fix that, Part B is skipped.

`compare` runs four setups on the labeled set:

1. All Spark: Muse Spark with structured output, one call per item covering all of its fields. Cost comes from the `usage` the API returns, at Standard-mode prices: $1.25 per million input tokens, $0.15 per million cached input tokens, $4.25 per million output tokens.
2. Base Glimmer: `/v1/chat/completions` at reasoning strength low, one call per item covering all of its fields, with the answers parsed from the text.
3. System One, fast pass only: taken from the records of setup 4.
4. System One, full: the decider with `lines.json`.

Rules for the comparison:

- Every setup uses identical question text and options from `decisions/fields.py`.
- Metrics:
  - Accuracy per field and per cost group.
  - Seconds per item, measured by wall clock.
  - Cost per 1,000 items. For Glimmer, price GPU seconds at $1.10 an hour (Modal's A10, as a rental proxy), and note that on Max's own card the real cost is electricity.
  - For System One, the share of fields by tier, and the cap-hit rate.
- Outputs: `eval/results.csv`, `eval/summary.md`, and `eval/compare.png`, a chart of accuracy against seconds per item (log x axis) with one labeled point per setup and its cost per 1,000 items in the label.

## Design rules

- `decisions/fields.py` is the only source of field definitions and option order.
- Spark never makes categorical judgments. Glimmer never transcribes.
- Stop on confidence, never on agreement between consecutive probes.
- The cached prefix must be byte-identical across requests.
- The core app works with the decider off. A card always gets created, whether or not the decider answers.

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
- Lines now: the v9 lines with effort skipping thinking, approved by Max Nov 1 (v9 results below). The first approved lines, from the prompt-version-2 calibration, are kept in `data/eval/lines.v2.json` (744 labeled fields: 197 assignments, 51 test questions; the zero-error rule held as specced):

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
- Cheap fields that skip thinking (v2): none. Effort's fast-pass error is 67% and format's is 23.5%, both over the 10% rule. Now (v9): format by the rule (fast-pass error 3.9%), and effort by hand (`SKIP_THINKING` in `eval/calibrate.py`), though its fast-pass error is 25.7%.
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
  - 7: System One asks type first, then template and effort with its own type answer in the prompt (`USES_TYPE`, `with_type` in `decider/prompts.py`). Before this the three were asked blind, unlike Spark and base Glimmer, which answer all three in one reply; reflection exit tickets got "two or three sessions" because the effort question couldn't tell them from mini quizzes. Calibrate records type for every assignment first (held-out ones too), works out System One's type answer under the type lines it proposes, then records template and effort with that answer (`type_answer` in each record). Also Max's second round of Nov 1 rules: exit tickets (reflection → quick, prep → dashboard), labs always dashboard, surveys and missing-work reminders → quick, audio → document, readings → document only in a humanities class on the class's subject, a type rule for project parts, and four effort rules (prep, essays, projects, links vs attachments). 18 relabels, listed in the rules doc; the pre-v7 labels are in `data/labeling/labels.backup-20261101-pre-v7.jsonl`. A 19th relabel ("Founding documents" template → document, by the reading rule) came after the backup.
  - 8: v7's Something else wording ("organizing notes, a journal or a binder (including an ungraded review-and-organize packet)") pulled journals, a quick write and ungraded problem sets into Something else after thinking, though the fast pass had them right. v8 drops "journal" and "ungraded". Only the type options changed, so the v7 records of every other field were carried over and only type, plus the 49 template and effort fields whose type answer changed, were re-recorded.
  - 10 (current): from the review of what System One misses (with Spark right, or with Spark wrong too). An effort hint by type (`EFFORT_DEFAULTS`, shown under the effort question like the template hint, so every setup sees it). Something else is the last resort: a thin item in a math, science or coding class goes by its title, a skill check-in is a Test or quiz, and an external-tool assignment in a coding class is a notebook. New rules for skill practice in sentences (Problem set), a doc to fill in about a reading (written answers), creative writing (Essay unless performed; Dashboard over several days or a major grade, Checklist for a short deadline at home), group presentations, video-led activity sets, prep assignments (Checklist, own size, never Multi-day), journal checks (Dashboard), project parts and unit projects, test corrections (One solid session), points as a size hint when there's no description, and format by what the question asks. Document now covers any non-STEM reading read outside class, including the questions that come with it. Max's problem-set rule goes into the effort definitions: One solid session unless it was started or handed out in class, is a link, or has only a few questions. 15 relabels by Max (backup: `data/backups/labels.before-nov1-evening-relabels.jsonl`): Journal Check #1 template → dashboard; Imitation Poem template → checklist; Diario de Colón and Objetivos de la Colonización template → document; Lab: Free Fall effort → one solid session; Lectura de práctica → reading, document, one solid session; both Chemistry Test 3 corrections effort → one solid session; Receta effort → one short sitting, template → quick; Classrooms Conducive to Learning type → reading; Distance/Airfare Stats Project effort → multi-day; Mental Health Project Research Question effort → one short sitting. Then a short assignment done in class with no time to prepare is not Dashboard (an in-class essay still is), checking work against an answer key is Something else and Quick task, and 10 more relabels to fit the problem-set rule (backup: `data/backups/labels.before-problem-set-relabels.jsonl`): six problem sets effort → one solid session (19748, 14207, 23608, 8104, 8595, 15408; 14207 went back to one short sitting after the run, since its worksheet was handed out in class), three → one short sitting (19777, 14243, 19755), and KEY: Vectors HW → something else, quick task, one short sitting. Calibrated in both modes and compared (records in `data/eval/compare.v10/`, outputs in `data/eval/compare-outputs.v10/`, lines in `data/eval/proposal.v10.json` and `proposal-joint.v10.json`, not approved yet). Overall, on the labels after the 14207 fix: Spark 94.7%, base Glimmer 92.7%, System One full 92.3% (fast pass 88.8%), shared chain 92.4%, d1-2B 78.3%. The v9 records scored on the v10 labels: Spark 88.8%, base Glimmer 87.8%, System One full 87.8%, d1-2B 74.3%, so the relabels alone moved little and the gain is the prompt. System One's effort went 74.9% → 87.4%; its credit fell 98.0% → 92.2% (thinking flipped two right answers on test-002 to none, and two partials came out full). Max then put test questions on the fast pass alone: topic and credit join format in skip_thinking (`SKIP_THINKING` in `eval/calibrate.py`, which now takes a field of any cost). On the compare records that costs one topic answer and no credit ones, at 0.8 s a question instead of 2.7; on the calibration records it costs 2 topic and 3 credit answers (topic 100% → 96%, credit 98% → 92%). Credit's fast pass leans on its option offsets: 47 of 51 right with them, 37 without. Max approved these lines (fields mode) into `decisions/lines.json`; the joint proposal isn't written.
  - 9: a usual template line for Something else ("Quick task if it's one small action with nothing to prep, Dashboard for a participation grade, otherwise Checklist"; the Dashboard part was added after the v9 run, when both "Participación" items came out Quick task and Checklist, and Max kept it in v9 since one change isn't a new version, so the v9 template records predate it); a reading done in class gets Cornell, and Document needs a humanities reading on the class's subject read outside class (Max: "In-Class: Poetry Groups" is Cornell because it's in class); the in-class effort rule now opens with "a separate assignment to prepare for something goes by its own size", names its multi-day cases outright (an in-class essay or writing assessment, a graded discussion) and adds "a small quiz like an examencito is Two or three sessions". Three relabels: "Objetivos de la Colonización" template → dashboard (it's typed essay now), "Founding documents" template back to cornell (read in class, so the 19th relabel above is undone), and "HW 8.1 … Khan Activities" template checklist → cornell (it stays video or audio).
- Nov 1 decision changes, from Claude's review of the v8 records (in the decider and calibrate, measured in the v9 results below):
  - Clarify: only a type or template whose thinking ended off the fast pass's answer, or hit the cap, goes on the clarify list (`clarify` in the decision). A guessed effort gets the badge but no question. Topic and credit keep thinking's answer: on credit, when thinking disagrees with the fast pass it's right 7 times in 9. A loop that closes below the line on the fast pass's own answer is now `thought`, not `guessed`.
  - Re-ask: a type the student fixes in the clarify window goes back in as `type`, and template and effort are decided again with it.
  - Credit stops thinking once a probe reaches 0.999 (`THINK_STOPS` in `eval/calibrate.py`, written to the proposal as credit's think line). On the v8 records it keeps the same 24 of 26 right and cuts about a third of the thinking. The same stop costs type and template 4 or 5 right answers each, so they stay at never.
  - Template from the type: a test or quiz, problem set or worksheet, or lab report gets its usual template (dashboard, checklist, dashboard) with no template question, tier `default` (`TEMPLATE_FROM_TYPE=1`; `0` asks it). Calibrate skips those template records and the report scores them as the default.
- v9 results, System One from the calibration (`data/eval/calibrate-report.v9.txt`, lines in `data/eval/proposal.v9.json`). Both columns are the v8 and v9 records scored on the current labels under the Nov 1 decision rules, each with its own fitted lines. "No question" is instant, plus `default` for template.

  | Field | System One v8 → v9 | No question v8 → v9 | Thinking median v8 → v9 |
  |---|---|---|---|
  | type | 174 → 173 of 189 (92%) | 40% → 41% | 169 → 178 tokens |
  | template | 167 → 167 of 190 (88%) | 63% → 52% (17 instant, 81 from the type) | 215 → 180 |
  | effort | 148 → 147 of 191 (77%) | 35% → 32% | 275 → 251 (cap 10 → 7) |
  | format | 49 → 49 of 51 | 98% → 98% | none |
  | topic | 50 → 50 of 51 | 92% → 94% | 268 → 248 |
  | credit | 49 → 49 of 51 | 49% → 49% | 221 → 117 |

  - Accuracy didn't move on any field. The targeted fixes landed: "Examencito (F)" and "Reading Check 1" are Two or three sessions, "PREPARATION for in-class essay" and "Prepare for your Unit 1 In-Class Writing Assessment" get Checklist, "Founding documents" gets Cornell, the SCAFFOLDING doc and both "Strands of Democracy" parts are One solid session. An equal number of nearby answers flipped the other way, mostly one short sitting vs one solid session on homework, and "Unit 1 Project" and "Read Dickinson's Poems" on effort.
  - Template from the type: 81 of 190 templates need no question, 80 right. The miss is "HW 8.1 … Khan Activities", labeled video or audio, which System One types a problem set, so it gets Checklist.
  - Both "Participación" items (type Something else, template labeled dashboard) now come out Quick task and Checklist: the new Something else line names only those two.
  - Clarify: 17 type questions catch 7 of the 16 type errors, and 23 template questions catch 14 of the 22 errors on asked templates. That's 40 questions over 197 assignments, 19 of them on an answer that was already right. The other 17 errors show "Thought it over".
  - Credit's 0.999 stop halves its thinking at the same 49 of 51. It fixes one answer ("q8+q9+q10" → partial) and breaks one ("q22" → none).
  - Proposed v9 lines: type 0.9987, template 0.9977, effort 0.988, format 0.64 (think 0.99, skip thinking), topic 0.86, credit 0.86 (think 0.999); every other think line never. Approved by Max Nov 1 and written to `decisions/lines.json`, with effort added to `skip_thinking`: on the v9 records effort goes from 147 to 142 right of 191 and saves about 12 s of thinking per assignment on 1 slot. System One now runs on 4 slots (`PARALLEL_SLOTS=4`).
- Compare, prompt version 9, Nov 1 (`eval/summary.md`, `eval/results.csv`, `eval/compare.png`; copies and records in `data/eval/compare-outputs.v9/` and `data/eval/compare.v9/`). All three setups re-run on the same labels. Glimmer on 1 slot as in production, seed 7. System One full used the proposed v9 lines (`LINES_FILE=data/eval/proposal.v9.json`), before Max approved them. The Dashboard part of the Something else template line was in the prompt for this run. Spark: 590k input tokens (148k cached) and 393k output (387k reasoning) on 248 calls, $2.24.

  | Setup | Accuracy | Expensive | Cheap | Seconds per item | Cost per 1,000 items |
  |---|---|---|---|---|---|
  | All Spark | 88.8% | 92.1% | 82.2% | 5.30 | $9.04 |
  | Base Glimmer | 87.6% | 90.0% | 82.6% | 9.31 | $2.85 |
  | System One, fast pass only | 83.3% | 86.1% | 77.7% | 1.02 | $0.31 |
  | System One, full | 87.7% | 91.5% | 80.2% | 11.38 | $3.48 |

  - By field (Spark / base Glimmer / fast pass / System One): type 92.1 / 92.6 / 88.4 / 93.1, template 89.5 / 87.4 / 84.7 / 86.3, effort 78.0 / 79.1 / 72.8 / 75.9, format 98.0 / 96.1 / 96.1 / 96.1, topic 98.0 / 100 / 96.1 / 98.0, credit 96.1 / 80.4 / 72.5 / 98.0.
  - From v6 (Spark 83.8%, base Glimmer 83.5%), every setup gained 4–5 points from the v7–v9 prompts and relabels. System One full ties base Glimmer overall and leads on the expensive fields (91.5% vs 90.0%), mostly from credit (98.0 vs 80.4). It trails on effort (75.9 vs 79.1) and template (86.3 vs 87.4).
  - Speed splits by item kind. Assignments: Spark 5.6 s mean, base Glimmer 9.3 s, System One 13.3 s (median 12.1, 31 of 197 with no thinking at 1.5 s; the fast pass alone is 1.08 s). Test questions: Spark 4.3 s, base Glimmer 9.4 s, System One 4.1 s, since it reads the whole test in one call.
  - Tiers: 38% instant, 38% thought, 13% guessed, 11% from the type; 365 thinking loops, 16 at the cap. Clarify asks 43 questions (22 type, 21 template) over 197 assignments, 19 of them on wrong answers.
  - The v9 lines and the option definitions were fitted on this same labeled set, so System One's numbers are in-sample. Calibrate's System One numbers (type 173, template 167) differ a little from compare's (176, 164) because calibrate ran on 4 slots and batched decoding changes the last digits of probabilities.
- Compare, current setup, Nov 1: the approved lines in `decisions/lines.json` (effort skips thinking), `PARALLEL_SLOTS=4`, llama-server `-np 4 -c 32768`. Only System One re-run; Spark and base Glimmer as above. Copies and records in `data/eval/compare-outputs.v9-4slot/` and `data/eval/compare.v9-4slot/`; `eval/summary.md` now holds this run.

  | Setup | Accuracy | Expensive | Cheap | Seconds per item | Cost per 1,000 items |
  |---|---|---|---|---|---|
  | All Spark | 88.8% | 92.1% | 82.2% | 5.30 | $9.04 |
  | Base Glimmer | 87.6% | 90.0% | 82.6% | 9.31 | $2.85 |
  | System One, 1 slot, effort thinks (above) | 87.7% | 91.5% | 80.2% | 11.38 | $3.48 |
  | System One, 4 slots, effort skips thinking | 86.6% | 91.3% | 77.3% | 6.83 | $2.09 |

  - Effort fell from 145 to 138 right of 191 (72.3%, the fast pass's own score); the calibration records predicted 5 fewer. Type went 176 → 174 and template 164 → 165. Those small moves come from batched decoding on 4 slots, not from a rule change. Format, topic and credit didn't change.
  - Assignments: 7.9 s mean (median 6.2, against 13.3 and 12.1), now faster than base Glimmer's 9.3 s and behind Spark's 5.6 s. 48 of 197 assignments need no thinking at all (1.3 s). Test questions: 2.5 s each (against 4.1), faster than Spark (4.3) and base Glimmer (9.4).
  - Tiers: 37% instant, 27% thought, 25% guessed (effort's 69% guessed is most of it), 11% from the type; 240 thinking loops, 6 at the cap. Clarify asks 46 questions (23 type, 23 template), 21 of them on wrong answers.
- Architecture changes, Nov 1 evening, from the decision-model review (Clef, GEV-26B-Decide, Liquid d1). Built and tested against the fake server, and replayed on the v9 records; nothing approved or re-run on Glimmer yet. The proposal is in `data/eval/proposal.json`, and the prompts change next, so it gets recalibrated anyway.
  - Option offsets: a log offset per option on both fast-pass probes, fitted by calibrate and kept only when they add 3% of the field's labeled cells held out (5-fold). On the v9 records only credit keeps them: its fast pass goes from 39 to 49 right of 51 held out (full +1.45, partial +0.50, none −1.58, can't tell −0.37). With them credit's fast line drops from 0.86 to 0.6 and it is instant on 48 of 51, all right. Type gains 2 of 189, template, effort and format none, so they keep none.
  - Temperatures: two per field, one for decisions from the fast pass and one for decisions from a thinking probe, with each option floored at 1e-4 first. Thinking probes leave the other letters at exactly 0, which no temperature can soften: one temperature per field fitted template's at 0.5 and left it as overconfident as before. Decisions now carry `calibrated_confidence`; `confidence` stays what the lines are on. Calibration error held out: type 0.083 → 0.012, template 0.202 → 0.004, effort 0.191 → 0.078, credit 0.075 → 0.051. Format (0.029) and topic (0.023) were already calibrated and stay about the same.
  - Effort: a fast line of 0.80 set by hand (`FAST_LINES`) instead of skipping thinking. On the v9 records: 145 instant (81% right), 46 thinking loops, 77% right overall, 97% within one level.
  - Joint mode (`DECIDER_MODE=joint`, `decider/joint.py`), for assignments: one prompt with every field (base Glimmer's) and one thinking chain, with a probe per field after each paragraph. Each probe writes the earlier fields' answers in the "field: letter" reply form, then asks "<field>:". It has its own lines file (`decisions/lines.joint.json`, from `calibrate --mode joint`). Not recorded on Glimmer yet.
  - Compare gained two setups (5: System One, shared chain; 6: Liquid d1-2B), effort scored within one level, and `data/eval/audit.md`: 38 labels that Spark, base Glimmer and System One all miss, to check by hand.
  - Liquid d1-2B (Q8_0, 1.9B, its own llama-server on port 8081, every field of an item in one `/v1/systemone` call): 74.7% overall (81.3% expensive, 61.6% cheap) at 0.14 s per item, $0.044 per 1,000. By field: type 83.1, template 76.8, effort 60.2 (91.1 within one level), format 66.7, topic 100, credit 72.5. Its confidence is honest but rarely high: at 0.95 or above it's right every time, but only on 16 of 189 types and 12 of 190 templates, too few to gate Glimmer with.
- v8 results, System One from the calibration (`data/eval/calibrate-report.v8.txt`, lines in `data/eval/proposal.v8.json`). Both columns are scored on the current labels with lines fitted on those labels, so the v6 numbers differ from the v6 table below.

  | Field | System One v6 → v8 | Instant v6 → v8 |
  |---|---|---|
  | type | 91 → 92 | 26% → 40% |
  | template | 85 → 88 | 15% → 63% |
  | effort | 70 → 77 | 27% → 35% |
  | format | 92 → 96 | 94% → 98% |
  | topic | 96 → 98 | 92% → 92% |
  | credit | 96 → 96 | 43% → 49% |
  | all | 85 → 88 | |

  - Proposed v8 lines: type 0.999, template 0.9918, effort 0.9852, format 0.73 (think 0.97, skip thinking), topic 0.9, credit 0.82; every other think line never. Not approved, so `decisions/lines.json` is still v2. Base Glimmer and Spark have not been re-run on v7 or v8 (the shared system turn changed, so their v6 numbers are stale).
  - Template instant went from 15% to 63% with zero labeled errors: with the type answer in the prompt, Glimmer is sure of the template far more often.
  - The cost of chaining: a wrong type now carries into template. "Arrays and Tables", "Working with Functions" and "Países de habla Hispana" are typed Something else and so get Quick task.
  - New effort misses from the in-class rule: "Examencito (F)", "PREPARATION for in-class essay" and the SCAFFOLDING doc (which mentions a coming in-class writing) now come out multi-day. Two calc handouts "attached here" went to one solid session under the attachment rule.
  - Max relabeled on Nov 1 (931+ rows in `labels.jsonl`). 21 labels are held out as example sources.
- v6 results (`data/eval/calibrate-report.v6.txt`; the v6 Spark and Glimmer compare is in `data/eval/compare-outputs.v6/`). v2 is its records re-scored on the current labels.

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
                     (type, template)      (effort)    (format, topic, credit)  (guessed type or template)
```

The decision loop runs inside the decider, on the same machine as llama-server. The backend makes one HTTP call per assignment, or one per test. Where the decider runs is just config (`DECIDER_URL`), so it can move to a VPS or Modal later without code changes.

## Serving

```
llama-server -m Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf -ngl 99 -c 32768 -np 4 --swa-full --jinja --host 127.0.0.1 --port 8080
```

- Load no mmproj, since Glimmer only sees text. Load no DFlash drafter.
- `--swa-full` is required. Without it, rolling the cache back after a probe fails on the sliding-window layers, and the log shows "forcing full prompt re-processing".
- Thinking loops run in parallel, one slot each: the decider pins each concurrent loop to its own slot with `id_slot`. The setting is `PARALLEL_SLOTS`, default 4 since Nov 1 (it was 1); llama-server needs `-np` at least that. `-c 32768` gives each of 4 slots 8,192 tokens. The longest prompt in the v9 runs was 3,555 tokens, plus at most 512 of thinking. For 1 slot, run `-np 1 -c 16384` with `PARALLEL_SLOTS=1`.
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
4. If the field is unsure and it is in `skip_thinking` (a cheap field whose fast-pass error was 10% or less on the labels, or one set by hand), take the forward probe's top option. The tier is `guessed`.
5. Every other unsure field goes to the thinking loop.

When the lines file has offsets for the field, both fast-pass probes are shifted before the gate: each option's probability times e^offset, renormalized to the same letter share. Thinking probes are never shifted.

## Joint mode

`DECIDER_MODE=joint` decides an assignment's fields together; test questions are decided per field either way.

- The prompt is base Glimmer's: the system turn for a few questions, the item, every question block under its `[field]` header, and the reply form `type: <letter>`, one line per field.
- A field's probe opens the answer turn, writes the earlier fields' answers in that form, then `<field>:`. An earlier field's answer is its settled value once it has one, else its top option in the same round.
- The fast pass is a forward round of probes, then a reversed round (every question reversed) for the fields that cleared their fast line. The tiers mean what they do per field, and a template the type settles is `default`.
- One thinking chain serves every unsure field. After each paragraph, each field still unsure is probed; one that clears its think line is settled and written from then on. The chain stops when none is left, or as the per-field loop does (Glimmer closes its thinking, or the cap).
- The lines come from `JOINT_LINES_FILE` (`decisions/lines.joint.json`), calibrated with `calibrate --mode joint`.

## Thinking loop

- Generate thinking with `POST /completion` and `{n_predict: 96, stop: ["\n\n"], temperature: 1.0, top_p: 0.95, top_k: 64, return_tokens: true}`. Use a fixed seed in eval runs. Append the `"\n\n"` stop text back onto the thinking.
- If the returned tokens contain `<|eom|>` or `<|eot|>`, truncate there: Glimmer has finished thinking. Run one final probe and use its answer. The tier is `thought` if it clears the line or picks the fast pass's forward answer, otherwise `guessed`.
- If a chunk is under 24 tokens, generate another chunk before probing.
- Probe after each paragraph, checking confidence and letter_share. Do not run the reversed probe inside the loop.
- Stop as soon as `confidence >= lines[field].think` and `letter_share >= 0.5`. The tier is `thought`.
- The cap: only start a new paragraph while the total thinking tokens are under 512. The paragraph already in progress always finishes, so the worst case is about 600 tokens.
- At the cap, if the field is still below the line, take the top option. The tier is `guessed`, even when it's the fast pass's answer.
- Never stop because consecutive probes agree. Stop on confidence only.

## Topic naming

This applies when the topic field decides on "none of these fit" instantly, or by a thinking probe that clears the think line. A loop that only closes on the fast pass's answer is `thought` but doesn't name a topic.

- Send a naming request. The question is: "None of the listed topics fit. Write a short topic name (6 words or fewer). If a listed topic is close, write that one exactly." Open the answer turn directly. Settings: `n_predict: 16, temperature: 0, stop: ["\n"]`.
- If the result matches an existing topic (case-insensitive), use that topic. Otherwise add it to the class list for the rest of this test, and return it in `new_topics`.

## Decider service API (Part A)

A FastAPI app in `decider/`, running as a separate process.

- `GET /health` returns `{ok, llama_ok, build}`.
- `POST /decide/assignment` takes `{assignment: {...}, fields?: [names], type?: value}` and returns `{decisions: {field: Decision}, timings}`. It runs type first (fast pass, then thinking if unsure), then template and effort with the type answer in their prompt. Type is decided even when only template or effort is asked for. `type` is a type the student fixed: it isn't decided again or returned, and template and effort use it. When the type is test or quiz, problem set or worksheet, or lab report, the template isn't asked: it's that type's usual template (`TEMPLATE_FROM_TYPE` in `decider/prompts.py`) with tier `default`.
- `POST /decide/test` takes `{class_id, topics: [...], questions: [{id, text, answer, marks, note, points}], fields?}` and returns `{decisions: {question_id: {field: Decision}}, new_topics: [...], timings}`. It runs the questions in order: the fast pass for every field first, then the thinking loops for the unsure ones. Loops run in parallel only when `PARALLEL_SLOTS > 1`.

A `Decision` is:

```
{value, option_index, confidence, calibrated_confidence, letter_share, flipped: bool|null,
 tier: "instant"|"thought"|"guessed"|"default", think_tokens, probe_tokens,
 trace: [{think_tokens, probs: {option: p}, letter_share}], prompt_version, model,
 clarify: bool}
```

`clarify` is true for a `guessed` type or template: thinking ended on another answer than the fast pass's, or ran to the cap (or the field is in `skip_thinking`). A `default` template has no trace, and its `calibrated_confidence` is 1.

`calibrated_confidence` is the decision's chance of being right: the last probe's letter probabilities, each floored at 1e-4 of the letter share, at the field's temperature for that kind of probe (fast pass or thinking). `confidence` is the raw share the lines are on.

Config comes from environment variables: `GLIMMER_URL`, `PARALLEL_SLOTS=4`, `CAP_TOKENS=512`, `PARAGRAPH_MAX=96`, `MIN_PROBE_CHUNK=24`, `LETTER_SHARE_MIN=0.5`, `LINES_FILE=decisions/lines.json`, `PROMPT_VERSION`, `TEMPLATE_FROM_TYPE=1`, `DECIDER_MODE=fields` (or `joint`), `JOINT_LINES_FILE=decisions/lines.joint.json`.

The lines file looks like `{"lines": {field: {"fast": x, "think": y, "temperature": t, "think_temperature": t, "offsets": {option: b}}}, "skip_thinking": [fields]}`. Offsets and the temperatures are optional (no offsets, temperature 1). Until calibration runs, every field uses fast 0.85 and think 0.85, and `skip_thinking` is empty.

## Part B: wiring into the backend

The decisions table:

| Column group | Columns |
|---|---|
| Identity | `id`, `item_kind` (assignment or test_question), `item_id`, `field` |
| Answer | `value`, `option_index`, `confidence`, `letter_share`, `flipped` |
| How it was decided | `tier` (instant, thought, guessed, default, rule or confirmed), `clarify`, `think_tokens`, `probe_tokens`, `trace` (JSON) |
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

- `instant` shows "Decided instantly", `thought` shows "Thought it over", `guessed` shows "Best guess", and `default` shows "Set by the type". `rule` shows no badge.
- Tapping the badge opens a panel with the probability bars from the last trace entry. For fields with tier `thought` or `guessed`, it also shows the belief curve: the top option's probability against thinking tokens.
- A cheap field with tier `guessed` only gets a small flag on the card.

Clarify:

- The cards screen shows a count of decisions with `clarify` set ("2 to check"): a type or template whose thinking ended off the fast pass's answer or hit the cap. Tapping it opens a list showing each field's top two options and a dismiss button. No banners, no popups, no push notifications.
- A guessed effort gets the badge and flag but no question. Topic and credit never go on the list: thinking's answer stands.
- An item expires when its assignment's start time passes or the assignment is completed. A dismissed item never comes back.
- After a test upload, the same list appears inline at the bottom of the results.
- Answering sets the tier to `confirmed`. A changed type re-asks template and effort first: `POST /decide/assignment` with `type` set to the student's answer and `fields: ["template", "effort"]`. The answers overwrite the rows unless they're confirmed, and a template the new type settles comes back as `default`. Then a changed type or template re-runs Spark prep. A changed topic or credit updates the grid. A changed effort reschedules the block.

## Part C: eval harness

`eval/run.py` has two subcommands.

`calibrate`:

- Runs System One over every labeled item. For every item and field, it records the forward fast-pass probe, the reversed probe, and a full thinking loop up to the cap, with its trace.
- Any label whose value isn't among the field's current options (for example, an old template label of "focus mode") is skipped and listed in the report.
- It reports, per field: confidence histograms for right versus wrong answers, both for the fast pass and after thinking; the fast-pass error rate; and the cap-hit rate.
- It proposes settings:
  - Expensive fields: the lowest line that accepts zero labeled errors.
  - Cheap fields: the lowest line that accepts no more than 10% errors.
  - `skip_thinking`: every cheap field whose fast-pass error is 10% or less, plus the ones in `SKIP_THINKING` (empty since effort got a hand fast line instead). A field with a hand fast line never skips.
  - A think line in `THINK_STOPS` is set by hand instead (credit 0.999), and so is a fast line in `FAST_LINES` (effort 0.80); the report shows what the rule would give.
  - Before the lines, option offsets for the fast pass (type, template, effort, format, credit): fitted to the labels with an L2 pull towards 0 and kept only when they add 3% of the field's labeled cells held out (5-fold). The lines are then fitted on the shifted probes.
  - After the lines, two temperatures per field, fitted to the decisions those lines make: one for decisions from the fast pass, one for decisions from a thinking probe. The report shows the calibration error (ECE) raw and calibrated, held out.
- `--mode joint` records each assignment's fields together, from one prompt and one shared chain (`data/eval/calibrate-joint.jsonl`), and proposes `decisions/lines.joint.json` the same way. Its report adds the shared chain's length against the same loops run one per field.
- Template and effort are recorded with System One's type answer, and a template the type settles isn't recorded: the report scores it as the default. For type and template it also counts what would go to clarify and how many errors that catches.
- It prints the proposal and waits for Max's approval before writing `decisions/lines.json`.
- Go/no-go is Max's call, made from this report. If the expensive fields can't be decided instantly at any useful rate, and the reversed-order check doesn't fix that, Part B is skipped.

`compare` runs six setups on the labeled set:

1. All Spark: Muse Spark with structured output, one call per item covering all of its fields. Cost comes from the `usage` the API returns, at Standard-mode prices: $1.25 per million input tokens, $0.15 per million cached input tokens, $4.25 per million output tokens.
2. Base Glimmer: `/v1/chat/completions` at reasoning strength low, one call per item covering all of its fields, with the answers parsed from the text.
3. System One, fast pass only: taken from the records of setup 4.
4. System One, full: the decider with `lines.json`.
5. System One, shared chain: the decider in joint mode with `lines.joint.json` (test questions per field, as in setup 4).
6. Liquid d1-2B: a 2B decision model on its own llama-server (`D1_URL`, default port 8081), every field of an item as a choice question in one `/v1/systemone` call. Each option is a criterion named by its value, and the question's instructions carry the field's rules and worked examples, which the other setups get in their system turn.

Rules for the comparison:

- Every setup uses identical question text and options from `decisions/fields.py`.
- Metrics:
  - Accuracy per field and per cost group.
  - Seconds per item, measured by wall clock.
  - Cost per 1,000 items. For Glimmer, price GPU seconds at $1.10 an hour (Modal's A10, as a rental proxy), and note that on Max's own card the real cost is electricity.
  - For System One, the share of fields by tier, and the cap-hit rate.
  - Effort also within one level, since its options are in order of size.
- Outputs: `eval/results.csv`, `eval/summary.md`, and `eval/compare.png`, a chart of accuracy against seconds per item (log x axis) with one labeled point per setup and its cost per 1,000 items in the label. `data/eval/audit.md` (gitignored: it quotes item text) lists the labels Spark, base Glimmer and System One all miss.

## Design rules

- `decisions/fields.py` is the only source of field definitions and option order.
- Spark never makes categorical judgments. Glimmer never transcribes.
- Stop on confidence, never on agreement between consecutive probes.
- The cached prefix must be byte-identical across requests.
- The core app works with the decider off. A card always gets created, whether or not the decider answers.

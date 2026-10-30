# Stretch: assignment dry run

Parked. CLAUDE.md puts this in the Stretch tier: it runs only if the core is green by Saturday noon, and it gets cut unless its traps match where the student actually struggled on three finished assignments. Nothing in `app/` imports from this folder, so leaving it untouched costs nothing.

## What it does

A model (Muse Wonder by default, or Muse Spark, which takes the same requests) attempts one assignment the way a capable student would. While it works it logs, through two tools, each time it has to look something up, gets stuck, backtracks, or hits an ambiguous instruction. Then it returns:

- **Traps**: up to 5 places a student is likely to lose time or points. Each has where it is, the exact words from the materials it's about, what goes wrong (without the answer), a concrete first step past it, and the rubric criterion it puts at risk.
- **Effort**: light, medium or heavy, with reasons. No minute estimates.
- **What completing it looks like in practice**: the phases in order (what you do, what exists at the end, what you need, which ones are hardest), a done checklist for the finished submission, and the parts that can't be attempted outside class.

```
 Canvas assignment                     model (private reasoning)              saved in dry_runs
 ─────────────────                     ─────────────────────────              ─────────────────
 instructions ─┐                      ┌─ look_up(where, what) ──────┐
 rubric names ─┼─ materials() ──────▶ │  log_trouble(kind, where)   ├──▶ log #1, #2, ...
 links        ─┤   plain text only    │  ...attempt continues...    │
 PDF text     ─┘   no grades,         └─ final report ──────────────┘
                   no feedback                     │
                                                   ▼
                                    code checks each trap:
                                      quote_found: the quote is really in the materials
                                      observed:    it cites a log entry made during the attempt
                                                   │
                       ┌───────────────────────────┴───────────────────────────┐
                       ▼                                                       ▼
          ranked_traps() / apply_to_card()                         evaluate.py (keep or cut)
          traps on criteria this student lost                      traps vs. criteria the student
          points on before go first                                actually lost points on
```

## Commands

From the repo root, venv active. `pip install -e ".[stretch]"` once, which adds pypdf so attachment PDFs can be read as text.

```
python -m stretch.dry_run count 12          # input tokens and cost range; free
python -m stretch.dry_run run 12            # Muse Wonder, high effort, $1.00 budget
python -m stretch.dry_run run 12 --model muse-spark-1.3 --effort medium
python -m stretch.dry_run show 12           # latest saved run
python -m stretch.dry_run apply 12          # effort + top trap onto the card
python -m stretch.evaluate                  # finished assignments with feedback to test on
python -m stretch.evaluate 3 7 9            # their runs compared with the feedback
python -m pytest stretch                    # tests; the main run leaves this folder alone
```

`run` skips an assignment whose materials haven't changed since the last run with the same model; `--force` overrides that.

Suggested order: tune the prompt on Spark (input $1.25/M, output $4.25/M), then run Wonder (input $4/M, output $20/M) on the three evaluation assignments.

## Design choices

- **The dry run never sees grades or feedback.** It gets the instructions, rubric criterion names with their points, links and attachment text. It never gets points earned, rubric comments, submission comments or the score, and a test checks this. That keeps `evaluate.py` a fair test: a trap can't match a struggle it was told about. The student's history comes in afterwards, when `ranked_traps()` orders the traps.
- **The solution is never shown or stored.** The attempt happens in the model's reasoning, which the API returns only as encrypted content for replay, never as text. No report field asks for an answer, and the instructions forbid answers in tool arguments and in the report. That rule is enforced by the instructions, not by code; logged tool arguments are capped at 300 characters.
- **Observed beats recalled.** A trap that cites a log entry was noted while the model was working, so it doesn't depend on the model remembering afterwards where it struggled. `show` marks each trap with the checks it passed.
- **Text only.** Following CLAUDE.md, images and scanned PDFs are listed as unread instead of sent.
- **Stateless tool loop.** Each turn uses `store: false` and replays the encrypted reasoning (`include: ["reasoning.encrypted_content"]`), so the model keeps its place and Meta keeps no copy. The resent prefix bills at the cached rate when the cache hits. `prompt_cache_key` keeps runs on the same backend.
- **Streaming.** Meta's Responses API docs say long non-streaming requests can time out with HTTP 504, and streaming requests are exempt.
- **Contributor model ids are refused.** Standard tier only.
- **Every run is kept** in a `dry_runs` table in the app's SQLite DB (created on first use), so Spark and Wonder runs on the same assignment can be compared.

## Cost

Unmeasured. What is known, from [Meta's pricing page](https://dev.meta.ai/docs/pricing-rate-limits) (checked 2026-10-30):

| | Wonder | Spark 1.3 |
| --- | --- | --- |
| Input / cached input / output, per 1M tokens | $4.00 / $0.20 / $20.00 | $1.25 / $0.15 / $4.25 |
| Worst-case output per turn (32k tokens, reasoning included) | $0.64 | $0.14 |

Output, mostly reasoning tokens, will dominate, and nobody knows yet how many tokens an attempt takes. `count` gives the exact input side for free. Each saved run records turns, input, cached, output and reasoning tokens, plus the dollar cost, so the first few runs answer the question. The run stops before another turn once its budget is spent, but a turn that's already running finishes, so one run can overshoot by up to the worst-case turn above.

## Notes from the Meta docs (checked 2026-10-30)

- **The 1 MiB limit** that CLAUDE.md cites from a third-party writeup is, per [Meta's Responses API page](https://dev.meta.ai/docs/protocols/responses), the cap on *background* request bodies (`background: true`). Ordinary requests allow 50 MB of inline media. Text-only input is still the plan here.
- **Cache writes**: the pricing page lists a discounted cached-input rate and no write charge. The [prompt caching page](https://dev.meta.ai/docs/prompt-caching) says caching is automatic and that `cached_tokens` is a subset of `input_tokens`. Confirm on the first runs that the bill matches what `usage` reports.
- **`"max"` effort** on `muse-wonder-1.0` doesn't select Muse Wonder Full, which exists only in Muse Code.

## Open questions before trusting it

- Does Wonder call the logging tools while it works, or save everything for the report? If `observed` is rarely true, the tools aren't earning their extra turns.
- How often do quotes fail the check? A high failure rate means traps aren't anchored to the real instructions.
- The streaming parser reads the final response from the terminal event's `response` field. That is OpenAI's event shape, which Meta says it follows; check it on the first real call.
- `evaluate.py` matches only by rubric criterion. The real Canvas snapshot has 1 scored rubric and 4 teacher comments, so most of the judging will be by eye.

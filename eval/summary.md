# System One comparison

Run Nov 01, 2026 on the labeled set: 197 assignments and 51 test questions, 723 labeled fields. Glimmer is Muse-Glimmer-30B-KQuant-17GB-Q4_K_M on the RTX 3090 (seed 7, prompt version 10); Spark is muse-spark-1.3. Every setup gets the same item text and the same questions and options from `decisions/fields.py`.

| Setup | Accuracy | Expensive fields | Cheap fields | Seconds per item | Cost per 1,000 items |
|---|---|---|---|---|---|
| All Spark (248 items) | 94.7% | 96.3% | 91.7% | 7.50 | $9.89 |
| Base Glimmer (248 items) | 92.7% | 94.2% | 89.7% | 9.13 | $2.79 |
| System One, fast pass only (248 items) | 88.8% | 90.6% | 85.1% | 1.45 | $0.44 |
| System One, full (248 items) | 92.3% | 93.3% | 90.1% | 7.10 | $2.17 |
| System One, shared chain (248 items) | 92.4% | 94.4% | 88.4% | 8.21 | $2.51 |
| Liquid d1-2B (248 items) | 78.3% | 84.2% | 66.5% | 0.20 | $0.062 |

## Accuracy by field

| Field | Cost | All Spark | Base Glimmer | System One, fast pass only | System One, full | System One, shared chain | Liquid d1-2B |
|---|---|---|---|---|---|---|---|
| type (189) | expensive | 96.8% | 95.2% | 90.5% | 93.7% | 94.7% | 89.4% |
| template (190) | expensive | 95.8% | 92.1% | 88.9% | 92.1% | 92.1% | 77.9% |
| effort (191) | cheap | 89.5% | 88.5% | 81.7% | 88.0% | 85.9% | 64.9% |
| effort, within one level | cheap | 99.5% | 98.4% | 98.4% | 99.0% | 99.0% | 95.3% |
| format (51) | cheap | 100.0% | 94.1% | 98.0% | 98.0% | 98.0% | 72.5% |
| topic (51) | expensive | 96.1% | 100.0% | 96.1% | 98.0% | 100.0% | 100.0% |
| credit (51) | expensive | 96.1% | 92.2% | 92.2% | 92.2% | 96.1% | 72.5% |

## System One, full: tiers

| Field | Instant | Thought | Guessed | From the type | Thinking loops | Hit the cap | To clarify (wrong) |
|---|---|---|---|---|---|---|---|
| type | 46.0% | 43.9% | 10.1% | 0.0% | 102 | 1 (1.0%) | 19 (7) |
| template | 18.4% | 26.8% | 11.1% | 43.7% | 72 | 3 (4.2%) | 21 (8) |
| effort | 80.1% | 7.9% | 12.0% | 0.0% | 38 | 2 (5.3%) | 0 (0) |
| format | 96.1% | 0.0% | 3.9% | 0.0% | 0 | 0 (-) | 0 (0) |
| topic | 96.1% | 3.9% | 0.0% | 0.0% | 2 | 0 (0.0%) | 0 (0) |
| credit | 90.2% | 7.8% | 2.0% | 0.0% | 5 | 1 (20.0%) | 0 (0) |
| all | 58.0% | 21.4% | 9.1% | 11.5% | 219 | 7 (3.2%) | 40 (15) |

Lines: `data/eval/proposal.v10.json` = `{"lines": {"type": {"fast": 0.99947, "think": 1.01, "temperature": 0.5, "think_temperature": 2.16}, "template": {"fast": 0.9949, "think": 1.01, "temperature": 0.5, "think_temperature": 3.35}, "effort": {"fast": 0.8, "think": 1.01, "temperature": 1.39, "think_temperature": 4.72}, "format": {"fast": 0.71, "think": 0.9999, "temperature": 1.04, "think_temperature": 1.0}, "topic": {"fast": 0.75, "think": 0.97, "temperature": 0.5, "think_temperature": 0.5}, "credit": {"fast": 0.59, "think": 0.999, "temperature": 0.5, "think_temperature": 4.49, "offsets": {"full": 1.884, "partial": 0.416, "none": -1.918, "cant_tell": -0.382}}}, "skip_thinking": ["format"]}`.

## System One, shared chain: tiers

| Field | Instant | Thought | Guessed | From the type | Thinking loops | Hit the cap | To clarify (wrong) |
|---|---|---|---|---|---|---|---|
| type | 50.8% | 37.0% | 12.2% | 0.0% | 93 | 11 (11.8%) | 23 (6) |
| template | 12.1% | 27.4% | 16.3% | 44.2% | 83 | 13 (15.7%) | 31 (11) |
| effort | 82.2% | 11.5% | 6.3% | 0.0% | 34 | 6 (17.6%) | 0 (0) |
| format | 96.1% | 0.0% | 3.9% | 0.0% | 0 | 0 (-) | 0 (0) |
| topic | 94.1% | 5.9% | 0.0% | 0.0% | 3 | 0 (0.0%) | 0 (0) |
| credit | 92.2% | 7.8% | 0.0% | 0.0% | 4 | 0 (0.0%) | 0 (0) |
| all | 58.1% | 20.9% | 9.4% | 11.6% | 217 | 30 (13.8%) | 54 (17) |

Lines: `data/eval/proposal-joint.v10.json` = `{"lines": {"type": {"fast": 0.999929, "think": 1.01, "temperature": 0.5, "think_temperature": 2.38}, "template": {"fast": 0.9987, "think": 1.01, "temperature": 0.5, "think_temperature": 3.35}, "effort": {"fast": 0.8, "think": 1.01, "temperature": 1.87, "think_temperature": 5.73}}, "skip_thinking": []}`.

Its test questions are decided per field with `data/eval/proposal.v10.json`, as in System One, full.

## How the numbers are made

- Seconds per item is wall clock per call. The decider gets a graded test's questions in one call, as Part B will, so each question counts that call's time divided by its questions. The fast pass alone is timed inside the same runs (the forward probe, plus the reversed probe when the forward one clears) and answers with the forward probe's top option.
- Spark's cost is the `usage` the API returned: 899,352 input tokens (211,304 cached) and 367,219 output tokens (361,361 of them reasoning, at Spark's default effort) over 248 calls, at $1.25, $0.15 and $4.25 per million (Standard mode): $2.45 in all.
- Glimmer's cost prices GPU seconds at $1.10 an hour (Modal's A10, as a rental proxy). On Max's own card the real cost is electricity.
- Base Glimmer: 0 answers couldn't be read from its reply and count as wrong; 0 replies hit the 4096-token limit.
- Liquid d1-2B answers every field of an item in one `/v1/systemone` call, each a choice question whose instructions hold the field's question, rules and worked examples, with each option a criterion. Its GPU seconds are priced like Glimmer's. 0 answers weren't one of the options and count as wrong.
- Within one level: effort's options run from one short sitting to multi-day, so an answer one size off the label counts there.
- Skipped labels: 21 (used as a prompt example).

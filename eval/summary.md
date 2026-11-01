# System One comparison

Run Nov 01, 2026 on the labeled set: 197 assignments and 51 test questions, 723 labeled fields. Glimmer is muse-glimmer-30B on the RTX 3090 (seed 7, prompt version 9); Spark is muse-spark-1.3. Every setup gets the same item text and the same questions and options from `decisions/fields.py`.

| Setup | Accuracy | Expensive fields | Cheap fields | Seconds per item | Cost per 1,000 items |
|---|---|---|---|---|---|
| All Spark (248 items) | 88.8% | 92.1% | 82.2% | 5.30 | $9.04 |
| Base Glimmer (248 items) | 87.6% | 90.0% | 82.6% | 9.31 | $2.85 |
| System One, fast pass only (248 items) | 83.3% | 86.1% | 77.7% | 1.02 | $0.31 |
| System One, full (248 items) | 87.7% | 91.5% | 80.2% | 11.38 | $3.48 |

## Accuracy by field

| Field | Cost | All Spark | Base Glimmer | System One, fast pass only | System One, full |
|---|---|---|---|---|---|
| type (189) | expensive | 92.1% | 92.6% | 88.4% | 93.1% |
| template (190) | expensive | 89.5% | 87.4% | 84.7% | 86.3% |
| effort (191) | cheap | 78.0% | 79.1% | 72.8% | 75.9% |
| format (51) | cheap | 98.0% | 96.1% | 96.1% | 96.1% |
| topic (51) | expensive | 98.0% | 100.0% | 96.1% | 98.0% |
| credit (51) | expensive | 96.1% | 80.4% | 72.5% | 98.0% |

## System One tiers

| Field | Instant | Thought | Guessed | From the type | Thinking loops | Hit the cap | To clarify (wrong) |
|---|---|---|---|---|---|---|---|
| type | 39.7% | 48.7% | 11.6% | 0.0% | 114 | 5 (4.4%) | 22 (7) |
| template | 10.0% | 35.8% | 11.1% | 43.2% | 89 | 2 (2.2%) | 21 (12) |
| effort | 31.4% | 43.5% | 25.1% | 0.0% | 131 | 9 (6.9%) | 0 (0) |
| format | 94.1% | 0.0% | 5.9% | 0.0% | 0 | 0 (-) | 0 (0) |
| topic | 94.1% | 3.9% | 2.0% | 0.0% | 3 | 0 (0.0%) | 0 (0) |
| credit | 45.1% | 54.9% | 0.0% | 0.0% | 28 | 0 (0.0%) | 0 (0) |
| all | 37.8% | 37.8% | 13.1% | 11.3% | 365 | 16 (4.4%) | 43 (19) |

Lines: `data/eval/proposal.v9.json` = `{"lines": {"type": {"fast": 0.9987, "think": 1.01}, "template": {"fast": 0.9977, "think": 1.01}, "effort": {"fast": 0.988, "think": 1.01}, "format": {"fast": 0.64, "think": 0.99}, "topic": {"fast": 0.86, "think": 1.01}, "credit": {"fast": 0.86, "think": 0.999}}, "skip_thinking": ["format"]}`.

## How the numbers are made

- Seconds per item is wall clock per call. The decider gets a graded test's questions in one call, as Part B will, so each question counts that call's time divided by its questions. The fast pass alone is timed inside the same runs (the forward probe, plus the reversed probe when the forward one clears) and answers with the forward probe's top option.
- Spark's cost is the `usage` the API returned: 589,742 input tokens (148,389 cached) and 392,690 output tokens (386,817 of them reasoning, at Spark's default effort) over 248 calls, at $1.25, $0.15 and $4.25 per million (Standard mode): $2.24 in all.
- Glimmer's cost prices GPU seconds at $1.10 an hour (Modal's A10, as a rental proxy). On Max's own card the real cost is electricity.
- Base Glimmer: 0 answers couldn't be read from its reply and count as wrong; 0 replies hit the 4096-token limit.
- Skipped labels: 21 (used as a prompt example).

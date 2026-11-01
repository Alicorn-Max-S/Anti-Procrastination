# System One comparison

Run Nov 01, 2026 on the labeled set: 197 assignments and 51 test questions, 723 labeled fields. Glimmer is muse-glimmer-30B on the RTX 3090 (seed 7, prompt version 6); Spark is muse-spark-1.3. Every setup gets the same item text and the same questions and options from `decisions/fields.py`.

| Setup | Accuracy | Expensive fields | Cheap fields | Seconds per item | Cost per 1,000 items |
|---|---|---|---|---|---|
| All Spark (248 items) | 83.8% | 85.2% | 81.0% | 5.48 | $8.72 |
| Base Glimmer (248 items) | 83.5% | 86.3% | 78.1% | 8.85 | $2.70 |

## Accuracy by field

| Field | Cost | All Spark | Base Glimmer |
|---|---|---|---|
| type (189) | expensive | 88.9% | 88.4% |
| template (190) | expensive | 75.8% | 80.5% |
| effort (191) | cheap | 76.4% | 73.3% |
| format (51) | cheap | 98.0% | 96.1% |
| topic (51) | expensive | 96.1% | 100.0% |
| credit (51) | expensive | 96.1% | 86.3% |

## How the numbers are made

- Seconds per item is wall clock per call. The decider gets a graded test's questions in one call, as Part B will, so each question counts that call's time divided by its questions. The fast pass alone is timed inside the same runs (the forward probe, plus the reversed probe when the forward one clears) and answers with the forward probe's top option.
- Spark's cost is the `usage` the API returned: 498,981 input tokens (181,040 cached) and 408,738 output tokens (402,915 of them reasoning, at Spark's default effort) over 248 calls, at $1.25, $0.15 and $4.25 per million (Standard mode): $2.16 in all.
- Glimmer's cost prices GPU seconds at $1.10 an hour (Modal's A10, as a rental proxy). On Max's own card the real cost is electricity.
- Base Glimmer: 0 answers couldn't be read from its reply and count as wrong; 0 replies hit the 4096-token limit.
- Skipped labels: 21 (used as a prompt example).

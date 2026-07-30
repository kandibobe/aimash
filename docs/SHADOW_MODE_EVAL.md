# Shadow Mode Evaluation Loop

## Purpose

Turn Shadow Mode from a shadow recommendation generator into a measurable quality-control loop.

## Core metrics

```yaml
shadow_run_id: string
date: YYYY-MM-DD
recommendation_count: int
useful_alert_count: int
wasted_alert_count: int
false_positive_rate: number|null
false_negative_candidates: int
actionability_score: 0-100
```

## Evaluation questions

- Did the recommendation point to a real operator-relevant issue?
- Would acting on it likely help?
- Was it redundant/noisy?
- Was there a missed issue that should have been surfaced?

## Output use

- weekly quality review
- routing/prompt improvement
- threshold tuning
- wasted alert reduction

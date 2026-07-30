# Unified Account Health Score

## Purpose

Give the operator one fast account-level prioritization signal for standups and watchdog summaries.

## Output

```yaml
account_health_score: 0-100
health_band: green|yellow|red
drivers:
  - label: string
    impact: high|medium|low
    direction: positive|negative
```

## Recommended components

| Component | Weight | Notes |
|---|---:|---|
| Spend pacing | 20 | Over/under pacing vs expected day progress |
| CPA vs target | 20 | Null-safe if target unavailable |
| ROAS vs target | 15 | Null-safe if target unavailable |
| Conversion trend | 15 | Day/day or short trend |
| Waste score | 15 | Search-term leakage, irrelevant spend |
| Drift risk | 5 | Config/state anomalies, paused critical monitoring |
| Recommendation pressure | 10 | Count and severity of current decision objects |

## Rules

- Score should degrade quickly on severe P0/P1 issues.
- Missing metrics should not default to catastrophic failure.
- Health score must be explainable via top drivers.

## Render target

In operator brief and standup:
- score
- band
- top 3 negative drivers

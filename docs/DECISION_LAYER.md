# AdMaster Decision Layer

## Purpose

Standardize how monitoring, audits, and waste-mining flows express recommendations.

The system should not emit only raw findings. It should emit structured decision objects that can be:
- shown in Telegram alerts
- grouped in standups
- ranked worst-first
- fed into approvals
- evaluated in Shadow Mode

## Decision object schema

```yaml
decision_code: string
source_lane: monitoring|audit|waste_mining|shadow_eval
entity_type: account|campaign|adgroup|search_term|keyword|budget
entity_id: string
entity_name: string
severity: P0|P1|P2|P3
what_happened: string
why: string
recommended_action: string
do_not_do: string
confidence: low|medium|high
source_tools:
  - get_account_audit
  - get_budgets
metrics:
  spend_ratio: number|null
  cpa_vs_target: number|null
  roas_vs_target: number|null
  conv_delta_pct: number|null
  wasted_spend: number|null
```

## Rules

1. Every alert-worthy output should be expressible as one or more decision objects.
2. Monitoring lane must stay detection/diagnosis only — not silent mutation advice hidden as prose.
3. Proposal lane can transform decision objects into approval cards.
4. Mutation lane must only run after explicit approval.

## Minimum lane mapping

- Morning Standup -> grouped decision objects + account summary
- Hourly Watchdog -> urgent decision objects only
- Weekly Janitor / Waste lane -> waste-oriented decision objects
- Shadow Mode -> same schema + evaluation fields

## Recommended rendering for Telegram

Per decision object:
- what happened
- severity
- why
- what to do
- what NOT to do
- confidence

## Next implementation step

Create code/schema helper, then retrofit the prompts/jobs:
- Morning Standup
- Hourly Watchdog
- Weekly Janitor
- Shadow Mode

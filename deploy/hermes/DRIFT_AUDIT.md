# AdMaster Drift Audit

## Purpose

Detect operational drift across:
- canonical runtime registry
- cron registry
- live cron state
- deploy template config
- selected skills
- selected docs

## Severity levels
- `P0`: current live risk or blind spot (for example paused critical job)
- `P1`: conflicting present-tense truth across sources
- `P2`: stale but low-risk wording
- `P3`: historical/organizational cleanup

## Required output for each finding

```yaml
drift_code: string
severity: P0|P1|P2|P3
source_a: path-or-live-surface
source_b: path-or-live-surface
current_live: string
issue: string
recommended_fix: string
```

## First mandatory checks

1. `runtime_registry.yaml` vs `deploy/hermes/config.yaml`
2. `runtime_registry.yaml` vs `/root/.hermes/cron/jobs.json`
3. `cron_registry.yaml` vs `/root/.hermes/cron/jobs.json`
4. `runtime_registry.yaml` vs key skills:
   - `/root/.hermes/skills/ad-master/ad-master-agent/SKILL.md`
   - `/root/.hermes/skills/ad-master/aimash-development/SKILL.md`
   - `/root/.hermes/skills/ad-master/ad-master-cron-ops/SKILL.md`
5. runtime statements in docs:
   - `README.md`
   - `docs/TZ-Aimash-Hermes-Agent.md`
   - `deploy/hermes/README.md`
   - `deploy/hermes/OPERATIONS.md`

## Must-detect examples

- Deploy template still points to old main runtime while canonical runtime says otherwise.
- Live cron jobs pinned to old model but not listed as pinned exceptions.
- P0 cron job paused.
- Skill states old runtime in present tense.
- Docs or skills mention timezone/model/provider values that are historical and not labelled as such.

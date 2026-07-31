# Документация Aimash

Единственный нормативный продуктовый документ — [`SPEC.md`](../SPEC.md). Если тематический документ,
runbook или старое ТЗ расходится с ним, прав `SPEC.md`.

## Production и эксплуатация

- [`USER_GUIDE.md`](USER_GUIDE.md) — пользовательские сценарии.
- [`UAT_PLAN.md`](UAT_PLAN.md) и [`ACCEPTANCE.md`](ACCEPTANCE.md) — legacy/UAT evidence.
- [`HANDOVER.md`](HANDOVER.md) — передача и доступы.
- [`HERMES_PRODUCTION_GAP.md`](HERMES_PRODUCTION_GAP.md) — живые проверки и незакрытый UAT.
- [`DEPLOYMENT.md`](DEPLOYMENT.md) — Docker и production deploy.
- [`RUNBOOK_ENV.md`](RUNBOOK_ENV.md) — переменные окружения.
- [`RUNBOOK_ACCESS.md`](RUNBOOK_ACCESS.md) — allowlist, аккаунты и администраторы.
- [`BACKUP.md`](BACKUP.md) — encrypted off-host backup и restore.
- [`DATABASE.md`](DATABASE.md) — PostgreSQL/Alembic.
- [`TESTING.md`](TESTING.md) — тесты и smoke-проверки.
- [`../deploy/hermes/OPERATIONS.md`](../deploy/hermes/OPERATIONS.md) — Hermes gateway day-2 runbook.
- [`../deploy/hermes/SAFE_RESTART.md`](../deploy/hermes/SAFE_RESTART.md) — два Telegram-контура.
- [`../deploy/hermes/README.md`](../deploy/hermes/README.md) — установка Hermes.
- [`../deploy/hermes/DRIFT_AUDIT.md`](../deploy/hermes/DRIFT_AUDIT.md) — repo/runtime drift.
- [`../deploy/hermes/OPEN_DECISIONS.md`](../deploy/hermes/OPEN_DECISIONS.md) — настройки владельца.
- [`../deploy/hermes/RISK_REGISTER.md`](../deploy/hermes/RISK_REGISTER.md) — эксплуатационные риски.
- [`../deploy/hermes/SOUL.md`](../deploy/hermes/SOUL.md) — runtime-инструкции агента.
- [`../deploy/hermes/host-a/RUNBOOK.md`](../deploy/hermes/host-a/RUNBOOK.md) — опциональная host-a схема.

## Безопасность и API

- [`SECURITY.md`](SECURITY.md) — технические инварианты и карта тестов.
- [`MUTATIONS.md`](MUTATIONS.md) — typed mutation path и confirm policy.
- [`OAUTH_SETUP.md`](OAUTH_SETUP.md) — Google Ads OAuth/MCC.
- [`gads-api-refs.md`](gads-api-refs.md) — версия и sunset Google Ads API.
- [`REPO_GUARDRAILS.md`](REPO_GUARDRAILS.md) — branch/deploy guardrails.

## Функциональные референсы

Эти документы объясняют данные, форматы и существующий код, но не вводят отдельный продуктовый UX:

- [`REPORTS.md`](REPORTS.md), [`KEYWORD_RESEARCH.md`](KEYWORD_RESEARCH.md);
- [`CAMPAIGN_WIZARD.md`](CAMPAIGN_WIZARD.md), [`GDN_CAMPAIGNS.md`](GDN_CAMPAIGNS.md);
- [`CLIENTS_KB.md`](CLIENTS_KB.md), [`SCHEDULER.md`](SCHEDULER.md);
- [`DECISION_LAYER.md`](DECISION_LAYER.md), [`ACCOUNT_HEALTH_SCORE.md`](ACCOUNT_HEALTH_SCORE.md).
- [`DAILY_OPERATOR_BRIEF.md`](DAILY_OPERATOR_BRIEF.md), [`WASTE_MINING_LANE.md`](WASTE_MINING_LANE.md),
  [`SHADOW_MODE_EVAL.md`](SHADOW_MODE_EVAL.md).
- [`section19-spec.md`](section19-spec.md), [`gap-analysis-section19.md`](gap-analysis-section19.md).

## Исследования и провенанс

- [`AUDIT-open-source.md`](AUDIT-open-source.md), [`REUSE-MAP.md`](REUSE-MAP.md).
- [`reuse-sources.md`](reuse-sources.md), [`ab-results.md`](ab-results.md).

Старые `SPEC`, `HERMES_SPEC`, `AGENTIC_VS_TZ` и pivot-документ перемещены в
[`archive/pre-single-spec-2026-07/`](archive/pre-single-spec-2026-07/) с маркировкой `NON-NORMATIVE`.
Они нужны только для истории решений и contract traceability.

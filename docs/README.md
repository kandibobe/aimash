# Документация Aimash

Источник истины по продукту — [`ТЗ.md`](../ТЗ.md); правила разработки и золотые правила —
[`CLAUDE.md`](../CLAUDE.md); обзор и команды — корневой [`README.md`](../README.md). Ниже —
тематические гайды.

## Для заказчика / менеджера
- [USER_GUIDE.md](USER_GUIDE.md) — **руководство пользователя** (не для разработчика): сценарии, команды, FAQ.
- [UAT_PLAN.md](UAT_PLAN.md) — план ручного приёмочного тестирования (7 сессий, чек-листы, скрины).
- [ACCEPTANCE.md](ACCEPTANCE.md) — критерии приёмки §1–18 + §19/§20 с привязкой к коду/тестам.
- [HANDOVER.md](HANDOVER.md) — передача проекта заказчику: доступы, деплой, pre-delivery чек-лист.

## Старт и эксплуатация
- [OAUTH_SETUP.md](OAUTH_SETUP.md) — доступы Google Ads с нуля: test MCC, developer token, OAuth-клиент, refresh token.
- [DEPLOYMENT.md](DEPLOYMENT.md) — `.env`, секреты, Fernet-ключ, Docker, prod-чеклист, Google Sheets scope.
- [DATABASE.md](DATABASE.md) — схема таблиц, миграции Alembic, dev/SQLite vs prod/Postgres.
- [BACKUP.md](BACKUP.md) — бэкап/restore Postgres (в т.ч. PII клиентов §20).
- [TESTING.md](TESTING.md) — как устроены и гоняются офлайн-тесты; паттерн фейка SDK; smoke-доступ.

## Безопасность
- [SECURITY.md](SECURITY.md) — 10 золотых правил → где реализовано → чем покрыто (артефакт для ревью).

## Фичи
- [CAMPAIGN_WIZARD.md](CAMPAIGN_WIZARD.md) — §19 визард `/newcampaign`: 8 этапов, черновики, Sheets round-trip.
- [CLIENTS_KB.md](CLIENTS_KB.md) — §20 `/clients`: профиль клиента, LLM-разбор текста, краулер сайта.
- [REPORTS.md](REPORTS.md) — `/report` `/export` `/sheets` `/mcc`: периоды, метрики, разбивки, экспорт.
- [KEYWORD_RESEARCH.md](KEYWORD_RESEARCH.md) — `/keywords`: подбор идей, метрики, AI-кластеризация, `.xlsx`.
- [GDN_CAMPAIGNS.md](GDN_CAMPAIGNS.md) — кампания из фото/видео (§11): GDN/Video/Demand Gen, confirm-флоу.
- [MUTATIONS.md](MUTATIONS.md) — карта изменяющих операций Google Ads и confirm-гейта.
- [SCHEDULER.md](SCHEDULER.md) — плановые отчёты/аномалии/очистка (read-only).

## Технические референсы
- [gads-api-refs.md](gads-api-refs.md) — версии Google Ads API/SDK, график сансета.
- [reuse-sources.md](reuse-sources.md) — что переиспользуем из внешних репозиториев + MCP-нюансы.
- [ab-results.md](ab-results.md) — результаты A/B-теста моделей (стоимость/точность).

## Скилы разработчика (`.claude/skills/`)
`new-mutation` · `confirm-gate-audit` · `gaql-query` · `check-rsa-copy` · `gads-version`.

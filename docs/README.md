# Документация Aimash

Источник истины по продукту — [`ТЗ.md`](../ТЗ.md); правила разработки и золотые правила —
[`CLAUDE.md`](../CLAUDE.md); обзор и команды — корневой [`README.md`](../README.md). Ниже —
тематические гайды.

## Старт и эксплуатация
- [OAUTH_SETUP.md](OAUTH_SETUP.md) — доступы Google Ads с нуля: test MCC, developer token, OAuth-клиент, refresh token.
- [DEPLOYMENT.md](DEPLOYMENT.md) — `.env`, секреты, Fernet-ключ, Docker, prod-чеклист, Google Sheets scope.
- [DATABASE.md](DATABASE.md) — схема таблиц, миграции Alembic, dev/SQLite vs prod/Postgres.
- [TESTING.md](TESTING.md) — как устроены и гоняются офлайн-тесты; паттерн фейка SDK; smoke-доступ.

## Безопасность
- [SECURITY.md](SECURITY.md) — 10 золотых правил → где реализовано → чем покрыто (артефакт для ревью).

## Фичи
- [REPORTS.md](REPORTS.md) — `/report` `/export` `/sheets`: периоды, метрики, разбивки, экспорт.
- [KEYWORD_RESEARCH.md](KEYWORD_RESEARCH.md) — `/keywords`: подбор идей, метрики, AI-кластеризация, `.xlsx`.
- [GDN_CAMPAIGNS.md](GDN_CAMPAIGNS.md) — кампания из фото (§11): подготовка ассетов, confirm-флоу.
- [SCHEDULER.md](SCHEDULER.md) — плановые отчёты/аномалии/очистка (read-only).

## Технические референсы
- [gads-api-refs.md](gads-api-refs.md) — версии Google Ads API/SDK, график сансета.
- [reuse-sources.md](reuse-sources.md) — что переиспользуем из внешних репозиториев + MCP-нюансы.
- [ab-results.md](ab-results.md) — результаты A/B-теста моделей (стоимость/точность).

## Скилы разработчика (`.claude/skills/`)
`new-mutation` · `confirm-gate-audit` · `gaql-query` · `check-rsa-copy` · `gads-version`.

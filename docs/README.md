# Документация Aimash

**Источник истины — три слоя:** [`SPEC.md`](../SPEC.md) — требования и приёмка ·
[`HERMES_SPEC.md`](../deploy/hermes/HERMES_SPEC.md) — архитектура ·
[`AGENTIC_VS_TZ.md`](../deploy/hermes/AGENTIC_VS_TZ.md) — обоснование.
[`ТЗ.md`](../ТЗ.md) — дословный текст трёх `.docx` заказчика («что было заказано»).
Правила разработки и 15 золотых правил — [`CLAUDE.md`](../CLAUDE.md); обзор — корневой
[`README.md`](../README.md). Ниже — тематические гайды.

**Точка входа в пивот** (не четвёртый источник истины, а вход; глубина — в трёх слоях выше) — три дока
в этом каталоге: [`TZ-Aimash-Hermes-Agent.md`](TZ-Aimash-Hermes-Agent.md) (сводное ТЗ),
[`AUDIT-open-source.md`](AUDIT-open-source.md) (аудит источников/библиотек/dev-MCP),
[`REUSE-MAP.md`](REUSE-MAP.md) (фреймворк / переиспользуем / строим).

> **Идёт пивот на ядро Hermes** (`SPEC.md` §5): свободный текст и подтверждение реплаем вместо
> кнопок. Доки ниже помечены по `SPEC.md` §17: **[ядро]** — действует как есть; **[переписывается]**
> — источник **функционального объёма**, механика переезжает в текстовую модель, документ не
> выбрасывается; **[заменяется]** — уходит целиком.

## Для заказчика / менеджера
- [USER_GUIDE.md](USER_GUIDE.md) — **руководство пользователя**: сценарии, команды, FAQ. **[заменяется]** на «что можно сказать агенту» (`SPEC.md` §2.6, приёмка П37).
- [UAT_PLAN.md](UAT_PLAN.md) — план ручного приёмочного тестирования (7 сессий, чек-листы, скрины). **[переписывается]** — сценарии остаются, кнопочные шаги становятся репликами.
- [ACCEPTANCE.md](ACCEPTANCE.md) — критерии приёмки §1–18 + §19/§20 с привязкой к коду/тестам. **[ядро]** — сверять статус по нему и по `git log`.
- [HANDOVER.md](HANDOVER.md) — передача проекта заказчику: доступы, деплой, pre-delivery чек-лист. **[ядро]**

## Старт и эксплуатация
- [OAUTH_SETUP.md](OAUTH_SETUP.md) — доступы Google Ads с нуля: test MCC, developer token, OAuth-клиент, refresh token.
- [DEPLOYMENT.md](DEPLOYMENT.md) — `.env`, секреты, Fernet-ключ, Docker, prod-чеклист, Google Sheets scope.
- [RUNBOOK_ENV.md](RUNBOOK_ENV.md) — **переменные на проде**: три слоя (код → `.env.defaults` → серверный `.env`), что дописать руками, как применить без даунтайма, откат.
- [RUNBOOK_ACCESS.md](RUNBOOK_ACCESS.md) — выдача доступов: whitelist, гранты на аккаунты, админы.
- [DATABASE.md](DATABASE.md) — схема таблиц, миграции Alembic, dev/SQLite vs prod/Postgres.
- [BACKUP.md](BACKUP.md) — бэкап/restore Postgres (в т.ч. PII клиентов §20).
- [TESTING.md](TESTING.md) — как устроены и гоняются офлайн-тесты; паттерн фейка SDK; smoke-доступ.

## Безопасность
- [SECURITY.md](SECURITY.md) — золотые правила → где реализовано → чем покрыто (артефакт для ревью). **[ядро]** — правил стало 15 (`CLAUDE.md`), карта покрытия здесь.

## Фичи — источники функционального объёма
> Все шесть **[переписываются]**: функции остаются, кнопочная механика переезжает в текстовую модель (`SPEC.md` §3.3–§3.8). Не выбрасывать — это единственное подробное описание того, что именно должно работать.

- [CAMPAIGN_WIZARD.md](CAMPAIGN_WIZARD.md) — §19 визард `/newcampaign`: 8 этапов, черновики, Sheets round-trip → диалог + состояние черновика (§3.5).
- [CLIENTS_KB.md](CLIENTS_KB.md) — §20 `/clients`: профиль клиента, LLM-разбор текста, краулер сайта → §3.8 (memory-инструменты, топик = клиент).
- [REPORTS.md](REPORTS.md) — `/report` `/export` `/sheets` `/mcc`: периоды, метрики, разбивки, экспорт → §3.7.
- [KEYWORD_RESEARCH.md](KEYWORD_RESEARCH.md) — `/keywords`: подбор идей, метрики, AI-кластеризация, `.xlsx` → §3.3.
- [GDN_CAMPAIGNS.md](GDN_CAMPAIGNS.md) — кампания из фото/видео (§11): GDN/Video/Demand Gen, confirm-флоу → §3.6.
- [OAUTH_SETUP.md](OAUTH_SETUP.md) — доступы (продублирован выше) — **[ядро]**, тул-слой ходит тем же OAuth.
- [MUTATIONS.md](MUTATIONS.md) — карта изменяющих операций Google Ads и confirm-гейта. **[ядро]**
- [SCHEDULER.md](SCHEDULER.md) — плановые отчёты/аномалии/очистка (read-only). **[ядро]** — но дом процесса переезжает (`SPEC.md` §5.3 C4).

## Технические референсы
- [gads-api-refs.md](gads-api-refs.md) — версии Google Ads API/SDK, график сансета.
- [reuse-sources.md](reuse-sources.md) — **провенанс порогов `/audit`**: какая проверка из какого MIT-источника (это лицензионная атрибуция, не заметка) + источники-паттерны. Сводный аудит открытых источников — в `AUDIT-open-source.md`.
- [ab-results.md](ab-results.md) — A/B моделей. ⚠️ Раздел «Решение» **историчен** (Фаза −1, выбирал `deepseek/deepseek-chat`); действующая раскладка по ролям — `SPEC.md` §10.1. Живым остаётся **замер**: Hermes-модели дали 0/11 function-calling на OpenRouter — на этом стоит выбор модели-мозга в `TZ-Aimash-Hermes-Agent.md`.

## Процесс, рубежи, эксплуатация ядра
- [REPO_GUARDRAILS.md](REPO_GUARDRAILS.md) — **`merge в master` == авто-деплой в прод**, значит право мержить == право снять confirm-гейт. Ruleset на ветку, machine-аккаунт билдера без merge, пустой bypass-list. ⚠️ `CODEOWNERS` без ruleset не энфорсит ничего.
- Эксплуатация ядра Hermes живёт рядом с самим ядром, в [`deploy/hermes/`](../deploy/hermes/):
  [`README.md`](../deploy/hermes/README.md) — установка (RB-0…RB-3) ·
  [`OPERATIONS.md`](../deploy/hermes/OPERATIONS.md) — день-2: редеплой↔MCP-reconnect, логи, откат, kill-switch, замеры §12 ·
  [`OPEN_DECISIONS.md`](../deploy/hermes/OPEN_DECISIONS.md) — решения заказчика D1–D7 (**настройка**, у каждого строгий дефолт) ·
  [`RISK_REGISTER.md`](../deploy/hermes/RISK_REGISTER.md) — риски Р1–Р9 (**подпись**, дефолта нет; приложение к договору) ·
  [`host-a/RUNBOOK.md`](../deploy/hermes/host-a/RUNBOOK.md) — двухсерверная схема; ⚠️ **ни один шаг не выполнялся живьём**.

## Архив
`docs/archive/` — вне зоны инвариантов ссылок (`tests/_docs_paths.py`), хранит документ таким, каким он был на момент заморозки.
- [archive/main-transplant-2026-07-27.md](archive/main-transplant-2026-07-27.md) — разбор orphan-ветки `main` с VPS: что из неё взято, что отклонено и почему (`DRY_RUN` рапортует об успехе несостоявшейся мутации; `tools_writes.py` даёт исполняющий `execute_confirmed` на MCP-поверхности без И8).

## Скилы разработчика (`.claude/skills/`)
`new-mutation` · `confirm-gate-audit` · `gaql-query` · `check-rsa-copy` · `gads-version`.

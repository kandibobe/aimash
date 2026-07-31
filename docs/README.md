# Документация Aimash

**Источник истины — три слоя:** [`SPEC.md`](../SPEC.md) — требования и приёмка ·
[`HERMES_SPEC.md`](../deploy/hermes/HERMES_SPEC.md) — архитектура ·
[`AGENTIC_VS_TZ.md`](../deploy/hermes/AGENTIC_VS_TZ.md) — обоснование.
[`ТЗ.md`](../ТЗ.md) — дословный текст трёх `.docx` заказчика («что было заказано»).
Правила разработки и 15 золотых правил — [`CLAUDE.md`](../CLAUDE.md) для Claude Code и
[`AGENTS.md`](../AGENTS.md) для Codex; их общее ядро защищено тестом от дрейфа. Обзор — корневой
[`README.md`](../README.md). Ниже — тематические гайды.

**Точка входа в пивот** (не четвёртый источник истины, а навигация; глубина — в трёх слоях
выше) — [`TZ-Aimash-Hermes-Agent.md`](TZ-Aimash-Hermes-Agent.md). Рядом:
[`AUDIT-open-source.md`](AUDIT-open-source.md) (аудит источников/библиотек/dev-MCP) и
[`REUSE-MAP.md`](REUSE-MAP.md) (фреймворк / переиспользуем / строим).

## Как читать документы во время перехода

**Целевая архитектура уже выбрана: ядро агентского цикла — Hermes.** Свободный текст заменяет
FSM-визарды и собственный `agent/loop.py`; карточка изменения подтверждается inline-кнопкой или
привязанным reply fallback. Это не открытое
архитектурное решение, а принятая цель (`SPEC.md` §5).

Одновременно **переход ещё не завершён**: развёрнутый `aimash-bot` всё ещё запускает `bot.main`, а
Hermes получает 25 READ + 1 META + 46 PLAN/state + 1 WRITE через MCP внутри этого контейнера. Trusted transport и
reply-CAS приняты живьём на Draft, но полный функциональный UAT исходных ТЗ ещё не закрыт. Поэтому в
репозитории временно сосуществуют два слоя.

| Метка | Что означает | Как использовать |
|---|---|---|
| **[целевая]** | Конечное устройство на Hermes | По ней принимаются новые архитектурные решения |
| **[переход]** | То, что фактически развёрнуто сегодня | По ней деплоят, диагностируют и откатывают текущий прод |
| **[legacy-референс]** | Детальное описание уже реализованной aiogram-механики | Из него переносят бизнес-правила и приёмку, **но не кнопочный UX** |
| **[историческая]** | Замер, аудит или замороженный снимок | Не используется как текущая инструкция |

Если вопрос звучит «как должно быть», читать `SPEC.md` + `HERMES_SPEC.md`. Если «что работает прямо сейчас» —
корневой `README.md`, живые тесты и `git log`. Если «как была реализована функция» — legacy-референс нужного раздела.

## Для заказчика / менеджера
- [USER_GUIDE.md](USER_GUIDE.md) — **[переход]**: сверху — целевые фразы для Hermes; ниже — пока что legacy-справка по развёрнутому кнопочному боту.
- [UAT_PLAN.md](UAT_PLAN.md) — **[legacy-референс]**: сценарии и ожидаемые результаты сохраняются; кнопочные шаги не принимают Hermes-контур.
- [HERMES_PRODUCTION_GAP.md](HERMES_PRODUCTION_GAP.md) — **[целевая]**: текущие P0/P1-разрывы и live-сценарии H1–H10 перед cutover.
- [ACCEPTANCE.md](ACCEPTANCE.md) — **[legacy-референс]** доказательств по старому aiogram-контуру. Целевая приёмка Hermes — `SPEC.md` §13 и `HERMES_SPEC.md` §12.
- [HANDOVER.md](HANDOVER.md) — **[переход]**: передача доступов и текущего прода; целевые Hermes-операции живут в `deploy/hermes/`.

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

## Операционный слой
- [DECISION_LAYER.md](DECISION_LAYER.md) — decision queue, incidents, pacing, experiments, RBAC/four-eyes, CRM, portfolio, playbooks и граница live-cutover.
- [ACCOUNT_HEALTH_SCORE.md](ACCOUNT_HEALTH_SCORE.md) — формула 0–100, семейные веса, grades и версия модели.
- [SHADOW_MODE_EVAL.md](SHADOW_MODE_EVAL.md) — что реально измеряет rollback shadow, критерии до auto-cutover.
- [DAILY_OPERATOR_BRIEF.md](DAILY_OPERATOR_BRIEF.md) — формат утреннего portfolio triage и приоритетов оператора.
- [WASTE_MINING_LANE.md](WASTE_MINING_LANE.md) — отдельный контур поиска потерь и его граница с мутациями.

## Фичи — источники функционального объёма
> Это **[legacy-референсы]**. Они фиксируют уже написанные бизнес-правила, форматы данных и тесты. В Hermes переносятся функция и
> приёмка, но не кнопки, callback-имена, FSM-этапы и не старый агентский цикл (`SPEC.md` §3.3–§3.8).

- [CAMPAIGN_WIZARD.md](CAMPAIGN_WIZARD.md) — §19 визард `/newcampaign`: 8 этапов, черновики, Sheets round-trip → диалог + состояние черновика (§3.5).
- [section19-spec.md](section19-spec.md) — дополнение §19 по созданию Search-кампании через Telegram.
- [gap-analysis-section19.md](gap-analysis-section19.md) — расхождения реализации с §19 и план закрытия.
- [CLIENTS_KB.md](CLIENTS_KB.md) — §20 `/clients`: профиль клиента, LLM-разбор текста, краулер сайта → §3.8 (memory-инструменты, топик = клиент).
- [REPORTS.md](REPORTS.md) — `/report` `/export` `/sheets` `/mcc`: периоды, метрики, разбивки, экспорт → §3.7.
- [KEYWORD_RESEARCH.md](KEYWORD_RESEARCH.md) — `/keywords`: подбор идей, метрики, AI-кластеризация, `.xlsx` → §3.3.
- [GDN_CAMPAIGNS.md](GDN_CAMPAIGNS.md) — кампания из фото/видео (§11): GDN/Video/Demand Gen, confirm-флоу → §3.6.
- [OAUTH_SETUP.md](OAUTH_SETUP.md) — доступы (продублирован выше) — **[ядро]**, тул-слой ходит тем же OAuth.
- [MUTATIONS.md](MUTATIONS.md) — **[переход]**: карта изменяющих операций и общего confirm-ядра; кнопочные точки входа — legacy, MCP WRITE — цель.
- [SCHEDULER.md](SCHEDULER.md) — **[переход]**: плановые отчёты/аномалии/очистка; бизнес-логика остаётся, дом процесса уже вынесен в `python -m scheduler`.

## Технические референсы
- [gads-api-refs.md](gads-api-refs.md) — версии Google Ads API/SDK, график сансета.
- [reuse-sources.md](reuse-sources.md) — **провенанс порогов `/audit`**: какая проверка из какого MIT-источника (это лицензионная атрибуция, не заметка) + источники-паттерны. Сводный аудит открытых источников — в `AUDIT-open-source.md`.
- [ab-results.md](ab-results.md) — A/B моделей. ⚠️ Раздел «Решение» **историчен** (Фаза −1, выбирал `deepseek/deepseek-chat`); действующая раскладка по ролям — `SPEC.md` §10.1. Живым остаётся **замер**: Hermes-модели дали 0/11 function-calling на OpenRouter — на этом стоит выбор модели-мозга в `TZ-Aimash-Hermes-Agent.md`.

## Процесс, рубежи, эксплуатация ядра
- [REPO_GUARDRAILS.md](REPO_GUARDRAILS.md) — **`merge в master` == авто-деплой в прод**, значит право мержить == право снять confirm-гейт. Ruleset на ветку, machine-аккаунт билдера без merge, пустой bypass-list. ⚠️ `CODEOWNERS` без ruleset не энфорсит ничего.
- Эксплуатация ядра Hermes живёт рядом с самим ядром, в [`deploy/hermes/`](../deploy/hermes/):
  [`README.md`](../deploy/hermes/README.md) — установка (RB-0…RB-3) ·
  [`OPERATIONS.md`](../deploy/hermes/OPERATIONS.md) — день-2: редеплой↔MCP-reconnect, логи, откат, kill-switch, замеры §12 ·
  [`SAFE_RESTART.md`](../deploy/hermes/SAFE_RESTART.md) — безопасный restart двух Telegram-контуров без второго poller ·
  [`DRIFT_AUDIT.md`](../deploy/hermes/DRIFT_AUDIT.md) — сверка repo/runtime/cron drift перед эксплуатационными изменениями ·
  [`OPEN_DECISIONS.md`](../deploy/hermes/OPEN_DECISIONS.md) — решения заказчика D1–D7 (**настройка**, у каждого строгий дефолт) ·
  [`RISK_REGISTER.md`](../deploy/hermes/RISK_REGISTER.md) — риски Р1–Р9 (**подпись**, дефолта нет; приложение к договору) ·
  [`SOUL.md`](../deploy/hermes/SOUL.md) — **слот №1 системного промпта** (деплоится в `~/.hermes/SOUL.md`): идентичность агента, а НЕ граница безопасности — границы дают feature-gated surface, HMAC trusted transport, confirm-CAS и таинт через недоступность инструментов ·
  [`host-a/RUNBOOK.md`](../deploy/hermes/host-a/RUNBOOK.md) — двухсерверная схема; ⚠️ **ни один шаг не выполнялся живьём**.

## Архив
`docs/archive/` — вне зоны инвариантов ссылок (`tests/_docs_paths.py`), хранит документ таким, каким он был на момент заморозки.
- [archive/main-transplant-2026-07-27.md](archive/main-transplant-2026-07-27.md) — разбор orphan-ветки `main` с VPS: что из неё взято, что отклонено и почему (`DRY_RUN` рапортует об успехе несостоявшейся мутации; `tools_writes.py` даёт исполняющий `execute_confirmed` на MCP-поверхности без И8).

## Конфигурация ИИ-разработчиков

- Claude Code: `.claude/skills/`, `.claude/commands/`, `.claude/hooks/`.
- Codex: `.agents/skills/`, `.codex/agents/`, `.codex/hooks/`.
- Общие скилы в `.claude/skills/` и `.agents/skills/` обязаны совпадать дословно; платформенные
  обёртки могут отличаться. Дрейф ловит `tests/test_agent_config_sync.py`.
- Локальные секреты Claude остаются в ignored `.claude/settings.local.json`; `.codex/config.toml`
  содержит только имена env-переменных и проектные команды, а не значения токенов.

Общие скилы: `new-mutation` · `confirm-gate-audit` · `gaql-query` · `check-rsa-copy` · `gads-version`.

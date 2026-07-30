# Aimash Hermes Agent — Полное ТЗ (сводное)

> **Статус:** сводное ТЗ пивота — **точка входа (START HERE)**, живёт в `ai mash tg bot/docs/`.
> Построено на структуре исходного промпта заказчика (ШАГ 1–6 / ФАЗА 1–7), **приземлённой на
> уже принятую архитектуру** проекта `ai mash tg bot`. Дата: 2026-07-24.
>
> **Это не четвёртый источник истины, а точка входа.** Глубина — в трёх существующих документах
> репозитория `ai mash tg bot`, на которые здесь идут ссылки:
> - `SPEC.md` — требования и критерии приёмки (что делаем);
> - `deploy/hermes/HERMES_SPEC.md` — архитектура пивота (как устроено, ~2400 строк);
> - `deploy/hermes/AGENTIC_VS_TZ.md` — обоснование (почему так; из 187 требований 160 невыполнимы
>   «чистой LLM», 0/40 опровергнуто).
>
> Модель-мозг агента: **`openai/gpt-5.6-terra`** через OpenRouter (pluggable через конфиг гейтвея).

---

## 1. Что изменилось относительно исходного промпта (читать первым)

Исходный промпт написан в общих терминах и по нескольким пунктам расходится с уже принятыми в проекте решениями. Ниже — построчная сверка, чтобы ожидания и документ совпадали.

| Посылка промпта | Реальность проекта (с источником) | Следствие для ТЗ |
|---|---|---|
| «Hermes 3 — модель через OpenRouter» | **«Hermes» = агент-ФРЕЙМВОРК `NousResearch/hermes-agent` v0.19.0** (не модель). Решение о полном пивоте принято 2026-07-23 ([AGENTIC_VS_TZ.md:28-33](../deploy/hermes/AGENTIC_VS_TZ.md)). | Мозг агента — фреймворк, не «модель Hermes». |
| Модель `hermes-3-llama-3.1-405b/70b` | Hermes-4 70b/405b дали **0/11 function-calling** через OpenRouter («No endpoints found that support tool use»), `docs/ab-results.md`. Гейтвей работает на **`openai/gpt-5.6-terra`** ([config.yaml:22-24](../deploy/hermes/config.yaml)). | Модель-мозг = gpt-5.6-terra, pluggable. Hermes-модели — только при самохостинге на vLLM с `--tool-call-parser hermes`. |
| «Node/TS или Python с нуля» | Зрелый Python-код уже есть; денежное ядро (`ads/mutations.py`, `ads/client.py`, `confirm/**`, `core/secrets.py`) «**лучшая часть кодовой базы, не трогается**» (HERMES_SPEC §Прочтение A). | Не greenfield. Переиспользуем денежное ядро; фреймворк ставится сверху. |
| «MCP-серверы — на будущее (best practice)» | MCP — **уже реальный и единственный канал** Hermes→Google Ads: read-only, 25 READ-инструментов, через `docker exec -i aimash-bot python -m mcp_server` ([config.yaml:213-236](../deploy/hermes/config.yaml)). WRITE физически отсутствует by construction. | Строим WRITE-MCP поверх готового `execute_confirmed`, а не «на будущее». |
| «ReAct-цикл, Max Iterations = 5, State Machine своими руками» | Агент-цикл, оркестрация, маршрутизация интентов, tool-calling, память, скилы — **встроены во фреймворк**: «встроенная машинерия автономии — брать готовым, не строить» (SPEC §5.6). | TriageAgent/state-machine/ReAct **не пишем** — конфигурируем фреймворк. |
| «Human-in-the-loop построить» | Confirm-гейт с CAS/TTL/one-shot и провенансом **уже готов** (`confirm/store.py`, `confirm/gate.py`). Approvals самого Hermes **не гейтят MCP** («Approval flows do not govern MCP tool invocations»); хуки Hermes **fail-OPEN**. | HITL держится в НАШЕМ коде (`execute_confirmed`, правило 10), не в хуках фреймворка. |
| «Guardrails построить» | Замки аккаунтов, capability-ceiling, freshness/TOCTOU, 2FA, денежные диапазоны — **уже есть**, но размазаны по 6 местам; бизнес-лимита «≤20% за шаг» и дневных лимитов — **нет**. | Достраиваем: единый `PolicyEngine` + бизнес-лимиты. |
| «Pgvector/векторная память — построить» | Фреймворк имеет toolsets `memory`/`context_engine` (сейчас погашены для харднинга). Артефактная память — HERMES_SPEC §17. | Память — сначала настройка фреймворка; свой pgvector — только под RAG бизнес-правил, если фреймворк не покрывает. |

**Вывод:** объём работ смещается от «написать агента с нуля» к «поставить фреймворк как мозг, сохранить денежное ядро, достроить WRITE-MCP + PolicyEngine + evals + observability, сконфигурировать память/скилы». Полная смета пивота — **3 296 ч** (SPEC §12, HERMES_SPEC §6).

---

## 2. Целевая архитектура (Контур A)

```
Пользователь (Telegram)
        │  текст + reply-подтверждение (кнопки архивируются, Вопрос 2 открыт)
        ▼
┌─────────────────────────────────────────────────────────────┐
│  hermes-agent gateway   (systemd --user сервис на VPS)        │
│  • модель: openai/gpt-5.6-terra через OpenRouter             │
│  • provider_routing.require_parameters: true (иначе tool-use  │
│    молча срезается на провайдере без `tools`)                 │
│  • toolsets: skills/todo/clarify + MCP; всё host-мощное off   │
│  • approvals: manual (гейтят ТОЛЬКО терминал, НЕ MCP)         │
└───────────────┬──────────────────────────────────────────────┘
                │  MCP stdio:  docker exec -i aimash-bot python -m mcp_server
                ▼
┌─────────────────────────────────────────────────────────────┐
│  aimash money-code (контейнер aimash-bot, Python)            │
│  • 25 READ-инструментов, read-only BY CONSTRUCTION           │
│  • WRITE-MCP (ДОСТРОИТЬ): propose_* → создаёт Proposal,        │
│    execute_approved → execute_confirmed ТОЛЬКО после «да»      │
│  • PolicyEngine, замки аккаунтов, confirm-гейт, 2FA           │
│  • OAuth (Fernet at-rest), audit_log, freshness/TOCTOU        │
└───────────────┬──────────────────────────────────────────────┘
                │  gRPC (google-ads SDK v24), сеть только отсюда
                ▼
        Google Ads API  (googleads.googleapis.com)
```

**Blast radius (изоляция доменов).** У модели нет сетевого доступа к Google Ads вовсе — единственный путь идёт через процесс MCP. READ-слой не содержит WRITE-инструментов физически (construction-time гард). WRITE-инструменты только **создают черновик** (Proposal); реальный вызов SDK — единственная точка `execute_confirmed`, и она отказывает без доверенной записи подтверждения. Ссылки: SPEC §5 (топология), §5.4 (MCP + plugin-hook), §6 (реестр инструментов), §8.1 (8 инвариантов изоляции).

**Двуххостовая проблема (важно для безопасности).** На боевом VPS «read-only агент» технически невозможен: пользователь systemd-сервиса Hermes обязан быть в группе `docker`, а она root-эквивалентна и даёт доступ к `SECRETS_ENCRYPTION_KEY`. Отсюда схема «Хост A (вольная ВМ, отдельный Google-пользователь Read-only) / Хост B (боевой VPS)». Единственная реальная граница read-пилота — **роль Google-пользователя**, а не конфиг. Ссылки: `deploy/hermes/host-a/RUNBOOK.md`, RISK_REGISTER R3.

---

## 3. Модельная стратегия (pluggable, дефолт gpt-5.6-terra)

- **Дефолт:** `model.provider: openrouter`, `model.default: openai/gpt-5.6-terra` (форма ОБЯЗАТЕЛЬНО mapping, иначе К10 молча откатит на Nous Portal). Смена модели = строка конфига гейтвея.
- **Обязательно:** `provider_routing.require_parameters: true` — гарантирует провайдера с поддержкой `tools`; иначе OpenRouter молча рероутит на эндпоинт без tool-calling и агент «отвечает текстом» вместо вызова MCP.
- **Hermes-модели:** оставлены как опция ТОЛЬКО через самохостинг (vLLM + `--tool-call-parser hermes`) — единственный способ получить у Hermes надёжный tool-use; требует GPU-инфраструктуры. По умолчанию не закладывается.
- **Потолок трат на LLM (D1):** дефолт **$10/сутки**, держится кредитным лимитом на ключе OpenRouter, **не нашим кодом** — расход делает gateway напрямую, минуя наш процесс. Ключ Hermes ≠ ключ бота.
- Ссылки: SPEC §10 (модель и бюджет), HERMES_SPEC §15 (GPT-5.6 на OpenRouter), `docs/ab-results.md` (почему не Hermes-модели), AUDIT-open-source.md.

---

## 4. Маппинг требований промпта → реализация

Ниже канонический разбор по 7 ФАЗАМ (более детальный набор промпта); в скобках — соответствующие ШАГи. Для каждой: **[Переиспользуем]** / **[Достраиваем]** / **[Даёт фреймворк]** и ссылка на глубокий раздел.

### Фаза 1 — Harness / Инфраструктура / телеметрия / retry / БД (ШАГ 1, ШАГ 3)
- **Даёт фреймворк:** агент-цикл, диспетчеризация, tool-calling, ретраи вызова модели, трейс шагов (SPEC §5.6).
- **Переиспользуем:** retry+backoff+семафор+квота Google Ads (`core/resilience.py`), структурные логи с корреляцией request_id/chat_id/operation (`core/logging.py`), учёт токенов/стоимости по ролям (`core/usage.py`), SQLAlchemy 2.0 + Alembic (`migrations/`; цепочка ревизий — docs/DATABASE.md), Sentry (`core/observability.py`).
- **Достраиваем:** persistent per-run трейсинг (Langfuse/Helicone) с привязкой токенов к шагу агента; новые таблицы (SPEC §9.2). ШАГ 1: VPS-харднинг (см. §12 ниже).
- Ссылки: SPEC §9 (схема данных), §11 (NFR/эксплуатация); OPERATIONS.md (день-2).

### Фаза 2 — Оркестрация / Router pattern (ШАГ 3)
- **Даёт фреймворк:** маршрутизация интентов и суб-агенты — встроенная машинерия; TriageAgent/AnalyticsWorker/GoogleAdsWorker **своими руками не пишем** (SPEC §5.5 «правило тонкого слоя», §5.6).
- **Переиспользуем:** реестр инструментов как контракт (`agent/tools/schemas.py`, Pydantic→schema) и обёртку вызова модели `agent/router.py` (`chat`/`finish_reason`/выбор модели по роли) — её на уровне модуля импортируют 10 модулей сохраняемых пакетов (`adcopy`, `keywords`, `clients`, `advisor`), поэтому при архивации `agent/` оба файла **переезжают в bot-free пакет, а не удаляются** (иначе рвётся импорт четырёх ядровых пакетов). Архивируется из `agent/` только свой цикл: `loop.py` (в нём же `SYSTEM`-промпт), `campaign_edit.py`, `campaign_settings.py`, `openrouter_account.py`; `agent/system_prompt.py`/`agent/tools.py` **не существуют**.
- **Достраиваем:** маппинг интентов на скилы/топики Hermes; `group_topics` (топик→скил) в конфиге.
- Ссылки: SPEC §6 (реестр: READ/MEMORY/PLAN/execute).

### Фаза 3 — Tool design / Google Ads skills / self-correction (ШАГ 4)
- **Переиспользуем:** READ-MCP — 25 READ-инструментов (envelope+error-codes, redaction); ~41 Google Ads skill (`ads/service.py` `SUPPORTED_OPERATIONS`), резолверы, post-apply verify.
- **Достраиваем:** **WRITE-MCP инструменты** — `propose_budget_change`, `propose_campaign_status`, `propose_bid_adjustment`, `execute_approved_action` — поверх готового `execute_confirmed` (`ads/service.py`). Self-correction: ошибка API → понятный JSON модели (`INVALID_ARGUMENT: budget must be a multiple of 100 …`) без прерывания цикла; во фреймворке цикл продолжается сам.
- Ссылки: SPEC §6.3 (PLAN — готовят, не исполняют), §6.4 (исполнение — единственная точка), HERMES_SPEC §8 (реестр MCP-инструментов).

### Фаза 4 — Guardrails / PolicyEngine (ШАГ 4)
- **Переиспользуем:** замки `ensure_allowed`/`ensure_read_allowed`/`ensure_manager_allowed`/`allowed_ceiling` (`ads/client.py`), capability-ceiling `SUPPORTED_OPERATIONS`, денежные диапазоны (`core/limits.py`), двухбитовый денежный гейт (`ads/mutations.py`), freshness/TOCTOU (`ads/service.py`), 2FA (`core/twofa.py`), construction-time гард (`core/guards.py`).
- **Достраиваем (главный build):** единый модуль `guardrails/policy.py` (middleware перед WRITE) + **бизнес-лимиты, которых нет**:
  - `Δбюджета ≤ 20% за шаг` (число промпта);
  - второй порог подтверждения (D5): новый дневной бюджет > текущего на 50% ИЛИ > 2× макс. дневного бюджета аккаунта;
  - дневной потолок бюджета `$X`, дневной лимит числа/суммы мутаций;
  - опц. запрет `remove_campaign` (сейчас разрешён с double-confirm).
  - Нарушение → `POLICY_VIOLATION_REJECTED`.
- Ссылки: SPEC §7 (модель подтверждения), §8 (безопасность), §15.1 (D5).

### Фаза 5 — Human-in-the-loop / approval gates (ШАГ 5)
- **Переиспользуем целиком:** `Proposal`+`build_summary`, `ConfirmStore` (CAS `claim`, TTL-в-CAS, one-shot, `needs_review`), исполнение `execute_confirmed`, статус-машина `pending→confirmed→executing→applied/failed/rejected`.
- **Достраиваем:** проброс провенанса `origin_human_turn` для нового агентного actor (иначе денежные `apply_*`, требующие двух битов, заблокируются fail-closed); карточка approval `🎯 действие / 📊 обоснование / ⚠️ риски` + подтверждение reply-текстом (кнопки архивируются по пивоту; Вопрос 2 «кнопки/слэш-команды» остаётся открытым — нужна подпись).
- **Критично:** approvals фреймворка НЕ покрывают MCP; хуки fail-OPEN → HITL держится в нашем коде (правило 10). Ссылки: SPEC §2.2 (контракт reply-подтверждения), §7, HERMES_SPEC §12 (критерии приёмки).

### Фаза 6 — Память: компрессия контекста + vector RAG (ШАГ 6)
- **Даёт фреймворк:** toolsets `memory`/`context_engine` (сейчас погашены для харднинга — включать осознанно); артефактная память «помнит всё, что писал/загружал» (HERMES_SPEC §17); скилы как долговременная процедурная память (§10, §21).
- **Переиспользуем:** структурная БЗ клиента (`client_profiles`/`client_dossiers`, map-reduce досье), recall applied-действий (`db/history.py`).
- **Достраиваем (если фреймворк не покрывает RAG бизнес-правил):** vector store на **pgvector** + инструмент `save_rule_to_memory` + retrieval бизнес-правил в системный контекст; компрессия/саммаризация длинного диалога (episodic memory). Эмбеддинги — `text-embedding-3-small` (1536) через OpenRouter либо локальные `bge/e5`.
- Ссылки: SPEC §3.10 (память, скилы, самообучение), §9.3 (артефактная память), HERMES_SPEC §17, §18, §29 (memory-домен).

### Фаза 7 — Evals / golden set / Shadow Mode (Best Practices)
- **Переиспользуем:** guardrail-инварианты в pytest (`tests/test_invariants_core.py`, `test_safety_core.py`, `test_money_checks_f6.py`, `test_reject_cas.py`), fact-guard `narrative_facts_preserved` (`audit/factguard.py`), скелет A/B (`scripts/ab_test_models.py`).
- **Достраиваем:** `tests/evals/` + **golden-датасет (≥20 сценариев)**: срабатывание PolicyEngine («снизь бюджет на 90%», «ставку до $1000» → блок), точность интентов, парсинг команд; **Shadow Mode** (агент генерит план, пишет в лог, не исполняет — метрика совпадения); расширить `ab_test_models.py` до eval-раннера.
- Ссылки: SPEC §13 (критерии приёмки), §14 (верификация).

### ШАГ 2 — Миграция из старого репозитория
Инструкция «извлечь ТОЛЬКО OAuth + SQL-схемы» устарела: в проекте переиспользуется намного больше (см. `REUSE-MAP.md`). Игнорируется, как и просили: детерминированная маршрутизация aiogram, хардкод-кнопки, визарды `bot/` (архивируются пивотом целиком). Ссылки: SPEC §5.2, HERMES_SPEC §3.

---

## 5. Guardrails / PolicyEngine — целевые числа

| Правило | Значение (дефолт) | Где живёт |
|---|---|---|
| Дельта бюджета за шаг | ≤ 20% | `guardrails/policy.py` (достроить) |
| Второй порог «да» (D5) | новый дн. бюджет > +50% ИЛИ > 2× макс. дн. бюджета | `guardrails/policy.py` |
| Потолок дневного бюджета | `$X` (уточнить с заказчиком) | `guardrails/policy.py` |
| Дневной лимит мутаций | число/сумма (уточнить) | `guardrails/policy.py` |
| Удаление кампаний | опц. запрет (сейчас double-confirm) | `SUPPORTED_OPERATIONS` / policy |
| Read⊆ceiling, allowed⊆ceiling | fail-closed, пустой набор = отказ | `ads/client.py` (есть) |
| Кратность биллинг-единице валюты | до `claim` | `core/limits.py` (есть) |
| API-квота | блок на 95% от 15000 оп/сут | `core/quota.py` (есть) |

Нарушение бизнес-политики → инструмент отбивает `POLICY_VIOLATION_REJECTED` (в понятном модели JSON — self-correction).

---

## 6. Observability

- **Есть:** структурные JSON-логи (`LOG_FORMAT=json`) с корреляцией, Sentry (опц.), учёт токенов/стоимости OpenRouter по ролям (per-process).
- **Достроить:** трейсинг per-iteration (Langfuse — ядро MIT, self-host; или Helicone-прокси base-URL) с привязкой токенов/латентности к шагу агента и `run_id`; формат лога шага `[TRACE_ID][NODE][ACTION][LATENCY][TOKENS]`. Учесть, что расход LLM делает **gateway напрямую** — часть трейсинга живёт на стороне OpenRouter (Usage Accounting) / самого Hermes-лога, а не только в нашем коде.
- Ссылки: SPEC §11, AUDIT-open-source.md.

---

## 7. Инфраструктура, деплой, безопасность VPS (ШАГ 1)

- **Пользователь `agent_dev`** с sudo без доступа к критичным директориям — учесть, что для Hermes-сервиса нужна группа `docker` (root-эквивалент) → отсюда двуххостовая схема (Хост A read-only / Хост B прод).
- **Не тащить в новый репо** старые `.env`, `aimash.db`, git-историю: в старом репо зафиксирована утечка паролей БД в git (`core/config.py`). Свежий `SECRETS_ENCRYPTION_KEY`, gitleaks pre-commit (есть).
- **Пин версии фреймворка:** `NousResearch/hermes-agent` release `v0.19.0`, git-тег `v2026.7.20` («Quicksilver»); тега `v0.19.0` в репо НЕТ — фетчить по `ref` (`PIN.json`). Версия на живом VPS НЕ сверена (`host_matches: null`) — снять замер V1.
- **Развёртывание:** systemd `--user` + `loginctl enable-linger`; MCP через `docker exec`; kill-switch, health-чеклист — `deploy/hermes/OPERATIONS.md`, `README.md`, `host-a/RUNBOOK.md`.
- **Docker Compose:** эволюция существующих Dockerfile/compose — Postgres-образ `pgvector/pgvector:pg17`, healthcheck + `depends_on: condition: service_healthy`, non-root, multi-stage.

---

## 8. Открытые решения (D1–D7) и риски (R1–R9)

**Решения заказчика** (дефолт действует без ответа) — `deploy/hermes/OPEN_DECISIONS.md`, SPEC §15.1:
D1 потолок LLM $10/сут (лимит ключа OpenRouter) · D2 кто вправе подтверждать деньги (только владелец; список ≠ whitelist чтения; пусто = никто) · D3 TTL карточки 60 мин (для бюджета/ставки — 15) · D4 изоляция клиентов (топик = клиент; клиентам — раздельные инстансы) · D5 порог второго «да» · D6 истина при расхождении (audit-row + повторное чтение API) · D7 адресат алертов (`ADMIN_CHAT_IDS` — **сейчас пуст ⇒ алерты в никуда**, закрыть).

**Топ-риски** — `deploy/hermes/RISK_REGISTER.md`, SPEC §15.3, HERMES_SPEC §34:
R1 агент пишет/исполняет свой код · R2 нет изоляции клиентов внутри инстанса · R3 потолок держится ролью Google-пользователя и ломается молча · R7 Hermes 0.x, релизы 5–6 дней, неизвестные ключи игнорируются молча (К10) · R9 квота Basic 15000 оп/сут на весь парк.

---

## 9. MCP-серверы для разработки

Настройка dev-окружения — файл `.mcp.json` (в корне новой папки): **context7** (свежие docs фреймворка/OpenRouter/pgvector), **postgres** (`crystaldba/postgres-mcp`, restricted), **filesystem**, **git**, **fetch**, **sequential-thinking**. Официальный `@modelcontextprotocol/server-postgres` архивирован (SQLi) — не использовать. Подробности и назначение — в `.mcp.json` и `AUDIT-open-source.md`.

---

## 10. Дорожная карта (Волны 0–3)

Из SPEC §12 / HERMES_SPEC §5:
- **Волна 0** — прототипы, снимают бинарную неопределённость (отдаёт ли gateway файл R3, принимает ли медиа R6; замеры V1–V22 из OPERATIONS.md §12 — **сейчас не сняты**).
- **Волна 1** — механизм: freshness/TTL-в-CAS/провенанс (чинят fail-open текущего кода).
- **Волна 2** — под новые требования (headless-ядро, транспорт, миграция тестов).
- **Волна 3** — функциональный объём (§24–§31: отчёты, keyword research, RSA, кампании, БЗ, проактив).

Смета: **3 296 ч** (2 301 механизм + 995 возврат функционала), ~25 мес одним разработчиком (HERMES_SPEC §6).

---

## 11. Verification / Definition of Done для этого ТЗ

1. Каждая ФАЗА имеет разбор [Даёт фреймворк]/[Переиспользуем]/[Достраиваем] с путями и ссылкой на SPEC/HERMES_SPEC.
2. Guardrails-лимиты заданы числами (20% / D5 / дневной потолок).
3. Модель-мозг = gpt-5.6-terra, зафиксировано `require_parameters: true`; Hermes-модели помечены как «только самохостинг».
4. Ни одна рекомендация не предлагает Node-стек как выбранный путь и не переоткрывает пивот.
5. Кросс-ссылки на SPEC.md/HERMES_SPEC.md/AGENTIC_VS_TZ.md разрешаются в реальные разделы.
6. `.mcp.json` подключается (`claude mcp list`), context7 отвечает; `.env.example` покрывает имена бота + гейтвея + новые.

---

## Источники истины (репозиторий `ai mash tg bot`)
- `SPEC.md` — требования/приёмка (ЧАСТЬ 0 границы автономии, I продукт §1–4, II архитектура §5–11, III исполнение §12–17).
- `deploy/hermes/HERMES_SPEC.md` — архитектура (§8 MCP-реестр, §9 новые таблицы, §11 конфиг-эталон, §15 модель GPT-5.6, §17 артефактная память, §32 отклонения, §33 матрица трассируемости, §34 риски).
- `deploy/hermes/AGENTIC_VS_TZ.md` — обоснование разреза «модель/код».
- `deploy/hermes/{README,OPERATIONS,OPEN_DECISIONS,RISK_REGISTER,PIN.json}`, `host-a/{RUNBOOK,config.yaml}`, `config.yaml`, `lint_config.py` — операционный корпус Контура A.
- `docs/ab-results.md` — A/B моделей (почему не Hermes-модели).

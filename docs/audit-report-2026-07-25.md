# 🔍 Aimash — Полный Технический и Архитектурный Аудит

**Дата:** 2026-07-25  
**Объект:** `github.com/kandibobe/aimash` (ветка `main`, актуальный снимок `/opt/aimash`)  
**Методология:** Полный обход исходного кода (200+ файлов): `core/`, `ads/`, `confirm/`, `agent/`, `bot/`, `mcp_server/`, `audit/`, `scheduler/`, `db/`, `tests/`, `deploy/`. Каждый модуль изучен построчно.  
**Цель:** Внешний Code Review с конкретными рекомендациями по улучшению.

---

## 1. Архитектура и Стек

### 1.1 Технологический стек — полная карта

| Слой | Технология | Версия | Файлы | Примечание |
|------|-----------|--------|-------|-----------|
| **Язык** | Python | 3.12+ | `pyproject.toml:7` | `requires-python = ">=3.12"` |
| **Фреймворк бота** | aiogram | ≥3.13 | `bot/main.py` (7447 строк), `bot/handlers/`, `bot/callbacks.py`, `bot/ux.py`, `bot/keyboards.py`, `bot/states.py`, `bot/throttle.py` | Центральная точка входа Telegram. Монолит с 30+ typed CallbackData |
| **MCP-сервер** | FastMCP (mcp SDK) | ≥1.25 | `mcp_server/server.py` (65 строк), `__main__.py`, `__init__.py` | `structured_output=False`, lifespan-bootstrap через `app.bootstrap.bootstrap_ads_layer()` |
| **LLM-клиент** | OpenAI SDK → OpenRouter | ≥1.50 | `agent/router.py` (342 строки) | Синглтон `AsyncOpenAI` с мемоизацией per event-loop. Langfuse-прокси при включённом трейсинге |
| **База данных** | PostgreSQL 16 + SQLAlchemy 2.0 (async) | SQLAlchemy ≥2.0 | `db/models.py` (727 строк, 20+ таблиц), `db/session.py` (272 строки), `db/history.py` | Dev: SQLite/aiosqlite. Pool: pre_ping + recycle (prod), NullPool (dev). Advisory-lock для single-instance |
| **Драйвер БД** | asyncpg (prod), aiosqlite (dev) | ≥0.30 / ≥0.20 | `db/session.py:24-39` | Автовыбор по DSN-префиксу. connect_args timeout=10с |
| **Миграции** | Alembic | ≥1.13 | `migrations/versions/0031_ads_quota_ops.py` (31 миграция) | `docker-entrypoint.sh → alembic upgrade head` при старте. Fail-fast |
| **Google Ads API** | google-ads SDK | ≥31.1,<32 (API v24) | `ads/mutations.py` (4220 строк), `ads/client.py` (582 строки), `ads/read.py`, `ads/resolve.py`, `ads/validation.py`, `ads/geo.py`, `ads/freshness.py`, `ads/assets.py`, `ads/keyword_plan.py` | Единственное место SDK-вызовов. Сансет v24 ~май 2027 |
| **Валидация схем** | Pydantic | ≥2.9 | `agent/tools/schemas.py` (1658 строк) | Все READ + WRITE инструменты. AfterValidator для валют/ключей. model_validator для кросс-полей |
| **Ретраи** | tenacity | ≥9.0 | `core/resilience.py` (317 строк) | 3 стратегии: `run_ads_call` (мутации), `run_ads_create_call` (без ретраев), `run_ads_read_call` (полный набор), `call_llm` |
| **Мониторинг** | Sentry + Langfuse | ≥2.0 / ≥4.14 | `core/langfuse_tracing.py`, `core/observability.py` | Sentry — опционален (нет DSN → no-op). Langfuse — авто-трейсинг через `langfuse.openai.AsyncOpenAI` |
| **Шифрование** | Fernet (cryptography) | ≥43.0 | `core/secrets.py` | OAuth-токены шифруются at-rest. Round-trip проверка в `_require_encryption_key_in_prod` |
| **Настройки** | pydantic-settings | ≥2.5 | `core/config.py` (650 строк) | `SecretStr` для всех токенов. Валидаторы: `_validate_env_name`, `_require_encryption_key_in_prod`. Нормализация MCC/валют/гео |
| **Контейнеризация** | Docker Compose | — | `Dockerfile` (32 строки), `docker-compose.yml` (219 строк) | 4 сервиса: bot, postgres, scheduler, mcp (profiles). Multi-stage build, non-root user |
| **Экспорт** | openpyxl + google-api-python-client | ≥3.1 / ≥2.140 | `reports/xlsx.py`, `reports/sheets.py` | XLSX + Google Sheets экспорт |
| **Краулинг** | httpx + beautifulsoup4 | ≥0.27 / ≥4.12 | `clients/crawler.py`, `clients/dossier.py`, `clients/profile_extract.py` | Статический краулер сайтов клиентов для досье (§20) |
| **2FA** | hmac.compare_digest | stdlib | `core/twofa.py` | Constant-time сравнение PIN. Экспоненциальный lockout |
| **Документы** | python-docx | ≥1.1 | `reports/docx.py`, `scripts/docx_to_tz.py` | Чтение .docx-брифов, экспорт аудитов |

### 1.2 Оркестрация и State Machine

Агентный цикл — **однопроходный ReAct без LangGraph/конечного автомата/чейн-оф-сот**.

**Поток обработки ОДНОЙ команды** (`agent/loop.py:handle_command`, 678 строк):

```
1. Пользовательский текст
   ↓
2. prompt_guard.check_message() — Gemini Flash 8B: SAFE / INJECTION
   ↓
3. agent.loop.handle_command(text, chat_id, context)
   ├─ Сборка messages: SYSTEM + контекст диалога (C3) + справочный контент (файлы/URL)
   ├─ agent.router.chat(messages, role="parsing", tools=TOOLS)
   │  ├─ parallel_tool_calls=False (A5: одно действие за вызов)
   │  ├─ provider.sort=throughput (опционально, только parsing)
   │  └─ fallback-модель при транзиентном отказе
   ├─ _classify_tool_call(name, args, chat_id, context):
   │  ├─ ask_clarification → {type: "clarify"}
   │  ├─ READ_TOOLS → _do_read (живое чтение Google Ads SDK)
   │  │  ├─ resolve_read_account(chat_id, args.account)
   │  │  ├─ build_client_async(cid) — per-account OAuth
   │  │  └─ run_ads_read_call(account_stats, client, cid, days)
   │  ├─ MUTATION_TOOLS → Proposal (черновик, БЕЗ исполнения)
   │  │  ├─ Pydantic-валидация: SCHEMAS[name](**args)
   │  │  ├─ Capability-guard: name ∈ SUPPORTED_OPERATIONS
   │  │  ├─ _resolve_pronoun_campaign(after, context) — «эта кампания» → имя
   │  │  └─ Proposal(operation, summary, params, confirmation_id)
   │  └─ text/unknown → ошибка
   └─ A5-страховка: несколько tool_calls → notice «обработано только первое»
```

**Ключевые архитектурные решения (полный разбор):**

1. **НЕТ LangGraph / многошагового агента.** Каждая команда — ровно 1 вызов модели + 1 классификация. Никаких цепочек мыслей. Это сознательное решение: каждый шаг атомарен и либо создаёт черновик, либо читает данные. Компромисс: нельзя дать агенту «подумать» в несколько шагов.

2. **Model router** (`agent/router.py`): разделение на 8 ролей с разными моделями и `max_tokens`:

| Роль | Дефолтная модель | max_tokens | Назначение |
|------|-----------------|------------|-----------|
| `parsing` | `deepseek/deepseek-chat` | 1024 | Парсинг команд (денежный путь), function calling |
| `copy` | `anthropic/claude-opus-4.8` | 2048 | Генерация RSA-текстов (качество RU) |
| `keywords` | `anthropic/claude-opus-4.8` | 2048 | Seed/релевантность/минус-слова/кластеризация |
| `clustering` | `llm_parsing` | 1024 | Кластеризация keyword research |
| `analyst` | `llm_parsing` | 1536 | Нарратив /audit (read-only) |
| `extract` | `llm_parsing` | 4096 | Извлечение фактов из чанка краула (десятки вызовов) |
| `dossier` | `llm_keywords` | 8192 | Синтез прозы досье (один вызов на сайт) |
| `guard` | `google/gemini-flash-8b-1.5` | 4 | Бинарная классификация SAFE/INJECTION |

3. **Fallback-модель** (строка 323-342): при транзиентном отказе основной — одна попытка на `llm_fallback`. Проверка: `_is_retryable_llm(e) and fb and fb != chosen`. Нетранзиентные (BadRequest) не фолбэчатся.

4. **Audit-нарратив** (`agent/loop.py:462-678`) — отдельный ReAct-цикл:
   - Только `ANALYSIS_TOOLS` (read-only, S4-инвариант)
   - `ANALYSIS_MAX_ITERS = 4` ходов с инструментами
   - `ANALYSIS_TIMEOUT_S = 45.0` — общий бюджет времени
   - `fact-guard` (S3): выдуманное число → нарратив отвергнут, детерминированная карточка
   - Сбой/timeout/бюджет-стоп → `None` → fallback на детерминированную карточку

5. **Жизненный цикл Proposal** (6 статусов):
   ```
   pending → confirmed → executing → applied / failed / rejected
                                      ↘ needs_review (зависшие)
   ```
   Все переходы — **атомарные CAS** (compare-and-set) через `confirm/store.py:ConfirmStore`. Ни одного `select-then-update`.

### 1.3 Маршрутизация Telegram — детальный разбор

**Архитектура:** Единый `bot/main.py` (7447 строк) с модульными хендлерами. Aiogram 3.x.

**Middleware pipeline:**
```
Update
  → WhitelistMiddleware (проверка chat_id ∈ env ∪ db.models.Whitelist)
    → TraceMiddleware (request_id + контекст + провенанс human_turn)
      → ThrottleMiddleware (0.7 сообщений/сек, anti-flood)
        → Хендлеры (commands.py, fallback.py, reports.py, ...)
```

**Хендлеры** (в `bot/handlers/`):
- `fallback.py` — свободный текст → `agent.loop.handle_command`
- `commands.py` — `/start`, `/audit`, `/report`, `/mcc`, `/model`, `/grant`, `/revoke`, `/adduser`, `/removeuser`, `/addadmin`, `/quota`, `/balance`, `/diag`, `/sheets`, `/export`, `/competitors`, `/journal`, `/account`, `/settings`, `/bizdigest`, `/lang`, `/bug`, `/rsa`, `/newsearch`, `/newgdn`, `/newvideo`, `/newdg`, `/keywords`, `/cc`, `/addkeys`, `/client`
- `reports.py` — отчёты с inline-клавиатурами
- `competitors.py` — `/competitors` + auction insights
- `menu_guard.py` — защита меню

**Callback-система** (`bot/callbacks.py`): 30+ typed `CallbackData` классов — строгая типизация inline-кнопок (ConfirmCB, CampCB, RsaCB, GeoCB, NavCB, PeriodCB, ModelCB, …). Каждый callback — dataclass с factory-префиксом.

**Топики супергруппы** — через `aiogram F.message_thread_id`:
- general → thread_id=1 → ad-master-agent
- google-ads → thread_id=153 → google-ads-worker
- meta-ads → thread_id=154 → meta-ads-worker
- tiktok-ads → thread_id=155 → tiktok-ads-worker
- approvals → thread_id=156 → ad-master-agent

**Провенанс хода** (`core/provenance.py`, 86 строк): `contextvar`-изолированный бит.
- `human_turn()` — открывается ТОЛЬКО из `WhitelistMiddleware` (доверенный вход)
- `machine_turn()` — явный сброс для фоновых задач (cron, краул, скрипты)
- `request_scope()` в `core/context.py:96-122` автоматически открывает `machine_turn()`
- Дефолт — `False` (fail-closed). Фоновая задача, порождённая `asyncio.create_task` из человеческого хода, унаследовала бы человеческий бит — `request_scope` это предотвращает.

---

## 2. Безопасность и Инструменты (Money-Path & Guardrails)

### 2.1 Схемы инструментов — полный разбор

**Pydantic ≥2.9** — все инструменты валидируются моделями из `agent/tools/schemas.py` (1658 строк).

**Архитектура валидации (трёхслойная):**

```
Слой 1: Pydantic-схема (agent/tools/schemas.py)
  ├─ Field(gt=0) для сумм
  ├─ AfterValidator для валют (_norm_currency: символы/алиасы → ISO)
  ├─ AfterValidator для ключевых слов (assert_keyword_ok: длина/форма)
  ├─ model_validator для кросс-полей (mode vs value)
  └─ MONEY_MAX_MICROS / MONEY_MAX_UNITS из core/limits.py

Слой 2: agent/loop.py:_classify_tool_call
  ├─ Pydantic ValidationError → текст ошибки пользователю
  ├─ Capability-guard: name ∈ SUPPORTED_OPERATIONS
  └─ _resolve_pronoun_campaign — «эта кампания» → реальное имя

Слой 3: ads/mutations.py (граница SDK)
  ├─ _require_user_command: оба бита провенанса
  ├─ _require_freshness: attested_snapshot
  ├─ ensure_allowed(customer_id)
  ├─ MONEY_MAX_MICROS / MAX_RADIUS_KM (defense-in-depth)
  ├─ billing_unit_micros(currency) — округление до биллинг-единицы
  └─ assert_keyword_ok / assert_asset_len
```

**Ключевое:** `core/limits.py` — единый источник числовых порогов. Оба слоя (схема + мутация) вычисляют границы из одного значения, исключая рассинхрон.

```python
# core/limits.py
MONEY_MAX_UNITS = 1_000_000        # потолок в единицах валюты
MONEY_MAX_MICROS = MONEY_MAX_UNITS * 1_000_000  # та же граница в micros
MAX_RADIUS_KM = 2000               # лимит Google Ads для proximity
```

### 2.2 Guardrails & Policy Engine — послойный разбор

**Рубеж 1: PolicyEngine** (`core/budget_policy.py`, 446 строк) — детерминированный, без LLM:

| Проверка | Порог | Действие | Строки |
|----------|-------|----------|--------|
| Δ бюджета/CPC | >20% | BLOCKED — `PolicyExceeded` | 257-273 |
| Δ бюджета/CPC | >15% | WARNING — близко к лимиту | 275-283 |
| Абсолютный дневной лимит | `MAX_DAILY_BUDGET` | BLOCKED | 286-300 |
| Пауза + конверсии | >0 конверсий за 7д | HIGH — требует подтверждения | 303-317 |
| Cooldown | <24h (1440 мин) | WARNING (advisory, не блок) | 319-335 |
| DRY_RUN | `True` (дефолт!) | Предупреждение в ответе | 338-342 |

**Конфигурация:**
- `MAX_BUDGET_DELTA_PCT` (env, default 20)
- `MAX_DAILY_BUDGET` (env, default 0 — без гарда)
- `BUDGET_COOLDOWN_MINUTES` (env, default 1440)
- `DRY_RUN` (env, default `true`)

**State storage:** JSON-файл `~/.hermes/admaster/budget_policy_state.json` — не БД. При рестарте контейнера без volume теряется. Нет конкурентного доступа.

**Рубеж 2: Financial Auditor** (`core/auditor.py`, 187 строк):

- `heuristic_audit()` — эвристическая проверка (микросекунды, без LLM):
  - Δ > 50% → +5 к risk score
  - CPA > target × 2.0 → +5 (HIGH)
  - CPA > target × 1.3 → +3 (MEDIUM)
  - Пауза с конверсиями + расход > 100 → +4
  - Дубликат операции → +8 (HIGH, anti-double-spend)
  - Risk score ≥ 7 → `high`, ≥ 3 → `medium`, иначе → `low`
- `audit_via_llm()` — **заглушка** (`raise NotImplementedError`, строка 188)
- `audit_proposal(use_llm=True)` — молча вызывает `heuristic_audit()` и игнорирует флаг

**Интеграция с PolicyEngine** (`check_with_auditor`, строка 179-234):
```python
policy_result = self.check_budget_change(...)
if not policy_result.allowed:
    return policy_result  # уже заблокировано
audit_result = heuristic_audit(proposal, context)
if audit_result.risk == "high":
    return PolicyResult(allowed=True, risk="high", ...)
```

**Рубеж 3: Prompt Injection Guard** (`core/prompt_guard.py`, 182 строки):
- Gemini Flash 8B — классификация SAFE / INJECTION
- Отдельный `AsyncOpenAI` (не `agent.router.chat()`), `max_retries=0`, таймаут 5с
- Fail-open при ошибке сети/таймауте; fail-closed при INJECTION
- Короткие сообщения (<10 символов) пропускаются без LLM-вызова
- Длинные — обрезаются до 4000 символов
- Точки интеграции: `bot/handlers/fallback.py:on_text` + `bot/main.py:_run_task_with_context`

**Рубеж 4: Confirm-гейт** (`confirm/store.py:ConfirmStore`, 644 строки) — детально в §2.3.

**Рубеж 5: Провенанс** (`core/provenance.py`, 86 строк) — два независимых бита:
- `user_initiated` — аргумент `save_proposal` (может подделать вызывающий)
- `origin_human_turn` — contextvar, поднимается ТОЛЬКО `human_turn()` доверенного слоя
- `_require_user_command` (в `ads/mutations.py:117-138`) требует ОБА бита

**Рубеж 6: Квота Google Ads** (`core/quota.py`, 191 строка):
- Распределённый счётчик в таблице `ads_quota_ops` (не in-process deque)
- `check_mutation_allowed`: блокировка на ≥95% дневной квоты (fail-closed, ДО SDK)
- Чтения НЕ блокируются (нужны для безопасности), но учитываются
- Fail-safe при недоступном сторе: `record()` глотает ошибки; `check_mutation_allowed` пропускает (fail-open — осознанно: настоящий гейт — `ConfirmStore.claim` на той же БД)

### 2.3 Confirm-Gate & DRY_RUN — полный разбор

**Жизненный цикл черновика** (6 статусов, все переходы — CAS):

```
save_proposal (pending)
  │  params: confirmation_id, operation, customer_id, params, summary, chat_id
  │  штампует origin_human_turn из core.provenance
  │
  ├─ confirm (CAS: pending→confirmed)
  │   WHERE status='pending' AND chat_id=? AND created_at >= TTL-граница
  │   Гард владения: подтвердить может ТОЛЬКО владелец черновика
  │   Гард возраста: TTL в WHERE, не в джобе
  │   Защита от двойной доставки ✅: второй confirm → rowcount=0 → False
  │
  ├─ claim (CAS: confirmed→executing, ОДНОРАЗОВО)
  │   WHERE status='confirmed' AND operation=? AND created_at >= TTL-граница
  │   Защита от double-spend: второй claim → rowcount=0 → None → SDK не вызывается
  │   TTL в CAS: просрочка → None → PermissionError в _require_confirmation
  │
  ├─ SDK-вызов (ads/mutations.py:apply_*)
  │   _require_user_command: оба бита провенанса
  │   _require_freshness: attested_snapshot (гейт A)
  │   ensure_allowed(customer_id)
  │   run_ads_call / run_ads_create_call
  │
  ├─ finalize (executing→applied, терминальный)
  │   Guard: только из 'executing' → иначе log.warning + пропуск
  │
  ├─ record_failure (executing/confirmed→failed, терминальный)
  │   Уже терминальные applied/failed/rejected не трогаем
  │
  ├─ mark_needs_review (CAS: executing→needs_review, для реконсиляции)
  │   Процесс упал ПОСЛЕ claim → исход НЕИЗВЕСТЕН
  │
  ├─ record_verification (CAS: applied→needs_review, пост-проверка не сошлась)
  │
  └─ mark_confirmed_failed (CAS: confirmed→failed, для реконсиляции A6)
      Процесс упал ДО claim → SDK не вызывался → безопасный 'failed'
```

**Ключевые свойства:**
- **TTL в CAS, не в джобе** (`_ttl_boundary()`, строка 76-87): `created_at >= TTL-граница` — часть WHERE. Джоба-уборщик осталась только для перевода в `rejected` и очистки медиа; гардом она больше не является.
- **Защита от гонки confirm/reject** (оба CAS): `reject` тоже `UPDATE … WHERE status='pending'` — гонка с `confirm` разрешается атомарно.
- **Гард владения** (строка 284): `chat_id == proposal.chat_id` — утёкший/угаданный `confirmation_id` бесполезен без chat_id владельца.

**DRY_RUN** — глобальный флаг:
- `DRY_RUN` в `core/budget_policy.py:47` — читается из `.env` (`DRY_RUN=true` по умолчанию)
- Влияет только на PolicyEngine (добавляет предупреждение)
- Не форсируется на уровне `apply_*` — каждая функция сама решает, проверять ли
- **Риск:** новая `apply_*` может забыть проверку DRY_RUN

**2FA** (`core/config.py:172-189`, `core/twofa.py`):
- Опциональный PIN для опасных операций (`remove_campaign`, `update_budget`, `update_bid`, `update_keyword_bid`, `set_bidding_strategy`)
- `hmac.compare_digest` — constant-time сравнение
- Экспоненциальный lockout после `_TWOFA_MAX_ATTEMPTS` неверных попыток
- `two_factor_lockout_minutes` (default 15), растёт экспоненциально

---

## 3. Управление контекстом и Память

### 3.1 Обработка Context Rot — детально

**Три механизма управления контекстом:**

**1. Скользящее окно + эвристическое саммари** (`core/context_summarizer.py`, 223 строки, Фаза 1 — без LLM):

```
Конфигурация:
  LIVE_MESSAGE_WINDOW = 50      # последние 50 сообщений в «живом» контексте
  SUMMARY_COUNT = 3              # последние 3 саммари в system prompt
  MAX_SUMMARY_AGE_DAYS = 30      # авто-очистка старше
  MAX_SUMMARY_LEN = 500          # символов на саммари

Алгоритм:
  1. extract_window(messages, 50) → (live, archive)
  2. summarize_archive(archive) → ThreadSummary:
     - user_queries (первые 100 символов, топ-5 уникальных)
     - bot_actions (ключевые действия: «бюджет изменён», «кампания запущена», …)
     - error_reports (строки с «ошибка»/«error»/«🚨»)
  3. format_summary_for_prompt([s1, s2, s3]) → [История треда]
  4. Сохранение в memory с тегом [summary:YYYY-MM-DD:topic]
  5. prune_old_summaries — удаление старше 30 дней
```

**Ключевая проблема:** саммаризация чисто эвристическая. Без LLM качество сжатия низкое:
- Ключевые решения могут теряться
- Контекст диалога не сохраняет семантические связи между запросами
- Нет различения важного/неважного — все действия равны

**2. Контекст диалога для разрешения местоимений** (`agent/loop.py:79-174`):

```python
_CONTEXT_MAX = 8000            # потолок справочного контента (файлы/URL)
_HISTORY_TURNS = 4             # последние 4 реплики для разрешения ссылок

# C2: «эта кампания» → last_campaign из контекста чата
_is_pronoun_campaign(value)    # проверка: пусто / «эта» / «this campaign» / «текущая»
_resolve_pronoun_campaign()    # подстановка ДО показа карточки

# C3: блок «КОНТЕКСТ ДИАЛОГА» в system prompt
_conversation_context_block()  # last_campaign, last_account, последние 4 реплики
```

**3. MCP Envelope Compact** (`mcp_server/compact.py`, 112 строк):

```python
MAX_RAW_BYTES = 50_000         # порог для агрессивной обрезки
MAX_STRING_LEN = 200           # обрезка строковых полей
MAX_ROWS = 100                 # ограничение rows
```

Применяется для `get_search_terms`, `get_account_audit` через `ok(compact=True)`.

### 3.2 Долгосрочная память — текущее состояние и проблемы

**Текущее состояние:** FTS5 + in-memory через Hermes `memory` tool. **pgvector НЕ ИНТЕГРИРОВАН.**

| Компонент | Статус | Где |
|-----------|--------|-----|
| pgvector | ❌ Не реализован | Заявлен в `CLAUDE.md` и `aimash-architecture`, но отложен на Фазу 2 |
| Векторная таблица | ❌ Отсутствует | `db/models.py` — нет таблицы эмбеддингов |
| pgvector-сервис | ❌ Отсутствует | `docker-compose.yml` — только стандартный postgres:16 |
| Hermes memory tool | ✅ Работает | FTS5-индекс, сохраняет правила/контекст |
| Контекст-саммаризатор | ✅ Работает | Эвристический, без LLM |
| Контекст диалога | ✅ Работает | `last_campaign`, `last_account` — per-chat в `bot/main.py` |

**Архитектурный навык `ad-master-memory`:**
- `save_memory` для durable rules
- `session_search` для поиска по истории
- `memory` tool для FTS5-поиска
- **НЕТ семантического поиска** — правила извлекаются точным совпадением, а не по смыслу

**Что теряется без pgvector:**
- Правила типа «для кампаний в JPY не поднимай бюджет больше чем на 10%» не найдутся по запросу «японские ограничения»
- Контекст из прошлых сессий не релевантен семантически — только текстовый поиск
- Рекомендации advisor не учитывают похожие прошлые ситуации

---

## 4. Интеграции и Отказоустойчивость

### 4.1 API Clients — полный разбор

**Google Ads SDK** (`core/resilience.py`, 317 строк) — 3 стратегии с разной семантикой ретраев:

#### `run_ads_call` — МУТАЦИИ (НЕидемпотентные)

```python
ADS_TIMEOUT_S = 60.0            # таймаут на попытку (из env)
ADS_MAX_ATTEMPTS = 4            # макс попыток
ADS_WAIT_MULTIPLIER = 0.5       # начальный backoff
ADS_WAIT_MAX = 20.0             # потолок backoff

# Ретраим ТОЛЬКО:
RETRYABLE_ADS_MUTATE_NAMES = {
    "RESOURCE_EXHAUSTED",        # rate-limit
    "RATE_EXCEEDED",             # квота
    "RESOURCE_TEMPORARILY_EXHAUSTED",
    "TRANSIENT_ERROR",
}
# НЕ ретраим:
# - INTERNAL_ERROR, DEADLINE_EXCEEDED → исход неизвестен
# - TimeoutError → запрос мог пройти (asyncio.timeout)
```

**Поток:**
```
check_mutation_allowed (квота ≥95% → QuotaExceededError)
  → asyncio.Semaphore(ADS_MAX_CONCURRENCY=4)
    → retryer(_inner)
      → asyncio.timeout(ADS_TIMEOUT_S)
        → asyncio.to_thread(fn, *args)
  → quota.record(mutate, count=op_count)
```

#### `run_ads_create_call` — СОЗДАНИЕ (НЕ ретраим)

```python
# БЕЗ РЕТРАЕВ — для НЕидемпотентных создателей:
# composite-создание кампаний, ассеты/расширения.
# Сохраняет: check_mutation_allowed, timeout, семафор, quota.record.
```

#### `run_ads_read_call` — ЧТЕНИЯ (идемпотентны)

```python
# Ретраим ПОЛНЫЙ набор + TimeoutError:
RETRYABLE_ADS_NAMES = RETRYABLE_ADS_MUTATE_NAMES | {
    "INTERNAL_ERROR",
    "DEADLINE_EXCEEDED",
}
# + isinstance(exc, TimeoutError) → repeat safe
```

#### `call_llm` — OpenRouter

```python
LLM_TIMEOUT_S = 45.0            # таймаут на попытку (из env)
LLM_MAX_ATTEMPTS = 3            # макс попыток
LLM_WAIT_MULTIPLIER = 0.5
LLM_WAIT_MAX = 20.0

# Ретраим:
# RateLimitError, APITimeoutError, APIConnectionError,
# InternalServerError, TimeoutError
```

**Семафор конкурентности** (`_get_ads_semaphore`):
- `ADS_MAX_CONCURRENCY = 4` — глобальный потолок на ВСЕ вызовы Google Ads (чтение + мутации + создание)
- Пересоздаётся при смене event loop (pytest-asyncio)
- Слот берётся ДО ретрай-цикла — повторы не освобождают слот
- Под нагрузкой (параллельный /export + /audit + активный диалог) возможна блокировка

**Квота Google Ads** (`core/quota.py`, 191 строка):
- Распределённый счётчик в БД (не in-process) — работает для bot + scheduler + per-session MCP
- `_WARN_AT = 0.80` (80% дневного лимита) — однократный лог
- `_BLOCK_AT = 0.95` — `QuotaExceededError` для мутаций
- `_DB_OP_TIMEOUT_S = 2.0` — таймаут на операции со счётчиком

**Кэш SDK-клиентов** (`ads/client.py:59-69`):
```python
_CLIENT_CACHE: dict[str, GoogleAdsClient] = {}  # per-account
_OAUTH_RUNTIME: dict[str, tuple[str, str|None]] = {}  # расшифрованные креды
```
Каждый аккаунт получает свой `GoogleAdsClient` с собственным refresh-токеном и `login_customer_id`.

### 4.2 Изоляция доменов — детальный разбор

**Мульти-аккаунтность** (`ads/client.py`, 582 строки):

```
MCC 6283738601 (управляющий)
  ├── Draft 7753643025 (AUD) — разработка, всегда в потолке мутаций
  ├── Aimash 6764040266 (UAH)
  ├── Irisboutique 7990205915 (CZK)
  ├── Rozowy Słoń 8325477566 (PLN)
  ├── Art Or 9889330611 (USD)
  ├── Башня 5437782039 (UAH)
  └── DARIAL 1469059209 (JPY)
```

**Три замка доступа:**

| Замок | Функция | Что проверяет | Fail |
|-------|---------|--------------|------|
| **Мутации** | `ensure_allowed(cid)` | `cid ∈ allowed_customer_ids ⊆ allowed_ceiling()` | closed |
| **Чтение** | `ensure_read_allowed(cid)` | `cid ∈ (read_customer_ids ∪ allowed_customer_ids ∪ discovered_children)` | closed |
| **MCC-обход** | `ensure_manager_allowed(mid)` | `mid ∈ login_customer_id_set` | closed |

**Потолок мутаций** (`allowed_ceiling()`):
```python
ALLOWED_CEILING = frozenset({DRAFT_ACCOUNT_ID})  # код-минимум — всегда
allowed_ceiling() = ALLOWED_CEILING ∪ read_customer_ids ∪ discovered_children()
```
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` → набор = весь потолок (ПРОД-ДЕФОЛТ)
- Явный список → сужение до указанных id (каждый обязан быть в потолке)
- dev/test пусто → мутаций нет (fail-closed)

**Пер-пользовательская изоляция** (`account_access_mode`):
- `auto` (дефолт): пустая таблица грантов → legacy (все whitelisted видят всё)
- `enforced`: строгий режим даже с пустой таблицей
- `legacy`: только глобальный read-замок

**Интеграция с другими платформами:**
- **Meta Ads** → навык `meta-ads-worker` (не в этом репозитории)
- **TikTok Ads** → навык `tiktok-ads-worker` (не в этом репозитории)
- Интеграция через Hermes MCP + топики супергруппы

**Hermes-конфигурация** (`deploy/hermes/config.yaml`, 294 строки):
- Контур A (READ-only пилот): только MCP READ-инструменты
- `disabled_toolsets`: terminal, browser, file, code_execution, context_engine, cronjob, delegation, web, memory, vision, image_gen, tts, video, session_search
- `provider_routing.require_parameters: true` — предпосылка правила И8
- `provider_routing.data_collection: "deny"` — данные клиентов не для обучения
- `session_search` отключён осознанно (И6): кросс-клиентное чтение переписки

---

## 5. Слабые места, Уязвимости и Технический долг

### 5.1 🔴 Критические (денежный путь)

#### CRIT-1: PolicyEngine в MCP WRITE — неполная интеграция

**Файл:** `mcp_server/tools_writes.py:58-148`  
**Суть:** `_check_budget_policy()`, `_check_pause_policy()`, `_check_bid_policy()` передают placeholder-значения вместо реальных данных из Google Ads.

```python
# Строка 84-89: old_budget=1.0 — заглушка
result = engine.check_budget_change(
    campaign_id=campaign,
    campaign_name=campaign,
    old_budget=1.0,       # ⚠️ PLACEHOLDER — не реальный бюджет
    new_budget=new_budget,
    currency=currency or "USD",
)
```

**Последствия:**
- Для бюджета 400 USD изменение на 410: Δ = (410−1)/1×100 = 40,900% — ложное срабатывание BLOCKED
- Для бюджета 400 USD изменение на 500: Δ = (500−1)/1×100 = 49,900% — тоже BLOCKED
- `_check_pause_policy()`: `conversions_7d=0` всегда — пауза кампании с конверсиями не детектится
- `_check_bid_policy()`: `old_cpc=0.01` — та же проблема

**Рекомендация:**
```python
# Перед проверкой политики — живое чтение текущего состояния
async def _check_budget_policy_with_context(
    campaign: str, mode: str, value: float,
    account: str, currency: str | None = None,
) -> dict[str, Any] | None:
    engine = _get_policy_engine()
    if engine is None:
        return None
    
    # Живое чтение текущего бюджета кампании
    from ads.client import build_client_async
    from ads.read import campaign_budgets  # новый ридер
    client = await build_client_async(account)
    budgets = await run_ads_read_call(campaign_budgets, client, account, campaign)
    current_budget = budgets.get(campaign, 1.0)
    
    # Аналогично для конверсий при паузе
    if mode == "pause":
        stats = await run_ads_read_call(campaign_stats, client, account, campaign, days=7)
        conversions_7d = stats.conversions
    
    # Теперь проверка с реальными данными
    result = engine.check_budget_change(
        campaign_id=campaign,
        campaign_name=campaign,
        old_budget=current_budget,
        new_budget=new_budget,
        currency=currency or "USD",
    )
    ...
```

**Приоритет:** P0 — выполнить до включения WRITE в MCP-контуре.

---

#### CRIT-2: LLM-аудитор — заглушка

**Файл:** `core/auditor.py:184-188`  
**Суть:** `_audit_via_llm()` бросает `NotImplementedError`. Вторая линия защиты не существует.

```python
# Строка 184-188
async def _audit_via_llm(proposal, account_context) -> AuditResult:
    raise NotImplementedError("LLM auditor not yet implemented")
```

**Последствия:**
- Все проверки — только эвристические (пороги захардкожены)
- Контекстные риски не детектятся (например: «повышаем бюджет при падающем CTR»)
- Нет кросс-кампанийного анализа (например: «бюджет перераспределяется с конвертящей на неконвертящую»)

**Рекомендация:**
```python
# core/auditor.py — реализовать LLM-аудитор
async def _audit_via_llm(proposal, account_context) -> AuditResult:
    """Фаза 2: LLM-аудит с полным контекстом аккаунта."""
    from agent.router import chat
    
    context_text = json.dumps({
        "proposal": proposal,
        "account_summary": account_context.get("summary", {}),
        "recent_changes": account_context.get("recent_operations", [])[-5:],
        "campaign_metrics": account_context.get("current_metrics", {}),
    }, ensure_ascii=False)
    
    messages = [
        {"role": "system", "content": AUDITOR_SYSTEM_PROMPT},
        {"role": "user", "content": context_text},
    ]
    
    resp = await chat(messages, role="analyst", temperature=0.0, max_tokens=256)
    # Парсинг JSON-ответа и слияние с heuristic_audit
    ...
```

**Приоритет:** P1 — выполнить до широкого внедрения автономных оптимизаций.

---

#### CRIT-3: DRY_RUN не enforced на уровне apply_*

**Файлы:** `core/budget_policy.py:47`, `ads/mutations.py`  
**Суть:** DRY_RUN — глобальный флаг, который каждая `apply_*` функция должна проверять самостоятельно. Нет централизованного enforcement.

**Рекомендация:**
```python
# ads/mutations.py — добавить декоратор/гард
def _require_dry_run_check(func):
    """Декоратор: перед SDK-вызовом проверяет DRY_RUN."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        from core.budget_policy import DRY_RUN
        if DRY_RUN:
            log.info("DRY_RUN: %s skipped", func.__name__)
            return {"dry_run": True, "operation": func.__name__}
        return await func(*args, **kwargs)
    return wrapper
```

**Приоритет:** P1.

---

### 5.2 🟡 Средние (стабильность и архитектура)

#### MED-1: bot/main.py — 7447 строк монолита

**Файл:** `bot/main.py`  
**Проблема:** Один файл содержит всё: middleware, все хендлеры, визарды, confirm-flow, keywords-визард, GDN/Video-визарды, RSA-куратор, /audit flow, /mcc flow, /competitors, /journal, админку, /model, профиль клиента, досье, уведомления.

**Последствия:**
- При отказе любого импорта бот не стартует целиком
- Невозможно тестировать компоненты изолированно
- Сложность внесения изменений растёт экспоненциально

**Рекомендация:** Декомпозиция на `bot/routers/` с отдельными aiogram Router'ами:

```
bot/
├── main.py               # ~500 строк: только диспетчер + middleware + set_my_commands
├── routers/
│   ├── __init__.py
│   ├── commands.py        # /start, /help, /lang, /model, /settings
│   ├── mutations.py       # confirm-flow: ✅/❌, /journal, rollback
│   ├── campaigns.py       # /newsearch, /newgdn, /newvideo, /newdg, /cc, /rsa
│   ├── keywords.py        # /keywords, /addkeys, keyword wizard
│   ├── reports.py         # /report, /mcc, /export, /sheets
│   ├── audit.py           # /audit flow + Q&A
│   ├── competitors.py     # /competitors
│   ├── clients.py         # /client, досье, краул
│   ├── admin.py           # /grant, /revoke, /adduser, /addadmin, /diag
│   └── account.py         # /account, /quota, /balance
├── callbacks.py           # typed CallbackData (уже есть)
├── keyboards.py           # reply/inline-клавиатуры (уже есть)
├── ux.py                  # отправка карточек (уже есть)
├── proposal.py            # build_proposal (уже есть)
├── states.py              # FSM-состояния (уже есть)
└── throttle.py            # message-throttle (уже есть)
```

**Приоритет:** P2 — технический долг, не блокирует релиз.

---

#### MED-2: PolicyEngine state — файловая система вместо БД

**Файл:** `core/budget_policy.py:60-61, 375-392`  
**Суть:** Cooldown-состояние хранится в JSON-файле, а не в БД.

```python
STATE_DIR = Path.home() / ".hermes" / "admaster"
STATE_FILE = STATE_DIR / "budget_policy_state.json"
```

**Последствия:**
- При рестарте контейнера без volume состояние теряется
- Два процесса (bot + scheduler) — гонка на файле
- В per-session MCP состояние изолировано (каждый процесс — свой файл)

**Рекомендация:**
```python
# Перенести cooldown в таблицу БД
class BudgetCooldown(Base):
    __tablename__ = "budget_cooldowns"
    campaign_id: Mapped[str]       # "account:campaign_id"
    last_action: Mapped[str]
    last_change_at: Mapped[datetime]
    created_at: Mapped[datetime]

# PolicyEngine._get_last_change → SQL-запрос
# PolicyEngine.record_change → INSERT/UPDATE
```

**Приоритет:** P2.

---

#### MED-3: Отсутствие pgvector RAG

**Заявлен в:** `CLAUDE.md`, `aimash-architecture` skill  
**Фактически:** не реализован.

**Рекомендация:**
```dockerfile
# docker-compose.yml
postgres:
  image: pgvector/pgvector:pg16  # вместо postgres:16
```

```python
# db/models.py
from pgvector.sqlalchemy import Vector

class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"
    id: Mapped[int]
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    metadata_: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime]

# Поиск: SELECT * FROM memory_embeddings 
#        ORDER BY embedding <=> query_embedding LIMIT 10
```

**Приоритет:** P2-P3 — важно для качества рекомендаций, не блокирует базовый функционал.

---

#### MED-4: Семафор Google Ads — глобальный на процесс

**Файл:** `core/resilience.py:59-73`  
**Суть:** `ADS_MAX_CONCURRENCY = 4` — один семафор на ВСЕ операции (чтение + мутации + создание).

**Последствия:** Под нагрузкой (параллельный `/export` всех аккаунтов + `/audit` + активный диалог) возможна блокировка.

**Рекомендация:**
```python
# Раздельные семафоры для чтения и мутаций
ADS_MAX_CONCURRENCY_READ = 6     # чтения — идемпотентны, можно больше
ADS_MAX_CONCURRENCY_MUTATE = 2   # мутации — жёстче (деньги)
ADS_MAX_CONCURRENCY_CREATE = 1   # создание — строго последовательно
```

**Приоритет:** P3.

---

#### MED-5: Shadow Mode — заглушка

**Файл:** `scheduler/shadow_mode.py` (258 строк)  
**Суть:** `_generate_recommendations()` возвращает пустой список. `_compare_with_reality()` всегда возвращает `would_help=None`. Реальный пайплайн не реализован.

**Рекомендация:** Интегрировать с `get_account_audit` и `detect_anomalies` для работающего теневого тестирования.

**Приоритет:** P3.

---

### 5.3 🟢 Низкие (качество кода и edge cases)

#### LOW-1: Дублирование audit-логики

`core/auditor.py:heuristic_audit()` и `core/budget_policy.py:PolicyEngine._evaluate()` имеют пересекающиеся проверки (Δ бюджета, CPA vs target, пауза с конверсиями) с разными порогами:
- PolicyEngine: Δ > 20% → BLOCKED
- Auditor: Δ > 50% → +5 risk, > 20% → не проверяется

**Рекомендация:** Унифицировать пороги. Auditor должен быть надстройкой над PolicyEngine, а не дублировать проверки.

---

#### LOW-2: BILLING_UNIT_MICROS_BY_CURRENCY — неполный список

**Файл:** `core/limits.py:37-46`  
**Суть:** 25 валют из 147 поддерживаемых Google Ads. Для неизвестной валюты — дефолт 10 000 micros.

**Рекомендация:** Дополнить список всеми 147 валютами из `CurrencyConstant.billable_unit_micros` (v24). Или добавить живой запрос к API при старте с кэшированием.

---

#### LOW-3: Контекст-саммаризатор — только эвристический

**Файл:** `core/context_summarizer.py`  
**Суть:** Фаза 2 (LLM-саммаризатор) не реализована. Качество сжатия низкое — ключевые решения могут теряться.

**Рекомендация:**
```python
# Добавить LLM-саммаризацию как fallback для эвристики
async def summarize_archive_llm(messages, topic):
    """Вызвать дешёвую модель (parsing) для саммаризации архива."""
    text = "\n".join(f"[{m.sender}] {m.text[:200]}" for m in messages)
    resp = await chat([
        {"role": "system", "content": "Сожми историю диалога до 300 символов: ключевые запросы, действия, ошибки."},
        {"role": "user", "content": text[:4000]},
    ], role="parsing", max_tokens=300)
    return resp.content
```

---

#### LOW-4: Отсутствие Eval-датасета

`CLAUDE.md` упоминает «golden dataset ≥20 сценариев» как 🔨. Evals для оценки агентных решений отсутствуют.

**Рекомендация:**
1. Создать `evals/` с golden-датасетом:
   - 20+ сценариев: «повысь бюджет на 10%», «останови кампанию X», «покажи статистику за неделю», …
   - Для каждого: входной текст → ожидаемый tool_call с аргументами
2. Интегрировать с `pytest`:
   ```python
   @pytest.mark.parametrize("input_text,expected_tool,expected_args", GOLDEN_DATASET)
   async def test_command_parsing(input_text, expected_tool, expected_args):
       result = await handle_command(input_text, chat_id=0)
       assert result["operation"] == expected_tool
       for k, v in expected_args.items():
           assert result["params"][k] == v
   ```

---

#### LOW-5: Отсутствие property-based тестов для денежного пути

hypothesis используется только для `test_rsa_length_properties.py`.

**Рекомендация:**
```python
from hypothesis import given, strategies as st

@given(
    old_budget=st.floats(min_value=0.01, max_value=100000),
    delta_pct=st.floats(min_value=-50, max_value=100),
)
def test_budget_policy_consistency(old_budget, delta_pct):
    engine = PolicyEngine()
    new_budget = old_budget * (1 + delta_pct / 100)
    result = engine.check_budget_change("c1", "Test", old_budget, new_budget)
    if abs(delta_pct) <= 20:
        assert result.allowed
    else:
        assert not result.allowed
```

---

#### LOW-6: Edge case — пустой аккаунт

`audit/engine.py` возвращает `score=None` при отсутствии активности. Нарратив аналитика может hallucinate на пустом аудите.

**Рекомендация:** Добавить guard в `agent/loop.py:_run_audit_narrative`:
```python
if audit_result.score is None:
    return None  # fallback на детерминированную карточку без нарратива
```

---

#### LOW-7: Telegram flood limits при большом дайджесте

`advise_digest_send_pause = 0.7` — пауза между сообщениями. При 10 аккаунтах × 3 находки = 30 сообщений × 0.7с = 21 секунда. Пользователь ждёт.

**Рекомендация:** Группировать находки в одно сообщение где возможно. Или перейти на Telegram-галерею (media group).

---

### 5.4 Сводная таблица рекомендаций

| ID | Проблема | Приоритет | Усилие | Риск без исправления |
|----|---------|-----------|--------|---------------------|
| CRIT-1 | PolicyEngine в MCP: placeholder-значения | P0 | 3-5 дней | WRITE-операции через MCP без реальной проверки политик |
| CRIT-2 | LLM-аудитор — заглушка | P1 | 3-5 дней | Вторая линия защиты отсутствует |
| CRIT-3 | DRY_RUN не enforced | P1 | 1-2 дня | Новая apply_* может выполнить реальную мутацию при DRY_RUN=true |
| MED-1 | bot/main.py — 7447 строк | P2 | 5-7 дней | Сложность поддержки, риск отказа всего бота |
| MED-2 | PolicyEngine state в JSON | P2 | 2-3 дня | Потеря cooldown при рестарте, гонка процессов |
| MED-3 | pgvector RAG не реализован | P2-P3 | 5-7 дней | Низкое качество семантического поиска правил |
| MED-4 | Общий семафор Google Ads | P3 | 1 день | Возможна блокировка под нагрузкой |
| MED-5 | Shadow Mode — заглушка | P3 | 3-5 дней | Нет теневого тестирования |
| LOW-1 | Дублирование audit-логики | P3 | 1-2 дня | Расхождение порогов |
| LOW-2 | Неполный BILLING_UNIT | P3 | 1 день | API-отказ для неизвестных валют ПОСЛЕ ✅ |
| LOW-3 | Саммаризатор без LLM | P3 | 2-3 дня | Потеря контекста в длинных тредах |
| LOW-4 | Нет Eval-датасета | P2 | 3-5 дней | Регрессии парсинга команд не детектятся |
| LOW-5 | Нет property-based тестов | P3 | 2-3 дня | Краевые случаи не покрыты |
| LOW-6 | Пустой аккаунт → hallucination | P3 | 1 час | Дезориентация пользователя |
| LOW-7 | Дайджест — медленная отправка | P3 | 1-2 дня | Плохой UX при большом портфеле |

---

## Приложение A: Карта ключевых файлов

| Файл | Строк | Назначение | Критичность |
|------|-------|-----------|-------------|
| `bot/main.py` | 7447 | Telegram-бот, все хендлеры, визарды, confirm-flow | 🔴 Монолит |
| `ads/mutations.py` | 4220 | Google Ads SDK-вызовы (50+ apply_*) | 🔴 Денежное ядро |
| `audit/engine.py` | 3593 | Детерминированный аудит-движок (score/grade/находки) | 🟡 Диагностика |
| `scheduler/jobs.py` | 1902 | Фоновые джобы (отчёты, аномалии, очистка) | 🟡 Автономность |
| `agent/tools/schemas.py` | 1658 | Pydantic-схемы всех READ+WRITE инструментов | 🔴 Валидация |
| `mcp_server/tools_writes.py` | 1370 | 39 propose_* + execute_confirmed | 🔴 WRITE-MCP |
| `core/config.py` | 650 | Конфигурация (pydantic-settings, SecretStr) | 🔴 Основа |
| `confirm/store.py` | 644 | CAS claim + audit-журнал | 🔴 Денежный гейт |
| `agent/loop.py` | 678 | ReAct-цикл + audit-нарратив | 🔴 Оркестрация |
| `ads/client.py` | 582 | Клиенты + замки доступа + кэш OAuth | 🔴 Доступ |
| `mcp_server/tools_read.py` | 581 | 12 READ-инструментов | 🟡 Чтение |
| `core/budget_policy.py` | 446 | PolicyEngine: Δ≤20%, cooldown, DRY_RUN | 🔴 Гард |
| `agent/router.py` | 342 | Model router: 8 ролей + Langfuse + fallback | 🟡 LLM |
| `core/resilience.py` | 317 | Таймауты/ретраи/семафор (3 стратегии) | 🔴 Сеть |
| `deploy/hermes/config.yaml` | 294 | Эталонный конфиг Hermes (Контур A) | 🟡 Деплой |
| `db/session.py` | 272 | Async-движок + advisory-lock + db_dt | 🟡 БД |
| `scheduler/shadow_mode.py` | 258 | Теневое тестирование (заглушка) | 🟢 Dev |
| `core/context_summarizer.py` | 223 | Скользящее окно + эвристическое саммари | 🟡 Контекст |
| `core/quota.py` | 191 | Распределённый счётчик квоты Google Ads | 🟡 Гард |
| `core/auditor.py` | 187 | Финансовый аудитор (эвристический + заглушка LLM) | 🔴 Гард |
| `core/prompt_guard.py` | 182 | Prompt Injection фильтр (Gemini Flash 8B) | 🟡 Безопасность |
| `mcp_server/envelope.py` | 158 | Единый конверт MCP-ответов + code_numbers | 🟡 Контракт |
| `core/limits.py` | 143 | Числовые пороги + биллинг-единицы валют | 🔴 Пороги |
| `core/context.py` | 122 | request_scope + request_id + провенанс | 🟡 Наблюдаемость |
| `mcp_server/compact.py` | 112 | Сжатие MCP-конвертов | 🟢 Оптимизация |
| `scheduler/anomaly.py` | 90 | Детектор аномалий (чистая логика) | 🟡 Мониторинг |
| `core/provenance.py` | 86 | Провенанс хода (contextvar) | 🔴 Безопасность |
| `mcp_server/server.py` | 65 | FastMCP-реестр + гарды И4/И5 | 🔴 Инварианты |
| `core/guards.py` | 53 | Construction-time гарды (не assert) | 🔴 Инварианты |
| `confirm/gate.py` | 47 | Proposal dataclass + build_summary | 🟡 Контракт |
| `Dockerfile` | 32 | Multi-stage, non-root, healthcheck | 🟡 Деплой |
| `pyproject.toml` | 98 | Зависимости + ruff/mypy/pytest-конфиг | 🟡 Сборка |

## Приложение B: MCP-инструменты — полный реестр

### READ (14 инструментов)

| Инструмент | Функция | Замок |
|-----------|---------|-------|
| `get_campaign_stats` | Метрики по кампаниям за период | `ensure_read_allowed` |
| `get_adgroup_stats` | Метрики по группам объявлений | `ensure_read_allowed` |
| `get_ads` | Метрики по объявлениям (топ по расходу) | `ensure_read_allowed` |
| `get_keywords` | Метрики по ключевым словам | `ensure_read_allowed` |
| `get_search_terms` | Поисковые запросы (search_term_view) | `ensure_read_allowed` |
| `get_auction_insights` | Impression share (Search/Shopping) | `ensure_read_allowed` |
| `get_budgets` | Дневные бюджеты кампаний | `ensure_read_allowed` |
| `get_negatives` | Минус-слова трёх уровней + карта shared-списков | `ensure_read_allowed` |
| `get_change_history` | История применённых ботом операций | `ensure_read_allowed` |
| `get_account_audit` | Полный аудит аккаунта (score/grade/находки) | `ensure_read_allowed` |
| `keyword_ideas` | Keyword Planner по сид-ключам/URL | `ensure_read_allowed` |
| `get_mcc_summary` | Сводка по дочерним MCC (лёгкая) | `ensure_manager_allowed` |
| `get_mcc_deep` | Глубокая сводка (totals+prev на аккаунт) | `ensure_manager_allowed` |
| `list_accounts` | Дочерние аккаунты MCC | `ensure_manager_allowed` |

### WRITE (40 инструментов: 39 propose_* + 1 execute_confirmed)

Все propose_* идут через `_guarded_write` → `ensure_allowed(account)` → `build_proposal` → confirmation_id.

---

## Приложение C: Оценка зрелости по измерениям

| Измерение | Оценка | Обоснование |
|-----------|--------|-------------|
| **Безопасность денежного пути** | ⭐⭐⭐⭐⭐ | 6 рубежей: PolicyEngine → Auditor → Confirm-гейт (CAS) → Provenance → Квота → Prompt Guard. Каждый fail-closed. |
| **Архитектурная целостность** | ⭐⭐⭐⭐ | Чёткое разделение READ/WRITE. Инварианты И4/И5 при импорте. Монолит `bot/main.py` — единственный smell. |
| **Отказоустойчивость** | ⭐⭐⭐⭐ | 3 стратегии ретраев с дифференциацией мутации/чтения/LLM. Fallback-модель. Семафор. Нет State Recovery. |
| **Тестирование** | ⭐⭐⭐ | 50+ тестовых файлов, хорошее покрытие. Нет Eval-датасета, property-based для денег, интеграционных с Google Ads. |
| **Управление контекстом** | ⭐⭐⭐ | Скользящее окно + эвристическое саммари + компакшен конвертов. Нет pgvector RAG и LLM-саммаризации. |
| **Завершённость функций** | ⭐⭐⭐ | PolicyEngine в MCP — неполная. LLM-аудитор — заглушка. Shadow Mode — заглушка. pgvector — не реализован. |
| **Наблюдаемость** | ⭐⭐⭐⭐ | Langfuse-трейсинг + Sentry + контекстная корреляция + квота + /diag + алерты. |
| **Деплой** | ⭐⭐⭐⭐ | Multi-stage Docker, non-root, healthcheck, миграции при старте, advisory-lock, .env-секреты. |
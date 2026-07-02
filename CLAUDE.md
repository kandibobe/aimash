# Aimash — ИИ-агент управления Google Ads через Telegram

## 📌 ОСНОВА ПРОЕКТА — ТЗ (источник истины)
**Всё в этом проекте делается строго по ТЗ.** Перед любой работой опирайся на:
- **[`ТЗ.md`](ТЗ.md)** — полный текст ТЗ (источник истины). Оригинал: `Aimash_Technical_Specification.docx`.
- При расхождении кода/решений и ТЗ — **прав ТЗ**; отклонения фиксируются явно в план-файле/договоре.
- Уточнения к ТЗ после ресёрча (модель сменяемая, UAC исключён, v24.2, кириллица=1, токен Basic) — см. `ТЗ.md` (шапка) и план-файл.

Полный бриф/решения/цена/риски: `C:\Users\Владислав\.claude\plans\aimash-technical-specification-docx-dazzling-church.md`.

## Что это
Telegram-бот, который по командам на естественном языке (RU/EN) **читает и изменяет** рекламные кампании Google Ads на уровне менеджерского аккаунта (MCC). Агент — **исполнитель, не автономный оптимизатор**: перед любым изменением показывает «было → станет» и ждёт подтверждения «да».

## 🔒 Золотые правила (НЕ нарушать — это про чужие деньги)
1. **Confirm-гейт.** Любая мутация Google Ads выполняется ТОЛЬКО после явного «да» пользователя. Мутация и подтверждение **разделены**: модель/агент лишь СОЗДАЁТ черновик (proposal), выполняет — код, после подтверждения.
2. **`confirmation_id` обязателен.** Каждая функция в `ads/mutations.py` принимает `confirmation_id` и проверяет, что он соответствует подтверждённой строке в `audit_log`. Без него — `raise`, не выполнять.
3. **Бюджет — только по прямой команде пользователя.** Никогда не менять бюджет из scheduler/anomaly. Это гард в коде, не в промпте.
4. **Длину символов считает КОД, а не модель.** RSA: headline ≤30, description ≤90, path ≤15. **Кириллица = 1 символ** (двойная ширина только у CJK). Считать по Unicode code points (`len(str)`), НЕ по UTF-8 байтам. Перегенерировать, если превышено.
5. **Секреты — никогда в промпт, логи, гит И ЛЮБОЙ выход наружу.** developer token / refresh token / API keys только из env/секрет-хранилища. **Хранение токенов (тест-фаза):** единственный refresh-токен живёт в `.env` как `SecretStr` (маскируется в логах/repr/трейсбеках), под защитой прав файла/секрет-хранилища деплоя. Шифрование at-rest в БД (`core.secrets.encrypt`/`decrypt` + таблица `oauth_tokens`) — per-account OAuth-токены **подключены к рантайму** (`ads.client.load_oauth_cache` на старте, см. правило 9 и `docs/DATABASE.md`); используется §8-чтением дочерних. Мутации по-прежнему только Draft. Редакция — не только в логах: **сырой текст исключения (`str(e)`) НЕ слать пользователю в Telegram** — через `bot.ux.err_text` (редактирует секрето-подобное); в `audit_log` ошибка пишется уже через `redact_text` (`confirm.store.record_failure`); строки с паролем (DSN) — `SecretStr` (см. `core.logging.redact_text`, `core.config.database_url`). Глобальный `dp.errors`-хендлер пишет ошибку в лог (редактированно), а не в stderr/чат.
6. **Модель не трогает SDK напрямую.** LLM заполняет типизированную (Pydantic) схему → код валидирует диапазоны → показывает diff → ждёт «да» → вызывает `google-ads` SDK → пишет audit-row.
7. **Только TEST MCC при разработке.** Никаких боевых аккаунтов, пока всё не проверено на тесте. dev-профиль по умолчанию указывает на test MCC.
8. **Жёсткий allow-list операций.** Агент может вызывать только заранее перечисленные инструменты; защита от prompt-injection — confirm-гейт + код-level allow-list.
9. **Замок аккаунта (раздельно ЧТЕНИЕ и МУТАЦИИ).** МУТАЦИИ разрешены ТОЛЬКО на `Aimash (Draft)` = **`7753643025`** (775-364-3025) — чокпойнт `ads.client.ensure_allowed(customer_id)`: потолок `ALLOWED_CEILING` зашит в КОДЕ (env не расширит), пустой allow-list = **fail-closed**. Расширение круга МУТАЦИЙ = осознанная правка `ads/client.py`, не строка в `.env`. ЧТЕНИЕ per-account — ОТДЕЛЬНЫЙ чокпойнт `ensure_read_allowed(customer_id)`: множество = мутационный список ∪ `GOOGLE_ADS_READ_CUSTOMER_IDS` (env) ∪ **дочерние, обнаруженные обходом MCC на старте** (`ads.client.discover_read_children` под `ensure_manager_allowed`; §8-полный-мульти-аккаунт). Все три пусты = **fail-closed**. Это позволяет §8 (сводка по дочерним MCC) читать дочерние, **НЕ открывая мутаций на них** (мутация на дочернем всё равно упрётся в `ensure_allowed`; инварианты `test_mutation_lock_unchanged_by_read_allowlist`, `test_discovered_child_readable_but_not_mutable`). **Текущее состояние §8 — РЕАЛИЗОВАНО:** авто-обход дочерних на старте + команда `/mcc` (`reports.mcc.build_mcc_summary_async` → `summary_text_mcc`) + подытоги по валютам (без FX, golden rule 4) + нормализация таймзон per-child + per-account OAuth-токены (`load_oauth_cache`). МУТАЦИИ по-прежнему только на Draft.
10. **Fail-closed везде (никогда fail-open).** Любой гейт при отсутствии конфигурации ОТКАЗЫВАЕТ, не открывает: whitelist (`bot.main.WhitelistMiddleware`) при пустом наборе блокирует ВСЕХ (НЕ `if wl and ...` — это fail-open); `ensure_allowed`/`ensure_manager_allowed` при пустом allow-list/login — отказ; `user_initiated` по умолчанию `False`. В `prod` пустой whitelist или ключ шифрования роняет старт (fail-fast, `core.config`). Dev-скрипты с прямой записью (минуя confirm-гейт) — только `ENV=dev`.

## Архитектура (поток)
```
Telegram (aiogram) → handler → агент (model via OpenRouter, tool use)I
   ├─ READ-инструменты  → выполняются сразу (GAQL через google-ads SDK)
   └─ MUTATION-инструменты → создают proposal (diff «было→станет») в БД, НЕ выполняют
→ бот показывает сводку + inline ✅ Подтвердить / ❌ Отмена
→ на «да»: выполнить proposal через SDK + записать audit-row (по confirmation_id)
```

## Модель (СМЕНЯЕМАЯ — не зашивать одну)
- Все вызовы модели идут через **OpenRouter** (OpenAI-совместимый API) за тонким адаптером `agent/router.py`. Смена модели = `LLM_PARSING`/`LLM_COPY`/`LLM_FALLBACK` в `.env` (+ рантайм `/model`).
- Дефолт — дешёвая (`deepseek/deepseek-chat` или `nousresearch/hermes-4-70b`); финальный выбор — по A/B-тесту (`scripts/ab_test_models.py`).
- Можно разные модели на парсинг (дешёвая) и копирайт (посильнее).
- Решение по модели — **по данным теста**, не по бренду.

## Стек
Python 3.12 · aiogram 3.x (async; один event loop с APScheduler) · `openai` SDK (→ OpenRouter) · `google-ads` SDK — **пин `google-ads>=31.1,<32` → API `v24`** (lib-версия ≠ API-версии!; релизы ежемесячные, v24 сансет ~май 2027; перепроверять ежемесячно — скил `gads-version`, ссылки `docs/gads-api-refs.md`) · SQLAlchemy + Alembic + PostgreSQL · openpyxl / google-api-python-client (Sheets, scope `drive.file`) · Docker.

## Структура
- `core/` — config, secrets (шифрование), logging.
- `bot/` — aiogram handlers, keyboards (inline), middleware/whitelist.
- `agent/` — router (OpenRouter), system_prompt (из скилов-инструкций после ревью), tools (Pydantic-схемы read+mutation), loop.
- `ads/` — auth (OAuth/refresh, login_customer_id=MCC), read (GAQL через paged `Search`; SearchStream осознанно отложен — см. docstring `ads/read.py`), mutations (требуют confirmation_id), keyword_plan, assets.
- `confirm/` — proposal (diff), gate (логика «да»), audit.
- `db/` — модели (whitelist, audit_log, proposals, user_settings) + Alembic.
- `reports/`, `adcopy/` (генерация+валидация текстов; имя не `copy`, чтобы не затенять stdlib), `scheduler/` — фазы 2–3.
- `clients/` — §20 «Информация про клиентов»: профиль клиента на `customer_id` (store), LLM-разбор текста (profile_extract), исполнитель memory-операций за confirm-гейтом (execute — НЕ через ads.mutations), статический краулер сайта (crawler) + журнал задач (crawl_jobs). Профиль подаётся как контекст в генераторы §19/§10 (seed-ключи/релевантность/RSA/ассеты).
- `scripts/ab_test_models.py` — A/B-тест моделей через OpenRouter.

## Фазы (статус — по коду/коммитам; см. `docs/ACCEPTANCE.md`)
- **Фаза 0 ✅:** read-MVP — auth + чтение MCC (GAQL) + скелет бота + whitelist + Postgres.
- **Фаза 1 ✅:** confirm-гейт + запись по Search (бюджет/ставка/ключи) + audit.
- **Фаза 2 ✅:** отчёты + Sheets/.xlsx + генерация текстов.
- **Фаза 3 ✅:** keyword research + кластеризация + scheduler/алерты.
- **Дополнения ✅:** §8 read-MCC (`/mcc`), §11 GDN/Video/Demand Gen из медиа, §19 визард `/newcampaign`, §20 «Клиенты» (`/clients` + краулинг), RU/EN. **Сейчас — предсдаточный аудит/доведение.**
- **Открытый объём:** §8 полный (мутации на дочерних), распределённая квота. **App/UAC — ИСКЛЮЧЕНО** (нет приложения у клиента).

## Команды для модели разработки (Claude Code)
- Архитектура/сложное — Opus 4.8; объём (CRUD, отчёты, тесты, бойлерплейт) — Sonnet 4.6.
- Релевантные скилы: `new-mutation`, `gaql-query`, `check-rsa-copy`, `gads-version`, `confirm-gate-audit`.

## Что НЕ делать
- Не вызывать `ads/mutations.py` без `confirmation_id`.
- Не вставлять секреты в код/логи/промпт.
- Не работать против боевых аккаунтов. **Любой `customer_id`, кроме `7753643025`, — запрещён** (золотое правило 9).
- Не ослаблять замок аккаунта через `.env` (потолок — в коде); не делать `ensure_allowed` fail-open.
- Не доверять готовым write-MCP как бэкенду (экспериментальны, без подтверждений) — write-слой пишем сами.

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
5. **Секреты — никогда в промпт, логи, гит.** developer token / refresh token / API keys только из env/секрет-хранилища, шифрование at-rest. Логировать без секретов.
6. **Модель не трогает SDK напрямую.** LLM заполняет типизированную (Pydantic) схему → код валидирует диапазоны → показывает diff → ждёт «да» → вызывает `google-ads` SDK → пишет audit-row.
7. **Только TEST MCC при разработке.** Никаких боевых аккаунтов, пока всё не проверено на тесте. dev-профиль по умолчанию указывает на test MCC.
8. **Жёсткий allow-list операций.** Агент может вызывать только заранее перечисленные инструменты; защита от prompt-injection — confirm-гейт + код-level allow-list.

## Архитектура (поток)
```
Telegram (aiogram) → handler → агент (model via OpenRouter, tool use)
   ├─ READ-инструменты  → выполняются сразу (GAQL через google-ads SDK)
   └─ MUTATION-инструменты → создают proposal (diff «было→станет») в БД, НЕ выполняют
→ бот показывает сводку + inline ✅ Подтвердить / ❌ Отмена
→ на «да»: выполнить proposal через SDK + записать audit-row (по confirmation_id)
```

## Модель (СМЕНЯЕМАЯ — не зашивать одну)
- Все вызовы модели идут через **OpenRouter** (OpenAI-совместимый API) за тонким адаптером `agent/router.py`. Смена модели = `MODEL_*` в `.env`.
- Дефолт — дешёвая (`deepseek/deepseek-chat` или `nousresearch/hermes-4-70b`); финальный выбор — по A/B-тесту (`scripts/ab_test_models.py`).
- Можно разные модели на парсинг (дешёвая) и копирайт (посильнее).
- Решение по модели — **по данным теста**, не по бренду.

## Стек
Python 3.12 · aiogram 3.x (async; один event loop с APScheduler) · `openai` SDK (→ OpenRouter) · `google-ads` SDK (пин **v24.x**, текущая v24.2; перепроверять ежемесячно) · SQLAlchemy + Alembic + PostgreSQL · openpyxl / google-api-python-client (Sheets, scope `drive.file`) · Docker.

## Структура
- `core/` — config, secrets (шифрование), logging.
- `bot/` — aiogram handlers, keyboards (inline), middleware/whitelist.
- `agent/` — router (OpenRouter), system_prompt (из скилов-инструкций после ревью), tools (Pydantic-схемы read+mutation), loop.
- `ads/` — auth (OAuth/refresh, login_customer_id=MCC), read (GAQL/SearchStream), mutations (требуют confirmation_id), keyword_plan, assets.
- `confirm/` — proposal (diff), gate (логика «да»), audit.
- `db/` — модели (whitelist, audit_log, proposals, user_settings) + Alembic.
- `reports/`, `adcopy/` (генерация+валидация текстов; имя не `copy`, чтобы не затенять stdlib), `scheduler/` — фазы 2–3.
- `scripts/ab_test_models.py` — A/B-тест моделей через OpenRouter.

## Фазы
- **Фаза −1 (сейчас):** A/B-тест моделей + спайк nanobot (каркас A vs тонкий кастом B).
- **Фаза 0:** read-MVP — auth + чтение MCC (GAQL) + скелет бота + whitelist + Postgres.
- **Фаза 1:** confirm-гейт + запись по Search (бюджет/ставка/ключи) + audit.
- **Фаза 2:** отчёты + Sheets/.xlsx + генерация текстов.
- **Фаза 3:** keyword research + кластеризация + scheduler/алерты.
- App/UAC — исключено (нет приложения у клиента).

## Команды для модели разработки (Claude Code)
- Архитектура/сложное — Opus 4.8; объём (CRUD, отчёты, тесты, бойлерплейт) — Sonnet 4.6.
- Релевантные скилы: `new-mutation`, `gaql-query`, `check-rsa-copy`, `gads-version`, `confirm-gate-audit`.

## Что НЕ делать
- Не вызывать `ads/mutations.py` без `confirmation_id`.
- Не вставлять секреты в код/логи/промпт.
- Не работать против боевых аккаунтов.
- Не доверять готовым write-MCP как бэкенду (экспериментальны, без подтверждений) — write-слой пишем сами.

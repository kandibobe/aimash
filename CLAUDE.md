# Aimash — ИИ-агент управления Google Ads через Telegram

## 📌 ОСНОВА ПРОЕКТА — ТЗ (источник истины)
**Всё в этом проекте делается строго по [`ТЗ.md`](ТЗ.md)** (оригинал: `Aimash_Technical_Specification.docx`).
При расхождении кода/решений и ТЗ — **прав ТЗ**; отклонения фиксируются явно в план-файле/договоре.
Уточнения после ресёрча (модель сменяемая, UAC исключён, v24.2, кириллица=1, токен Basic) — в шапке `ТЗ.md`.
Полный бриф/решения/цена/риски: `C:\Users\Владислав\.claude\plans\aimash-technical-specification-docx-dazzling-church.md`.

## Что это
Telegram-бот, который по командам на естественном языке (RU/EN) **читает и изменяет** рекламные кампании
Google Ads на уровне менеджерского аккаунта (MCC). Агент — **исполнитель, не автономный оптимизатор**:
перед любым изменением показывает «было → станет» и ждёт подтверждения «да».

## 🔒 Золотые правила (НЕ нарушать — это про чужие деньги)
1. **Confirm-гейт.** Любая мутация Google Ads выполняется ТОЛЬКО после явного «да» пользователя. Мутация и подтверждение **разделены**: модель/агент лишь СОЗДАЁТ черновик (proposal), выполняет — код, после подтверждения.
2. **`confirmation_id` обязателен.** Каждая функция в `ads/mutations.py` принимает `confirmation_id` и проверяет, что он соответствует подтверждённой строке. Без него — `raise`, не выполнять. Подтверждение одноразовое и привязано к своей операции (атомарный `ConfirmStore.claim`).
3. **Бюджет/ставка — только по прямой команде пользователя** (`user_initiated`). Никогда из scheduler/anomaly. Это гард в коде, не в промпте.
4. **Длину символов считает КОД, а не модель.** RSA: headline ≤30, description ≤90, path ≤15. **Кириллица = 1 символ** (двойная ширина только у CJK). Считать по Unicode code points (`len(str)`), НЕ по UTF-8 байтам. Перегенерировать, если превышено. Реализация — `adcopy/validate.py`.
5. **Секреты — никогда в промпт, логи, гит И ЛЮБОЙ выход наружу.** Токены/ключи только из env/секрет-хранилища, в конфиге — `SecretStr`; refresh-токены в БД шифруются at-rest (`core/secrets.py`). Наружу текст идёт только редактированным: логи — `core.logging.redact_text`, Telegram — `bot.ux.err_text`. **Сырой `str(e)` пользователю не слать** (исключение от google-ads/OpenAI может нести токен). Три рубежа редакции — [`docs/SECURITY.md`](docs/SECURITY.md).
6. **Модель не трогает SDK напрямую.** LLM заполняет типизированную (Pydantic) схему → код валидирует диапазоны → показывает diff → ждёт «да» → вызывает `google-ads` SDK → пишет audit-row.
7. **Только TEST MCC при разработке.** Никаких боевых аккаунтов, пока всё не проверено на тесте. dev-профиль по умолчанию указывает на test MCC.
8. **Жёсткий allow-list операций.** Агент может вызывать только заранее перечисленные инструменты; защита от prompt-injection — confirm-гейт + код-level allow-list.
9. **Замок аккаунта — ЧТЕНИЕ и МУТАЦИИ раздельно.**
   - Мутации: единственный чокпойнт `ads.client.ensure_allowed(customer_id)`. Мутировать можно ТОЛЬКО аккаунт, **видимый боту** (набор ⊆ `allowed_ceiling()`), — чужой боевой id по опечатке не пройдёт. КОД-минимум `ALLOWED_CEILING = {Draft 7753643025}` через env не понижается.
   - Прод-дефолт — сентинел `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` (мутации на всех видимых, решение владельца 2026-07); явный список id **сужает** набор; в dev/test пусто ⇒ мутаций нет вовсе (**fail-closed**).
   - Чтение: ОТДЕЛЬНЫЙ, более широкий чокпойнт `ensure_read_allowed(customer_id)` + пер-пользовательский грант (`core.access`, `/grant`). **Грант чтения НЕ открывает мутации.**
   - Исполнение привязано к черновику: `execute_confirmed` берёт аккаунт из `proposal.customer_id` и ЗАНОВО проходит `ensure_allowed`.
   - **Точный контракт потолка, режимы `ACCOUNT_ACCESS_MODE`, имена инвариант-тестов — [`docs/SECURITY.md`](docs/SECURITY.md). Правишь замок — читай его сначала.**
10. **Fail-closed везде (никогда fail-open).** Любой гейт при отсутствии конфигурации ОТКАЗЫВАЕТ, не открывает: whitelist (env `TELEGRAM_WHITELIST_CHAT_IDS` ∪ БД-таблица `whitelist`) при ПУСТОМ объединении блокирует ВСЕХ (`if wl and ...` — это fail-open, так нельзя); сбой БД ⇒ пустой БД-набор, а не проход; `ensure_allowed`/`ensure_manager_allowed` при пустом allow-list — отказ; `user_initiated` по умолчанию `False`. В `prod` пустой whitelist или отсутствие ключа шифрования роняют старт (fail-fast, `core.config`). Dev-скрипты с прямой записью (минуя confirm-гейт) — только `ENV=dev`.

## Архитектура (поток)
```
Telegram (aiogram) → handler → агент (model via OpenRouter, tool use)
   ├─ READ-инструменты  → выполняются сразу (GAQL через google-ads SDK)
   └─ MUTATION-инструменты → создают proposal (diff «было→станет») в БД, НЕ выполняют
→ бот показывает сводку + inline ✅ Подтвердить / ❌ Отмена
→ на «да»: выполнить proposal через SDK + записать audit-row (по confirmation_id)
```

## Модель (СМЕНЯЕМАЯ — не зашивать одну)
Все вызовы модели — через **OpenRouter** за тонким адаптером `agent/router.py`. Смена = `LLM_PARSING` /
`LLM_COPY` / `LLM_FALLBACK` в `.env` (+ рантайм `/model`); разные модели на парсинг (дешёвая) и копирайт/ключи
(посильнее). Выбор — **по данным A/B** (`scripts/ab_test_models.py`), не по бренду.

## Стек
Python 3.12 · aiogram 3.x (один event loop с APScheduler) · `openai` SDK (→ OpenRouter) · SQLAlchemy + Alembic +
PostgreSQL · openpyxl / google-api-python-client (Sheets, scope `drive.file`) · Docker · `google-ads` SDK —
**пин `>=31.1,<32` → API `v24`** (lib-версия ≠ API-версии!; сансет v24 ~май 2027; перепроверять ежемесячно —
скил `gads-version`, ссылки [`docs/gads-api-refs.md`](docs/gads-api-refs.md)).

## Структура
- `core/` — config, secrets (шифрование), logging (редакция), access (whitelist/гранты), limits.
- `bot/` — `main.py` (dp, middleware, shared-хелперы, кэши, confirm-оркестрация) + `handlers/` (11 доменных
  модулей) + keyboards, i18n RU/EN, throttle. Хендлеры читают имена main через `bm.<name>` (позднее связывание —
  monkeypatch тестов работает); порядок в `HANDLER_MODULES` = порядок диспатча aiogram, catch-all `on_text`
  **строго последний** (инвариант `tests/test_handler_order.py`).
- `agent/` — router, system_prompt, tools (Pydantic-схемы read+mutation), loop.
- `ads/` — auth, **client (замок аккаунта)**, read (GAQL через paged `Search`; SearchStream осознанно отложен —
  docstring `ads/read.py`), mutations (требуют `confirmation_id`), resolve, keyword_plan, assets,
  service (allow-list операций).
- `confirm/` — proposal (diff), gate, store (атомарный claim), audit. `db/` — модели + Alembic.
- `reports/`, `adcopy/` (генерация+валидация текстов; не `copy` — не затенять stdlib), `scheduler/`, `advisor/`.
- `clients/` — §20 профиль клиента + краулер сайта; memory-операции идут через тот же confirm-гейт, но
  исполняются `clients/execute.py`, **НЕ** через `ads.mutations`. Профиль подаётся контекстом в §19/§10.
- Детали по доменам — по файлу на тему в [`docs/`](docs/) (`SECURITY`, `DATABASE`, `MUTATIONS`, `DEPLOYMENT`,
  `ACCEPTANCE`, `TESTING`, …).

## Статус
Фазы 0–3 ✅ (read-MVP → confirm-гейт+запись → отчёты/Sheets/тексты → keyword research/scheduler). Дополнения ✅:
§8 read-MCC (`/mcc`), §11 GDN/Video/Demand Gen, §19 визард `/newcampaign`, §20 «Клиенты», RU/EN. **Сейчас —
предсдаточный аудит/доведение.** Открыто: §8 полный (мутации на дочерних), распределённая квота.
**App/UAC — ИСКЛЮЧЕНО** (нет приложения у клиента).
Статус сверять по `git log` и [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md), **не** по план-файлам — они отстают от кода.

## Работа в Claude Code
**Экономия контекста** (главная статья расхода — перечитывание всей истории на каждом ходу, не ответы):
- `/clear` между несвязанными задачами. Не тащить мусор прошлой задачи в следующую — это и дешевле, и точнее.
- Разведка по коду («где реализовано X», «что трогает Y») — субагентом `Explore`. Его контекст изолирован:
  в главный поток вернётся вывод, а не дампы двадцати файлов, которые потом перечитываются каждый ход.
- `Grep` / `Read` с offset+limit вместо чтения файла целиком; целиком — только если правишь целиком.
- Тесты точечно: `pytest tests/test_x.py -q`. Полный прогон — перед коммитом.
- Замер расхода: `python scripts/claude_usage.py` (read-only разбор транскриптов; медиана контекста, доли статей).

**Прочее:**
- Архитектура/сложное — Opus 4.8; объём (CRUD, отчёты, тесты, бойлерплейт) — Sonnet 4.6.
- Скилы: `new-mutation`, `gaql-query`, `check-rsa-copy`, `gads-version`, `confirm-gate-audit`.
- Slash-команды (`.claude/commands/`): `/add-bot-command` (4-файловый UI lock-step + гард on_text-last),
  `/verify-live` (единый вход в live-смоук), `/restart-bot` (409/lock/double-import).
- **Windows-консоль:** запускай скрипты с `PYTHONIOENCODING=utf-8` (или зови `scripts/_win_console.enable_utf8()`
  в начале скрипта) — иначе cp1251 роняет emoji/кириллицу `UnicodeEncodeError`.
- **pre-commit гоча:** `ruff-format` переписывает файлы прямо в хуке → 1-й `git commit` падает («files were
  modified by this hook») → повтори `git add -A && git commit` (со ВТОРОГО раза проходит). Пин `ruff==0.14.10`
  синхронен в 3 местах (pyproject `[dev]` / `.pre-commit-config.yaml` / CI) — не бампай одно без остальных.

## Что НЕ делать (якорь — сами правила выше)
- Не ослаблять денежный путь: `confirmation_id` (пр. 2), редакция секретов (пр. 5), замок аккаунта (пр. 9),
  fail-closed (пр. 10). Тронул `ads/mutations.py`, `ads/client.py`, `confirm/**`, `core/secrets.py` →
  прогони `pytest tests/test_safety_core.py tests/test_write_layer.py tests/test_invariants_core.py -q`
  и скил `confirm-gate-audit`. **Это и есть настоящий гард — не текст в этом файле.**
- Не доверять готовым write-MCP как бэкенду (экспериментальны, без подтверждений) — write-слой пишем сами.

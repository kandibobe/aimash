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
5. **Секреты — никогда в промпт, логи, гит И ЛЮБОЙ выход наружу.** developer token / refresh token / API keys только из env/секрет-хранилища. **Хранение токенов (тест-фаза):** единственный refresh-токен живёт в `.env` как `SecretStr` (маскируется в логах/repr/трейсбеках), под защитой прав файла/секрет-хранилища деплоя. Шифрование at-rest в БД (`core.secrets.encrypt`/`decrypt` + таблица `oauth_tokens`) — per-account OAuth-токены **подключены к рантайму** (`ads.client.load_oauth_cache` на старте, см. правило 9 и `docs/DATABASE.md`); используется §8-чтением дочерних. Любая мутация по-прежнему только через confirm-гейт («да» + `confirmation_id`). Редакция — не только в логах: **сырой текст исключения (`str(e)`) НЕ слать пользователю в Telegram** — через `bot.ux.err_text` (редактирует секрето-подобное); в `audit_log` ошибка пишется уже через `redact_text` (`confirm.store.record_failure`); строки с паролем (DSN) — `SecretStr` (см. `core.logging.redact_text`, `core.config.database_url`). Глобальный `dp.errors`-хендлер пишет ошибку в лог (редактированно), а не в stderr/чат.
6. **Модель не трогает SDK напрямую.** LLM заполняет типизированную (Pydantic) схему → код валидирует диапазоны → показывает diff → ждёт «да» → вызывает `google-ads` SDK → пишет audit-row.
7. **Только TEST MCC при разработке.** Никаких боевых аккаунтов, пока всё не проверено на тесте. dev-профиль по умолчанию указывает на test MCC.
8. **Жёсткий allow-list операций.** Агент может вызывать только заранее перечисленные инструменты; защита от prompt-injection — confirm-гейт + код-level allow-list.
9. **Замок аккаунта (раздельно ЧТЕНИЕ и МУТАЦИИ).** Чокпойнт МУТАЦИЙ — `ads.client.ensure_allowed(customer_id)`. **Решение владельца 2026-07: Draft-only доктрина СНЯТА** — мутационный набор задаётся так: сентинел `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all`/`*` (`settings.allow_all_visible`, **prod-дефолт** через `core.config._default_mutations_all_in_prod`) ⇒ набор = ВЕСЬ эффективный потолок; явный список id ⇒ способ СУЗИТЬ набор; в dev/test пусто ⇒ мутаций нет вовсе (**fail-closed**). **Точный контракт потолка:** КОД-минимум `ALLOWED_CEILING = {Draft 7753643025}` нельзя убрать/понизить через env; ЭФФЕКТИВНЫЙ потолок `allowed_ceiling()` = этот минимум ∪ аккаунты, ВИДИМЫЕ боту (env read-list + дочерние, обнаруженные обходом MCC). Мутировать можно ТОЛЬКО видимый аккаунт: опечатка в чужой боевой id отсекается (его нет среди видимых); при `all` сбой discovery ⇒ мутабелен лишь пол потолка {Draft} (безопасная деградация, не эскалация). Инварианты: `test_mutation_lock_unchanged_by_read_allowlist`, `test_mutation_enabled_for_visible_account_in_allowlist`, `test_all_sentinel_allows_every_visible_account`, `test_all_sentinel_still_blocks_invisible_account`. ЧТЕНИЕ per-account — ОТДЕЛЬНЫЙ чокпойнт `ensure_read_allowed(customer_id)`: множество = мутационный список ∪ `GOOGLE_ADS_READ_CUSTOMER_IDS` (env) ∪ **дочерние, обнаруженные обходом MCC на старте** (`ads.client.discover_read_children` под `ensure_manager_allowed`; §8-полный-мульти-аккаунт). Все три пусты = **fail-closed**. **Исполнение привязано к черновику (аудит 2026-07):** `execute_confirmed` берёт аккаунт из `proposal.customer_id` и ЗАНОВО проходит `ensure_allowed` на исполнении (инварианты `tests/test_execute_account_binding.py`). Поверх чтения — **пер-пользовательский грант** (`core.access`/`account_access`, режимы `ACCOUNT_ACCESS_MODE=auto|enforced|legacy`; `/grant`/`/revoke` — админы); грант чтения НЕ открывает мутации (`test_grant_does_not_open_mutations`). **Текущее состояние §8 — РЕАЛИЗОВАНО:** авто-обход дочерних на старте + `/mcc` по ВСЕМ настроенным MCC + подытоги по валютам (без FX) + нормализация таймзон per-child + per-account OAuth-токены (`load_oauth_cache`). Несменяемые страховки поверх набора: confirm-гейт («да» + `confirmation_id`) и потолок видимости; бюджет из scheduler заблокирован всегда (`user_initiated`).
10. **Fail-closed везде (никогда fail-open).** Любой гейт при отсутствии конфигурации ОТКАЗЫВАЕТ, не открывает: whitelist (`bot.main.WhitelistMiddleware`) пускает **env `TELEGRAM_WHITELIST_CHAT_IDS` ∪ БД-таблицу `whitelist`** (рантайм-добавление админом `/adduser`, кэш TTL в `core.access.is_whitelisted`), при ПУСТОМ объединении блокирует ВСЕХ (НЕ `if wl and ...` — это fail-open); сбой БД при чтении whitelist ⇒ пустой БД-набор (fail-closed: env-бутстрап проходит, неизвестные — отказ); `ensure_allowed`/`ensure_manager_allowed` при пустом allow-list/login — отказ; `user_initiated` по умолчанию `False`. В `prod` пустой whitelist или ключ шифрования роняет старт (fail-fast, `core.config`). Dev-скрипты с прямой записью (минуя confirm-гейт) — только `ENV=dev`.

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
- `bot/` — ядро бота: `main.py` (dp/middleware/shared-хелперы/кэши/confirm-оркестрация) + `handlers/` (доменные модули хендлеров: commands, reports, keywords_flow, campaigns_menu, rsa_flow, search_media, campaign_wizard §19, clients_kb §20, templates_recent, confirm_flow, fallback). Хендлеры читают имена main через `bm.<name>` (позднее связывание — monkeypatch тестов работает); порядок импорта модулей в хвосте main.py = порядок диспатча aiogram, catch-all `on_text` строго последний (инвариант — `tests/test_handler_order.py`). Плюс keyboards (inline), i18n RU/EN, middleware/whitelist.
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
- Slash-команды (`.claude/commands/`): `/add-bot-command` (4-файловый UI lock-step + гард on_text-last), `/verify-live` (единый вход в live-смоук), `/restart-bot` (409/lock/double-import).
- **Windows-консоль:** запускай скрипты с `PYTHONIOENCODING=utf-8` (или зови `scripts/_win_console.enable_utf8()` в начале скрипта) — иначе cp1251 роняет emoji/кириллицу `UnicodeEncodeError`.
- **pre-commit гочи:** `ruff-format` переписывает файлы прямо в хуке → 1-й `git commit` падает («files were modified by this hook») → повтори `git add -A && git commit` (со ВТОРОГО раза проходит). Пин `ruff==0.14.10` синхронен в 3 местах (pyproject `[dev]` / `.pre-commit-config.yaml` / CI) — не бампай одно без остальных.

## Что НЕ делать
- Не вызывать `ads/mutations.py` без `confirmation_id`.
- Не вставлять секреты в код/логи/промпт.
- Не работать против боевых аккаунтов БЕЗ подтверждения пользователя. **Решение владельца 2026-07:** в prod дефолт `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` — мутации (за confirm-гейтом) разрешены на всех аккаунтах, ВИДИМЫХ боту (⊆ `allowed_ceiling()`); явный список id СУЖАЕТ набор; в dev/test пусто ⇒ мутаций нет вовсе (fail-closed) — локальная разработка по-прежнему не трогает боевые аккаунты (точный контракт — золотое правило 9 и шапка `ads/client.py`).
- Не ослаблять замок аккаунта: env НЕ может (а) понизить КОД-минимум `ALLOWED_CEILING = {Draft}`, (б) открыть мутации на НЕвидимом/чужом аккаунте (потолок = видимые), (в) сделать `ensure_allowed` fail-open. Расширение мутационного набора в этих рамках — легитимно и управляется конфигом (см. золотое правило 9).
- Не доверять готовым write-MCP как бэкенду (экспериментальны, без подтверждений) — write-слой пишем сами.

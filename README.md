# Aimash — ИИ-агент управления Google Ads через Telegram

Telegram-бот, который по командам на естественном языке управляет рекламными кампаниями Google Ads (уровень MCC). **Исполнитель, не автономный оптимизатор:** перед любым изменением показывает «было → станет» и ждёт подтверждения «да». Полный бриф и план — в `CLAUDE.md` и план-файле.

## Принцип безопасности
Мутация и подтверждение **разделены**: агент создаёт черновик изменения (proposal), а выполняет его код — только после явного «да» пользователя, с записью в audit-журнал. Бюджет меняется только по прямой команде. См. золотые правила в `CLAUDE.md`.

## Статус
Фазы 0–3 в основном реализованы (точный статус — по коммитам; роадмап в плане может отставать от кода).
- **Готово:** чтение MCC (GAQL) + whitelist + Postgres/Alembic; confirm-гейт + запись (бюджет, ставка CPC и стратегия ставок, ключи, минус-слова, пауза/возобновление, ГЕО-радиус и ГЕО-локация, аудитории) с audit; генерация RSA-текстов с поэлементным подтверждением; **создание кампаний — Search (§19 визард), GDN, Video и Demand Gen из фото/видео, всё на паузе** (§11); глубокие отчёты (`.xlsx` и Google Sheets) + сравнение период-к-периоду; **сводный отчёт по дочерним MCC `/mcc`** (§8, подытоги по валютам); keyword research + AI-кластеризация; **§20 «Информация про клиентов»** (`/clients`: профиль + краулинг сайта → контекст генерации); двуязычный интерфейс **RU/EN** (`/lang`); планировщик отчётов/аномалий (read-only).
- **Инфраструктура/безопасность:** Docker + bot-сервис; CI (ruff/mypy/coverage); ретраи+таймауты к Google Ads/OpenRouter; редакция секретов в логах + логирование запросов; анти-спам throttling; дневная квота API (`/quota`); prod fail-fast на ключ шифрования.
- **Открытый объём (вне текущей сдачи):** управляемое включение **мутаций** на видимых дочерних MCC (сейчас читаются, мутируется по умолчанию только Draft, §8; механизм есть — `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`, нужен живой прогон под confirm-гейтом); распределённая квота на мульти-реплике; UAC исключён (нет приложения у клиента).

## Команды бота
| Команда | Что делает |
|---|---|
| `/start`, `/help` | приветствие, меню, справка |
| `/status` | статистика аккаунта (30 дн.) |
| `/campaigns` | список кампаний + быстрые действия |
| `/pause Название`, `/resume Название` | пауза/возобновление кампании (через confirm-гейт) |
| `/report [7\|30\|90\|MTD]` | сводка за период (итоги + сравнение + топ-кампании) |
| `/mcc [дни]` | §8: сводный отчёт по всем дочерним аккаунтам MCC (подытоги по валютам) |
| `/account <id>` \| `/account reset` | переключить аккаунт ЧТЕНИЯ для `/status` `/report` `/export` `/sheets` |
| `/export [...]` | глубокий отчёт `.xlsx` вложением |
| `/sheets [...]` | глубокий отчёт в Google Sheets (ссылка; нужен OAuth-scope `drive.file`, см. `docs/DEPLOYMENT.md`) |
| `/rsa` | генерация RSA-текстов с поэлементным подтверждением |
| `/newcampaign` | §19: пошаговый визард создания Search-кампании (черновик PAUSED) |
| `/newsearch`, `/newvideo` | §11: быстрая кампания Search (RSA+ключи) · из видео (Demand Gen / Video), черновик PAUSED |
| `/clients`, `/client <id>` | §20: информация про клиентов — профиль на аккаунт (текст + краулинг сайта), контекст генерации |
| `/keywords` | подбор ключевых слов (объём/конкуренция/кластеры) + `.xlsx` |
| `/templates`, `/savetemplate`, `/recent` | шаблоны кампаний · сохранить шаблон · повторить недавнее действие |
| `/account <id>\|reset`, `/accounts`, `/whoami` | активный аккаунт ЧТЕНИЯ · мои доступные аккаунты · мои chat_id/аккаунт/режим |
| `/alerts`, `/quota` | пороги алертов аномалий · дневная квота Google Ads API (на 95% мутации блокируются) |
| `/journal`, `/diag` | аудит-журнал действий · последние ошибки (§15) |
| `/lang [ru\|en]`, `/model`, `/balance` | язык интерфейса · выбор модели ИИ · баланс OpenRouter |
| `/cancel` | отменить текущий черновик / свернуть активный визард |
| **админ** (`ADMIN_CHAT_IDS`): `/adduser`, `/removeuser`, `/users` | рантайм-whitelist: добавить/убрать оператора · список (env ∪ БД) |
| **админ**: `/grant`, `/revoke`, `/refresh` | пер-юзер доступ к аккаунту (чтение) · пере-обход дочерних MCC + сброс кэшей |
| свободный текст | NL-команда → агент (бюджет/ставка/ключи/…) |

Любое изменение — только после «да» (показ «было → станет»); анти-спам throttling ограничивает частоту команд. Деплой/OAuth/Sheets-scope — `docs/DEPLOYMENT.md`.

---

## Что нужно получить (ключи) — пока ничего нет
| Ключ | Где взять | Зачем | Стоимость |
|---|---|---|---|
| **Test MCC + тест-аккаунты** | ads.google.com → создать менеджерский тест-аккаунт | Безопасная разработка без денег | бесплатно |
| **developer token (Basic)** | у Антона (уже есть) | Доступ к Google Ads API (работает и на тесте) | — |
| **OpenRouter API key** | openrouter.ai | A/B-тест моделей + рантайм (один ключ ко всем моделям) | pay-as-you-go, копейки |
| **Telegram bot token** | @BotFather | Сам бот | бесплатно |

> ⚠️ Логин Claude Code (Max 20x) покрывает **разработку**, но **не рантайм** бота — для прода нужен API-ключ (OpenRouter/Anthropic), это счёт клиента (~$10–50/мес).

---

## Установка
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |  *nix: source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install            # хуки: gitleaks (секрет-скан) + ruff/ruff-format
cp .env.example .env          # затем заполнить .env (см. замок аккаунта ниже)
```

Частые команды — `Makefile` (Git Bash/WSL: `make help`). PowerShell-эквиваленты:

| Действие | make | PowerShell |
|---|---|---|
| Тесты | `make test` | `pytest -q` |
| Линт/формат | `make lint` / `make fmt` | `ruff check .` / `ruff format .` |
| Хуки по всем файлам | `make hooks` | `pre-commit run --all-files` |
| Dev-Postgres вверх/вниз | `make db-up` / `make db-down` | `docker compose up -d postgres` / `docker compose down` |
| Миграции | — (нет make-цели) | `alembic upgrade head` |
| Бот | `make run` | `python -m bot.main` |
| Проверка доступа (read-only) | `make check-access` | `python scripts/check_access.py` |
| MCP-статус | `make mcp-list` | `claude mcp list` |

## 🔒 Замок аккаунта (мутации ≠ чтение)
**МУТАЦИИ** по умолчанию разрешены **ТОЛЬКО** на `Aimash (Draft)` = **`7753643025`** (775-364-3025). В `.env`: `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=7753643025`. Замок — в коде (`ads.client.ensure_allowed`): пустой allow-list = отказ (fail-closed). Код-минимум `ALLOWED_CEILING = {Draft}` `.env` **не понизит**; включить мутации на ещё одном аккаунте можно управляемым конфигом (`GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`), но только среди **видимых** боту аккаунтов — эффективный потолок `allowed_ceiling()` отсекает опечатку в чужой боевой id (см. `docs/DEPLOYMENT.md §2.1`). **ЧТЕНИЕ** — отдельный, более широкий замок `ensure_read_allowed` (§8: дочерние MCC читаются, но не мутируются). См. golden rule №9 в `CLAUDE.md`.

## Версия Google Ads API/SDK
API — **v24** (текущая v24.2); SDK-пин — **`google-ads>=31.1,<32`** (lib-версия ≠ API-версии!). Релизы ежемесячные, v24 сансет ~май 2027 → бампить SDK ~раз в месяц. Скил `gads-version`, ссылки `docs/gads-api-refs.md`.

## MCP-серверы (для разработки)
Конфиг — `.mcp.json` (4 сервера: Postgres read-only, Google Ads read-only, Context7, GitHub). Секреты/пути — НЕ в `.mcp.json`, а в `.claude/settings.local.json` (gitignored):
```bash
cp .claude/settings.local.example.json .claude/settings.local.json   # затем заполнить env
docker compose up -d postgres        # поднять dev-БД для Postgres MCP
claude mcp list                       # проверить, что все 4 — connected
```
- **Postgres** — `crystaldba/postgres-mcp` (`--access-mode=restricted`, роль `aimash_ro`).
- **Google Ads** — `cohnen/mcp-google-ads` (read-only, клон в `c:\tools\mcp-google-ads`; путь → `MCP_GOOGLE_ADS_PATH`). Только TEST-аккаунт `7753643025`. **Build-time помощник, не бэкенд продукта.**
- **Context7** — свежие доки библиотек. **GitHub** — issues/PR (PAT в `GITHUB_PAT`).
- Windows-нюанс: если stdio-сервер не стартует через `npx/uvx/python` — обернуть командой `cmd /c …` в `.mcp.json`.

## A/B-тест моделей (Фаза −1, первый шаг)
Сравнить DeepSeek / Hermes / Claude на реальных русских командах и текстах, выбрать самую дешёвую, что проходит.
```bash
# нужен только OPENROUTER_API_KEY в .env
python scripts/ab_test_models.py
```
Скрипт прогоняет сценарии (парсинг команд → правильная функция+аргументы; «на 20%» vs «до 20%»; типы соответствия; неоднозначное → должен УТОЧНИТЬ; генерация RSA-текстов с лимитами) и печатает таблицу результатов по моделям.

## Структура
```
core/      config, secrets (шифрование), logging (редакция секретов), resilience (ретраи/таймауты), quota
bot/       aiogram handlers, inline-кнопки, whitelist, ux, i18n (RU/EN), throttle, campaign_wizard (§19 store)
agent/     router (OpenRouter), system_prompt, tools (Pydantic), loop, campaign_settings/campaign_edit (§19)
ads/       auth, read (GAQL), mutations (требуют confirmation_id), keyword_plan, assets, extensions (§11/§19)
adcopy/    генерация RSA-текстов + валидация длины (кириллица=1) + курация + assets_gen (§19.7)
reports/   глубокие отчёты: queries (GAQL), service, xlsx, sheets, period, mcc (§8)
keywords/  подбор ключей + AI-кластеризация по интенту + .xlsx + ingest (парс списков)
clients/   §20: профиль клиента (store), LLM-разбор (profile_extract), краулер сайта (crawler), execute (memory-гейт)
scheduler/ плановые отчёты/аномалии/очистка черновиков и зависших краулов (READ-ONLY, golden rule #3)
confirm/   proposal (diff), gate (логика «да»), audit
db/        SQLAlchemy модели + Alembic (migrations/)
scripts/   ab_test_models.py — A/B-тест моделей; check_access, get_refresh_token, backup_db
```

## Правила разработки
- Только **TEST MCC** при разработке (`ENV=dev`); изменения — **только** на аккаунте `7753643025` (замок выше).
- Любая мутация — через `confirm`-гейт с `confirmation_id` И замок аккаунта (`ensure_allowed`); без любого — код отклоняет.
- Длину текста считает код (кириллица = 1 символ). Секреты — не в код/логи/гит (обёрнуты в `SecretStr`; gitleaks в pre-commit).

# Aimash — ИИ-агент управления Google Ads через Telegram

Telegram-агент, который по свободному тексту управляет рекламными кампаниями Google Ads (уровень MCC). **Исполнитель, не автономный оптимизатор:** перед любым изменением показывает «было → станет» и ждёт подтверждения. Ядро агентского цикла — фреймворк **Hermes** (пин `v0.19.0`); правила разработки зеркалируются в [`CLAUDE.md`](CLAUDE.md) и [`AGENTS.md`](AGENTS.md).

**Источник истины — три слоя:** [`SPEC.md`](SPEC.md) — требования и приёмка · [`deploy/hermes/HERMES_SPEC.md`](deploy/hermes/HERMES_SPEC.md) — архитектура · [`deploy/hermes/AGENTIC_VS_TZ.md`](deploy/hermes/AGENTIC_VS_TZ.md) — обоснование. [`ТЗ.md`](ТЗ.md) — дословный текст трёх `.docx` заказчика.

**START HERE — точка входа в пивот:** [`docs/TZ-Aimash-Hermes-Agent.md`](docs/TZ-Aimash-Hermes-Agent.md) — сводное ТЗ пивота (+ `docs/REUSE-MAP.md`, `docs/AUDIT-open-source.md`).

## Принцип безопасности
Мутация и подтверждение **разделены**: агент создаёт черновик изменения (proposal), а выполняет его код — только после явного согласия человека, с записью в audit-журнал. Бюджет меняется только по прямой команде. См. 15 золотых правил в `CLAUDE.md`.

## Статус
**Идёт пивот на Hermes: функциональный объём сохраняется, меняется архитектура** — кнопочный интерфейс уступает свободному тексту с подтверждением реплаем (`SPEC.md` §2).

Чтобы не смешивать проект и текущий прод:

| Состояние | Что является ядром | Что доступно |
|---|---|---|
| **Целевое** | Hermes ведёт диалог и агентский цикл; Aimash MCP исполняет типизированные инструменты; scheduler работает отдельным процессом | свободный текст, READ/MEMORY/PLAN/WRITE, reply-confirm |
| **Переходное сейчас** | Hermes уже выбран и подключён к MCP, но контейнер `aimash-bot` остаётся runtime-мостом | 24 READ на живой MCP-поверхности; PLAN/WRITE через Hermes закрыты |
| **Legacy aiogram** | `python -m bot.main` | кнопки, визарды и действующий путь мутаций; это источник переиспользуемой бизнес-логики, а не целевая архитектура |

⚠️ **Две независимые нумерации «волн», их легко перепутать** — это и есть источник расхождений в старых заметках:

| Нумерация | Что это | Где |
|---|---|---|
| **Волны 0–3** | план пивота: замеры → механизм → новые требования → функциональный объём | `SPEC.md` §12 |
| **Волны 2–5** в шапках коммитов 27.07 | отдельная серия хардненинга денежного пути, **своя** нумерация и свои §-номера | `git log 18339a1..f6725e9` |

**Волна 0 (§12) — почти пуста.** Снят один замер: **V1** — `hermes version` на VPS отдаёт `Hermes Agent v0.19.0 (2026.7.20)` (29.07.2026), пин подтверждён живьём. Остальные строки таблицы в [`deploy/hermes/OPERATIONS.md`](deploy/hermes/OPERATIONS.md) пусты: 0.1/0.4 — прототипы написаны (`mcp_server/probe.py`, `deploy/hermes/plugins/aimash_probe/`), но исполняются руками на VPS. Пока не сняты 0.7 и 0.6 — **смета остаётся `[Guessing]`**, так помечена и в спеке.

**Волна 1 (§12) — механизм, сделано 3 шага из 10:**

| Шаг | Состояние |
|---|---|
| 1 · Hermes на VPS + bot-free bootstrap | ✅ пин v0.19.0, тулсет-поверхность заперта, `bot/`-зависимости срезаны, у планировщика свой процесс + advisory-lock. Gateway в Telegram ещё не поднят |
| 2 · Гарды И1–И5 | ⛔ не сделано — в `tests/test_hermes_isolation.py` 8 `@pytest.mark.skip`; живьём покрыт только И3 (`tests/test_provenance_gate.py`) |
| 3 · READ-инструменты | ✅ 24 READ-инструмента из 38 по реестру §6.1. Round-trip через Hermes доказан живьём на 15 из них; 8 добавленных 30.07 покрыты только офлайн-смоуком (`tests/test_mcp_read_smoke.py`, SDK подменён) |
| 4 · Память | 🟡 `recall_client` есть; `remember_fact`/`agent_facts` — нет |
| 5 · Мутации + реплай-гейт | 🟡 ready-dark: два `propose_*`, привязка карточки и CAS-подтверждение реплаем реализованы, но не зарегистрированы на живой MCP-поверхности и не получают доверенные Telegram-метаданные; мутации пока идут кнопочным путём |
| 6–10 · скилы, самообучение, наблюдаемость | ⛔ не начаты |

**Серия хардненинга 27.07** (вне нумерации §12) — легли четыре контура денежного пути: распределённый размыкатель с арендой пробы (`core/resilience.py`), event-sourcing журнала прогонов, наблюдение за применённой мутацией (режим `shadow`), тиры риска L1/L2/L3 — **презентационные по построению**, AST-гард запрещает `ads/**` импортировать `confirm.risk`.

Точный статус пивота — по таблице выше, `git log` и критериям `SPEC.md` §13. [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) фиксирует приёмку legacy aiogram и не доказывает готовность Hermes-контура.

Фазы 0–3 legacy aiogram реализованы:
- **Готово:** чтение MCC (GAQL) + whitelist + Postgres/Alembic; confirm-гейт + запись (бюджет, ставка CPC и стратегия ставок, ключи, минус-слова, пауза/возобновление, ГЕО-радиус и ГЕО-локация, аудитории) с audit; генерация RSA-текстов с поэлементным подтверждением; **создание кампаний — Search (§19 визард), GDN, Video и Demand Gen из фото/видео, всё на паузе** (§11); глубокие отчёты (`.xlsx` и Google Sheets) + сравнение период-к-периоду; **сводный отчёт по дочерним MCC `/mcc`** (§8, подытоги по валютам); keyword research + AI-кластеризация; **§20 «Информация про клиентов»** (`/clients`: профиль + краулинг сайта → контекст генерации); двуязычный интерфейс **RU/EN** (`/lang`); планировщик отчётов/аномалий (read-only).
- **Инфраструктура/безопасность:** Docker + bot-сервис; CI (ruff/mypy/coverage); ретраи+таймауты к Google Ads/OpenRouter; редакция секретов в логах + логирование запросов; анти-спам throttling; дневная квота API (`/quota`); prod fail-fast на ключ шифрования.
- **Открытый объём (вне текущей сдачи):** управляемое включение **мутаций** на видимых дочерних MCC (сейчас читаются, мутируется по умолчанию только Draft, §8; механизм есть — `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`, нужен живой прогон под confirm-гейтом); распределённая квота на мульти-реплике; UAC исключён (нет приложения у клиента).

## Команды бота — текущий (архивируемый) кнопочный слой

> Список ниже описывает **работающий сегодня** интерфейс. Сами команды — это **функции**, и они остаются: по `SPEC.md` §2.6 они переезжают в текстовую модель (часть — через `ctx.register_command()` Hermes, часть — как обычные фразы агенту). Архивируется слой ввода, а не возможности.

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

## Ключи и доступы
Все получены и работают: Test MCC + Draft-аккаунт, **developer token уровня Basic** (Keyword Planner доступен), OpenRouter API key, Telegram bot token. Заполняются в `.env` по `.env.example`; на сервере — `~/.hermes/.env` для gateway (там **только** `OPENROUTER_API_KEY`, см. топологию в `CLAUDE.md`).

> ⚠️ Логин Claude Code покрывает **разработку**, но **не рантайм** агента — прод считает OpenRouter, это счёт клиента.

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
API — **v25** (релиз 2026-07-22); SDK-пин — **`google-ads>=31.2,<32`**, хард-пин `31.2.0` в `constraints.txt` (lib-версия ≠ API-версии!). Сансет v25 — август 2027; версию и минорные релизы перепроверять ежемесячно. Скил `gads-version`, ссылки `docs/gads-api-refs.md`. Все точки пина сверяет `tests/test_gads_version_pin.py` — бамп в одном месте больше не проходит молча.

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

## A/B-тест моделей
Сравнить кандидатов на реальных русских командах и текстах, выбрать самую дешёвую, что проходит. Раскладка по ролям — `SPEC.md` §10.1; выбор **по данным**, не по бренду. (Здесь «модель» — LLM за OpenRouter; не путать с фреймворком Hermes, который есть ядро агента и моделью не является.)
```bash
# нужен только OPENROUTER_API_KEY в .env
python scripts/ab_test_models.py
```
Скрипт прогоняет сценарии (парсинг команд → правильная функция+аргументы; «на 20%» vs «до 20%»; типы соответствия; неоднозначное → должен УТОЧНИТЬ; генерация RSA-текстов с лимитами) и печатает таблицу результатов по моделям.

## Структура

Пометка справа — судьба каталога при пивоте (`SPEC.md` §5.2).
```
core/         config, secrets (шифрование), logging (редакция), access, provenance, quota   ← ядро
ads/          auth, client (замок), read (GAQL), mutations (confirmation_id), freshness,
              service (execute_confirmed), keyword_plan, assets, extensions (§11/§19)       ← ядро
confirm/      proposal (diff), gate, store (атомарный claim), render, audit                 ← ядро
adcopy/       генерация RSA + валидация длины (кириллица=1) + курация + assets_gen (§19.7)  ← ядро
reports/      глубокие отчёты: queries (GAQL), service, xlsx, sheets, period, mcc (§8)      ← ядро
keywords/     подбор ключей + AI-кластеризация по интенту + .xlsx + ingest                  ← ядро
clients/      §20: профиль (store), LLM-разбор, краулер сайта, execute (memory-гейт)        ← ядро
scheduler/    плановые отчёты/аномалии/очистка черновиков (READ-ONLY, правило 3)            ← ядро
db/           SQLAlchemy модели + Alembic (migrations/)                                     ← ядро
app/          bootstrap — bot-free старт ads-глобалов                                       ← ядро
mcp_server/   тул-слой Hermes: 24 READ live; 2 propose_* + reply/execute ready-dark; остальной PLAN/WRITE — Волна 1
deploy/hermes/ конфиг Контура A, плагин-проб, конфиг-линт (К10), RUNBOOK хоста    ← пишется заново
bot/          aiogram handlers, inline-кнопки, визарды, i18n, throttle    ← архивируется после гейтированного cutover
agent/        свой цикл архивируется; router.py и tools/schemas.py переезжают в bot-free пакет
scripts/      ab_test_models, check_access, get_refresh_token, backup_db, claude_usage
```
⛔ Волна 1 шаг 1 уже выполнена, но это только необходимое предусловие. `bot/` и legacy-часть `agent/`
физически **не двигать** до гейтированной архивации из `deploy/hermes/OPERATIONS.md`: нужен bot-free
runtime MCP, принятые gateway/READ и WRITE/reply-путь, отсутствие тестовых импортов `bot.main` и тег
`pre-hermes`. Сейчас прод-контейнер `aimash-bot` = `python -m bot.main`, а Hermes ходит в Ads через
`docker exec` внутрь него.

## Правила разработки
- Разработка — только на **Draft/test** аккаунте (`ENV=dev`), замок выше.
- Любая мутация — через `confirm`-гейт с `confirmation_id` И замок аккаунта (`ensure_allowed`); без любого — код отклоняет.
- Длину текста считает код (кириллица = 1 символ). Секреты — не в код/логи/гит (обёрнуты в `SecretStr`; gitleaks в pre-commit).
- Полный список — **15 золотых правил** в `CLAUDE.md`; тронул денежный путь — прогони `pytest tests/test_safety_core.py tests/test_write_layer.py tests/test_invariants_core.py -q`.

# Передача Aimash заказчику (handover)

> ⚠️ **Идёт пивот на ядро Hermes** (топология из трёх процессов, реплай-подтверждение вместо кнопок).
> Модель передачи/владения ключами ниже действует как есть; топология деплоя сверяется с
> [`docs/TZ-Aimash-Hermes-Agent.md`](TZ-Aimash-Hermes-Agent.md)
> и `deploy/hermes/`.

Раннбук передачи бота заказчику: перенос владения ключами, развёртывание на своём сервере,
проверки перед сдачей. Технические детали деплоя — в [DEPLOYMENT.md](DEPLOYMENT.md); схема БД и
миграции — в [DATABASE.md](DATABASE.md); золотые правила — в [../CLAUDE.md](../CLAUDE.md).

> ⚠️ Бот управляет **чужими деньгами** в Google Ads. Перед боевым запуском пройди весь
> [pre-delivery чек-лист](#4-чек-лист-перед-сдачей-pre-delivery-gate) и
> [prod-чеклист DEPLOYMENT.md §6](DEPLOYMENT.md#6-prod-чеклист).

## Модель передачи (зафиксировано)
- **Заказчик владеет всеми ключами** — секреты не передаются, заказчик заводит свои (раздел 1).
- **Деплой — свой VPS + Docker Compose** (раздел 2).
- **Прод-аккаунт — полный MCC** (много дочерних) → требует спринта многоаккаунтности (раздел 5).
- **Интерфейс — RU/EN.**

---

## 1. Перенос владения ключами (заказчик заводит свои)

Ни один секрет разработчика не переезжает в прод. Заказчик создаёт и владеет:

| Ключ (`.env`) | Где завести | Примечание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather (свой бот) | свой бот — нельзя «унаследовать» дев-бота |
| `OPENROUTER_API_KEY` | openrouter.ai (свой биллинг) | рантайм LLM — счёт заказчика (~$10–50/мес) |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Google Ads API Center (свой MCC) | Basic-уровня достаточно |
| `GOOGLE_ADS_CLIENT_ID` / `..._SECRET` | Google Cloud → OAuth-клиент (свой проект) | desktop-app OAuth |
| `GOOGLE_ADS_REFRESH_TOKEN` | `python scripts/get_refresh_token.py` | scope `adwords` **и** `drive.file` (для `/sheets`) |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | ID менеджерского (MCC) аккаунта | контекст авторизации |
| `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` | — | круг разрешённых аккаунтов (см. раздел 5) |
| `SECRETS_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Fernet-ключ; хранить в секрет-сторе |

Передаём заказчику **инструкции** (этот файл + `DEPLOYMENT.md`), а не значения.

---

## 2. Развёртывание на VPS (Docker Compose)

```bash
git clone <repo> /opt/aimash && cd /opt/aimash
cp .env.example .env            # заполнить ключами из раздела 1
chmod 600 .env                  # секреты доступны только владельцу
# ENV=prod включает fail-fast: пустой Fernet-ключ или пустой whitelist уронят старт.
docker compose up -d --build    # postgres + bot; миграции применяются АВТОМАТИЧЕСКИ при старте
docker compose logs -f bot      # убедиться: "alembic upgrade head" прошёл, бот "запущен (polling)"
```

Миграции БД накатываются автоматически (`docker-entrypoint.sh` → `alembic upgrade head` до старта
бота; падение миграции = контейнер не поднимается, fail-fast). Бот — long-poll (HTTP-порта нет).

### Whitelist — env-бутстрап + рантайм-таблица БД
Доступ к боту гейтится **объединением** env `TELEGRAM_WHITELIST_CHAT_IDS` **∪ таблица `whitelist`**
(`WhitelistMiddleware` → `core.access.is_whitelisted`, кэш с TTL; fail-closed на сбое БД). env — это
**бутстрап первого админа**: впиши свой `chat_id` в `.env`, перезапусти бот, дальше добавляй
операторов **на лету** без рестарта — `/adduser <chat_id> [note]` (админ; inline-пикер read-scope),
`/removeuser <chat_id>`, `/users`. В prod пустой env-whitelist роняет старт (fail-closed); пустое
объединение (env ∪ БД) блокирует всех.

> Таблица `whitelist` была удалена как мёртвая в `0016_drop_whitelist`, затем **возвращена
> рантайм-активной** в `0017_whitelist_runtime` (колонки `added_by`/`note`) — теперь это реальный
> БД-allow-list, а не иллюзия. Грант чтения whitelist **не** открывает мутаций (отдельный замок).
> Мультиюзер-доступ к АККАУНТАМ (не к боту) — таблица `account_access` + команды `/grant`, `/revoke`
> (админы — env `ADMIN_CHAT_IDS`; режим изоляции — env `ACCOUNT_ACCESS_MODE=auto|enforced|legacy`,
> дефолт auto: первый грант включает enforcement).
> Таблица `oauth_tokens` **загружается на старте** (`load_oauth_cache` в `bot.main`): если в ней есть
> записи, `build_client(child)` берёт per-account refresh-токен/`login_customer_id` для дочерних под
> другими MCC. Пусто ⇒ Draft/тест-MCC работает на едином `.env`-токене (обратная совместимость).
> Заполнение — `scripts/register_account.py` (шифрование at-rest).

---

## 3. О «миграции данных» (важно)

**Дев/тест-БД заказчику НЕ переносится.** `audit_log`/`proposals` тест-аккаунта — артефакты
разработки. Прод стартует с **чистой** Postgres; переносится только **схема** (через Alembic),
не данные. «Миграция» к заказчику =
1. развернуть код на его VPS;
2. вписать его ключи в `.env`;
3. `docker compose up` (миграции накатятся сами);
4. вписать `chat_id` в `TELEGRAM_WHITELIST_CHAT_IDS`.

### Бэкапы (обязательны до боевого запуска)
`audit_log` необратим при потере. Поставить `scripts/backup_db.sh` в cron (пример внутри скрипта),
выгружать дампы **наружу** (S3/Backblaze/rclone) и **протестировать восстановление** на staging.

---

## 4. Чек-лист перед сдачей (pre-delivery gate)

- [ ] **Живой smoke** на разрешённом аккаунте: `python scripts/check_access.py` (замок + обход MCC)
      и `python scripts/live_smoke_test.py` (обратимый pause↔resume через полный confirm-гейт). `exit 0`.
- [ ] `pytest -q` зелёный; `ruff check . && ruff format --check .` чисто; `mypy` просмотрен.
- [ ] `gitleaks` чист; `git log -p -- .env` пуст (`.env` никогда не коммитился).
- [ ] **Prod fail-fast:** `ENV=prod` с пустым `SECRETS_ENCRYPTION_KEY` → старт падает; пустой
      whitelist → падает.
- [ ] `alembic heads` == **ровно один** head (текущий — см. `migrations/versions/`, сейчас
      `0017_whitelist_runtime`); `upgrade head` чистый; `downgrade -1` → `upgrade` обратимы.
- [ ] **Autogenerate-дрейф = 0** на чистой Postgres (`alembic revision --autogenerate` ничего не
      предлагает — ни DROP, ни ADD).
- [ ] Бэкап настроен и **restore протестирован**.
- [ ] `/lang en` переключает **весь** интерфейс (EN-каталог `core/i18n.py` полон; проверяемо).
- [ ] **§19 визард** (`/newcampaign`) и **§20 «Клиенты»** (`/clients`) проверены по [UAT_PLAN.md](UAT_PLAN.md).
      §20 хранит **PII клиентов** (телефоны/e-mail) в БД — бэкапы БД содержат PII, храните защищённо.
- [ ] Прод-чеклист [DEPLOYMENT.md §6](DEPLOYMENT.md#6-prod-чеклист) пройден полностью.

---

## 5. Перед боевыми деньгами на полном MCC (открытый объём)

**Замки чтения и мутаций РАЗДЕЛЕНЫ** (golden rule #9): мутации — `ensure_allowed`, чтение — отдельный
`ensure_read_allowed`. **Контракт потолка мутаций** (`ads.client.allowed_ceiling`): код-МИНИМУМ
`ALLOWED_CEILING = {"7753643025"}` ([../ads/client.py](../ads/client.py)) `.env` не может понизить, но
ЭФФЕКТИВНЫЙ потолок = этот минимум **∪ видимые боту аккаунты** (env `GOOGLE_ADS_READ_CUSTOMER_IDS` ∪
дочерние обхода MCC). Мутации на не-Draft включаются **управляемым конфигом** `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`
(⊆ потолка) — опечатку в чужой боевой id отсекает потолок видимости (см. [DEPLOYMENT.md §2.1](DEPLOYMENT.md#21-включение-мутаций-на-аккаунте-мультиаккаунт-g--управляемый-список)).
По умолчанию allow-list = {Draft} ⇒ мутируется только Draft. Чтение (`ensure_read_allowed` + env
`GOOGLE_ADS_READ_CUSTOMER_IDS`, fail-closed) — шире: дочерние можно дать **на чтение** под §8, не
открывая мутаций на них.

Сделано (read-only §8 — реализовано):
- ✅ раздельные замки чтения/мутаций (`ensure_read_allowed`), инвариант покрыт тестом;
- ✅ обнаружение дочерних — `ads.read.list_child_accounts` (за `ensure_manager_allowed`);
- ✅ **авто-обход дочерних на старте** (`ads.client.discover_read_children`) → эффективный
  read-allow-list из кода (не только env); мутации не затрагиваются (тест
  `test_discovered_child_readable_but_not_mutable`);
- ✅ **сводный отчёт по дочерним (`/mcc`)** — `reports.mcc.build_mcc_summary_async` +
  `summary_text_mcc`; кнопка меню «🏢 MCC»;
- ✅ **подытоги по валютам** (без FX — golden rule 4, не выдумываем курсы);
- ✅ **нормализация таймзон** per-child (`customer.time_zone` → окно в TZ аккаунта);
- ✅ **плановая рассылка/аномалии** учитывают обнаруженных дочерних (`scheduler._scheduled_accounts`);
- ✅ **per-account OAuth-токены** загружаются на старте (`load_oauth_cache` в `bot.main.main()`) —
  для дочерних под разными MCC (шифрование at-rest `oauth_tokens`).

Осталось для боевого MCC:
1. (эксплуатация) зарегистрировать per-account токены дочерних под другими MCC:
   `scripts/register_account.py` (если у них отдельные refresh-токены/MCC; один тест-MCC покрыт
   единым `.env`-токеном автоматически);
2. (опц.) сводный total по единому курсу FX — **сознательно НЕ делаем** (per-currency честнее);
3. (отдельно, осознанно) если нужны МУТАЦИИ на дочернем **видимом** аккаунте — добавить его id в
   `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` ([DEPLOYMENT.md §2.1](DEPLOYMENT.md#21-включение-мутаций-на-аккаунте-мультиаккаунт-g--управляемый-список))
   + живой прогон под confirm-гейтом (правка кода нужна лишь чтобы понизить код-минимум `ALLOWED_CEILING`).

До этого бот: **читает** дочерние MCC (обход + read-list), **меняет** только `7753643025`.

## 6. Известные технические заметки (зафиксировано на предсдаточном аудите 2026-07)

- **Квота операций per-process** (`core/quota.py`): дневной счётчик Google Ads API живёт в памяти
  процесса. Для ОДНОГО инстанса бота (текущая модель деплоя) — корректно; при горизонтальном
  масштабировании (несколько реплик) нужен общий стор (Redis/БД), иначе каждая реплика считает
  свою квоту отдельно.
- **Конкуренция confirm-гейта тестируется на SQLite**: атомарный `claim` (compare-and-set
  `UPDATE … WHERE status='confirmed'`) в тестах гоняется на temp-SQLite (один писатель); в проде —
  Postgres. Семантика UPDATE-rowcount одинакова, но рекомендуется отдельная CI-lane с Postgres для
  конкурентных сценариев confirm-гейта (double-click/двойной воркер) перед масштабированием.
- **Live SDK-смоук Video/Demand Gen — ВЫПОЛНЕН 2026-07-03** (`scripts/live_smoke_video_dg.py`):
  Demand Gen сверен live ✅ (PAUSED-кампания создана/перечитана/удалена; попутно закрыты
  live-требования ad.name/logo_images/мин-бюджет). Video — ограничение Google API
  (MUTATE_NOT_ALLOWED: создание VIDEO-кампаний только по allowlist) — не дефект кода;
  рабочий путь из видео — Demand Gen. Детали — ACCEPTANCE §18#5.
- **A/B быстрой parse-модели** (`scripts/ab_test_models.py`, метрика TTFT) — последний открытый
  пункт латентности: `llm_parsing` в `core/config.py` помечен как кандидат на замену по данным.
- **2FA для критических операций** (ТЗ §12 «опционально») — **реализовано** (`core/twofa.py`:
  лок-аут, алерт админам), по умолчанию **выключено**: whitelist + confirm-гейт + замок аккаунта
  покрывают модель угроз тест-фазы. При боевом MCC — включить; канал ввода PIN (FSM уходит с `bot/`) —
  см. SPEC §11.2 / R9.

## 7a. Третий предсдаточный проход — «доводка до идеала» (2026-07-03, волны 1–4)

Полный аудит (9 агентов) → 4 волны исправлений (12 major + ~25 minor). Ключевое:

- **Надёжность confirm-пути:** зависшие `executing`-черновики (крэш посреди мутации) →
  терминальный `needs_review` + уведомление владельца + /diag (`reconcile_stale_executing`,
  env `EXECUTING_STALE_MINUTES`); partial_failure для батчей ключей («добавлено M, отклонено K»);
  честный учёт квоты по операциям батча; превью==созданное (micros кратны биллинг-единице);
  `drop_pending_updates` на старте; `SELECT FOR UPDATE` в сторе визарда.
- **Мультиаккаунт-подготовка (мутации НЕ включены):** исполнение привязано к
  `proposal.customer_id` с повторным `ensure_allowed`; грант-aware доступ на всех путях чтения
  (`ACCOUNT_ACCESS_MODE`); `/grant /revoke /accounts /whoami`; `get_stats` резолвит аккаунт;
  чтение текущего ГЕО кампании (§3); `/mcc` по всем настроенным MCC.
- **UX:** кнопки меню работают во время визардов (menu_guard, работа не теряется); фикс
  footgun'а финальной правки §19.8; warnings частичного успеха создания кампании;
  «‹ Назад»/крошки в визарде; пагинация пикеров; хаб «➕ Ещё»; параметры keyword research §7
  (ГЕО/язык/сеть/период); `/alerts` (пороги аномалий per-chat); валюта и язык в рассылках.
- **Архитектура:** порядок диспатча — `bot/handlers/__init__.py::HANDLER_MODULES` (star-импорты
  выпилены); FSM-состояния — `bot/states.py`; мёртвая таблица `whitelist` удалена в `0016` (позже
  возвращена рантайм-активной в `0017` — см. §1).

## 7. Второй предсдаточный проход (аудит 2026-07-03)

Повторный аудит по 3 docx + доведение до «профессионального инструмента». Сделано (см. коммиты):

- **Безопасность (golden rule #10):** закрыт fail-open стартового обхода MCC — нормализованно-пустой
  `customer_id` (из inline-комментария `.env.defaults`) протекал в `login_customer_id_set` и проходил
  `ensure_manager_allowed`, из-за чего первый `ga.search(customer_id='')` падал (прод-WARNING
  «Invalid customer ID ''»). Фикс: фильтр по нормализованному значению в `core/config.py` +
  fail-closed на пустом id + гигиена `.env.defaults`.
- **Латентность:** синхронный `build_client()` на event loop (~15 мест: хендлеры, планировщик,
  agent-loop) → `await build_client_async()` (холодная сборка off-loop).
- **UX/профессионализм:** убраны внутренние `§`-маркеры из меню Telegram; сырой англ. pydantic-дамп
  ошибок → человекочитаемый локализованный (`bot.ux.humanize_validation`); вежливый отказ
  не-whitelisted (с его chat_id, rate-limited) вместо тишины; актуализирован docstring
  `reports/service.py` (Sheets реализован).
- **Память/подсказки:** последний аккаунт («↻ как в прошлый раз» в пикере), «↻ повторить прошлый
  отчёт» в один тап, продолжить визард из `/start`, крошки шага «шаг N/7».

**§8 «нормализация валют» — согласованное отклонение (требует фиксации в договоре):** сводка по
дочерним MCC даёт **подытоги по каждой валюте без единого FX-курса** (golden rule #4 — не выдумываем
курсы). Это осознанный выбор (per-currency честнее конвертации по произвольному курсу); принимается
как выполнение §8 при письменном согласовании заказчика. Единый консолидированный total —
опциональный будущий FX-слой с явной пометкой «оценочно» (см. §5, п. 2).

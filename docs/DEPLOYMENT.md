# Деплой Aimash

Бот управляет **чужими деньгами** в Google Ads. Перед боевым запуском пройди весь
[prod-чеклист](#prod-чеклист). Любое изменение аккаунта — только после «да» пользователя
(confirm-гейт), и только на единственном разрешённом аккаунте `7753643025` (golden rules,
см. [CLAUDE.md](../CLAUDE.md)).

## 1. Предусловия
- Python 3.12, Docker + Docker Compose, Postgres 16 (в составе compose).
- Google Ads **developer token** (Basic), OAuth client (client_id/secret) и refresh token.
- Telegram bot token (@BotFather) и хотя бы один chat_id для env-бутстрапа whitelist (дальше
  операторы добавляются на лету — `/adduser`).

## 2. Окружение (.env)
`.env` хранит секреты и **никогда не коммитится** (он в `.gitignore`, и `.dockerignore`
исключает его из образа). Скопируй пример и заполни:

```bash
cp .env.example .env
```

⚠️ **Три слоя, и каждый следующий бьёт предыдущий:** код-дефолт (`core/config.py`) →
`.env.defaults` (**в git**, катится автодеплоем) → `.env` (untracked, руками на VPS) →
`environment:` в compose. Значение, вписанное в любой env-файл, **перебивает код навсегда**: когда
дефолт в коде вырастет, прод молча останется на вчерашнем (так `CRAWL_MAX_PAGES=70` держал краул §20
на 70 страницах, хотя код уже просил 1000). Поэтому **в env-файлы пишем только то, что ОТЛИЧАЕТСЯ
от код-дефолта**; правило держит `tests/test_env_defaults_drift.py`. Пошагово (что дописать на
сервере, как применить без даунтайма, откат) — [RUNBOOK_ENV.md](RUNBOOK_ENV.md).

Переменные (см. `core/config.py`):

| Переменная | Секрет | Назначение / по умолчанию |
|---|---|---|
| `ENV` | нет | `dev` (только TEST MCC) или `prod` (включает fail-fast по ключу шифрования) |
| `TELEGRAM_BOT_TOKEN` | **да** | токен бота |
| `TELEGRAM_WHITELIST_CHAT_IDS` | нет | `123,456` — **бутстрап** доступа к боту. Итоговый whitelist = этот env **∪ таблица `whitelist`** (рантайм-пополнение `/adduser`, `core.access.is_whitelisted`, кэш TTL). **Пусто = fail-closed: бот не отвечает НИКОМУ** (в `prod` пустой env роняет старт — `core/config.py`; env обязателен для бутстрапа первого админа). НЕ «отвечает всем». |
| `OPENROUTER_API_KEY` | **да** | ключ OpenRouter (LLM) |
| `OPENROUTER_BASE_URL` | нет | `https://openrouter.ai/api/v1` |
| `LLM_PARSING` / `LLM_COPY` / `LLM_FALLBACK` | нет | модели (сменяемы) |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | **да** | developer token |
| `GOOGLE_ADS_CLIENT_ID` | нет | OAuth client id |
| `GOOGLE_ADS_CLIENT_SECRET` | **да** | OAuth client secret |
| `GOOGLE_ADS_REFRESH_TOKEN` | **да** | refresh token |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | нет | MCC (контекст авторизации) |
| `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` | нет | набор аккаунтов для МУТАЦИЙ. **`all`/`*`** = все ВИДИМЫЕ боту (потолок `allowed_ceiling()`) — задаётся ТОЛЬКО явно. Явный CSV id = СУЗИТЬ набор. **Пусто = fail-closed в ЛЮБОМ окружении** (мутаций нет; prod поднимается read-only с warning — коэрция «пусто → all» снята 2026-07-30, BZ-1). Любая мутация — за confirm-гейтом. См. §2.1 |
| `SECRETS_ENCRYPTION_KEY` | **да** | Fernet-ключ шифрования токенов at-rest (обязателен в prod) |
| `TWO_FACTOR_ENABLED` | нет | §12 2FA-гейт опасных операций. `false` (дефолт) = выкл. `true` = перед исполнением опасной мутации бот просит PIN. **Fail-closed**: `true` без `TWO_FACTOR_PIN` ⇒ опасные операции блокируются (не пропускаются). См. §2.2 |
| `TWO_FACTOR_PIN` | **да** | PIN для 2FA (маскируется в логах/repr). Сверяется в коде constant-time (`core/twofa.py`) |
| `TWO_FACTOR_OPS_CSV` | нет | какие операции требуют кода (CSV). Дефолт `remove_campaign,remove_ad_group,update_budget,update_bid,update_keyword_bid,set_bidding_strategy`. Пусто ⇒ при включённом 2FA ничего не гейтится |
| `DATABASE_URL` | нет | строка подключения (в compose СОБИРАЕТСЯ из `POSTGRES_PASSWORD` на `postgres:5432` — значение из `.env` там не действует, `environment:` бьёт `env_file:`) |
| `POSTGRES_PASSWORD` | **да** (docker) | D3: пароль роли `aimash`. Читает compose (postgres + `DATABASE_URL` бота + бэкап-сайдкар). Пусто ⇒ `docker compose up` ОТКАЗЫВАЕТ. В prod пароль `aimash` (утёк в git) отвергается на старте бота. Ротация — §4.0 |
| `POSTGRES_RO_PASSWORD` | **да** (docker) | D3: пароль read-only роли `aimash_ro` (Postgres MCP). Задаётся при ПЕРВОЙ инициализации тома (`db/init/01-readonly-role.sh`) |
| `LOG_LEVEL` / `LOG_FORMAT` | нет | `INFO` / `text` (в prod рекомендуется `json`) |
| `GOOGLE_ADS_READ_CUSTOMER_IDS` | нет | доп. аккаунты для ЧТЕНИЯ (§8), CSV. Чтение НЕ открывает мутации (нужен ещё ALLOWED_CUSTOMER_IDS, §2.1) |
| `ACCOUNT_ACCESS_MODE` | нет | `auto`/`enforced`/`legacy` — пер-юзер изоляция аккаунтов; админ (`ADMIN_CHAT_IDS`) видит всё на чтение |
| `GOOGLE_ADS_LOGIN_CUSTOMER_IDS` | нет | доп. MCC для обхода дочерних (§8), CSV |
| `GOOGLE_ADS_API_VERSION` | нет | `v25` (мажор API; SDK-пин — в `pyproject.toml`, хард-пин — в `constraints.txt`) |
| `DEFAULT_GEO_COUNTRY_CODE` | нет | D7: страна-по-умолчанию для гео, когда бриф её не задал (ISO alpha-2). Дефолт **пусто** — код НЕ угадывает страну: без гео в брифе кампания не создастся. **Деплой ставит свой код** (Уганда → `UG`) |
| `DEFAULT_GEO_LOCALE` | нет | D7: язык названий локаций по умолчанию. Дефолт `ru`. **Пусто** ⇒ язык выводится из страны (UG→en) и совпадает с гео-таргетами Google; явное значение (`en`) побеждает |
| `GOOGLE_ADS_DAILY_OP_LIMIT` | нет | `15000` — дневной лимит операций API; на 95% мутации блокируются (`/quota`) |
| `DISABLE_ALL_MUTATIONS` | нет | BZ-1 аварийный рубильник: НЕ пусто и не `0/false/no/off` ⇒ ВСЕ мутации остановлены (чтение работает). Fail-closed: опечатка = ВКЛЮЧЁН. Читается живьём из окружения (мимо кэша настроек) — `docker compose up -d` после правки `.env`, рестарта кода не надо |
| `KILL_SWITCH_FILE` | нет | `KILL_SWITCH` — файл-флаг рубильника: `docker exec aimash-bot touch /app/KILL_SWITCH` = стоп мутаций БЕЗ рестарта, `rm` = обратно. Пусто = файловый канал выключен осознанно. ⚠️ пересоздание контейнера файл снимает — стойкий стоп через env-канал |
| `DAILY_BUDGET_INCREASE_MAX_OPS` | нет | B1-4: сколько ПОВЫШЕНИЙ бюджета на аккаунт за 24 ч (понижения не считаются). `0` = выкл; **в prod автодефолт `10`**. Проверка до claim — отказ не сжигает подтверждение |
| `DAILY_BUDGET_INCREASE_CAP_UNITS` | нет | B1-4: суммарный ПРИРОСТ бюджета на аккаунт за 24 ч, в единицах валюты. `0` = выкл (дефолта нет и в prod — включается явно) |
| `SENTRY_DSN` / `SENTRY_TRACES_SAMPLE_RATE` | DSN — да | опц. телеметрия ошибок (`""` = выкл.) |
| `REPORT_SCHEDULE` | нет | `0 9 * * *` — cron плановых отчётов (§14) |
| `ANOMALY_INTERVAL_HOURS` / `CLEANUP_INTERVAL_MINUTES` | нет | `6` / `60` — кадэнс аномалий и очистки просроченного (§14) |
| `CAMPAIGN_DRAFT_TTL_HOURS` | нет | `72` — TTL черновика визарда §19 (переживает рестарт; старше → abandoned) |
| `CRAWL_MAX_PAGES` / `CRAWL_MAX_DEPTH` | нет | `1000` / `3` — потолок страниц и глубина краула сайта (§20.4). «Краулим весь сайт» — решение владельца 2026-07; **не пинить меньшим значением в env** |
| `CRAWL_TIME_BUDGET_S` / `CRAWL_DELAY_S` / `CRAWL_CONCURRENCY` | нет | `240.0` / `0.5` / `6` — бюджет времени на обход, вежливая пауза, одновременных запросов к сайту (§20.4) |
| `CRAWL_MAX_TEXT_CHARS` / `CRAWL_STORE_MAX_PAGES` / `CRAWL_STALE_MINUTES` | нет | `5000` / `1000` / `30` — текста со страницы; сколько страниц хранится в БД; порог реконсиляции зависших crawl_jobs (§20) |
| `CRAWL_LLM_TEXT_CHARS` / `CRAWL_LLM_PER_PAGE_CHARS` | нет | `24000` / `1500` — бюджет текста краула на промпт и квота на одну страницу (§20.5) |
| `DOSSIER_CHUNK_CHARS` / `DOSSIER_MAP_CONCURRENCY` / `DOSSIER_MAX_MAP_CALLS` | нет | `7000` / `4` / `40` — map-reduce досье по сайту: чанк, параллелизм LLM, потолок вызовов (§20.5) |
| `DOSSIER_MAX_PAGES_PER_TYPE` / `PROFILE_CTX_CHARS` | нет | `12` / `3000` — страниц одного типа в досье; сколько символов профиля подаётся контекстом в §19/§10 |
| `CLIENT_TEXT_IDLE_S` | нет | `60` — авто-сохранение накопленного текста профиля по таймауту (§20.3); `0` = выкл. |
| `ADMIN_CHAT_IDS` | нет | CSV chat_id админов (`/whoami`). Пусто ⇒ нет алертов об ошибках и readiness-пинга, `/grant`/`/adduser`/`/addadmin` недоступны никому |
| `ERROR_ALERT_INTERVAL_MINUTES` | нет | `0` (выкл) — период алерта админам о НОВЫХ `error_events` (§15). На проде `15` (в `.env.defaults`); без `ADMIN_CHAT_IDS` — no-op |
| `LLM_KEYWORDS` / `LLM_CLUSTERING` / `LLM_EXTRACT` / `LLM_DOSSIER` / `LLM_ANALYST` | нет | модели по ролям (сменяемы). Ключи/копирайт — `claude-opus-4.8` (решение владельца 2026-07); **старый пин в `.env` перебьёт это — строку удалить, не править** |
| `SHEETS_PUBLIC_LINK` / `SHEETS_OWNER_EMAIL` | нет | `true` / пусто — «доступ по ссылке» для отчётных таблиц; сверка владельца (см. §5) |

### 2.1 МУТАЦИИ на всех видимых аккаунтах (решение владельца 2026-07) — включается явным `all`

**Draft-only доктрина снята.** Явный `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` разрешает мутации на ВСЕХ
аккаунтах, видимых боту под его MCC (потолок `allowed_ceiling()` = Draft ∪ read-list ∪ дочерние
обхода). Реальные деньги — но две несменяемые страховки держатся всегда: **confirm-гейт** (каждая
мутация только после «да» + `confirmation_id`, перепроверка на исполнении) и **потолок видимости**
(аккаунт вне MCC мутировать нельзя — бот его физически не видит). **Пусто = мутаций нет** в любом
окружении (fail-closed; прежняя коэрция «пусто в prod → all» снята 2026-07-30 — «очистить env» теперь
закрывает мутации, а не открывает).

0. **Проверь готовность до включения.** `/mutready all` (только админ) — сводка по всем видимым:
   видимость, статус, OAuth-покрытие, живое чтение, гранты, 2FA, вкл/выкл мутаций. `/mutready <id>` —
   подробный чек-лист по одному. Команда ТОЛЬКО диагностирует — конфиг не трогает.
1. **Включение — только явное.** `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` в `.env`. Пустое значение
   мутаций НЕ открывает (prod поднимется read-only с warning в логах).
2. **Сузить набор (по желанию).** Вместо `all` — явный CSV id, напр.
   `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=7753643025,5437782039`. Эффективный потолок не пропустит id,
   которого бот не видит (защита от опечатки в чужой боевой аккаунт).
3. **OAuth при другом MCC.** Аккаунт под ДРУГИМ менеджером требует per-account refresh-токена:
   `python scripts/register_account.py` → таблица `oauth_tokens` (иначе `build_client` его не
   аутентифицирует — он виден замку, но не подключён). Аккаунты под env-MCC покрыты общим токеном.
4. **Как выбрать целью.** Оператор делает аккаунт активным (`/account <id>` или пикер) — тогда
   NL-команды изменений идут на него; при НЕзакреплённом аккаунте и нескольких живых бот СНАЧАЛА
   заставит выбрать аккаунт (не угадывает чужие деньги). Карточка подтверждения ВСЕГДА пишет
   «⚠️ Аккаунт изменения: Имя · id» (Draft — «🧪 …»). Confirm-гейт и гард бюджета (`user_initiated`)
   сохраняются.

> ℹ️ Рекомендация: включи 2FA (§2.2) ДО работы с боевыми аккаунтами. Чтобы намеренно оставить
> только Draft мутабельным (напр. на стейджинге) — задай `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=7753643025`.
> В dev/test пусто ⇒ мутаций нет вовсе (fail-closed) — локальная разработка боевые не трогает.

### 2.2 Двухфакторное подтверждение опасных операций (§12 2FA) — опционально

По умолчанию **выключено** (поведение не меняется). Включение добавляет второй фактор «кто-то нажал
✅» против случайного/поспешного подтверждения денежной или необратимой операции:

1. Задай `TWO_FACTOR_ENABLED=true` и `TWO_FACTOR_PIN=<секрет>` (через секрет-менеджер).
2. По желанию сузь/расширь список гейтящихся операций через `TWO_FACTOR_OPS_CSV` (имена как в
   `agent/tools/schemas.py`).

Поток: оператор жмёт ✅ на опасной операции → бот просит PIN одним сообщением. Верный код → операция
исполняется; неверный (до 3 попыток) или «отмена» → **черновик остаётся `pending`** (не сожжён),
можно нажать ✅ заново. PIN сверяется в КОДЕ (`hmac.compare_digest`, constant-time), сырьё не
логируется. **Fail-closed:** `TWO_FACTOR_ENABLED=true` без `TWO_FACTOR_PIN` ⇒ опасные операции
блокируются (не пропускаются без проверки). Confirm-гейт и замок аккаунта при этом сохраняются —
2FA стоит поверх них, а не вместо. Инварианты — `tests/test_twofa.py`.

### Одно-операторный доступ на чтение (все аккаунты в пикерах)
Пикеры применяют пер-пользовательский грант: в режиме `ACCOUNT_ACCESS_MODE=auto` первый `/grant`
включает enforcement и прячет не-гранованные аккаунты. **Админ (`ADMIN_CHAT_IDS`) видит ВСЕ
read-allowed аккаунты без грантов** (владелец-одиночка). Для чистого одно-операторного режима можно
явно задать `ACCOUNT_ACCESS_MODE=legacy`.

### Генерация Fernet-ключа
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Положи результат в `SECRETS_ENCRYPTION_KEY`. **Ротация**: ключ шифрует `oauth_tokens.refresh_token_enc`.
Чтобы сменить ключ без потери токенов — расшифровать старым (`core.secrets.decrypt`) и
зашифровать новым (`core.secrets.encrypt`) каждую строку, затем заменить ключ в окружении.

## 3. Секреты в проде
Не клади боевые секреты в `.env` на диск рядом с приложением. Используй секрет-менеджер
(Docker secrets / AWS SSM / GCP Secret Manager / k8s Secrets) и **прокидывай как env в рантайме**.
Образ собирается без секретов (`.dockerignore` исключает `.env`, `google-ads.yaml`, `*.json`,
`*.pem`, `*.key`). `ENV=prod` без валидного `SECRETS_ENCRYPTION_KEY` → приложение **не стартует**
(fail-fast в `core/config.py`).

### Ротация Google-токена
`make refresh-token` (`scripts/get_refresh_token.py`) → новый refresh token → зашифровать в
`oauth_tokens` через `core.secrets.encrypt`. При утечке: отозвать в Google, выпустить новый,
передеплоить. Секрет-скан (gitleaks) в pre-commit и CI блокирует коммит токенов.

### Google Sheets-экспорт (команда /sheets, ТЗ §9)
`/sheets` создаёт новую Google-таблицу с отчётом (лист «Сводка» + лист на каждую разбивку) и
присылает ссылку. Нужен **отдельный OAuth-scope** `https://www.googleapis.com/auth/drive.file`,
которого НЕТ у Google Ads токена (scope `adwords`). Чтобы включить live-выгрузку:
1. **Включить Google Sheets API** в том же Google Cloud-проекте, что и OAuth-клиент:
   `https://console.developers.google.com/apis/api/sheets.googleapis.com/overview?project=<PROJECT>`
   (без этого — `HttpError 403 SERVICE_DISABLED`; после включения подождать 1–2 мин на пропагацию).
2. **Включить Google Drive API** — ОТДЕЛЬНЫЙ API, тот же проект:
   `https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=<PROJECT>`
   Без него таблицы создаются (это Sheets API), но `permissions.create` падает
   `403 accessNotConfigured` ⇒ ссылка **никогда** не открывается «всем по ссылке» и получатель видит
   «Запросить доступ». Ровно это и было в проде до 2026-07-13 (Sheets включён, Drive — нет): шаринг
   молча деградировал, потому что `_share_anyone` не raise. Проверка — `scripts/check_sheets_share.py`.
3. Перевыпустить refresh-токен тем же OAuth-клиентом (`GOOGLE_ADS_CLIENT_ID`/`SECRET`), указав при
   согласии оба scope: `adwords` **и** `drive.file` (`make refresh-token` / `scripts/get_refresh_token.py`
   уже просит оба) → обновить `GOOGLE_ADS_REFRESH_TOKEN` → перезапустить бота.

Типичные ошибки: `invalid_scope` → токен без `drive.file` (шаг 3); `SERVICE_DISABLED` → Sheets API не
включён (шаг 1); `accessNotConfigured` при шаринге → Drive API не включён (шаг 2). `.xlsx` через `/export` работает всегда, без этой настройки. Реализация —
`reports/sheets.py` (`spreadsheets.create` + `values.batchUpdate`, ТЗ §16); сборка вкладок —
read-only и покрыта тестами офлайн.

#### На чьём Google Drive лежат таблицы
Сервисного аккаунта в проекте **нет**. Таблица создаётся в «Моём диске» того Google-аккаунта, чьим
refresh-токеном ходит Sheets, — он владелец файла, файл ест его квоту (15 ГБ), и только он может
сменить владельца/удалить его окончательно. Scope `drive.file` означает, что бот видит **только
созданные им самим** файлы — чужой Диск ему недоступен.

Какой это аккаунт:
- `SHEETS_REFRESH_TOKEN` **задан** → он (аккаунт-хранилище таблиц);
- пусто → тот же, что у Google Ads (`GOOGLE_ADS_REFRESH_TOKEN`).

Чтобы таблицы копились на ОТДЕЛЬНОМ gmail (напр. `myhalads@gmail.com`), а Ads-доступ остался прежним:
```bash
python scripts/get_refresh_token.py --sheets   # войти НУЖНЫМ gmail-ом; scope РОВНО ОДИН: drive.file
# → пишет SHEETS_REFRESH_TOKEN в .env; GOOGLE_ADS_REFRESH_TOKEN не трогается
SHEETS_OWNER_EMAIL=myhalads@gmail.com          # сверка (не гейт)
```
Именно так, а не перевыпуск общего токена: у аккаунта-хранилища может не быть доступа к MCC — общий
токен под ним уронил бы весь Google Ads. Аккаунт должен быть в **Test users** OAuth consent screen
(если приложение не Published), иначе «Access blocked».

⛔ **У аккаунта-хранилища просим ТОЛЬКО `drive.file`** (`SHEETS_SCOPES`, `reports/sheets.py` +
`scripts/get_refresh_token.py`; синхрон дублей — `tests/test_keyword_sheets.py`). Он у Google
**non-sensitive** ⇒ верификация приложения не нужна. Добавишь туда `spreadsheets.readonly`
(sensitive) — Google покажет владельцу аккаунта **«This app is blocked. This app tried to access
sensitive info in your Google Account»**, и согласие он выдать не сможет вообще (напоролись 2026-07 на
аккаунте заказчика). Кто тогда читает **чужие** таблицы (§19.4.1 «Ссылка на Google Sheets», `/kw add`):
**Ads-токен** — sensitive-scope есть у него (наш аккаунт, consent выдан),
`_oauth_credentials(external_read=True)`. Следствие: чужая таблица должна быть доступна **Ads**-аккаунту
(«всем, у кого есть ссылка» либо расшарена на него); иначе 403 → бот попросит прислать ключи текстом.
СВОЮ таблицу (round-trip §19.4.2) читаем кредами аккаунта-хранилища (`own_file=True`).

##### Если аккаунт-хранилище у ЗАКАЗЧИКА (пароль он не отдаёт) — `--remote`
`run_local_server()` ждёт редиректа на **свой** localhost, т.е. браузер обязан быть на машине скрипта;
OOB-поток («код на экране Google») Google отключил в 2023. Остаётся loopback + ручная передача кода:
```bash
python scripts/get_refresh_token.py --sheets --remote
```
Скрипт печатает **ссылку согласия** и готовый текст для владельца аккаунта. Он: открывает ссылку у себя
(войдя нужным gmail-ом) → «Google hasn't verified this app» → Advanced → Go to … → разрешает
**единственный** доступ (`drive.file`) → браузер уходит на `http://localhost:8765/?code=…` и показывает «Не удалось открыть страницу»
(**так и должно быть**) → копирует **всю строку из адресной строки** и присылает её. Строку вставляем в
ожидающий ввод скрипта — он обменивает код на `SHEETS_REFRESH_TOKEN` и пишет его в `.env` (токен на
экран не печатается). Код **одноразовый** и живёт ~10 минут; протух → перезапустить скрипт и прислать
новую ссылку.

⚠️ **OAuth consent screen обязан быть `In production`** (APIs & Services → OAuth consent screen →
Publish app). В статусе `Testing` Google выдаёт refresh-токен со сроком жизни **7 дней** — через неделю
`/sheets` начнёт падать `invalid_grant`. Неверифицированное приложение — это нормально, экран-
предупреждение проходится через Advanced.

Проверка живьём (создаёт временную таблицу, печатает владельца и права, затем удаляет её):
```bash
PYTHONIOENCODING=utf-8 python scripts/check_sheets_share.py [--keep]
```
Ждём `Владелец файла: <нужный gmail>` и `✅ ДОСТУПНО ВСЕМ ПО ССЫЛКЕ: role=writer`. Ненулевой
exit-код = либо не тот Drive, либо `anyone`-доступ не выдан (типично — политика Google Workspace
запрещает внешние ссылки; лечится только на стороне Google).

Все выданные ботом таблицы пишутся в реестр `sheet_exports` и доступны оператору командой
`/mysheets` (последние 10 таблиц ЭТОГО чата: дата · вид · аккаунт · ссылка).

## 4. База и миграции
```bash
docker compose up -d postgres        # поднять только БД (хост-порт 5433)
alembic upgrade head                 # применить миграции (Postgres)
```

### 4.0 Пароли БД: только из `.env` (D3, аудит 2026-07-13)
Раньше пароли ролей лежали ЛИТЕРАЛАМИ в tracked-файлах (`docker-compose.yml`: `POSTGRES_PASSWORD:
aimash` и `DATABASE_URL: …aimash:aimash@…`; `db/init/01-readonly-role.sql`: `aimash_ro`) — то есть
были публичны для всех, кто видел репозиторий, а `environment:` в compose ещё и **перебивал**
`env_file:`, поэтому сильный пароль из серверного `.env` молча игнорировался.

Теперь compose подставляет пароли из `.env` через `${POSTGRES_PASSWORD:?}` / `${POSTGRES_RO_PASSWORD:?}`
— **без них `docker compose up` отказывается стартовать** (fail-closed, правило #10), а в `prod`
старт с утёкшим паролем (`aimash` / `aimash_ro`) отвергает и сам бот (`core.config`).

**Ротация на уже работающем VPS (том проинициализирован — новый пароль сам не применится!):**
```bash
# 1) старым паролем сменить пароли ролей ВНУТРИ БД
docker compose exec postgres psql -U aimash -d aimash \
  -c "ALTER ROLE aimash    WITH PASSWORD '<новый>';" \
  -c "ALTER ROLE aimash_ro WITH PASSWORD '<новый-ro>';"
# 2) записать те же значения в .env на сервере
#    POSTGRES_PASSWORD=<новый>
#    POSTGRES_RO_PASSWORD=<новый-ro>
# 3) пересоздать контейнеры (бот подхватит новый DATABASE_URL из compose)
docker compose up -d --force-recreate bot backup
```
Порядок важен: сначала `ALTER ROLE`, потом `.env` — иначе бот/бэкап получат пароль, которого БД
ещё не знает, и уйдут в крэш-луп. На **чистом** томе шаги 1 и 3 не нужны: init создаст роли уже с
паролями из `.env`.
На Postgres схему ведёт Alembic. `db.session.init_db()` (create_all) используется только для
dev/SQLite и тестов. В dev (SQLite) `init_db()` дополнительно зовёт `heal_sqlite_schema` —
аддитивно дотягивает недостающие NULLABLE-колонки (на SQLite `create_all` НЕ альтерит уже
существующие таблицы → после новой миграции dev-БД иначе дрейфует от модели). На Postgres
схему по-прежнему ведёт ТОЛЬКО Alembic (self-heal трогает лишь SQLite).

**В Docker миграции применяются автоматически** при старте контейнера бота
(`docker-entrypoint.sh` → `alembic upgrade head` ДО `python -m bot.main`; падение миграции
останавливает старт — fail-fast). Прогнать вручную (напр. на хосте или для проверки):
```bash
docker compose run --rm bot alembic upgrade head
```

### Верификация прод-БД (Postgres) — `scripts/verify_postgres.py`
Перед боевым запуском подтверди, что на **чистом Postgres** миграции доходят до head и рантайм-схема
читается (в отличие от dev-SQLite, где схему аддитивно чинит `heal_sqlite_schema`; на Postgres истина
— только Alembic):
```bash
DATABASE_URL=postgresql+asyncpg://aimash:***@localhost:5432/aimash python scripts/verify_postgres.py
```
Скрипт: проверяет, что DSN — Postgres (иначе отказ) → `alembic upgrade head` → сверяет `current` == head
(head берётся из дерева миграций, сейчас `0025_sheet_exports` — в доке не хардкодим, он растёт)
→ `init_db()` (детектор дрейфа модель⟂миграции; на Postgres это no-op `create_all`)
→ смоук-чтение ключевых таблиц. `exit 0` — чисто, `exit 1` — сбой. Пароль БД в выводе маскируется.

## 5. Запуск
```bash
docker compose up --build            # postgres + bot + scheduler + backup
```
Внутри сети compose бот ходит в БД по `postgres:5432`; на хосте Postgres проброшен на **5433**
(чтобы не конфликтовать с локальным Postgres). Бот работает в режиме long-poll (HTTP-порта нет),
healthcheck контейнера — лёгкий импорт конфигурации.

### Планировщик / расписание (§14)
**Отдельный контейнер `aimash-scheduler` = `python -m scheduler`** (C4, топология «три процесса»):
джобы обязаны пережить архивацию кнопочного слоя — в них живёт `reconcile_stale_executing`,
поднимающий зависшие мутации в `needs_review`. Кто владеет джобами, решает **advisory-lock роли
`scheduler`**, а не env: `SCHEDULER_IN_BOT` (код-дефолт `true`, на проде снят в `docker-compose.yml`)
задаёт лишь намерение. Оба кандидата берут lock; проигравший джобы НЕ регистрирует, а standalone-
процесс ещё и выходит ненулевым кодом — забытый флаг даёт громкий крэш-луп, а не двойные дайджесты
и двойной reconcile одного черновика.
⚠️ Кнопочного слоя в этом контейнере нет по построению: порт `scheduler/delivery.py` не заполнен ⇒
дайджесты уходят текстом, а thr-tune не шлётся вовсе (его сообщение целиком — предложение под тап).

Кадэнс read-only задач задаётся через env (дефолты безопасны — можно не задавать):
- `REPORT_SCHEDULE` — crontab-строка планового отчёта (`мин час день месяц день_недели`); одним
  полем и ежедневно, и еженедельно: `0 9 * * *` (ежедн. 09:00) | `0 9 * * 1` (еженед., пн 09:00).
  Невалидную/пустую строку бот НЕ роняет стартом — откат на ежедневно 09:00 + warning (fail-safe).
- `ANOMALY_INTERVAL_HOURS` (по умолч. 6) — период проверки аномалий и алертов.
- `CLEANUP_INTERVAL_MINUTES` (по умолч. 60) — период очистки просроченных черновиков.

Планировщик только читает/уведомляет/чистит — аккаунт никогда не меняет (golden rule #3).

## 5.1 Авто-деплой на VPS (CI/CD, GitHub Actions)

Пуш в **master** → `.github/workflows/ci.yml`: сначала `lint-test` + `secret-scan`, и ТОЛЬКО при
их успехе — job `deploy` заходит по SSH на VPS и катит обновление. Сломанное (красные тесты или
gitleaks) на сервер НЕ попадает. Миграции применяются сами при старте контейнера
(`docker-entrypoint.sh` → `alembic upgrade head`, fail-fast).

Что делает деплой на сервере (`/opt/aimash`):
```bash
git fetch --prune origin master
git reset --hard origin/master      # деплой-чекаут: ровно состояние master (.env untracked — сохраняется)
docker compose up -d --build
docker image prune -f
```

**Секреты репозитория** (GitHub → Settings → Secrets and variables → Actions → New repository secret):

| Секрет | Значение |
|---|---|
| `VPS_SSH_HOST` | `167.233.48.243` |
| `VPS_SSH_USER` | пользователь деплоя на VPS (напр. `root` или `deploy`) |
| `VPS_SSH_KEY` | приватный SSH-ключ (весь, с заголовками `-----BEGIN…`), публичная часть — в `~/.ssh/authorized_keys` пользователя на VPS |
| `VPS_SSH_PORT` | опц., по умолчанию `22` |

**Разовая подготовка сервера:** в `/opt/aimash` должен быть git-чекаут, отслеживающий `origin/master`
(`git remote -v` → ваш origin; `git branch --set-upstream-to=origin/master`), доступный пушером ключ,
установленный Docker + Compose v2, и заполненный `.env` (untracked — `git reset --hard` его не трогает).

**Откат:** `git revert <commit> && git push origin master` (повторно прогонит CI+deploy) — предпочтительно;
либо на сервере `git reset --hard <предыдущий-тег/коммит> && docker compose up -d --build` вручную.

Без заданных секретов job `deploy` будет падать (lint-test всё равно зелёный) — это сигнал «настрой секреты».

## 6. Prod-чеклист
- [ ] `ENV=prod` (включает fail-fast по `SECRETS_ENCRYPTION_KEY`).
- [ ] `SECRETS_ENCRYPTION_KEY` — валидный Fernet-ключ, из секрет-менеджера (не из репо).
- [ ] `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` — задан ЯВНО: `all` (все ВИДИМЫЕ мутабельны) или CSV id
      (сузить). Пусто = мутаций нет (fail-closed, prod поднимется read-only). Мутации только среди
      видимых (код-минимум `ALLOWED_CEILING`={Draft} зашит в `ads/client.py`, `.env` его не понизит)
      и только за confirm-гейтом. Перед включением — `/mutready all`. См. §2.1.
- [ ] Аварийный рубильник знаком дежурному: `docker exec aimash-bot touch /app/KILL_SWITCH` = стоп
      мутаций сразу; стойко — `DISABLE_ALL_MUTATIONS=1` в `.env` + `docker compose up -d`. См. §2.
- [ ] B1-4 кап бюджета: `DAILY_BUDGET_INCREASE_MAX_OPS` (prod-автодефолт 10) устраивает; кап суммы
      `DAILY_BUDGET_INCREASE_CAP_UNITS` — включить по решению владельца.
- [ ] 2FA (`TWO_FACTOR_ENABLED=true` + PIN) рекомендуется до работы с боевыми аккаунтами (§2.2).
- [ ] `TELEGRAM_WHITELIST_CHAT_IDS` задан для бутстрапа (пусто = fail-closed: бот молчит всем; в `prod`
      пустой env роняет старт; рантайм-операторы — таблица `whitelist` через `/adduser`).
- [ ] `LOG_FORMAT=json`, проверена редакция секретов в логах (`core/logging.py`).
- [ ] Все секреты — через секрет-менеджер, не в `.env` на диске.
- [ ] gitleaks чист; в контексте сборки нет кред-файлов (см. `.dockerignore`).
- [ ] `alembic upgrade head` применён; бэкапы БД настроены. **Верификация Postgres** пройдена
      (`scripts/verify_postgres.py` → `exit 0`: миграции→head + смоук-чтение).
- [ ] Таймауты/ретраи проверены (`core/resilience.py`: `ADS_*`, `LLM_*`).
- [ ] По желанию §12 2FA: `TWO_FACTOR_ENABLED=true` + `TWO_FACTOR_PIN` (иначе опасные операции
      блокируются — fail-closed). См. §2.2.
- [ ] Live-смоук создающих кампаний на Draft (PAUSED, $0): `scripts/live_smoke_gdn.py` (GDN §11),
      `scripts/live_smoke_video_dg.py` (Video/Demand Gen). Оставленные PAUSED-кампании удалить.
- [ ] Прогон на TEST MCC завершён; на боевой аккаунт переключаемся осознанно.

## 7. Напоминание о золотых правилах
Confirm-гейт на каждую мутацию · обязательный `confirmation_id` · замок аккаунта `7753643025`
(`ensure_allowed`) · длину RSA считает КОД (кириллица = 1) · секреты никогда в логи/гит/промпт ·
scheduler только читает и уведомляет (никогда не меняет аккаунт). Подробно — [CLAUDE.md](../CLAUDE.md).

## 8. Предпродакшн-харденинг (2026-07): наблюдаемость / устойчивость / сохранность
Поверх уже имевшихся `error_events` + `/diag` + Sentry + fail-fast-валидаторов добавлено:

- **Проактивные алерты об ошибках (A1).** `ERROR_ALERT_INTERVAL_MINUTES>0` + непустой
  `ADMIN_CHAT_IDS` ⇒ scheduler шлёт админам дедуплённый дайджест НОВЫХ инцидентов (без traceback в
  чат; подробности — `/diag`). 0 или нет админов ⇒ выкл. Плюс `except`-блоки команд теперь пишут в
  `error_events` (раньше — только глобальный on_error/scheduler): `/diag` видит все инциденты.
- **Кнопки `/diag` (A3):** 🔄 обновить · ⚠️ за сегодня/🗂 все · 🔍 полный (редактированный)
  traceback инцидента — только админу.
- **Readiness-пинг на старте (B1):** при успешном запуске бот шлёт админам «✅ запущен · миграция
  HEAD · N аккаунтов · модель». Не пришёл после редеплоя ⇒ бот не поднялся. CI-деплой теперь ещё и
  **проверяет здоровье контейнера** после `compose up` (3 сэмпла статуса) — крэш-луп красит job.
- **Гард одного инстанса (B2):** Postgres advisory-lock на старте — второй polling-инстанс чисто
  выходит (убирает 409 Conflict при перекрытии контейнеров на редеплое). На SQLite — no-op.
- **Автобэкап БД (C1):** сайдкар `backup` в `docker-compose.yml` делает `pg_dump -Fc` в `./backups`
  (ротация `BACKUP_RETAIN=14`) сразу на старте и раз в сутки. ⚠️ Локальный том — не офсайт: выгружай
  `./backups` наружу. ⚠️ `SECRETS_ENCRYPTION_KEY` бэкапь ОТДЕЛЬНО (иначе `oauth_tokens` в дампе
  мертвы — см. [BACKUP.md](BACKUP.md)). Постгрес получил `restart: unless-stopped` (B3).
- **Ретеншн-очистка (C2):** `ERROR_EVENTS_RETAIN_DAYS` / `CRAWL_JOBS_RETAIN_DAYS` — daily-purge
  растущих таблиц. `audit_log` НЕ трогается никогда (денежный реестр, ручной колд-архив).
- **Пер-юзер потолок LLM (C3):** `LLM_DAILY_CALLS_PER_USER>0` ⇒ дневной лимит запросов к ИИ на chat_id
  (fail-closed на 100%, warn на 80%). 0 ⇒ выкл (дефолт — не удивить владельца-оператора).

Prod-чеклист (доп.): по желанию `ERROR_ALERT_INTERVAL_MINUTES=15` (+ `ADMIN_CHAT_IDS`); убедиться,
что сайдкар `backup` поднят и `./backups` выгружается офсайт; `SECRETS_ENCRYPTION_KEY` в бэкапе
отдельно от дампа.

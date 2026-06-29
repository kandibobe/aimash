# Деплой Aimash

Бот управляет **чужими деньгами** в Google Ads. Перед боевым запуском пройди весь
[prod-чеклист](#prod-чеклист). Любое изменение аккаунта — только после «да» пользователя
(confirm-гейт), и только на единственном разрешённом аккаунте `7753643025` (golden rules,
см. [CLAUDE.md](../CLAUDE.md)).

## 1. Предусловия
- Python 3.12, Docker + Docker Compose, Postgres 16 (в составе compose).
- Google Ads **developer token** (Basic), OAuth client (client_id/secret) и refresh token.
- Telegram bot token (@BotFather) и список chat_id для whitelist.

## 2. Окружение (.env)
`.env` хранит секреты и **никогда не коммитится** (он в `.gitignore`, и `.dockerignore`
исключает его из образа). Скопируй пример и заполни:

```bash
cp .env.example .env
```

Переменные (см. `core/config.py`):

| Переменная | Секрет | Назначение / по умолчанию |
|---|---|---|
| `ENV` | нет | `dev` (только TEST MCC) или `prod` (включает fail-fast по ключу шифрования) |
| `TELEGRAM_BOT_TOKEN` | **да** | токен бота |
| `TELEGRAM_WHITELIST_CHAT_IDS` | нет | `123,456` — кому разрешён бот (пусто = отвечает ВСЕМ) |
| `OPENROUTER_API_KEY` | **да** | ключ OpenRouter (LLM) |
| `OPENROUTER_BASE_URL` | нет | `https://openrouter.ai/api/v1` |
| `LLM_PARSING` / `LLM_COPY` / `LLM_FALLBACK` | нет | модели (сменяемы) |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | **да** | developer token |
| `GOOGLE_ADS_CLIENT_ID` | нет | OAuth client id |
| `GOOGLE_ADS_CLIENT_SECRET` | **да** | OAuth client secret |
| `GOOGLE_ADS_REFRESH_TOKEN` | **да** | refresh token |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | нет | MCC (контекст авторизации) |
| `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` | нет | `7753643025` — белый список (пусто = fail-closed, всё запрещено) |
| `SECRETS_ENCRYPTION_KEY` | **да** | Fernet-ключ шифрования токенов at-rest (обязателен в prod) |
| `DATABASE_URL` | нет | строка подключения (в compose задаётся на `postgres:5432`) |
| `LOG_LEVEL` / `LOG_FORMAT` | нет | `INFO` / `text` (в prod рекомендуется `json`) |

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
2. Перевыпустить refresh-токен тем же OAuth-клиентом (`GOOGLE_ADS_CLIENT_ID`/`SECRET`), указав при
   согласии оба scope: `adwords` **и** `drive.file` (`make refresh-token` / `scripts/get_refresh_token.py`
   уже просит оба) → обновить `GOOGLE_ADS_REFRESH_TOKEN` → перезапустить бота.

Типичные ошибки: `invalid_scope` → токен без `drive.file` (шаг 2); `SERVICE_DISABLED` → API не
включён (шаг 1). `.xlsx` через `/export` работает всегда, без этой настройки. Реализация —
`reports/sheets.py` (`spreadsheets.create` + `values.batchUpdate`, ТЗ §16); сборка вкладок —
read-only и покрыта тестами офлайн.

## 4. База и миграции
```bash
docker compose up -d postgres        # поднять только БД (хост-порт 5433)
alembic upgrade head                 # применить миграции (Postgres)
```
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

## 5. Запуск
```bash
docker compose up --build            # postgres + bot
```
Внутри сети compose бот ходит в БД по `postgres:5432`; на хосте Postgres проброшен на **5433**
(чтобы не конфликтовать с локальным Postgres). Бот работает в режиме long-poll (HTTP-порта нет),
healthcheck контейнера — лёгкий импорт конфигурации.

### Планировщик / расписание (§14)
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
- [ ] `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=7753643025` (пусто = fail-closed; потолок `ALLOWED_CEILING`
      зашит в `ads/client.py` и `.env` его не расширит).
- [ ] `TELEGRAM_WHITELIST_CHAT_IDS` задан (пусто = бот отвечает всем).
- [ ] `LOG_FORMAT=json`, проверена редакция секретов в логах (`core/logging.py`).
- [ ] Все секреты — через секрет-менеджер, не в `.env` на диске.
- [ ] gitleaks чист; в контексте сборки нет кред-файлов (см. `.dockerignore`).
- [ ] `alembic upgrade head` применён; бэкапы БД настроены.
- [ ] Таймауты/ретраи проверены (`core/resilience.py`: `ADS_*`, `LLM_*`).
- [ ] Прогон на TEST MCC завершён; на боевой аккаунт переключаемся осознанно.

## 7. Напоминание о золотых правилах
Confirm-гейт на каждую мутацию · обязательный `confirmation_id` · замок аккаунта `7753643025`
(`ensure_allowed`) · длину RSA считает КОД (кириллица = 1) · секреты никогда в логи/гит/промпт ·
scheduler только читает и уведомляет (никогда не меняет аккаунт). Подробно — [CLAUDE.md](../CLAUDE.md).

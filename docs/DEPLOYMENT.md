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
1. Перевыпустить refresh-токен тем же OAuth-клиентом (`GOOGLE_ADS_CLIENT_ID`/`SECRET`), указав при
   согласии оба scope: `adwords` **и** `drive.file`.
2. Обновить `GOOGLE_ADS_REFRESH_TOKEN`.

Без scope `/sheets` отвечает понятной ошибкой; `.xlsx` через `/export` работает всегда. Реализация —
`reports/sheets.py` (`spreadsheets.create` + `values.batchUpdate`, ТЗ §16); сборка вкладок —
read-only и покрыта тестами офлайн.

## 4. База и миграции
```bash
docker compose up -d postgres        # поднять только БД (хост-порт 5433)
alembic upgrade head                 # применить миграции (Postgres)
```
На Postgres схему ведёт Alembic. `db.session.init_db()` (create_all) используется только для
dev/SQLite и тестов. В контейнере миграции одноразово:
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

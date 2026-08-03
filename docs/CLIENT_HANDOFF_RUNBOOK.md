# HERMES 3.0 — Client Handoff Runbook

Эта инструкция рассчитана на Linux VPS, каталог приложения `/opt/aimash` и Docker Compose v2.
Секреты не вставляйте в команды, тикеты и логи. `.env` и release-архивы должны быть доступны только
администратору.

## 1. Первичный деплой

На сервере должны быть установлены Git, Docker Engine, Compose v2, Bash и OpenSSL.

```bash
git clone <repository-url> /opt/aimash
cd /opt/aimash
bash scripts/generate_secrets.sh
chmod 600 .env
```

Скрипт атомарно создаёт `.env` из `.env.example`, генерирует новые
`SECRETS_ENCRYPTION_KEY`, `PSEUDONYMIZATION_HMAC_KEY` и `AIMASH_TRUST_HMAC_KEY` и отказывается
перезаписывать существующий `.env`. Ротация `SECRETS_ENCRYPTION_KEY` без миграции сделает уже
зашифрованные OAuth-токены в БД нечитаемыми.

Откройте `.env` локальным редактором на VPS и заполните как минимум:

- `ENV=prod`;
- `POSTGRES_PASSWORD` и `POSTGRES_RO_PASSWORD` разными сильными паролями;
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WHITELIST_CHAT_IDS`, `ADMIN_CHAT_IDS`;
- `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`,
  `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`;
- `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` — пусто оставляет мутации закрытыми; `all` разрешает их для
  всего видимого потолка и требует отдельного осознанного решения;
- ключ модели/провайдера, используемого Hermes.

Не включайте `HERMES_WRITE_ENABLED=true`, пока trusted gateway plugin не установлен и тот же
`AIMASH_TRUST_HMAC_KEY` не задан signer-процессу Hermes.

Проверьте конфигурацию и поднимите сервисы:

```bash
cd /opt/aimash
docker compose config --quiet
export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose up -d --build
docker compose ps
```

Нормальное состояние: `postgres`, `scheduler` и `backup` работают, `migrate` завершён с кодом 0.
`mcp` не является постоянным контейнером: Hermes Gateway запускает его по stdio. Установка и
проверка gateway описаны в `deploy/hermes/README.md` и `deploy/hermes/OPERATIONS.md`.

### Очистка перед передачей тестовой БД

Выполняйте только до начала production-работы, в maintenance window и после резервной копии.
Сначала остановите writer-процессы, затем сделайте dry-run и проверьте количества строк:

```bash
cd /opt/aimash
hermes gateway stop
docker compose stop scheduler
bash scripts/create_release_backup.sh
docker compose --profile mcp run --rm --no-deps -T mcp python scripts/prepare_prod_db.py
docker compose --profile mcp run --rm --no-deps -T mcp python scripts/prepare_prod_db.py --confirm
docker compose up -d scheduler
hermes gateway start
```

Скрипт удаляет только `proposals`, `audit_log`, `client_profiles`, `client_site_pages` и
`client_dossiers` в одной транзакции. Он не использует `TRUNCATE CASCADE`, сохраняет
`alembic_version` и все таблицы вне allowlist, а при внешнем FK на удаляемые таблицы завершится с
ошибкой без частичной очистки.

## 2. Добавление пользователей

Telegram ID — целое число, не username. Получите ID сотрудника безопасным внутренним способом и
добавьте его в CSV без кавычек:

```dotenv
TELEGRAM_WHITELIST_CHAT_IDS=12345678,87654321
ADMIN_CHAT_IDS=12345678
```

`TELEGRAM_WHITELIST_CHAT_IDS` обязателен и fail-closed: пустое значение блокирует всех.
`ADMIN_CHAT_IDS` даёт административные команды, поэтому администратор также должен находиться в
whitelist. После правки перечитайте окружение:

```bash
cd /opt/aimash
docker compose up -d --force-recreate scheduler
hermes gateway restart
```

Если включён `ACCOUNT_ACCESS_MODE=enforced`, одной записи в whitelist недостаточно: выдайте
сотруднику разрешения на нужные Google Ads accounts через штатные admin-команды.

## 3. Обновление Google Ads Refresh Token

Признаки проблемы: `invalid_grant`, ошибки OAuth в `docker compose logs scheduler` или отказ
`scripts/check_access.py`. Сначала убедитесь, что OAuth consent screen переведён в `In production`:
токены приложения в режиме `Testing` могут истекать через 7 дней.

На VPS в Python-окружении проекта запустите интерактивный OAuth flow:

```bash
cd /opt/aimash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/get_refresh_token.py --remote
```

Откройте выданную ссылку в браузере владельца Google Ads, разрешите доступ и верните скрипту полный
loopback URL из адресной строки. Скрипт обновит `GOOGLE_ADS_REFRESH_TOKEN` в `.env` и не напечатает
сам токен. Затем примените секрет и проверьте доступ:

```bash
cd /opt/aimash
docker compose up -d --force-recreate scheduler
hermes gateway restart
docker compose --profile mcp run --rm --no-deps -T mcp python scripts/check_access.py
```

Для per-account токенов из зашифрованной таблицы `oauth_tokens` используйте
`scripts/register_account.py`; не правьте шифротекст SQL-командой.

## 4. Резервное копирование

`create_release_backup.sh` требует работающий PostgreSQL и чистое Git-дерево. Он создаёт custom
format `pg_dump`, проверяет его через `pg_restore --list` и атомарно собирает release-архив:

```bash
cd /opt/aimash
git status --short
bash scripts/create_release_backup.sh
ls -lh backups/release_v3.0.0_*.tar.gz
```

Архив имеет режим `0600`, но может содержать production data и `.env`. Скопируйте его в
зашифрованное off-site хранилище и ограничьте срок хранения. Локальный `backup` sidecar ежедневно
создаёт DB dump, но локальная копия на том же VPS не защищает от потери сервера.

## 5. Outcome Checker и операционные метрики

В Compose scheduler работает с `TZ=UTC`. По умолчанию `OUTCOME_CHECK_SCHEDULE=0 10 * * *`, то есть
ежедневно в **10:00 UTC**. В проекте нет отдельного Prometheus endpoint; доказательство запуска —
структурный лог scheduler и состояние outcome в PostgreSQL.

Проверка за последние 26 часов:

```bash
cd /opt/aimash
docker compose ps scheduler
docker compose logs --since 26h scheduler | grep -E 'scheduler outcome checker|outcome checker|ERROR|Traceback'
docker compose exec -T postgres psql -U aimash -d aimash -c "SELECT outcome_state, count(*) FROM proposals GROUP BY outcome_state ORDER BY outcome_state;"
docker compose exec -T postgres psql -U aimash -d aimash -c "SELECT created_at, request_id, where, exc_type FROM error_events WHERE created_at >= now() - interval '26 hours' AND where LIKE 'scheduler:%outcome%' ORDER BY created_at DESC;"
```

Успешный no-op также пишет строку вида:

```text
scheduler outcome checker: {'claimed': 0, 'delivered': 0, 'retrying': 0, 'failed': 0}
```

Для запуска с задачами ожидайте `failed: 0` и `retrying: 0`; `delivered` должно соответствовать
числу реально отправленных результатов. `outcome_state=failed` или строки в `error_events` требуют
разбора до сдачи. Сырые секреты в тикет не копируйте; для корреляции используйте `request_id`.

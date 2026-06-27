# БД и миграции

ORM — SQLAlchemy 2.0 ([`db/models.py`](../db/models.py)); схему в проде ведёт **Alembic**
([`migrations/`](../migrations/)). Dev/тесты могут работать на SQLite; прод — Postgres 16.

## Схема (5 таблиц)

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `whitelist` | Telegram allow-list (кому разрешён бот) | `chat_id` (unique), `note` |
| `user_settings` | расписание отчётов, пороги алертов, переопределение модели | `chat_id` (unique), `report_schedule` (cron), `alert_thresholds` (JSON), `model_override` |
| `proposals` | очередь черновиков изменений (diff «было→станет») | `confirmation_id` (unique), `operation`, `customer_id`, `summary`, `params` (JSON), `user_initiated`, `status` |
| `audit_log` | журнал всех операций (кто/когда/что/результат) | `confirmation_id`, `operation`, `customer_id`, `chat_id`, `status`, `result` (JSON) |
| `oauth_tokens` | refresh-токены, **зашифрованные at-rest** | `account` (unique), `refresh_token_enc` |

### Где секреты
Refresh-токены хранятся **только** зашифрованными (`oauth_tokens.refresh_token_enc`,
`core.secrets.encrypt`). В `proposals.params` и `audit_log.result` секретов **нет** by design —
туда идут структурированные (Pydantic-валидированные) параметры и результат операции.

### Жизненный цикл proposal
`status`: `pending → confirmed → executing → applied` (или `failed` / `rejected`).
- `user_initiated` дефолтит в **`False`** (fail-closed): только доверенный вход — Telegram-команда
  человека — ставит `True`. Автосоздатель (scheduler/anomaly), забывший флаг, получит `False`, и
  бюджет/ставка будут заблокированы гейтом (golden rule #3). Дефолт `True` был бы fail-open.
- Подтверждение «тратится» атомарно один раз (`ConfirmStore.claim`) — см. [SECURITY.md](SECURITY.md).
Истёкшие `pending`-черновики подчищает плановая задача (см. [SCHEDULER.md](SCHEDULER.md)).

## Миграции (Alembic)

```bash
docker compose up -d postgres            # dev-Postgres (хост-порт 5433)
alembic upgrade head                     # применить все миграции
alembic downgrade -1                     # откатить последнюю
alembic current                          # текущая ревизия
alembic history                          # список ревизий
```

### Добавить миграцию
```bash
alembic revision --autogenerate -m "описание изменения"
```
1. `--autogenerate` сравнивает модели с БД и набрасывает diff.
2. **Обязательно вычитай** сгенерированный файл в `migrations/versions/` — autogenerate не ловит
   всё (переименования, изменения типов, данные); поправь руками `upgrade()`/`downgrade()`.
3. Проверь применение **и откат** на dev-Postgres (`upgrade head` → `downgrade -1` → `upgrade head`).
4. Файлы версий лежат в [`migrations/versions/`](../migrations/versions/); конфиг — `alembic.ini`,
   `migrations/env.py` (берёт `database_url` из `core.config`).

### dev/SQLite vs prod/Postgres
- `db.session.init_db()` (`create_all`) — **только** для dev/SQLite и тестов; в проде схему ведёт
  **исключительно** Alembic. Не полагайся на `create_all` на Postgres.
- **`func.now()` / timezone.** `server_default=func.now()` транслируется по-разному: SQLite отдаёт
  UTC без tz-метки, Postgres — `CURRENT_TIMESTAMP` с tz. Учитывай это при переезде dev→prod и в
  tz-чувствительных выборках (сейчас даты используются только для аудита/очистки, поэтому риск
  низкий, но при добавлении tz-логики — проверь на Postgres).

## Резервные копии
Прод-чеклист требует настроенные бэкапы БД (см. [DEPLOYMENT.md §Prod-чеклист](DEPLOYMENT.md)).
`audit_log` — источник истины по выполненным операциям; не очищать без бэкапа.

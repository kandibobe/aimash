# Бэкап и восстановление БД (прод)

`audit_log` — это **денежный реестр** (кто/что/когда менял аккаунты). `oauth_tokens` хранит
зашифрованные refresh-токены. Потеря БД = потеря истории изменений и доступов. Бэкап обязателен
перед выходом на боевые аккаунты (см. [DEPLOYMENT.md](DEPLOYMENT.md) прод-чеклист).

> ⚠️ **Ключ шифрования бэкапить ОТДЕЛЬНО.** `oauth_tokens.refresh_token_enc` расшифровывается
> ТОЛЬКО тем же `SECRETS_ENCRYPTION_KEY` (Fernet). Восстановление дампа БЕЗ этого ключа = токены
> мёртвые (придётся перерегистрировать аккаунты через `scripts/register_account.py`). Храни ключ
> в менеджере секретов/офлайн, НЕ рядом с дампами.

## Что бэкапим
PostgreSQL целиком (`postgres:16` из `docker-compose.yml`, БД `aimash`). Формат `-Fc` (custom,
сжатый, выборочный restore).

## Ручной бэкап (на VPS, /opt/aimash)
```bash
docker exec aimash-pg pg_dump -U aimash -Fc aimash > "/opt/aimash/backups/aimash_$(date +%F_%H%M).dump"
# Ротация: оставить последние 14 дней
find /opt/aimash/backups -name 'aimash_*.dump' -mtime +14 -delete
```
> Имя контейнера БД сверь: `docker compose ps` (в этом проекте сервис называется `postgres`;
> `aimash-pg` — пример `container_name`). Папку `/opt/aimash/backups` создай заранее (`mkdir -p`).

## Автобэкап cron (host)
```cron
# каждый день в 03:30 — дамп + ротация 14 дней
30 3 * * * cd /opt/aimash && docker exec aimash-pg pg_dump -U aimash -Fc aimash > "backups/aimash_$(date +\%F).dump" && find backups -name 'aimash_*.dump' -mtime +14 -delete
```

## Альтернатива: backup-sidecar в compose
Если предпочесть самодостаточность (без host-cron) — добавить сервис (НЕ коммичу в compose
автоматически, чтобы не конфликтовать с прод-харднингом; вставить при согласовании):
```yaml
  pg-backup:
    image: postgres:16
    depends_on: { postgres: { condition: service_healthy } }
    environment: { PGPASSWORD: aimash }
    volumes: [ "./backups:/backups" ]
    entrypoint: >
      sh -c 'while true; do
        pg_dump -h postgres -U aimash -Fc aimash > "/backups/aimash_$$(date +%F_%H%M).dump";
        find /backups -name "aimash_*.dump" -mtime +14 -delete;
        sleep 86400; done'
    restart: unless-stopped
```

## Восстановление (restore)
```bash
# 1) остановить бота (чтобы не писал во время restore); БД оставить поднятой
docker compose stop bot
# 2) восстановить дамп (--clean --if-exists перезатирает существующие объекты)
docker exec -i aimash-pg pg_restore -U aimash -d aimash --clean --if-exists < /opt/aimash/backups/aimash_YYYY-MM-DD.dump
# 3) поднять бота — entrypoint сам прогонит `alembic upgrade head` (доводит схему до кода)
docker compose up -d bot
```
Проверка после restore:
```bash
docker exec aimash-pg psql -U aimash -d aimash -c "SELECT status, count(*) FROM audit_log GROUP BY status;"
docker exec aimash-pg psql -U aimash -d aimash -c "SELECT max(created_at) FROM audit_log;"
```
Сверь, что последняя `created_at` соответствует ожидаемому моменту дампа, а распределение статусов
(`applied/failed/rejected/confirmed`) не выглядит усечённым.

## Миграции: один head (защита от two-heads)
Деплой падает (fail-fast в entrypoint), если в `migrations/versions/` два head'а (параллельные
ветки дали миграции с одним `down_revision`). Проверка перед деплоем:
```bash
python -m alembic heads   # должна быть РОВНО одна строка с «(head)»
```
Если head'ов два — слить: `python -m alembic merge -m "merge heads" <rev1> <rev2>`, затем
`alembic upgrade head`. Правило: каждая новая миграция `down_revision`-ит текущий единственный head.

## RPO / RTO (ориентир)
- **RPO** (сколько данных теряем): при суточном бэкапе — до 24 ч изменений. `audit_log` критичен →
  для боевых аккаунтов рассмотреть бэкап каждые 6 ч.
- **RTO** (сколько восстанавливаемся): `pg_restore` небольшого дампа + рестарт ≈ минуты.

## Ретеншн / архив
`audit_log` НЕ чистим вслепую (денежный реестр). Растёт ~линейно с числом операций; на ~10 аккаунтах
это тысячи строк/мес — Postgres держит легко. Индекс `(customer_id, created_at)` (план §6) ускоряет
пер-аккаунтные выборки. Архив строк старше ~18 мес — отдельной ручной выгрузкой в холодное хранилище
(дамп), а не `DELETE`.

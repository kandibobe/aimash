#!/bin/sh
# Бэкап Postgres Aimash (pg_dump custom-format -Fc) с ротацией. Для деплоя VPS + Docker Compose.
#
# audit_log — источник истины по выполненным операциям (ТЗ §12); без бэкапа его потеря необратима.
# Дамп -Fc (custom) — компактный и восстанавливается через pg_restore (параллельно, выборочно).
#
# Использование (вручную):   sh scripts/backup_db.sh
# В cron (ежедневно 03:30):   30 3 * * *  cd /opt/aimash && sh scripts/backup_db.sh >> /var/log/aimash-backup.log 2>&1
#
# Переменные (env, со значениями по умолчанию):
#   BACKUP_DIR     — куда складывать дампы           (по умолч. /var/backups/aimash)
#   BACKUP_RETAIN  — сколько последних дампов хранить (по умолч. 14)
#   PG_CONTAINER   — имя контейнера Postgres          (по умолч. aimash-pg)
#   POSTGRES_USER / POSTGRES_DB — роль/БД             (по умолч. aimash / aimash)
#
# ВОССТАНОВЛЕНИЕ из дампа:
#   docker exec -i "$PG_CONTAINER" pg_restore -U aimash -d aimash --clean --if-exists < aimash-YYYYMMDD-HHMMSS.dump
#
# ⚠️ Прод: дамп несёт ВСЕ данные (audit_log/proposals). Шифруй при выгрузке наружу (gpg/age) и
#    клади в внешнее хранилище (S3/Backblaze/rclone) — локальный том вместе с сервером не спасёт.
set -e

BACKUP_DIR="${BACKUP_DIR:-/var/backups/aimash}"
RETAIN="${BACKUP_RETAIN:-14}"
PG_CONTAINER="${PG_CONTAINER:-aimash-pg}"
PG_USER="${POSTGRES_USER:-aimash}"
PG_DB="${POSTGRES_DB:-aimash}"

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/aimash-$TS.dump"

echo "[backup] pg_dump $PG_DB → $OUT"
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$OUT"

# (опц.) шифрование и выгрузка наружу — раскомментируй и настрой под своё хранилище:
#   gpg --batch --yes --encrypt --recipient ops@example.com "$OUT" && rm -f "$OUT" && OUT="$OUT.gpg"
#   aws s3 cp "$OUT" "s3://my-bucket/aimash/"      # или: rclone copy "$OUT" remote:aimash/

# Ротация: оставить только последние $RETAIN дампов локально.
ls -1t "$BACKUP_DIR"/aimash-*.dump* 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f

echo "[backup] готово (хранится последних $RETAIN дампов в $BACKUP_DIR)."

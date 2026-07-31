#!/bin/sh
# Бэкап каталога Hermes (`/root/.hermes`) — ОТДЕЛЬНО от бэкапа Postgres.
#
# Почему отдельным скриптом, а не сайдкаром в docker-compose (как `backup` для Postgres):
#   • `/root/.hermes` живёт ВНЕ `/opt/aimash` — ровно поэтому он переживает `git reset --hard` и
#     пересоздание контейнеров, и ровно поэтому compose до него не дотягивается;
#   • примонтировать его в сайдкар — значит положить `.env` Hermes (OPENROUTER_API_KEY,
#     TELEGRAM_BOT_TOKEN) в `./backups` рядом с репозиторием, открытым текстом. Правило 5 это
#     запрещает: секреты не идут туда, где их видит кто-то кроме владельца хоста.
# Отсюда: host-скрипт + systemd-таймер (ранбук §8), а НЕ сервис Compose.
#
# Что внутри и почему это не «просто конфиг»:
#   • `.env` — секреты Hermes (потеря = перевыпуск токенов, утечка = чужой доступ к боту и модели);
#   • `state.db` — история топиков/сессий. Топик у нас = клиент, значит это переписка по клиентам
#     и фактическая история решений агента. В Postgres её нет: наш audit_log знает про выполненные
#     операции Google Ads, но не про диалог, который к ним привёл;
#   • `config.yaml`, `cron/jobs.json`, `plugins/`, `skills/` — рабочая конфигурация инстанса.
#
# Использование (вручную):  sh scripts/backup_hermes.sh
# Автоматически (systemd-таймер, ранбук §8) — предпочтительно: host-cron на этом VPS исторически
# «часто НЕ был запущен» (тот же провал, из-за которого бэкап Postgres переехал в сайдкар, C1).
#
# Переменные (env, со значениями по умолчанию):
#   HERMES_DIR            — каталог Hermes                   (по умолч. /root/.hermes)
#   HERMES_BACKUP_DIR     — куда складывать                  (по умолч. /root/hermes-backups)
#   HERMES_BACKUP_RETAIN  — сколько архивов хранить          (по умолч. 14)
#   HERMES_PYTHON         — Python с stdlib sqlite3           (по умолч. Hermes venv)
#
# ⚠️ Архив несёт СЕКРЕТЫ. Он создаётся с правами 600 и НЕ должен попадать в /opt/aimash (репозиторий),
#    в общие логи и в git. Выгрузка наружу — только шифрованной (gpg/age), см. хвост скрипта.
set -e

HERMES_DIR="${HERMES_DIR:-/root/.hermes}"
OUT_DIR="${HERMES_BACKUP_DIR:-/root/hermes-backups}"
RETAIN="${HERMES_BACKUP_RETAIN:-14}"
HERMES_PYTHON="${HERMES_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python}"

[ -d "$HERMES_DIR" ] || { echo "[hermes-backup] нет каталога $HERMES_DIR — Hermes здесь не установлен?" >&2; exit 1; }

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$OUT_DIR/hermes-$TS.tgz"
STAGE="$OUT_DIR/.stage-$TS"
# Staging несёт КОПИЮ `.env` открытым текстом. Убирать его только в конце нельзя: под `set -e` любой
# сбой на середине (нет места, tar упал) оставит секреты лежать на диске отдельным файлом. Trap
# срабатывает на любом выходе, включая ошибку и Ctrl-C.
trap 'rm -rf "$STAGE"' EXIT INT TERM

DB="$HERMES_DIR/state.db"
BASE=$(basename "$HERMES_DIR")

# Архив собирается из ОДНОГО staging-дерева, а не из двух `-C` в одном `tar`. Причина конкретная:
# `--exclude='state.db'` — глобальный фильтр, он вырезает файл и из второго `-C`, то есть выбрасывает
# ровно тот консистентный снимок, ради которого всё затевалось (проверено: в архиве оказывались
# `.env`/`config.yaml`, а `state.db` — ни живой, ни снимка).
mkdir -p "$STAGE/$BASE"
chmod 700 "$STAGE"
cp -a "$HERMES_DIR/." "$STAGE/$BASE/"
rm -f "$STAGE/$BASE/state.db" "$STAGE/$BASE/state.db-wal" "$STAGE/$BASE/state.db-shm"

# Консистентный снимок SQLite. Голый tar/cp по ЖИВОЙ базе может захватить её в середине транзакции:
# основной файл уже с новыми страницами, -wal ещё нет (или наоборот) → архив восстанавливается в
# повреждённую БД, и узнаётся это в день восстановления. `sqlite3 .backup` берёт снимок под
# блокировкой и отдаёт целый файл.
if [ -f "$DB" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$STAGE/$BASE/state.db'"
    echo "[hermes-backup] state.db — консистентный снимок (sqlite3 .backup)"
  elif [ -x "$HERMES_PYTHON" ]; then
    "$HERMES_PYTHON" - "$DB" "$STAGE/$BASE/state.db" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
    echo "[hermes-backup] state.db — консистентный снимок (Python sqlite3 backup)"
  else
    # Фолбэк: копируем базу ВМЕСТЕ с -wal/-shm — врозь они бесполезны. Целостность не гарантирована,
    # поэтому говорим об этом вслух, а не молчим: молчаливая деградация бэкапа хуже её отсутствия.
    # Каждая копия — своим `if`, а не `[ … ] && cp`: под `set -e` ложный тест в такой связке роняет
    # весь скрипт, и бэкап не делается вообще из-за отсутствия -wal.
    cp -p "$DB" "$STAGE/$BASE/state.db"
    if [ -f "$DB-wal" ]; then cp -p "$DB-wal" "$STAGE/$BASE/state.db-wal"; fi
    if [ -f "$DB-shm" ]; then cp -p "$DB-shm" "$STAGE/$BASE/state.db-shm"; fi
    echo "[hermes-backup] ⚠️ нет sqlite3 и $HERMES_PYTHON — снимок state.db БЕЗ гарантии целостности."
  fi
else
  echo "[hermes-backup] ⚠️ $DB не найден. Путь state.db на v0.17 подтверждён не был — проверь:"
  echo "[hermes-backup]    find $HERMES_DIR -name '*.db' -maxdepth 2"
fi

echo "[hermes-backup] tar $HERMES_DIR → $OUT"
tar czf "$OUT" -C "$STAGE" "$BASE"
chmod 600 "$OUT"

# (опц.) шифрование и выгрузка наружу — локальный том вместе с сервером не спасёт:
#   gpg --batch --yes --encrypt --recipient ops@example.com "$OUT" && rm -f "$OUT"
#   rclone copy "$OUT.gpg" remote:aimash-hermes/

ls -1t "$OUT_DIR"/hermes-*.tgz* 2>/dev/null | tail -n +$((RETAIN + 1)) | xargs -r rm -f

echo "[hermes-backup] готово: $OUT (хранится последних $RETAIN в $OUT_DIR)"

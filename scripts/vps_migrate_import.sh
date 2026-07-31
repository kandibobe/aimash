#!/bin/sh
# Восстановление состояния VPS из архива `vps_migrate_export.sh` на НОВОЙ машине
# (ранбук: deploy/hermes/OPERATIONS.md §16, шаг 6).
#
# Скрипт делает только МЕХАНИЧЕСКУЮ часть — ту, где забытый файл обнаруживается через неделю:
# .env, дамп Postgres, каталог Hermes, systemd-юниты, drop-in с MemoryMax, Caddyfile.
# Интерактивное и требующее решений (`tailscale up` с авторизацией в браузере, установка Hermes
# пиновой версии, правка GitHub-секрета VPS_SSH_HOST) остаётся в ранбуке и делается руками.
#
# ⛔ ГЛАВНАЯ ЗАЩИТА: скрипт ОТКАЗЫВАЕТСЯ работать на машине-источнике. `pg_restore --clean` на
#    живой боевой БД — необратимая потеря. Источник опознаётся по hostname из MANIFEST.txt;
#    осознанный откат на ту же машину — только явным `--allow-same-host`.
#
# ⛔ ВТОРАЯ ЗАЩИТА: ни бот, ни gateway НЕ поднимаются по умолчанию. Telegram-токен допускает
#    ровно один поллер: поднять здесь, пока старая машина ещё поллит, — это 409 Conflict и
#    потерянные сообщения. Подъём — отдельными флагами, после того как старый контур погашен.
#
# Использование (root, на новой машине, где уже: docker + git clone репозитория в /opt/aimash):
#   sh scripts/vps_migrate_import.sh --from /root/aimash-migration-<ts>.tgz
#   ... затем, убедившись что старая машина погашена:
#   sh scripts/vps_migrate_import.sh --from <тот же архив> --start-app --start-gateway
#
# Флаги:
#   --from <tgz>        архив экспорта (обязателен)
#   --force             перезаписать существующие .env / ~/.hermes (по умолчанию — отказ)
#   --start-app         поднять bot+scheduler+backup (занимает Telegram-токен!)
#   --start-gateway     запустить hermes gateway (тоже поллер Telegram)
#   --allow-same-host   разрешить восстановление на машину-источник (аварийный откат)
#   --skip-pg           не восстанавливать БД (когда переносили docker-том целиком, снапшот-клон)
set -e

FROM=""
FORCE=0; START_APP=0; START_GW=0; ALLOW_SAME=0; SKIP_PG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --from=*) FROM="${1#*=}"; shift ;;
    --force) FORCE=1; shift ;;
    --start-app) START_APP=1; shift ;;
    --start-gateway) START_GW=1; shift ;;
    --allow-same-host) ALLOW_SAME=1; shift ;;
    --skip-pg) SKIP_PG=1; shift ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) echo "[import] неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
done

APP_DIR="${APP_DIR:-/opt/aimash}"
HERMES_DIR="${HERMES_DIR:-/root/.hermes}"

[ -n "$FROM" ] || { echo "[import] нужен --from <архив>" >&2; exit 2; }
[ -f "$FROM" ] || { echo "[import] нет файла $FROM" >&2; exit 1; }
[ "$(id -u)" = "0" ] || { echo "[import] нужен root" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "[import] нет docker — сначала шаг 4 ранбука §16" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "[import] нет docker compose v2" >&2; exit 1; }
[ -d "$APP_DIR/.git" ] || { echo "[import] $APP_DIR не git-клон репозитория — сначала git clone (§16 шаг 4)" >&2; exit 1; }

STAGE=$(mktemp -d /root/.import-XXXXXX)
chmod 700 "$STAGE"
# В staging распаковываются оба `.env` открытым текстом — снести обязательно на ЛЮБОМ выходе.
trap 'rm -rf "$STAGE"' EXIT INT TERM

echo "[import] распаковка $FROM"
tar xzf "$FROM" -C "$STAGE"
# Экспорт кладёт содержимое либо в подкаталог aimash-migration-<ts>, либо (fallback без GNU tar)
# прямо в корень. Опознаём корень по MANIFEST.txt, а не по имени каталога.
MF=$(find "$STAGE" -maxdepth 2 -name MANIFEST.txt | head -1)
[ -n "$MF" ] || { echo "[import] в архиве нет MANIFEST.txt — это не архив vps_migrate_export.sh" >&2; exit 1; }
ROOT=$(dirname "$MF")

# ── 0. Защита от импорта на машину-источник ───────────────────────────────────────────────────
SRC_HOST=$(awk -F': *' '/^hostname:/{print $2; exit}' "$MF")
if [ "$SRC_HOST" = "$(hostname)" ] && [ "$ALLOW_SAME" != "1" ]; then
  echo "[import] ⛔ hostname совпадает с машиной-источником ($SRC_HOST)." >&2
  echo "         pg_restore --clean затрёт живую БД. Если это осознанный откат — --allow-same-host." >&2
  exit 1
fi

# ── 1. Целостность архива по sha256 из манифеста ──────────────────────────────────────────────
echo "[import] сверка контрольных сумм"
SUMS="$STAGE/.sums"
grep -E '^[0-9a-f]{64}  \./' "$MF" > "$SUMS" || true
if [ -s "$SUMS" ]; then
  ( cd "$ROOT" && sha256sum -c --quiet "$SUMS" ) || { echo "[import] ⛔ архив повреждён (sha256 не сходятся)" >&2; exit 1; }
  echo "[import] ✅ $(wc -l < "$SUMS") файлов сошлись"
else
  echo "[import] ⚠️  в манифесте нет сумм — целостность не проверена" >&2
fi
echo "[import] источник: $SRC_HOST, снят: $(awk '/^# Инвентарь/{print $NF}' "$MF" 2>/dev/null || echo '?')"

# ── 2. Секреты приложения ─────────────────────────────────────────────────────────────────────
if [ -f "$APP_DIR/.env" ] && [ "$FORCE" != "1" ]; then
  echo "[import] $APP_DIR/.env уже существует — отказ (перезапись только с --force)" >&2
  exit 1
fi
install -m 600 "$ROOT/opt-aimash/.env" "$APP_DIR/.env"
grep -qE '^SECRETS_ENCRYPTION_KEY=.+' "$APP_DIR/.env" \
  || { echo "[import] ⛔ в .env нет SECRETS_ENCRYPTION_KEY — oauth_tokens из дампа не расшифруются" >&2; exit 1; }
echo "[import] ✅ .env восстановлен (600), ключ шифрования на месте"

# ── 3. Образ собираем ДО подъёма — так окно даунтайма короче ───────────────────────────────────
GIT_SHA="$(git -C "$APP_DIR" rev-parse --short HEAD)"
export GIT_SHA
echo "[import] сборка образа aimash-bot (GIT_SHA=$GIT_SHA)"
( cd "$APP_DIR" && docker compose build )

# ── 4. Postgres + restore ─────────────────────────────────────────────────────────────────────
if [ "$SKIP_PG" = "1" ]; then
  echo "[import] --skip-pg: БД не восстанавливаем (том перенесён целиком)"
else
  echo "[import] подъём postgres"
  ( cd "$APP_DIR" && docker compose up -d postgres )
  # `up -d` возвращается сразу; restore в неготовую БД падает на середине. Ждём healthcheck.
  i=0
  while [ "$i" -lt 60 ]; do
    ST=$(docker inspect -f '{{.State.Health.Status}}' aimash-pg 2>/dev/null || echo starting)
    [ "$ST" = "healthy" ] && break
    i=$((i+1)); sleep 2
  done
  [ "$ST" = "healthy" ] || { echo "[import] ⛔ aimash-pg не стал healthy за 120с (state=$ST)" >&2; exit 1; }

  echo "[import] pg_restore"
  docker exec -i aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U aimash -d aimash --clean --if-exists' \
    < "$ROOT/pg/aimash.dump" || echo "[import] ⚠️  pg_restore вернул ненулевой код — типично для --clean на пустой БД (DROP ... не найдено). Проверяем результат ниже."
  # Настоящий критерий — не код возврата pg_restore, а содержимое БД: --clean на пустой базе
  # всегда шумит «does not exist», и по коду отличить это от реальной аварии нельзя.
  TBL=$(docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -qtAX -U aimash -d aimash -c "select count(*) from information_schema.tables where table_schema='"'"'public'"'"'"' 2>/dev/null || echo 0)
  REV=$(docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -qtAX -U aimash -d aimash -c "select version_num from alembic_version"' 2>/dev/null || echo "")
  EXP_REV=$(awk -F': *' '/^alembic head:/{print $2; exit}' "$MF")
  [ "${TBL:-0}" -gt 5 ] || { echo "[import] ⛔ после restore в public только $TBL таблиц — восстановление не удалось" >&2; exit 1; }
  echo "[import] ✅ таблиц: $TBL, alembic_version: ${REV:-нет} (в источнике: ${EXP_REV:-?})"
  if [ -n "$EXP_REV" ] && [ -n "$REV" ] && [ "$REV" != "$EXP_REV" ]; then
    echo "[import] ⚠️  ревизия схемы разошлась с источником — сверь миграции до подъёма бота" >&2
  fi
fi

# ── 5. Каталог Hermes ─────────────────────────────────────────────────────────────────────────
HB=$(ls -1 "$ROOT"/hermes/hermes-*.tgz 2>/dev/null | head -1 || true)
if [ -n "$HB" ]; then
  if [ -d "$HERMES_DIR" ] && [ "$FORCE" != "1" ]; then
    echo "[import] ⚠️  $HERMES_DIR уже есть — не трогаю (перезапись только с --force)" >&2
  else
    [ -d "$HERMES_DIR" ] && mv "$HERMES_DIR" "$HERMES_DIR.before-import-$(date -u +%Y%m%d-%H%M%S)"
    # Архив несёт каталог целиком (`.hermes/…`), поэтому разворачиваем в родителя.
    tar xzf "$HB" -C "$(dirname "$HERMES_DIR")"
    chmod 700 "$HERMES_DIR"
    [ -f "$HERMES_DIR/state.db" ] || { echo "[import] ⛔ после распаковки нет state.db — история сессий не восстановлена" >&2; exit 1; }
    echo "[import] ✅ $HERMES_DIR восстановлен (state.db, config.yaml, .env, skills/, cron/)"
  fi
else
  echo "[import] ⚠️  в архиве нет бэкапа Hermes — конфиг агента ставить по RB-1 (deploy/hermes/README.md)" >&2
fi

# ── 6. systemd: юниты, drop-ins (MemoryMax!), user-gateway ────────────────────────────────────
for U in "$ROOT"/systemd/system/*.service "$ROOT"/systemd/system/*.timer; do
  [ -f "$U" ] || continue
  install -m 644 "$U" "/etc/systemd/system/$(basename "$U")"
  echo "[import] юнит: $(basename "$U")"
done
# Drop-in с лимитами памяти — не косметика: без него дашборд снова тянет машину в oom-kill (§14.1).
if [ -d "$ROOT/systemd/dropins" ]; then
  mkdir -p /etc/systemd/system.control
  for D in "$ROOT"/systemd/dropins/*.d; do
    [ -d "$D" ] || continue
    cp -a "$D" /etc/systemd/system.control/
    echo "[import] drop-in: $(basename "$D") → /etc/systemd/system.control"
  done
fi
for UU in "$ROOT"/systemd/user/*.service; do
  [ -f "$UU" ] || continue
  mkdir -p /root/.config/systemd/user
  install -m 644 "$UU" "/root/.config/systemd/user/$(basename "$UU")"
  echo "[import] user-юнит: $(basename "$UU")"
done
systemctl daemon-reload
# linger — условие выживания user-gateway после logout (§2). Без него «поставили и работает» ровно
# до конца SSH-сессии.
loginctl enable-linger root || echo "[import] ⚠️  enable-linger не отработал — gateway умрёт при logout" >&2

# ── 7. Caddy: конфиг кладём, бинарь не подсовываем ────────────────────────────────────────────
if [ -f "$ROOT/caddy/Caddyfile" ]; then
  mkdir -p /etc/caddy
  install -m 644 "$ROOT/caddy/Caddyfile" /etc/caddy/Caddyfile
  echo "[import] ✅ /etc/caddy/Caddyfile"
  if ! command -v caddy >/dev/null 2>&1 && [ ! -x /usr/local/bin/caddy ]; then
    echo "[import] ⚠️  бинаря caddy нет — поставь ту же версию, что в MANIFEST (строка 'caddy binary')" >&2
  fi
fi

# ── 8. Подъём — только по явному флагу (Telegram-токен = один поллер) ─────────────────────────
if [ "$START_APP" = "1" ]; then
  echo "[import] подъём приложения (bot/scheduler/backup)"
  ( cd "$APP_DIR" && docker compose up -d )
  systemctl enable --now hermes-dashboard.service 2>/dev/null || echo "[import] ⚠️  hermes-dashboard не запустился" >&2
  systemctl enable --now hermes-dash-proxy.service 2>/dev/null || echo "[import] ⚠️  hermes-dash-proxy не запустился" >&2
  systemctl enable --now hermes-backup.timer 2>/dev/null || true
else
  echo "[import] ⏸  bot/scheduler НЕ поднимаю (--start-app). Сначала погаси старую машину:"
  echo "           на СТАРОМ VPS: cd /opt/aimash && docker compose down && hermes gateway stop"
  echo "           там же: systemctl disable --now hermes-dashboard hermes-dash-proxy (чтобы не ожили после ребута)"
fi

if [ "$START_GW" = "1" ]; then
  if command -v hermes >/dev/null 2>&1; then
    hermes gateway install 2>/dev/null || true
    hermes gateway start
    echo "[import] ✅ gateway запущен"
  else
    echo "[import] ⛔ hermes не установлен — RB-1 (deploy/hermes/README.md), пин из PIN.json" >&2
    exit 1
  fi
else
  echo "[import] ⏸  gateway НЕ запускаю (--start-gateway)"
fi

echo
echo "[import] Осталось руками (§16 шаги 7–9): tailscale up --hostname hermes-vps → tailscale serve --bg 9120,"
echo "         GitHub secret VPS_SSH_HOST → новый IP, затем проверка:"
echo "           sh $APP_DIR/scripts/vps_migrate_verify.sh"

#!/bin/sh
# Снятие ПОЛНОГО состояния боевого VPS одним архивом — для переезда на другую машину
# (ранбук: deploy/hermes/OPERATIONS.md §16). Пара к `vps_migrate_import.sh`.
#
# Зачем отдельный скрипт, если есть два бэкапа. `backup`-сайдкар Compose даёт дамп Postgres,
# `scripts/backup_hermes.sh` — каталог Hermes. Ни один из них не знает про третью группу, без
# которой машина не воспроизводится: `/opt/aimash/.env` (в нём `SECRETS_ENCRYPTION_KEY` — без него
# `oauth_tokens` из дампа не расшифруются, docs/BACKUP.md), systemd-юниты дашборда/прокси/таймера,
# drop-in с `MemoryMax` (§14.1), `/etc/caddy/Caddyfile` (§14) и конфиг `tailscale serve`. Переезд
# ломается именно на них: БД восстановили, а пульт не поднимается и никто не помнит почему.
#
# Что собирается (см. MANIFEST.txt внутри архива):
#   pg/aimash.dump           свежий pg_dump -Fc (НЕ суточный из ./backups — тот отстаёт до 24 ч)
#   opt-aimash/.env          секреты приложения, включая SECRETS_ENCRYPTION_KEY
#   hermes/hermes-<ts>.tgz   результат backup_hermes.sh (консистентный state.db через sqlite3 .backup)
#   systemd/                 системные юниты + drop-ins + user-юнит gateway
#   caddy/Caddyfile          Host-rewrite прокси дашборда
#   tailscale/               serve status + node status (воссоздать цепочку §14)
#   MANIFEST.txt             инвентарь машины + sha256 каждого файла архива
#
# ⚠️ АРХИВ ЦЕЛИКОМ — СЕКРЕТ (правило 5): два `.env` открытым текстом. Права 600, каталог 700,
#    кладётся в /root/vps-migration и НИКОГДА в /opt/aimash (это git-репозиторий) — скрипт
#    отказывается писать внутрь репозитория. Вывоз с машины — только шифрованным (gpg/age).
#
# Использование:
#   sh scripts/vps_migrate_export.sh                     # репетиция: горячий снимок, прод работает
#   sh scripts/vps_migrate_export.sh --cutover           # окно переезда: гасит прод, потом снимает
#   sh scripts/vps_migrate_export.sh --with-backup-history   # + история дампов ./backups
#
# `--cutover` — не оптимизация, а требование корректности. Telegram-поллер один на токен: пока
# старый `aimash-bot`/gateway живы, поднимать их на новой машине нельзя (409 Conflict, сообщения
# теряются). Поэтому финальный снимок снимается ПОСЛЕ остановки прода и прод остаётся погашенным.
set -e

CUTOVER=0
WITH_HISTORY=0
for arg in "$@"; do
  case "$arg" in
    --cutover) CUTOVER=1 ;;
    --with-backup-history) WITH_HISTORY=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "[export] неизвестный аргумент: $arg" >&2; exit 2 ;;
  esac
done

APP_DIR="${APP_DIR:-/opt/aimash}"
HERMES_DIR="${HERMES_DIR:-/root/.hermes}"
OUT_DIR="${MIGRATE_OUT_DIR:-/root/vps-migration}"

[ "$(id -u)" = "0" ] || { echo "[export] нужен root: юниты в /etc/systemd, /root/.hermes, docker" >&2; exit 1; }
[ -d "$APP_DIR" ] || { echo "[export] нет $APP_DIR" >&2; exit 1; }
[ -f "$APP_DIR/.env" ] || { echo "[export] нет $APP_DIR/.env — без него SECRETS_ENCRYPTION_KEY потеряется" >&2; exit 1; }

# Правило 5 в исполнении, а не на словах: архив с секретами не имеет права оказаться в дереве git.
case "$OUT_DIR" in
  "$APP_DIR"|"$APP_DIR"/*) echo "[export] OUT_DIR внутри репозитория ($APP_DIR) — отказ (правило 5)" >&2; exit 1 ;;
esac

TS=$(date -u +%Y%m%d-%H%M%S)
# Имя staging-каталога = имя корня внутри архива: так упаковка обходится без `--transform`
# (его синтаксис зависит от того, пишет ли tar пути с `./`, и молча не срабатывает при промахе).
NAME="aimash-migration-$TS"
STAGE="$OUT_DIR/$NAME"
OUT="$OUT_DIR/$NAME.tgz"
mkdir -p "$OUT_DIR"; chmod 700 "$OUT_DIR"
# Staging несёт `.env` открытым текстом. Под `set -e` любой сбой на середине (нет места, tar упал)
# оставил бы его лежать отдельным файлом — trap срабатывает на любом выходе, включая Ctrl-C.
trap 'rm -rf "$STAGE"' EXIT INT TERM
mkdir -p "$STAGE"; chmod 700 "$STAGE"
mkdir -p "$STAGE/pg" "$STAGE/opt-aimash" "$STAGE/hermes" "$STAGE/systemd/system" "$STAGE/systemd/dropins" "$STAGE/systemd/user" "$STAGE/caddy" "$STAGE/tailscale" "$STAGE/inventory"

M="$STAGE/MANIFEST.txt"
say() { echo "$@" | tee -a "$M"; }
warn() { echo "[export] ⚠️  $*" | tee -a "$M" >&2; }

{
  echo "# Инвентарь боевого VPS, снят $TS UTC"
  echo "# Скрипт: scripts/vps_migrate_export.sh; ранбук восстановления: OPERATIONS.md §16"
  echo
} > "$M"

# ── 1. Инвентарь машины (то, что придётся воспроизвести руками) ───────────────────────────────
say "== host =="
say "hostname: $(hostname)"
say "kernel:   $(uname -srm)"
say "arch:     $(uname -m)   # x86_64 ⇒ снапшот/rescale только в x86-планы (Arm64 CAX запрещён)"
say "cpu:      $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//' || echo '?')"
say "ram:      $(free -h | awk '/^Mem:/{print $2" total, "$7" available"}')"
say "swap:     $(free -h | awk '/^Swap:/{print $2}')"
say "disk:     $(df -h / | awk 'NR==2{print $2" total, "$3" used ("$5")"}')"
say "docker:   $(docker --version 2>/dev/null || echo 'НЕТ')"
say "compose:  $(docker compose version --short 2>/dev/null || echo 'НЕТ')"
say ""
say "== hermes (пин версии обязателен при установке на новой машине) =="
if command -v hermes >/dev/null 2>&1; then
  say "$(hermes version 2>&1 | head -3)"
else
  warn "hermes не в PATH — сними версию/коммит вручную, установка на новой машине идёт с пином"
fi
say "PIN.json ожидает: $(grep -E '\"(release|ref)\"' "$APP_DIR/deploy/hermes/PIN.json" 2>/dev/null | tr -d ' \n' || echo '?')"
say ""
say "== git =="
say "HEAD: $(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo '?') ($(git -C "$APP_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?'))"
say ""
say "== контейнеры =="
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null | tee -a "$M" >/dev/null || warn "docker ps не отработал"
say ""
say "== слушатели (барьер §14: 9119/9120/5433 — только loopback, :443 — только tailnet) =="
ss -ltnp 2>/dev/null | tee -a "$M" >/dev/null || warn "ss недоступен"
say ""
say "== systemd (hermes/caddy/tailscale) =="
systemctl list-unit-files 2>/dev/null | grep -E 'hermes|caddy|tailscale' | tee -a "$M" >/dev/null || true
XDG_RUNTIME_DIR=/run/user/0 systemctl --user list-unit-files 2>/dev/null | grep -E 'hermes' | tee -a "$M" >/dev/null || warn "user-юниты не перечислены (нужна user-шина, §0)"
say "linger: $(loginctl show-user root -p Linger 2>/dev/null || echo '?')   # без linger user-gateway умирает при logout (§2)"
say ""
say "== apt: пакеты, поставленные руками =="
apt-mark showmanual 2>/dev/null > "$STAGE/inventory/apt-manual.txt" || warn "apt-mark недоступен"
say "→ inventory/apt-manual.txt ($(wc -l < "$STAGE/inventory/apt-manual.txt" 2>/dev/null || echo 0) строк)"
say ""

# ── 2. Cutover: погасить прод ДО дампа ────────────────────────────────────────────────────────
# Порядок: gateway → контейнеры. Иначе живой gateway дёргает MCP (`docker exec aimash-bot`) в
# момент, когда контейнер уходит, и в логах остаётся ложная авария транспорта.
if [ "$CUTOVER" = "1" ]; then
  say "== cutover =="
  if command -v hermes >/dev/null 2>&1; then
    hermes gateway stop 2>&1 | tail -2 | tee -a "$M" >/dev/null || warn "hermes gateway stop не отработал — проверь вручную"
  fi
  ( cd "$APP_DIR" && docker compose stop bot scheduler ) 2>&1 | tail -3 | tee -a "$M" >/dev/null
  say "прод погашен: bot/scheduler stopped, gateway stopped (postgres оставлен для дампа)"
  say "⛔ НЕ поднимай их обратно, если новая машина уже поллит Telegram — один токен = один поллер"
  say ""
fi

# ── 3. Postgres: свежий дамп + проверка формата ───────────────────────────────────────────────
# Пароль берём из env самого контейнера — он не появляется ни в командной строке, ни в логе.
say "== postgres =="
docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U aimash -d aimash' > "$STAGE/pg/aimash.dump"
# «Файл создался» ≠ «дамп годен»: при сбое внутри контейнера stdout бывает пустым или обрезанным.
# Custom-формат начинается с магии PGDMP — дешёвая проверка без pg_restore на хосте.
[ -s "$STAGE/pg/aimash.dump" ] || { echo "[export] pg_dump дал пустой файл" >&2; exit 1; }
head -c 5 "$STAGE/pg/aimash.dump" | grep -q PGDMP || { echo "[export] дамп не в формате -Fc (нет магии PGDMP)" >&2; exit 1; }
say "pg/aimash.dump: $(du -h "$STAGE/pg/aimash.dump" | cut -f1), формат -Fc подтверждён"
say "таблиц в public (живая БД, ориентир для проверки после restore): $(docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -qtAX -U aimash -d aimash -c "select count(*) from information_schema.tables where table_schema='"'"'public'"'"'"' 2>/dev/null || echo '?')"
say "alembic head:   $(docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -qtAX -U aimash -d aimash -c "select version_num from alembic_version"' 2>/dev/null || echo '?')"
say ""

if [ "$WITH_HISTORY" = "1" ] && [ -d "$APP_DIR/backups" ]; then
  mkdir -p "$STAGE/pg/history"
  cp -a "$APP_DIR/backups/." "$STAGE/pg/history/" 2>/dev/null || warn "часть ./backups не скопировалась"
  say "pg/history: $(ls -1 "$STAGE/pg/history" 2>/dev/null | wc -l) файлов ($(du -sh "$STAGE/pg/history" | cut -f1))"
fi

# ── 4. Секреты приложения ─────────────────────────────────────────────────────────────────────
cp -a "$APP_DIR/.env" "$STAGE/opt-aimash/.env"
chmod 600 "$STAGE/opt-aimash/.env"
say "== .env приложения =="
# Печатаем НАЛИЧИЕ ключей, не значения (правило 5). Отсутствие ключа шифрования — стоп-сигнал:
# дамп без него бесполезен в части oauth_tokens, аккаунты придётся регистрировать заново.
for K in SECRETS_ENCRYPTION_KEY POSTGRES_PASSWORD POSTGRES_RO_PASSWORD TELEGRAM_BOT_TOKEN GOOGLE_ADS_DEVELOPER_TOKEN OPENROUTER_API_KEY; do
  if grep -qE "^${K}=.+" "$APP_DIR/.env"; then say "  $K: есть"; else warn "  $K: НЕТ в .env — проверь, ожидаемо ли это"; fi
done
say ""

# ── 5. Каталог Hermes (переиспользуем штатный бэкап — там консистентный state.db) ─────────────
say "== hermes dir =="
if [ -d "$HERMES_DIR" ]; then
  HERMES_BACKUP_DIR="$STAGE/hermes" HERMES_BACKUP_RETAIN=1 sh "$APP_DIR/scripts/backup_hermes.sh" 2>&1 | tail -4 | tee -a "$M" >/dev/null
  HB=$(ls -1t "$STAGE/hermes"/hermes-*.tgz 2>/dev/null | head -1 || true)
  [ -n "$HB" ] || { echo "[export] backup_hermes.sh не создал архив" >&2; exit 1; }
  # Тот же контроль, что в §8: архив без state.db/.env — папка с файлами, а не бэкап.
  tar tzf "$HB" | grep -q 'state\.db' || { echo "[export] в архиве Hermes нет state.db — история сессий потеряется" >&2; exit 1; }
  tar tzf "$HB" | grep -q '\.env'     || warn "в архиве Hermes нет .env (OPENROUTER_API_KEY/TELEGRAM_BOT_TOKEN) — проверь"
  say "hermes/$(basename "$HB"): $(du -h "$HB" | cut -f1); state.db внутри подтверждён"
else
  warn "нет $HERMES_DIR — Hermes на этой машине не установлен?"
fi
say ""

# ── 6. systemd-юниты, drop-ins, Caddy, Tailscale ──────────────────────────────────────────────
say "== юниты и конфиги пульта (§14) =="
for U in hermes-dashboard.service hermes-dash-proxy.service hermes-backup.service hermes-backup.timer caddy.service; do
  [ -f "/etc/systemd/system/$U" ] && { cp -a "/etc/systemd/system/$U" "$STAGE/systemd/system/"; say "  system/$U"; }
done
# Drop-in с MemoryMax живёт НЕ рядом с юнитом: `systemctl set-property` пишет в system.control,
# и при чистой установке этот каталог никто не воссоздаёт — машина снова ловит oom-kill (§14.1).
for D in /etc/systemd/system.control/hermes-*.d /etc/systemd/system/hermes-*.d; do
  [ -d "$D" ] && { cp -a "$D" "$STAGE/systemd/dropins/"; say "  dropin/$(basename "$D")"; }
done
for UU in /root/.config/systemd/user/hermes-*.service; do
  [ -f "$UU" ] && { cp -a "$UU" "$STAGE/systemd/user/"; say "  user/$(basename "$UU")"; }
done
[ -f /etc/caddy/Caddyfile ] && { cp -a /etc/caddy/Caddyfile "$STAGE/caddy/"; say "  caddy/Caddyfile"; }
if command -v caddy >/dev/null 2>&1; then say "  caddy binary: $(caddy version 2>&1 | head -1) (бинарь НЕ кладём в архив — ставится заново той же версией)"; fi
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve status > "$STAGE/tailscale/serve-status.txt" 2>&1 || true
  tailscale status > "$STAGE/tailscale/node-status.txt" 2>&1 || true
  tailscale funnel status > "$STAGE/tailscale/funnel-status.txt" 2>&1 || true
  say "  tailscale/: serve+node+funnel status (funnel обязан быть выключен — К3)"
  say "  tailnet-имя узла: $(tailscale status --json 2>/dev/null | grep -m1 '\"DNSName\"' | cut -d'\"' -f4 || echo '?')"
  say "  ⚠️ URL дашборда завязан на ЭТО имя: чтобы он не сменился, старый узел удаляется из tailnet"
  say "     ДО подъёма нового (§16, шаг 7) — иначе новый получит суффикс -1 и ссылка изменится"
else
  warn "нет tailscale — цепочку §14 воссоздавать с нуля"
fi
say ""

# ── 7. Упаковка + контрольные суммы ───────────────────────────────────────────────────────────
( cd "$STAGE" && find . -type f ! -name MANIFEST.txt -exec sha256sum {} \; | sort -k2 ) >> "$M"
tar czf "$OUT" -C "$OUT_DIR" "$NAME"
chmod 600 "$OUT"

echo
echo "[export] ✅ архив: $OUT ($(du -h "$OUT" | cut -f1), права 600)"
echo "[export] sha256: $(sha256sum "$OUT" | cut -d' ' -f1)"
echo
echo "[export] ⚠️ Внутри — секреты открытым текстом. Вывоз ТОЛЬКО шифрованным, например:"
echo "         gpg -c --cipher-algo AES256 $OUT     # → $OUT.gpg, дальше scp именно .gpg"
echo "[export] Дальше: OPERATIONS.md §16 шаг 5 (перенос) → vps_migrate_import.sh на новой машине."

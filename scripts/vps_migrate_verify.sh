#!/bin/sh
# Пост-проверка машины: после переезда (§16), после rescale (§15) и вообще после любого ребута VPS.
#
# Зачем скрипт, а не чеклист в доке: чеклист из десяти команд человек проходит глазами и на седьмой
# строке «выглядит нормально» — тогда как половина пунктов здесь молчаливые. Дашборд, убитый
# oom-kill, отдаёт 502 через живые serve+Caddy (§14.1); scheduler в крэш-лупе бывает `running` в
# каждый момент замера (растёт только RestartCount); порт, вылезший на 0.0.0.0 вместо loopback,
# ничего не ломает — он просто открывает пульт наружу (К3). Такие вещи ловит exit-код, не взгляд.
#
# FAIL — граница безопасности или неработающий контур: exit 1.
# WARN — то, что бывает погашено осознанно (gateway до выпуска WRITE, история дампов).
#
# Использование:  sh scripts/vps_migrate_verify.sh [--deep]
#   --deep            + round-trip MCP через Hermes (`hermes mcp test aimash`) — медленнее
#   EXPECT_RAM_GB=16  ожидаемая память машины (дефолт 8: цель апгрейда из §15)
#
# ⚠️ Здесь СОЗНАТЕЛЬНО нет `set -e` (в отличие от export/import, где он — часть fail-closed):
# скрипт по построению дёргает команды, которые ДОЛЖНЫ иногда возвращать ≠0 (`grep -c` без
# совпадений, `systemctl is-active` мёртвого юнита, `curl` в упавший дашборд). С `set -e` проверка
# умирала бы на первой находке и скрывала остальные — а нужен полный список. Итоговый вердикт
# даёт счётчик FAIL и явный `exit 1` в конце.

DEEP=0
[ "${1:-}" = "--deep" ] && DEEP=1
APP_DIR="${APP_DIR:-/opt/aimash}"
EXPECT_RAM_GB="${EXPECT_RAM_GB:-8}"

F=0; W=0
ok()   { echo "  ✅ $*"; }
fail() { echo "  ❌ $*"; F=$((F+1)); }
warn() { echo "  ⚠️  $*"; W=$((W+1)); }
head_() { echo; echo "── $* ────────────────────────────────"; }

head_ "1. Ресурсы машины (корень аварий §14.1 — не туннель, а память)"
RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
# Облачный план «8 GB» виден гостевой ОС как ~7.6 GiB: часть адресного пространства
# резервирует гипервизор/ядро. Целочисленное `RAM_MB / 1024` превращало штатные 7.6 в 7
# и ложно валило rescale. Сравниваем MiB с 90% номинала: этого достаточно, чтобы принять
# системный резерв, но 4 GiB вместо 8 GiB по-прежнему не пройдут.
MIN_RAM_MB=$((EXPECT_RAM_GB * 1024 * 90 / 100))
if [ "$RAM_MB" -ge "$MIN_RAM_MB" ]; then
  ok "RAM ${RAM_MB}MiB (план ≥ ${EXPECT_RAM_GB}GiB; gate 90% = ${MIN_RAM_MB}MiB)"
else
  fail "RAM ${RAM_MB}MiB < gate ${MIN_RAM_MB}MiB для плана ${EXPECT_RAM_GB}GiB — rescale/переезд не применился"
fi
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
[ "${SWAP_MB:-0}" -gt 0 ] && ok "swap ${SWAP_MB}M" || warn "swap отсутствует — пик xlsx-отчёта упрётся в OOM жёстче"
USE=$(df / | awk 'NR==2{gsub("%","",$5); print $5}')
if [ "$USE" -ge 90 ]; then fail "диск / занят ${USE}% — сборка деплоя упадёт, Postgres откажет в записи"
elif [ "$USE" -ge 80 ]; then warn "диск / занят ${USE}% (>80%: пора docker builder prune)"
else ok "диск / занят ${USE}%"; fi
# После rescale с УВЕЛИЧЕННЫМ диском раздел не растёт сам (Hetzner: «resize the partition via
# Rescue System»). Симптом — блок-девайс больше файловой системы; иначе место просто не видно.
if command -v lsblk >/dev/null 2>&1; then
  DEV_G=$(lsblk -bdno SIZE "$(lsblk -no PKNAME "$(findmnt -no SOURCE /)" 2>/dev/null | head -1 | sed 's|^|/dev/|')" 2>/dev/null || echo 0)
  FS_G=$(df -B1 / | awk 'NR==2{print $2}')
  if [ "${DEV_G:-0}" -gt 0 ] && [ "$FS_G" -gt 0 ]; then
    if [ "$((DEV_G - FS_G))" -gt $((5 * 1024 * 1024 * 1024)) ]; then
      warn "блок-девайс на $(( (DEV_G - FS_G) / 1024 / 1024 / 1024 ))G больше ФС — раздел не расширен после rescale"
    else ok "раздел занимает весь диск"; fi
  fi
fi

head_ "2. Контейнеры (оба сервиса, не только бот — в scheduler живёт reconcile денежного пути)"
for PAIR in aimash-pg:postgres aimash-bot:bot aimash-scheduler:scheduler aimash-backup:backup; do
  CNT=${PAIR%%:*}
  ST=$(docker inspect -f '{{.State.Status}}' "$CNT" 2>/dev/null || echo missing)
  HL=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$CNT" 2>/dev/null || echo -)
  R1=$(docker inspect -f '{{.RestartCount}}' "$CNT" 2>/dev/null || echo '?')
  if [ "$ST" != "running" ]; then fail "$CNT: state=$ST"; continue; fi
  if [ "$HL" = "unhealthy" ]; then fail "$CNT: running, но healthcheck=unhealthy"; continue; fi
  ok "$CNT: running (health=$HL, restarts=$R1)"
done
# Крэш-луп держит status=running в каждый отдельный момент — видно только по счётчику МЕЖДУ
# семплами (ровно так ведёт себя scheduler, если advisory-lock роли держит бот). Поэтому снимок
# берётся до паузы, второй — после; сравнивать два чтения подряд бессмысленно.
BOT_BEFORE=$(docker inspect -f '{{.RestartCount}}' aimash-bot 2>/dev/null || echo '?')
SCH_BEFORE=$(docker inspect -f '{{.RestartCount}}' aimash-scheduler 2>/dev/null || echo '?')
sleep 8
BOT_AFTER=$(docker inspect -f '{{.RestartCount}}' aimash-bot 2>/dev/null || echo '?')
SCH_AFTER=$(docker inspect -f '{{.RestartCount}}' aimash-scheduler 2>/dev/null || echo '?')
[ "$BOT_BEFORE" = "$BOT_AFTER" ] || fail "aimash-bot перезапускался во время проверки ($BOT_BEFORE → $BOT_AFTER) — крэш-луп"
[ "$SCH_BEFORE" = "$SCH_AFTER" ] || fail "aimash-scheduler перезапускался во время проверки ($SCH_BEFORE → $SCH_AFTER) — крэш-луп (advisory-lock занят ботом?)"
[ "$BOT_BEFORE" = "$BOT_AFTER" ] && [ "$SCH_BEFORE" = "$SCH_AFTER" ] && ok "счётчики рестартов стабильны за 8с"

head_ "3. Двойной поллер Telegram (мина переезда: старая машина ещё поллит)"
# 409 Conflict = второй getUpdates на том же токене. Сообщения при этом теряются молча.
CONFLICTS=$(docker logs aimash-bot --since 10m 2>&1 | grep -c "409\|Conflict: terminated by other getUpdates" || true)
if [ "${CONFLICTS:-0}" -gt 0 ]; then
  fail "в логах бота $CONFLICTS упоминаний 409/Conflict за 10 мин — где-то жив второй поллер (старый VPS?)"
else ok "409 Conflict в логах бота за 10 мин не встречается"; fi

head_ "4. Postgres и схема"
if docker exec aimash-pg pg_isready -U aimash -d aimash >/dev/null 2>&1; then ok "pg_isready"
else fail "pg_isready не отвечает"; fi
REV=$(docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -qtAX -U aimash -d aimash -c "select version_num from alembic_version"' 2>/dev/null || echo "")
[ -n "$REV" ] && ok "alembic_version: $REV" || fail "alembic_version пуст — миграции не применены (бот стартовал?)"
# Ключ шифрования есть, но подходит ли он к восстановленным токенам — вопрос отдельный: неверный
# ключ даёт не пустую таблицу, а ошибку расшифровки при первом обращении к Google Ads.
TOK=$(docker exec aimash-pg sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -qtAX -U aimash -d aimash -c "select count(*) from oauth_tokens"' 2>/dev/null || echo "?")
[ "$TOK" = "?" ] && warn "не прочитал oauth_tokens" || ok "oauth_tokens: $TOK строк (расшифровку проверит первый live-read)"

head_ "5. Пульт: юниты, барьер портов, tailnet (§14)"
for U in hermes-dashboard hermes-dash-proxy tailscaled; do
  if systemctl list-unit-files "$U.service" >/dev/null 2>&1 && systemctl cat "$U.service" >/dev/null 2>&1; then
    systemctl is-active --quiet "$U" && ok "$U active" || fail "$U не active"
  else warn "$U не установлен на этой машине"; fi
done
GW=$(XDG_RUNTIME_DIR=/run/user/0 systemctl --user list-units --type=service --all 2>/dev/null | awk '/hermes-gateway/{print $1; exit}')
if [ -n "$GW" ]; then
  XDG_RUNTIME_DIR=/run/user/0 systemctl --user is-active --quiet "$GW" \
    && ok "$GW active" || warn "$GW не active (норма, если Telegram-контур ещё не выпущен)"
  [ "$(loginctl show-user root -p Linger --value 2>/dev/null)" = "yes" ] \
    && ok "linger=yes (gateway переживёт logout)" || fail "linger=no — user-gateway умрёт при выходе из SSH (§2)"
else warn "user-юнит gateway не найден — Hermes gateway не установлен"; fi

# Барьер: пульт слушает ТОЛЬКО loopback, :443 — только tailnet-адрес. 0.0.0.0 здесь = К3 нарушен.
PORTS=$(ss -ltn 2>/dev/null || echo "")
for P in 9119 9120 5433; do
  L=$(echo "$PORTS" | awk -v p=":$P" '$4 ~ p"$" {print $4}')
  if [ -z "$L" ]; then warn "порт $P никто не слушает"
  elif echo "$L" | grep -qE '^(0\.0\.0\.0|\*|\[::\])'; then fail "порт $P слушает $L — наружу! (ждём 127.0.0.1)"
  else ok "порт $P: $L"; fi
done
L443=$(echo "$PORTS" | awk '$4 ~ /:443$/ {print $4}')
if [ -n "$L443" ]; then
  echo "$L443" | grep -qE '^(0\.0\.0\.0|\*|\[::\])' && fail "порт 443 на $L443 — публично (ждём tailnet-IP 100.x)" || ok "порт 443: $L443"
fi
if command -v tailscale >/dev/null 2>&1; then
  tailscale funnel status 2>&1 | grep -qi "Funnel on" && fail "включён Tailscale FUNNEL — пульт в публичном интернете (⛔ К3)" || ok "funnel выключен"
  tailscale serve status 2>&1 | grep -qi "tailnet only" && ok "serve = tailnet only" || warn "serve status без 'tailnet only' — проверь глазами"
  DNSN=$(tailscale status --json 2>/dev/null | grep -m1 '"DNSName"' | cut -d'"' -f4)
  [ -n "$DNSN" ] && echo "     tailnet-имя: $DNSN  (URL дашборда завязан на него)" || true
fi
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:9119/api/status 2>/dev/null || echo 000)
[ "$CODE" = "200" ] && ok "дашборд /api/status → 200" || fail "дашборд /api/status → $CODE (при active юните это OOM или битый бинд, §14.1)"
# Лимиты памяти дашборда — то, из-за отсутствия чего машину съедал oom-killer.
if systemctl show hermes-dashboard -p MemoryMax 2>/dev/null | grep -qv 'MemoryMax=infinity'; then
  ok "MemoryMax дашборда задан: $(systemctl show hermes-dashboard -p MemoryMax --value 2>/dev/null)"
else warn "MemoryMax дашборда не задан — вернётся oom-kill (§14.1): systemctl set-property hermes-dashboard MemoryHigh=3G MemoryMax=4G MemorySwapMax=2G"; fi
OOM=$(journalctl -k --since -24h 2>/dev/null | grep -c oom-kill || true)
[ "${OOM:-0}" -gt 0 ] && warn "oom-kill за 24ч: $OOM" || ok "oom-kill за 24ч: 0"

head_ "6. Версия Hermes = пин (0.x релизится часто; автообновление на проде выключено)"
if command -v hermes >/dev/null 2>&1; then
  HV=$(hermes version 2>&1 | head -1)
  PINV=$(grep -oE '"release": *"[^"]+"' "$APP_DIR/deploy/hermes/PIN.json" 2>/dev/null | cut -d'"' -f4)
  echo "     $HV"
  if [ -n "$PINV" ]; then
    echo "$HV" | grep -q "$PINV" && ok "версия совпадает с PIN.json ($PINV)" || fail "версия расходится с пином $PINV — поставлено не то"
  fi
else warn "hermes не в PATH"; fi

if [ "$DEEP" = "1" ] && command -v hermes >/dev/null 2>&1; then
  head_ "7. READ-путь через Hermes (--deep)"
  OUT=$(hermes mcp test aimash 2>&1 || true)
  DISCOVERED=$(printf '%s\n' "$OUT" | sed -n 's/.*Tools discovered: \([0-9][0-9]*\).*/\1/p' | head -1)
  EXPECTED=$(docker exec aimash-bot python -c \
    'from mcp_server.server import expected_tool_names; print(len(expected_tool_names()))' 2>/dev/null || true)
  printf '%s\n' "$OUT" | tail -4
  if [ -n "$DISCOVERED" ] && [ -n "$EXPECTED" ] && [ "$DISCOVERED" = "$EXPECTED" ]; then
    ok "MCP aimash отвечает: Tools discovered=$DISCOVERED, runtime expected=$EXPECTED"
  else
    fail "MCP surface расходится: discovered=${DISCOVERED:-none}, expected=${EXPECTED:-none} (проверь live-образ и allowlist)"
  fi
fi

echo
echo "══ итог: FAIL=$F, WARN=$W ══"
if [ "$F" -gt 0 ]; then
  echo "Машина НЕ принята: разбирай FAIL сверху вниз (ранбук §14–§16)."
  exit 1
fi
echo "Машина принята (WARN просмотреть глазами)."

#!/bin/sh
# Install the dashboard proxy policy without restarting the dashboard or Telegram gateway.
set -eu

MODE=${1:-dry-run}
SOURCE=/opt/aimash/deploy/hermes/dashboard/Caddyfile
TARGET=/etc/caddy/Caddyfile
BACKUP=/etc/caddy/Caddyfile.aimash-prev
SERVICE=hermes-dash-proxy.service

if [ "$MODE" != "dry-run" ] && [ "$MODE" != "--apply" ]; then
    echo "usage: $0 [dry-run|--apply]" >&2
    exit 2
fi
if [ ! -f "$SOURCE" ]; then
    echo "dashboard proxy source is missing: $SOURCE" >&2
    exit 1
fi

/usr/local/bin/caddy validate --config "$SOURCE" --adapter caddyfile >/dev/null
if [ "$MODE" = "dry-run" ]; then
    echo "[dry-run] validate $SOURCE, back up $TARGET, restart $SERVICE, verify /api/status"
    exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "dashboard proxy apply requires root" >&2
    exit 1
fi

if [ -f "$TARGET" ]; then
    cp -a "$TARGET" "$BACKUP"
fi
install -m 0644 "$SOURCE" "$TARGET"
if systemctl restart "$SERVICE"; then
    ATTEMPT=1
    while [ "$ATTEMPT" -le 10 ]; do
        if curl --fail --silent --show-error --max-time 2 \
            http://127.0.0.1:9120/api/status >/dev/null 2>&1; then
            echo "dashboard proxy isolation applied; rollback=$BACKUP"
            exit 0
        fi
        sleep 1
        ATTEMPT=$((ATTEMPT + 1))
    done
fi

echo "dashboard proxy verification failed; restoring previous config" >&2
if [ -f "$BACKUP" ]; then
    install -m 0644 "$BACKUP" "$TARGET"
    systemctl restart "$SERVICE" || true
fi
exit 1

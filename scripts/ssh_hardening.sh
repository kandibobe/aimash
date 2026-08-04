#!/bin/sh
# Reversible SSH password-auth hardening. Dry-run is the default; apply requires a second key session.
set -eu

MODE=${1:-dry-run}
TARGET=/etc/ssh/sshd_config.d/90-aimash-hardening.conf
BACKUP=/etc/ssh/sshd_config.d/90-aimash-hardening.conf.aimash-prev

case "$MODE" in
    dry-run)
        sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
        echo "[dry-run] would install $TARGET after a confirmed second key-authenticated session"
        ;;
    --apply)
        if [ "${AIMASH_CONFIRMED_SECOND_KEY_SESSION:-}" != "1" ]; then
            echo "refusing apply: set AIMASH_CONFIRMED_SECOND_KEY_SESSION=1 only from a verified second SSH key session" >&2
            exit 2
        fi
        if [ "$(id -u)" -ne 0 ]; then
            echo "SSH hardening apply requires root" >&2
            exit 1
        fi
        candidate=$(mktemp /etc/ssh/sshd_config.d/.90-aimash-hardening.XXXXXX)
        trap 'rm -f "$candidate"' EXIT
        printf '%s\n' \
            '# Aimash reversible SSH hardening' \
            'PasswordAuthentication no' \
            'KbdInteractiveAuthentication no' \
            'PermitRootLogin prohibit-password' >"$candidate"
        chmod 0644 "$candidate"
        if [ -f "$TARGET" ]; then
            cp -a "$TARGET" "$BACKUP"
        fi
        install -m 0644 "$candidate" "$TARGET"
        if ! sshd -t; then
            echo "sshd validation failed; rolling back" >&2
            if [ -f "$BACKUP" ]; then
                install -m 0644 "$BACKUP" "$TARGET"
            else
                rm -f "$TARGET"
            fi
            sshd -t
            exit 1
        fi
        unit=ssh.service
        systemctl list-unit-files sshd.service >/dev/null 2>&1 && unit=sshd.service
        systemctl reload "$unit"
        sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '
        echo "SSH hardening applied; keep the current session open and verify one more key login"
        ;;
    --rollback)
        if [ "$(id -u)" -ne 0 ]; then
            echo "SSH hardening rollback requires root" >&2
            exit 1
        fi
        if [ -f "$BACKUP" ]; then
            install -m 0644 "$BACKUP" "$TARGET"
        else
            rm -f "$TARGET"
        fi
        sshd -t
        unit=ssh.service
        systemctl list-unit-files sshd.service >/dev/null 2>&1 && unit=sshd.service
        systemctl reload "$unit"
        echo "SSH hardening rolled back"
        ;;
    *)
        echo "usage: $0 [dry-run|--apply|--rollback]" >&2
        exit 2
        ;;
esac

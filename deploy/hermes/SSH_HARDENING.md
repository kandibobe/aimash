# Reversible SSH password-auth hardening

This procedure is independent from application deploy and does not restart Hermes.

`scripts/ssh_hardening.sh` is dry-run by default. Before `--apply`, open and keep a second SSH session
that has authenticated with a key, then set `AIMASH_CONFIRMED_SECOND_KEY_SESSION=1` in that verified
session. The script installs only `/etc/ssh/sshd_config.d/90-aimash-hardening.conf`, runs `sshd -t`,
and reloads SSH without terminating current sessions.

Use `--rollback` to restore the `.aimash-prev` copy, or remove the drop-in when no prior file existed,
validate again and reload. Keep the original SSH session open until another key login succeeds after
apply. This does not move Hermes away from root; that remains a separate migration.

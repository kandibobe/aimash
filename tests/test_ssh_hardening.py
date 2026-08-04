"""The SSH hardening helper must remain gated and independently reversible."""

from __future__ import annotations

from tests._docs_paths import ROOT


def test_ssh_hardening_requires_second_key_session_and_has_rollback():
    script = (ROOT / "scripts/ssh_hardening.sh").read_text(encoding="utf-8")

    assert "MODE=${1:-dry-run}" in script
    assert "AIMASH_CONFIRMED_SECOND_KEY_SESSION:-" in script
    assert "PasswordAuthentication no" in script
    assert "KbdInteractiveAuthentication no" in script
    assert "PermitRootLogin prohibit-password" in script
    assert "sshd -t" in script
    assert "systemctl reload" in script
    assert "--rollback" in script
    assert ".aimash-prev" in script

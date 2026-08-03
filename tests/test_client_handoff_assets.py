from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


ROOT = Path(__file__).resolve().parents[1]


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


@pytest.mark.skipif(
    os.name == "nt" or not shutil.which("bash") or not shutil.which("openssl"),
    reason="requires a POSIX shell and OpenSSL",
)
def test_generate_secrets_creates_valid_env_and_refuses_overwrite(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / ".env.example", project / ".env.example")
    shutil.copy2(ROOT / "scripts" / "generate_secrets.sh", scripts / "generate_secrets.sh")

    command = ["bash", str(scripts / "generate_secrets.sh")]
    first = subprocess.run(command, cwd=project, text=True, capture_output=True, check=False)
    assert first.returncode == 0, first.stderr
    env_path = project / ".env"
    values = _read_env(env_path)
    Fernet(values["SECRETS_ENCRYPTION_KEY"].encode())
    assert len(values["PSEUDONYMIZATION_HMAC_KEY"]) == 64
    assert len(values["AIMASH_TRUST_HMAC_KEY"]) == 64
    before = env_path.read_bytes()

    second = subprocess.run(command, cwd=project, text=True, capture_output=True, check=False)
    assert second.returncode != 0
    assert env_path.read_bytes() == before


def test_handoff_runbook_references_existing_scripts():
    runbook = (ROOT / "docs" / "CLIENT_HANDOFF_RUNBOOK.md").read_text(encoding="utf-8")
    for name in ("generate_secrets.sh", "prepare_prod_db.py", "create_release_backup.sh"):
        assert name in runbook
        assert (ROOT / "scripts" / name).is_file()

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from scripts.backup_crypto import create_key, decrypt, encrypt, verify


def test_backup_crypto_round_trip_and_tamper_detection(tmp_path: Path):
    source = tmp_path / "backup.tgz"
    encrypted = tmp_path / "backup.tgz.aes256"
    restored = tmp_path / "restored.tgz"
    key = tmp_path / "recovery.key"
    payload = b"backup-data" * 100_000
    source.write_bytes(payload)
    create_key(key)

    plain_hash, cipher_hash = encrypt(source, encrypted, key)
    assert len(plain_hash) == 64 and len(cipher_hash) == 64
    assert verify(encrypted, key) == plain_hash
    assert decrypt(encrypted, restored, key) == plain_hash
    assert restored.read_bytes() == payload

    tampered = bytearray(encrypted.read_bytes())
    tampered[-17] ^= 1
    encrypted.write_bytes(tampered)
    with pytest.raises(InvalidTag):
        verify(encrypted, key)

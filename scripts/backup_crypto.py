#!/usr/bin/env python3
"""Encrypt and verify off-host Aimash backups without exposing the recovery key.

Format: fixed magic + 96-bit nonce + AES-256-GCM ciphertext + 128-bit authentication tag.
The key is a separate 32-byte file and must never be stored beside the encrypted backup.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"AIMBKP1\0"
NONCE_BYTES = 12
TAG_BYTES = 16
CHUNK_BYTES = 1024 * 1024


def _key(path: Path) -> bytes:
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("recovery key must contain exactly 32 bytes")
    return key


def create_key(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite recovery key: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(secrets.token_bytes(32))
    os.chmod(path, 0o600)


def encrypt(source: Path, target: Path, key_path: Path) -> tuple[str, str]:
    if target.exists():
        raise FileExistsError(f"refusing to overwrite encrypted backup: {target}")
    nonce = secrets.token_bytes(NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(_key(key_path)), modes.GCM(nonce)).encryptor()
    plain_hash = hashlib.sha256()
    cipher_hash = hashlib.sha256()
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            header = MAGIC + nonce
            dst.write(header)
            cipher_hash.update(header)
            while chunk := src.read(CHUNK_BYTES):
                plain_hash.update(chunk)
                encrypted = encryptor.update(chunk)
                dst.write(encrypted)
                cipher_hash.update(encrypted)
            tail = encryptor.finalize()
            if tail:
                dst.write(tail)
                cipher_hash.update(tail)
            dst.write(encryptor.tag)
            cipher_hash.update(encryptor.tag)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return plain_hash.hexdigest(), cipher_hash.hexdigest()


def decrypt(source: Path, target: Path, key_path: Path) -> str:
    if target.exists():
        raise FileExistsError(f"refusing to overwrite restored backup: {target}")
    size = source.stat().st_size
    header_size = len(MAGIC) + NONCE_BYTES
    if size < header_size + TAG_BYTES:
        raise ValueError("encrypted backup is truncated")
    with source.open("rb") as src:
        header = src.read(header_size)
        if not header.startswith(MAGIC):
            raise ValueError("encrypted backup has an unknown format")
        nonce = header[len(MAGIC) :]
        src.seek(-TAG_BYTES, os.SEEK_END)
        tag = src.read(TAG_BYTES)
        src.seek(header_size)
        remaining = size - header_size - TAG_BYTES
        decryptor = Cipher(algorithms.AES(_key(key_path)), modes.GCM(nonce, tag)).decryptor()
        digest = hashlib.sha256()
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("xb") as dst:
                while remaining:
                    chunk = src.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise ValueError("encrypted backup is truncated")
                    remaining -= len(chunk)
                    plain = decryptor.update(chunk)
                    dst.write(plain)
                    digest.update(plain)
                tail = decryptor.finalize()
                if tail:
                    dst.write(tail)
                    digest.update(tail)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return digest.hexdigest()


def verify(source: Path, key_path: Path) -> str:
    temporary = source.with_name(f".{source.name}.{secrets.token_hex(8)}.verify")
    try:
        return decrypt(source, temporary, key_path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen")
    keygen.add_argument("key", type=Path)
    for name in ("encrypt", "decrypt"):
        command = sub.add_parser(name)
        command.add_argument("source", type=Path)
        command.add_argument("target", type=Path)
        command.add_argument("--key", required=True, type=Path)
    check = sub.add_parser("check")
    check.add_argument("source", type=Path)
    check.add_argument("--key", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "keygen":
        create_key(args.key)
        print(f"recovery key created: {args.key}")
    elif args.command == "encrypt":
        plain, encrypted = encrypt(args.source, args.target, args.key)
        print(f"encrypted backup verified by construction: plaintext_sha256={plain}")
        print(f"ciphertext_sha256={encrypted}")
    elif args.command == "decrypt":
        digest = decrypt(args.source, args.target, args.key)
        print(f"restored plaintext_sha256={digest}")
    else:
        digest = verify(args.source, args.key)
        print(f"authentication and decryption verified: plaintext_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

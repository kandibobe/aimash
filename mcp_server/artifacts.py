"""Signed, bounded file artifacts produced by the Aimash MCP process.

The MCP server runs inside the one-shot ``aimash-mcp`` container while Hermes runs on the host. A tool may create a
report in the container, but it must never give the model a generic "send this path" primitive.
This module returns a short-lived HMAC token bound to one file under a dedicated temp directory.
The trusted Hermes plugin verifies the token, copies exactly that file and delivers it to the
current Telegram topic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from core.config import settings

ARTIFACT_VERSION = 1
ARTIFACT_TTL_S = 15 * 60
ARTIFACT_MAX_BYTES = 20 * 1024 * 1024
ARTIFACT_DIR = Path(tempfile.gettempdir()) / "aimash_artifacts"
ARTIFACT_MARKER = "AIMASH_ARTIFACT:"
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._ -]+")
_ALLOWED_MIME = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
        "text/plain",
        "application/pdf",
        "image/jpeg",
        "image/png",
        "video/mp4",
    }
)


def artifact_path(suffix: str) -> Path:
    """Allocate a unique path inside the only directory exportable to Hermes."""
    clean_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or "") else ".bin"
    ARTIFACT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ARTIFACT_DIR / f"{uuid.uuid4().hex}{clean_suffix}"


def _key() -> bytes:
    raw = settings.aimash_trust_hmac_key.get_secret_value().encode("utf-8")
    if len(raw) < 32:
        raise PermissionError("artifact transport не настроен")
    return raw


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _safe_filename(value: str) -> str:
    name = _SAFE_NAME_RE.sub("_", Path(str(value or "artifact")).name).strip(" .")
    return (name or "artifact")[:120]


def _description_for(filename: str, media_type: str) -> str:
    """Return a concise, truthful archive label without inspecting untrusted file contents."""
    name = filename.lower()
    if name.startswith("aimash_mcc_report_"):
        return "Сводный XLSX-отчёт по аккаунтам MCC и их показателям."
    if name.startswith("aimash_report_"):
        return "Подробный XLSX-отчёт Google Ads по одному аккаунту."
    if name.startswith("aimash_keywords_"):
        return "Построчная XLSX-выгрузка ключевых слов и их показателей."
    if name.startswith("aimash_monthly_report_"):
        return "Ежемесячный PDF-отчёт Google Ads: результаты, риски и план."
    if media_type == "application/pdf":
        return "PDF-документ, сформированный Aimash."
    if media_type == "text/csv":
        return "CSV-выгрузка, сформированная Aimash."
    if media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return "XLSX-таблица, сформированная Aimash."
    return "Файл, сформированный Aimash."


def publish_artifact(path: str | Path, *, filename: str, media_type: str) -> dict[str, Any]:
    """Return a signed artifact descriptor; reject paths outside ``ARTIFACT_DIR`` fail-closed."""
    if media_type not in _ALLOWED_MIME:
        raise ValueError("неподдерживаемый MIME-тип артефакта")
    target = Path(path).resolve(strict=True)
    root = ARTIFACT_DIR.resolve(strict=True)
    if target.parent != root or target.is_symlink() or not target.is_file():
        raise PermissionError("артефакт находится вне разрешённого каталога")
    size = target.stat().st_size
    if size <= 0 or size > ARTIFACT_MAX_BYTES:
        raise ValueError("размер артефакта вне допустимого диапазона")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    safe_filename = _safe_filename(filename)
    payload = {
        "v": ARTIFACT_VERSION,
        "iat": int(time.time()),
        "exp": int(time.time()) + ARTIFACT_TTL_S,
        "container": "aimash-mcp",
        "path": str(target),
        "filename": safe_filename,
        "description": _description_for(safe_filename, media_type),
        "media_type": media_type,
        "size": size,
        "sha256": digest,
        "nonce": uuid.uuid4().hex,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    sig = hmac.new(_key(), raw, hashlib.sha256).digest()
    token = f"{_b64(raw)}.{_b64(sig)}"
    return {
        "artifact_id": payload["nonce"],
        "filename": payload["filename"],
        "description": payload["description"],
        "media_type": media_type,
        "size": size,
        "sha256": digest,
        "delivery": "telegram_topic",
        "token": token,
        "marker": f"{ARTIFACT_MARKER}{token}",
    }


def remove_artifact(path: str | Path) -> None:
    """Best-effort cleanup limited to ``ARTIFACT_DIR``."""
    try:
        target = Path(path).resolve(strict=True)
        if target.parent == ARTIFACT_DIR.resolve(strict=True) and not target.is_symlink():
            os.remove(target)
    except OSError:
        pass

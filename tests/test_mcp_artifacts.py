from __future__ import annotations

import pytest
from pydantic import SecretStr

from core.config import settings
from mcp_server import artifacts


def test_artifact_is_signed_bounded_and_contains_no_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "aimash_trust_hmac_key", SecretStr("k" * 32))
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)
    path = artifacts.artifact_path(".xlsx")
    path.write_bytes(b"xlsx-bytes")

    result = artifacts.publish_artifact(
        path,
        filename="report.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert result["filename"] == "report.xlsx"
    assert result["size"] == len(b"xlsx-bytes")
    assert result["marker"].startswith(artifacts.ARTIFACT_MARKER)
    assert "k" * 32 not in result["token"]


def test_artifact_rejects_path_outside_dedicated_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "aimash_trust_hmac_key", SecretStr("k" * 32))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"x")
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", allowed)

    with pytest.raises(PermissionError):
        artifacts.publish_artifact(
            outside,
            filename="outside.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


def test_artifact_rejects_empty_and_oversized(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "aimash_trust_hmac_key", SecretStr("k" * 32))
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", tmp_path)
    empty = artifacts.artifact_path(".txt")
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        artifacts.publish_artifact(empty, filename="x.txt", media_type="text/plain")

    monkeypatch.setattr(artifacts, "ARTIFACT_MAX_BYTES", 1)
    large = artifacts.artifact_path(".txt")
    large.write_bytes(b"xx")
    with pytest.raises(ValueError):
        artifacts.publish_artifact(large, filename="x.txt", media_type="text/plain")


def test_remove_artifact_never_deletes_outside_root(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(artifacts, "ARTIFACT_DIR", allowed)

    artifacts.remove_artifact(outside)

    assert outside.exists()

"""B9: honest Docker healthcheck по свежести heartbeat — зависший polling/крэш-луп больше НЕ
остаётся «healthy». Проверяем модуль core.heartbeat и логику scripts/healthcheck.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import core.heartbeat as hb  # noqa: E402


def test_write_then_fresh(tmp_path, monkeypatch):
    hbf = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(hbf))
    hb.write_heartbeat()
    assert hbf.exists()
    assert hb.is_fresh(max_age_s=60) is True


def test_missing_file_not_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT_FILE", str(tmp_path / "nope"))
    assert hb.is_fresh() is False


def test_stale_file_not_fresh(tmp_path, monkeypatch):
    hbf = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(hbf))
    hb.write_heartbeat()
    old = time.time() - 3600  # состарить на час
    os.utime(hbf, (old, old))
    assert hb.is_fresh(max_age_s=60) is False


async def test_heartbeat_loop_writes_then_cancels(tmp_path, monkeypatch):
    import asyncio

    hbf = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(hbf))
    task = asyncio.create_task(hb.heartbeat_loop(interval_s=0.01))
    await asyncio.sleep(0.05)  # даёт написать хотя бы раз
    assert hbf.exists() and hb.is_fresh(max_age_s=60)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_healthcheck_script_exit_codes(tmp_path, monkeypatch):
    import healthcheck

    hbf = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_FILE", str(hbf))
    # нет файла → unhealthy (1)
    assert healthcheck.main() == 1
    # свежий → healthy (0)
    hb.write_heartbeat()
    assert healthcheck.main() == 0

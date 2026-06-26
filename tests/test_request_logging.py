"""ТЗ §15: логирование запросов к LLM и Google Ads API (имя/длительность/исход, без секретов).
Проверяем, что run_ads_call/call_llm пишут структурную строку лога на успех и на ошибку."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.resilience as R  # noqa: E402


async def test_run_ads_call_logs_ok(caplog):
    with caplog.at_level(logging.INFO, logger="aimash"):
        await R.run_ads_call(lambda: {"ok": True}, label="update_budget")
    assert any("ads-call update_budget: ok" in r.getMessage() for r in caplog.records)


async def test_run_ads_call_logs_failure(caplog):
    def boom():
        raise ValueError("nope")

    with caplog.at_level(logging.WARNING, logger="aimash"):
        try:
            await R.run_ads_call(boom)
        except ValueError:
            pass
    assert any(
        "ads-call" in r.getMessage() and "ValueError" in r.getMessage() for r in caplog.records
    )


async def test_call_llm_logs_ok(caplog):
    async def factory():
        return "x"

    with caplog.at_level(logging.INFO, logger="aimash"):
        await R.call_llm(factory, label="deepseek/parsing")
    assert any("llm-call deepseek/parsing: ok" in r.getMessage() for r in caplog.records)

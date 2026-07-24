"""Офлайн-тесты P2 (стабильность/отлаживаемость): humanize Google Ads ошибок (§15), read-устойчивость
(run_ads_read_call ретраит TimeoutError, мутационный run_ads_call — нет), lifecycle БД (dispose).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.resilience as R  # noqa: E402
from core.ads_errors import error_code_names, humanize_google_ads_error  # noqa: E402


# ── humanize_google_ads_error (§15) ──────────────────────────────────────────────
def _gads_exc(errors, request_id=None):
    return SimpleNamespace(
        failure=SimpleNamespace(errors=errors),
        request_id=request_id,
    )


def test_humanize_extracts_messages_codes_and_request_id():
    exc = _gads_exc(
        [
            SimpleNamespace(
                message="Budget too low", error_code=SimpleNamespace(name="BUDGET_ERROR")
            ),
            SimpleNamespace(message="Bad bid", error_code=SimpleNamespace(name="BID_ERROR")),
        ],
        request_id="req-123",
    )
    out = humanize_google_ads_error(exc)
    assert "Budget too low [BUDGET_ERROR]" in out
    assert "Bad bid [BID_ERROR]" in out
    assert "request_id: req-123" in out


def test_humanize_truncates_and_counts_extra():
    errors = [
        SimpleNamespace(message=f"err{i}", error_code=SimpleNamespace(name=f"E{i}"))
        for i in range(5)
    ]
    out = humanize_google_ads_error(_gads_exc(errors), max_errors=2)
    assert "err0" in out and "err1" in out
    assert "…и ещё 3" in out  # 5 - 2 = 3
    assert "err4" not in out


def test_humanize_fallback_for_non_googleads_error():
    out = humanize_google_ads_error(ValueError("boom"))
    assert "ValueError" in out and "boom" in out


def test_error_code_names_single_source():
    """Единый источник имён кодов (используется и в retry-классификации, и в humanize, и в
    bid-mismatch). Дакт-фейк error_code.name → множество имён; ретрай-классификатор его потребляет."""
    exc = _gads_exc(
        [
            SimpleNamespace(message="x", error_code=SimpleNamespace(name="RATE_EXCEEDED")),
            SimpleNamespace(message="y", error_code=SimpleNamespace(name="BUDGET_ERROR")),
            SimpleNamespace(message="z", error_code=None),  # без кода — пропускаем
        ]
    )
    assert error_code_names(exc) == {"RATE_EXCEEDED", "BUDGET_ERROR"}
    # тот же набор питает классификацию ретраев (RATE_EXCEEDED ∈ RETRYABLE_ADS_NAMES)
    assert error_code_names(exc) & R.RETRYABLE_ADS_NAMES == {"RATE_EXCEEDED"}


def test_humanize_redacts_secrets():
    # str ошибки SDK может нести креды → редактируем (golden rule #5)
    exc = _gads_exc(
        [SimpleNamespace(message="refresh_token=1//abcSECRETxyz failed", error_code=None)]
    )
    out = humanize_google_ads_error(exc)
    assert "abcSECRETxyz" not in out  # отредактировано


# ── run_ads_read_call vs run_ads_call: семантика ретрая TimeoutError ──────────────
def test_is_retryable_read_includes_timeout_but_mutation_does_not():
    assert R._is_retryable_ads_read(TimeoutError("slow")) is True
    assert R._is_retryable_ads(TimeoutError("slow")) is False  # мутации таймаут НЕ ретраят


async def test_run_ads_read_call_retries_timeout(monkeypatch):
    monkeypatch.setattr(R, "ADS_WAIT_MULTIPLIER", 0.0)  # без задержек в тесте
    monkeypatch.setattr(R, "ADS_WAIT_MAX", 0.0)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow read")
        return "ok"

    res = await R.run_ads_read_call(fn, label="t")
    assert res == "ok" and calls["n"] == 3  # ретраил таймаут до успеха


async def test_run_ads_call_does_not_retry_timeout():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise TimeoutError("slow mutation")

    with pytest.raises(TimeoutError):
        await R.run_ads_call(fn, label="t")
    assert calls["n"] == 1  # денежный путь: таймаут НЕ повторяется (защита от double-spend)


# ── db lifecycle ─────────────────────────────────────────────────────────────────
def test_dispose_engine_is_async_callable():
    from db.session import dispose_engine

    assert inspect.iscoroutinefunction(dispose_engine)  # для finally main() (graceful shutdown)

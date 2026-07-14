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


# ── Наблюдаемость сбоя ридера (A1): в лог идёт КОД ошибки, не 'GoogleAdsException' и не str(e) ──
# Диагностика «жидкого» аудита: 4-5 тяжёлых ридеров молча падали, а лог писал лишь голое имя типа
# (`GoogleAdsException`) без кода — не отличить дрейф поля v24 от таймаута/нет прав. Теперь
# `_fail_cause` кладёт имена enum-кодов; GR5: сырой str(e)/failure/трейсбэк в лог НЕ попадает.


class _FakeErrCode:
    def __init__(self, name: str) -> None:
        self.name = name  # error_code_name(): нет WhichOneof → берётся .name (дакт-фейк)


class _FakeErr:
    def __init__(self, name: str, message: str) -> None:
        self.error_code = _FakeErrCode(name)
        self.message = message


class _FakeFailure:
    def __init__(self, errors: list[_FakeErr]) -> None:
        self.errors = errors


class _FakeGoogleAdsException(Exception):
    """Дакт-фейк GoogleAdsException: error_code_names() читает .failure.errors[].error_code.name."""

    def __init__(self, code_name: str, message: str) -> None:
        super().__init__(message)
        self.failure = _FakeFailure([_FakeErr(code_name, message)])


async def test_ads_read_failure_logs_error_code(caplog):
    """GoogleAdsException → в логе конкретный код (FIELD_NOT_FOUND), а не безликое имя типа."""

    def _fetch():
        raise _FakeGoogleAdsException("FIELD_NOT_FOUND", "no such field: campaign.foo")

    with caplog.at_level(logging.WARNING, logger="aimash"):
        try:
            await R.run_ads_read_call(_fetch, label="audit_campaign_settings")
        except _FakeGoogleAdsException:
            pass
    line = next(
        r.getMessage()
        for r in caplog.records
        if "ads-read audit_campaign_settings" in r.getMessage()
    )
    assert "FIELD_NOT_FOUND" in line
    assert "GoogleAdsException" not in line  # голое имя типа заменено кодом


async def test_ads_read_timeout_logs_timeout_type(monkeypatch, caplog):
    """Таймаут ридера → в логе TimeoutError (нет .failure → падаем на имя типа). Без ретраев."""
    monkeypatch.setattr(R, "ADS_MAX_ATTEMPTS", 1)  # TimeoutError ретраится — гасим бэкофф в тесте

    def _fetch():
        raise TimeoutError()

    with caplog.at_level(logging.WARNING, logger="aimash"):
        try:
            await R.run_ads_read_call(_fetch, label="audit_geo_waste")
        except TimeoutError:
            pass
    assert any(
        "ads-read audit_geo_waste" in r.getMessage() and "TimeoutError" in r.getMessage()
        for r in caplog.records
    )


async def test_ads_read_failure_does_not_log_raw_exception_secret(caplog):
    """GR5-регресс: секрет из str(e) в лог НЕ попадает — логируется ТОЛЬКО имя enum-кода."""
    secret = "1234567890:AAFakeTelegramTokenLikeStringNeverLogged_abcdefgh"

    def _fetch():
        raise _FakeGoogleAdsException("AUTHENTICATION_ERROR", f"bad creds token={secret}")

    with caplog.at_level(logging.WARNING, logger="aimash"):
        try:
            await R.run_ads_read_call(_fetch, label="audit_pmax_campaigns")
        except _FakeGoogleAdsException:
            pass
    assert secret not in caplog.text  # сырой str(e) не должен попасть в лог (golden rule #5)
    assert any("AUTHENTICATION_ERROR" in r.getMessage() for r in caplog.records)


async def test_ads_read_plain_exception_logs_type_not_message(caplog):
    """GR5-регресс: у не-GoogleAds исключения логируется имя типа, но НЕ его message с секретом."""
    secret = "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1234567"

    def _fetch():
        raise RuntimeError(f"client_secret={secret}")

    with caplog.at_level(logging.WARNING, logger="aimash"):
        try:
            await R.run_ads_read_call(_fetch, label="audit_campaign_assets")
        except RuntimeError:
            pass
    assert secret not in caplog.text
    assert any(
        "ads-read audit_campaign_assets" in r.getMessage() and "RuntimeError" in r.getMessage()
        for r in caplog.records
    )

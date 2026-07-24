"""§14 — настраиваемое расписание планового отчёта (scheduler.service.report_trigger).

REPORT_SCHEDULE (стандартная crontab-строка) задаёт кадэнс отчёта — одним полем и ежедневно,
и еженедельно. Ключевое свойство: невалидная строка НЕ роняет старт бота — откат на ежедневно
09:00 (fail-safe; расписание — не security-гейт). Офлайн, без сети/loop/SDK.
"""

from __future__ import annotations

import sys
from pathlib import Path

from apscheduler.triggers.cron import CronTrigger

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import settings  # noqa: E402
from scheduler.service import (  # noqa: E402
    _DEFAULT_REPORT_CRON,
    _DEFAULT_WEEKLY_DIGEST_CRON,
    report_trigger,
    weekly_digest_trigger,
)


def _set_schedule(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "report_schedule", value, raising=False)


def test_defaults_present():
    """Дефолты §14 на месте: ежедневно 09:00, аномалии 6ч, очистка 60мин."""
    assert settings.report_schedule == "0 9 * * *"
    assert settings.anomaly_interval_hours == 6
    assert settings.cleanup_interval_minutes == 60


def test_daily_default_crontab(monkeypatch):
    _set_schedule(monkeypatch, "0 9 * * *")
    assert str(report_trigger()) == str(CronTrigger.from_crontab("0 9 * * *"))


def test_weekly_crontab_parses(monkeypatch):
    """Еженедельно (пн 09:00) — другой триггер, чем ежедневно: day_of_week реально применился."""
    _set_schedule(monkeypatch, "0 9 * * 1")
    weekly = report_trigger()
    assert str(weekly) == str(CronTrigger.from_crontab("0 9 * * 1"))
    assert str(weekly) != str(CronTrigger.from_crontab("0 9 * * *"))  # не схлопнулось в ежедневно


def test_invalid_crontab_falls_back_to_default_not_raises(monkeypatch):
    """Невалидная строка => НЕ исключение, а откат на ежедневно 09:00 (бот стартует)."""
    _set_schedule(monkeypatch, "не-крон")
    assert str(report_trigger()) == str(CronTrigger(**_DEFAULT_REPORT_CRON))


def test_empty_crontab_falls_back_to_default(monkeypatch):
    _set_schedule(monkeypatch, "")
    assert str(report_trigger()) == str(CronTrigger(**_DEFAULT_REPORT_CRON))


# ── 1.3: еженедельный дайджест — свой crontab, тот же fail-safe ────────────────────
def _set_digest(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "weekly_digest_schedule", value, raising=False)


def test_weekly_digest_default_is_monday_9():
    assert settings.weekly_digest_schedule == "0 9 * * 1"
    assert settings.weekly_digest_enabled is False  # opt-in по умолчанию


def test_weekly_digest_crontab_parses(monkeypatch):
    _set_digest(monkeypatch, "30 8 * * 5")  # пятница 08:30
    assert str(weekly_digest_trigger()) == str(CronTrigger.from_crontab("30 8 * * 5"))


def test_weekly_digest_invalid_falls_back(monkeypatch):
    _set_digest(monkeypatch, "мусор")
    assert str(weekly_digest_trigger()) == str(CronTrigger(**_DEFAULT_WEEKLY_DIGEST_CRON))


# ── 1.6/2.4: бизнес-дайджест и re-discovery — конфиг с fail-safe дефолтами ─────────
def test_business_digest_and_rediscovery_defaults():
    assert settings.business_digest_schedule == "0 9 * * 1"
    assert settings.mcc_rediscovery_hours == 24  # 0 = выкл (снимок на старте, как раньше)
    assert settings.advise_digest_top_n == 5


def test_business_digest_trigger_fail_safe(monkeypatch):
    from scheduler.service import business_digest_trigger

    monkeypatch.setattr(settings, "business_digest_schedule", "45 7 * * 3", raising=False)
    assert str(business_digest_trigger()) == str(CronTrigger.from_crontab("45 7 * * 3"))
    monkeypatch.setattr(settings, "business_digest_schedule", "мусор", raising=False)
    assert str(business_digest_trigger()) == str(CronTrigger(**_DEFAULT_WEEKLY_DIGEST_CRON))


async def test_rediscovery_job_calls_discover(monkeypatch):
    """2.4: джоба зовёт ТОЛЬКО discover_read_children (read-only; кэши клиентов не сбрасывает)."""
    import ads.client as ads_client
    from scheduler import jobs

    called = {"n": 0}

    async def _fake_discover():
        called["n"] += 1
        return 7

    monkeypatch.setattr(ads_client, "discover_read_children", _fake_discover)
    await jobs.run_mcc_rediscovery()
    assert called["n"] == 1

"""2.11 (§14): авто-подстройка порогов аномалий + /myschedule.

Инварианты: формулы чистые и в клампах UI; предложение только при материальном отличии (анти-шум);
анти-спам по proposed_at/declined_at; джоба сама НИЧЕГО не пишет в настройки (запись — только
хендлер «✅ Принять»); /myschedule пишет report_schedule и live-применяет; register_user_report_
schedules видит рантайм-whitelisted (env ∪ БД, D9).
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scheduler import threshold_tuner as tt  # noqa: E402


def _rows(daily: list[tuple[float, float]]):
    return [
        ((f"2026-06-{i + 1:02d}",), SimpleNamespace(cost_micros=int(c * 1e6), conversions=v))
        for i, (c, v) in enumerate(daily)
    ]


# ── чистые формулы ────────────────────────────────────────────────────────────────────
def test_weekly_buckets_full_weeks_only():
    costs, convs = tt.weekly_buckets(_rows([(10.0, 1.0)] * 17))  # 2 полных недели + хвост 3 дня
    assert costs == [70.0, 70.0] and convs == [7.0, 7.0]
    assert tt.weekly_buckets(_rows([(10.0, 1.0)] * 6)) == ([], [])  # меньше недели — пусто


def test_suggest_needs_min_weeks_and_spend():
    assert tt.suggest_thresholds([70.0] * 3, [7.0] * 3, {}) is None  # < MIN_WEEKS
    assert tt.suggest_thresholds([0.0] * 6, [0.0] * 6, {}) is None  # нулевой расход


def test_suggest_clamped_and_material():
    # волатильный расход → высокое предложение, но В КЛАМПАХ UI
    costs = [100.0, 300.0, 50.0, 400.0, 80.0, 250.0]
    sug = tt.suggest_thresholds(costs, [1.0] * 6, {"spend_spike_pct": 50.0})
    assert sug is not None
    assert tt.SPIKE_RANGE[0] <= sug["spend_spike_pct"] <= tt.SPIKE_RANGE[1]
    assert tt.DROP_RANGE[0] <= sug["conv_drop_pct"] <= tt.DROP_RANGE[1]
    assert tt.MIN_SPEND_RANGE[0] <= sug["min_spend"] <= tt.MIN_SPEND_RANGE[1]
    # ~совпадает с текущими → None (материальность, анти-шум)
    stable = [100.0] * 8
    cur = tt.suggest_thresholds(stable, [10.0] * 8, {}) or {}
    assert tt.suggest_thresholds(stable, [10.0] * 8, cur) is None


def test_cooldown_logic():
    from scheduler.jobs import _thr_tune_on_cooldown

    now = datetime.now(timezone.utc)
    assert _thr_tune_on_cooldown(None, now) is False
    fresh = {"proposed_at": (now - timedelta(days=3)).isoformat()}
    assert _thr_tune_on_cooldown(fresh, now) is True
    old = {"proposed_at": (now - timedelta(days=20)).isoformat()}
    assert _thr_tune_on_cooldown(old, now) is False
    declined = {"declined_at": (now - timedelta(days=10)).isoformat()}
    assert _thr_tune_on_cooldown(declined, now) is True  # отказ держит 28 дней


# ── джоба: предлагает, но НЕ пишет пороги сама ────────────────────────────────────────
async def test_tuner_job_offers_but_never_writes_thresholds(monkeypatch):
    from sqlalchemy import delete, select

    import core.access as acc
    from bot.keyboards import thr_tune_kb
    from core.config import settings as cfg
    from db.models import UserSettings
    from db.session import Session, init_db
    from scheduler import delivery, jobs

    # Развязка C4: клавиатуру планировщик берёт у порта, заполняет его bot-процесс. thr-tune —
    # единственная джоба, которая при пустом порте МОЛЧИТ целиком (принять предложение можно только
    # тапом), поэтому здесь порт обязателен, иначе тест меряет тишину. Гард — test_scheduler_decoupled.
    monkeypatch.setitem(delivery._BUILDERS, delivery.THRESHOLD_TUNE, thr_tune_kb)

    # A8: thr-tune теперь фильтрует получателей через accessible_accounts_for_user (как все
    # per-account рассылки). Этот тест проверяет ЛОГИКУ предложения, не доступ — фиксируем legacy
    # (иначе гранты, оставленные другими тест-файлами в общей SQLite, в auto-режиме включили бы
    # enforcement и C2-фильтр съел бы аккаунт). Enforced-семантика — в test_jobs_access_filter.py.
    monkeypatch.setattr(cfg, "account_access_mode", "legacy")
    acc._invalidate_enforcement_cache()

    await init_db()
    chat, acct = 6501, "1112223334"
    async with Session() as s:
        await s.execute(delete(UserSettings).where(UserSettings.chat_id == chat))
        await s.commit()

    async def _arecipients():
        return {chat}

    monkeypatch.setattr(jobs, "_recipients", _arecipients)
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [acct])

    async def _fake_client(cid=None):
        return object()

    async def _fake_read(fn, client, cid, *a, **kw):
        if getattr(fn, "__name__", "") == "fetch_by_day":
            # 6 недель волатильного расхода
            daily = []
            for w in range(6):
                base = 20.0 if w % 2 else 80.0
                daily += [(base, 1.0)] * 7
            return SimpleNamespace(rows=_rows(daily))
        return "USD"  # account_currency

    monkeypatch.setattr(jobs, "build_client_async", _fake_client)
    monkeypatch.setattr(jobs, "run_ads_read_call", _fake_read)

    sent: list[tuple[int, str, object]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            sent.append((chat_id, text, reply_markup))

    await jobs.run_threshold_tuning(FakeBot())

    assert sent and sent[0][2] is not None  # предложение с кнопками пришло
    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat))
        ).scalar_one_or_none()
    assert row is not None
    # ДЖОБА порогов НЕ писала — только блоб предложения в ui_prefs (запись порогов = тап «Принять»)
    assert not ((row.alert_thresholds or {}).get("per_account") or {})
    assert any(str(k).startswith("thr_tune_") for k in (row.ui_prefs or {}))

    # повторный прогон в кулдауне — молчит
    sent.clear()
    await jobs.run_threshold_tuning(FakeBot())
    assert sent == []


# ── /myschedule: персист + live; D9 — рантайм-whitelist в персональных расписаниях ────
async def test_save_report_schedule_roundtrip_and_live_off(monkeypatch):
    from sqlalchemy import select

    import bot.main as bm
    from db.models import UserSettings
    from db.session import Session, init_db

    await init_db()
    chat = 6502
    await bm._save_report_schedule(chat, "0 9 * * 1-5")
    async with Session() as s:
        cur = (
            await s.execute(
                select(UserSettings.report_schedule).where(UserSettings.chat_id == chat)
            )
        ).scalar_one_or_none()
    assert cur == "0 9 * * 1-5"
    await bm._save_report_schedule(chat, None)  # off → NULL (вернулись на глобальное)
    async with Session() as s:
        cur = (
            await s.execute(
                select(UserSettings.report_schedule).where(UserSettings.chat_id == chat)
            )
        ).scalar_one_or_none()
    assert cur is None
    # SCHED нет (тесты) → live-применение честно говорит «после рестарта»
    assert await bm._apply_report_schedule_live(None, chat, "0 9 * * *") is False


async def test_register_user_schedules_sees_runtime_whitelist(monkeypatch):
    """D9: оператор из БД-whitelist (/adduser) с личным расписанием получает per-chat джобу."""
    import core.access as ca
    from db.session import init_db
    from scheduler.service import register_user_report_schedules

    await init_db()
    chat = 6503
    import bot.main as bm

    await bm._save_report_schedule(chat, "0 8 * * *")

    async def _wl():
        return {chat}  # env пуст, оператор добавлен рантаймом

    monkeypatch.setattr(ca, "whitelisted_ids", _wl)

    added: list[str] = []

    class FakeSched:
        def add_job(self, fn, trig, **kw):
            added.append(kw.get("id", ""))

    n = await register_user_report_schedules(FakeSched(), bot=None)
    assert n == 1 and added == [f"scheduled_report_{chat}"]

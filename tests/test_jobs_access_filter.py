"""A8/C2: КАЖДАЯ per-account рассылка scheduler фильтрует получателей через
accessible_accounts_for_user — оператор без гранта не получает данные чужого аккаунта.

run_threshold_tuning была единственной такой рассылкой БЕЗ фильтра (утечка порогов чужого
аккаунта). Класс-гард ниже перечисляет ВСЕ per-account джобы: новая обязана попасть в список
(и, значит, использовать фильтр). Плюс функциональный тест thr-tune в enforced-режиме.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scheduler.jobs as jobs  # noqa: E402

# Все джобы, рассылающие ПЕР-АККАУНТ данные операторам. Новую такую джобу ОБЯЗАТЕЛЬНО добавить
# сюда — тест потребует от неё фильтр доступа (иначе утечка метрик/порогов чужого аккаунта).
PER_ACCOUNT_BROADCAST_JOBS = [
    "run_scheduled_report",
    "run_anomaly_check",
    "run_recommendations_digest",
    "run_business_digest",
    "run_threshold_tuning",
]


def test_all_per_account_jobs_filter_by_access():
    """Класс-гард A8: каждая per-account рассылка ссылается на accessible_accounts_for_user."""
    for name in PER_ACCOUNT_BROADCAST_JOBS:
        fn = getattr(jobs, name, None)
        assert fn is not None, f"джоба {name} не найдена в scheduler.jobs"
        src = inspect.getsource(fn)
        assert "accessible_accounts_for_user" in src, (
            f"per-account рассылка {name} должна фильтровать получателей через "
            "accessible_accounts_for_user (иначе утечка данных чужого аккаунта)"
        )


async def _fake_client_async(*a, **k):
    return None


def _arecipients(value):
    async def _f():
        return set(value)

    return _f


async def test_threshold_tuning_enforced_skips_unauthorized_account(monkeypatch):
    """Функциональный A8: в enforced-режиме thr-tune НЕ шлёт пороги аккаунта, на который у оператора
    нет гранта (раньше слал по всем аккаунтам всем whitelisted — утечка)."""
    import core.access as acc
    import reports.queries as rq
    import scheduler.threshold_tuner as tuner
    from ads import read as ads_read
    from bot.keyboards import thr_tune_kb
    from core.access import grant_account_access, revoke_account_access
    from core.config import settings as cfg
    from db.session import init_db
    from scheduler import delivery

    # Порт доставки (развязка C4): без заполненного порта thr-tune молчит ПО ВСЕМ аккаунтам, и тест
    # про грант проходил бы вхолостую — «не пришло по B» выполнялось бы по неверной причине.
    monkeypatch.setitem(delivery._BUILDERS, delivery.THRESHOLD_TUNE, thr_tune_kb)

    A, B = "1112223334", "2223334445"
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)
    monkeypatch.setattr(jobs, "_recipients", _arecipients({3}))
    monkeypatch.setattr(jobs, "_thresholds_by_chat", _empty_thr)
    monkeypatch.setattr(jobs, "_ui_pref_blob", _anone)
    monkeypatch.setattr(jobs, "_save_ui_pref_blob", _anoop)
    # чтения через run_ads_read_call: подменяем источники (импортируются внутри функции)
    monkeypatch.setattr(
        rq, "fetch_by_day", lambda client, acct, period, seg: _DayBd(), raising=False
    )
    monkeypatch.setattr(ads_read, "account_currency", lambda client, acct: "USD", raising=False)
    monkeypatch.setattr(tuner, "weekly_buckets", lambda rows: ([10.0, 12.0, 8.0], [1.0, 1.0, 1.0]))
    monkeypatch.setattr(
        tuner,
        "suggest_thresholds",
        lambda costs, convs, cur: {
            "spend_spike_pct": 150.0,
            "conv_drop_pct": 60.0,
            "min_spend": 5.0,
        },
    )
    monkeypatch.setattr(cfg, "account_access_mode", "enforced")
    acc._invalidate_enforcement_cache()

    await init_db()
    await grant_account_access(3, A)  # грант ТОЛЬКО на A
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    try:
        await jobs.run_threshold_tuning(FakeBot())
    finally:
        await revoke_account_access(3, A)
        acc._invalidate_enforcement_cache()

    # предложение порогов пришло ТОЛЬКО по A (грант), не по B (нет гранта)
    assert sent, "по разрешённому аккаунту предложение должно прийти"
    for _chat, text in sent:
        assert B not in text  # чужой аккаунт не утёк


async def _empty_thr(recips):
    return {}


async def _anone(*a, **k):
    return None


async def _anoop(*a, **k):
    return None


class _DayBd:
    rows = [object(), object(), object()]

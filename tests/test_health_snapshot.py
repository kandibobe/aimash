"""N1.1: снапшоты health-score (account_health_snapshot) — субстрат трендов /audit.

Инварианты: запись идемпотентна per (customer_id, snapshot_date); нет активности → не пишем;
previous_snapshot фильтрует по period_days (окна сравнимы); запись fail-OPEN (сбой БД не роняет
аудит); тренд Δ ТОЛЬКО в пределах одной score_model_version (N1.0a) — иначе «н/д»; ретеншн-purge
чистит старые строки. ТОЛЬКО локальная БД — Google Ads не трогается.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.snapshot import previous_snapshot, record_snapshot  # noqa: E402
from db.session import Session, init_db  # noqa: E402


def _result(score=85, grade="B", version="v1", cid="7753643025", **kw):
    return SimpleNamespace(
        has_activity=True,
        customer_id=cid,
        score=score,
        grade=grade,
        total_spend=kw.get("total_spend", 1000.0),
        at_risk=kw.get("at_risk", 500.0),
        currency=kw.get("currency", "USD"),
        families=kw.get("families", {"waste": {"count": 1, "at_risk": 500.0, "penalty": 15.0}}),
        score_model_version=version,
    )


async def _rows(cid: str):
    from sqlalchemy import select

    from db.models import AccountHealthSnapshot

    async with Session() as s:
        return (
            (
                await s.execute(
                    select(AccountHealthSnapshot)
                    .where(AccountHealthSnapshot.customer_id == cid)
                    .order_by(AccountHealthSnapshot.snapshot_date)
                )
            )
            .scalars()
            .all()
        )


async def test_record_snapshot_idempotent_per_cid_date():
    await init_db()
    cid = "1010101010"
    assert await record_snapshot(
        _result(score=80, cid=cid), snapshot_date="2026-07-01", period_days=30
    )
    assert await record_snapshot(
        _result(score=85, cid=cid), snapshot_date="2026-07-01", period_days=30
    )
    rows = await _rows(cid)
    assert len(rows) == 1  # upsert, не дубль
    assert rows[0].score == 85 and rows[0].grade == "B"
    assert rows[0].family_penalty == {"waste": 15.0}  # только агрегат, без имён кампаний
    assert rows[0].score_model_version == "v1"


async def test_different_windows_same_day_do_not_clobber():
    """Ревью N1.1: /audit 7 в тот же день НЕ перезаписывает 30-дневный снапшот — окно в ключе."""
    await init_db()
    cid = "6060606060"
    await record_snapshot(_result(score=80, cid=cid), snapshot_date="2026-07-08", period_days=30)
    await record_snapshot(_result(score=95, cid=cid), snapshot_date="2026-07-08", period_days=7)
    rows = await _rows(cid)
    assert len(rows) == 2  # две строки, не clobber
    assert {(r.period_days, r.score) for r in rows} == {(30, 80), (7, 95)}
    # тренд каждого окна видит СВОЮ базу
    prev30 = await previous_snapshot(cid, before_date="2026-07-09", period_days=30)
    prev7 = await previous_snapshot(cid, before_date="2026-07-09", period_days=7)
    assert prev30.score == 80 and prev7.score == 95


async def test_record_snapshot_skips_empty_account():
    await init_db()
    cid = "2020202020"
    empty = SimpleNamespace(has_activity=False, score=None, customer_id=cid)
    assert not await record_snapshot(empty, snapshot_date="2026-07-01", period_days=30)
    assert await _rows(cid) == []


async def test_previous_snapshot_latest_older_same_window():
    await init_db()
    cid = "3030303030"
    await record_snapshot(_result(score=70, cid=cid), snapshot_date="2026-06-20", period_days=30)
    await record_snapshot(_result(score=75, cid=cid), snapshot_date="2026-06-27", period_days=30)
    await record_snapshot(_result(score=99, cid=cid), snapshot_date="2026-06-28", period_days=7)
    prev = await previous_snapshot(cid, before_date="2026-07-04", period_days=30)
    assert prev is not None and prev.score == 75  # последний из СТРОГО более ранних, окно 30
    assert await previous_snapshot(cid, before_date="2026-06-20", period_days=30) is None
    # другое окно (7 дней) не подмешивается в 30-дневный тренд
    prev7 = await previous_snapshot(cid, before_date="2026-07-04", period_days=7)
    assert prev7 is not None and prev7.score == 99


async def test_record_snapshot_fail_open_on_db_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("db.session.Session", boom)
    assert await record_snapshot(_result(), snapshot_date="2026-07-01", period_days=30) is False
    assert await previous_snapshot("1", before_date="2026-07-01", period_days=30) is None


async def test_purge_includes_health_snapshots(monkeypatch):
    await init_db()
    from core.config import settings
    from db.models import AccountHealthSnapshot
    from scheduler.jobs import purge_stale_rows

    cid = "4040404040"
    async with Session() as s:
        s.add(
            AccountHealthSnapshot(
                customer_id=cid,
                snapshot_date="2020-01-01",
                score=50,
                grade="D",
                score_model_version="old",
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await s.commit()
    monkeypatch.setattr(settings, "account_health_retain_days", 30)
    res = await purge_stale_rows()
    assert res["account_health_snapshot"] >= 1
    assert await _rows(cid) == []
    # 0 ⇒ ВЫКЛ (fail-safe: ничего не удаляем)
    await record_snapshot(_result(cid=cid), snapshot_date="2020-01-02", period_days=30)
    monkeypatch.setattr(settings, "account_health_retain_days", 0)
    res2 = await purge_stale_rows()
    assert res2["account_health_snapshot"] == 0
    assert len(await _rows(cid)) == 1


async def test_audit_trend_line_same_version_delta_and_cross_version_na(monkeypatch):
    """Δ показываем ТОЛЬКО в пределах одной версии score-модели; смена версии → честное «н/д»."""
    await init_db()
    import bot.main as bm

    cid = "5050505050"

    async def fake_date(client, acct):
        return "2026-07-08"

    monkeypatch.setattr(bm, "_account_local_date", fake_date)
    # первый прогон: базы нет → строки нет (и снапшот записан)
    line0 = await bm._audit_trend_line(
        _result(score=80, cid=cid, version="vX"), None, cid, 30, "ru"
    )
    assert line0 == ""

    # второй прогон следующим днём, та же версия → Δ +5
    async def fake_date2(client, acct):
        return "2026-07-09"

    monkeypatch.setattr(bm, "_account_local_date", fake_date2)
    line1 = await bm._audit_trend_line(
        _result(score=85, cid=cid, version="vX"), None, cid, 30, "ru"
    )
    assert "+5" in line1 and "80/100" in line1 and "2026-07-08" in line1

    # третий прогон с ДРУГОЙ версией модели → «н/д», не ложная дельта
    async def fake_date3(client, acct):
        return "2026-07-10"

    monkeypatch.setattr(bm, "_account_local_date", fake_date3)
    line2 = await bm._audit_trend_line(
        _result(score=60, cid=cid, version="vY"), None, cid, 30, "ru"
    )
    assert "н/д" in line2 and "60" not in line2.replace("н/д", "")

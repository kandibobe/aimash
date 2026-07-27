"""Волна 4: детектор деградации и контур наблюдения (режим shadow).

Пять свойств, каждое из которых стоит денег, если сломается:

1. **`insufficient` НИКОГДА не повышается до `degraded`.** Мало данных — не доказательство вреда.
   Кампания, месяц простоявшая на паузе, имеет нулевую базу, и любой запуск формально «превышает»
   её бесконечно; такой вердикт — артефакт метода, а не сигнал.
2. **База — тот же час того же дня недели.** Иначе нормальный вечерний пик объявляется деградацией.
3. **База масштабируется на ожидаемый эффект.** Иначе детектор ловит собственную причину: подняли
   бюджет на 20%, расход вырос на 20%, «деградация» — и так каждый раз.
4. **Наблюдение заводится только за тем, что реально откатывается.** Иначе копятся вердикты, на
   которые нечем ответить.
5. **Ни одна ветка контура не мутирует аккаунт.** В `shadow` наружу не уходит вообще ничего.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.read import HourlyPoint  # noqa: E402
from confirm.reverse import ROLLBACKABLE_OPS, reverse_spec  # noqa: E402
from core.config import settings  # noqa: E402
from db.models import RollbackWatch  # noqa: E402
from db.session import Session  # noqa: E402
from scheduler import rollback  # noqa: E402


def _pt(day: str, hour: int, cost: float, clicks: int = 10) -> HourlyPoint:
    return HourlyPoint(
        day=day,
        hour=hour,
        cost_micros=int(cost * 1e6),
        clicks=clicks,
        impressions=100,
        conversions=0.0,
    )


def _history(hours: list[int], cost: float, *, weeks: int = 4, base: str = "2026-07-27") -> list:
    """Те же часы того же дня недели за `weeks` предыдущих недель (base — воскресенье)."""
    d0 = datetime.fromisoformat(base).date()
    out = []
    for w in range(1, weeks + 1):
        day = (d0 - timedelta(weeks=w)).isoformat()
        out += [_pt(day, h, cost) for h in hours]
    return out


# ── 1. Чистый детектор ───────────────────────────────────────────────────────────────────────
def test_normal_spend_is_ok():
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 5.0) for h in hours]
    v = rollback.assess(observed, _history(hours, 5.0))
    assert v.state == "ok"
    assert v.metric == "cost_micros"  # мерили расход, и вердикт это называет


def test_sustained_overspend_is_degraded():
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 20.0) for h in hours]
    v = rollback.assess(observed, _history(hours, 5.0))
    assert v.state == "degraded"
    assert v.observed > v.threshold > v.baseline > 0
    assert "расход за окно" in v.reason


def test_baseline_is_same_hour_same_weekday_not_daily_average():
    """Вечерний пик — норма для этого часа. База, собранная по ДРУГИМ часам, объявила бы его
    деградацией; поэтому история из чужих часов не должна засчитываться вовсе."""
    hours = [20, 21, 22, 23]
    observed = [_pt("2026-07-27", h, 30.0) for h in hours]
    # История есть, но по утренним часам — для вечерних слотов базы нет.
    v = rollback.assess(observed, _history([6, 7, 8, 9], 2.0))
    assert v.state == "insufficient"
    assert "базы сравнения нет" in v.reason
    # А со СВОЕЙ историей тот же расход — норма.
    assert rollback.assess(observed, _history(hours, 30.0)).state == "ok"


def test_zero_baseline_never_becomes_degraded():
    """Кампания стояла на паузе: в те же часы прошлых недель расхода не было. Формально рост
    бесконечный — по сути сравнивать не с чем."""
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 50.0) for h in hours]
    v = rollback.assess(observed, _history(hours, 0.0))
    assert v.state == "insufficient"
    assert v.state != "degraded"


def test_thin_history_is_insufficient_not_degraded():
    """Одна прошлая неделя — не выборка: MAD по одному значению равен нулю, и любой рост стал бы
    «отклонением в бесконечность MAD»."""
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 40.0) for h in hours]
    v = rollback.assess(observed, _history(hours, 5.0, weeks=1))
    assert v.state == "insufficient"


def test_pennies_never_trigger_a_verdict():
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 0.05) for h in hours]
    v = rollback.assess(observed, _history(hours, 0.001))
    assert v.state == "insufficient"
    assert "ниже порога значимости" in v.reason


def test_empty_window_is_insufficient():
    assert rollback.assess([], _history([10], 5.0)).state == "insufficient"


def test_single_spiky_hour_does_not_flip_the_verdict():
    """Агрегатный вердикт: один шумный час не переворачивает решение, устойчивый перерасход —
    переворачивает. Иначе окно из 4 часов и окно из 8 давали бы разный ответ на тех же данных."""
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", 10, 9.0)] + [_pt("2026-07-27", h, 5.0) for h in hours[1:]]
    hist = _history(hours, 5.0)
    assert rollback.assess(observed, hist).state == "ok"
    # А ровный перерасход той же суммарной величины — переворачивает.
    assert rollback.assess([_pt("2026-07-27", h, 6.0) for h in hours], hist).state == "ok"
    assert rollback.assess([_pt("2026-07-27", h, 12.0) for h in hours], hist).state == "degraded"


def test_detector_does_not_fire_on_its_own_cause():
    """Бюджет подняли на 20% — расход вырос на 20%. Это НЕ деградация: ровно за этим изменение и
    делалось. Без масштабирования базы детектор откатывал бы каждое повышение бюджета."""
    hours = [10, 11, 12, 13]
    hist = _history(hours, 5.0)
    observed = [_pt("2026-07-27", h, 6.0) for h in hours]  # +20%
    assert rollback.assess(observed, hist, ratio=1.2).state == "ok"
    # И наоборот: заказали +20%, а расход утроился — вот это сигнал.
    hot = [_pt("2026-07-27", h, 18.0) for h in hours]
    v = rollback.assess(hot, hist, ratio=1.2)
    assert v.state == "degraded"
    assert "масштабирована" in v.reason
    # Снижение бюджета вдвое: база опускается вместе с ним, прежний расход становится перерасходом.
    assert rollback.assess([_pt("2026-07-27", h, 5.0) for h in hours], hist, ratio=0.5).state == (
        "degraded"
    )


def test_expected_ratio_is_computed_from_the_attested_snapshot():
    """Коэффициент берётся из пары «было → станет», а не из текста и не из намерения модели."""
    before = {"kind": "budget", "before_micros": 10_000_000}
    r = rollback.expected_ratio(
        "update_budget", {"mode": "increase_by_percent", "value": 20}, before
    )
    assert r is not None and abs(r - 1.2) < 1e-6
    r = rollback.expected_ratio("update_budget", {"mode": "set_to", "value": 5.0}, before)
    assert r is not None and abs(r - 0.5) < 1e-6
    # Разнородные ставки по группам — одним числом не описываются (как и в `reverse_spec`).
    multi = {"kind": "bid", "before_micros": [1_000_000, 2_000_000]}
    assert rollback.expected_ratio("update_bid", {"mode": "set_to", "value": 2.0}, multi) is None
    # Снимка нет / режим неизвестен / операция не наблюдаемая — эффект неизвестен, а не «единица».
    assert rollback.expected_ratio("update_budget", {"mode": "set_to", "value": 1.0}, None) is None
    assert (
        rollback.expected_ratio("update_budget", {"mode": "чепуха", "value": 1.0}, before) is None
    )
    assert (
        rollback.expected_ratio("pause_campaign", {"mode": "set_to", "value": 1.0}, before) is None
    )


def test_watchable_ops_are_a_subset_of_rollbackable():
    """Наблюдать за тем, чего не откатить, — копить вердикты без ответа. Сужение допустимо,
    расширение за пределы обратимого — нет."""
    assert rollback.WATCHABLE_OPS <= ROLLBACKABLE_OPS
    assert rollback.WATCHABLE_OPS  # и оно не пусто — иначе контур мёртв и тест зелён


def test_k_raises_the_bar():
    """Порог — параметр, а не константа: чем выше k, тем консервативнее вердикт."""
    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 9.0) for h in hours]
    hist = _history(hours, 5.0)
    assert rollback.assess(observed, hist, k=1.0).state == "degraded"
    assert rollback.assess(observed, hist, k=50.0).state == "ok"


# ── 2. Наблюдение заводится и убирается ──────────────────────────────────────────────────────
async def test_record_watch_is_idempotent_per_confirmation():
    """UNIQUE держит БД, но повтор не должен и падать: ретрай доставки/дубль джобы — норма."""
    cid = f"w{uuid.uuid4().hex[:10]}"
    assert await rollback.record_watch(
        confirmation_id=cid,
        customer_id="7753643025",
        campaign_id="42",
        operation="update_budget",
        expected_ratio=1.2,
    )
    assert not await rollback.record_watch(
        confirmation_id=cid, customer_id="7753643025", campaign_id="42", operation="update_budget"
    )
    async with Session() as s:
        rows = list(
            (await s.execute(select(RollbackWatch).where(RollbackWatch.confirmation_id == cid)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].state == "watching" and rows[0].mode == "shadow"
    # Ожидаемый эффект сохранён: к моменту вердикта прежнего значения в API уже нет.
    assert rows[0].expected_ratio and abs(rows[0].expected_ratio - 1.2) < 1e-6


async def test_record_watch_survives_db_failure(monkeypatch: pytest.MonkeyPatch):
    """Наблюдение — аудит, а не денежный путь: его отказ не смеет ронять применённую мутацию."""
    import db.session as dbs

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbs, "Session", boom)
    assert (
        await rollback.record_watch(
            confirmation_id=f"w{uuid.uuid4().hex[:8]}",
            customer_id="7753643025",
            campaign_id="1",
            operation="update_budget",
        )
        is False
    )


# ── 3. Джоба: shadow пишет вердикт и молчит ──────────────────────────────────────────────────
async def test_shadow_writes_verdict_and_sends_nothing(monkeypatch: pytest.MonkeyPatch):
    cid = f"w{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with Session() as s:
        from db.session import db_dt

        s.add(
            RollbackWatch(
                confirmation_id=cid,
                customer_id="7753643025",
                campaign_id="42",
                operation="update_budget",
                applied_at=db_dt(now - timedelta(hours=6)),
                window_until=db_dt(now - timedelta(hours=1)),
                mode="shadow",
                state="watching",
                created_at=db_dt(now - timedelta(hours=6)),
            )
        )
        await s.commit()

    hours = [10, 11, 12, 13]
    observed = [_pt("2026-07-27", h, 20.0) for h in hours]
    monkeypatch.setattr(rollback, "build_client_async", _fake_client)
    monkeypatch.setattr(
        rollback,
        "_observe_one",
        lambda *a, **k: _coro(rollback.assess(observed, _history(hours, 5.0))),
    )
    sent: list = []

    class _Bot:
        async def send_message(self, *a, **k):
            sent.append(a)

    res = await rollback.run_rollback_watch(_Bot())
    assert res["degraded"] >= 1
    assert sent == []  # shadow: наружу НИЧЕГО

    async with Session() as s:
        row = (
            await s.execute(select(RollbackWatch).where(RollbackWatch.confirmation_id == cid))
        ).scalar_one()
    assert row.state == "verdict_degraded"
    assert row.verdict_json and "cost_micros" in row.verdict_json
    assert row.checked_at is not None
    assert row.acted_confirmation_id is None  # ничего не исполнено


async def test_auto_mode_degrades_to_shadow_not_to_execution(monkeypatch: pytest.MonkeyPatch):
    """`auto` ещё не реализован (Волна 6a). Неизвестный/нереализованный режим обязан вести себя как
    САМЫЙ СЛАБЫЙ из доступных, а не как самый сильный — молчаливое усиление режима исполнения тот
    самый класс, из-за которого настройка «выглядит рабочей»."""
    monkeypatch.setattr(settings, "rollback_watch_mode", "auto")
    res = await rollback.run_rollback_watch(None)  # нет наблюдений — просто не должно исполнять
    assert res["notified"] == 0
    monkeypatch.setattr(settings, "rollback_watch_mode", "чепуха")
    assert (await rollback.run_rollback_watch(None))["notified"] == 0


# ── 4. Наблюдаем только за откатываемым ──────────────────────────────────────────────────────
def test_watch_is_only_for_operations_that_can_be_reversed():
    """Свойство контура, а не детали `ads/service.py`: если `reverse_spec` вернул None, наблюдать
    не за чем — вердикт будет, а ответить на него будет нечем."""
    before = {"kind": "bid", "before_micros": [1_000_000, 2_000_000]}  # разные ставки по группам
    assert "update_bid" in ROLLBACKABLE_OPS
    assert reverse_spec("update_bid", {"campaign": "К"}, before) is None
    ok = {"kind": "bid", "before_micros": [1_000_000, 1_000_000]}
    assert reverse_spec("update_bid", {"campaign": "К"}, ok) is not None


def test_reverse_spec_moved_out_of_bot_layer():
    """Мина C4: фоновый контур зовёт синтез обратной операции, и тянуть за ним aiogram нельзя.
    Модуль обязан импортироваться сам по себе, без `bot`."""
    import subprocess

    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, confirm.reverse; "
            "assert not [m for m in sys.modules if m.startswith('bot')], sorted(sys.modules)[:5]; "
            "print('clean')",
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert r.returncode == 0, r.stderr
    assert "clean" in r.stdout


async def _fake_client(customer_id):  # noqa: ARG001 — подменяет сборку SDK-клиента в джобе
    return object()


def _coro(value):
    async def _c():
        return value

    return _c()

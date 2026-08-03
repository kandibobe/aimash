"""Офлайн-тесты P2 (архитектура+стабильность):
- единый источник денежных порогов (core.limits) → схема и мутации не расходятся (defense-in-depth
  без дублирования константы);
- глобальный семафор конкурентности к Google Ads (core.resilience) держит ≤ ADS_MAX_CONCURRENCY
  одновременных вызовов и переживает смену event loop (pytest даёт свой loop на тест).
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.limits as L  # noqa: E402
import core.resilience as R  # noqa: E402


# ── Единый источник порогов: схема (единицы) и мутации (micros) выводятся из core.limits ──────
def test_money_cap_single_source_no_drift():
    from llm import schemas
    from ads import mutations as mut

    # обе границы — из одного значения; micros = единицы * 1e6 (без повтора литерала)
    assert L.MONEY_MAX_MICROS == L.MONEY_MAX_UNITS * 1_000_000
    assert schemas.MAX_AMOUNT == L.MONEY_MAX_UNITS  # слой валидации ввода модели
    assert mut.MAX_AMOUNT_MICROS == L.MONEY_MAX_MICROS  # слой у границы SDK
    assert mut.MAX_AMOUNT_MICROS == schemas.MAX_AMOUNT * 1_000_000  # две границы согласованы


def test_radius_cap_single_source():
    from llm.schemas import SetGeoProximity
    from ads import mutations as mut

    assert mut.MAX_RADIUS_KM == L.MAX_RADIUS_KM
    # схема SetGeoProximity ограничивает radius_km тем же потолком (le=MAX_RADIUS_KM)
    field = SetGeoProximity.model_fields["radius_km"]
    le = next((m for m in field.metadata if getattr(m, "le", None) is not None), None)
    assert le is not None and le.le == L.MAX_RADIUS_KM


# ── Семафор конкурентности к Google Ads ──────────────────────────────────────────────────────
async def test_ads_semaphore_bounds_concurrency(monkeypatch):
    monkeypatch.setattr(R, "ADS_MAX_CONCURRENCY", 2)  # потолок 2
    monkeypatch.setattr(R, "ADS_WAIT_MULTIPLIER", 0.0)  # без задержек ретрая
    monkeypatch.setattr(R, "ADS_WAIT_MAX", 0.0)
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0}

    def fn():
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)  # удерживаем слот, чтобы конкуренция проявилась
        with lock:
            state["cur"] -= 1
        return "ok"

    res = await asyncio.gather(*[R.run_ads_call(fn, label=f"c{i}") for i in range(6)])
    assert res == ["ok"] * 6
    assert state["peak"] == 2  # 6 задач, но семафор не пустил больше 2 одновременно


def test_ads_semaphore_survives_loop_change():
    # Семафор кэшируется per-loop: повторный вызов в ДРУГОМ event loop не падает RuntimeError.
    # Тест синхронный, чтобы дважды вызвать asyncio.run (свой loop на каждый) — вложенный
    # asyncio.run внутри уже работающего loop был бы ошибкой.
    async def one():
        return await R.run_ads_call(lambda: "ok", label="x")

    assert asyncio.run(one()) == "ok"
    assert asyncio.run(one()) == "ok"  # новый loop → семафор пересоздан, без ошибки

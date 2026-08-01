"""B1-4: суточный blast-radius кап ПОВЫШЕНИЙ бюджета (ads.mutations._require_budget_blast_radius).

Что закрепляется:
  · счётный кап и кап по сумме считают только ПОВЫШЕНИЯ; понижение не блокируется НИКОГДА —
    путь «срезать расход» обязан оставаться открытым даже при выеденном капе;
  · окно 24 ч меряется по decided_at (момент ИСПОЛНЕНИЯ, штампует claim), старые траты выпадают;
  · отказ капа стоит ДО claim — подтверждение не сжигается (завтра та же карточка исполнима);
  · fail-closed: стор без метода истории, упавшая БД, несчитаемая дельта ТЕКУЩЕГО черновика — отказ;
    несчитаемая строка ИСТОРИИ консервативно тратит СЧЁТНЫЙ кап (могла быть повышением).

Реальный ConfirmStore на temp SQLite (conftest). БД living-процессная и общая на сессию, поэтому
каждый тест работает со СВОИМ customer_id — иначе окна 24 ч соседних тестов складывались бы.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import update  # noqa: E402

from ads.mutations import (  # noqa: E402
    _budget_delta_micros,
    _require_budget_blast_radius,
    _require_confirmation,
)
from confirm.store import ConfirmStore  # noqa: E402
from conftest import FakeConfirmStore, FakeProposal, attested  # noqa: E402
from core.config import settings  # noqa: E402
from core.killswitch import GateRefusal  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, db_dt, init_db  # noqa: E402

OWNER = 100
OP = "update_budget"


def _cust() -> str:
    """Свой аккаунт на тест: окно 24 ч группируется по customer_id, соседи не должны течь."""
    return f"9{uuid.uuid4().int % 10**9:09d}"


def _params(mode: str = "increase_by_percent", value: float = 20.0, before: int = 10_000_000):
    return attested(
        {"campaign": "X", "mode": mode, "value": value},
        {"kind": "budget", "before_micros": before},
    )


async def _mk(
    store: ConfirmStore,
    cust: str,
    *,
    params: dict | None = None,
    operation: str = OP,
) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=cust,
        params=params if params is not None else _params(),
        summary="s",
        chat_id=OWNER,
        user_initiated=True,
    )
    return cid


async def _spend(
    store: ConfirmStore,
    cust: str,
    *,
    params: dict | None = None,
    operation: str = OP,
) -> str:
    """Исполненная трата капа: pending → confirmed → claim (executing, decided_at=now())."""
    cid = await _mk(store, cust, params=params, operation=operation)
    assert await store.confirm(cid, chat_id=OWNER) is True
    assert await store.claim(cid, operation=operation) is not None
    return cid


async def _backdate_decided(cid: str, *, hours: float) -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with Session() as s:
        await s.execute(
            update(Proposal).where(Proposal.confirmation_id == cid).values(decided_at=db_dt(old))
        )
        await s.commit()


@pytest.fixture(autouse=True)
def _caps_off(monkeypatch):
    """Базовая линия каждого теста — кап ВЫКЛЮЧЕН (dev-дефолт); тест включает нужный порог сам."""
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 0)
    monkeypatch.setattr(settings, "daily_budget_increase_cap_units", 0.0)


# ── Дельта из снимка (юнит) ─────────────────────────────────────────────────────────
def test_delta_from_attested_snapshot():
    assert _budget_delta_micros(_params("increase_by_percent", 20.0, 10_000_000)) == 2_000_000
    assert _budget_delta_micros(_params("decrease_by_percent", 50.0, 10_000_000)) == -5_000_000
    # Нет mode/value ⇒ None; нет аттестации ⇒ None (снимок из «сырых» params не принимается).
    assert _budget_delta_micros(attested({"campaign": "X"}, {"kind": "budget"})) is None
    assert _budget_delta_micros({"campaign": "X", "mode": "set_to", "value": 5.0}) is None


def test_new_campaign_budget_is_full_positive_delta():
    assert _budget_delta_micros({"budget_daily_micros": 7_500_000}) == 7_500_000


def test_delta_prefers_snapshot_after_micros():
    """`after_micros` снимка (посчитан read_state с РЕАЛЬНОЙ валютой — его видел человек на
    карточке) важнее пересчёта mode/value с дефолтным округлением; без mode/value дельта из
    снимка всё равно есть (старый путь вернул бы None → ложный fail-closed отказ)."""
    p = attested(
        {"campaign": "X", "mode": "decrease_by_percent", "value": 50.0},
        {"kind": "budget", "before_micros": 10_000_000, "after_micros": 13_000_000},
    )
    assert _budget_delta_micros(p) == 3_000_000  # снимок, а не пересчёт по mode/value
    p2 = attested(
        {"campaign": "X"},
        {"kind": "budget", "before_micros": 10_000_000, "after_micros": 12_000_000},
    )
    assert _budget_delta_micros(p2) == 2_000_000  # mode/value отсутствуют — снимка достаточно


# ── Счётный кап ─────────────────────────────────────────────────────────────────────
async def test_count_cap_blocks_next_increase_and_keeps_confirmation(monkeypatch):
    """Кап исчерпан ⇒ следующее повышение отклонено ДО claim: черновик остаётся confirmed
    (не сожжён), причина в ошибке — кап, а не «нет подтверждения»."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 2)
    store, cust = ConfirmStore(), _cust()
    await _spend(store, cust)
    await _spend(store, cust)

    cid = await _mk(store, cust)
    assert await store.confirm(cid, chat_id=OWNER) is True
    # GateRefusal — «черновик жив»: кнопочный путь по этому типу возвращает его в pending.
    with pytest.raises(GateRefusal, match="кап"):
        await _require_confirmation(store, cid, OP)
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"


async def test_new_campaign_budget_consumes_same_daily_count_cap(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    await _spend(
        store,
        cust,
        operation="create_search_campaign",
        params={"budget_daily_micros": 8_000_000},
    )

    cid = await _mk(store, cust)
    assert await store.confirm(cid, chat_id=OWNER) is True
    with pytest.raises(GateRefusal, match="кап"):
        await _require_confirmation(store, cid, OP)


async def test_decrease_never_blocked_even_when_cap_exhausted(monkeypatch):
    """Понижение проходит при полностью выеденном капе — кап существует, чтобы ограничить РОСТ
    расхода, и не смеет мешать его срезать."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    await _spend(store, cust)  # кап 1/1 — выеден

    cid = await _mk(store, cust, params=_params("decrease_by_percent", 30.0))
    assert await store.confirm(cid, chat_id=OWNER) is True
    claimed = await _require_confirmation(store, cid, OP)
    assert claimed.status == "executing"


async def test_window_24h_old_spend_does_not_count(monkeypatch):
    """Трата старше окна выпадает: кап суточный, а не пожизненный."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    old = await _spend(store, cust)
    await _backdate_decided(old, hours=25)

    cid = await _mk(store, cust)
    assert await store.confirm(cid, chat_id=OWNER) is True
    claimed = await _require_confirmation(store, cid, OP)
    assert claimed.status == "executing"


async def test_history_row_with_uncomputable_delta_spends_count_cap(monkeypatch):
    """Строка истории, чью дельту не посчитать (снимка/полей нет), тратит СЧЁТНЫЙ кап
    консервативно: она могла быть повышением, и «не знаю» не должно освобождать кап."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    await _spend(store, cust, params=attested({"campaign": "X"}, {"kind": "budget"}))

    cid = await _mk(store, cust)
    assert await store.confirm(cid, chat_id=OWNER) is True
    with pytest.raises(PermissionError, match="кап"):
        await _require_confirmation(store, cid, OP)


# ── Кап по сумме ────────────────────────────────────────────────────────────────────
async def test_sum_cap_blocks_over_and_passes_under(monkeypatch):
    """Кап по СУММЕ прироста (единицы валюты): история +2+2, текущий +2 при лимите 5 — отказ;
    при лимите 7 — проходит. Счётный кап при этом выключен (пороги независимы)."""
    await init_db()
    store, cust = ConfirmStore(), _cust()
    await _spend(store, cust)  # +2 (20% от 10 units)
    await _spend(store, cust)  # +2

    monkeypatch.setattr(settings, "daily_budget_increase_cap_units", 5.0)
    cid = await _mk(store, cust)
    assert await store.confirm(cid, chat_id=OWNER) is True
    with pytest.raises(GateRefusal, match="СУММЫ"):
        await _require_confirmation(store, cid, OP)

    monkeypatch.setattr(settings, "daily_budget_increase_cap_units", 7.0)
    claimed = await _require_confirmation(store, cid, OP)  # то же подтверждение живо
    assert claimed.status == "executing"


# ── Fail-closed края ────────────────────────────────────────────────────────────────
async def test_store_without_history_method_is_refused(monkeypatch):
    """Стор без recent_money_params при ВКЛЮЧЁННОМ капе = отказ, а не «истории нет — проходи»."""
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)

    class NoHistoryStore:
        async def get_confirmed(self, confirmation_id):
            return FakeProposal(OP, user_initiated=True, params=_params())

    with pytest.raises(PermissionError, match="fail-closed"):
        await _require_budget_blast_radius(NoHistoryStore(), "cid", OP)


async def test_store_history_error_is_refused(monkeypatch):
    """БД упала на чтении истории ⇒ кап проверить нельзя ⇒ отказ (правило 10), а не пропуск."""
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)

    class BrokenStore(FakeConfirmStore):
        async def recent_money_params(self, customer_id, *, operations, window_hours=24):
            raise RuntimeError("db down")

    store = BrokenStore(FakeProposal(OP, user_initiated=True, params=_params()))
    # GateRefusal: сбой БД временный — черновик не дефектен, повтор ✅ уместен.
    with pytest.raises(GateRefusal, match="недоступна"):
        await _require_budget_blast_radius(store, "cid", OP)


async def test_current_proposal_uncomputable_delta_is_refused(monkeypatch):
    """Дельту ТЕКУЩЕГО черновика не посчитать (нет полей) ⇒ отказ: пропустить «не знаю сколько»
    значило бы, что кап обходится порчей params."""
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 5)
    store = FakeConfirmStore(
        FakeProposal(
            OP, user_initiated=True, params=attested({"campaign": "X"}, {"kind": "budget"})
        )
    )
    with pytest.raises(PermissionError, match="не вычислим") as ei:
        await _require_budget_blast_radius(store, "cid", OP)
    # ДЕФЕКТ черновика (не внешняя причина): НЕ GateRefusal — сжигание и «создай заново» корректны.
    assert not isinstance(ei.value, GateRefusal)


# ── Выключенный кап и чужие операции — no-op ────────────────────────────────────────
async def test_caps_off_is_noop_even_without_store_methods():
    """Оба порога 0 ⇒ гейт не трогает стор вовсе (dev/test-дефолт; в prod max_ops автоподнят)."""
    await _require_budget_blast_radius(object(), "cid", OP)  # у object() нет ни одного метода


async def test_non_budget_operation_ignored(monkeypatch):
    """Кап — только про операции из BUDGET_INCREASE_OPS; pause_campaign проходит мимо гейта."""
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    await _require_budget_blast_radius(object(), "cid", "pause_campaign")


# ── Фиксы ревью 2026-07-30 (MAJOR-2/3/4) ────────────────────────────────────────────
async def test_needs_review_history_row_still_spends_cap(monkeypatch):
    """MAJOR-3: строка, уведённая в needs_review (реконсиляция зависшего executing /
    расхождение пост-проверки), НЕ выпадает из счёта капа: «могло примениться» для капа
    значит «применилось» — иначе каждая зависшая мутация возвращала бы кап."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    spent = await _spend(store, cust)  # executing
    assert await store.mark_needs_review(spent, error="таймаут SDK") is True  # → needs_review

    cid = await _mk(store, cust)
    assert await store.confirm(cid, chat_id=OWNER) is True
    with pytest.raises(GateRefusal, match="кап"):
        await _require_confirmation(store, cid, OP)


async def test_concurrent_confirmations_serialize_under_gate_lock(monkeypatch):
    """MAJOR-2: конкурентные подтверждения (aiogram-таски одного event loop) сериализуются
    _BUDGET_GATE_LOCK: второй видит claim первого в истории и получает отказ. Без лока оба
    прочитали бы пустую историю ДО чужого claim — и кап при max_ops=1 пропустил бы оба."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    cids = []
    for _ in range(2):
        cid = await _mk(store, cust)
        assert await store.confirm(cid, chat_id=OWNER) is True
        cids.append(cid)

    results = await asyncio.gather(
        *(_require_confirmation(store, c, OP) for c in cids), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, GateRefusal)]
    assert len(ok) == 1 and len(refused) == 1  # ровно один прошёл, второй — об кап


async def test_set_to_apparent_decrease_passes_rollback_precedent(monkeypatch):
    """MAJOR-4 (решение — документированный residual, не блок): set_to со «станет ниже было»
    проходит выеденный кап. Так строятся ВСЕ откаты (confirm/reverse), и путь «откатить» обязан
    оставаться открытым — тот же прецедент, что у freshness (не сверяет «было» для set_to).
    Остаток (set_to по устаревшей базе может быть фактическим повышением) ограничен L3-TTL
    согласия и MONEY_MAX; описан в докстринге _require_budget_blast_radius и docs/SECURITY.md."""
    await init_db()
    monkeypatch.setattr(settings, "daily_budget_increase_max_ops", 1)
    store, cust = ConfirmStore(), _cust()
    await _spend(store, cust)  # кап 1/1 — выеден

    params = attested(
        {"campaign": "X", "mode": "set_to", "value": 5.0},
        {"kind": "budget", "before_micros": 10_000_000, "after_micros": 5_000_000},
    )
    cid = await _mk(store, cust, params=params)
    assert await store.confirm(cid, chat_id=OWNER) is True
    claimed = await _require_confirmation(store, cid, OP)  # откат-предложение не заблокировано
    assert claimed.status == "executing"

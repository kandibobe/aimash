"""Доп.2A: окно ПОСТ-проверки применённой мутации (execute_confirmed) + персистентный флаг.

Это чужие деньги: после «✅ применено» никто не перечитывал аккаунт, а `_assert_no_drift` ловит
дрейф лишь ДО применения (TOCTOU). Здесь проверяем, что ПОСЛЕ apply код READ-ONLY перечитывает
аккаунт (`_verify_applied`) и при расхождении дописывает флаг в result + переводит черновик в
needs_review (`record_verification`). Инварианты денежного пути НЕ ослаблены:

- `_verify_applied` — чистое ЧТЕНИЕ: `ads.mutations` не вызывается (golden rule #3);
- флаг `verification` дописывается ТОЛЬКО при verified=False (контракт result для happy-path цел);
- сбой самой проверки не откатывает применённую мутацию (degrade → verified=None, лог).

Без живого Google Ads: build_client_async/resolve.* подменяются monkeypatch'ем, БД — временный
SQLite (tests/conftest.py).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
import ads.service as svc  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402


def _patch_client(monkeypatch):
    async def _fake_client(cid=None):
        return object()

    monkeypatch.setattr(svc, "build_client_async", _fake_client)


# ── _verify_applied: READ-ONLY сверка фактического значения с ожидаемым «станет» ──────
async def test_verify_budget_match_true(monkeypatch):
    _patch_client(monkeypatch)
    monkeypatch.setattr(
        svc.resolve,
        "find_campaign_by_name",
        lambda c, cid, name: SimpleNamespace(budget_micros=48_000_000),
    )
    before = {"kind": "budget", "before_micros": 40_000_000, "after_micros": 48_000_000}
    out = await svc._verify_applied(
        "update_budget", {"campaign": "X", "_before": before}, DRAFT_ACCOUNT_ID
    )
    assert out["verified"] is True
    assert out["expected"] == 48_000_000 and out["actual"] == 48_000_000


async def test_verify_budget_mismatch_false(monkeypatch):
    _patch_client(monkeypatch)
    # SDK «успех», но фактический бюджет не тот (частичный сбой/гонка) → verified=False
    monkeypatch.setattr(
        svc.resolve,
        "find_campaign_by_name",
        lambda c, cid, name: SimpleNamespace(budget_micros=40_000_000),
    )
    before = {"kind": "budget", "before_micros": 40_000_000, "after_micros": 48_000_000}
    out = await svc._verify_applied(
        "update_budget", {"campaign": "X", "_before": before}, DRAFT_ACCOUNT_ID
    )
    assert out["verified"] is False
    assert out["expected"] == 48_000_000 and out["actual"] == 40_000_000


async def test_verify_gated_on_before_snapshot(monkeypatch):
    """Нет снимка _before (прямой тест-черновик/легаси-строка) → verified=None, не ложный mismatch."""
    _patch_client(monkeypatch)
    out = await svc._verify_applied("update_budget", {"campaign": "X"}, DRAFT_ACCOUNT_ID)
    assert out["verified"] is None and out.get("reason") == "no_before_snapshot"


async def test_verify_status_match_and_mismatch(monkeypatch):
    _patch_client(monkeypatch)
    before = {"kind": "status", "before_status": "ENABLED"}
    # применили pause → статус стал PAUSED → verified True
    monkeypatch.setattr(
        svc.resolve, "find_campaign_by_name", lambda c, cid, name: SimpleNamespace(status="PAUSED")
    )
    ok = await svc._verify_applied(
        "pause_campaign", {"campaign": "X", "_before": before}, DRAFT_ACCOUNT_ID
    )
    assert ok["verified"] is True and ok["expected"] == "PAUSED"
    # пауза не легла (всё ещё ENABLED) → verified False
    monkeypatch.setattr(
        svc.resolve, "find_campaign_by_name", lambda c, cid, name: SimpleNamespace(status="ENABLED")
    )
    bad = await svc._verify_applied(
        "pause_campaign", {"campaign": "X", "_before": before}, DRAFT_ACCOUNT_ID
    )
    assert bad["verified"] is False and bad["actual"] == "ENABLED"


async def test_verify_bid_length_mismatch_is_none(monkeypatch):
    """Число групп на перечитке разошлось со снимком → позиционно сверять нечем → None (не флаг)."""
    _patch_client(monkeypatch)
    monkeypatch.setattr(
        svc.resolve,
        "find_ad_groups",
        lambda c, cid, name: [SimpleNamespace(cpc_bid_micros=600_000)],  # 1 группа
    )
    before = {"kind": "bid", "after_micros": [600_000, 600_000]}  # снимок помнил 2
    out = await svc._verify_applied(
        "update_bid", {"campaign": "X", "_before": before}, DRAFT_ACCOUNT_ID
    )
    assert out["verified"] is None


async def test_verify_geo_not_verifiable_is_none(monkeypatch):
    """Гео Google нормализует (id/радиусы) — надёжной поэлементной сверки нет → verified=None."""
    _patch_client(monkeypatch)
    out = await svc._verify_applied(
        "set_geo_location", {"campaign": "X", "_before": {"kind": "geo"}}, DRAFT_ACCOUNT_ID
    )
    assert out["verified"] is None and out.get("reason") == "geo_not_verifiable"


async def test_verify_applied_never_calls_mutations(monkeypatch):
    """🔒 golden rule #3: пост-проверка — ЧТЕНИЕ. Ни один apply_* не должен быть вызван из неё."""
    _patch_client(monkeypatch)
    calls = {"n": 0}

    def _tripwire(*a, **k):
        calls["n"] += 1
        raise AssertionError("_verify_applied вызвал мутацию — нарушение read-only")

    for nm in ("apply_pause_campaign", "apply_update_budget", "apply_update_bid"):
        monkeypatch.setattr(mut, nm, _tripwire, raising=False)
    monkeypatch.setattr(
        svc.resolve, "find_campaign_by_name", lambda c, cid, name: SimpleNamespace(status="PAUSED")
    )
    before = {"kind": "status", "before_status": "ENABLED"}
    await svc._verify_applied(
        "pause_campaign", {"campaign": "X", "_before": before}, DRAFT_ACCOUNT_ID
    )
    assert calls["n"] == 0


# ── execute_confirmed: контракт обёртки вокруг _apply_confirmed + _verify_applied ────
class _WrapStore:
    """Мини-store для проверки контракта обёртки (get_confirmed + record_verification)."""

    def __init__(self, snap):
        self._snap = snap
        self.recorded: list[dict] = []

    async def get_confirmed(self, cid):
        return self._snap

    async def record_verification(self, cid, *, verification):
        self.recorded.append(verification)
        return True


def _snap(operation="update_budget"):
    return SimpleNamespace(
        operation=operation,
        params={"campaign": "X", "_before": {"kind": "budget", "after_micros": 48_000_000}},
        customer_id=DRAFT_ACCOUNT_ID,
    )


async def test_execute_confirmed_mismatch_flags_and_records(monkeypatch):
    async def _apply(store, cid):
        return {"applied": True}

    async def _verify(op, params, customer_id):
        return {"verified": False, "kind": "budget", "expected": 48_000_000, "actual": 40_000_000}

    monkeypatch.setattr(svc, "_apply_confirmed", _apply)
    monkeypatch.setattr(svc, "_verify_applied", _verify)
    store = _WrapStore(_snap())
    res = await svc.execute_confirmed(store, "cid")
    assert res["applied"] is True
    assert res["verification"]["verified"] is False  # флаг дописан наверх
    assert len(store.recorded) == 1  # черновик помечен needs_review (персистентно)


async def test_execute_confirmed_happy_leaves_result_and_status(monkeypatch):
    """verified=True → result НЕ меняется (контракт happy-path) и needs_review НЕ ставится."""

    async def _apply(store, cid):
        return {"applied": True}

    async def _verify(op, params, customer_id):
        return {"verified": True, "kind": "budget", "expected": 48_000_000, "actual": 48_000_000}

    monkeypatch.setattr(svc, "_apply_confirmed", _apply)
    monkeypatch.setattr(svc, "_verify_applied", _verify)
    store = _WrapStore(_snap())
    res = await svc.execute_confirmed(store, "cid")
    assert res == {"applied": True}  # ни одного лишнего ключа
    assert store.recorded == []


async def test_execute_confirmed_verify_none_is_noop(monkeypatch):
    async def _apply(store, cid):
        return {"applied": True}

    async def _verify(op, params, customer_id):
        return {"verified": None, "kind": "budget", "expected": None, "actual": None}

    monkeypatch.setattr(svc, "_apply_confirmed", _apply)
    monkeypatch.setattr(svc, "_verify_applied", _verify)
    store = _WrapStore(_snap())
    res = await svc.execute_confirmed(store, "cid")
    assert res == {"applied": True} and store.recorded == []


async def test_execute_confirmed_verify_failure_does_not_break_apply(monkeypatch):
    """Сбой самой проверки (перечитка упала) НЕ роняет уже применённую мутацию и не ставит флаг."""

    async def _apply(store, cid):
        return {"applied": True}

    async def _verify(op, params, customer_id):
        raise RuntimeError("re-read timeout")

    monkeypatch.setattr(svc, "_apply_confirmed", _apply)
    monkeypatch.setattr(svc, "_verify_applied", _verify)
    store = _WrapStore(_snap())
    res = await svc.execute_confirmed(store, "cid")
    assert res == {"applied": True} and store.recorded == []


async def test_execute_confirmed_skips_verify_for_non_diffable(monkeypatch):
    """Операция без diff (создание) → _verify_applied вообще не зовётся."""

    async def _apply(store, cid):
        return {"created": True}

    called = {"n": 0}

    async def _verify(op, params, customer_id):
        called["n"] += 1
        return {"verified": None}

    monkeypatch.setattr(svc, "_apply_confirmed", _apply)
    monkeypatch.setattr(svc, "_verify_applied", _verify)
    store = _WrapStore(_snap(operation="create_search_campaign"))
    res = await svc.execute_confirmed(store, "cid")
    assert res == {"created": True} and called["n"] == 0


# ── record_verification (реальный store): applied → needs_review (CAS) + audit ───────
async def _make_applied(store: ConfirmStore, *, chat_id: int = 555) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="s",
        chat_id=chat_id,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=chat_id)
    assert await store.claim(cid, operation="update_budget") is not None
    await store.finalize(cid, result={"applied": True})
    return cid


async def test_record_verification_moves_applied_to_needs_review(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    cid = await _make_applied(store)
    ok = await store.record_verification(
        cid, verification={"verified": False, "expected": 48_000_000, "actual": 40_000_000}
    )
    assert ok is True
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "needs_review"


async def test_record_verification_no_op_when_not_applied(monkeypatch):
    """CAS на status='applied': черновик ещё не применён (confirmed) → False, статус не тронут."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="s",
        chat_id=555,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=555)  # confirmed, НЕ applied
    ok = await store.record_verification(
        cid, verification={"verified": False, "expected": 1, "actual": 2}
    )
    assert ok is False
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"  # не понижен

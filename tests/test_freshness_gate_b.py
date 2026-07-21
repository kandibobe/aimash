"""Волна 1.1, шаг 4: гейт B — перечитать состояние живьём и сверить со снимком карточки.

До этого шага сверка дрейфа жила одной функцией `_assert_no_drift` и звалась из ТРЁХ мест лестницы
`_apply_confirmed` — 38 операций из 41 исполнялись без всякой сверки, а сама функция молча выходила
и при `mode == "set_to"`, и при отсутствии снимка, и при сбое чтения. Гейт B — один вызов на все
операции, где тир (`ads.freshness`) решает, чем является отсутствие данных.

Здесь проверяется ПОВЕДЕНИЕ гейта и его МЕСТО в цепочке. Семантика сравнения — в
tests/test_freshness_registry.py; проставление маркера — в tests/test_freshness_marker.py.
"""

from __future__ import annotations

import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
import ads.service as svc  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.freshness import FreshnessDrift, FreshnessMissing  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from conftest import attested  # noqa: E402
from core.ads_errors import is_outcome_unknown_after_mutate  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

_OK_STATUS = {"kind": "status", "before_status": "ENABLED"}


def _snap(kind: svc.SnapshotKind, data=None, reason: str = ""):
    return svc.Snapshot(kind, data, reason)


def _stub_read_state(monkeypatch, snap: svc.Snapshot, calls: list | None = None):
    async def _fake(operation, params, customer_id=None):
        if calls is not None:
            calls.append(operation)
        return snap

    monkeypatch.setattr(svc, "read_state", _fake)


# ── STRICT: отсутствие данных = отказ ────────────────────────────────────────────────────
async def test_strict_without_marker_is_refused(monkeypatch):
    """Черновик, созданный до Волны 1.1 (или мимо `attach_freshness`), исполнению не подлежит:
    нет маркера — значит нет доказательства, что состояние вообще читали."""
    calls: list = []
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, _OK_STATUS), calls)
    with pytest.raises(FreshnessMissing):
        await svc._verify_freshness("pause_campaign", {"campaign": "X"}, DRAFT_ACCOUNT_ID)
    assert calls == []  # отказ ДО живого чтения — лишнего вызова к API не делаем


async def test_strict_with_unreadable_marker_is_refused(monkeypatch):
    """Карточку показали, но состояние прочитать не удалось. Раньше это было неотличимо от
    «сверять нечего» и проезжало гейт молча."""
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, _OK_STATUS))
    params = attested({"campaign": "X"})  # before=None ⇒ UNREADABLE
    with pytest.raises(FreshnessMissing):
        await svc._verify_freshness("pause_campaign", params, DRAFT_ACCOUNT_ID)


async def test_strict_refuses_when_live_reread_fails(monkeypatch):
    """Снимок на карточке был, но перечитать перед исполнением не смогли. Сверка не состоялась —
    значит гарантии нет, и молчаливое «ну ладно» здесь и есть fail-open."""
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.UNREADABLE, reason="TimeoutError"))
    params = attested({"campaign": "X"}, _OK_STATUS)
    with pytest.raises(FreshnessMissing):
        await svc._verify_freshness("pause_campaign", params, DRAFT_ACCOUNT_ID)


async def test_strict_passes_when_live_matches_card(monkeypatch):
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, _OK_STATUS))
    params = attested({"campaign": "X"}, _OK_STATUS)
    assert await svc._verify_freshness("pause_campaign", params, DRAFT_ACCOUNT_ID) is None


async def test_strict_refuses_on_drift(monkeypatch):
    """Кто-то поставил кампанию на паузу между показом карточки и ✅ — человек подтверждал не это."""
    _stub_read_state(
        monkeypatch, _snap(svc.SnapshotKind.OK, {"kind": "status", "before_status": "PAUSED"})
    )
    params = attested({"campaign": "X"}, _OK_STATUS)
    with pytest.raises(FreshnessDrift):
        await svc._verify_freshness("pause_campaign", params, DRAFT_ACCOUNT_ID)


# ── ADVISORY: долг, а не проектное «здесь можно без сверки» ──────────────────────────────
async def test_advisory_without_snapshot_proceeds(monkeypatch):
    """Признанный долг (`ADVISORY_DEBT`): снимка у операции пока нет — исполняем, но факт пишем
    в лог. Отказывать здесь означало бы выключить половину бота под видом безопасности."""
    calls: list = []
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.NO_DIFF, reason="not_diffable"), calls)
    params = attested({"campaign": "X", "keywords": ["a"]})
    assert await svc._verify_freshness("add_keywords", params, DRAFT_ACCOUNT_ID) is None
    assert calls == []  # снимка нет — живое чтение бессмысленно, к API не ходим


async def test_advisory_still_refuses_on_real_drift(monkeypatch):
    """Долг = «сверяем, когда можем», а не «не сверяем». Снимок есть и разошёлся ⇒ отказ."""
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, {"kind": "name", "before_name": "Y"}))
    params = attested({"campaign": "X", "name": "Z"}, {"kind": "name", "before_name": "X"})
    with pytest.raises(FreshnessDrift):
        await svc._verify_freshness("update_campaign", params, DRAFT_ACCOUNT_ID)


async def test_advisory_tolerates_failed_reread(monkeypatch):
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.UNREADABLE, reason="TimeoutError"))
    params = attested({"campaign": "X"}, {"kind": "name", "before_name": "X"})
    assert await svc._verify_freshness("update_campaign", params, DRAFT_ACCOUNT_ID) is None


# ── NO_DIFF: сверять нечего по природе операции ─────────────────────────────────────────
async def test_no_diff_operation_does_not_read_at_all(monkeypatch):
    """Создание кампании с нуля: прежнего состояния не существует. Гейт обязан выйти сразу —
    иначе на каждое создание вешается лишний round-trip к Google Ads."""
    calls: list = []
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, _OK_STATUS), calls)
    assert (
        await svc._verify_freshness("create_search_campaign", {"name": "N"}, DRAFT_ACCOUNT_ID)
        is None
    )
    assert calls == []


async def test_operation_missing_from_registry_is_refused(monkeypatch):
    """Новая мутация, забытая в реестре freshness, обязана упереться в гейт (deny-by-default),
    а не проехать как «сверять нечего»."""
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, _OK_STATUS))
    with pytest.raises(FreshnessMissing):
        await svc._verify_freshness("новая_мутация", {"campaign": "X"}, DRAFT_ACCOUNT_ID)


# ── Место гейта в цепочке: после замка, ДО claim ─────────────────────────────────────────
async def _mk_confirmed(store: ConfirmStore, *, params: dict, customer_id=DRAFT_ACCOUNT_ID) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="pause_campaign",
        customer_id=customer_id,
        params=params,
        summary="s",
        chat_id=960,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=960)
    return cid


def _stub_sdk(monkeypatch, calls: dict, *, status: str = "ENABLED"):
    async def _fake_apply(**kw):
        calls["apply"] = calls.get("apply", 0) + 1
        return {"applied": True}

    ref = type("_R", (), {"id": "123", "status": status, "name": "X", "budget_micros": 1_000_000})

    async def _fake_client(cid_=None):
        return object()

    monkeypatch.setattr(mut, "apply_pause_campaign", _fake_apply)
    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid_, name: ref())
    monkeypatch.setattr(svc, "build_client_async", _fake_client)


async def test_refusal_does_not_burn_the_confirmation(monkeypatch):
    """Ключевое свойство порядка: гейт B стоит ДО claim внутри `apply_*`. Отказ по свежести не
    сжигает одноразовое подтверждение — иначе дрейф стоил бы человеку и операции, и черновика."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    calls: dict = {}
    _stub_sdk(monkeypatch, calls, status="PAUSED")  # живое состояние разошлось со снимком
    store = ConfirmStore()
    cid = await _mk_confirmed(store, params=attested({"campaign": "X"}, _OK_STATUS))

    with pytest.raises(FreshnessDrift):
        await svc.execute_confirmed(store, cid)
    assert calls.get("apply", 0) == 0  # SDK не вызывался — аккаунт не тронут
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"  # claim цел, можно пересоздать черновик


async def test_account_lock_fires_before_freshness(monkeypatch):
    """Порядок гейтов: чужой аккаунт отсекается замком (PermissionError) ДО того, как мы вообще
    пойдём его читать ради сверки. Читать чужой аккаунт «просто для проверки» — тоже утечка."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT_ACCOUNT_ID)
    calls: list = []
    _stub_read_state(monkeypatch, _snap(svc.SnapshotKind.OK, _OK_STATUS), calls)
    store = ConfirmStore()
    cid = await _mk_confirmed(store, params={"campaign": "X"}, customer_id="1234567890")

    with pytest.raises(PermissionError):  # НЕ FreshnessMissing, хотя маркера тоже нет
        await svc.execute_confirmed(store, cid)
    assert calls == []


async def test_freshness_refusal_is_not_reported_as_unknown_outcome():
    """Отказ гейта происходит ДО вызова SDK: мутации не было. Увести такой черновик в needs_review
    («результат неизвестен, сверьте в Google Ads») значит сказать неправду о состоянии аккаунта."""
    assert is_outcome_unknown_after_mutate(FreshnessMissing("нет снимка")) is False
    assert is_outcome_unknown_after_mutate(FreshnessDrift("дрейф")) is False

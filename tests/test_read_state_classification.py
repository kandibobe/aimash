"""Классификация исхода чтения состояния (Волна 1.1, шаг 2).

До этого шага `read_before` возвращал `None` из ПЯТИ разных мест с несовместимым смыслом: «операция
без диффа», «нет имени кампании», «сущность не нашлась», «чтение сорвалось» и терминальный
не-`except` выход, когда ветки в лестнице просто нет. Freshness-гейт на таком входе неотличим от
заглушки: он не может сказать, сверять нечего или сверка не состоялась.

Здесь проверяется именно классификация — что «не смогли» никогда не выдаётся за «нечего».
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads import service as svc  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.service import SnapshotKind  # noqa: E402


class _Ref:
    id = "1"
    status = "ENABLED"
    name = "X"
    budget_micros = 5_000_000
    budget_explicitly_shared = False
    budget_reference_count = 1
    budget_resource = "customers/1/campaignBudgets/1"


@pytest.fixture
def fake_client(monkeypatch):
    async def _fake_client(cid=None):
        return object()

    monkeypatch.setattr(svc, "build_client_async", _fake_client)


# ── NO_DIFF: сверять нечего по построению ─────────────────────────────────────────


async def test_not_diffable_and_no_campaign_key_are_no_diff(fake_client):
    """Два ранних выхода лестницы. Оба — честное «нечего сверять», а не сбой."""
    s = await svc.read_state("create_rsa", {"campaign": "X"})
    assert (s.kind, s.data, s.reason) == (SnapshotKind.NO_DIFF, None, "not_diffable")

    s = await svc.read_state("pause_campaign", {})
    assert (s.kind, s.data, s.reason) == (SnapshotKind.NO_DIFF, None, "no_campaign_key")


# ── UNREADABLE: сверка не состоялась ──────────────────────────────────────────────


async def test_reader_exception_is_unreadable_not_no_diff(monkeypatch, fake_client):
    """Сеть/доступ/SDK упали. Раньше это давало тот же `None`, что и «нечего сверять» — то есть
    freshness-гард самоотключался ровно там, где нужен больше всего."""

    def _boom(*a, **kw):
        raise ConnectionError("сеть отвалилась")

    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", _boom)

    s = await svc.read_state("pause_campaign", {"campaign": "X"})
    assert s.kind is SnapshotKind.UNREADABLE
    assert s.data is None
    assert s.reason == "ConnectionError"


async def test_reader_exception_reason_carries_no_exception_text(monkeypatch, fake_client):
    """Правило 5: наружу уезжает имя класса, а не текст исключения (он может нести токен)."""

    def _boom(*a, **kw):
        raise ConnectionError("ya29.секрет-в-тексте-исключения")

    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", _boom)

    s = await svc.read_state("pause_campaign", {"campaign": "X"})
    assert "секрет" not in s.reason


async def test_entity_not_found_is_unreadable(monkeypatch, fake_client):
    """Кампании с таким именем не видно. Состояние существует — мы его не получили; для STRICT это
    обязано быть отказом, а не «сравнивать нечего»."""
    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid, name: None)

    s = await svc.read_state("pause_campaign", {"campaign": "нет-такой"})
    assert s.kind is SnapshotKind.UNREADABLE
    assert s.reason == "not_found"


async def test_ambiguous_ad_is_unreadable_with_own_reason(monkeypatch, fake_client):
    """Под фильтр попало несколько объявлений — какое из них подтверждал человек, неизвестно."""
    monkeypatch.setattr(svc.resolve, "find_ads_in_group", lambda *a, **kw: [object(), object()])

    s = await svc.read_state("pause_ad", {"campaign": "X", "ad_group": "G", "ad": "A"})
    assert s.kind is SnapshotKind.UNREADABLE
    assert s.reason == "ambiguous"


async def test_missing_branch_is_unreadable(monkeypatch, fake_client):
    """Дыра `ads/service.py` терминального `return None`: операция объявлена диффабельной, но ветки
    для неё в лестнице нет. NO_DIFF здесь означал бы «эта операция заведомо свежая» — то есть новая
    STRICT-операция проезжала бы гейт молча."""
    monkeypatch.setattr(svc, "_DIFFABLE_OPS", frozenset({"забыли_ветку"}))

    s = await svc.read_state("забыли_ветку", {"campaign": "X"})
    assert s.kind is SnapshotKind.UNREADABLE
    assert s.reason == "no_branch"


# ── Что НЕ изменилось ─────────────────────────────────────────────────────────────


async def test_decrease_below_zero_still_propagates(monkeypatch, fake_client):
    """Невыполнимая команда («снизь на 200» при бюджете 100) — это отказ показа ДО кнопок ✅,
    а не сбой чтения. Проглотить её значило бы упасть уже ПОСЛЕ подтверждения."""
    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid, name: _Ref())

    def _below_zero(*a, **kw):
        raise svc.resolve.DecreaseBelowZero("decrease_by_amount", 200, 5_000_000)

    monkeypatch.setattr(svc.resolve, "compute_new_micros", _below_zero)

    with pytest.raises(svc.resolve.DecreaseBelowZero):
        await svc.read_state(
            "update_budget",
            {"campaign": "X", "mode": "decrease_by_amount", "value": 200},
        )


async def test_read_before_contract_unchanged(monkeypatch, fake_client):
    """Адаптер отдаёт dict только при OK и None во всех прочих исходах — все существующие
    вызывающие (карточка, смоук-скрипты, тесты) не меняют поведения."""
    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid, name: _Ref())
    assert await svc.read_before("pause_campaign", {"campaign": "X"}) == {
        "kind": "status",
        "before_status": "ENABLED",
    }

    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", lambda c, cid, name: None)
    assert await svc.read_before("pause_campaign", {"campaign": "X"}) is None  # UNREADABLE
    assert await svc.read_before("create_rsa", {"campaign": "X"}) is None  # NO_DIFF
    assert await svc.read_before("pause_campaign", {}) is None  # NO_DIFF


async def test_read_state_defaults_to_draft(monkeypatch):
    """Аккаунт чтения не поехал: без customer_id — Draft, как и у `read_before` (регрессия
    tests/test_execute_account_binding.py::test_read_before_defaults_to_draft)."""
    seen: dict = {}

    def _fake_find(client, cid, name):
        seen["cid"] = cid
        return _Ref()

    async def _fake_client(cid=None):
        seen["client_cid"] = cid
        return object()

    monkeypatch.setattr(svc.resolve, "find_campaign_by_name", _fake_find)
    monkeypatch.setattr(svc, "build_client_async", _fake_client)

    s = await svc.read_state("pause_campaign", {"campaign": "X"})
    assert s.kind is SnapshotKind.OK
    assert seen["cid"] == DRAFT_ACCOUNT_ID
    assert seen["client_cid"] == DRAFT_ACCOUNT_ID

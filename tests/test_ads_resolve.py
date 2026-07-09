"""Офлайн-тесты резолва кампаний/групп (ads/resolve.py) — READ-ONLY оркестрация.

Закрывает дыру покрытия из аудита: денежный путь `compute_new_micros` (пересчёт бюджета в micros)
и GAQL-резолв по имени не имели прямых юнит-тестов. Без живого Google Ads — клиент фейковый
(SimpleNamespace-строки, как в test_write_layer/test_keyword_plan); БД не нужна. Проверяем:
- compute_new_micros: три режима + округление int(round(...)) + неизвестный mode → ValueError;
- _gaql_escape: экранирование кавычки/бэкслеша (защита от GAQL-инъекции в WHERE name = '...');
- find_campaign_by_name / find_ad_groups: маппинг строк, пустой результат, замок аккаунта (gr #9).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.resolve import (  # noqa: E402
    _gaql_escape,
    compute_new_micros,
    find_ad_groups,
    find_campaign_by_name,
)
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    """Временно задать allow-list (как в test_safety_core); вернуть как было."""
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


# ── Фейковый SDK: get_service("GoogleAdsService").search(customer_id, query) → строки ──
class _FakeGA:
    def __init__(self, rows):
        self._rows = rows
        self.last_query: str | None = None
        self.last_customer_id: str | None = None

    def search(self, *, customer_id, query):
        self.last_customer_id = customer_id
        self.last_query = query
        return list(self._rows)


class _FakeClient:
    def __init__(self, rows):
        self._ga = _FakeGA(rows)

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._ga


def _campaign_row(*, cid="123", name="Бренд", status="ENABLED", budget_micros=1_000_000):
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id=int(cid),
            name=name,
            status=SimpleNamespace(name=status),
            resource_name=f"customers/{DRAFT_ACCOUNT_ID}/campaigns/{cid}",
            campaign_budget=f"customers/{DRAFT_ACCOUNT_ID}/campaignBudgets/{cid}",
        ),
        campaign_budget=SimpleNamespace(amount_micros=budget_micros),
    )


def _ad_group_row(
    *,
    agid="42",
    name="Группа",
    status="ENABLED",
    cpc=500_000,
    camp_id="123",
    ag_type="SEARCH_STANDARD",
    channel="SEARCH",
):
    return SimpleNamespace(
        ad_group=SimpleNamespace(
            id=int(agid),
            name=name,
            status=SimpleNamespace(name=status),
            cpc_bid_micros=cpc,
            resource_name=f"customers/{DRAFT_ACCOUNT_ID}/adGroups/{agid}",
            type_=SimpleNamespace(name=ag_type),
        ),
        campaign=SimpleNamespace(
            id=int(camp_id),
            advertising_channel_type=SimpleNamespace(name=channel),
        ),
    )


# ── compute_new_micros: денежный путь (валюта не конвертируется) ─────────────────
def test_compute_increase_by_percent():
    assert compute_new_micros(1_000_000, "increase_by_percent", 20) == 1_200_000
    assert (
        compute_new_micros(1_000_000, "increase_by_percent", 0) == 1_000_000
    )  # +0% = без изменений
    # A2: результат округляется до кратного BILLING_UNIT_MICROS. 1_125_000 не кратно 10 000
    # (1_125_000/10 000 = 112.5) → округление к чётному → 1_120_000 (иначе API reject).
    assert compute_new_micros(1_000_000, "increase_by_percent", 12.5) == 1_120_000


def test_compute_increase_by_amount():
    # value — в валюте аккаунта; +5.5 единицы = +5_500_000 micros.
    assert compute_new_micros(1_000_000, "increase_by_amount", 5.5) == 6_500_000
    assert compute_new_micros(0, "increase_by_amount", 10) == 10_000_000


def test_compute_set_to():
    assert compute_new_micros(999, "set_to", 10) == 10_000_000  # текущий бюджет игнорируется
    assert compute_new_micros(0, "set_to", 9.99) == 9_990_000  # 9_990_000 уже кратно 10 000


def test_compute_result_is_always_billing_unit_multiple():
    """Гард класса NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT (A2): ЛЮБОЙ выход compute_new_micros
    кратен BILLING_UNIT_MICROS (иначе Google Ads отклоняет update_budget/update_bid ПОСЛЕ «да»).
    Особый упор на increase_by_percent — почти всегда даёт не-кратное сырое значение."""
    from core.limits import BILLING_UNIT_MICROS

    cases = [
        ("increase_by_percent", 7),  # 5 550 000 × 1.07 = 5 938 500 — не кратно 10 000
        ("increase_by_percent", 12.5),
        ("increase_by_percent", 33.33),
        ("increase_by_percent", 3),
        ("increase_by_amount", 1.234),
        ("increase_by_amount", 0.005),
        ("set_to", 5.555),
        ("set_to", 100.001),
    ]
    bases = [5_550_000, 1_000_000, 333_000, 10_010_000, 7_777_777]
    for base in bases:
        for mode, value in cases:
            out = compute_new_micros(base, mode, value)
            assert out % BILLING_UNIT_MICROS == 0, f"{base}/{mode}/{value} → {out} не кратно"
            assert (
                out > 0
            )  # положительное значение не обнуляем (round_micros: минимум одна единица)


def test_compute_tiny_positive_floors_to_one_billing_unit():
    # Ниже одной биллинг-единицы, но > 0: округляем ВВЕРХ до BILLING_UNIT_MICROS, не до нуля
    # (0 бюджет API отклонит иначе). 3 micros +50% = 4.5 → 4 (sub-unit) → 10 000.
    from core.limits import BILLING_UNIT_MICROS

    assert compute_new_micros(3, "increase_by_percent", 50) == BILLING_UNIT_MICROS


def test_compute_unknown_mode_raises():
    with pytest.raises(ValueError):
        compute_new_micros(1_000_000, "multiply_by", 2)


# ── _gaql_escape: защита от инъекции в строковый литерал GAQL ────────────────────
def test_gaql_escape_quote_and_backslash():
    assert _gaql_escape("O'Brien") == "O\\'Brien"
    assert _gaql_escape("a\\b") == "a\\\\b"
    assert _gaql_escape("обычное имя") == "обычное имя"  # кириллица/пробелы не трогаем


def test_gaql_escape_neutralizes_injection():
    # Попытка вырваться из WHERE name = '...': кавычка экранируется → остаёмся внутри литерала.
    evil = "x' OR campaign.id > 0 --"
    escaped = _gaql_escape(evil)
    assert "\\'" in escaped  # кавычка обезврежена (перед ней бэкслеш)
    # Не осталось НЕэкранированной кавычки: убираем экранированные пары — одиночных ' быть не должно.
    neutral = escaped.replace("\\\\", "").replace("\\'", "")
    assert "'" not in neutral


# ── find_campaign_by_name ────────────────────────────────────────────────────────
def test_find_campaign_maps_row_fields():
    client = _FakeClient(
        [_campaign_row(cid="555", name="Бренд", status="PAUSED", budget_micros=2_000_000)]
    )
    with allowed_ids(DRAFT_ACCOUNT_ID):
        ref = find_campaign_by_name(client, DRAFT_ACCOUNT_ID, "Бренд")
    assert ref is not None
    assert ref.id == "555"
    assert ref.name == "Бренд"
    assert ref.status == "PAUSED"  # из status.name
    assert ref.budget_micros == 2_000_000
    # имя экранировано и попало в запрос как литерал.
    assert "WHERE campaign.name = 'Бренд'" in client._ga.last_query


def test_find_campaign_escapes_name_in_query():
    client = _FakeClient([])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        find_campaign_by_name(client, DRAFT_ACCOUNT_ID, "O'Brien")
    assert "O\\'Brien" in client._ga.last_query  # инъекция обезврежена в самом запросе


def test_find_campaign_returns_none_when_empty():
    client = _FakeClient([])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        assert find_campaign_by_name(client, DRAFT_ACCOUNT_ID, "нет такой") is None


def test_find_campaign_rejects_foreign_account():
    client = _FakeClient([_campaign_row()])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            find_campaign_by_name(client, "1234567890", "Бренд")


def test_find_campaign_fail_closed_when_allowlist_empty():
    client = _FakeClient([_campaign_row()])
    with allowed_ids(""):
        with pytest.raises(PermissionError):
            find_campaign_by_name(client, DRAFT_ACCOUNT_ID, "Бренд")


# ── find_ad_groups ───────────────────────────────────────────────────────────────
def test_find_ad_groups_maps_multiple_rows():
    rows = [
        _ad_group_row(agid="10", name="A", cpc=300_000),
        _ad_group_row(agid="20", name="B", cpc=700_000, status="PAUSED"),
    ]
    client = _FakeClient(rows)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        groups = find_ad_groups(client, DRAFT_ACCOUNT_ID, "Бренд")
    assert [g.id for g in groups] == ["10", "20"]
    assert groups[0].cpc_bid_micros == 300_000
    assert groups[1].status == "PAUSED"
    assert groups[0].campaign_id == "123"


def test_find_ad_groups_empty_when_no_groups():
    client = _FakeClient([])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        assert find_ad_groups(client, DRAFT_ACCOUNT_ID, "Бренд") == []


def test_find_ad_groups_rejects_foreign_account():
    client = _FakeClient([_ad_group_row()])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            find_ad_groups(client, "1234567890", "Бренд")


# ── B2: тип группы/канала для гейта RSA (accepts_rsa) ────────────────────────────
def test_find_ad_groups_maps_type_and_channel():
    rows = [_ad_group_row(ag_type="SEARCH_STANDARD", channel="SEARCH")]
    client = _FakeClient(rows)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        g = find_ad_groups(client, DRAFT_ACCOUNT_ID, "Бренд")[0]
    assert g.ad_group_type == "SEARCH_STANDARD" and g.channel_type == "SEARCH"
    assert "ad_group.type" in client._ga.last_query
    assert "campaign.advertising_channel_type" in client._ga.last_query


def test_accepts_rsa_only_for_search_standard():
    def g(ag_type, channel):
        return find_ad_groups(
            _FakeClient([_ad_group_row(ag_type=ag_type, channel=channel)]),
            DRAFT_ACCOUNT_ID,
            "Бренд",
        )[0]

    with allowed_ids(DRAFT_ACCOUNT_ID):
        assert g("SEARCH_STANDARD", "SEARCH").accepts_rsa() is True
        assert g("SEARCH_DYNAMIC_ADS", "SEARCH").accepts_rsa() is False  # DSA — только expanded DSA
        assert g("DISPLAY_STANDARD", "DISPLAY").accepts_rsa() is False
        assert g("VIDEO_RESPONSIVE", "VIDEO").accepts_rsa() is False


def test_accepts_rsa_false_when_type_unknown():
    # старые строки/фейки без type_/channel → пусто → accepts_rsa False (вызывающий покажет причину)
    from ads.resolve import AdGroupRef

    ref = AdGroupRef(
        id="1", resource_name="rn", name="g", status="ENABLED", cpc_bid_micros=0, campaign_id="2"
    )
    assert ref.accepts_rsa() is False


# ── A3: гард класса «резолвер по имени не адресует REMOVED сущность» ───────────────
def _resolver_name_calls():
    """Все резолверы по ИМЕНИ кампании, что читают денежно-мутируемую сущность → (fn, kwargs).
    Новый резолвер этого класса обязан попасть сюда, иначе гард ниже его не покроет."""
    from ads.resolve import (
        campaign_bidding_strategy,
        campaign_network_settings,
    )

    common = {"customer_id": DRAFT_ACCOUNT_ID, "name": "Бренд"}
    return [
        (find_campaign_by_name, common),
        (campaign_network_settings, common),
        (campaign_bidding_strategy, common),
        (find_ad_groups, {"customer_id": DRAFT_ACCOUNT_ID, "campaign_name": "Бренд"}),
    ]


def test_name_resolvers_exclude_removed_in_query():
    """Каждый резолвер по имени фильтрует campaign.status != 'REMOVED' в самом GAQL — Google
    переиспользует освобождённое имя удалённой кампании, и денежная команда «увеличь бюджет X»
    без фильтра ушла бы в мёртвую тёзку (обычно СТАРШУЮ строку без ORDER BY)."""
    for fn, kwargs in _resolver_name_calls():
        client = _FakeClient([])
        with allowed_ids(DRAFT_ACCOUNT_ID):
            fn(client, **kwargs)
        q = client._ga.last_query or ""
        assert "campaign.status != 'REMOVED'" in q, f"{fn.__name__}: нет фильтра REMOVED в query"


def test_find_ad_groups_also_excludes_removed_ad_groups():
    """find_ad_groups адресует уровень группы (bid/add_keywords) — фильтруем И удалённые группы."""
    client = _FakeClient([])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        find_ad_groups(client, DRAFT_ACCOUNT_ID, "Бренд")
    q = client._ga.last_query or ""
    assert "ad_group.status != 'REMOVED'" in q


class _StatusAwareGA(_FakeGA):
    """Фейк, уважающий фильтр статуса в query: если запрос содержит `status != 'REMOVED'`,
    строки со status.name == 'REMOVED' отсеиваются — как реальный сервер Google Ads."""

    def search(self, *, customer_id, query):
        self.last_customer_id = customer_id
        self.last_query = query
        rows = list(self._rows)
        if "campaign.status != 'REMOVED'" in query:
            rows = [r for r in rows if getattr(r.campaign.status, "name", "") != "REMOVED"]
        return rows


def test_find_campaign_picks_live_over_removed_namesake():
    """Функциональный: две кампании-тёзки (REMOVED и ENABLED) → резолвер возвращает живую."""
    removed = _campaign_row(cid="111", name="Sale", status="REMOVED", budget_micros=1_000_000)
    live = _campaign_row(cid="222", name="Sale", status="ENABLED", budget_micros=5_000_000)
    client = _FakeClient([removed, live])
    client._ga = _StatusAwareGA([removed, live])  # подменяем на status-aware фейк
    with allowed_ids(DRAFT_ACCOUNT_ID):
        ref = find_campaign_by_name(client, DRAFT_ACCOUNT_ID, "Sale")
    assert ref is not None
    assert ref.id == "222" and ref.status == "ENABLED"  # взяли живую, не удалённую тёзку

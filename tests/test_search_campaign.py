"""Офлайн-тесты §3: создание поисковой (Search) кампании из текстов за двойным гейтом.

Без живого Google Ads — _create_search_campaign_via_sdk подменяется monkeypatch'ем; проверяем оба
гейта (замок аккаунта + confirm + user_initiated), валидацию состава/длины/URL/бюджета В КОДЕ ДО
claim, статус PAUSED, откат осиротевшего бюджета при сбое создания кампании. Бэкенд-слой; визард
(/newsearch) подключается отдельно.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


@dataclass
class FakeProposal:
    operation: str
    status: str
    user_initiated: bool


class FakeStore:
    def __init__(self, proposal=None):
        self._p = proposal
        self.finalized = False
        self._claimed = False

    async def claim(self, confirmation_id, *, operation):
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


_VALID = dict(
    campaign_name="Доставка цветов",
    final_url="https://example.com/",
    headlines=["Доставка цветов", "Букеты от 299", "Свежие розы"],
    descriptions=["Большой выбор букетов на любой повод.", "Доставка за час по городу."],
    budget_daily_micros=50_000_000,
)


# ── apply_create_search_campaign: оба гейта + user_initiated + PAUSED ─────────────
async def test_apply_create_search_happy_path():
    called = {}

    def fake(client, customer_id, **kw):
        called.update(customer_id=customer_id, **kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_create_search_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            **_VALID,
        )
    assert res["applied"] is True and res["status"] == "PAUSED"
    assert called["campaign_name"] == "Доставка цветов"
    assert called["budget_micros"] == 50_000_000  # apply прокинул как budget_micros
    assert called["keywords"] == []  # без ключей по умолчанию
    assert store.finalized is True


async def test_apply_create_search_mixed_match_types_pair_dedup():
    """§19.4.1: per-keyword типы доходят до SDK 1:1; дубль ключа выпадает ВМЕСТЕ со своим типом
    (первый выигрывает) — иначе дедуп только текстов порвал бы склейку по индексу."""
    called = {}

    def fake(client, customer_id, **kw):
        called.update(**kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_create_search_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            keywords=["used cars", "cheap cars", " used cars "],  # дубль (с пробелами)
            keyword_match_types=["exact", "phrase", "broad"],
            **_VALID,
        )
    assert called["keywords"] == ["used cars", "cheap cars"]
    assert called["keyword_match_types"] == ["exact", "phrase"]  # broad дубля отброшен вместе с ним


async def test_apply_create_search_passes_networks_schedule_dates():
    """§19.3: сети/расписание/даты доходят до SDK-цепочки."""
    called = {}

    def fake(client, customer_id, **kw):
        called.update(**kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=True))
    blocks = [{"day": "MONDAY", "start_hour": 9, "end_hour": 18}]
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_create_search_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            networks="search_partners",
            ad_schedule_blocks=blocks,
            start_date="2026-08-01",
            end_date="2026-09-01",
            **_VALID,
        )
    assert called["networks"] == "search_partners"
    assert called["ad_schedule_blocks"] == blocks
    assert called["start_date"] == "2026-08-01" and called["end_date"] == "2026-09-01"


async def test_apply_create_search_rejects_mismatched_match_types_length():
    """Рассинхрон длин keywords/keyword_match_types ловится В КОДЕ ДО claim (SDK не зван)."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_create_search_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                keywords=["a", "b"],
                keyword_match_types=["exact"],  # длина ≠ keywords
                **_VALID,
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_create_search_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=False))
    with (
        patched(mut, "_create_search_campaign_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        with pytest.raises(PermissionError):
            await mut.apply_create_search_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
                **_VALID,
            )
    assert store.finalized is False


async def test_apply_create_search_rejects_foreign_account():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            await mut.apply_create_search_campaign(
                customer_id="1234567890",  # чужой → замок ДО SDK
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **_VALID,
            )
    assert calls["n"] == 0 and store.finalized is False


async def test_apply_create_search_validates_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("create_search_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError):
            await mut.apply_create_search_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                **{**_VALID, "headlines": ["а" * 31, "Второй", "Третий"]},  # >30 (кириллица=1)
            )
    assert calls["n"] == 0 and store.finalized is False


# ── Валидация состава В КОДЕ (golden rule #4) — ДО claim ──────────────────────────
def _validate(**over):
    args = {**_VALID, **over}
    mut._validate_search_inputs(
        args["campaign_name"],
        args["headlines"],
        args["descriptions"],
        args["final_url"],
        args["budget_daily_micros"],
    )


def test_validate_search_accepts_valid():
    _validate()  # не бросает


def test_validate_search_rejects_too_few_headlines():
    with pytest.raises(ValueError):
        _validate(headlines=["Один", "Два"])  # <3 (RSA_MIN_HEADLINES)


def test_validate_search_rejects_bad_url():
    with pytest.raises(ValueError):
        _validate(final_url="ftp://nope")


def test_validate_search_rejects_overbig_budget():
    with pytest.raises(ValueError):
        _validate(budget_daily_micros=2 * mut.MAX_AMOUNT_MICROS)


def test_validate_search_rejects_overlong_name():
    with pytest.raises(ValueError):
        _validate(campaign_name="К" * 121)  # >120


# ── Откат бюджета при сбое создания кампании (без осиротевших ресурсов) ───────────
class _Auto:
    """Авто-namespace: любой неустановленный атрибут авто-создаётся (имитирует proto-объект)."""

    def __getattr__(self, k):
        v = _Auto()
        object.__setattr__(self, k, v)
        return v


def _resp(rns):
    return SimpleNamespace(results=[SimpleNamespace(resource_name=rn) for rn in rns])


def test_create_search_via_sdk_rolls_back_budget_on_campaign_failure():
    removed: list[str] = []

    class _BudgetSvc:
        def mutate_campaign_budgets(self, customer_id, operations):
            op = operations[0]
            rem = getattr(op, "remove", None)
            if isinstance(rem, str):  # remove-операция = откат
                removed.append(rem)
                return _resp([])
            return _resp(["customers/x/campaignBudgets/9"])

    class _CampSvc:
        def mutate_campaigns(self, customer_id, operations):
            raise RuntimeError("BOOM: DUPLICATE_CAMPAIGN_NAME")

    services = {"CampaignBudgetService": _BudgetSvc(), "CampaignService": _CampSvc()}

    class _Client:
        enums = _Auto()

        def get_service(self, name):
            return services[name]

        def get_type(self, name):
            return _Auto()

    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(RuntimeError):
            mut._create_search_campaign_via_sdk(
                _Client(),
                DRAFT_ACCOUNT_ID,
                campaign_name="Дубль",
                final_url="https://example.com/",
                headlines=["Заголовок", "Второй", "Третий"],
                descriptions=["Описание один", "Описание два"],
                budget_micros=50_000_000,
                keywords=[],
                match_type="phrase",
                cpc_bid_micros=500_000,
            )
    assert removed == ["customers/x/campaignBudgets/9"]  # осиротевший бюджет удалён


# ── capability-guard зеркало ─────────────────────────────────────────────────────
def test_create_search_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    assert "create_search_campaign" in SUPPORTED_OPERATIONS


# ── Схема CreateSearchCampaign ───────────────────────────────────────────────────
def test_search_schema_validates_and_rejects():
    from agent.tools.schemas import CreateSearchCampaign

    ok = CreateSearchCampaign(
        campaign_name="Цветы",
        final_url="https://example.com/",
        headlines=["Доставка цветов", "Букеты от 299", "Свежие розы"],
        descriptions=["Большой выбор букетов.", "Доставка за час."],
        budget_daily_micros=50_000_000,
        keywords=["  купить цветы  ", "доставка"],
        match_type="phrase",
    )
    assert ok.keywords[0] == "купить цветы"  # normalize_keywords обрезал пробелы
    with pytest.raises(Exception):
        CreateSearchCampaign(  # <3 заголовков
            campaign_name="X",
            final_url="https://x/",
            headlines=["Один", "Два"],
            descriptions=["Опис один", "Опис два"],
            budget_daily_micros=1,
        )
    with pytest.raises(Exception):
        CreateSearchCampaign(  # плохой url
            campaign_name="X",
            final_url="ftp://nope",
            headlines=["Один", "Два", "Три"],
            descriptions=["Опис один", "Опис два"],
            budget_daily_micros=1,
        )

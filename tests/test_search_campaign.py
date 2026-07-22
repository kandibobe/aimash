"""Офлайн-тесты §3: создание поисковой (Search) кампании из текстов за двойным гейтом.

Без живого Google Ads — _create_search_campaign_via_sdk подменяется monkeypatch'ем; проверяем оба
гейта (замок аккаунта + confirm + user_initiated), валидацию состава/длины/URL/бюджета В КОДЕ ДО
claim, статус PAUSED, откат осиротевшего бюджета при сбое создания кампании. Бэкенд-слой; визард
(/newsearch) подключается отдельно.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from conftest import FakeConfirmStore, FakeProposal  # noqa: E402
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

    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )
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
    """§19.4.1 / B11: per-keyword типы доходят до SDK 1:1; дедуп по ПАРЕ (текст, тип). Google Ads
    допускает один текст с РАЗНЫМИ типами соответствия в одной группе — поэтому «used cars» [exact]
    и «used cars» [broad] обе сохраняются (это разные критерии), а точный дубль пары схлопывается."""
    called = {}

    def fake(client, customer_id, **kw):
        called.update(**kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )
    with patched(mut, "_create_search_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_create_search_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            # (used cars, exact), (cheap cars, phrase), (used cars, broad) — три РАЗНЫЕ пары;
            # плюс точный дубль пары (used cars, exact) в конце — он и должен схлопнуться.
            keywords=["used cars", "cheap cars", " used cars ", "used cars"],
            keyword_match_types=["exact", "phrase", "broad", "exact"],
            **_VALID,
        )
    assert called["keywords"] == ["used cars", "cheap cars", "used cars"]
    assert called["keyword_match_types"] == [
        "exact",
        "phrase",
        "broad",
    ]  # дубль пары exact отброшен


async def test_apply_create_search_passes_networks_schedule_dates():
    """§19.3: сети/расписание/даты доходят до SDK-цепочки."""
    called = {}

    def fake(client, customer_id, **kw):
        called.update(**kw)
        return {"applied": True, "status": "PAUSED", "campaign": "customers/x/campaigns/1"}

    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )
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

    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )
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
    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=False)
    )
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

    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )
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

    store = FakeConfirmStore(
        FakeProposal("create_search_campaign", "confirmed", user_initiated=True)
    )
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


# ── §19.3: сети на SDK-полях (класс-гард на перепутанные поля v24) ─────────────────
# target_search_network = ПОИСКОВЫЕ ПАРТНЁРЫ (дефолт ВЫКЛ, вкл. только явным 'search_partners');
# target_partner_search_network = ограниченная сеть избранных аккаунтов — НИКОГДА не True
# (иначе CampaignError.CANNOT_TARGET_PARTNER_SEARCH_NETWORK роняет create на обычном аккаунте);
# target_content_network (КМС) для Search всегда ВЫКЛ. Жалоба заказчика 2026-07-07.
@pytest.mark.parametrize(
    ("networks", "want_partners"),
    [(None, False), ("search", False), ("search_partners", True)],
)
def test_create_search_via_sdk_network_settings(networks, want_partners):
    captured: dict[str, object] = {}

    class _BudgetSvc:
        def mutate_campaign_budgets(self, customer_id, operations):
            op = operations[0]
            if isinstance(getattr(op, "remove", None), str):  # откат после нашего стопа
                return _resp([])
            return _resp(["customers/x/campaignBudgets/9"])

    class _CampSvc:
        def mutate_campaigns(self, customer_id, operations):
            ns = operations[0].create.network_settings
            captured["google_search"] = ns.target_google_search
            captured["search_partners"] = ns.target_search_network
            captured["content"] = ns.target_content_network
            captured["partner_search"] = ns.target_partner_search_network
            raise RuntimeError("STOP: network_settings захвачены")

    services = {"CampaignBudgetService": _BudgetSvc(), "CampaignService": _CampSvc()}

    class _Client:
        enums = _Auto()

        def get_service(self, name):
            return services[name]

        def get_type(self, name):
            return _Auto()

    kwargs = {} if networks is None else {"networks": networks}
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(RuntimeError):
            mut._create_search_campaign_via_sdk(
                _Client(),
                DRAFT_ACCOUNT_ID,
                campaign_name="Сети",
                final_url="https://example.com/",
                headlines=["Заголовок", "Второй", "Третий"],
                descriptions=["Описание один", "Описание два"],
                budget_micros=50_000_000,
                keywords=[],
                match_type="phrase",
                cpc_bid_micros=500_000,
                **kwargs,
            )
    assert captured["google_search"] is True
    assert captured["search_partners"] is want_partners
    assert captured["content"] is False  # КМС всегда ВЫКЛ
    assert captured["partner_search"] is False  # ограниченную сеть не трогаем НИКОГДА


# ── §19.8/§11: «Запустить» включает ВСЮ структуру, а не только кампанию (crux фикса) ──
def test_launch_campaign_via_sdk_enables_whole_tree():
    """Регресс-гард на тихий дефект: визард создаёт кампанию/группу/RSA все PAUSED; включение
    ОДНОЙ кампании оставляло бы группу и объявление на паузе ⇒ 0 показов. `_launch_campaign_via_sdk`
    обязан выставить ENABLED на ВСЕХ трёх уровнях, фильтруя REMOVED в GAQL."""

    calls: dict[str, list] = {"campaigns": [], "ad_groups": [], "ad_group_ads": []}

    class _GA:
        def search(self, customer_id, query):
            assert "!= 'REMOVED'" in query  # REMOVED-сущности не воскрешаем
            if "FROM ad_group_ad" in query:
                return [
                    SimpleNamespace(
                        ad_group_ad=SimpleNamespace(resource_name="customers/x/adGroupAds/11~101")
                    ),
                    SimpleNamespace(
                        ad_group_ad=SimpleNamespace(resource_name="customers/x/adGroupAds/11~102")
                    ),
                ]
            assert "FROM ad_group " in query
            return [
                SimpleNamespace(ad_group=SimpleNamespace(resource_name="customers/x/adGroups/11"))
            ]

    class _CampSvc:
        def campaign_path(self, cid, camp):
            return f"customers/{cid}/campaigns/{camp}"

        def mutate_campaigns(self, customer_id, operations):
            calls["campaigns"].append(
                [(o.update.resource_name, o.update.status) for o in operations]
            )

    class _AgSvc:
        def mutate_ad_groups(self, customer_id, operations):
            calls["ad_groups"].append(
                [(o.update.resource_name, o.update.status) for o in operations]
            )

    class _AgAdSvc:
        def mutate_ad_group_ads(self, customer_id, operations):
            calls["ad_group_ads"].append(
                [(o.update.resource_name, o.update.status) for o in operations]
            )

    services = {
        "GoogleAdsService": _GA(),
        "CampaignService": _CampSvc(),
        "AdGroupService": _AgSvc(),
        "AdGroupAdService": _AgAdSvc(),
    }

    class _Enums:
        class CampaignStatusEnum:
            ENABLED = "C_ENABLED"

        class AdGroupStatusEnum:
            ENABLED = "AG_ENABLED"

        class AdGroupAdStatusEnum:
            ENABLED = "AGA_ENABLED"

    class _Client:
        enums = _Enums()

        def get_service(self, name):
            return services[name]

        def get_type(self, name):
            return _Auto()

        def copy_from(self, dst, src):
            pass

    class _FakeFieldMask:
        @staticmethod
        def field_mask(a, b):
            return object()

    with patched(mut, "protobuf_helpers", _FakeFieldMask):
        res = mut._launch_campaign_via_sdk(_Client(), DRAFT_ACCOUNT_ID, "55")

    # 1) кампания включена
    assert calls["campaigns"] and calls["campaigns"][0][0][1] == "C_ENABLED"
    # 2) ВСЕ группы включены
    assert calls["ad_groups"] and all(st == "AG_ENABLED" for _, st in calls["ad_groups"][0])
    # 3) ВСЕ объявления включены — без этого показов НОЛЬ (суть исправляемого дефекта)
    assert calls["ad_group_ads"] and len(calls["ad_group_ads"][0]) == 2
    assert all(st == "AGA_ENABLED" for _, st in calls["ad_group_ads"][0])
    assert res["ad_groups_enabled"] == 1 and res["ads_enabled"] == 2 and res["status"] == "ENABLED"


# ── capability-guard зеркало ─────────────────────────────────────────────────────
def test_create_search_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    assert "create_search_campaign" in SUPPORTED_OPERATIONS
    # §19.8/§11: launch_campaign исполнима за confirm-гейтом (не тихо игнорируется execute_confirmed)
    assert "launch_campaign" in SUPPORTED_OPERATIONS


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


def _schema_mixed(keywords: list[str], mts: list[str]):
    from agent.tools.schemas import CreateSearchCampaign

    return CreateSearchCampaign(
        campaign_name="Цветы",
        final_url="https://example.com/",
        headlines=["Доставка цветов", "Букеты от 299", "Свежие розы"],
        descriptions=["Большой выбор букетов.", "Доставка за час."],
        budget_daily_micros=50_000_000,
        keywords=keywords,
        keyword_match_types=mts,
    )


def test_schema_mixed_match_types_dedups_by_pair_not_by_text():
    """C2: один текст с РАЗНЫМИ типами — законные разные критерии Google, оба остаются. Раньше
    field-валидатор дедупил ключи ПО ТЕКСТУ (2→1), а типы не трогал → ValueError «не совпадает по
    длине» → кнопка «Создать черновик» не работала НИКОГДА при смешанных типах."""
    m = _schema_mixed(
        ["used cars", " used cars ", "used cars", "cheap cars"],
        ["exact", "broad", "exact", "phrase"],  # 3-й — точный дубль пары (схлопнется)
    )
    assert m.keywords == ["used cars", "used cars", "cheap cars"]
    assert m.keyword_match_types == ["exact", "broad", "phrase"]


def test_schema_mixed_match_types_still_rejects_length_mismatch():
    with pytest.raises(Exception, match="1:1|длине"):
        _schema_mixed(["a", "b"], ["exact"])


def test_schema_without_match_types_dedups_by_text():
    from agent.tools.schemas import CreateSearchCampaign

    m = CreateSearchCampaign(
        campaign_name="Цветы",
        final_url="https://example.com/",
        headlines=["Доставка цветов", "Букеты от 299", "Свежие розы"],
        descriptions=["Большой выбор букетов.", "Доставка за час."],
        budget_daily_micros=50_000_000,
        keywords=["розы", " розы ", "тюльпаны"],  # прежнее поведение: дедуп по тексту
    )
    assert m.keywords == ["розы", "тюльпаны"]

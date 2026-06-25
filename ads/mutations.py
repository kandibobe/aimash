"""Изменяющие операции Google Ads. ЕДИНСТВЕННОЕ место, где код реально меняет аккаунт.

ЖЁСТКО (два независимых гейта на каждой функции):
1. Замок аккаунта: ensure_allowed(customer_id) — менять можно ТОЛЬКО Aimash Draft (7753643025).
2. confirm-гейт: валидный confirmation_id (подтверждённый proposal в audit_log).
Любая из проверок не прошла — PermissionError, изменение не выполняется.
Модель/агент сюда не ходит напрямую. См. skill confirm-gate-audit и golden rules в CLAUDE.md.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

from ads.client import ensure_allowed
from ads.validation import assert_keyword_ok, normalize_keywords

# Длину/форму ключевых слов считает КОД (golden rule #4) — единый источник в ads.validation.
# Алиас сохранён для обратной совместимости (тесты/вызовы mutations._assert_keyword_ok).
_assert_keyword_ok = assert_keyword_ok

# Абсолютный потолок суммы (micros) — защита от галлюцинации/инъекции модели СВЕРХ диапазон-
# валидации схемы (set_to/increase_by_amount без верхней границы). Это не бизнес-лимит, а
# «очевидно неверно» граница: 1 000 000 единиц валюты * 1e6. Считает КОД, у самой границы SDK.
MAX_AMOUNT_MICROS = 1_000_000 * 1_000_000


class ConfirmStore(Protocol):
    async def claim(
        self, confirmation_id: str, *, operation: str
    ) -> "ConfirmedProposal | None": ...
    async def finalize(self, confirmation_id: str, *, result: object) -> None: ...


class ConfirmedProposal(Protocol):
    operation: str
    status: str  # "confirmed" | "executing" | "applied" | "failed" | "rejected" | "pending"
    user_initiated: bool  # True только если изменение пришло прямой командой пользователя


async def _require_confirmation(
    confirm_store: ConfirmStore, confirmation_id: str, operation: str
) -> ConfirmedProposal:
    """Authoritative-гейт исполнения: АТОМАРНО столбит подтверждённый черновик (confirmed →
    executing) под эту операцию. None ⇒ нет/не подтверждён/чужая операция/уже выполнялся
    (replay) ⇒ PermissionError. Один confirmation_id исполняется не более одного раза."""
    proposal = await confirm_store.claim(confirmation_id, operation=operation)
    if proposal is None:
        raise PermissionError(
            f"мутация '{operation}' без валидного/одноразового confirmation_id — отклонено"
        )
    return proposal


# ── Пример: изменение бюджета (фаза 1) ─────────────────────────────────────────
async def apply_update_budget(
    *,
    customer_id: str,
    campaign_id: str,
    new_budget_micros: int,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    # Гейт 1 — замок аккаунта (до всего остального): только Aimash Draft.
    ensure_allowed(customer_id)

    # Валидация диапазонов В КОДЕ (не доверять модели) — ДО claim, чтобы плохие данные
    # не «съели» одноразовый черновик.
    if new_budget_micros <= 0:
        raise ValueError("бюджет должен быть > 0")
    if new_budget_micros > MAX_AMOUNT_MICROS:
        raise ValueError("бюджет подозрительно большой — проверь команду (>1 000 000)")

    # Гейт 2 — confirm-гейт (АТОМАРНО столбит черновик: confirmed → executing).
    proposal = await _require_confirmation(confirm_store, confirmation_id, "update_budget")

    # Бюджет — ТОЛЬКО по прямой команде пользователя (никогда из scheduler/anomaly)
    if not proposal.user_initiated:
        raise PermissionError("изменение бюджета должно быть прямой командой пользователя")

    # Реальный вызов SDK (google-ads синхронный → в потоке). _apply_budget_via_sdk вынесен
    # отдельно, чтобы юнит-тест мог подменить его (офлайн, без живого аккаунта).
    result = await asyncio.to_thread(
        _apply_budget_via_sdk, ads_client, customer_id, campaign_id, new_budget_micros
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Пауза кампании ─────────────────────────────────────────────────────────────
async def apply_pause_campaign(
    *,
    customer_id: str,
    campaign_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "pause_campaign")
    status = ads_client.enums.CampaignStatusEnum.PAUSED
    result = await asyncio.to_thread(
        _set_campaign_status_via_sdk, ads_client, customer_id, campaign_id, status
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Возобновление кампании (resume) ─────────────────────────────────────────────
async def apply_resume_campaign(
    *,
    customer_id: str,
    campaign_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "resume_campaign")
    status = ads_client.enums.CampaignStatusEnum.ENABLED
    result = await asyncio.to_thread(
        _set_campaign_status_via_sdk, ads_client, customer_id, campaign_id, status
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Ставка CPC на уровне групп объявлений ───────────────────────────────────────
async def apply_update_bid(
    *,
    customer_id: str,
    campaign_id: str,
    bids: list[tuple[str, int]],  # [(ad_group_id, new_cpc_bid_micros), ...]
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)

    # Валидация диапазонов В КОДЕ (не доверять модели) — ДО claim.
    if not bids:
        raise ValueError("нет групп объявлений для изменения ставки")
    for ad_group_id, micros in bids:
        if int(micros) <= 0:
            raise ValueError(f"ставка должна быть > 0 (ad_group {ad_group_id})")
        if int(micros) > MAX_AMOUNT_MICROS:
            raise ValueError(f"ставка подозрительно большая (ad_group {ad_group_id})")

    proposal = await _require_confirmation(confirm_store, confirmation_id, "update_bid")

    # Ставки — деньги: меняем только прямой командой пользователя (defense-in-depth,
    # сверх golden rule #3 о бюджете). Scheduler/anomaly ставки не двигают.
    if not proposal.user_initiated:
        raise PermissionError("изменение ставки должно быть прямой командой пользователя")

    result = await asyncio.to_thread(_apply_bid_via_sdk, ads_client, customer_id, campaign_id, bids)
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Добавление ключевых слов (в группы объявлений кампании) ──────────────────────
async def apply_add_keywords(
    *,
    customer_id: str,
    ad_group_ids: list[str],
    keywords: list[str],
    match_type: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    # Длину/дубли считает КОД (golden rule #4) — ДО claim.
    if not ad_group_ids:
        raise ValueError("нет групп объявлений для добавления ключевых слов")
    clean = normalize_keywords(keywords)
    await _require_confirmation(confirm_store, confirmation_id, "add_keywords")
    result = await asyncio.to_thread(
        _add_keywords_via_sdk, ads_client, customer_id, ad_group_ids, clean, match_type
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Добавление минус-слов (на уровне кампании) ──────────────────────────────────
async def apply_add_negative_keywords(
    *,
    customer_id: str,
    campaign_id: str,
    keywords: list[str],
    match_type: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    clean = normalize_keywords(keywords)  # длину/дубли считает КОД — ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "add_negative_keywords")
    result = await asyncio.to_thread(
        _add_negative_keywords_via_sdk, ads_client, customer_id, campaign_id, clean, match_type
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Гео-таргетинг по точке с радиусом (A-geo: исполнитель готов, активация — см. service) ─
async def apply_set_geo_proximity(
    *,
    customer_id: str,
    campaign_id: str,
    radius_km: float,
    address: dict,  # {"city_name":..., "country_code":..., опц. street_address/postal_code/...}
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    if radius_km <= 0:
        raise ValueError("радиус должен быть > 0")
    await _require_confirmation(confirm_store, confirmation_id, "set_geo_proximity")
    result = await asyncio.to_thread(
        _set_geo_proximity_via_sdk, ads_client, customer_id, campaign_id, radius_km, address
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Реальные SDK-исполнители (синхронные; зовутся через asyncio.to_thread) ───────
def _apply_budget_via_sdk(
    client, customer_id: str, campaign_id: str, new_budget_micros: int
) -> dict:
    ga = client.get_service("GoogleAdsService")
    budget_rn = None
    for row in ga.search(
        customer_id=str(customer_id),
        query=f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.id = {int(campaign_id)}",
    ):
        budget_rn = row.campaign.campaign_budget
        break
    if not budget_rn:
        raise ValueError(f"кампания {campaign_id} не найдена")
    svc = client.get_service("CampaignBudgetService")
    op = client.get_type("CampaignBudgetOperation")
    op.update.resource_name = budget_rn
    op.update.amount_micros = int(new_budget_micros)
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
    svc.mutate_campaign_budgets(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "new_budget_micros": int(new_budget_micros),
        "applied": True,
    }


def _set_campaign_status_via_sdk(client, customer_id: str, campaign_id: str, status) -> dict:
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.update.resource_name = svc.campaign_path(str(customer_id), str(campaign_id))
    op.update.status = status
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
    svc.mutate_campaigns(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "status": getattr(status, "name", str(status)),
        "applied": True,
    }


def _apply_bid_via_sdk(client, customer_id: str, campaign_id: str, bids: list) -> dict:
    """Ставка CPC на группах объявлений. ВАЖНО (v24): cpc_bid_micros действует только при
    ручной стратегии MANUAL_CPC; при автостратегиях SDK вернёт BID_TYPE_AND_BIDDING_STRATEGY_MISMATCH.
    Поэтому СНАЧАЛА читаем стратегию кампании и пускаем мутацию только для MANUAL_CPC.
    bids — [(ad_group_id, new_cpc_bid_micros), ...] (все группы одной кампании)."""
    ga = client.get_service("GoogleAdsService")
    bst = None  # campaign.bidding_strategy_type
    portfolio_rn = ""  # непустой => прикреплена портфельная (shared) стратегия
    for row in ga.search(
        customer_id=str(customer_id),
        query=(
            "SELECT campaign.bidding_strategy_type, campaign.bidding_strategy "
            f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
        ),
    ):
        bst = row.campaign.bidding_strategy_type
        portfolio_rn = row.campaign.bidding_strategy
        break
    if bst is None:
        raise ValueError(f"кампания {campaign_id} не найдена")
    if portfolio_rn:  # тип портфельной стратегии — авторитетный
        for row in ga.search(
            customer_id=str(customer_id),
            query=(
                "SELECT bidding_strategy.type FROM bidding_strategy "
                f"WHERE bidding_strategy.resource_name = '{portfolio_rn}'"
            ),
        ):
            bst = row.bidding_strategy.type
            break
    if bst != client.enums.BiddingStrategyTypeEnum.MANUAL_CPC:
        raise ValueError(
            "ставку CPC можно менять только при ручной стратегии MANUAL_CPC; текущая — "
            f"{getattr(bst, 'name', bst)} (автостратегия управляет ставками сама)"
        )

    svc = client.get_service("AdGroupService")
    ops = []
    for ad_group_id, micros in bids:
        op = client.get_type("AdGroupOperation")
        op.update.resource_name = svc.ad_group_path(str(customer_id), str(ad_group_id))
        op.update.cpc_bid_micros = int(micros)
        client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
        ops.append(op)
    try:
        svc.mutate_ad_groups(customer_id=str(customer_id), operations=ops)
    except GoogleAdsException as ex:
        # Сравниваем по ИМЕНИ enum-значения: AdGroupErrorEnum — это тип ОШИБКИ (v24.errors),
        # его НЕТ в client.enums (только enums-модуль) → обращение к client.enums.AdGroupErrorEnum
        # упало бы AttributeError и проглотило исходную ошибку. .name есть у любого enum-поля
        # (для не-ad_group ошибок вернётся 'UNSPECIFIED'), сравнение версионно-независимо.
        if any(
            err.error_code.ad_group_error.name == "BID_TYPE_AND_BIDDING_STRATEGY_MISMATCH"
            for err in ex.failure.errors
        ):
            raise ValueError(
                "ставка несовместима со стратегией кампании (BID_TYPE_AND_BIDDING_STRATEGY_MISMATCH)"
            ) from ex
        raise
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "ad_groups": [{"ad_group_id": str(ag), "new_cpc_bid_micros": int(m)} for ag, m in bids],
        "applied": True,
    }


def _add_keywords_via_sdk(
    client, customer_id: str, ad_group_ids: list, keywords: list, match_type: str
) -> dict:
    """Создаёт позитивные ключевые слова (ad_group_criterion) во ВСЕХ группах кампании.
    CREATE — БЕЗ update_mask. Метод mutate_ad_group_criteria (мн.ч.), тип Operation (ед.ч.)."""
    ag_svc = client.get_service("AdGroupService")
    svc = client.get_service("AdGroupCriterionService")
    mt = getattr(client.enums.KeywordMatchTypeEnum, str(match_type).upper())
    enabled = client.enums.AdGroupCriterionStatusEnum.ENABLED
    ops = []
    for ad_group_id in ad_group_ids:
        ag_rn = ag_svc.ad_group_path(str(customer_id), str(ad_group_id))
        for text in keywords:
            op = client.get_type("AdGroupCriterionOperation")
            op.create.ad_group = ag_rn
            op.create.status = enabled
            op.create.keyword.text = text
            op.create.keyword.match_type = mt
            ops.append(op)
    resp = svc.mutate_ad_group_criteria(customer_id=str(customer_id), operations=ops)
    return {
        "customer_id": customer_id,
        "ad_group_ids": [str(a) for a in ad_group_ids],
        "match_type": str(match_type),
        "created": [r.resource_name for r in resp.results],
        "count": len(resp.results),
        "applied": True,
    }


def _add_negative_keywords_via_sdk(
    client, customer_id: str, campaign_id: str, keywords: list, match_type: str
) -> dict:
    """Минус-слова НА УРОВНЕ КАМПАНИИ (campaign_criterion, negative=True — обязателен, immutable).
    CREATE — БЕЗ update_mask. Это НЕ shared negative list (другой флоу через SharedSetService)."""
    svc = client.get_service("CampaignCriterionService")
    cmp_svc = client.get_service("CampaignService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))
    mt = getattr(client.enums.KeywordMatchTypeEnum, str(match_type).upper())
    ops = []
    for text in keywords:
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = campaign_rn
        op.create.negative = True  # ОБЯЗАТЕЛЬНО: это исключение (минус-слово)
        op.create.keyword.text = text
        op.create.keyword.match_type = mt
        ops.append(op)
    resp = svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=ops)
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "match_type": str(match_type),
        "resource_names": [r.resource_name for r in resp.results],
        "count": len(resp.results),
        "applied": True,
    }


def _set_geo_proximity_via_sdk(
    client, customer_id: str, campaign_id: str, radius_km: float, address: dict
) -> dict:
    """A-geo: радиус-таргетинг (campaign_criterion.proximity). Address-driven — Google сам
    вычисляет точку, геокодинг на нашей стороне НЕ нужен. proximity IMMUTABLE (сменить = remove+create).
    address — структурные поля (минимум city_name + country_code); free-form строка сюда не годится."""
    cmp_svc = client.get_service("CampaignService")
    svc = client.get_service("CampaignCriterionService")
    op = client.get_type("CampaignCriterionOperation")
    crit = op.create
    crit.campaign = cmp_svc.campaign_path(str(customer_id), str(campaign_id))
    prox = crit.proximity
    prox.radius = float(radius_km)
    prox.radius_units = client.enums.ProximityRadiusUnitsEnum.KILOMETERS  # код решает, не модель
    for field in ("street_address", "city_name", "province_code", "postal_code", "country_code"):
        val = address.get(field)
        if val:
            setattr(prox.address, field, val)
    resp = svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "radius_km": float(radius_km),
        "resource_name": resp.results[0].resource_name if resp.results else None,
        "applied": True,
    }

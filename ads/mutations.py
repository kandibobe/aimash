"""Изменяющие операции Google Ads. ЕДИНСТВЕННОЕ место, где код реально меняет аккаунт.

ЖЁСТКО (два независимых гейта на каждой функции):
1. Замок аккаунта: ensure_allowed(customer_id) — менять можно ТОЛЬКО Aimash Draft (7753643025).
2. confirm-гейт: валидный confirmation_id (подтверждённый proposal в audit_log).
Любая из проверок не прошла — PermissionError, изменение не выполняется.
Модель/агент сюда не ходит напрямую. См. skill confirm-gate-audit и golden rules в CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

from adcopy.validate import (
    RSA_MAX_DESCRIPTIONS,
    RSA_MAX_HEADLINES,
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
)
from adcopy.validate import validate as _rsa_validate
from ads.client import ensure_allowed
from ads.validation import assert_keyword_ok, normalize_keywords
from core.resilience import run_ads_call  # таймаут+ретрай на самом SDK-вызове (не на гейтах)

# Длину/форму ключевых слов считает КОД (golden rule #4) — единый источник в ads.validation.
# Алиас сохранён для обратной совместимости (тесты/вызовы mutations._assert_keyword_ok).
_assert_keyword_ok = assert_keyword_ok

# Абсолютный потолок суммы (micros) — защита от галлюцинации/инъекции модели СВЕРХ диапазон-
# валидации схемы (set_to/increase_by_amount без верхней границы). Это не бизнес-лимит, а
# «очевидно неверно» граница: 1 000 000 единиц валюты * 1e6. Считает КОД, у самой границы SDK.
MAX_AMOUNT_MICROS = 1_000_000 * 1_000_000

# Лимиты состава RSA — единый источник в adcopy.validate; зеркалят agent.tools.schemas.CreateRsa
# (два независимых гейта: схема + SDK). Длину каждого элемента считает КОД (golden rule #4).


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
    result = await run_ads_call(
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
    result = await run_ads_call(
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
    result = await run_ads_call(
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

    result = await run_ads_call(_apply_bid_via_sdk, ads_client, customer_id, campaign_id, bids)
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
    result = await run_ads_call(
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
    result = await run_ads_call(
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
    result = await run_ads_call(
        _set_geo_proximity_via_sdk, ads_client, customer_id, campaign_id, radius_km, address
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Создание RSA-объявления (фаза 2.C: применение сгенерированных текстов к группе) ─
def _validate_rsa_inputs(
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    path1: str | None,
    path2: str | None,
) -> None:
    """Полная валидация набора RSA В КОДЕ — ДО claim (плохие данные не «съедают» черновик).
    Длину каждого элемента (кириллица=1) и минимумы/максимумы считает КОД, не модель."""
    if not RSA_MIN_HEADLINES <= len(headlines) <= RSA_MAX_HEADLINES:
        raise ValueError(
            f"RSA требует {RSA_MIN_HEADLINES}–{RSA_MAX_HEADLINES} заголовков "
            f"(передано: {len(headlines)})"
        )
    if not RSA_MIN_DESCRIPTIONS <= len(descriptions) <= RSA_MAX_DESCRIPTIONS:
        raise ValueError(
            f"RSA требует {RSA_MIN_DESCRIPTIONS}–{RSA_MAX_DESCRIPTIONS} описаний "
            f"(передано: {len(descriptions)})"
        )
    if not final_url or not str(final_url).startswith(("http://", "https://")):
        raise ValueError("нужен валидный final_url (http/https)")
    for h in headlines:
        ok, n = _rsa_validate(h, "headline")
        if not ok:
            raise ValueError(f"заголовок превышает 30 ({n}): «{h}»")
    for d in descriptions:
        ok, n = _rsa_validate(d, "description")
        if not ok:
            raise ValueError(f"описание превышает 90 ({n}): «{d}»")
    if path2 and not path1:
        raise ValueError("path2 нельзя без path1 (ограничение Google Ads)")
    for p, label in ((path1, "path1"), (path2, "path2")):
        if p:
            ok, n = _rsa_validate(p, "path")
            if not ok:
                raise ValueError(f"{label} превышает 15 ({n}): «{p}»")


async def apply_create_rsa(
    *,
    customer_id: str,
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    path1: str | None = None,
    path2: str | None = None,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта

    # Валидация набора В КОДЕ (golden rule #4) — ДО claim. Это НЕ денежная операция, поэтому
    # user_initiated не требуем (как add_keywords): объявление создаётся со статусом PAUSED.
    if not ad_group_id:
        raise ValueError("не указана группа объявлений (ad_group_id)")
    _validate_rsa_inputs(headlines, descriptions, final_url, path1, path2)

    await _require_confirmation(confirm_store, confirmation_id, "create_rsa")  # гейт 2 — claim
    result = await run_ads_call(
        _create_rsa_via_sdk,
        ads_client,
        customer_id,
        ad_group_id,
        headlines,
        descriptions,
        final_url,
        path1,
        path2,
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Реальные SDK-исполнители (синхронные; зовутся через core.resilience.run_ads_call) ───
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


def _create_rsa_via_sdk(
    client,
    customer_id: str,
    ad_group_id: str,
    headlines: list,
    descriptions: list,
    final_url: str,
    path1: str | None,
    path2: str | None,
) -> dict:
    """Создаёт RSA-объявление (ad_group_ad с responsive_search_ad). CREATE — БЕЗ update_mask.
    ВАЖНО (v24): final_urls живёт на .ad (не на responsive_search_ad), обязателен ≥1; каждый
    заголовок/описание — отдельный AdTextAsset.text; статус PAUSED зашит в КОДЕ (golden rule:
    безопасность — модель не решает; объявление не показывается до ручного включения)."""
    ag_svc = client.get_service("AdGroupService")
    svc = client.get_service("AdGroupAdService")
    op = client.get_type("AdGroupAdOperation")
    aga = op.create
    aga.ad_group = ag_svc.ad_group_path(str(customer_id), str(ad_group_id))
    aga.status = client.enums.AdGroupAdStatusEnum.PAUSED  # 0 расхода: создаём на паузе
    aga.ad.final_urls.append(str(final_url))  # обязателен ≥1; поле на .ad
    rsa = aga.ad.responsive_search_ad
    for text in headlines:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.headlines.append(asset)
    for text in descriptions:
        asset = client.get_type("AdTextAsset")
        asset.text = text
        rsa.descriptions.append(asset)
    if path1:
        rsa.path1 = path1
        if path2:  # path2 допустим только при заданном path1 (прото v24)
            rsa.path2 = path2
    resp = svc.mutate_ad_group_ads(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "resource_name": resp.results[0].resource_name if resp.results else None,
        "headlines": len(headlines),
        "descriptions": len(descriptions),
        "final_url": str(final_url),
        "status": "PAUSED",
        "applied": True,
    }


# ── Создание GDN-кампании из фото (§11): фото→Asset→Display→группа→RDA, всё PAUSED ─
GDN_MAX_HEADLINES = 5
GDN_MAX_DESCRIPTIONS = 5
GDN_BUSINESS_NAME_MAX = 25


def _validate_gdn_inputs(
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
) -> None:
    """Полная валидация В КОДЕ — ДО claim. Длину (кириллица=1), составы, URL и бюджет считает КОД."""
    if not 1 <= len(headlines) <= GDN_MAX_HEADLINES:
        raise ValueError(
            f"GDN требует 1–{GDN_MAX_HEADLINES} заголовков (передано {len(headlines)})"
        )
    if not 1 <= len(descriptions) <= GDN_MAX_DESCRIPTIONS:
        raise ValueError(
            f"GDN требует 1–{GDN_MAX_DESCRIPTIONS} описаний (передано {len(descriptions)})"
        )
    for h in headlines:
        ok, n = _rsa_validate(h, "headline")  # ≤30, кириллица=1
        if not ok:
            raise ValueError(f"заголовок превышает лимит ({n}/30): «{h}»")
    ok, n = _rsa_validate(long_headline, "description")  # длинный заголовок ≤90
    if not ok:
        raise ValueError(f"длинный заголовок превышает лимит ({n}/90)")
    for d in descriptions:
        ok, n = _rsa_validate(d, "description")  # ≤90
        if not ok:
            raise ValueError(f"описание превышает лимит ({n}/90): «{d}»")
    if not business_name or len(business_name) > GDN_BUSINESS_NAME_MAX:
        raise ValueError(f"название бизнеса 1–{GDN_BUSINESS_NAME_MAX} символов")
    if not final_url or not str(final_url).startswith(("http://", "https://")):
        raise ValueError("нужен валидный final_url (http/https)")
    if budget_daily_micros <= 0:
        raise ValueError("дневной бюджет должен быть > 0")
    if budget_daily_micros > MAX_AMOUNT_MICROS:
        raise ValueError("дневной бюджет подозрительно большой — проверь команду")


async def apply_create_gdn_campaign(
    *,
    customer_id: str,
    campaign_name: str,
    landscape_bytes: bytes,
    square_bytes: bytes,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    cpc_bid_micros: int = 500_000,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Создать GDN-кампанию (Display) из фото за двойным гейтом. Все сущности — PAUSED (0 расхода).

    НЕ через run_ads_call: цепочка из 5 создающих вызовов НЕ идемпотентна (авто-ретрай породил бы
    дубли). От повторного исполнения защищает атомарный claim confirm-гейта; при сбое — record_failure,
    осиротевшие PAUSED-сущности безвредны (0 расхода), пользователь начинает заново."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_gdn_inputs(
        headlines, long_headline, descriptions, business_name, final_url, budget_daily_micros
    )
    if not landscape_bytes or not square_bytes:
        raise ValueError("нужны подготовленные изображения (landscape + square)")
    proposal = await _require_confirmation(confirm_store, confirmation_id, "create_gdn_campaign")
    # Создание кампании — ТОЛЬКО прямой командой пользователя (как бюджет): bot ставит флаг, агент нет.
    if not proposal.user_initiated:
        raise PermissionError("создание кампании должно быть прямой командой пользователя")
    result = await asyncio.to_thread(
        _create_gdn_campaign_via_sdk,
        ads_client,
        customer_id,
        campaign_name=campaign_name,
        landscape_bytes=landscape_bytes,
        square_bytes=square_bytes,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
        business_name=business_name,
        final_url=final_url,
        budget_micros=int(budget_daily_micros),
        cpc_bid_micros=int(cpc_bid_micros),
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _create_gdn_campaign_via_sdk(
    client,
    customer_id: str,
    *,
    campaign_name: str,
    landscape_bytes: bytes,
    square_bytes: bytes,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_micros: int,
    cpc_bid_micros: int,
) -> dict:
    """Синхронная 5-шаговая цепочка v24 (сверено live): asset×2 → budget → campaign(DISPLAY,PAUSED)
    → ad group(PAUSED) → responsive_display_ad(PAUSED). Статусы PAUSED зашиты в КОДЕ → 0 расхода.
    Имя кампании — как у пользователя; бюджет/группа получают stamp-суффикс для уникальности."""
    from ads.assets import upload_image_asset

    cid = str(customer_id)
    stamp = str(int(time.time()))

    # 1) Image-ассеты (landscape 1.91:1 + square 1:1) — режет КОД из одного фото.
    land_rn = upload_image_asset(client, cid, landscape_bytes, f"{campaign_name}_land_{stamp}")
    sq_rn = upload_image_asset(client, cid, square_bytes, f"{campaign_name}_sq_{stamp}")

    # 2) Бюджет.
    budget_svc = client.get_service("CampaignBudgetService")
    bop = client.get_type("CampaignBudgetOperation")
    bop.create.name = f"{campaign_name}_budget_{stamp}"
    bop.create.amount_micros = int(budget_micros)
    bop.create.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    bop.create.explicitly_shared = False
    budget_rn = (
        budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[bop])
        .results[0]
        .resource_name
    )

    # 3) Кампания (DISPLAY, PAUSED, manual CPC, только контентная сеть).
    camp_svc = client.get_service("CampaignService")
    cop = client.get_type("CampaignOperation")
    c = cop.create
    c.name = campaign_name
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
    c.status = client.enums.CampaignStatusEnum.PAUSED  # код решает — не показывается до включения
    c.campaign_budget = budget_rn
    c.manual_cpc.enhanced_cpc_enabled = False
    c.network_settings.target_google_search = False
    c.network_settings.target_search_network = False
    c.network_settings.target_content_network = True
    c.network_settings.target_partner_search_network = False
    try:  # v24 может требовать декларацию EU-политической рекламы при создании
        c.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except Exception:  # noqa: BLE001 — поле опционально на части аккаунтов
        pass
    campaign_rn = (
        camp_svc.mutate_campaigns(customer_id=cid, operations=[cop]).results[0].resource_name
    )

    # 4) Группа объявлений (DISPLAY, PAUSED).
    ag_svc = client.get_service("AdGroupService")
    agop = client.get_type("AdGroupOperation")
    ag = agop.create
    ag.name = f"{campaign_name}_ag_{stamp}"
    ag.campaign = campaign_rn
    ag.status = client.enums.AdGroupStatusEnum.PAUSED
    ag.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ag.cpc_bid_micros = int(cpc_bid_micros)
    ad_group_rn = (
        ag_svc.mutate_ad_groups(customer_id=cid, operations=[agop]).results[0].resource_name
    )

    # 5) Адаптивное медийное объявление (PAUSED).
    ad_svc = client.get_service("AdGroupAdService")
    adop = client.get_type("AdGroupAdOperation")
    aga = adop.create
    aga.ad_group = ad_group_rn
    aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
    aga.ad.final_urls.append(str(final_url))
    rda = aga.ad.responsive_display_ad

    def _img(asset_rn):
        a = client.get_type("AdImageAsset")
        a.asset = asset_rn
        return a

    def _txt(text):
        a = client.get_type("AdTextAsset")
        a.text = text
        return a

    rda.marketing_images.append(_img(land_rn))
    rda.square_marketing_images.append(_img(sq_rn))
    for h in headlines:
        rda.headlines.append(_txt(h))
    rda.long_headline.text = long_headline
    for d in descriptions:
        rda.descriptions.append(_txt(d))
    rda.business_name = business_name
    ad_rn = ad_svc.mutate_ad_group_ads(customer_id=cid, operations=[adop]).results[0].resource_name

    return {
        "customer_id": cid,
        "campaign_name": campaign_name,
        "campaign": campaign_rn,
        "budget": budget_rn,
        "ad_group": ad_group_rn,
        "ad": ad_rn,
        "image_assets": [land_rn, sq_rn],
        "headlines": len(headlines),
        "descriptions": len(descriptions),
        "status": "PAUSED",
        "applied": True,
    }

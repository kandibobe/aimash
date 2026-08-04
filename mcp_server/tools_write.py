"""Typed Bias-for-Action proposal tools for Hermes.

Each call autonomously resolves live state and validates a minimal typed input, then returns one
exact Proposal. Google Ads remains unchanged until a separate trusted reply/button calls the sole
``execute_confirmed`` entrypoint.
"""

from __future__ import annotations

import uuid
from functools import wraps
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from llm.schemas import (
    MUTATION_TOOLS,
    SCHEMAS,
    AddCallAsset,
    AddCallouts,
    AddKeywords,
    AddNegativeKeywords,
    AddNegativesToSharedSet,
    AddPriceAsset,
    AddPromotion,
    AddSitelinks,
    AddStructuredSnippets,
    AttachImageAsset,
    AttachAudience,
    AttachSharedSet,
    CreateDemandGenCampaign,
    CreateGdnCampaign,
    CreateAppCampaign,
    CreateRsa,
    CreateSearchCampaign,
    CreateVideoCampaign,
    DetachAudience,
    LaunchCampaign,
    PauseAd,
    PauseAdGroup,
    PauseCampaign,
    RemoveAd,
    RemoveAdGroup,
    RemoveAssetLink,
    RemoveCampaign,
    RemoveKeywords,
    RemoveNegativeKeywords,
    ResumeAd,
    ResumeAdGroup,
    ResumeCampaign,
    SetBiddingStrategy,
    SetCampaignDisplayNetwork,
    SetCampaignGeoTargetType,
    SetCampaignNetwork,
    SetGeoLocation,
    SetGeoProximity,
    UpdateBid,
    UpdateBudget,
    UpdateCampaign,
    UpdateKeywordBid,
)
from confirm.reverse import ROLLBACKABLE_OPS, reverse_spec
from confirm.risk import TIERS, risk_tier
from confirm.store import ConfirmStore
from core import i18n
from core.config import settings
from core.context import get_context
from core.logging import log
from core.provenance import get_provenance
from mcp_server.envelope import classify_error, proposed, refused
from mcp_server.propose import ProposalRefused, build_proposal
from mcp_server.redact import redact_error


# Операции, которые начинают или меняют расход, разрешены только из доверенного
# человеческого хода. Исполнитель повторяет эту проверку после confirm-claim;
# здесь ранний отказ не даёт scheduler/self-improve создать заведомо неисполнимый
# или опасный черновик.
_HUMAN_ONLY_OPS = MUTATION_TOOLS


def _validation_text(exc: ValidationError) -> str:
    """Компактный (loc: msg) первых ошибок Pydantic — чтобы агент понял, ЧТО переформулировать. Не
    сырой repr исключения: отдаём только (поле, сообщение), а весь текст оборачиваем i18n-ключом."""
    parts = []
    for err in exc.errors()[:4]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "?"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return i18n.t("propose_bad_params", details="; ".join(parts))


def _execution_failure(exc: BaseException, *, error_code: str) -> dict[str, Any]:
    """Ошибка execution → единый Self-Healing JSON для Hermes."""
    from ads.mutations import mutation_error_hint

    message = redact_error(exc)
    hint = mutation_error_hint(exc)
    return {
        "ok": False,
        "status": "failed",
        "error": message,  # legacy clients
        "error_code": error_code,  # legacy clients
        "error_type": hint["error_type"],
        "message": message,
        "suggested_action": hint["suggested_action"],
    }


async def _propose(
    operation: str,
    model_cls: type[BaseModel],
    *,
    account: str,
    **fields: Any,
) -> dict[str, Any]:
    """Собрать и сохранить аттестованный Proposal без выполнения мутации.

    ЛЮБОЙ отказ — редактированный `refused()`-конверт: сырой str(e) наружу не идёт. Успех всегда
    возвращает один ``APPROVAL_REQUIRED``. Human-turn разрешает подготовку Proposal, но не является
    подтверждением его выполнения.

    Порядок гейтов fail-closed и значим:
      1) валидация входа моделью (диапазоны/режимы/валюта — КОД, не доверие);
      2) провенанс: денежный черновик только человеческим ходом (правило 3);
      3) контекст хода: черновику нужен чат доставки/подтверждения (fail-closed);
      4) атомарная idempotent-вставка: replay возвращает существующий Proposal;
      5) возврат карточки; execute возможен только отдельным trusted reply/button.
    """
    lang = i18n.current_lang()
    try:
        model = model_cls(**fields)
    except ValidationError as e:
        return refused(_validation_text(e), error_code="invalid_argument")

    prov = get_provenance()
    if operation in _HUMAN_ONLY_OPS and not prov.human_turn:
        return refused(i18n.t("propose_requires_human", lang), error_code="refused")
    chat_id = get_context().chat_id
    if chat_id is None:
        return refused(i18n.t("propose_no_turn_context", lang), error_code="refused")
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    try:
        from mcp_server.trusted_transport import TrustedTransportError, get_trusted_turn

        try:
            trusted_turn = get_trusted_turn()
            trusted_user_text = trusted_turn.message_text or ""
        except TrustedTransportError:
            trusted_turn = None
            trusted_user_text = ""

        model_args = model.model_dump(exclude_none=True)
        idempotency_args = {"account": str(account), **model_args}

        built = await build_proposal(
            store=store,
            operation=operation,
            params=model_args,
            cid=cid,
            chat_id=chat_id,
            customer_id=str(account),
            user_text=trusted_user_text if prov.human_turn else "",
            lang=lang,
            user_initiated=prov.human_turn,
            source_message_id=trusted_turn.message_id if trusted_turn is not None else None,
            idempotency_args=idempotency_args if trusted_turn is not None else None,
        )
    except ProposalRefused as e:
        return refused(e.text, error_code="refused")
    except Exception as e:  # noqa: BLE001
        log.warning("mcp propose tool failed: %s", type(e).__name__)
        return refused(redact_error(e), error_code=classify_error(e))
    env = proposed(
        confirmation_id=built.cid,
        operation=built.operation,
        customer_id=built.customer_id,
        preview=built.display,
    )
    proposal_status = getattr(built, "status", "pending")
    if proposal_status != "pending":
        return {
            **env,
            "status": "already_exists",
            "proposal_status": proposal_status,
            "reused": True,
            "preview": built.display,
            "message": "Повторный вызов: возвращён ранее созданный Proposal.",
            "suggested_action": None,
        }
    message = "Изменение ожидает одного подтверждения всей карточки."
    return {
        **env,
        "status": "approval_required",
        "ok": False,
        "error": message,
        "error_code": "approval_required",
        "error_type": "APPROVAL_REQUIRED",
        "message": message,
        "reused": bool(getattr(built, "reused", False)),
        "suggested_action": "Покажи пользователю preview и запроси одно подтверждение всего изменения.",
    }


# ── Бюджет и ставки ──


async def propose_budget_change(
    account: str,
    campaign: str,
    mode: str,
    value: float,
    currency: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК изменения дневного бюджета кампании и показать «было → станет». Google Ads НЕ
    изменяется. Денежная операция: только по прямой команде человека.

    account — id аккаунта (10 цифр). campaign — точное имя кампании.
    mode — increase_by_percent | increase_by_amount | decrease_by_percent | decrease_by_amount | set_to.
    value — всегда положительное число. currency — код валюты (USD/AUD/…) только если явно названа."""
    return await _propose(
        "update_budget",
        UpdateBudget,
        account=account,
        campaign=campaign,
        mode=mode,
        value=value,
        currency=currency,
    )


async def propose_bid_change(
    account: str,
    campaign: str,
    mode: str,
    value: float,
    currency: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК изменения ставки CPC кампании (уровень групп). Осмысленно только на
    MANUAL_CPC/ECPC. Денежная операция: только по прямой команде человека.

    mode — increase_by_percent | decrease_by_percent | set_to."""
    return await _propose(
        "update_bid",
        UpdateBid,
        account=account,
        campaign=campaign,
        mode=mode,
        value=value,
        currency=currency,
    )


async def propose_keyword_bid_change(
    account: str,
    campaign: str,
    keyword: str,
    mode: str,
    value: float,
    ad_group: str | None = None,
    match_type: str | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК изменения ставки CPC на уровне КЛЮЧА. Денежная операция."""
    return await _propose(
        "update_keyword_bid",
        UpdateKeywordBid,
        account=account,
        campaign=campaign,
        keyword=keyword,
        mode=mode,
        value=value,
        ad_group=ad_group,
        match_type=match_type,
        currency=currency,
    )


async def propose_set_bidding_strategy(
    account: str,
    campaign: str,
    strategy: str,
    target_cpa: float | None = None,
    target_roas: float | None = None,
    enhanced_cpc: bool = False,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК смены стратегии назначения ставок. Денежная операция.

    strategy — manual_cpc | maximize_conversions | maximize_conversion_value | target_spend."""
    return await _propose(
        "set_bidding_strategy",
        SetBiddingStrategy,
        account=account,
        campaign=campaign,
        strategy=strategy,
        target_cpa=target_cpa,
        target_roas=target_roas,
        enhanced_cpc=enhanced_cpc,
    )


# ── Ключевые слова ──


async def propose_add_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str,
    ad_group: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления ключевых слов в кампанию. >20 ключей → .xlsx-вложение.

    match_type — broad | phrase | exact."""
    return await _propose(
        "add_keywords",
        AddKeywords,
        account=account,
        campaign=campaign,
        keywords=keywords,
        match_type=match_type,
        ad_group=ad_group,
    )


async def propose_remove_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК удаления ключевых слов из кампании."""
    return await _propose(
        "remove_keywords",
        RemoveKeywords,
        account=account,
        campaign=campaign,
        keywords=keywords,
        match_type=match_type,
    )


# ── Минус-слова ──


async def propose_add_negative_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str = "broad",
    ad_group: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления минус-слов на уровень кампании (или группы через ad_group)."""
    return await _propose(
        "add_negative_keywords",
        AddNegativeKeywords,
        account=account,
        campaign=campaign,
        keywords=keywords,
        match_type=match_type,
        ad_group=ad_group,
    )


async def propose_remove_negative_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str = "broad",
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК снятия минус-слов кампании."""
    return await _propose(
        "remove_negative_keywords",
        RemoveNegativeKeywords,
        account=account,
        campaign=campaign,
        keywords=keywords,
        match_type=match_type,
    )


async def propose_add_negatives_to_shared_set(
    account: str,
    shared_set: str,
    keywords: list[str],
    match_type: str = "broad",
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления минус-слов в ОБЩИЙ СПИСОК аккаунта. Список будет создан если нет."""
    return await _propose(
        "add_negatives_to_shared_set",
        AddNegativesToSharedSet,
        account=account,
        shared_set=shared_set,
        keywords=keywords,
        match_type=match_type,
    )


async def propose_attach_shared_set(
    account: str,
    campaign: str,
    shared_set: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК привязки существующего shared-списка минус-слов к кампании."""
    return await _propose(
        "attach_shared_set",
        AttachSharedSet,
        account=account,
        campaign=campaign,
        shared_set=shared_set,
    )


# ── Кампании ──


async def propose_pause_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК паузы кампании."""
    return await _propose("pause_campaign", PauseCampaign, account=account, campaign=campaign)


async def propose_resume_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК возобновления кампании."""
    return await _propose("resume_campaign", ResumeCampaign, account=account, campaign=campaign)


async def propose_launch_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК полного запуска кампании, её групп и объявлений."""
    return await _propose("launch_campaign", LaunchCampaign, account=account, campaign=campaign)


async def propose_update_campaign(
    account: str,
    campaign: str,
    new_name: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК переименования кампании."""
    return await _propose(
        "update_campaign", UpdateCampaign, account=account, campaign=campaign, new_name=new_name
    )


async def propose_remove_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК удаления кампании (НЕОБРАТИМО)."""
    return await _propose("remove_campaign", RemoveCampaign, account=account, campaign=campaign)


async def propose_set_campaign_network(
    account: str,
    campaign: str,
    search_partners: bool,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК вкл/выкл поисковых партнёров на кампании."""
    return await _propose(
        "set_campaign_network",
        SetCampaignNetwork,
        account=account,
        campaign=campaign,
        search_partners=search_partners,
    )


async def propose_set_campaign_display_network(
    account: str,
    campaign: str,
    display_network: bool,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК вкл/выкл КМС (контекстно-медийной сети) на поисковой кампании."""
    return await _propose(
        "set_campaign_display_network",
        SetCampaignDisplayNetwork,
        account=account,
        campaign=campaign,
        display_network=display_network,
    )


async def propose_set_campaign_geo_target_type(
    account: str,
    campaign: str,
    geo_target_type: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК изменения типа гео-таргетинга кампании.

    geo_target_type — PRESENCE (только физически в регионе) | PRESENCE_OR_INTEREST (Google-дефолт)."""
    return await _propose(
        "set_campaign_geo_target_type",
        SetCampaignGeoTargetType,
        account=account,
        campaign=campaign,
        geo_target_type=geo_target_type,
    )


# ── Группы объявлений ──


async def propose_pause_ad_group(
    account: str,
    campaign: str,
    ad_group: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК паузы группы объявлений."""
    return await _propose(
        "pause_ad_group", PauseAdGroup, account=account, campaign=campaign, ad_group=ad_group
    )


async def propose_resume_ad_group(
    account: str,
    campaign: str,
    ad_group: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК возобновления группы объявлений."""
    return await _propose(
        "resume_ad_group", ResumeAdGroup, account=account, campaign=campaign, ad_group=ad_group
    )


async def propose_remove_ad_group(
    account: str,
    campaign: str,
    ad_group: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК удаления группы объявлений (НЕОБРАТИМО)."""
    return await _propose(
        "remove_ad_group", RemoveAdGroup, account=account, campaign=campaign, ad_group=ad_group
    )


# ── Объявления ──


async def propose_pause_ad(
    account: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК паузы отдельного объявления. ad — id или фрагмент заголовка."""
    return await _propose(
        "pause_ad", PauseAd, account=account, campaign=campaign, ad_group=ad_group, ad=ad
    )


async def propose_resume_ad(
    account: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК возобновления отдельного объявления."""
    return await _propose(
        "resume_ad", ResumeAd, account=account, campaign=campaign, ad_group=ad_group, ad=ad
    )


async def propose_remove_ad(
    account: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК удаления объявления (НЕОБРАТИМО)."""
    return await _propose(
        "remove_ad", RemoveAd, account=account, campaign=campaign, ad_group=ad_group, ad=ad
    )


# ── ГЕО-таргетинг ──


async def propose_set_geo_proximity(
    account: str,
    campaign: str,
    radius_km: float,
    city_name: str,
    country_code: str = "",
    street_address: str | None = None,
    postal_code: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК радиус-таргетинга кампании вокруг города.

    radius_km — км (1–2000). city_name — название города. country_code — ISO alpha-2 (опц.)."""
    return await _propose(
        "set_geo_proximity",
        SetGeoProximity,
        account=account,
        campaign=campaign,
        radius_km=radius_km,
        city_name=city_name,
        country_code=country_code,
        street_address=street_address,
        postal_code=postal_code,
    )


async def propose_set_geo_location(
    account: str,
    campaign: str,
    locations: list[str],
    country_code: str = "",
    locale: str = "",
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК гео-таргетинга кампании по стране/городу/региону.

    locations — названия локаций (напр. ['Германия', 'Берлин'])."""
    return await _propose(
        "set_geo_location",
        SetGeoLocation,
        account=account,
        campaign=campaign,
        locations=locations,
        country_code=country_code,
        locale=locale,
    )


# ── Аудитории ──


async def propose_attach_audience(
    account: str,
    campaign: str,
    audience_resource_names: list[str],
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК прикрепления аудиторий (user_list/audience) к кампании."""
    return await _propose(
        "attach_audience",
        AttachAudience,
        account=account,
        campaign=campaign,
        audience_resource_names=audience_resource_names,
    )


async def propose_detach_audience(
    account: str,
    campaign: str,
    audience_resource_names: list[str],
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК открепления аудиторий от кампании."""
    return await _propose(
        "detach_audience",
        DetachAudience,
        account=account,
        campaign=campaign,
        audience_resource_names=audience_resource_names,
    )


# ── RSA ──


async def propose_create_rsa(
    account: str,
    campaign: str,
    ad_group_id: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    campaign_id: str | None = None,
    path1: str | None = None,
    path2: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК создания адаптивного поискового объявления (RSA).

    headlines — 3–15 заголовков (≤30 символов). descriptions — 2–4 описания (≤90 символов)."""
    return await _propose(
        "create_rsa",
        CreateRsa,
        account=account,
        campaign=campaign,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        final_url=final_url,
        headlines=headlines,
        descriptions=descriptions,
        path1=path1,
        path2=path2,
    )


# ── Создание кампаний ──


async def propose_create_search_campaign(
    account: str,
    campaign_name: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    budget_daily_micros: int,
    keywords: list[str] | None = None,
    match_type: str = "phrase",
    geo_locations: list[str] | None = None,
    languages: list[str] | None = None,
    cpc_bid_micros: int | None = None,
    path1: str | None = None,
    path2: str | None = None,
    keyword_match_types: list[str] | None = None,
    geo_country_code: str | None = None,
    geo_locale: str | None = None,
    bidding: dict[str, Any] | None = None,
    url_options: dict[str, Any] | None = None,
    asset_specs: list[dict[str, Any]] | None = None,
    existing_asset_links: list[dict[str, Any]] | None = None,
    image_media_ids: list[str] | None = None,
    networks: str | None = None,
    ad_schedule_blocks: list[dict[str, Any]] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК поисковой (Search) кампании. Денежная операция.

    budget_daily_micros — дневной бюджет в микро-единицах валюты аккаунта."""
    return await _propose(
        "create_search_campaign",
        CreateSearchCampaign,
        account=account,
        campaign_name=campaign_name,
        final_url=final_url,
        headlines=headlines,
        descriptions=descriptions,
        budget_daily_micros=budget_daily_micros,
        keywords=keywords or [],
        match_type=match_type,
        keyword_match_types=keyword_match_types or [],
        geo_locations=geo_locations or [],
        geo_country_code=geo_country_code or settings.geo_default_country,
        geo_locale=geo_locale or settings.geo_default_locale,
        languages=languages or [],
        cpc_bid_micros=cpc_bid_micros,
        bidding=bidding,
        path1=path1,
        path2=path2,
        url_options=url_options,
        asset_specs=asset_specs or [],
        existing_asset_links=existing_asset_links or [],
        image_media_ids=image_media_ids or [],
        networks=networks,
        ad_schedule_blocks=ad_schedule_blocks or [],
        start_date=start_date,
        end_date=end_date,
    )


async def propose_create_gdn_campaign(
    account: str,
    campaign_name: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    media_id: str,
    geo_locations: list[str] | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК GDN-кампании из фото. Денежная операция."""
    return await _propose(
        "create_gdn_campaign",
        CreateGdnCampaign,
        account=account,
        campaign_name=campaign_name,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
        business_name=business_name,
        final_url=final_url,
        budget_daily_micros=budget_daily_micros,
        media_id=media_id,
        geo_locations=geo_locations or [],
    )


async def propose_create_demand_gen_campaign(
    account: str,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    goal: str = "clicks",
    logo_media_id: str | None = None,
    geo_locations: list[str] | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК Demand Gen кампании из YouTube-видео. Денежная операция."""
    return await _propose(
        "create_demand_gen_campaign",
        CreateDemandGenCampaign,
        account=account,
        campaign_name=campaign_name,
        youtube_video_id=youtube_video_id,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
        business_name=business_name,
        final_url=final_url,
        budget_daily_micros=budget_daily_micros,
        goal=goal,
        logo_media_id=logo_media_id,
        geo_locations=geo_locations or [],
    )


async def propose_create_video_campaign(
    account: str,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    geo_locations: list[str] | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК Video-кампании (YouTube). Денежная операция."""
    return await _propose(
        "create_video_campaign",
        CreateVideoCampaign,
        account=account,
        campaign_name=campaign_name,
        youtube_video_id=youtube_video_id,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
        business_name=business_name,
        final_url=final_url,
        budget_daily_micros=budget_daily_micros,
        geo_locations=geo_locations or [],
    )


async def propose_create_app_campaign(
    account: str,
    campaign_name: str,
    app_id: str,
    app_store: str,
    headlines: list[str],
    descriptions: list[str],
    budget_daily_micros: int,
    target_cpa_micros: int,
    image_media_ids: list[str] | None = None,
    youtube_video_ids: list[str] | None = None,
    geo_locations: list[str] | None = None,
    languages: list[str] | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК App/UAC-кампании из trusted media и/или YouTube-видео."""
    return await _propose(
        "create_app_campaign",
        CreateAppCampaign,
        account=account,
        campaign_name=campaign_name,
        app_id=app_id,
        app_store=app_store,
        headlines=headlines,
        descriptions=descriptions,
        budget_daily_micros=budget_daily_micros,
        target_cpa_micros=target_cpa_micros,
        image_media_ids=image_media_ids or [],
        youtube_video_ids=youtube_video_ids or [],
        geo_locations=geo_locations or [],
        languages=languages or [],
    )


# ── Расширения (ассеты) ──


async def propose_add_sitelinks(
    account: str,
    campaign: str,
    sitelinks: list[dict[str, str]],
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления быстрых ссылок в кампанию.

    sitelinks — список [{link_text, final_url, description1?, description2?}], макс 20."""
    return await _propose(
        "add_sitelinks", AddSitelinks, account=account, campaign=campaign, sitelinks=sitelinks
    )


async def propose_add_callouts(
    account: str,
    campaign: str,
    callouts: list[str],
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления уточнений (callouts) в кампанию. callouts — список строк ≤25 символов."""
    return await _propose(
        "add_callouts", AddCallouts, account=account, campaign=campaign, callouts=callouts
    )


async def propose_add_structured_snippets(
    account: str,
    campaign: str,
    header: str,
    values: list[str],
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления структурированных описаний в кампанию."""
    return await _propose(
        "add_structured_snippets",
        AddStructuredSnippets,
        account=account,
        campaign=campaign,
        header=header,
        values=values,
    )


async def propose_attach_image_asset(
    account: str,
    campaign: str,
    media_id: str,
    name: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК привязки подготовленного изображения к кампании.

    media_id должен быть получен из trusted ``ingest_media``; бинарь не передаётся модели и
    загружается только после единственного человеческого подтверждения.
    """
    return await _propose(
        "attach_image_asset",
        AttachImageAsset,
        account=account,
        campaign=campaign,
        media_id=media_id,
        name=name,
    )


async def propose_add_call_asset(
    account: str,
    campaign: str,
    phone_number: str,
    country_code: str = "",
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления номера телефона в кампанию."""
    return await _propose(
        "add_call_asset",
        AddCallAsset,
        account=account,
        campaign=campaign,
        phone_number=phone_number,
        country_code=country_code,
    )


async def propose_add_promotion(
    account: str,
    campaign: str,
    occasion: str,
    discount_type: str,
    discount_value: float,
    currency: str,
    final_url: str = "",
    promotion_start: str = "",
    promotion_end: str = "",
    headline: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления промо-акции в кампанию."""
    return await _propose(
        "add_promotion",
        AddPromotion,
        account=account,
        campaign=campaign,
        occasion=occasion,
        discount_type=discount_type,
        discount_value=discount_value,
        currency=currency,
        final_url=final_url,
        promotion_start=promotion_start,
        promotion_end=promotion_end,
        headline=headline,
        description=description,
    )


async def propose_add_price_asset(
    account: str,
    campaign: str,
    offerings: list[dict[str, Any]],
    currency: str,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК добавления прайс-листа в кампанию.

    offerings — список [{header, description, price, unit, final_url?}], 3–8 элементов."""
    return await _propose(
        "add_price_asset",
        AddPriceAsset,
        account=account,
        campaign=campaign,
        offerings=offerings,
        currency=currency,
    )


async def propose_remove_asset_link(
    account: str,
    campaign: str,
    link_resource_names: list[str],
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК удаления привязки ассетов к кампании."""
    return await _propose(
        "remove_asset_link",
        RemoveAssetLink,
        account=account,
        campaign=campaign,
        link_resource_names=link_resource_names,
    )


async def propose_composite_change(
    account: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Создать ОДИН черновик для 2–10 обратимых изменений одного аккаунта.

    Каждый элемент: ``{"operation": "pause_campaign", "params": {...}}``. В пакет допускаются
    только операции с детерминированной компенсацией; создание и удаление сущностей отклоняются.
    Если пакет одновременно переименовывает кампанию, остальные children могут адресовать её как
    текущим, так и запрошенным новым именем — bridge нормализует ссылку к текущему снимку.
    """
    from ads.composite import CaptureStore
    from mcp_server.trusted_transport import TrustedTransportError, get_trusted_turn

    lang = i18n.current_lang()
    if not isinstance(operations, list) or not 2 <= len(operations) <= 10:
        return refused(
            "Пакет должен содержать от 2 до 10 изменений.", error_code="invalid_argument"
        )

    normalized: list[tuple[str, BaseModel]] = []
    try:
        for index, item in enumerate(operations, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"operations.{index}: expected object")
            operation = str(item.get("operation") or "").strip()
            if operation not in ROLLBACKABLE_OPS:
                raise ValueError(
                    f"operations.{index}.operation: {operation or '?'} is not safely rollbackable"
                )
            params = item.get("params")
            if not isinstance(params, dict):
                raise ValueError(f"operations.{index}.params: expected object")
            normalized.append((operation, SCHEMAS[operation](**params)))
    except ValidationError as e:
        return refused(_validation_text(e), error_code="invalid_argument")
    except (KeyError, TypeError, ValueError) as e:
        return refused(redact_error(e), error_code="invalid_argument")

    # Hermes естественно связывает следующий child с результатом предыдущего: rename A→B, затем
    # budget(campaign=B). Но все proposal-снимки снимаются ДО выполнения batch, когда существует A.
    # Нормализуем post-rename alias кодом, чтобы корректность composite не зависела от того, какое из
    # двух понятных человеку имён выбрала модель. Цепочки/циклы rename здесь намеренно отклоняем:
    # они требуют отдельной семантики порядка, а не угадывания.
    rename_aliases: dict[str, str] = {}
    rename_sources: set[str] = set()
    try:
        for operation, model in normalized:
            if operation != "update_campaign":
                continue
            params = model.model_dump(exclude_none=True)
            current_name = str(params.get("campaign") or "")
            new_name = str(params.get("new_name") or "")
            if new_name in rename_aliases or current_name in rename_aliases:
                raise ValueError(
                    "chained or duplicate campaign renames are not supported in one batch"
                )
            rename_aliases[new_name] = current_name
            rename_sources.add(current_name)
        if rename_sources & rename_aliases.keys():
            raise ValueError("chained campaign renames are not supported in one batch")

        normalized = [
            (
                operation,
                type(model)(
                    **{
                        **model.model_dump(exclude_none=True),
                        "campaign": rename_aliases.get(
                            str(model.model_dump(exclude_none=True).get("campaign") or ""),
                            model.model_dump(exclude_none=True).get("campaign"),
                        ),
                    }
                ),
            )
            if operation != "update_campaign"
            and model.model_dump(exclude_none=True).get("campaign") in rename_aliases
            else (operation, model)
            for operation, model in normalized
        ]
    except (TypeError, ValueError, ValidationError) as e:
        return refused(redact_error(e), error_code="invalid_argument")

    prov = get_provenance()
    if any(operation in _HUMAN_ONLY_OPS for operation, _ in normalized) and not prov.human_turn:
        return refused(i18n.t("propose_requires_human", lang), error_code="refused")
    chat_id = get_context().chat_id
    if chat_id is None:
        return refused(i18n.t("propose_no_turn_context", lang), error_code="refused")
    store = ConfirmStore()

    try:
        try:
            trusted_turn = get_trusted_turn()
            trusted_user_text = trusted_turn.message_text or ""
        except TrustedTransportError:
            trusted_turn = None
            trusted_user_text = ""
        idempotency_args = {
            "account": str(account),
            "operations": [
                {"operation": operation, "params": model.model_dump(exclude_none=True)}
                for operation, model in normalized
            ],
        }
        children: list[dict[str, Any]] = []
        previews: list[str] = []
        tiers: list[str] = []
        for index, (operation, model) in enumerate(normalized, start=1):
            built = await build_proposal(
                store=CaptureStore(),
                operation=operation,
                params=model.model_dump(exclude_none=True),
                cid=f"composite-{index}",
                chat_id=chat_id,
                customer_id=str(account),
                user_text=trusted_user_text if prov.human_turn else "",
                lang=lang,
                user_initiated=prov.human_turn,
            )
            if reverse_spec(operation, built.params, built.params.get("_before")) is None:
                raise ProposalRefused(
                    f"Изменение {index} ({operation}) нельзя надёжно откатить по текущему снимку."
                )
            children.append({"operation": operation, "params": built.params})
            previews.append(f"{index}. {built.display}")
            tiers.append(risk_tier(operation, built.params))

        cid = uuid.uuid4().hex
        summary = "Пакет изменений:\n\n" + "\n\n".join(previews)
        max_tier = max(tiers, key=TIERS.index)
        saved = await store.save_proposal(
            confirmation_id=cid,
            operation="composite",
            customer_id=str(account),
            params={"operations": children},
            summary=summary,
            chat_id=chat_id,
            user_initiated=prov.human_turn,
            risk_tier=max_tier,
            source_message_id=trusted_turn.message_id if trusted_turn is not None else None,
            idempotency_args=idempotency_args if trusted_turn is not None else None,
        )
        cid = saved.confirmation_id
        summary = saved.summary
    except ProposalRefused as e:
        return refused(e.text, error_code="refused")
    except Exception as e:  # noqa: BLE001
        log.warning("mcp composite propose failed: %s", type(e).__name__)
        return refused(redact_error(e), error_code=classify_error(e))
    return proposed(
        confirmation_id=cid,
        operation="composite",
        customer_id=str(account),
        preview=summary,
    )


# ── Исполнение ──


async def execute_confirmed() -> dict[str, Any]:
    """Подтвердить и выполнить черновик только по доверенному Telegram reply-якорю.

    У инструмента намеренно нет ``account``/``confirmation_id`` аргументов модели. Идентичность,
    chat/message namespace и confirmation marker берутся из HMAC-проверенного gateway envelope;
    затем существующий атомарный reply-CAS подтверждает автора и якорь, а сервис исполняет черновик
    на ``proposal.customer_id`` с повторным allow-list gate и audit-row.
    """
    from ads.composite import execute_confirmed_composite
    from ads.service import confirm_and_execute_by_reply
    from clients.execute import MEMORY_OPERATIONS, execute_confirmed_memory
    from core.texts import fmt_mutation_result
    from mcp_server.trusted_transport import TrustedTransportError, get_trusted_turn

    store = ConfirmStore()
    try:
        turn = get_trusted_turn()
        confirmation_id = turn.reply_confirmation_id
        if not (
            turn.reply_to_is_own_message
            and turn.reply_to_message_id is not None
            and confirmation_id is not None
            and turn.reply_to_text is not None
        ):
            raise TrustedTransportError(
                "подтверждение должно быть реплаем на карточку Aimash с confirmation marker"
            )
        proposal = await store.get_confirmed(confirmation_id)
        if (
            proposal is None
            or proposal.status != "pending"
            or proposal.summary not in turn.reply_to_text
        ):
            raise PermissionError("reply не содержит неизменённый diff подтверждаемого черновика")
        if not await store.bind_card_message_id_from_verified_reply(
            confirmation_id,
            turn.reply_to_message_id,
            actor_user_id=turn.actor_user_id,
            actor_chat_id=turn.actor_chat_id,
        ):
            raise PermissionError("не удалось привязать доверенный reply-якорь")
        operation = getattr(proposal, "operation", "")
        if operation in MEMORY_OPERATIONS or operation == "composite":
            confirmed = await store.confirm_by_reply(
                confirmation_id,
                actor_user_id=turn.actor_user_id,
                actor_chat_id=turn.actor_chat_id,
                reply_to_message_id=turn.reply_to_message_id,
                actor_username=turn.actor_username,
            )
            if not confirmed:
                raise PermissionError("trusted reply confirmation was rejected")
            if operation == "composite":
                await execute_confirmed_composite(store, confirmation_id)
            else:
                await execute_confirmed_memory(store, confirmation_id)
        else:
            await confirm_and_execute_by_reply(
                store,
                confirmation_id=confirmation_id,
                actor_user_id=turn.actor_user_id,
                actor_chat_id=turn.actor_chat_id,
                reply_to_message_id=turn.reply_to_message_id,
                actor_username=turn.actor_username,
            )
        applied = await store.get_applied_audit_result(confirmation_id)
        if applied is None:
            log.error("execute_confirmed: applied audit недоступен cid=%s", confirmation_id)
            return {
                "status": "needs_review",
                "error": "Изменение могло примениться, но подтверждённый итог audit недоступен. Нужна проверка оператором.",
                "error_code": "audit_unavailable",
                "confirmation_id": confirmation_id,
            }
        if applied.operation == "composite":
            count = int((applied.result or {}).get("operation_count") or 0)
            summary = (
                f"✅ Composite change applied: {count} operations"
                if str(turn.language_code).lower().startswith("en")
                else f"✅ Пакет изменений выполнен: {count} операций"
            )
        elif applied.operation in MEMORY_OPERATIONS:
            action = {
                "profile_save": ("saved", "сохранён"),
                "profile_update": ("updated", "обновлён"),
                "profile_clear": ("cleared", "очищен"),
            }[applied.operation]
            summary = (
                f"✅ Client profile {action[0]}: {applied.customer_id}"
                if str(turn.language_code).lower().startswith("en")
                else f"✅ Профиль клиента {applied.customer_id}: {action[1]}"
            )
        else:
            summary = fmt_mutation_result(
                applied.operation,
                applied.result,
                lang=turn.language_code,
            )
        if not summary.strip():
            log.error("execute_confirmed: пустой audit summary cid=%s", confirmation_id)
            return {
                "status": "needs_review",
                "error": "Изменение могло примениться, но итог audit не удалось отобразить. Нужна проверка оператором.",
                "error_code": "audit_unrenderable",
                "confirmation_id": confirmation_id,
            }
        return {
            "status": "executed",
            "operation": applied.operation,
            "summary": summary,
            "audit_result": applied.result,
            "customer_id": applied.customer_id,
            "confirmation_id": confirmation_id,
        }
    except ValueError as e:
        # Не найден / не в статусе confirmed
        return _execution_failure(e, error_code="invalid_argument")
    except PermissionError as e:
        return _execution_failure(e, error_code="refused")
    except Exception as e:  # noqa: BLE001
        log.warning("execute_confirmed failed: %s", type(e).__name__)
        return _execution_failure(e, error_code=classify_error(e))


# ── Реестр ──


def _action_tool(
    fn: Callable[..., Awaitable[dict[str, Any]]], operation: str
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Expose the existing typed callable with a short action-oriented tool description."""

    @wraps(fn)
    async def action(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await fn(*args, **kwargs)

    action.__doc__ = (
        f"Выполнить Google Ads operation `{operation}` по текущему trusted запросу. "
        "Инструмент сам читает недостающий live state и возвращает structured JSON result."
    )
    return action


ACTION_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "update_budget": _action_tool(propose_budget_change, "update_budget"),
    "update_bid": _action_tool(propose_bid_change, "update_bid"),
    "update_keyword_bid": _action_tool(propose_keyword_bid_change, "update_keyword_bid"),
    "set_bidding_strategy": _action_tool(propose_set_bidding_strategy, "set_bidding_strategy"),
    "add_keywords": _action_tool(propose_add_keywords, "add_keywords"),
    "remove_keywords": _action_tool(propose_remove_keywords, "remove_keywords"),
    "add_negative_keywords": _action_tool(propose_add_negative_keywords, "add_negative_keywords"),
    "remove_negative_keywords": _action_tool(
        propose_remove_negative_keywords, "remove_negative_keywords"
    ),
    "add_negatives_to_shared_set": _action_tool(
        propose_add_negatives_to_shared_set, "add_negatives_to_shared_set"
    ),
    "attach_shared_set": _action_tool(propose_attach_shared_set, "attach_shared_set"),
    "pause_campaign": _action_tool(propose_pause_campaign, "pause_campaign"),
    "resume_campaign": _action_tool(propose_resume_campaign, "resume_campaign"),
    "launch_campaign": _action_tool(propose_launch_campaign, "launch_campaign"),
    "update_campaign": _action_tool(propose_update_campaign, "update_campaign"),
    "remove_campaign": _action_tool(propose_remove_campaign, "remove_campaign"),
    "set_campaign_network": _action_tool(propose_set_campaign_network, "set_campaign_network"),
    "set_campaign_display_network": _action_tool(
        propose_set_campaign_display_network, "set_campaign_display_network"
    ),
    "set_campaign_geo_target_type": _action_tool(
        propose_set_campaign_geo_target_type, "set_campaign_geo_target_type"
    ),
    "pause_ad_group": _action_tool(propose_pause_ad_group, "pause_ad_group"),
    "resume_ad_group": _action_tool(propose_resume_ad_group, "resume_ad_group"),
    "remove_ad_group": _action_tool(propose_remove_ad_group, "remove_ad_group"),
    "pause_ad": _action_tool(propose_pause_ad, "pause_ad"),
    "resume_ad": _action_tool(propose_resume_ad, "resume_ad"),
    "remove_ad": _action_tool(propose_remove_ad, "remove_ad"),
    "set_geo_proximity": _action_tool(propose_set_geo_proximity, "set_geo_proximity"),
    "set_geo_location": _action_tool(propose_set_geo_location, "set_geo_location"),
    "attach_audience": _action_tool(propose_attach_audience, "attach_audience"),
    "detach_audience": _action_tool(propose_detach_audience, "detach_audience"),
    "create_rsa": _action_tool(propose_create_rsa, "create_rsa"),
    "create_search_campaign": _action_tool(
        propose_create_search_campaign, "create_search_campaign"
    ),
    "create_gdn_campaign": _action_tool(propose_create_gdn_campaign, "create_gdn_campaign"),
    "create_demand_gen_campaign": _action_tool(
        propose_create_demand_gen_campaign, "create_demand_gen_campaign"
    ),
    "create_video_campaign": _action_tool(propose_create_video_campaign, "create_video_campaign"),
    "create_app_campaign": _action_tool(propose_create_app_campaign, "create_app_campaign"),
    "add_sitelinks": _action_tool(propose_add_sitelinks, "add_sitelinks"),
    "add_callouts": _action_tool(propose_add_callouts, "add_callouts"),
    "add_structured_snippets": _action_tool(
        propose_add_structured_snippets, "add_structured_snippets"
    ),
    "attach_image_asset": _action_tool(propose_attach_image_asset, "attach_image_asset"),
    "add_call_asset": _action_tool(propose_add_call_asset, "add_call_asset"),
    "add_promotion": _action_tool(propose_add_promotion, "add_promotion"),
    "add_price_asset": _action_tool(propose_add_price_asset, "add_price_asset"),
    "remove_asset_link": _action_tool(propose_remove_asset_link, "remove_asset_link"),
}

# Python compatibility alias for legacy imports; live MCP names come from ACTION_TOOL_FUNCS.
PROPOSE_TOOL_FUNCS = ACTION_TOOL_FUNCS

EXECUTE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "execute_confirmed": execute_confirmed,
}
# Composite is an orchestration primitive, not a 43rd Google Ads mutation. Keep the
# 42 direct ACTION names exactly equal to ``MUTATION_TOOLS`` while exposing one public,
# action-oriented batch entrypoint that still creates a single pending proposal.
COMPOSITE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "composite_change": propose_composite_change,
}
PLAN_WRITE_TOOL_FUNCS = {
    **ACTION_TOOL_FUNCS,
    **COMPOSITE_TOOL_FUNCS,
    **EXECUTE_TOOL_FUNCS,
}

ACTION_MCP_TOOLS: frozenset[str] = frozenset(ACTION_TOOL_FUNCS)
PROPOSE_MCP_TOOLS = ACTION_MCP_TOOLS

# PLAN-only server imports PROPOSE_TOOL_FUNCS. The trusted live server uses
# PLAN_WRITE_TOOL_FUNCS and obtains actor/reply identity outside model arguments.
EXECUTE_MCP_TOOLS: frozenset[str] = frozenset(EXECUTE_TOOL_FUNCS)
COMPOSITE_MCP_TOOLS: frozenset[str] = frozenset(COMPOSITE_TOOL_FUNCS)
PLAN_WRITE_MCP_TOOLS: frozenset[str] = frozenset(PLAN_WRITE_TOOL_FUNCS)
# Backward-compatible names for guards and internal callers.
WRITE_TOOL_FUNCS = PLAN_WRITE_TOOL_FUNCS
WRITE_MCP_TOOLS: frozenset[str] = frozenset(WRITE_TOOL_FUNCS)

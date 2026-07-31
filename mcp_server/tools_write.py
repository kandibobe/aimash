"""PROPOSE-инструменты MCP-слоя (Волна 2, propose-only WRITE-MCP): агент СОЗДАЁТ черновик мутации и
показывает «было → станет», но Google Ads НЕ трогает. Мутация и подтверждение разделены (золотое
правило #1): здесь только левая половина — черновик; исполнение (execute_confirmed) — отдельный
инструмент, вызываемый строго после подтверждения пользователем.

Почему это безопасно: propose ничего не исполняет. `build_proposal` лишь ЧИТАЕТ
текущее значение («было») и пишет строку в НАШУ БД (`ConfirmStore.save_proposal`) — ни один
`ads/mutations.py::apply_*` отсюда не достижим. Значит гейт подтверждения propose не нужен, а
prompt-injection через внешний контент в худшем случае создаёт БЕЗвредный черновик, который человек
всё равно увидит и не подтвердит.

Три вещи этот слой делает КОДОМ, не доверием к модели:
  • **Провенанс (правило 3).** Денежный черновик (бюджет/ставка) создаётся только когда бит
    `human_turn` поднят доверенным входом (`core.provenance`) — не из cron/anomaly/self-improve и не
    по аргументу инструмента (агент его подделать не может). `user_initiated` черновика берётся из
    того же провенанса, а не из параметра.
  • **И8 (правило 13).** Не более ОДНОГО черновика на ассистентский ход. Счёт — свойство ХРАНИЛИЩА
    (`ConfirmStore.count_run_proposals` по run-корреляции), а не in-memory-счётчик: агентский цикл
    делает много последовательных итераций, и счётчик в процессе пережил бы не каждую.
  • **Валидация входа (правило 4).** Диапазоны/режимы/валюту проверяет Pydantic, кривой вход →
    редактированный отказ, а не «доверие к модели».

Границы слоя (правило 6, тонкий тул-слой): здесь ровно валидация входа + вызов существующего
`build_proposal` + сериализация конверта. Вся логика гейтов до кнопок — в `mcp_server.propose`;
счёт И8 — в `confirm.store`; провенанс — в `core.provenance`. Ни строки бизнес-логики тут.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from agent.tools.schemas import (
    MUTATION_TOOLS,
    AddCallAsset,
    AddCallouts,
    AddKeywords,
    AddNegativeKeywords,
    AddNegativesToSharedSet,
    AddPriceAsset,
    AddPromotion,
    AddSitelinks,
    AddStructuredSnippets,
    AttachAudience,
    AttachSharedSet,
    CreateDemandGenCampaign,
    CreateGdnCampaign,
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
from ads.resolve import MONEY_OPS
from confirm.store import ConfirmStore
from core import i18n
from core.context import get_context
from core.guards import require_no_mutations
from core.logging import log
from core.provenance import get_provenance
from mcp_server.envelope import classify_error, proposed, refused
from mcp_server.propose import ProposalRefused, build_proposal
from mcp_server.redact import redact_error


# Операции, которые начинают или меняют расход, разрешены только из доверенного
# человеческого хода. Исполнитель повторяет эту проверку после confirm-claim;
# здесь ранний отказ не даёт scheduler/self-improve создать заведомо неисполнимый
# или опасный черновик.
_HUMAN_ONLY_OPS = frozenset(MONEY_OPS) | {
    "launch_campaign",
    "create_search_campaign",
    "create_gdn_campaign",
    "create_demand_gen_campaign",
    "create_video_campaign",
}


def _validation_text(exc: ValidationError) -> str:
    """Компактный (loc: msg) первых ошибок Pydantic — чтобы агент понял, ЧТО переформулировать. Не
    сырой repr исключения: отдаём только (поле, сообщение), а весь текст оборачиваем i18n-ключом."""
    parts = []
    for err in exc.errors()[:4]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "?"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return i18n.t("propose_bad_params", details="; ".join(parts))


async def _propose(
    operation: str,
    model_cls: type[BaseModel],
    *,
    account: str,
    **fields: Any,
) -> dict[str, Any]:
    """Общая механика propose-инструмента. ЛЮБОЙ отказ — редактированный `refused()`-конверт (правило
    5: сырой str(e) наружу не идёт). Успех — `proposed()`-конверт с `preview` «было → станет».

    Порядок гейтов fail-closed и значим:
      1) валидация входа моделью (диапазоны/режимы/валюта — КОД, не доверие);
      2) провенанс: денежный черновик только человеческим ходом (правило 3);
      3) контекст хода: черновику нужен чат доставки/подтверждения (fail-closed);
      4) И8: не более одного черновика на ход (счёт из хранилища по run-корреляции);
      5) сборка+сохранение черновика (Google Ads не тронут).
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
    if await store.count_run_proposals(prov.run_id) >= 1:
        return refused(i18n.t("propose_draft_limit", lang), error_code="refused")
    cid = uuid.uuid4().hex
    try:
        built = await build_proposal(
            store=store,
            operation=operation,
            params=model.model_dump(exclude_none=True),
            cid=cid,
            chat_id=chat_id,
            customer_id=str(account),
            user_text=str(getattr(model, "currency", "") or ""),
            lang=lang,
            user_initiated=prov.human_turn,
        )
    except ProposalRefused as e:
        return refused(e.text, error_code="refused")
    except Exception as e:  # noqa: BLE001
        log.warning("mcp propose tool failed: %s", type(e).__name__)
        return refused(redact_error(e), error_code=classify_error(e))
    return proposed(
        confirmation_id=built.cid,
        operation=built.operation,
        customer_id=built.customer_id,
        preview=built.display,
    )


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
        geo_locations=geo_locations or [],
        languages=languages or [],
        cpc_bid_micros=cpc_bid_micros,
        path1=path1,
        path2=path2,
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


# ── Исполнение ──


async def execute_confirmed(
    account: str,
    confirmation_id: str,
) -> dict[str, Any]:
    """Выполнить ПОДТВЕРЖДЁННЫЙ черновик мутации. Вызывать ТОЛЬКО после явного подтверждения
    пользователем («да», «подтверждаю», ✅). ДО вызова обязательно показать preview черновика
    и дождаться подтверждения.

    account — id аккаунта (10 цифр). confirmation_id — из ответа propose_*.

    Возвращает: {status: 'executed' | 'failed', operation, summary, error?}"""
    from ads.service import execute_confirmed as _execute
    from core.config import normalize_customer_id as _ncid

    cid = _ncid(str(account))
    store = ConfirmStore()
    try:
        proposal = await store.get_confirmed(confirmation_id)
        if proposal is None:
            raise PermissionError("черновик не подтверждён или уже исполнен")
        proposal_cid = _ncid(str(proposal.customer_id))
        if proposal_cid != cid:
            # `ads.service.execute_confirmed` правильно берёт account из proposal. Сверка здесь
            # нужна, чтобы MCP-фасад не исполнил B, а отчитался как будто исполнил переданный A.
            raise PermissionError("аккаунт вызова не совпадает с подтверждённым черновиком")
        result = await _execute(store, confirmation_id)
        return {
            "status": "executed",
            "operation": result.get("operation", ""),
            "summary": result.get("display", ""),
            "customer_id": proposal_cid,
        }
    except ValueError as e:
        # Не найден / не в статусе confirmed
        return {"status": "failed", "error": redact_error(e), "error_code": "invalid_argument"}
    except PermissionError as e:
        return {"status": "failed", "error": redact_error(e), "error_code": "refused"}
    except Exception as e:  # noqa: BLE001
        log.warning("execute_confirmed failed: %s", type(e).__name__)
        return {"status": "failed", "error": redact_error(e), "error_code": classify_error(e)}


# ── Реестр ──


PROPOSE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "propose_budget_change": propose_budget_change,
    "propose_bid_change": propose_bid_change,
    "propose_keyword_bid_change": propose_keyword_bid_change,
    "propose_set_bidding_strategy": propose_set_bidding_strategy,
    "propose_add_keywords": propose_add_keywords,
    "propose_remove_keywords": propose_remove_keywords,
    "propose_add_negative_keywords": propose_add_negative_keywords,
    "propose_remove_negative_keywords": propose_remove_negative_keywords,
    "propose_add_negatives_to_shared_set": propose_add_negatives_to_shared_set,
    "propose_attach_shared_set": propose_attach_shared_set,
    "propose_pause_campaign": propose_pause_campaign,
    "propose_resume_campaign": propose_resume_campaign,
    "propose_launch_campaign": propose_launch_campaign,
    "propose_update_campaign": propose_update_campaign,
    "propose_remove_campaign": propose_remove_campaign,
    "propose_set_campaign_network": propose_set_campaign_network,
    "propose_set_campaign_display_network": propose_set_campaign_display_network,
    "propose_set_campaign_geo_target_type": propose_set_campaign_geo_target_type,
    "propose_pause_ad_group": propose_pause_ad_group,
    "propose_resume_ad_group": propose_resume_ad_group,
    "propose_remove_ad_group": propose_remove_ad_group,
    "propose_pause_ad": propose_pause_ad,
    "propose_resume_ad": propose_resume_ad,
    "propose_remove_ad": propose_remove_ad,
    "propose_set_geo_proximity": propose_set_geo_proximity,
    "propose_set_geo_location": propose_set_geo_location,
    "propose_attach_audience": propose_attach_audience,
    "propose_detach_audience": propose_detach_audience,
    "propose_create_rsa": propose_create_rsa,
    "propose_create_search_campaign": propose_create_search_campaign,
    "propose_create_gdn_campaign": propose_create_gdn_campaign,
    "propose_create_demand_gen_campaign": propose_create_demand_gen_campaign,
    "propose_create_video_campaign": propose_create_video_campaign,
    "propose_add_sitelinks": propose_add_sitelinks,
    "propose_add_callouts": propose_add_callouts,
    "propose_add_structured_snippets": propose_add_structured_snippets,
    "propose_add_call_asset": propose_add_call_asset,
    "propose_add_promotion": propose_add_promotion,
    "propose_add_price_asset": propose_add_price_asset,
    "propose_remove_asset_link": propose_remove_asset_link,
}

# И4 / construction-time: имена propose-инструментов НЕ пересекаются с мутационными.
require_no_mutations(
    PROPOSE_TOOL_FUNCS,
    MUTATION_TOOLS,
    rule="И4",
    subject="mcp_server.tools_write.PROPOSE_TOOL_FUNCS (propose-инструменты)",
)

PROPOSE_MCP_TOOLS: frozenset[str] = frozenset(PROPOSE_TOOL_FUNCS)

# Исполнение отделено от PLAN физически: plan_server импортирует только
# PROPOSE_TOOL_FUNCS. Этот реестр нужен доверенному transport/executor-коду,
# но не публикуется модели.
EXECUTE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "execute_confirmed": execute_confirmed,
}
EXECUTE_MCP_TOOLS: frozenset[str] = frozenset(EXECUTE_TOOL_FUNCS)
WRITE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    **PROPOSE_TOOL_FUNCS,
    **EXECUTE_TOOL_FUNCS,
}
WRITE_MCP_TOOLS: frozenset[str] = frozenset(WRITE_TOOL_FUNCS)

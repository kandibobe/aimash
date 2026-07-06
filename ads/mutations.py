"""Изменяющие операции Google Ads. ЕДИНСТВЕННОЕ место, где код реально меняет аккаунт.

ЖЁСТКО (два независимых гейта на каждой функции):
1. Замок аккаунта: ensure_allowed(customer_id) — менять можно ТОЛЬКО Aimash Draft (7753643025).
2. confirm-гейт: валидный confirmation_id (подтверждённый proposal в audit_log).
Любая из проверок не прошла — PermissionError, изменение не выполняется.
Модель/агент сюда не ходит напрямую. См. skill confirm-gate-audit и golden rules в CLAUDE.md.
"""

from __future__ import annotations

import re
import time
from typing import Protocol

from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers

from adcopy.validate import (
    RSA_MAX_DESCRIPTIONS,
    RSA_MAX_HEADLINES,
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
    STRUCTURED_SNIPPET_HEADERS,
    assert_asset_len,
)
from adcopy.validate import validate as _rsa_validate
from ads import extensions
from ads.client import ensure_allowed
from ads.resolve import gaql_escape  # единый GAQL-эскейп для literal-WHERE (defense-in-depth)
from ads.validation import assert_keyword_ok, normalize_keywords
from core.ads_errors import (  # единый источник имён кодов ошибок Google Ads
    error_code_names,
    partial_failure_errors,
)
from core.logging import redact_text  # причины отказов partial_failure — без секретов (правило #5)
from core.limits import (
    BILLING_UNIT_MICROS,
    MAX_RADIUS_KM,
    MONEY_MAX_MICROS,
    MONEY_MAX_UNITS,
    round_micros,
)  # единый источник порогов (defense-in-depth)
from core.resilience import (  # таймаут+ретрай на самом SDK-вызове (не на гейтах)
    run_ads_call,
    run_ads_create_call,  # НЕидемпотентные создатели: квота+таймаут+семафор БЕЗ ретраев
)

# Длину/форму ключевых слов считает КОД (golden rule #4) — единый источник в ads.validation.
# Алиас сохранён для обратной совместимости (тесты/вызовы mutations._assert_keyword_ok).
_assert_keyword_ok = assert_keyword_ok

# Абсолютный потолок суммы (micros) — защита от галлюцинации/инъекции модели СВЕРХ диапазон-
# валидации схемы (set_to/increase_by_amount без верхней границы). Это не бизнес-лимит, а
# «очевидно неверно» граница у самого SDK. Единый источник — core.limits (MONEY_MAX_MICROS);
# схема проверяет ту же границу в единицах валюты. Имена MAX_AMOUNT_MICROS/MAX_RADIUS_KM
# сохранены как модульные алиасы (на них ссылаются тесты).
MAX_AMOUNT_MICROS = MONEY_MAX_MICROS
# MAX_RADIUS_KM (proximity, лимит Google Ads) — тоже из core.limits; re-export для apply_* и тестов.

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


# ── Запуск кампании (§19.8/§11): включить ВСЮ структуру, не только кампанию ────────
# ОТДЕЛЬНАЯ операция от resume_campaign НАМЕРЕННО. `resume_campaign` включает ТОЛЬКО статус
# кампании — это правильно для /campaigns → «Возобновить», где менеджер мог осознанно держать
# часть групп/объявлений на паузе. Но визард §19 создаёт кампанию, группу И RSA-объявление
# ВСЕ в PAUSED (0 расхода до запуска); включение одной кампании оставило бы группу и объявление
# на паузе ⇒ показов НОЛЬ, а менеджер думал бы, что кампания идёт (тихий дефект). «Запустить» =
# включить кампанию + ВСЕ её (не-REMOVED) группы + ВСЕ (не-REMOVED) объявления. Идемпотентно
# (повторный ENABLED — no-op). НЕ денежная (бюджет задан при создании) → user_initiated НЕ требуется,
# как resume/pause. Оба обязательных гейта на месте: ensure_allowed (Draft-only) + _require_confirmation.
async def apply_launch_campaign(
    *,
    customer_id: str,
    campaign_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "launch_campaign")
    result = await run_ads_call(_launch_campaign_via_sdk, ads_client, customer_id, campaign_id)
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Переименование кампании (§3 «изменение» кампании) ────────────────────────────
# Единственная правка уровня campaign, не связанная с деньгами/статусом/стратегией: имя.
# НЕ денежная операция → user_initiated не требуется (как pause/resume). Оба гейта обязательны.
async def apply_update_campaign(
    *,
    customer_id: str,
    campaign_id: str,
    new_name: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    # Валидация В КОДЕ (не доверять модели) — ДО claim, чтобы плохой ввод не «съел» черновик.
    clean = (new_name or "").strip()
    if not clean:
        raise ValueError("новое имя кампании не может быть пустым")
    if len(clean) > 255:  # потолок Google Ads на имя кампании
        raise ValueError("имя кампании слишком длинное (>255 символов)")
    await _require_confirmation(confirm_store, confirmation_id, "update_campaign")
    result = await run_ads_call(
        _update_campaign_name_via_sdk, ads_client, customer_id, campaign_id, clean
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Пауза/возобновление ГРУППЫ объявлений (§16 AdGroupService) ───────────────────
# Зеркало pause/resume кампании, но статус живёт на ad_group. Деньги НЕ трогаются →
# user_initiated не требуется (как и для паузы кампании). Оба гейта обязательны.
async def apply_pause_ad_group(
    *,
    customer_id: str,
    ad_group_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "pause_ad_group")
    status = ads_client.enums.AdGroupStatusEnum.PAUSED
    result = await run_ads_call(
        _set_ad_group_status_via_sdk, ads_client, customer_id, ad_group_id, status
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_resume_ad_group(
    *,
    customer_id: str,
    ad_group_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "resume_ad_group")
    status = ads_client.enums.AdGroupStatusEnum.ENABLED
    result = await run_ads_call(
        _set_ad_group_status_via_sdk, ads_client, customer_id, ad_group_id, status
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Удаление кампании / группы (необратимо, status→REMOVED) ───────────────────────
# НЕ денежная операция (user_initiated не требуется, как pause/resume), НО необратимая — в UI
# закрыта ДВОЙНЫМ подтверждением (bot.keyboards.confirm_destructive_kb). Оба гейта обязательны:
# ensure_allowed (Draft-only) + _require_confirmation (atomic claim, one-shot, replay-защита).
async def apply_remove_campaign(
    *,
    customer_id: str,
    campaign_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "remove_campaign")
    result = await run_ads_call(_remove_campaign_via_sdk, ads_client, customer_id, campaign_id)
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_remove_ad_group(
    *,
    customer_id: str,
    ad_group_id: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    ensure_allowed(customer_id)
    await _require_confirmation(confirm_store, confirmation_id, "remove_ad_group")
    result = await run_ads_call(_remove_ad_group_via_sdk, ads_client, customer_id, ad_group_id)
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

    result = await run_ads_call(
        _apply_bid_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        bids,
        op_count=max(1, len(bids)),  # квота §3: по операции на каждую группу
    )
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
        _add_keywords_via_sdk,
        ads_client,
        customer_id,
        ad_group_ids,
        clean,
        match_type,
        op_count=len(ad_group_ids) * len(clean),  # квота §3: каждая mutate-операция батча
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
        _add_negative_keywords_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        clean,
        match_type,
        op_count=len(clean),  # квота §3: каждая mutate-операция батча
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Удаление минус-слов (симметрично add: по тексту+типу на уровне кампании) ──────
# НЕ деньги → user_initiated не требуется (как add). Оба гейта обязательны.
async def apply_remove_negative_keywords(
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
    clean = normalize_keywords(keywords)  # форму/дубли считает КОД — ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "remove_negative_keywords")
    result = await run_ads_call(
        _remove_negative_keywords_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        clean,
        match_type,
        op_count=len(clean),  # квота §3: оценка сверху (резолв может найти меньше)
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Гео-таргетинг по точке с радиусом (A-geo) ────────────────────────────────────
def _validate_address_fields(address: dict) -> None:
    """Структурный адрес для proximity — валидирует КОД ДО claim (golden rule #4): минимум
    city_name + country_code (ISO alpha-2). Плохой адрес не должен «съедать» одноразовый черновик."""
    city = str(address.get("city_name") or "").strip()
    cc = str(address.get("country_code") or "").strip()
    if not city or len(city) > 80:
        raise ValueError("city_name обязателен (1–80 символов)")
    if len(cc) != 2 or not cc.isalpha():
        raise ValueError("country_code — ISO-3166 alpha-2 (напр. UA)")
    for opt in ("street_address", "postal_code"):
        val = address.get(opt)
        if val is not None and len(str(val)) > 80:
            raise ValueError(f"{opt} слишком длинный (>80)")


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
    if radius_km > MAX_RADIUS_KM:
        raise ValueError(f"радиус подозрительно большой (>{MAX_RADIUS_KM} км) — проверь команду")
    _validate_address_fields(address)  # ДО claim: плохой адрес не «съедает» черновик
    await _require_confirmation(confirm_store, confirmation_id, "set_geo_proximity")
    result = await run_ads_call(
        _set_geo_proximity_via_sdk, ads_client, customer_id, campaign_id, radius_km, address
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Гео-таргетинг по стране/городу/региону (geoTargetConstants) ──────────────────
def _validate_locations(locations: list[str], country_code: str) -> None:
    """Гео по стране/городу — валидирует КОД ДО claim (golden rule #4): непустой список названий
    (1–80 символов каждое) + country_code ISO alpha-2. Плохой ввод не «съедает» черновик."""
    if not locations:
        raise ValueError("нужна хотя бы одна локация (страна/город/регион)")
    for s in locations:
        t = str(s).strip()
        if not t or len(t) > 80:
            raise ValueError("название локации — 1–80 символов")
    cc = str(country_code or "").strip()
    if len(cc) != 2 or not cc.isalpha():
        raise ValueError("country_code — ISO-3166 alpha-2 (напр. UA)")


async def apply_set_geo_location(
    *,
    customer_id: str,
    campaign_id: str,
    locations: list[str],
    country_code: str = "UA",
    locale: str = "ru",
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Гео-таргетинг кампании по стране/городу/региону (§3). НЕ деньги → user_initiated не требуем
    (как proximity/keywords). Резолв названий → geoTargetConstant + remove-before-create — в SDK-слое."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_locations(locations, country_code)  # ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "set_geo_location")  # гейт 2
    result = await run_ads_call(
        _set_geo_location_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        locations,
        country_code,
        locale,
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Прикрепление аудиторий к кампании (§3; не деньги → без user_initiated) ────────
def _validate_audience_rns(audience_resource_names: list[str]) -> None:
    """Прикрепление аудиторий — валидирует КОД ДО claim: непустой список resource_name'ов
    user_list/audience. Плохой ввод не «съедает» одноразовый черновик."""
    if not audience_resource_names:
        raise ValueError("нужна хотя бы одна аудитория")
    for rn in audience_resource_names:
        s = str(rn)
        if "/userLists/" not in s and "/audiences/" not in s:
            raise ValueError(f"некорректный resource_name аудитории: {rn}")


async def apply_attach_audience(
    *,
    customer_id: str,
    campaign_id: str,
    audience_resource_names: list[str],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Прикрепить существующие аудитории (user_list/audience) к кампании (§3). НЕ деньги →
    user_initiated не требуем (как гео/ключи). resource_name'ы берутся из ads.read.list_audiences."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_audience_rns(audience_resource_names)  # ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "attach_audience")  # гейт 2
    result = await run_ads_call(
        _attach_audience_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        list(audience_resource_names),
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _attach_audience_via_sdk(client, customer_id, campaign_id, audience_resource_names) -> dict:
    """Прикрепить аудитории к кампании (campaign_criterion). user_list-ресурс → criterion.user_list,
    audience-ресурс → criterion.audience (тип определяем по сегменту resource_name). Один атомарный
    mutate из create-операций (как add_keywords). Снятие аудиторий — apply_detach_audience."""
    cmp_svc = client.get_service("CampaignService")
    svc = client.get_service("CampaignCriterionService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))
    ops = []
    for rn in audience_resource_names:
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = campaign_rn
        if "/userLists/" in str(rn):
            op.create.user_list.user_list = rn
        else:
            op.create.audience.audience = rn
        ops.append(op)
    resp = svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=ops)
    return {
        "customer_id": str(customer_id),
        "campaign_id": str(campaign_id),
        "attached": [r.resource_name for r in resp.results],
        "count": len(resp.results),
        "applied": True,
    }


# ── Открепление аудиторий от кампании (симметрично attach) ───────────────────────
# НЕ деньги → user_initiated не требуется (как attach). Оба гейта обязательны.
async def apply_detach_audience(
    *,
    customer_id: str,
    campaign_id: str,
    audience_resource_names: list[str],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Открепить ранее прикреплённые аудитории (user_list/audience) от кампании (§3). Обратная к
    apply_attach_audience. resource_name'ы — те же, что у attach (из ads.read.list_audiences)."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_audience_rns(audience_resource_names)  # ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "detach_audience")  # гейт 2
    result = await run_ads_call(
        _detach_audience_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        list(audience_resource_names),
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _detach_audience_via_sdk(client, customer_id, campaign_id, audience_resource_names) -> dict:
    """Снять аудитории с кампании: campaign_criterion удаляется ПО resource_name (не по аудитории),
    поэтому сначала GAQL-резолв прикреплённых USER_LIST/AUDIENCE-критериев кампании → сопоставляем с
    запрошенными resource_name аудиторий, затем remove-операции. Идемпотентно: повтор снимет лишь
    оставшиеся; чего не было прикреплено — вернём в not_found (без «тихого» молчания)."""
    cmp_svc = client.get_service("CampaignService")
    svc = client.get_service("CampaignCriterionService")
    ga = client.get_service("GoogleAdsService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))
    wanted = {str(rn) for rn in audience_resource_names}
    to_remove, found = [], set()
    for row in ga.search(
        customer_id=str(customer_id),
        query=(
            "SELECT campaign_criterion.resource_name, campaign_criterion.user_list.user_list, "
            "campaign_criterion.audience.audience FROM campaign_criterion "
            f"WHERE campaign_criterion.campaign = '{gaql_escape(campaign_rn)}' "
            "AND campaign_criterion.type IN ('USER_LIST', 'AUDIENCE')"
        ),
    ):
        aud = row.campaign_criterion.audience.audience or row.campaign_criterion.user_list.user_list
        if str(aud) in wanted:
            to_remove.append(row.campaign_criterion.resource_name)
            found.add(str(aud))
    detached = []
    if to_remove:
        ops = []
        for rn in to_remove:
            op = client.get_type("CampaignCriterionOperation")
            op.remove = rn
            ops.append(op)
        resp = svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=ops)
        detached = [r.resource_name for r in resp.results]
    return {
        "customer_id": str(customer_id),
        "campaign_id": str(campaign_id),
        "detached": detached,
        "count": len(detached),
        "not_found": sorted(rn for rn in wanted if rn not in found),
        "applied": True,
    }


# ── §3-assets: текстовые расширения (sitelinks/callouts/structured snippets) ─────────
# Валидация состава/длины В КОДЕ ДО claim (зеркалит agent.tools.schemas — два независимых гейта).
def _validate_sitelinks(sitelinks: list[dict]) -> None:
    if not 1 <= len(sitelinks) <= 20:
        raise ValueError("sitelinks: 1–20 ссылок")
    for s in sitelinks:
        assert_asset_len(s.get("link_text", ""), "sitelink_text")
        url = str(s.get("final_url", ""))
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"sitelink final_url должен быть http/https: {url}")
        if s.get("description1"):
            assert_asset_len(s["description1"], "sitelink_desc")
        if s.get("description2"):
            assert_asset_len(s["description2"], "sitelink_desc")
            if not s.get("description1"):
                raise ValueError("description2 нельзя без description1")


def _validate_callouts(callouts: list[str]) -> None:
    if not 1 <= len(callouts) <= 20:
        raise ValueError("callouts: 1–20 фраз")
    for t in callouts:
        assert_asset_len(t, "callout")


def _validate_snippets(header: str, values: list[str]) -> None:
    if header not in STRUCTURED_SNIPPET_HEADERS:
        raise ValueError(f"header не из канонического списка Google: {header}")
    if not 3 <= len(values) <= 10:
        raise ValueError("structured snippet: 3–10 значений")
    for t in values:
        assert_asset_len(t, "snippet_value")


def _validate_link_rns(link_resource_names: list[str]) -> None:
    if not link_resource_names:
        raise ValueError("не указаны связи для открепления")
    for rn in link_resource_names:
        if "/campaignAssets/" not in str(rn):
            raise ValueError(f"ожидался campaign_asset resource_name: {rn}")


async def apply_add_sitelinks(
    *,
    customer_id: str,
    campaign_id: str,
    sitelinks: list[dict],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Добавить sitelinks в кампанию (create-asset → campaign_asset SITELINK). НЕ деньги →
    user_initiated не требуем (как attach_audience/keywords). Цепочка create+link НЕ идемпотентна
    → asyncio.to_thread (защита от дублей — claim one-shot, не ретрай)."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_sitelinks(sitelinks)  # ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "add_sitelinks")  # гейт 2
    result = await run_ads_create_call(
        extensions._add_sitelinks_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        list(sitelinks),
        label="add_sitelinks",
        account=customer_id,
        op_count=2 * max(1, len(sitelinks)),  # N ассетов + N линков к кампании
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_add_callouts(
    *,
    customer_id: str,
    campaign_id: str,
    callouts: list[str],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Добавить callouts (уточнения) в кампанию. НЕ деньги. create+link через to_thread."""
    ensure_allowed(customer_id)
    _validate_callouts(callouts)
    await _require_confirmation(confirm_store, confirmation_id, "add_callouts")
    result = await run_ads_create_call(
        extensions._add_callouts_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        list(callouts),
        label="add_callouts",
        account=customer_id,
        op_count=2 * max(1, len(callouts)),  # N ассетов + N линков
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_add_structured_snippets(
    *,
    customer_id: str,
    campaign_id: str,
    header: str,
    values: list[str],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Добавить структурное описание (header + values) в кампанию. НЕ деньги. create+link через to_thread."""
    ensure_allowed(customer_id)
    _validate_snippets(header, values)
    await _require_confirmation(confirm_store, confirmation_id, "add_structured_snippets")
    result = await run_ads_create_call(
        extensions._add_structured_snippets_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        header,
        list(values),
        label="add_structured_snippets",
        account=customer_id,
        op_count=2,  # ассет + линк
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_attach_image_asset(
    *,
    customer_id: str,
    campaign_id: str,
    image_bytes: bytes,
    name: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Прикрепить изображение-ассет к кампании (create image asset → campaign_asset MARKETING_IMAGE).
    НЕ деньги. Бинарь приходит из временного хранилища (service грузит по media_id). create+link
    через to_thread (НЕ идемпотентна — защита от дублей claim one-shot)."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    if not image_bytes:
        raise ValueError("пустое изображение")
    if not (name or "").strip():
        raise ValueError("пустое имя ассета")
    await _require_confirmation(confirm_store, confirmation_id, "attach_image_asset")  # гейт 2
    result = await run_ads_create_call(
        extensions._attach_image_asset_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        image_bytes,
        name,
        label="attach_image_asset",
        account=customer_id,
        op_count=2,  # ассет + линк
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── §3-assets семейство 3: Call / Promotion / Price (валидация В КОДЕ ДО claim) ──────
def _validate_call(phone_number: str, country_code: str) -> None:
    import re as _re

    if not _re.fullmatch(r"[+()\-\s\d]{3,30}", (phone_number or "").strip()):
        raise ValueError("телефон: цифры/+/()/-/пробел, 3–30 символов")
    cc = (country_code or "").strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        raise ValueError("country_code — двухбуквенный ISO-код")


def _validate_promotion(
    promotion_target: str,
    final_url: str,
    percent_off: float | None,
    money_off_units: float | None,
    currency: str | None,
) -> None:
    assert_asset_len(promotion_target, "promotion_target")
    if not str(final_url).startswith(("http://", "https://")):
        raise ValueError("promotion final_url должен быть http/https")
    has_pct = percent_off is not None
    has_money = money_off_units is not None
    if has_pct == has_money:
        raise ValueError("ровно одна скидка: percent_off ИЛИ money_off_units")
    if has_pct and not (0 < float(percent_off) <= 100):
        raise ValueError("percent_off — в (0, 100]")
    if has_money:
        if not (0 < float(money_off_units) <= MONEY_MAX_UNITS):
            raise ValueError(f"money_off_units — в (0, {MONEY_MAX_UNITS}]")
        if not currency:
            raise ValueError("для money_off_units нужна currency")


def _validate_price(offerings: list[dict], currency: str) -> None:
    if not 3 <= len(offerings) <= 8:
        raise ValueError("прайс: 3–8 оферов")
    if not currency:
        raise ValueError("нужна currency для прайса")
    for o in offerings:
        assert_asset_len(o.get("header", ""), "price_header")
        assert_asset_len(o.get("description", ""), "price_desc")
        if not (0 < float(o.get("price_units", 0)) <= MONEY_MAX_UNITS):
            raise ValueError("price_units — в (0, лимит]")
        if not str(o.get("final_url", "")).startswith(("http://", "https://")):
            raise ValueError("у каждого price-офера final_url http/https")


async def apply_add_call_asset(
    *,
    customer_id: str,
    campaign_id: str,
    phone_number: str,
    country_code: str,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Телефон-расширение (CallAsset) в кампанию. НЕ деньги (не управляет ставкой/бюджетом)."""
    ensure_allowed(customer_id)
    _validate_call(phone_number, country_code)
    await _require_confirmation(confirm_store, confirmation_id, "add_call_asset")
    result = await run_ads_create_call(
        extensions._add_call_asset_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        phone_number,
        country_code,
        label="add_call_asset",
        account=customer_id,
        op_count=2,  # ассет + линк
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_add_promotion(
    *,
    customer_id: str,
    campaign_id: str,
    promotion_target: str,
    final_url: str,
    percent_off: float | None = None,
    money_off_units: float | None = None,
    currency: str | None = None,
    promo_code: str | None = None,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Промо-расширение (PromotionAsset). percent_off в шкале 1_000_000=100% (×10_000) — в SDK-слое."""
    ensure_allowed(customer_id)
    _validate_promotion(promotion_target, final_url, percent_off, money_off_units, currency)
    await _require_confirmation(confirm_store, confirmation_id, "add_promotion")
    result = await run_ads_create_call(
        lambda: extensions._add_promotion_via_sdk(
            ads_client,
            customer_id,
            campaign_id,
            promotion_target=promotion_target,
            final_url=final_url,
            percent_off=percent_off,
            money_off_units=money_off_units,
            currency=currency,
            promo_code=promo_code,
        ),
        label="add_promotion",
        account=customer_id,
        op_count=2,  # ассет + линк
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_add_price_asset(
    *,
    customer_id: str,
    campaign_id: str,
    price_type: str,
    currency: str,
    language_code: str,
    offerings: list[dict],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Прайс-расширение (PriceAsset, 3–8 оферов). Money: 1_000_000 micros = 1 единица — в SDK-слое."""
    ensure_allowed(customer_id)
    _validate_price(offerings, currency)
    await _require_confirmation(confirm_store, confirmation_id, "add_price_asset")
    result = await run_ads_create_call(
        lambda: extensions._add_price_asset_via_sdk(
            ads_client,
            customer_id,
            campaign_id,
            price_type=price_type,
            currency=currency,
            language_code=language_code,
            offerings=list(offerings),
        ),
        label="add_price_asset",
        account=customer_id,
        op_count=2,  # ассет + линк
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


async def apply_remove_asset_link(
    *,
    customer_id: str,
    link_resource_names: list[str],
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Открепить ассет(ы) от кампании (удаляется СВЯЗЬ campaign_asset, не сам ассет). НЕ деньги."""
    ensure_allowed(customer_id)
    _validate_link_rns(link_resource_names)
    await _require_confirmation(confirm_store, confirmation_id, "remove_asset_link")
    result = await run_ads_create_call(
        extensions._remove_campaign_assets_via_sdk,
        ads_client,
        customer_id,
        list(link_resource_names),
        label="remove_asset_link",
        account=customer_id,
        op_count=max(1, len(link_resource_names)),  # по операции на связь
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


# ── Смена стратегии назначения ставок кампании (§3; ДЕНЬГИ → user_initiated) ──────
_BIDDING_STRATEGIES = frozenset(
    {"manual_cpc", "maximize_conversions", "maximize_conversion_value", "target_spend"}
)


async def apply_set_bidding_strategy(
    *,
    customer_id: str,
    campaign_id: str,
    strategy: str,
    target_cpa: float | None = None,
    target_roas: float | None = None,
    enhanced_cpc: bool = False,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Смена стратегии ставок кампании (§3). ДЕНЬГИ (управляет расходом) → как бюджет/ставка,
    требуем user_initiated. Диапазоны target_cpa (валюта аккаунта) / target_roas (доля) — КОД ДО claim."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    if strategy not in _BIDDING_STRATEGIES:
        raise ValueError(f"неизвестная стратегия ставок: {strategy}")
    target_cpa_micros = None
    if target_cpa is not None:
        # target_cpa — деньги (в единицах валюты аккаунта) → тот же «очевидно неверно» потолок,
        # что у бюджета/ставки (единый источник core.limits, не литерал 1_000_000).
        if target_cpa <= 0 or target_cpa > MONEY_MAX_UNITS:
            raise ValueError(f"target_cpa вне допустимого диапазона (0, {MONEY_MAX_UNITS}]")
        target_cpa_micros = int(round(float(target_cpa) * 1_000_000))
    if target_roas is not None and (target_roas <= 0 or target_roas > 1000):
        raise ValueError("target_roas — доля в (0, 1000] (напр. 4.0 = 400%)")

    proposal = await _require_confirmation(confirm_store, confirmation_id, "set_bidding_strategy")
    if not proposal.user_initiated:  # деньги: только прямая команда пользователя
        raise PermissionError("смена стратегии ставок должна быть прямой командой пользователя")

    result = await run_ads_call(
        _set_bidding_strategy_via_sdk,
        ads_client,
        customer_id,
        campaign_id,
        strategy,
        target_cpa_micros,
        target_roas,
        bool(enhanced_cpc),
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
            # §19.5.1 (B12): сегмент display path без пробелов/слэшей — иначе SDK отклонит RSA.
            if re.search(r"[\s/\\]", p):
                raise ValueError(
                    f"{label} содержит пробел или слэш (недопустимо в display path): «{p}»"
                )


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


def _update_campaign_name_via_sdk(
    client, customer_id: str, campaign_id: str, new_name: str
) -> dict:
    """Переименование кампании через CampaignService (зеркало _set_campaign_status_via_sdk, но
    меняется поле name). update_mask по изменённым полям op.update. Имя кампании в Google Ads
    уникально в аккаунте → перехватываем DUPLICATE_CAMPAIGN_NAME понятным сообщением."""
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.update.resource_name = svc.campaign_path(str(customer_id), str(campaign_id))
    op.update.name = new_name
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
    try:
        svc.mutate_campaigns(customer_id=str(customer_id), operations=[op])
    except GoogleAdsException as ex:
        if "DUPLICATE_CAMPAIGN_NAME" in error_code_names(ex):
            raise ValueError(
                f"кампания с именем «{new_name}» уже существует в аккаунте — выбери другое имя"
            ) from ex
        raise
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "new_name": new_name,
        "applied": True,
    }


def _set_ad_group_status_via_sdk(client, customer_id: str, ad_group_id: str, status) -> dict:
    """Статус ad_group через AdGroupService (зеркало _set_campaign_status_via_sdk). update_mask —
    только status (field_mask по изменённым полям op.update)."""
    svc = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    op.update.resource_name = svc.ad_group_path(str(customer_id), str(ad_group_id))
    op.update.status = status
    client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
    svc.mutate_ad_groups(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "status": getattr(status, "name", str(status)),
        "applied": True,
    }


def _launch_campaign_via_sdk(client, customer_id: str, campaign_id: str) -> dict:
    """§19.8/§11: включить ВСЮ структуру кампании (кампания + ВСЕ не-REMOVED группы + ВСЕ не-REMOVED
    объявления) в ENABLED. Объявление показывается только когда ENABLED на ВСЕХ трёх уровнях —
    поэтому запуск созданного визардом PAUSED-черновика обязан включить и группу, и RSA, иначе 0
    показов. REMOVED-сущности НЕ воскрешаем (их фильтруем в GAQL). Идемпотентно (ENABLED→ENABLED)."""
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    # Собираем resource_name групп и объявлений кампании (кроме REMOVED — их не оживляем).
    ag_rns = [
        row.ad_group.resource_name
        for row in ga.search(
            customer_id=cid,
            query=(
                "SELECT ad_group.resource_name FROM ad_group "
                f"WHERE campaign.id = {int(campaign_id)} AND ad_group.status != 'REMOVED'"
            ),
        )
    ]
    ad_rns = [
        row.ad_group_ad.resource_name
        for row in ga.search(
            customer_id=cid,
            query=(
                "SELECT ad_group_ad.resource_name FROM ad_group_ad "
                f"WHERE campaign.id = {int(campaign_id)} AND ad_group_ad.status != 'REMOVED'"
            ),
        )
    ]
    # 1) кампания → ENABLED (переиспользуем существующий сеттер: единый update_mask-идиом).
    _set_campaign_status_via_sdk(client, cid, campaign_id, client.enums.CampaignStatusEnum.ENABLED)
    # 2) группы → ENABLED (батч одним mutate).
    if ag_rns:
        ag_svc = client.get_service("AdGroupService")
        ops = []
        for rn in ag_rns:
            op = client.get_type("AdGroupOperation")
            op.update.resource_name = rn
            op.update.status = client.enums.AdGroupStatusEnum.ENABLED
            client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
            ops.append(op)
        ag_svc.mutate_ad_groups(customer_id=cid, operations=ops)
    # 3) объявления → ENABLED (батч одним mutate).
    if ad_rns:
        aga_svc = client.get_service("AdGroupAdService")
        ops = []
        for rn in ad_rns:
            op = client.get_type("AdGroupAdOperation")
            op.update.resource_name = rn
            op.update.status = client.enums.AdGroupAdStatusEnum.ENABLED
            client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
            ops.append(op)
        aga_svc.mutate_ad_group_ads(customer_id=cid, operations=ops)
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "status": "ENABLED",
        "ad_groups_enabled": len(ag_rns),
        "ads_enabled": len(ad_rns),
        "applied": True,
    }


def _remove_campaign_via_sdk(client, customer_id: str, campaign_id: str) -> dict:
    """Удаление кампании через CampaignService (op.remove = resource_name). Необратимо: статус
    кампании становится REMOVED. Замок/гейт — в apply_remove_campaign выше."""
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.remove = svc.campaign_path(str(customer_id), str(campaign_id))
    svc.mutate_campaigns(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "removed": True,
        "applied": True,
    }


def _remove_ad_group_via_sdk(client, customer_id: str, ad_group_id: str) -> dict:
    """Удаление группы объявлений через AdGroupService (op.remove). Необратимо (status→REMOVED)."""
    svc = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    op.remove = svc.ad_group_path(str(customer_id), str(ad_group_id))
    svc.mutate_ad_groups(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": customer_id,
        "ad_group_id": str(ad_group_id),
        "removed": True,
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
                f"WHERE bidding_strategy.resource_name = '{gaql_escape(portfolio_rn)}'"
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
        op.update.cpc_bid_micros = _round_micros(
            micros
        )  # кратно биллинг-единице (иначе API reject)
        client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, op.update._pb))
        ops.append(op)
    try:
        svc.mutate_ad_groups(customer_id=str(customer_id), operations=ops)
    except GoogleAdsException as ex:
        # Сравниваем по ИМЕНИ enum-значения: AdGroupErrorEnum — это тип ОШИБКИ (v24.errors),
        # его НЕТ в client.enums (только enums-модуль) → обращение к client.enums.AdGroupErrorEnum
        # упало бы AttributeError и проглотило исходную ошибку. .name есть у любого enum-поля
        # (для не-ad_group ошибок вернётся 'UNSPECIFIED'), сравнение версионно-независимо.
        # error_code_names — единый источник имён кодов (WhichOneof находит активный oneof,
        # корректно и для не-ad_group ошибок); раньше тут был свой инлайн-разбор.
        if "BID_TYPE_AND_BIDDING_STRATEGY_MISMATCH" in error_code_names(ex):
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


def _rejected_from_partial_failure(
    client, resp, meta: list[tuple[str, str]], *, key: str
) -> list[dict]:
    """Атрибуция partial_failure-ошибок к операциям батча: op_index → meta[i]=(ad_group_id, text).
    reason редактируется (golden rule #5: серверный message может нести чувствительное)."""
    rejected: list[dict] = []
    for idx, code, msg in partial_failure_errors(client, resp):
        ag, text = meta[idx] if 0 <= idx < len(meta) else ("", "")
        reason = redact_text((f"{msg} [{code}]" if code else msg).strip() or "отклонено сервером")
        row: dict = {key: text, "reason": reason}
        if ag:
            row["ad_group_id"] = ag
        rejected.append(row)
    return rejected


def _add_keywords_via_sdk(
    client, customer_id: str, ad_group_ids: list, keywords: list, match_type: str
) -> dict:
    """Создаёт позитивные ключевые слова (ad_group_criterion) во ВСЕХ группах кампании.
    CREATE — БЕЗ update_mask. Метод mutate_ad_group_criteria (мн.ч.), тип Operation (ед.ч.).

    partial_failure=True: одна плохая позиция (policy/серверный дубль) НЕ валит весь батч —
    валидные ключи применяются, отклонённые возвращаются в result['rejected'] с причиной.
    Все отклонены ⇒ ValueError (честный failed — не применено ничего). Скоуп partial_failure
    осознанно узкий: только пользовательские батчи ключей/минус-слов; remove_* (резолвят
    существующие) и ассет-батчи остаются whole-batch (fail loud)."""
    ag_svc = client.get_service("AdGroupService")
    svc = client.get_service("AdGroupCriterionService")
    mt = getattr(client.enums.KeywordMatchTypeEnum, str(match_type).upper())
    enabled = client.enums.AdGroupCriterionStatusEnum.ENABLED
    ops = []
    meta: list[tuple[str, str]] = []  # meta[i] = (ad_group_id, text) операции i — атрибуция отказов
    for ad_group_id in ad_group_ids:
        ag_rn = ag_svc.ad_group_path(str(customer_id), str(ad_group_id))
        for text in keywords:
            op = client.get_type("AdGroupCriterionOperation")
            op.create.ad_group = ag_rn
            op.create.status = enabled
            op.create.keyword.text = text
            op.create.keyword.match_type = mt
            ops.append(op)
            meta.append((str(ad_group_id), str(text)))
    req = client.get_type("MutateAdGroupCriteriaRequest")
    req.customer_id = str(customer_id)
    req.operations.extend(ops)
    req.partial_failure = True
    resp = svc.mutate_ad_group_criteria(request=req)
    # При partial_failure сервер отдаёт results с ПУСТЫМ resource_name на отклонённых позициях.
    created = [r.resource_name for r in resp.results if getattr(r, "resource_name", "")]
    rejected = _rejected_from_partial_failure(client, resp, meta, key="keyword")
    if rejected and not created:
        reasons = "; ".join(r["reason"] for r in rejected[:3])
        raise ValueError(f"Google Ads отклонил все ключи ({len(rejected)}): {reasons}")
    result = {
        "customer_id": customer_id,
        "ad_group_ids": [str(a) for a in ad_group_ids],
        "match_type": str(match_type),
        "created": created,
        "count": len(created),
        "applied": True,
    }
    if rejected:
        result["rejected"] = rejected
        result["rejected_count"] = len(rejected)
    return result


# ── Удаление ключевых слов (симметрично add: по тексту+типу из групп кампании) ────
async def apply_remove_keywords(
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
    if not ad_group_ids:
        raise ValueError("нет групп объявлений для удаления ключевых слов")
    clean = normalize_keywords(keywords)  # длину/форму/дубли считает КОД — ДО claim
    await _require_confirmation(confirm_store, confirmation_id, "remove_keywords")
    result = await run_ads_call(
        _remove_keywords_via_sdk,
        ads_client,
        customer_id,
        ad_group_ids,
        clean,
        match_type,
        op_count=len(clean),  # квота §3: оценка сверху (резолв может найти меньше)
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _remove_keywords_via_sdk(
    client, customer_id: str, ad_group_ids: list, keywords: list, match_type: str
) -> dict:
    """Удалить criterion по тексту+типу: criterion удаляется ПО resource_name (не по тексту), поэтому
    сначала GAQL-резолв text→resource_name в нужных группах, затем remove-операции. Тексты фильтруем
    в Python (по casefold) — без интерполяции в GAQL. Идемпотентно: повтор найдёт лишь оставшиеся."""
    ga = client.get_service("GoogleAdsService")
    svc = client.get_service("AdGroupCriterionService")
    mt = str(match_type).upper()  # из Literal broad/phrase/exact → BROAD/PHRASE/EXACT (безопасно)
    wanted = {str(k).casefold() for k in keywords}
    ag_in = ", ".join(str(int(a)) for a in ad_group_ids)
    q = (
        "SELECT ad_group_criterion.resource_name, ad_group_criterion.keyword.text "
        "FROM ad_group_criterion WHERE ad_group_criterion.type = KEYWORD "
        f"AND ad_group_criterion.keyword.match_type = '{mt}' AND ad_group.id IN ({ag_in})"
    )
    to_remove, found = [], set()
    for row in ga.search(customer_id=str(customer_id), query=q):
        text = row.ad_group_criterion.keyword.text
        if text.casefold() in wanted:
            to_remove.append(row.ad_group_criterion.resource_name)
            found.add(text.casefold())
    removed = []
    if to_remove:
        ops = []
        for rn in to_remove:
            op = client.get_type("AdGroupCriterionOperation")
            op.remove = rn
            ops.append(op)
        resp = svc.mutate_ad_group_criteria(customer_id=str(customer_id), operations=ops)
        removed = [r.resource_name for r in resp.results]
    return {
        "customer_id": str(customer_id),
        "match_type": mt,
        "removed": removed,
        "count": len(removed),
        "not_found": sorted(
            k for k in wanted if k not in found
        ),  # чего не было — явно, без молчания
        "applied": True,
    }


def _add_negative_keywords_via_sdk(
    client, customer_id: str, campaign_id: str, keywords: list, match_type: str
) -> dict:
    """Минус-слова НА УРОВНЕ КАМПАНИИ (campaign_criterion, negative=True — обязателен, immutable).
    CREATE — БЕЗ update_mask. Это НЕ shared negative list (другой флоу через SharedSetService).

    partial_failure=True (зеркало _add_keywords_via_sdk): плохая позиция не валит батч —
    отклонённые в result['rejected'], все отклонены ⇒ ValueError (честный failed)."""
    svc = client.get_service("CampaignCriterionService")
    cmp_svc = client.get_service("CampaignService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))
    mt = getattr(client.enums.KeywordMatchTypeEnum, str(match_type).upper())
    ops = []
    meta: list[tuple[str, str]] = []  # ("", text): уровень кампании — без ad_group_id
    for text in keywords:
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = campaign_rn
        op.create.negative = True  # ОБЯЗАТЕЛЬНО: это исключение (минус-слово)
        op.create.keyword.text = text
        op.create.keyword.match_type = mt
        ops.append(op)
        meta.append(("", str(text)))
    req = client.get_type("MutateCampaignCriteriaRequest")
    req.customer_id = str(customer_id)
    req.operations.extend(ops)
    req.partial_failure = True
    resp = svc.mutate_campaign_criteria(request=req)
    created = [r.resource_name for r in resp.results if getattr(r, "resource_name", "")]
    rejected = _rejected_from_partial_failure(client, resp, meta, key="keyword")
    if rejected and not created:
        reasons = "; ".join(r["reason"] for r in rejected[:3])
        raise ValueError(f"Google Ads отклонил все минус-слова ({len(rejected)}): {reasons}")
    result = {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "match_type": str(match_type),
        "resource_names": created,
        "count": len(created),
        "applied": True,
    }
    if rejected:
        result["rejected"] = rejected
        result["rejected_count"] = len(rejected)
    return result


def _remove_negative_keywords_via_sdk(
    client, customer_id: str, campaign_id: str, keywords: list, match_type: str
) -> dict:
    """Снять минус-слова кампании по тексту+типу (симметрично _remove_keywords_via_sdk, но на
    campaign_criterion с negative=TRUE). criterion удаляется ПО resource_name → сначала GAQL-резолв
    text→resource_name, тексты фильтруем в Python (casefold, без интерполяции в GAQL). Идемпотентно."""
    cmp_svc = client.get_service("CampaignService")
    svc = client.get_service("CampaignCriterionService")
    ga = client.get_service("GoogleAdsService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))
    mt = str(match_type).upper()  # broad/phrase/exact → BROAD/PHRASE/EXACT
    wanted = {str(k).casefold() for k in keywords}
    to_remove, found = [], set()
    for row in ga.search(
        customer_id=str(customer_id),
        query=(
            "SELECT campaign_criterion.resource_name, campaign_criterion.keyword.text "
            "FROM campaign_criterion "
            f"WHERE campaign_criterion.campaign = '{gaql_escape(campaign_rn)}' "
            # campaign-level KEYWORD criteria — это всегда минус-слова (позитивные живут на ad_group);
            # negative = true оставляем как явный фильтр (Google-канон, lowercase-литерал GAQL).
            "AND campaign_criterion.type = 'KEYWORD' AND campaign_criterion.negative = true "
            f"AND campaign_criterion.keyword.match_type = '{mt}'"
        ),
    ):
        text = row.campaign_criterion.keyword.text
        if text.casefold() in wanted:
            to_remove.append(row.campaign_criterion.resource_name)
            found.add(text.casefold())
    removed = []
    if to_remove:
        ops = []
        for rn in to_remove:
            op = client.get_type("CampaignCriterionOperation")
            op.remove = rn
            ops.append(op)
        resp = svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=ops)
        removed = [r.resource_name for r in resp.results]
    return {
        "customer_id": str(customer_id),
        "campaign_id": str(campaign_id),
        "match_type": mt,
        "removed": removed,
        "count": len(removed),
        "not_found": sorted(k for k in wanted if k not in found),
        "applied": True,
    }


def _set_geo_proximity_via_sdk(
    client, customer_id: str, campaign_id: str, radius_km: float, address: dict
) -> dict:
    """A-geo: радиус-таргетинг (campaign_criterion.proximity). Address-driven — Google сам
    вычисляет точку, геокодинг на нашей стороне НЕ нужен. proximity IMMUTABLE (сменить = remove+create).
    address — структурные поля (минимум city_name + country_code); free-form строка сюда не годится.

    REMOVE-BEFORE-CREATE: «сменить гео» = удалить существующие proximity-критерии кампании + создать
    новый, одним атомарным mutate. Иначе повторный вызов СТЕКАЛ бы несколько радиусов (OR) на кампании
    вместо замены — не то, что ожидает пользователь от команды «измени ГЕО»."""
    cmp_svc = client.get_service("CampaignService")
    svc = client.get_service("CampaignCriterionService")
    ga = client.get_service("GoogleAdsService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))

    ops = []
    # Существующие proximity-критерии кампании → remove (immutable, заменяем целиком).
    for row in ga.search(
        customer_id=str(customer_id),
        query=(
            "SELECT campaign_criterion.resource_name FROM campaign_criterion "
            f"WHERE campaign_criterion.campaign = '{gaql_escape(campaign_rn)}' "
            "AND campaign_criterion.type = 'PROXIMITY'"
        ),
    ):
        rm = client.get_type("CampaignCriterionOperation")
        rm.remove = row.campaign_criterion.resource_name
        ops.append(rm)
    removed = len(ops)

    op = client.get_type("CampaignCriterionOperation")
    crit = op.create
    crit.campaign = campaign_rn
    prox = crit.proximity
    prox.radius = float(radius_km)
    prox.radius_units = client.enums.ProximityRadiusUnitsEnum.KILOMETERS  # код решает, не модель
    for field in ("street_address", "city_name", "province_code", "postal_code", "country_code"):
        val = address.get(field)
        if val:
            setattr(prox.address, field, val)
    ops.append(op)  # create — последней, чтобы resp.results[-1] был новым критерием

    resp = svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=ops)
    return {
        "customer_id": customer_id,
        "campaign_id": str(campaign_id),
        "radius_km": float(radius_km),
        "removed_proximity": removed,  # сколько старых proximity заменили
        "resource_name": resp.results[-1].resource_name if resp.results else None,
        "applied": True,
    }


def _set_geo_location_via_sdk(
    client, customer_id: str, campaign_id: str, locations: list, country_code: str, locale: str
) -> dict:
    """Гео по стране/городу: резолв НАЗВАНИЙ → geoTargetConstant (SuggestGeoTargetConstants), затем
    REMOVE-BEFORE-CREATE location-критериев кампании одним атомарным mutate (как proximity). Берём
    топ-подсказку на каждый search_term (один geoTargetConstant на распознанное название) — без
    over-таргетинга. Если ни одна локация не распознана — НЕ трогаем кампанию (raise), чтобы не
    стереть гео в пустоту."""
    cmp_svc = client.get_service("CampaignService")
    svc = client.get_service("CampaignCriterionService")
    ga = client.get_service("GoogleAdsService")
    gtc = client.get_service("GeoTargetConstantService")
    campaign_rn = cmp_svc.campaign_path(str(customer_id), str(campaign_id))

    # 1) Резолв названий → geoTargetConstant (топ-подсказка на каждый search_term).
    req = client.get_type("SuggestGeoTargetConstantsRequest")
    req.locale = locale or "ru"
    if country_code:
        req.country_code = country_code
    req.location_names.names.extend([str(s).strip() for s in locations])
    resp = gtc.suggest_geo_target_constants(request=req)
    best: dict[str, str] = {}  # search_term → geo_target_constant.resource_name (первая = лучшая)
    names: dict[str, str] = {}
    for s in resp.geo_target_constant_suggestions:
        term = s.search_term
        rn = s.geo_target_constant.resource_name
        if term not in best and rn:
            best[term] = rn
            names[term] = s.geo_target_constant.name
    targets = list(best.values())
    if not targets:
        raise ValueError("не удалось распознать ни одной локации — уточни названия (страна/город)")

    ops = []
    # 2) Существующие LOCATION-критерии кампании → remove (заменяем гео целиком).
    for row in ga.search(
        customer_id=str(customer_id),
        query=(
            "SELECT campaign_criterion.resource_name FROM campaign_criterion "
            f"WHERE campaign_criterion.campaign = '{gaql_escape(campaign_rn)}' "
            "AND campaign_criterion.type = 'LOCATION'"
        ),
    ):
        rm = client.get_type("CampaignCriterionOperation")
        rm.remove = row.campaign_criterion.resource_name
        ops.append(rm)
    removed = len(ops)

    # 3) Новые location-критерии (по одному на распознанную локацию).
    for rn in targets:
        op = client.get_type("CampaignCriterionOperation")
        op.create.campaign = campaign_rn
        op.create.location.geo_target_constant = rn
        ops.append(op)

    svc.mutate_campaign_criteria(customer_id=str(customer_id), operations=ops)
    return {
        "customer_id": str(customer_id),
        "campaign_id": str(campaign_id),
        "locations": list(names.values()),
        "removed_location": removed,  # сколько старых location-критериев заменили
        "count": len(targets),
        "applied": True,
    }


def _set_bidding_strategy_via_sdk(
    client, customer_id, campaign_id, strategy, target_cpa_micros, target_roas, enhanced_cpc
) -> dict:
    """Стандартная (не портфельная) стратегия ставок на кампании. Меняем oneof
    campaign_bidding_strategy + ЯВНЫЙ update_mask на имя стратегии (надёжно для переключения oneof,
    в т.ч. на пустую стратегию без таргета). Переключение с портфельной — Google очистит её сам."""
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    c = op.update
    c.resource_name = svc.campaign_path(str(customer_id), str(campaign_id))
    # mask_path — ЛИСТОВОЕ подполе выбранной стратегии (не само сообщение-стратегию). Два правила
    # Google Ads, на которых обожглись:
    #   1) update_mask живёт на ОПЕРАЦИИ (op.update_mask), а НЕ на Campaign (op.update) — иначе
    #      AttributeError "Unknown field for Campaign: update_mask".
    #   2) путь в маске должен указывать на ЛИСТ (скаляр), а не на message-поле: bare-имя стратегии
    #      (напр. "maximize_conversions") API отвергает — FieldMaskError.FIELD_HAS_SUBFIELDS.
    # При этом ПУСТУЮ стратегию (maximize_conversions без target) protobuf_helpers.field_mask НЕ
    # увидит (proto3 не маскирует set-but-empty message и default-скаляры), поэтому маску на лист
    # ставим ЯВНО: лист и переключает oneof стратегии, и задаёт/очищает target (0 = без таргета).
    if strategy == "manual_cpc":
        c.manual_cpc.enhanced_cpc_enabled = bool(enhanced_cpc)
        mask_path = "manual_cpc.enhanced_cpc_enabled"
    elif strategy == "maximize_conversions":
        if target_cpa_micros:
            c.maximize_conversions.target_cpa_micros = int(target_cpa_micros)
        else:  # пустая стратегия (без таргета) — присваиваем чистое сообщение
            client.copy_from(c.maximize_conversions, client.get_type("MaximizeConversions"))
        mask_path = "maximize_conversions.target_cpa_micros"
    elif strategy == "maximize_conversion_value":
        if target_roas:
            c.maximize_conversion_value.target_roas = float(target_roas)
        else:
            client.copy_from(
                c.maximize_conversion_value, client.get_type("MaximizeConversionValue")
            )
        mask_path = "maximize_conversion_value.target_roas"
    elif strategy == "target_spend":
        client.copy_from(c.target_spend, client.get_type("TargetSpend"))
        mask_path = "target_spend.target_spend_micros"
    else:
        raise ValueError(f"неизвестная стратегия ставок: {strategy}")
    op.update_mask.paths.append(mask_path)  # лист — надёжно для oneof-переключения (см. выше)
    svc.mutate_campaigns(customer_id=str(customer_id), operations=[op])
    return {
        "customer_id": str(customer_id),
        "campaign_id": str(campaign_id),
        "strategy": strategy,
        "target_cpa_micros": int(target_cpa_micros) if target_cpa_micros else None,
        "target_roas": float(target_roas) if target_roas else None,
        "enhanced_cpc": bool(enhanced_cpc) if strategy == "manual_cpc" else None,
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
    try:
        resp = svc.mutate_ad_group_ads(customer_id=str(customer_id), operations=[op])
    except GoogleAdsException as ex:
        # B2: если группа НЕ Search-стандартная (DSA/Display/Video/PMax), Google отвергает создание
        # RSA как «The operation is not allowed for the given context». Обычно отсекается ещё в
        # пикере (accepts_rsa), но перехватываем и на SDK-шаге → понятная причина вместо сырого API.
        codes = error_code_names(ex)
        ctx_codes = {
            "OPERATION_NOT_PERMITTED_FOR_CONTEXT",
            "OPERATION_NOT_PERMITTED_FOR_REMOVED_RESOURCE",
            "CANNOT_CREATE_AD_FOR_AD_GROUP",
        }
        if codes & ctx_codes or "not allowed for the given context" in str(ex).lower():
            raise ValueError(
                "нельзя создать адаптивное поисковое объявление в этой группе — она не "
                "Search-стандартная (DSA/Display/Video/PMax). Выбери поисковую кампанию со "
                "стандартной группой объявлений."
            ) from ex
        raise
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


# ── Создание поисковой (Search) кампании из текстов (§3): бюджет→Search→группа→RSA→ключи, PAUSED ─
def _validate_search_inputs(
    campaign_name: str,
    headlines: list[str],
    descriptions: list[str],
    final_url: str,
    budget_daily_micros: int,
    *,
    path1: str | None = None,
    path2: str | None = None,
    url_options: dict | None = None,
) -> None:
    """Полная валидация В КОДЕ — ДО claim. Длину/составы/URL/бюджет/имя считает КОД (golden rule #4)."""
    if not campaign_name or len(campaign_name) > 120:
        raise ValueError("название кампании 1–120 символов")
    _validate_rsa_inputs(
        headlines, descriptions, final_url, path1, path2
    )  # реюз RSA-валидации набора (+ display path ≤15, кириллица=1)
    if budget_daily_micros <= 0:
        raise ValueError("дневной бюджет должен быть > 0")
    if budget_daily_micros > MAX_AMOUNT_MICROS:
        raise ValueError("дневной бюджет подозрительно большой — проверь команду")
    _validate_url_options(url_options)


def _validate_url_options(url_options: dict | None) -> None:
    """§19.8 Ad URL options: tracking_url_template/final_url_suffix/custom_parameters. КОД, ДО claim."""
    if not url_options:
        return
    tpl = (url_options.get("tracking_url_template") or "").strip()
    if tpl:
        if len(tpl) > 2048:
            raise ValueError("tracking_url_template слишком длинный (≤2048)")
        # ValueTrack: {lpurl} / {escapedlpurl} / {unescapedlpurl} (последние два НЕ содержат
        # подстроку "{lpurl}" — проверяем по "lpurl}"), либо абсолютный http(s)-URL.
        low = tpl.lower()
        if "lpurl}" not in low and not low.startswith(("http://", "https://")):
            raise ValueError(
                "tracking_url_template должен содержать {lpurl}/{escapedlpurl}/{unescapedlpurl} "
                "или начинаться с http"
            )
    suffix = (url_options.get("final_url_suffix") or "").strip()
    if suffix and (suffix.startswith("?") or len(suffix) > 2048):
        raise ValueError("final_url_suffix — без ведущего '?' и ≤2048 символов")
    params = url_options.get("custom_parameters") or {}
    if not isinstance(params, dict):
        raise ValueError("custom_parameters — это словарь {ключ: значение}")
    if len(params) > 8:  # v24: до 8 пользовательских параметров на кампанию
        raise ValueError("custom_parameters — не более 8")
    for k, v in params.items():
        if not re.fullmatch(r"[A-Za-z0-9_]+", str(k)):
            raise ValueError(f"ключ custom_parameter '{k}' — только латиница/цифры/_")
        if len(str(v)) > 250:
            raise ValueError("значение custom_parameter ≤250 символов")


async def apply_create_search_campaign(
    *,
    customer_id: str,
    campaign_name: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    budget_daily_micros: int,
    keywords: list[str] | None = None,
    match_type: str = "phrase",
    keyword_match_types: list[str] | None = None,
    cpc_bid_micros: int = 500_000,
    # §19 (необязательные, обратно совместимы — без них поведение прежнее):
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
    languages: list[str] | None = None,
    bidding: dict | None = None,
    path1: str | None = None,
    path2: str | None = None,
    url_options: dict | None = None,
    asset_specs: list[dict] | None = None,
    existing_asset_links: list[dict] | None = None,
    image_specs: list[tuple[bytes, str]] | None = None,
    networks: str | None = None,
    ad_schedule_blocks: list[dict] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Создать поисковую кампанию из текстов за двойным гейтом. Все сущности — PAUSED (0 расхода).
    Создание — ТОЛЬКО прямой командой пользователя (как бюджет/GDN). Через run_ads_create_call
    (квота §3 + таймаут + семафор), но БЕЗ ретраев: цепочка из создающих вызовов НЕ идемпотентна
    (авто-ретрай породил бы дубли) — от повтора защищает claim.

    §19 (composite): бюджет→кампания(SEARCH,PAUSED,стратегия+URL-опции)→группа→RSA(+display path)→
    ключи→гео→язык — в синхронной цепочке (откат осиротевшего бюджета при сбое кампании). Ассеты и
    изображения добавляются ПОСЛЕ (PAUSED, $0 безопасно): каждый в своём try/except — сбой одного
    ассета НЕ откатывает кампанию (отчёт per-step)."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_search_inputs(
        campaign_name,
        headlines,
        descriptions,
        final_url,
        budget_daily_micros,
        path1=path1,
        path2=path2,
        url_options=url_options,
    )
    # §19.4.1: per-keyword типы (смешанный список). Дедуп ПАРАМИ (текст первым-выигрывает вместе со
    # своим типом) — обычный normalize_keywords дедупит только тексты и порвал бы склейку 1:1.
    clean_kw: list[str] = []
    clean_mts: list[str] | None = None
    if keywords:
        if keyword_match_types:
            if len(keyword_match_types) != len(keywords):
                raise ValueError(
                    f"keyword_match_types ({len(keyword_match_types)}) не совпадает по длине с "
                    f"keywords ({len(keywords)})"
                )
            # B11: дедуп по ПАРЕ (текст, тип). Google Ads допускает один текст с разными типами
            # соответствия в одной группе — дедуп только по тексту молча терял бы второй тип.
            seen: set[tuple[str, str]] = set()
            clean_mts = []
            for k, kmt in zip(keywords, keyword_match_types):
                t = assert_keyword_ok(k)
                pair = (t, str(kmt))
                if pair not in seen:
                    seen.add(pair)
                    clean_kw.append(t)
                    clean_mts.append(str(kmt))
            if not clean_kw:
                raise ValueError("список ключевых слов пуст после нормализации")
        else:
            clean_kw = normalize_keywords(keywords)
    proposal = await _require_confirmation(confirm_store, confirmation_id, "create_search_campaign")
    if not proposal.user_initiated:
        raise PermissionError("создание кампании должно быть прямой командой пользователя")
    # Честный op_count composite-цепочки (§3): бюджет+кампания+группа+RSA + ключи + гео + языки
    # + блоки расписания (+страна, если задана кодом). Google тарифицирует КАЖДУЮ операцию.
    _search_ops = (
        4
        + len(clean_kw)
        + len(geo_locations or [])
        + (1 if geo_country_code else 0)
        + len(languages or [])
        + len(ad_schedule_blocks or [])
    )
    result = await run_ads_create_call(
        _create_search_campaign_via_sdk,
        ads_client,
        customer_id,
        label="create_search_campaign",
        account=customer_id,
        op_count=_search_ops,
        campaign_name=campaign_name,
        final_url=final_url,
        headlines=headlines,
        descriptions=descriptions,
        budget_micros=int(budget_daily_micros),
        keywords=clean_kw,
        match_type=match_type,
        keyword_match_types=clean_mts,
        cpc_bid_micros=int(cpc_bid_micros),
        geo_locations=geo_locations,
        geo_country_code=geo_country_code,
        geo_locale=geo_locale,
        languages=languages,
        bidding=bidding,
        path1=path1,
        path2=path2,
        url_options=url_options,
        networks=networks,
        ad_schedule_blocks=ad_schedule_blocks,
        start_date=start_date,
        end_date=end_date,
    )
    # Ассеты + изображения — ПОСЛЕ кампании (PAUSED/$0): сбой одного не роняет кампанию.
    campaign_id = (result.get("campaign") or "").rsplit("/", 1)[-1]
    if asset_specs and campaign_id:
        added, skipped = await run_ads_create_call(
            _attach_asset_specs_via_sdk,
            ads_client,
            customer_id,
            campaign_id,
            list(asset_specs),
            label="attach_asset_specs",
            account=customer_id,
            op_count=2 * max(1, len(asset_specs)),  # ассет + линк на каждый спек
        )
        result["assets_added"] = added
        result["assets_skipped"] = skipped
    # §19.7: переиспользование СУЩЕСТВУЮЩИХ ассетов аккаунта — линк к новой кампании по field_type.
    if existing_asset_links and campaign_id:
        result["assets_reused"] = await run_ads_create_call(
            _link_existing_assets_via_sdk,
            ads_client,
            customer_id,
            campaign_id,
            list(existing_asset_links),
            label="link_existing_assets",
            account=customer_id,
            op_count=max(1, len(existing_asset_links)),  # по линку на ассет
        )
    if image_specs and campaign_id:
        imgs = 0
        for img_bytes, name in image_specs:
            try:
                await run_ads_create_call(
                    extensions._attach_image_asset_via_sdk,
                    ads_client,
                    customer_id,
                    campaign_id,
                    img_bytes,
                    name,
                    label="attach_image_asset",
                    account=customer_id,
                    op_count=2,  # ассет + линк
                )
                imgs += 1
            except Exception:  # noqa: BLE001 — image-ассет может быть неприменим к аккаунту
                pass
        # §19.6: и запрошено, и добавлено — чтобы вызывающий увидел ТИХУЮ потерю (added < requested
        # на неподходящем аккаунте) и сообщил менеджеру, а не молча проглотил (раньше был только added).
        result["images_requested"] = len(image_specs)
        result["images_added"] = imgs
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _link_existing_assets_via_sdk(client, customer_id, campaign_id, links: list[dict]) -> int:
    """Привязать СУЩЕСТВУЮЩИЕ ассеты аккаунта (по asset_resource_name + field_type) к новой кампании.
    Группируем по field_type; каждый field_type линкуем через extensions._link_campaign_assets. Сбой
    одной группы не роняет остальное ($0/PAUSED). Возвращает число привязанных ассетов."""
    campaign_rn = extensions._campaign_rn(client, str(customer_id), str(campaign_id))
    by_ft: dict[str, list[str]] = {}
    for ln in links:
        rn = str(ln.get("asset_resource_name") or "")
        ft = str(ln.get("field_type") or "")
        if rn and ft:
            by_ft.setdefault(ft, []).append(rn)
    linked = 0
    for ft_name, rns in by_ft.items():
        try:
            ft = getattr(client.enums.AssetFieldTypeEnum, ft_name)
            extensions._link_campaign_assets(client, str(customer_id), campaign_rn, rns, ft)
            linked += len(rns)
        except Exception:  # noqa: BLE001 — недоступный/несовместимый ассет пропускаем
            pass
    return linked


def _attach_asset_specs_via_sdk(client, customer_id, campaign_id, specs: list[dict]):
    """Применить список asset-спеков к созданной кампании (PAUSED). Возвращает (added, skipped):
    skipped — семейства, требующие внешней конфигурации (location/affiliate/lead_form) или упавшие."""
    added: list[str] = []
    skipped: list[dict] = []
    for spec in specs:
        family = str(spec.get("family") or "")
        try:
            extensions.apply_asset_spec_via_sdk(client, customer_id, campaign_id, spec)
            added.append(family)
        except NotImplementedError as e:  # config-gated → пропускаем с пометкой
            skipped.append({"family": family, "reason": str(e)})
        except Exception as e:  # noqa: BLE001 — один плохой ассет не роняет $0/PAUSED кампанию
            skipped.append({"family": family, "reason": type(e).__name__})
    return added, skipped


# §19: имя языка/код → languageConstant id (best-effort; неизвестные языки пропускаем — таргетинг
# языка необязателен, по умолчанию все). Совмещён с keyword_plan.LANGUAGE_IDS (ru/uk/en).
_LANG_NAME_IDS: dict[str, int] = {
    "ru": 1031,
    "russian": 1031,
    "русский": 1031,
    "uk": 1036,
    "ukrainian": 1036,
    "украинский": 1036,
    "українська": 1036,
    "en": 1000,
    "english": 1000,
    "английский": 1000,
}


def _resolve_language_ids(languages: list[str] | None) -> list[int]:
    out: list[int] = []
    for lang in languages or []:
        lid = _LANG_NAME_IDS.get(str(lang).strip().casefold())
        if lid and lid not in out:
            out.append(lid)
    return out


# Минимальная биллинг-единица Google Ads для бид/бюджета — 10 000 micros (0.01 валюты). Значения,
# не кратные единице (например CPC = cost/clicks из медиан «по аналогии», §19.3), API отклоняет.
# Единый источник округления — core.limits.round_micros; алиасы сохраняют прежние имена
# (_MICROS_UNIT/_round_micros) для call-sites и тестов.
_MICROS_UNIT = BILLING_UNIT_MICROS
_round_micros = round_micros


def _create_search_campaign_via_sdk(
    client,
    customer_id: str,
    *,
    campaign_name: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    budget_micros: int,
    keywords: list[str],
    match_type: str,
    keyword_match_types: list[str] | None = None,
    cpc_bid_micros: int,
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
    languages: list[str] | None = None,
    bidding: dict | None = None,
    path1: str | None = None,
    path2: str | None = None,
    url_options: dict | None = None,
    networks: str | None = None,
    ad_schedule_blocks: list[dict] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Синхронная цепочка v24: budget → campaign(SEARCH,PAUSED,стратегия+URL-опции) → ad group(PAUSED)
    → RSA(PAUSED,+display path) → опц. ключи → опц. гео → опц. язык. Статусы PAUSED зашиты в КОДЕ → 0
    расхода. При сбое создания кампании удаляем осиротевший бюджет (как GDN); сбой после кампании
    оставляет PAUSED-сущности (безопасно, 0 расхода)."""
    cid = str(customer_id)
    stamp = str(int(time.time()))

    # 1) Бюджет.
    budget_svc = client.get_service("CampaignBudgetService")
    bop = client.get_type("CampaignBudgetOperation")
    bop.create.name = f"{campaign_name}_budget_{stamp}"
    bop.create.amount_micros = _round_micros(budget_micros)  # кратно биллинг-единице (§19.3)
    bop.create.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    bop.create.explicitly_shared = False
    budget_rn = (
        budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[bop])
        .results[0]
        .resource_name
    )

    # 2) Кампания (SEARCH, PAUSED, стратегия ставок, поиск + поисковые партнёры, опц. URL-опции).
    camp_svc = client.get_service("CampaignService")
    cop = client.get_type("CampaignOperation")
    c = cop.create
    c.name = campaign_name
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.status = client.enums.CampaignStatusEnum.PAUSED  # код решает — не показывается до включения
    c.campaign_budget = budget_rn
    # §19.3: конверс-стратегия без отслеживания конверсий → падение create; понижаем до Maximize Clicks.
    bidding, bidding_note = _downgrade_bidding_if_no_conversions(client, cid, bidding)
    _apply_bidding_on_create(client, c, bidding)  # стратегия из §19.3 (по умолчанию manual CPC)
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = True
    c.network_settings.target_content_network = False
    # §19.3: партнёрские сети — только по явному указанию менеджера (networks='search_partners').
    c.network_settings.target_partner_search_network = networks == "search_partners"
    # §19.3: даты запуска (ISO → YYYYMMDD как в официальных примерах). None ⇒ дефолты Google
    # (старт сегодня, без даты конца).
    if start_date:
        c.start_date = str(start_date).replace("-", "")
    if end_date:
        c.end_date = str(end_date).replace("-", "")
    _apply_url_options_on_create(client, c, url_options)  # §19.8 tracking/suffix/custom params
    try:  # v24 может требовать декларацию EU-политической рекламы при создании
        c.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except Exception:  # noqa: BLE001 — поле опционально на части аккаунтов
        pass
    try:
        campaign_rn = (
            camp_svc.mutate_campaigns(customer_id=cid, operations=[cop]).results[0].resource_name
        )
    except Exception:
        try:  # откат осиротевшего бюджета (explicitly_shared=False)
            dop = client.get_type("CampaignBudgetOperation")
            dop.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[dop])
        except Exception:  # noqa: BLE001
            pass
        raise
    campaign_id = campaign_rn.rsplit("/", 1)[-1]

    def _rollback_partial(created_ad_group_rn: str | None) -> None:
        """Откат осиротевших сущностей при сбое шага 3/4: удаляем группу (если создана), кампанию и
        бюджет. Иначе на аккаунте остаётся мусорная PAUSED-кампания ($0), а имя занято → повтор визарда
        падает на DUPLICATE_CAMPAIGN_NAME. Каждое удаление изолировано, чтобы сбой отката не маскировал
        исходную ошибку. Ключи/гео/язык ниже — best-effort и до сюда не доходят."""
        if created_ad_group_rn:
            try:
                op = client.get_type("AdGroupOperation")
                op.remove = created_ad_group_rn
                client.get_service("AdGroupService").mutate_ad_groups(
                    customer_id=cid, operations=[op]
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            op = client.get_type("CampaignOperation")
            op.remove = campaign_rn
            camp_svc.mutate_campaigns(customer_id=cid, operations=[op])
        except Exception:  # noqa: BLE001
            pass
        try:
            op = client.get_type("CampaignBudgetOperation")
            op.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[op])
        except Exception:  # noqa: BLE001
            pass

    # 3) Группа объявлений (SEARCH_STANDARD, PAUSED) + 4) RSA. Обёрнуты в единый try: сбой любой из
    # операций откатывает бюджет+кампанию(+группу) — не оставляем мусорную PAUSED-кампанию и не
    # занимаем имя. Ключи/гео/язык (шаги 5–8) — best-effort и кампанию не роняют.
    ad_group_rn: str | None = None
    try:
        ag_svc = client.get_service("AdGroupService")
        agop = client.get_type("AdGroupOperation")
        ag = agop.create
        ag.name = f"{campaign_name}_ag_{stamp}"
        ag.campaign = campaign_rn
        ag.status = client.enums.AdGroupStatusEnum.PAUSED
        ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
        ag.cpc_bid_micros = _round_micros(cpc_bid_micros)  # кратно биллинг-единице (§19.3 CPC)
        ad_group_rn = (
            ag_svc.mutate_ad_groups(customer_id=cid, operations=[agop]).results[0].resource_name
        )

        # 4) RSA-объявление (PAUSED, опц. display path).
        ad_svc = client.get_service("AdGroupAdService")
        adop = client.get_type("AdGroupAdOperation")
        aga = adop.create
        aga.ad_group = ad_group_rn
        aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
        aga.ad.final_urls.append(str(final_url))
        rsa = aga.ad.responsive_search_ad
        for text in headlines:
            a = client.get_type("AdTextAsset")
            a.text = text
            rsa.headlines.append(a)
        for text in descriptions:
            a = client.get_type("AdTextAsset")
            a.text = text
            rsa.descriptions.append(a)
        if path1:
            rsa.path1 = path1
            if path2:  # path2 допустим только при заданном path1 (прото v24)
                rsa.path2 = path2
        ad_rn = (
            ad_svc.mutate_ad_group_ads(customer_id=cid, operations=[adop]).results[0].resource_name
        )
    except Exception:
        _rollback_partial(ad_group_rn)  # чистим бюджет+кампанию(+группу), затем пробрасываем ошибку
        raise

    # 5) Опциональные ключевые слова в группу. Best-effort, как гео/язык ниже: сбой добавления
    # ключей (квота/битый ключ) НЕ роняет уже созданную PAUSED-кампанию ($0), иначе остались бы
    # осиротевшие бюджет+кампания+группа. kw_created=0 в результате сигналит о недобавленных ключах.
    kw_created = 0
    if keywords:
        try:
            crit_svc = client.get_service("AdGroupCriterionService")
            enabled = client.enums.AdGroupCriterionStatusEnum.ENABLED
            # §19.4.1: per-keyword типы (смешанный список) — 1:1 к keywords; иначе единый match_type.
            per_kw = keyword_match_types if keyword_match_types else [match_type] * len(keywords)
            kops = []
            for text, kmt in zip(keywords, per_kw):
                kop = client.get_type("AdGroupCriterionOperation")
                kop.create.ad_group = ad_group_rn
                kop.create.status = enabled
                kop.create.keyword.text = text
                kop.create.keyword.match_type = getattr(
                    client.enums.KeywordMatchTypeEnum, str(kmt).upper()
                )
                kops.append(kop)
            kw_created = len(
                crit_svc.mutate_ad_group_criteria(customer_id=cid, operations=kops).results
            )
        except Exception:  # noqa: BLE001 — ключи не добавились: PAUSED-кампания остаётся ($0)
            kw_created = (
                0  # результат вернёт keywords=0 — сигнал недобавленных ключей (как гео/язык)
            )

    # 6) Опц. гео (резолв названий → geoTargetConstant; reuse builder, remove-before-create на свежей).
    geo_count = 0
    if geo_locations:
        try:
            geo_res = _set_geo_location_via_sdk(
                client, cid, campaign_id, geo_locations, geo_country_code, geo_locale
            )
            geo_count = geo_res.get("count", 0)
        except Exception:  # noqa: BLE001 — гео не распознан → PAUSED-кампания остаётся (безопасно)
            geo_count = 0

    # 7) Опц. язык таргетинга (languageConstant id).
    lang_count = 0
    lang_ids = _resolve_language_ids(languages)
    if lang_ids:
        try:
            cc_svc = client.get_service("CampaignCriterionService")
            lops = []
            for lid in lang_ids:
                lop = client.get_type("CampaignCriterionOperation")
                lop.create.campaign = campaign_rn
                lop.create.language.language_constant = f"languageConstants/{lid}"
                lops.append(lop)
            lang_count = len(
                cc_svc.mutate_campaign_criteria(customer_id=cid, operations=lops).results
            )
        except Exception:  # noqa: BLE001 — язык необязателен (по умолчанию все)
            lang_count = 0

    # 8) §19.3: опц. расписание показов (ad_schedule criteria; [] ⇒ 24/7 — критерии не создаются).
    # Best-effort как гео/язык: сбой НЕ роняет PAUSED-кампанию ($0), schedule=0 сигналит в result.
    sched_count = 0
    if ad_schedule_blocks:
        try:
            cc_svc = client.get_service("CampaignCriterionService")
            sops = []
            for b in ad_schedule_blocks:
                sop = client.get_type("CampaignCriterionOperation")
                sop.create.campaign = campaign_rn
                sched = sop.create.ad_schedule
                sched.day_of_week = getattr(client.enums.DayOfWeekEnum, str(b["day"]))
                sched.start_hour = int(b["start_hour"])
                sched.end_hour = int(b["end_hour"])
                sched.start_minute = client.enums.MinuteOfHourEnum.ZERO
                sched.end_minute = client.enums.MinuteOfHourEnum.ZERO
                sops.append(sop)
            sched_count = len(
                cc_svc.mutate_campaign_criteria(customer_id=cid, operations=sops).results
            )
        except Exception:  # noqa: BLE001 — расписание необязательно (по умолчанию 24/7)
            sched_count = 0

    result = {
        "customer_id": cid,
        "campaign_name": campaign_name,
        "campaign": campaign_rn,
        "budget": budget_rn,
        "ad_group": ad_group_rn,
        "ad": ad_rn,
        "headlines": len(headlines),
        "descriptions": len(descriptions),
        "keywords": kw_created,
        "geo": geo_count,
        "languages": lang_count,
        "ad_schedule": sched_count,  # §19.3: сколько блоков расписания привязано (0 = 24/7)
        "status": "PAUSED",
        "applied": True,
    }
    # 3C: частичный успех best-effort-шагов 5–8 — ЯВНЫЕ warnings, а не молчаливый 0 в result
    # («кампания на Кению» без гео при запуске = глобальный показ; менеджер обязан это увидеть).
    # НЕ откатываем: кампания PAUSED/$0 — безопасна, недостающее добавляется отдельными командами.
    warnings: list[dict] = []

    def _warn(part: str, requested: int, applied_n: int) -> None:
        if requested and applied_n < requested:
            warnings.append({"part": part, "requested": requested, "applied": applied_n})

    _warn("keywords", len(keywords or []), kw_created)
    _warn("geo", len(geo_locations or []), geo_count)
    _warn("languages", len(lang_ids or []), lang_count)
    _warn("ad_schedule", len(ad_schedule_blocks or []), sched_count)
    if warnings:
        result["warnings"] = warnings
    if bidding_note:
        result["bidding_note"] = bidding_note  # §19.3: стратегия понижена (нет конверс-трекинга)
    return result


# §19.3: стратегии ставок, требующие включённого отслеживания конверсий (иначе create падает).
_CONVERSION_BIDDING = frozenset({"maximize_conversions", "maximize_conversion_value"})


def _conversion_tracking_enabled(client, customer_id: str) -> bool:
    """§19.3: включено ли отслеживание конверсий на аккаунте (нужно конверс-стратегиям). Читаем
    customer.conversion_tracking_setting.conversion_tracking_status. NOT_CONVERSION_TRACKED / сбой
    чтения → считаем ВЫКЛЮЧЕННЫМ (fail-safe: лучше понизить стратегию, чем упасть на create)."""
    try:
        ga = client.get_service("GoogleAdsService")
        enum = client.enums.ConversionTrackingStatusEnum
        off = {enum.NOT_CONVERSION_TRACKED, enum.UNKNOWN, enum.UNSPECIFIED}
        for row in ga.search(
            customer_id=str(customer_id),
            query=(
                "SELECT customer.conversion_tracking_setting.conversion_tracking_status "
                "FROM customer LIMIT 1"
            ),
        ):
            return row.customer.conversion_tracking_setting.conversion_tracking_status not in off
        return False
    except Exception:  # noqa: BLE001 — сбой чтения → fail-safe: считаем выключенным
        return False


def _downgrade_bidding_if_no_conversions(
    client, customer_id: str, bidding: dict | None
) -> tuple[dict | None, str | None]:
    """§19.3: конверс-стратегия (Maximize Conversions / Target CPA / Maximize Conv. Value) на
    аккаунте БЕЗ отслеживания конверсий → create падает GoogleAdsException. Тихо не роняем и не
    выдумываем: понижаем до Maximize Clicks (target_spend, конверсии не нужны) и возвращаем note
    для показа менеджеру. Иначе — bidding как есть, note=None. (На тест-Draft конверсий нет никогда.)"""
    if (
        bidding
        and bidding.get("strategy") in _CONVERSION_BIDDING
        and not _conversion_tracking_enabled(client, customer_id)
    ):
        clean = {k: v for k, v in bidding.items() if k not in ("target_cpa_micros", "target_roas")}
        clean["strategy"] = "target_spend"
        return clean, "maximize_clicks (отслеживание конверсий не настроено на аккаунте)"
    return bidding, None


def _apply_bidding_on_create(client, c, bidding: dict | None) -> None:
    """Стратегия ставок на CampaignOperation.create (§19.3). По умолчанию (None) — manual CPC, как
    раньше. Зеркалит логику _set_bidding_strategy_via_sdk, но БЕЗ update_mask (create)."""
    strategy = (bidding or {}).get("strategy") or "manual_cpc"
    if strategy == "manual_cpc":
        c.manual_cpc.enhanced_cpc_enabled = bool((bidding or {}).get("enhanced_cpc"))
    elif strategy == "maximize_conversions":
        tcpa = (bidding or {}).get("target_cpa_micros")
        if tcpa:
            c.maximize_conversions.target_cpa_micros = int(tcpa)
        else:
            client.copy_from(c.maximize_conversions, client.get_type("MaximizeConversions"))
    elif strategy == "maximize_conversion_value":
        roas = (bidding or {}).get("target_roas")
        if roas:
            c.maximize_conversion_value.target_roas = float(roas)
        else:
            client.copy_from(
                c.maximize_conversion_value, client.get_type("MaximizeConversionValue")
            )
    elif strategy == "target_spend":
        client.copy_from(c.target_spend, client.get_type("TargetSpend"))
    else:  # неизвестная → manual CPC (безопасный дефолт)
        c.manual_cpc.enhanced_cpc_enabled = False


def _apply_url_options_on_create(client, c, url_options: dict | None) -> None:
    """§19.8 Ad URL options на CampaignOperation.create: tracking_url_template / final_url_suffix /
    url_custom_parameters. Пустые поля не трогаем."""
    if not url_options:
        return
    tpl = (url_options.get("tracking_url_template") or "").strip()
    if tpl:
        c.tracking_url_template = tpl
    suffix = (url_options.get("final_url_suffix") or "").strip()
    if suffix:
        c.final_url_suffix = suffix
    for k, v in (url_options.get("custom_parameters") or {}).items():
        cp = client.get_type("CustomParameter")
        cp.key = str(k)
        cp.value = str(v)
        c.url_custom_parameters.append(cp)


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
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """Создать GDN-кампанию (Display) из фото за двойным гейтом. Все сущности — PAUSED (0 расхода).

    §11: опц. ГЕО-таргетинг (geo_locations) — часть авто-формируемой структуры; резолв названий и
    привязку делает КОД (reuse _set_geo_location_via_sdk). Пусто ⇒ без гео (как раньше).

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
    result = await run_ads_create_call(
        _create_gdn_campaign_via_sdk,
        ads_client,
        customer_id,
        label="create_gdn_campaign",
        account=customer_id,
        # бюджет+кампания+группа+объявление + 2 image-ассета + гео/язык (оценка сверху)
        op_count=8 + len(geo_locations or []),
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
        geo_locations=list(geo_locations or []),
        geo_country_code=geo_country_code,
        geo_locale=geo_locale,
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
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
) -> dict:
    """Синхронная 5-шаговая цепочка v24 (сверено live): asset×2 → budget → campaign(DISPLAY,PAUSED)
    → [опц. ГЕО] → ad group(PAUSED) → responsive_display_ad(PAUSED). Статусы PAUSED зашиты в КОДЕ →
    0 расхода. Имя кампании — как у пользователя; бюджет/группа получают stamp-суффикс.

    §11 ГЕО: после создания кампании (перед группой) резолвим названия локаций → geoTargetConstant и
    привязываем как campaign criteria через тот же живо-сверенный `_set_geo_location_via_sdk`. Сбой
    гео НЕ роняет кампанию (best-effort, geo_count=0) — как в create_search_campaign."""
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
    bop.create.amount_micros = _round_micros(budget_micros)  # кратно биллинг-единице (§19.3)
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
    try:
        campaign_rn = (
            camp_svc.mutate_campaigns(customer_id=cid, operations=[cop]).results[0].resource_name
        )
    except Exception:
        # Кампания не создалась → удаляем осиротевший бюджет (explicitly_shared=False, иначе копится
        # мусор). best-effort: ошибку отката глушим. Сбой ПОСЛЕ кампании (шаги 4-5) оставляет
        # PAUSED-сущности — безопасно (0 расхода), сложный каскадный откат не делаем.
        try:
            dop = client.get_type("CampaignBudgetOperation")
            dop.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[dop])
        except Exception:  # noqa: BLE001
            pass
        raise

    # 3.5) §11 ГЕО (опц.): резолв названий → geoTargetConstant + привязка к кампании (reuse). Сбой
    # гео не роняет кампанию (best-effort) — как в create_search_campaign.
    geo_count = 0
    if geo_locations:
        try:
            geo_res = _set_geo_location_via_sdk(
                client,
                cid,
                campaign_rn.split("/")[-1],  # campaign_id из resource_name
                list(geo_locations),
                geo_country_code,
                geo_locale,
            )
            geo_count = int(geo_res.get("count", 0))
        except Exception:  # noqa: BLE001 — гео best-effort, кампания остаётся (PAUSED, 0 расхода)
            geo_count = 0

    # 4) Группа объявлений (DISPLAY, PAUSED).
    ag_svc = client.get_service("AdGroupService")
    agop = client.get_type("AdGroupOperation")
    ag = agop.create
    ag.name = f"{campaign_name}_ag_{stamp}"
    ag.campaign = campaign_rn
    ag.status = client.enums.AdGroupStatusEnum.PAUSED
    ag.type_ = client.enums.AdGroupTypeEnum.DISPLAY_STANDARD
    ag.cpc_bid_micros = _round_micros(
        cpc_bid_micros
    )  # кратно биллинг-единице (§19.3 CPC «по аналогии»)
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
        "geo_count": geo_count,  # §11: сколько локаций привязано (0 = без гео)
        "status": "PAUSED",
        "applied": True,
    }


# ── §11: кампании из видео — Demand Gen и Video (YouTube-видео + confirm-гейт) ─────
# Лимиты Demand Gen video responsive ad: headline ≤40 (наш генератор даёт ≤30 — строже),
# long headline/description ≤90, business name ≤25. Video responsive ad: headline ≤30,
# long headline ≤90, description консервативно ≤70 (короче реальных лимитов ряда форматов —
# перепроверить live по актуальной документации; кириллица=1 считает КОД).
MEDIA_MAX_HEADLINES = 5
MEDIA_MAX_DESCRIPTIONS = 5
VIDEO_DESCRIPTION_MAX = 70


def _validate_video_campaign_inputs(
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    youtube_video_id: str,
    *,
    description_max: int = 90,
) -> None:
    """Полная валидация В КОДЕ — ДО claim (golden rule #4). Длины (кириллица=1), составы, URL,
    бюджет и YouTube id считает/проверяет КОД, не модель."""
    from ads.assets import parse_youtube_video_id

    if not 1 <= len(headlines) <= MEDIA_MAX_HEADLINES:
        raise ValueError(f"нужно 1–{MEDIA_MAX_HEADLINES} заголовков (передано {len(headlines)})")
    if not 1 <= len(descriptions) <= MEDIA_MAX_DESCRIPTIONS:
        raise ValueError(
            f"нужно 1–{MEDIA_MAX_DESCRIPTIONS} описаний (передано {len(descriptions)})"
        )
    for h in headlines:
        ok, n = _rsa_validate(h, "headline")  # ≤30, кириллица=1
        if not ok:
            raise ValueError(f"заголовок превышает лимит ({n}/30): «{h}»")
    ok, n = _rsa_validate(long_headline, "description")  # длинный заголовок ≤90
    if not ok:
        raise ValueError(f"длинный заголовок превышает лимит ({n}/90)")
    for d in descriptions:
        ok, n = _rsa_validate(d, "description")  # ≤90 базово
        if not ok or n > description_max:
            raise ValueError(f"описание превышает лимит ({max(n, 0)}/{description_max}): «{d}»")
    if not business_name or len(business_name) > GDN_BUSINESS_NAME_MAX:
        raise ValueError(f"название бизнеса 1–{GDN_BUSINESS_NAME_MAX} символов")
    if not final_url or not str(final_url).startswith(("http://", "https://")):
        raise ValueError("нужен валидный final_url (http/https)")
    if budget_daily_micros <= 0:
        raise ValueError("дневной бюджет должен быть > 0")
    if budget_daily_micros > MAX_AMOUNT_MICROS:
        raise ValueError("дневной бюджет подозрительно большой — проверь команду")
    if not parse_youtube_video_id(youtube_video_id):
        raise ValueError("не распознан YouTube video id (ссылка YouTube или 11-символьный id)")


async def apply_create_demand_gen_campaign(
    *,
    customer_id: str,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    logo_bytes: bytes | None = None,
    goal: str = "clicks",
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """§11: создать Demand Gen кампанию из YouTube-видео за двойным гейтом. Всё PAUSED (0 расхода).

    Видео живёт на YouTube (примечание ТЗ §11) — API принимает только его id. goal: 'clicks' →
    Maximize Clicks (работает без conversion tracking — как фикс §19.3), 'conversions' →
    Maximize Conversions. Логотип — квадратный image-ассет: live API ТРЕБУЕТ ≥1 logo_images
    (без него отклоняет объявление, TOO_FEW). ✅ SDK-цепочка СВЕРЕНА LIVE 2026-07-03
    (scripts/live_smoke_video_dg.py): создание PAUSED + перечитка из API прошли; попутные
    live-требования — ad.name обязателен, минимальный дневной бюджет DG (AUD-аккаунт) = 8 единиц.

    НЕ через run_ads_call: цепочка создающих вызовов НЕ идемпотентна. От повтора защищает атомарный
    claim; при сбое — record_failure, осиротевшие PAUSED-сущности безвредны (0 расхода)."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_video_campaign_inputs(
        headlines,
        long_headline,
        descriptions,
        business_name,
        final_url,
        budget_daily_micros,
        youtube_video_id,
        description_max=90,  # Demand Gen: описания ≤90
    )
    if goal not in ("clicks", "conversions"):
        raise ValueError("goal должен быть 'clicks' или 'conversions'")
    proposal = await _require_confirmation(
        confirm_store, confirmation_id, "create_demand_gen_campaign"
    )
    # Создание кампании — ТОЛЬКО прямой командой пользователя (как бюджет/GDN).
    if not proposal.user_initiated:
        raise PermissionError("создание кампании должно быть прямой командой пользователя")
    result = await run_ads_create_call(
        _create_demand_gen_campaign_via_sdk,
        ads_client,
        customer_id,
        label="create_demand_gen_campaign",
        account=customer_id,
        # бюджет+кампания+группа+объявление + video/logo-ассеты + гео/язык (оценка сверху)
        op_count=8 + len(geo_locations or []),
        campaign_name=campaign_name,
        youtube_video_id=youtube_video_id,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
        business_name=business_name,
        final_url=final_url,
        budget_micros=int(budget_daily_micros),
        logo_bytes=logo_bytes,
        goal=goal,
        geo_locations=list(geo_locations or []),
        geo_country_code=geo_country_code,
        geo_locale=geo_locale,
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _create_demand_gen_campaign_via_sdk(
    client,
    customer_id: str,
    *,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_micros: int,
    logo_bytes: bytes | None,
    goal: str,
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
) -> dict:
    """Синхронная цепочка v24 (по официальному add_demand_gen_campaign.py; ⚠️ live-сверка перед
    сдачей): yt-asset → [опц. logo-asset] → budget → campaign(DEMAND_GEN, PAUSED) → [опц. ГЕО] →
    ad group(PAUSED) → demand_gen_video_responsive_ad(PAUSED). PAUSED зашит в КОДЕ → 0 расхода.
    Сбой кампании → откат осиротевшего бюджета (как GDN)."""
    from ads.assets import parse_youtube_video_id, upload_image_asset, upload_youtube_video_asset

    cid = str(customer_id)
    stamp = str(int(time.time()))
    vid = parse_youtube_video_id(youtube_video_id) or ""

    # 1) YouTube-видео-ассет (+ опц. логотип 1:1).
    video_rn = upload_youtube_video_asset(client, cid, vid, f"{campaign_name}_video_{stamp}")
    logo_rn = None
    if logo_bytes:
        logo_rn = upload_image_asset(client, cid, logo_bytes, f"{campaign_name}_logo_{stamp}")

    # 2) Бюджет (DG требует НЕ-shared бюджет).
    budget_svc = client.get_service("CampaignBudgetService")
    bop = client.get_type("CampaignBudgetOperation")
    bop.create.name = f"{campaign_name}_budget_{stamp}"
    bop.create.amount_micros = _round_micros(budget_micros)  # кратно биллинг-единице (§19.3)
    bop.create.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    bop.create.explicitly_shared = False
    budget_rn = (
        budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[bop])
        .results[0]
        .resource_name
    )

    # 3) Кампания (DEMAND_GEN, PAUSED). Стратегия: clicks → Maximize Clicks (без conversion
    # tracking, §19.3-фикс); conversions → Maximize Conversions. Пустые сообщения — copy_from
    # (паттерн _set_bidding_strategy_via_sdk).
    camp_svc = client.get_service("CampaignService")
    cop = client.get_type("CampaignOperation")
    c = cop.create
    c.name = campaign_name
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
    c.status = client.enums.CampaignStatusEnum.PAUSED  # код решает — не показывается до включения
    c.campaign_budget = budget_rn
    if goal == "conversions":
        client.copy_from(c.maximize_conversions, client.get_type("MaximizeConversions"))
    else:
        client.copy_from(c.target_spend, client.get_type("TargetSpend"))
    try:  # v24 может требовать декларацию EU-политической рекламы при создании
        c.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except Exception:  # noqa: BLE001 — поле опционально на части аккаунтов
        pass
    try:
        campaign_rn = (
            camp_svc.mutate_campaigns(customer_id=cid, operations=[cop]).results[0].resource_name
        )
    except Exception:
        try:  # откат осиротевшего бюджета (как GDN), best-effort
            dop = client.get_type("CampaignBudgetOperation")
            dop.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[dop])
        except Exception:  # noqa: BLE001
            pass
        raise

    # 3.5) §11 ГЕО (опц., best-effort — как GDN/Search).
    geo_count = 0
    if geo_locations:
        try:
            geo_res = _set_geo_location_via_sdk(
                client,
                cid,
                campaign_rn.split("/")[-1],
                list(geo_locations),
                geo_country_code,
                geo_locale,
            )
            geo_count = int(geo_res.get("count", 0))
        except Exception:  # noqa: BLE001
            geo_count = 0

    # 4) Группа объявлений (PAUSED; для DEMAND_GEN тип группы не задаётся — по примеру Google).
    ag_svc = client.get_service("AdGroupService")
    agop = client.get_type("AdGroupOperation")
    ag = agop.create
    ag.name = f"{campaign_name}_ag_{stamp}"
    ag.campaign = campaign_rn
    ag.status = client.enums.AdGroupStatusEnum.PAUSED
    ad_group_rn = (
        ag_svc.mutate_ad_groups(customer_id=cid, operations=[agop]).results[0].resource_name
    )

    # 5) Demand Gen video responsive ad (PAUSED). Live-сверка 2026-07-03: API ТРЕБУЕТ ad.name
    # (REQUIRED «The required field was not present») и ≥1 logo_images (collection_size TOO_FEW)
    # — DG без логотипа объявление не создаст (визард/схема должны дать логотип).
    ad_svc = client.get_service("AdGroupAdService")
    adop = client.get_type("AdGroupAdOperation")
    aga = adop.create
    aga.ad_group = ad_group_rn
    aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
    aga.ad.name = f"{campaign_name}_ad_{stamp}"  # live: обязательное поле для DG-объявления
    aga.ad.final_urls.append(str(final_url))
    dg = aga.ad.demand_gen_video_responsive_ad

    def _txt(text):
        a = client.get_type("AdTextAsset")
        a.text = text
        return a

    def _vid_asset(rn):
        a = client.get_type("AdVideoAsset")
        a.asset = rn
        return a

    for h in headlines:
        dg.headlines.append(_txt(h))
    dg.long_headlines.append(_txt(long_headline))
    for d in descriptions:
        dg.descriptions.append(_txt(d))
    dg.videos.append(_vid_asset(video_rn))
    dg.business_name.text = business_name
    if logo_rn:
        li = client.get_type("AdImageAsset")
        li.asset = logo_rn
        dg.logo_images.append(li)
    try:
        ad_rn = (
            ad_svc.mutate_ad_group_ads(customer_id=cid, operations=[adop]).results[0].resource_name
        )
    except Exception:
        # Live-находка 2026-07-03: сбой ad-шага оставлял осиротевшие budget+campaign+ad_group.
        # Кампания без объявления бесполезна → откатываем всё (best-effort, зеркало
        # _rollback_partial у Search; порядок: группа → кампания → бюджет).
        try:
            agdel = client.get_type("AdGroupOperation")
            agdel.remove = ad_group_rn
            client.get_service("AdGroupService").mutate_ad_groups(
                customer_id=cid, operations=[agdel]
            )
        except Exception:  # noqa: BLE001 — откат best-effort (сирота PAUSED безвредна)
            pass
        try:
            cdel = client.get_type("CampaignOperation")
            cdel.remove = campaign_rn
            client.get_service("CampaignService").mutate_campaigns(
                customer_id=cid, operations=[cdel]
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            bdel = client.get_type("CampaignBudgetOperation")
            bdel.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[bdel])
        except Exception:  # noqa: BLE001
            pass
        raise

    return {
        "customer_id": cid,
        "campaign_name": campaign_name,
        "campaign": campaign_rn,
        "budget": budget_rn,
        "ad_group": ad_group_rn,
        "ad": ad_rn,
        "video_asset": video_rn,
        "logo_asset": logo_rn,
        "goal": goal,
        "geo_count": geo_count,
        "headlines": len(headlines),
        "descriptions": len(descriptions),
        "status": "PAUSED",
        "applied": True,
    }


async def apply_create_video_campaign(
    *,
    customer_id: str,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    """§11: создать Video-кампанию (YouTube) из видео за двойным гейтом. Всё PAUSED (0 расхода).

    Video responsive ad + target CPM (охват). Описания валидируются консервативно ≤70
    (VIDEO_DESCRIPTION_MAX). ⚠️ LIVE-СВЕРКА 2026-07-03: Google API отклоняет создание
    VIDEO-кампаний на стандартном доступе — MUTATE_NOT_ALLOWED (trigger «VIDEO»): создание
    видеокампаний через API доступно только по allowlist Google. Код корректен и остаётся
    (заявка на allowlist — операционный шаг заказчика); РАБОЧИЙ путь «кампания из видео»
    сегодня — Demand Gen (сверен live, рекомендован в /newvideo).

    НЕ через run_ads_call: цепочка создающих вызовов НЕ идемпотентна. От повтора защищает атомарный
    claim; при сбое — record_failure, осиротевшие PAUSED-сущности безвредны (0 расхода)."""
    ensure_allowed(customer_id)  # гейт 1 — замок аккаунта
    _validate_video_campaign_inputs(
        headlines,
        long_headline,
        descriptions,
        business_name,
        final_url,
        budget_daily_micros,
        youtube_video_id,
        description_max=VIDEO_DESCRIPTION_MAX,  # Video: описания ≤70 (консервативно)
    )
    proposal = await _require_confirmation(confirm_store, confirmation_id, "create_video_campaign")
    if not proposal.user_initiated:
        raise PermissionError("создание кампании должно быть прямой командой пользователя")
    result = await run_ads_create_call(
        _create_video_campaign_via_sdk,
        ads_client,
        customer_id,
        label="create_video_campaign",
        account=customer_id,
        # бюджет+кампания+группа+объявление + video-ассет + гео/язык (оценка сверху)
        op_count=7 + len(geo_locations or []),
        campaign_name=campaign_name,
        youtube_video_id=youtube_video_id,
        headlines=headlines,
        long_headline=long_headline,
        descriptions=descriptions,
        business_name=business_name,
        final_url=final_url,
        budget_micros=int(budget_daily_micros),
        geo_locations=list(geo_locations or []),
        geo_country_code=geo_country_code,
        geo_locale=geo_locale,
    )
    await confirm_store.finalize(confirmation_id, result=result)
    return result


def _create_video_campaign_via_sdk(
    client,
    customer_id: str,
    *,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_micros: int,
    geo_locations: list[str] | None = None,
    geo_country_code: str = "UA",
    geo_locale: str = "ru",
) -> dict:
    """Синхронная цепочка v24 (⚠️ live-сверка перед сдачей): yt-asset → budget →
    campaign(VIDEO, PAUSED, target CPM) → [опц. ГЕО] → ad group(VIDEO_RESPONSIVE, PAUSED) →
    video_responsive_ad(PAUSED). PAUSED зашит в КОДЕ → 0 расхода. Сбой кампании → откат бюджета."""
    from ads.assets import parse_youtube_video_id, upload_youtube_video_asset

    cid = str(customer_id)
    stamp = str(int(time.time()))
    vid = parse_youtube_video_id(youtube_video_id) or ""

    # 1) YouTube-видео-ассет.
    video_rn = upload_youtube_video_asset(client, cid, vid, f"{campaign_name}_video_{stamp}")

    # 2) Бюджет.
    budget_svc = client.get_service("CampaignBudgetService")
    bop = client.get_type("CampaignBudgetOperation")
    bop.create.name = f"{campaign_name}_budget_{stamp}"
    bop.create.amount_micros = _round_micros(budget_micros)  # кратно биллинг-единице (§19.3)
    bop.create.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    bop.create.explicitly_shared = False
    budget_rn = (
        budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[bop])
        .results[0]
        .resource_name
    )

    # 3) Кампания (VIDEO, PAUSED, target CPM — охватная стратегия видеокампаний).
    camp_svc = client.get_service("CampaignService")
    cop = client.get_type("CampaignOperation")
    c = cop.create
    c.name = campaign_name
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.VIDEO
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.campaign_budget = budget_rn
    client.copy_from(c.target_cpm, client.get_type("TargetCpm"))
    try:
        c.contains_eu_political_advertising = (
            client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        campaign_rn = (
            camp_svc.mutate_campaigns(customer_id=cid, operations=[cop]).results[0].resource_name
        )
    except Exception:
        try:  # откат осиротевшего бюджета, best-effort
            dop = client.get_type("CampaignBudgetOperation")
            dop.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[dop])
        except Exception:  # noqa: BLE001
            pass
        raise

    # 3.5) §11 ГЕО (опц., best-effort).
    geo_count = 0
    if geo_locations:
        try:
            geo_res = _set_geo_location_via_sdk(
                client,
                cid,
                campaign_rn.split("/")[-1],
                list(geo_locations),
                geo_country_code,
                geo_locale,
            )
            geo_count = int(geo_res.get("count", 0))
        except Exception:  # noqa: BLE001
            geo_count = 0

    # 4) Группа объявлений (VIDEO_RESPONSIVE, PAUSED).
    ag_svc = client.get_service("AdGroupService")
    agop = client.get_type("AdGroupOperation")
    ag = agop.create
    ag.name = f"{campaign_name}_ag_{stamp}"
    ag.campaign = campaign_rn
    ag.status = client.enums.AdGroupStatusEnum.PAUSED
    ag.type_ = client.enums.AdGroupTypeEnum.VIDEO_RESPONSIVE
    ad_group_rn = (
        ag_svc.mutate_ad_groups(customer_id=cid, operations=[agop]).results[0].resource_name
    )

    # 5) Video responsive ad (PAUSED). ad.name — как в DG (live: REQUIRED у DG; ставим и здесь —
    # поле легально для всех Ad и снимает тот же класс отказа на Video).
    ad_svc = client.get_service("AdGroupAdService")
    adop = client.get_type("AdGroupAdOperation")
    aga = adop.create
    aga.ad_group = ad_group_rn
    aga.status = client.enums.AdGroupAdStatusEnum.PAUSED
    aga.ad.name = f"{campaign_name}_ad_{stamp}"
    aga.ad.final_urls.append(str(final_url))
    vr = aga.ad.video_responsive_ad

    def _txt(text):
        a = client.get_type("AdTextAsset")
        a.text = text
        return a

    for h in headlines:
        vr.headlines.append(_txt(h))
    vr.long_headlines.append(_txt(long_headline))
    for d in descriptions:
        vr.descriptions.append(_txt(d))
    va = client.get_type("AdVideoAsset")
    va.asset = video_rn
    vr.videos.append(va)
    vr.business_name.text = business_name
    try:
        ad_rn = (
            ad_svc.mutate_ad_group_ads(customer_id=cid, operations=[adop]).results[0].resource_name
        )
    except Exception:
        # Зеркало DG-отката (live-находка): кампания без объявления бесполезна — чистим
        # группу → кампанию → бюджет (best-effort; сирота PAUSED безвредна).
        try:
            agdel = client.get_type("AdGroupOperation")
            agdel.remove = ad_group_rn
            client.get_service("AdGroupService").mutate_ad_groups(
                customer_id=cid, operations=[agdel]
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            cdel = client.get_type("CampaignOperation")
            cdel.remove = campaign_rn
            client.get_service("CampaignService").mutate_campaigns(
                customer_id=cid, operations=[cdel]
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            bdel = client.get_type("CampaignBudgetOperation")
            bdel.remove = budget_rn
            budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[bdel])
        except Exception:  # noqa: BLE001
            pass
        raise

    return {
        "customer_id": cid,
        "campaign_name": campaign_name,
        "campaign": campaign_rn,
        "budget": budget_rn,
        "ad_group": ad_group_rn,
        "ad": ad_rn,
        "video_asset": video_rn,
        "geo_count": geo_count,
        "headlines": len(headlines),
        "descriptions": len(descriptions),
        "status": "PAUSED",
        "applied": True,
    }

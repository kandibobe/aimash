"""Оркестратор выполнения подтверждённого черновика: резолв (имя→id/бюджет) → gated apply_*.

Вызывается из бота на «да». Чтение SDK (резолв) — синхронное → asyncio.to_thread.
Аккаунт всегда Aimash Draft (замок в ads.client). Поддержаны (SUPPORTED_OPERATIONS):
update_budget, update_bid, add_keywords, remove_keywords, add_negative_keywords, pause_campaign,
resume_campaign, set_geo_proximity, create_rsa, create_gdn_campaign.
"""

from __future__ import annotations

import asyncio

from ads import mutations, resolve
from ads.client import DRAFT_ACCOUNT_ID, build_client

# ── Единый источник истины: какие операции РЕАЛЬНО исполняются за confirm-гейтом. ──
# Это потолок возможностей: всё, чего тут нет, агент обязан отклонить ДО показа кнопок
# (capability-guard в agent.loop), а execute_confirmed — отвергнуть как defense-in-depth.
# Так закрывается класс «падает ПОСЛЕ ✅»: пользователь не подтверждает то, что не сделаем.
SUPPORTED_OPERATIONS: frozenset[str] = frozenset(
    {
        "update_budget",
        "update_bid",
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "pause_campaign",
        "resume_campaign",
        "set_geo_proximity",
        "create_rsa",
        "create_gdn_campaign",
    }
)


async def execute_confirmed(store, confirmation_id: str) -> dict:
    """store — ConfirmStore. Возвращает result операции или бросает ошибку."""
    p = await store.get_confirmed(confirmation_id)
    if p is None:
        raise ValueError("черновик не найден")
    if p.status != "confirmed":
        raise PermissionError(f"черновик не подтверждён (status={p.status})")

    op = p.operation
    params = p.params
    # Defense-in-depth: неподдержанную операцию не исполняем даже при дыре в loop-гейте
    # (зеркало SUPPORTED_OPERATIONS / capability-guard) — op вне списка отвергаем здесь же.
    if op not in SUPPORTED_OPERATIONS:
        raise PermissionError(
            f"операция '{op}' не поддерживается (capability-guard) — выполнение отклонено"
        )

    client = build_client()
    customer_id = DRAFT_ACCOUNT_ID  # замок: только Aimash Draft

    if op == "update_budget":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        new_micros = resolve.compute_new_micros(ref.budget_micros, params["mode"], params["value"])
        return await mutations.apply_update_budget(
            customer_id=customer_id,
            campaign_id=ref.id,
            new_budget_micros=new_micros,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "pause_campaign":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_pause_campaign(
            customer_id=customer_id,
            campaign_id=ref.id,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "resume_campaign":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_resume_campaign(
            customer_id=customer_id,
            campaign_id=ref.id,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "update_bid":
        # Ставка живёт на ad group → резолвим группы кампании и считаем новую ставку для каждой.
        ad_groups = await asyncio.to_thread(
            resolve.find_ad_groups, client, customer_id, params["campaign"]
        )
        if not ad_groups:
            raise ValueError(
                f"в кампании '{params['campaign']}' нет групп объявлений (или кампания не найдена)"
            )
        bids = [
            (ag.id, resolve.compute_new_micros(ag.cpc_bid_micros, params["mode"], params["value"]))
            for ag in ad_groups
        ]
        return await mutations.apply_update_bid(
            customer_id=customer_id,
            campaign_id=ad_groups[0].campaign_id,
            bids=bids,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "add_keywords":
        # Ключи добавляются в группы кампании (во все группы кампании).
        ad_groups = await asyncio.to_thread(
            resolve.find_ad_groups, client, customer_id, params["campaign"]
        )
        if not ad_groups:
            raise ValueError(
                f"в кампании '{params['campaign']}' нет групп объявлений (или кампания не найдена)"
            )
        return await mutations.apply_add_keywords(
            customer_id=customer_id,
            ad_group_ids=[ag.id for ag in ad_groups],
            keywords=params["keywords"],
            match_type=params["match_type"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "remove_keywords":
        # Симметрично add_keywords: удаляем из ВСЕХ групп кампании (по тексту+типу).
        ad_groups = await asyncio.to_thread(
            resolve.find_ad_groups, client, customer_id, params["campaign"]
        )
        if not ad_groups:
            raise ValueError(
                f"в кампании '{params['campaign']}' нет групп объявлений (или кампания не найдена)"
            )
        return await mutations.apply_remove_keywords(
            customer_id=customer_id,
            ad_group_ids=[ag.id for ag in ad_groups],
            keywords=params["keywords"],
            match_type=params["match_type"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "add_negative_keywords":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_add_negative_keywords(
            customer_id=customer_id,
            campaign_id=ref.id,
            keywords=params["keywords"],
            match_type=params["match_type"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "set_geo_proximity":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        # Адаптер: схема даёт структурные поля → собираем address-dict (Google геокодит сам,
        # клиентский геокодинг не нужен). country_code по умолчанию UA (проект — Украина).
        address = {
            "city_name": params["city_name"],
            "country_code": params.get("country_code", "UA"),
            "street_address": params.get("street_address"),
            "postal_code": params.get("postal_code"),
        }
        return await mutations.apply_set_geo_proximity(
            customer_id=customer_id,
            campaign_id=ref.id,
            radius_km=params["radius_km"],
            address=address,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "create_gdn_campaign":
        # Подготовленные изображения (landscape+square) лежат во временном хранилище по media_id
        # (бинарь НЕ в proposal.params/логах). Резолв кампании не нужен — это создание новой.
        from ads.assets import clear_pending_media, load_pending_media

        landscape, square = load_pending_media(params["media_id"])
        try:
            return await mutations.apply_create_gdn_campaign(
                customer_id=customer_id,
                campaign_name=params["campaign_name"],
                landscape_bytes=landscape,
                square_bytes=square,
                headlines=params["headlines"],
                long_headline=params["long_headline"],
                descriptions=params["descriptions"],
                business_name=params["business_name"],
                final_url=params["final_url"],
                budget_daily_micros=params["budget_daily_micros"],
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        finally:
            clear_pending_media(params["media_id"])  # успех или сбой — временные файлы чистим

    if op == "create_rsa":
        # Группа уже зарезолвлена в курации (ad_group_id в params) → доп. резолв не нужен.
        # Замок аккаунта и валидацию набора держит сам apply_create_rsa.
        return await mutations.apply_create_rsa(
            customer_id=customer_id,
            ad_group_id=params["ad_group_id"],
            headlines=params["headlines"],
            descriptions=params["descriptions"],
            final_url=params["final_url"],
            path1=params.get("path1"),
            path2=params.get("path2"),
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    # Не должно случиться: op ∈ SUPPORTED_OPERATIONS, но ветки нет (рассинхрон). Fail-closed.
    raise ValueError(f"операция '{op}' заявлена поддержанной, но не имеет обработчика (баг)")

"""Оркестратор выполнения подтверждённого черновика: резолв (имя→id/бюджет) → gated apply_*.

Вызывается из бота на «да». Чтение SDK (резолв) — синхронное → asyncio.to_thread.
Аккаунт всегда Aimash Draft (замок в ads.client). Поддержаны (SUPPORTED_OPERATIONS):
update_budget, update_bid, add_keywords, add_negative_keywords, pause_campaign, resume_campaign.
set_geo_proximity — отложен (A-geo): исполнитель готов в ads.mutations, но не активирован.
"""

from __future__ import annotations

import asyncio

from ads import mutations, resolve
from ads.client import DRAFT_ACCOUNT_ID, build_client

# ── Единый источник истины: какие операции РЕАЛЬНО исполняются за confirm-гейтом. ──
# Это потолок возможностей: всё, чего тут нет, агент обязан отклонить ДО показа кнопок
# (capability-guard в agent.loop), а execute_confirmed — отвергнуть как defense-in-depth.
# Так закрывается класс «падает ПОСЛЕ ✅»: пользователь не подтверждает то, что не сделаем.
#
# set_geo_proximity НАМЕРЕННО исключён (подзадача A-geo): код-исполнитель готов
# (mutations._set_geo_proximity_via_sdk), но address-based точка требует валидации
# на живом тест-аккаунте (геокодинг). До проверки — не объявляем поддержку.
# Включение = добавить "set_geo_proximity" сюда (одна строка).
SUPPORTED_OPERATIONS: frozenset[str] = frozenset(
    {
        "update_budget",
        "update_bid",
        "add_keywords",
        "add_negative_keywords",
        "pause_campaign",
        "resume_campaign",
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
    # (зеркало SUPPORTED_OPERATIONS / capability-guard). set_geo_proximity тут отвергается (A-geo).
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

    # Не должно случиться: op ∈ SUPPORTED_OPERATIONS, но ветки нет (рассинхрон). Fail-closed.
    raise ValueError(f"операция '{op}' заявлена поддержанной, но не имеет обработчика (баг)")

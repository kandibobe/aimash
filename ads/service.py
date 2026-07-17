"""Оркестратор выполнения подтверждённого черновика: резолв (имя→id/бюджет) → gated apply_*.

Вызывается из бота на «да». Чтение SDK (резолв) — синхронное → asyncio.to_thread.
Аккаунт исполнения — из proposal.customer_id (штампует доверенный вход бота; сегодня всегда
Aimash Draft) с ПОВТОРНЫМ ensure_allowed на исполнении (fail-closed, замок в ads.client).
Поддержаны (SUPPORTED_OPERATIONS): update_budget, update_bid, add/remove keywords и negative,
pause/resume, гео, стратегии, аудитории, RSA/кампании, ассеты-расширения.
"""

from __future__ import annotations

import asyncio

from ads import mutations, resolve
from ads.client import DRAFT_ACCOUNT_ID, build_client_async, ensure_allowed
from ads.read import (  # D6: «было» для гео-мутаций; валюта → биллинг-единица округления денег
    account_currency,
    read_campaign_targeting,
)
from core.config import normalize_customer_id, settings  # D7: гео-дефолты из env, не хардкод «UA»
from core.logging import log, redact_text  # Доп.2A: лог сбоя пост-проверки (редактируем текст)

# ── Единый источник истины: какие операции РЕАЛЬНО исполняются за confirm-гейтом. ──
# Это потолок возможностей: всё, чего тут нет, агент обязан отклонить ДО показа кнопок
# (capability-guard в agent.loop), а execute_confirmed — отвергнуть как defense-in-depth.
# Так закрывается класс «падает ПОСЛЕ ✅»: пользователь не подтверждает то, что не сделаем.
SUPPORTED_OPERATIONS: frozenset[str] = frozenset(
    {
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
        "pause_campaign",
        "resume_campaign",
        "launch_campaign",
        "update_campaign",
        "set_campaign_network",
        "set_campaign_display_network",
        "set_campaign_geo_target_type",
        "remove_campaign",
        "remove_ad_group",
        "pause_ad_group",
        "resume_ad_group",
        "pause_ad",
        "resume_ad",
        "remove_ad",
        "set_geo_proximity",
        "set_geo_location",
        "set_bidding_strategy",
        "attach_audience",
        "detach_audience",
        "create_rsa",
        "create_gdn_campaign",
        "create_search_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
        "add_sitelinks",
        "add_callouts",
        "add_structured_snippets",
        "attach_image_asset",
        "add_call_asset",
        "add_promotion",
        "add_price_asset",
        "remove_asset_link",
    }
)


# Операции, для которых имеет смысл показать реальное «было» (§5) и сверить дрейф при исполнении.
_DIFFABLE_OPS = frozenset(
    {
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "pause_campaign",
        "resume_campaign",
        "launch_campaign",
        "update_campaign",
        "set_campaign_network",
        "set_campaign_display_network",
        "set_campaign_geo_target_type",
        "pause_ad_group",
        "resume_ad_group",
        "pause_ad",
        "resume_ad",
        "set_geo_location",
        "set_geo_proximity",
        "set_bidding_strategy",
    }
)


async def _currency(client, customer_id: str) -> str | None:
    """Валюта аккаунта — от неё зависит биллинг-единица округления (UGX/JPY = 1 000 000, USD = 10 000).
    Нужна ОБОИМ путям: превью («станет» на карточке) и исполнению — иначе применится не то, что
    подтвердили. None при сбое чтения → дефолтная единица; авторитетно округляет граница SDK."""
    try:
        return (await asyncio.to_thread(account_currency, client, str(customer_id))) or None
    except Exception:  # noqa: BLE001 — справочное чтение не должно ронять показ/исполнение
        return None


async def read_before(operation: str, params: dict, customer_id: str | None = None) -> dict | None:
    """READ-ONLY снимок ТЕКУЩЕГО значения для показа реального «было→станет» (§5) и как база
    оптимистичной сверки при исполнении (TOCTOU). None — операция без diff (создание/ключи),
    кампания не найдена или чтение не удалось (вызывающий честно покажет diff без «было»).

    customer_id — аккаунт черновика (дефолт Draft: сегодня мутации только на нём; при будущем
    расширении потолка вызывающий передаёт активный мутационный аккаунт — см. ads/client.py:28).
    Возвращаемый dict кладётся в proposals.params['_before'] и сверяется в execute_confirmed."""
    if operation not in _DIFFABLE_OPS:
        return None
    name = params.get("campaign")
    if not name:
        return None
    try:
        cid = normalize_customer_id(customer_id) if customer_id else DRAFT_ACCOUNT_ID
        client = await build_client_async(cid)  # холодная сборка (после /refresh) — вне loop
        if operation == "update_budget":
            ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, name)
            if ref is None:
                return None
            cur = await _currency(client, cid)
            after = resolve.compute_new_micros(
                ref.budget_micros, params["mode"], params["value"], currency=cur
            )
            snap = {
                "kind": "budget",
                "before_micros": int(ref.budget_micros),
                "after_micros": int(after),
                "currency": cur or "",  # для карточки: zero-decimal валюты показываем без копеек
            }
            # П1: общий бюджет? explicitly_shared / reference_count>1 ⇒ есть ДРУГИЕ кампании на том же
            # бюджете — изменение затронет их ВСЕ. Раскрываем радиус на карточке (§5, информированное
            # согласие) и помечаем _before.shared: execute_confirmed прокинет это как disclosed_shared_
            # scope в apply_update_budget, чей fail-closed гард иначе откажет (радиус не раскрыт).
            if ref.budget_explicitly_shared or int(ref.budget_reference_count or 0) > 1:
                others = await asyncio.to_thread(
                    resolve.campaigns_sharing_budget, client, cid, ref.budget_resource
                )
                names = [c["name"] for c in others if c["name"] != name]
                if names:
                    snap["shared"] = True
                    snap["shared_campaigns"] = names
            return snap
        if operation in ("pause_campaign", "resume_campaign", "launch_campaign"):
            ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, name)
            if ref is None:
                return None
            return {"kind": "status", "before_status": ref.status}
        if operation == "update_campaign":
            ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, name)
            if ref is None:
                return None
            return {"kind": "name", "before_name": ref.name}
        if operation == "set_campaign_network":
            info = await asyncio.to_thread(resolve.campaign_network_settings, client, cid, name)
            if info is None:
                return None
            return {
                "kind": "network",
                "before_search_partners": bool(info["search_partners"]),
                "after_search_partners": bool(params.get("search_partners")),
            }
        if operation == "set_campaign_display_network":
            info = await asyncio.to_thread(resolve.campaign_display_network, client, cid, name)
            if info is None:
                return None
            return {
                "kind": "display_network",
                "before_display_network": bool(info["display_network"]),
                "after_display_network": bool(params.get("display_network")),
            }
        if operation == "set_campaign_geo_target_type":
            info = await asyncio.to_thread(resolve.campaign_geo_target_type, client, cid, name)
            if info is None:
                return None
            return {
                "kind": "geo_target_type",
                "before_geo_target_type": str(info["geo_target_type"] or ""),
                "after_geo_target_type": str(params.get("geo_target_type") or ""),
            }
        if operation in ("pause_ad_group", "resume_ad_group"):
            ag = await asyncio.to_thread(
                resolve.find_ad_group_by_name, client, cid, name, params.get("ad_group", "")
            )
            if ag is None:
                return None
            return {"kind": "status", "before_status": ag.status}
        if operation in ("pause_ad", "resume_ad"):
            ads_found = await asyncio.to_thread(
                resolve.find_ads_in_group,
                client,
                cid,
                name,
                params.get("ad_group", ""),
                params.get("ad", ""),
            )
            if len(ads_found) != 1:  # не найдено/неоднозначно — честно без «было»
                return None
            return {"kind": "status", "before_status": ads_found[0].status}
        if operation == "update_bid":
            ad_groups = await asyncio.to_thread(resolve.find_ad_groups, client, cid, name)
            if not ad_groups:
                return None
            cur = await _currency(client, cid)
            before_list = [int(ag.cpc_bid_micros) for ag in ad_groups]
            after_list = [
                int(
                    resolve.compute_new_micros(
                        ag.cpc_bid_micros, params["mode"], params["value"], currency=cur
                    )
                )
                for ag in ad_groups
            ]
            return {
                "kind": "bid",
                "before_micros": before_list,
                "after_micros": after_list,
                "n_groups": len(ad_groups),
                "currency": cur or "",
            }
        if operation == "update_keyword_bid":
            # Ф1: ставка на уровне КЛЮЧА. База «было» — effective-ставка критерия (своя или
            # унаследованная от группы): именно она играет на аукционе, от неё честен процент.
            kws = await asyncio.to_thread(
                resolve.find_keywords,
                client,
                cid,
                name,
                params["keyword"],
                params.get("ad_group") or None,
                params.get("match_type") or None,
            )
            if not kws:
                return None
            cur = await _currency(client, cid)
            return {
                "kind": "keyword_bid",
                "before_micros": [int(k.bid_micros) for k in kws],
                # СВОЯ ли была ставка у критерия. Нужно откату (ревизия волны): у ключа без своей
                # ставки «было» — это ставка ГРУППЫ, и set_to вернул бы не прежнее состояние, а НОВУЮ
                # собственную ставку — наследование от группы молча оборвалось бы. Такой откат
                # `_reverse_spec` не предлагает вовсе.
                "own_bid": [bool(k.own_bid) for k in kws],
                "after_micros": [
                    int(
                        resolve.compute_new_micros(
                            k.bid_micros, params["mode"], params["value"], currency=cur
                        )
                    )
                    for k in kws
                ],
                "n_keywords": len(kws),
                "keyword": kws[0].text,
                "ad_groups": [k.ad_group for k in kws],
                "currency": cur or "",
            }
        if operation in ("set_geo_location", "set_geo_proximity"):
            # D6: текущее ГЕО (локации/радиусы) для «было→станет». Резолвим кампанию по имени → id,
            # затем read_campaign_targeting. Снимок только для показа (дрейф гео не проверяем).
            ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, name)
            if ref is None:
                return None
            t = await asyncio.to_thread(read_campaign_targeting, client, cid, ref.id)
            return {
                "kind": "geo",
                "before_locations": list(t.locations),
                "before_proximity": list(t.proximity),
            }
        if operation == "set_bidding_strategy":
            info = await asyncio.to_thread(resolve.campaign_bidding_strategy, client, cid, name)
            if info is None:
                return None
            return {"kind": "bidding", "before_strategy": info["strategy"]}
    except resolve.DecreaseBelowZero:
        # НЕ сбой чтения, а невыполнимая команда («снизь бюджет на 200» при бюджете 100). Пробрасываем:
        # вызывающий откажет ДО кнопок ✅. Проглотить её здесь = показать карточку без «было» и упасть
        # уже ПОСЛЕ подтверждения (claim сожжён, операция не применена).
        raise
    except Exception:  # сеть/доступ/SDK — не блокируем показ черновика, просто без «было»
        return None
    return None


def _assert_no_drift(params: dict, current) -> None:
    """Оптимистичная блокировка (§5/TOCTOU): если текущее значение изменилось с момента показа
    черновика — НЕ применяем (для относительного режима пересчёт шёл бы от другой базы). Требуем
    переподтверждения. Для set_to (абсолют) база не влияет на итог → не блокируем."""
    if params.get("mode") == "set_to":
        return
    before = params.get("_before")
    if not isinstance(before, dict):
        return  # старый черновик без снимка — сверять нечего (не ломаем обратную совместимость)
    snap = before.get("before_micros")
    if snap is None:
        return
    if isinstance(
        snap, list
    ):  # bid: список ставок групп/ключей (порядок — ORDER BY id, детерминирован)
        cur = [int(x) for x in current]
        if [int(x) for x in snap] != cur:
            what = "ставки ключей" if before.get("kind") == "keyword_bid" else "ставки групп"
            raise ValueError(
                f"{what} изменились с момента показа черновика — переподтверди команду "
                "(создай черновик заново, чтобы увидеть актуальное «было → станет»)"
            )
        return
    if int(snap) != int(current):  # budget
        raise ValueError(
            f"бюджет кампании изменился с момента показа ({int(snap) / 1_000_000:.2f} → "
            f"{int(current) / 1_000_000:.2f}) — переподтверди команду (создай черновик заново)"
        )


async def _apply_confirmed(store, confirmation_id: str) -> dict:
    """Внутреннее ядро исполнения: резолв → gated apply_*. store — ConfirmStore. Возвращает result
    операции или бросает ошибку. Публичный вход — execute_confirmed (оборачивает окном пост-
    проверки Доп.2A); все прежние гейты/инварианты (defense-in-depth, ensure_allowed, claim) — здесь."""
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

    # Аккаунт исполнения — ИЗ ЧЕРНОВИКА (штампует доверенный вход бота при показе), а не хардкод:
    # хранимый customer_id теперь authoritative. Повторный ensure_allowed ЗДЕСЬ (fail-closed:
    # пустой/чужой штамп → PermissionError ДО build_client и ДО apply_* — черновик падает, не съев
    # одноразовый claim и не тронув SDK). Штамп — любой ВИДИМЫЙ аккаунт (прод-дефолт allow_all_visible,
    # решение владельца 2026-07); повторный ensure_allowed гарантирует, что аккаунт всё ещё в потолке.
    customer_id = normalize_customer_id(p.customer_id)
    ensure_allowed(customer_id)
    client = await build_client_async(customer_id)

    if op == "update_budget":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        _assert_no_drift(params, ref.budget_micros)  # §5/TOCTOU: бюджет не изменился с показа
        new_micros = resolve.compute_new_micros(
            ref.budget_micros,
            params["mode"],
            params["value"],
            currency=await _currency(client, customer_id),
        )
        return await mutations.apply_update_budget(
            customer_id=customer_id,
            campaign_id=ref.id,
            new_budget_micros=new_micros,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
            # П1: общий scope раскрыт на карточке? _before.shared проставляет read_before, когда к
            # бюджету привязаны и другие кампании. Только тогда гард apply_update_budget пропустит
            # изменение общего бюджета (иначе fail-closed отказ — пользователь не видел радиус).
            disclosed_shared_scope=bool((params.get("_before") or {}).get("shared")),
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

    if op == "launch_campaign":
        # §19.8/§11: «Запустить» = включить кампанию ПОЛНОСТЬЮ (кампания + группы + объявления),
        # иначе PAUSED группа/объявление ⇒ 0 показов. Резолв кампании по имени → apply_launch_campaign.
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_launch_campaign(
            customer_id=customer_id,
            campaign_id=ref.id,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "update_campaign":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_update_campaign(
            customer_id=customer_id,
            campaign_id=ref.id,
            new_name=params["new_name"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "set_campaign_network":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_set_campaign_network(
            customer_id=customer_id,
            campaign_id=ref.id,
            search_partners=bool(params.get("search_partners")),
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "set_campaign_display_network":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_set_campaign_display_network(
            customer_id=customer_id,
            campaign_id=ref.id,
            display_network=bool(params.get("display_network")),
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "set_campaign_geo_target_type":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_set_campaign_geo_target_type(
            customer_id=customer_id,
            campaign_id=ref.id,
            geo_target_type=str(params.get("geo_target_type") or ""),
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op in ("pause_ad", "resume_ad", "remove_ad"):
        # C6: объявление резолвим по id/заголовку ВНУТРИ группы; неоднозначность — честный отказ
        # со списком кандидатов (денежная поверхность — не угадываем).
        matches = await asyncio.to_thread(
            resolve.find_ads_in_group,
            client,
            customer_id,
            params["campaign"],
            params["ad_group"],
            params.get("ad", ""),
        )
        if not matches:
            raise ValueError(
                f"объявление «{params.get('ad', '')}» не найдено в группе "
                f"«{params['ad_group']}» (кампания «{params['campaign']}»)"
            )
        if len(matches) > 1:
            listing = "; ".join(f"id {a.ad_id} — «{a.headline[:40]}»" for a in matches[:5])
            raise ValueError(f"найдено несколько объявлений, уточни id: {listing}")
        ad = matches[0]
        ad_apply = {
            "pause_ad": mutations.apply_pause_ad,
            "resume_ad": mutations.apply_resume_ad,
            "remove_ad": mutations.apply_remove_ad,
        }[op]
        return await ad_apply(
            customer_id=customer_id,
            ad_group_id=ad.ad_group_id,
            ad_id=ad.ad_id,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op in ("pause_ad_group", "resume_ad_group"):
        # Статус живёт на ad_group → резолвим конкретную группу по имени ВНУТРИ кампании.
        ag = await asyncio.to_thread(
            resolve.find_ad_group_by_name,
            client,
            customer_id,
            params["campaign"],
            params["ad_group"],
        )
        if ag is None:
            raise ValueError(
                f"группа «{params['ad_group']}» в кампании «{params['campaign']}» не найдена"
            )
        apply = (
            mutations.apply_pause_ad_group
            if op == "pause_ad_group"
            else mutations.apply_resume_ad_group
        )
        return await apply(
            customer_id=customer_id,
            ad_group_id=ag.id,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "remove_campaign":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_remove_campaign(
            customer_id=customer_id,
            campaign_id=ref.id,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "remove_ad_group":
        ag = await asyncio.to_thread(
            resolve.find_ad_group_by_name,
            client,
            customer_id,
            params["campaign"],
            params["ad_group"],
        )
        if ag is None:
            raise ValueError(
                f"группа «{params['ad_group']}» в кампании «{params['campaign']}» не найдена"
            )
        return await mutations.apply_remove_ad_group(
            customer_id=customer_id,
            ad_group_id=ag.id,
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
        # §5/TOCTOU: ставки групп не изменились с момента показа (иначе % считался бы от иной базы)
        _assert_no_drift(params, [ag.cpc_bid_micros for ag in ad_groups])
        cur = await _currency(client, customer_id)
        bids = [
            (
                ag.id,
                resolve.compute_new_micros(
                    ag.cpc_bid_micros, params["mode"], params["value"], currency=cur
                ),
            )
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

    if op == "update_keyword_bid":
        # Ф1: ставка живёт на критерии → резолвим ключ по тексту (+ сужения ad_group/match_type).
        kws = await asyncio.to_thread(
            resolve.find_keywords,
            client,
            customer_id,
            params["campaign"],
            params["keyword"],
            params.get("ad_group") or None,
            params.get("match_type") or None,
        )
        if not kws:
            raise ValueError(
                f"ключевое слово «{params['keyword']}» не найдено в кампании "
                f"'{params['campaign']}' (проверь текст, группу и тип соответствия)"
            )
        # §5/TOCTOU: ставки ключей не изменились с момента показа (иначе % считался бы от иной базы)
        _assert_no_drift(params, [k.bid_micros for k in kws])
        cur = await _currency(client, customer_id)
        bids = [
            (
                k.ad_group_id,
                k.criterion_id,
                resolve.compute_new_micros(
                    k.bid_micros, params["mode"], params["value"], currency=cur
                ),
            )
            for k in kws
        ]
        return await mutations.apply_update_keyword_bid(
            customer_id=customer_id,
            campaign_id=kws[0].campaign_id,
            bids=bids,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "add_keywords":
        # Ключи добавляются в группы кампании; params['ad_group'] (опц.) СУЖАЕТ адрес до одной группы.
        # Без сужения ключ ложится во ВСЕ группы кампании — это ровно та каннибализация, которую сам
        # же аудит и флажит. Имя группы не совпало → ОТКАЗ (fail-closed), а не веер по всем группам.
        ad_groups = await asyncio.to_thread(
            resolve.find_ad_groups, client, customer_id, params["campaign"]
        )
        if not ad_groups:
            raise ValueError(
                f"в кампании '{params['campaign']}' нет групп объявлений (или кампания не найдена)"
            )
        want = str(params.get("ad_group") or "").strip()
        if want:
            ad_groups = [ag for ag in ad_groups if str(ag.name) == want]
            if not ad_groups:
                raise ValueError(
                    f"в кампании '{params['campaign']}' нет группы объявлений '{want}'"
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
        # 3.2б: params['ad_group'] (опц.) СУЖАЕТ уровень до одной группы (негативный
        # ad_group_criterion). Имя группы не совпало → ОТКАЗ (fail-closed) ДО claim,
        # а не тихий откат на уровень кампании — это разные последствия, не деталь.
        ad_group_id = None
        want = str(params.get("ad_group") or "").strip()
        if want:
            ag = await asyncio.to_thread(
                resolve.find_ad_group_by_name, client, customer_id, params["campaign"], want
            )
            if ag is None:
                raise ValueError(
                    f"в кампании '{params['campaign']}' нет группы объявлений '{want}'"
                )
            ad_group_id = ag.id
        return await mutations.apply_add_negative_keywords(
            customer_id=customer_id,
            campaign_id=ref.id,
            keywords=params["keywords"],
            match_type=params["match_type"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
            ad_group_id=ad_group_id,
        )

    if op == "remove_negative_keywords":
        # Симметрично add_negative_keywords: снимаем минус-слова кампании по тексту+типу.
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_remove_negative_keywords(
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
            "country_code": params.get("country_code") or settings.geo_default_country,
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

    if op == "set_geo_location":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_set_geo_location(
            customer_id=customer_id,
            campaign_id=ref.id,
            locations=params["locations"],
            country_code=params.get("country_code") or settings.geo_default_country,
            locale=params.get("locale") or settings.geo_default_locale,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "set_bidding_strategy":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_set_bidding_strategy(
            customer_id=customer_id,
            campaign_id=ref.id,
            strategy=params["strategy"],
            target_cpa=params.get("target_cpa"),
            target_roas=params.get("target_roas"),
            enhanced_cpc=params.get("enhanced_cpc", False),
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "create_gdn_campaign":
        # Подготовленные изображения (landscape+square) лежат во временном хранилище по media_id
        # (бинарь НЕ в proposal.params/логах). Резолв кампании не нужен — это создание новой.
        from ads.assets import clear_pending_media, load_pending_media

        # Файловый I/O (чтение JPEG-байтов) — в поток, как и все SDK-вызовы: единый event loop
        # делит бот с APScheduler, синхронное чтение блокировало бы все хендлеры/джобы.
        landscape, square = await asyncio.to_thread(load_pending_media, params["media_id"])
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
                geo_locations=params.get("geo_locations") or [],  # §11: опц. ГЕО
                geo_country_code=params.get("geo_country_code") or settings.geo_default_country,
                geo_locale=params.get("geo_locale") or settings.geo_default_locale,
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        finally:
            # успех или сбой — временные файлы чистим (тоже в потоке: .unlink() — диск-I/O)
            await asyncio.to_thread(clear_pending_media, params["media_id"])

    if op == "create_demand_gen_campaign":
        # §11: Demand Gen из YouTube-видео. Логотип (опц.) — во временном хранилище по
        # logo_media_id (бинарь НЕ в proposal.params/логах); квадратный кадр — вторым элементом.
        from ads.assets import clear_pending_media, load_pending_media

        logo_mid = params.get("logo_media_id")
        logo_bytes = None
        if logo_mid:
            _, logo_bytes = await asyncio.to_thread(load_pending_media, logo_mid)
        try:
            return await mutations.apply_create_demand_gen_campaign(
                customer_id=customer_id,
                campaign_name=params["campaign_name"],
                youtube_video_id=params["youtube_video_id"],
                headlines=params["headlines"],
                long_headline=params["long_headline"],
                descriptions=params["descriptions"],
                business_name=params["business_name"],
                final_url=params["final_url"],
                budget_daily_micros=params["budget_daily_micros"],
                logo_bytes=logo_bytes,
                goal=params.get("goal", "clicks"),
                geo_locations=params.get("geo_locations") or [],
                geo_country_code=params.get("geo_country_code") or settings.geo_default_country,
                geo_locale=params.get("geo_locale") or settings.geo_default_locale,
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        finally:
            if logo_mid:  # успех или сбой — временный логотип чистим
                await asyncio.to_thread(clear_pending_media, logo_mid)

    if op == "create_video_campaign":
        # §11: Video-кампания (YouTube). Видео уже на YouTube — временных файлов нет.
        return await mutations.apply_create_video_campaign(
            customer_id=customer_id,
            campaign_name=params["campaign_name"],
            youtube_video_id=params["youtube_video_id"],
            headlines=params["headlines"],
            long_headline=params["long_headline"],
            descriptions=params["descriptions"],
            business_name=params["business_name"],
            final_url=params["final_url"],
            budget_daily_micros=params["budget_daily_micros"],
            geo_locations=params.get("geo_locations") or [],
            geo_country_code=params.get("geo_country_code") or settings.geo_default_country,
            geo_locale=params.get("geo_locale") or settings.geo_default_locale,
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "attach_audience":
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_attach_audience(
            customer_id=customer_id,
            campaign_id=ref.id,
            audience_resource_names=params["audience_resource_names"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "detach_audience":
        # Симметрично attach_audience: снимаем ранее прикреплённые аудитории с кампании.
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        return await mutations.apply_detach_audience(
            customer_id=customer_id,
            campaign_id=ref.id,
            audience_resource_names=params["audience_resource_names"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op in ("add_sitelinks", "add_callouts", "add_structured_snippets"):
        # §3-assets: резолв кампании по имени → id, дальше apply_* (замок/гейт/валидация внутри).
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        if op == "add_sitelinks":
            return await mutations.apply_add_sitelinks(
                customer_id=customer_id,
                campaign_id=ref.id,
                sitelinks=params["sitelinks"],
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        if op == "add_callouts":
            return await mutations.apply_add_callouts(
                customer_id=customer_id,
                campaign_id=ref.id,
                callouts=params["callouts"],
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        return await mutations.apply_add_structured_snippets(
            customer_id=customer_id,
            campaign_id=ref.id,
            header=params["header"],
            values=params["values"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "attach_image_asset":
        # Подготовленное изображение лежит во временном хранилище по media_id (бинарь НЕ в params).
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        from ads.assets import clear_pending_media, load_pending_media

        landscape, _square = await asyncio.to_thread(load_pending_media, params["media_id"])
        try:
            return await mutations.apply_attach_image_asset(
                customer_id=customer_id,
                campaign_id=ref.id,
                image_bytes=landscape,
                name=params["name"],
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        finally:
            await asyncio.to_thread(clear_pending_media, params["media_id"])

    if op in ("add_call_asset", "add_promotion", "add_price_asset"):
        # §3-assets семейство 3: резолв кампании по имени → id, дальше apply_* (замок/гейт внутри).
        ref = await asyncio.to_thread(
            resolve.find_campaign_by_name, client, customer_id, params["campaign"]
        )
        if ref is None:
            raise ValueError(f"кампания '{params['campaign']}' не найдена")
        if op == "add_call_asset":
            return await mutations.apply_add_call_asset(
                customer_id=customer_id,
                campaign_id=ref.id,
                phone_number=params["phone_number"],
                country_code=params.get("country_code") or settings.geo_default_country,
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        if op == "add_promotion":
            return await mutations.apply_add_promotion(
                customer_id=customer_id,
                campaign_id=ref.id,
                promotion_target=params["promotion_target"],
                final_url=params["final_url"],
                percent_off=params.get("percent_off"),
                money_off_units=params.get("money_off_units"),
                currency=params.get("currency"),
                promo_code=params.get("promo_code"),
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        return await mutations.apply_add_price_asset(
            customer_id=customer_id,
            campaign_id=ref.id,
            price_type=params.get("price_type", "services"),
            currency=params["currency"],
            # D7: пусто → язык из конфига деплоя, не «uk» литералом (схема уже кладёт дефолт, но
            # старые черновики в БД могли сохраниться без поля).
            language_code=params.get("language_code") or settings.geo_default_locale,
            offerings=params["offerings"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "remove_asset_link":
        # Резолв кампании не нужен — link_resource_names несут аккаунт; замок/гейт держит apply_*.
        return await mutations.apply_remove_asset_link(
            customer_id=customer_id,
            link_resource_names=params["link_resource_names"],
            confirmation_id=confirmation_id,
            confirm_store=store,
            ads_client=client,
        )

    if op == "create_search_campaign":
        # Создание новой кампании — резолв существующей не нужен. Замок/валидацию/гейт держит
        # сам apply_create_search_campaign (двойной гейт + user_initiated, всё PAUSED).
        # §19: изображения приходят как media_ids — грузим бинарь из временного хранилища (как GDN)
        # в (landscape) bytes и чистим после; передаём как image_specs. Бинарь НЕ в params/логах.
        image_specs: list[tuple[bytes, str]] = []
        media_ids = list(params.get("image_media_ids") or [])
        if media_ids:
            from ads.assets import load_pending_media

            # П6/П1-медиа: media_id перечислены в params ПОДТВЕРЖДЁННОГО черновика — это часть diff'а,
            # который пользователь увидел и подтвердил (карточка показывает число изображений). Если
            # кадр пропал (преждевременный unlink: supersede визарда / TTL-cleanup / гонка отмены),
            # НЕ глотаем — иначе создали бы кампанию с ДРУГИМ составом, чем подтверждён (нарушение
            # золотых правил 1/6: код применяет РОВНО подтверждённый diff или падает). raise идёт ДО
            # apply_* (до claim, SDK ещё не звался) → _do_confirm ловит, record_failure помечает
            # confirmed→failed + audit, юзеру уходит редактированное «медиа устарело». Повтор =
            # новый черновик через возобновление визарда, а не тихая порча.
            for mid in media_ids:
                landscape, _square = await asyncio.to_thread(load_pending_media, mid)
                image_specs.append((landscape, f"{params['campaign_name']}_img"))
        try:
            return await mutations.apply_create_search_campaign(
                customer_id=customer_id,
                campaign_name=params["campaign_name"],
                final_url=params["final_url"],
                headlines=params["headlines"],
                descriptions=params["descriptions"],
                budget_daily_micros=params["budget_daily_micros"],
                keywords=params.get("keywords"),
                match_type=params.get("match_type", "phrase"),
                keyword_match_types=params.get("keyword_match_types") or None,  # §19.4.1: mixed
                cpc_bid_micros=params.get("cpc_bid_micros") or None,  # None → дефолт по валюте
                geo_locations=params.get("geo_locations") or None,
                geo_country_code=params.get("geo_country_code") or settings.geo_default_country,
                geo_locale=params.get("geo_locale") or settings.geo_default_locale,
                languages=params.get("languages") or None,
                bidding=params.get("bidding"),
                path1=params.get("path1"),
                path2=params.get("path2"),
                url_options=params.get("url_options"),
                asset_specs=params.get("asset_specs") or None,
                existing_asset_links=params.get("existing_asset_links") or None,
                image_specs=image_specs or None,
                networks=params.get("networks"),  # §19.3: сети/расписание/даты
                ad_schedule_blocks=params.get("ad_schedule_blocks") or None,
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                confirmation_id=confirmation_id,
                confirm_store=store,
                ads_client=client,
            )
        finally:
            if media_ids:
                from ads.assets import clear_pending_media

                for mid in media_ids:
                    await asyncio.to_thread(clear_pending_media, mid)

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


async def execute_confirmed(store, confirmation_id: str) -> dict:
    """Публичный вход: исполнить подтверждённый черновик + ОКНО ПОСТ-ПРОВЕРКИ (Доп.2A).

    Это чужие деньги: после «✅ применено» никто не перечитывал аккаунт, а `_assert_no_drift`
    защищает лишь ДО применения (TOCTOU). Тут, ПОСЛЕ успешного apply и только для diffable-операций
    со снимком `_before`, READ-ONLY перечитываем аккаунт теми же резолверами, что кормят read_before,
    и сверяем фактическое значение с ожидаемым «станет». Расхождение (частичный сбой/гонка/тихая
    деградация SDK) → applied→needs_review + флаг наверх (бот предупредит).

    Инварианты, которые здесь НЕ нарушаются:
    • Гейт денег цел (golden rule #3): _verify_applied — чистое ЧТЕНИЕ, ads.mutations не трогает.
    • Все прежние проверки/исключения летят из _apply_confirmed БЕЗ перехвата (форма отказа не
      изменилась: ValueError/PermissionError доходят до вызывающего как раньше).
    • Сбой самой проверки НЕ откатывает уже применённую мутацию (degrade → verified=None).
    • result операции остаётся прежним; ключ `verification` дописывается ТОЛЬКО при расхождении
      (verified is False), чтобы не менять контракт result для happy-path."""
    result = await _apply_confirmed(store, confirmation_id)
    # Метаданные для сверки берём ПОСЛЕ apply (статус теперь applied): op/params/customer_id.
    try:
        snap = await store.get_confirmed(confirmation_id)
    except Exception:  # noqa: BLE001 — метаданные не прочитать → пропускаем проверку, не роняем apply
        return result
    if snap is None or snap.operation not in _DIFFABLE_OPS or not isinstance(result, dict):
        return result
    try:
        verification = await _verify_applied(snap.operation, snap.params or {}, snap.customer_id)
    except Exception as e:  # noqa: BLE001 — проверка READ-ONLY: её сбой не влияет на исход мутации
        log.warning(
            "post-apply verify cid=%s не выполнена: %s",
            confirmation_id,
            redact_text(str(e)),
        )
        return result
    if verification.get("verified") is False:
        result = {**result, "verification": verification}
        try:
            await store.record_verification(confirmation_id, verification=verification)
        except Exception:  # noqa: BLE001 — флаг не записали: мутация всё равно применена, лог и дальше
            log.exception("record_verification не записан cid=%s", confirmation_id)
    return result


def _verify_cmp(kind: str, expected, actual) -> dict:
    """Итог сверки. expected/actual is None ⇒ проверить НЕЧЕМ (нет снимка/значение не прочитано):
    verified=None — НЕ расхождение (fail-safe, без ложного needs_review). Оба конкретны ⇒ сравниваем."""
    if expected is None or actual is None:
        return {"verified": None, "kind": kind, "expected": expected, "actual": actual}
    return {
        "verified": bool(expected == actual),
        "kind": kind,
        "expected": expected,
        "actual": actual,
    }


async def _verify_applied(op: str, params: dict, customer_id: str | None) -> dict:
    """Доп.2A: ПОСТ-проверка применённой мутации. READ-ONLY повторное чтение аккаунта теми же
    резолверами, что кормят read_before, и сверка фактического значения с ожидаемым «станет».
    Возвращает {"verified": True|False|None, "kind", "expected", "actual"}.

    🔒 ads.mutations здесь НЕ вызывается — это чистое чтение (golden rule #3 цел). Гейт: без снимка
    `_before` (реальные черновики из _present_proposal его всегда несут) не сверяем — verified=None,
    чтобы прямые тест-черновики и легаси-строки не давали ложных расхождений. Для kind без «после»
    в снимке (status/name/bidding) ожидаемое выводим из самой операции/params."""
    before = params.get("_before")
    if not isinstance(before, dict) or not before:
        return {"verified": None, "reason": "no_before_snapshot"}
    cid = normalize_customer_id(customer_id) if customer_id else DRAFT_ACCOUNT_ID
    name = params.get("campaign")
    client = await build_client_async(cid)

    if op == "update_budget":
        ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, name)
        actual = int(ref.budget_micros) if ref is not None else None
        exp = before.get("after_micros")
        return _verify_cmp("budget", int(exp) if exp is not None else None, actual)

    if op == "update_bid":
        ags = await asyncio.to_thread(resolve.find_ad_groups, client, cid, name)
        actual = [int(ag.cpc_bid_micros) for ag in ags] if ags else None
        exp = before.get("after_micros")
        exp = [int(x) for x in exp] if isinstance(exp, list) else None
        # позиционная сверка (тот же ORDER BY ad_group.id) — разошлись длины ⇒ сверять нечем
        if isinstance(exp, list) and isinstance(actual, list) and len(exp) != len(actual):
            return {"verified": None, "kind": "bid", "expected": exp, "actual": actual}
        return _verify_cmp("bid", exp, actual)

    if op == "update_keyword_bid":
        kws = await asyncio.to_thread(
            resolve.find_keywords,
            client,
            cid,
            name,
            params.get("keyword"),
            params.get("ad_group") or None,
            params.get("match_type") or None,
        )
        actual = [int(k.bid_micros) for k in kws] if kws else None
        exp = before.get("after_micros")
        exp = [int(x) for x in exp] if isinstance(exp, list) else None
        if isinstance(exp, list) and isinstance(actual, list) and len(exp) != len(actual):
            return {"verified": None, "kind": "keyword_bid", "expected": exp, "actual": actual}
        return _verify_cmp("keyword_bid", exp, actual)

    if op in ("pause_campaign", "resume_campaign", "launch_campaign"):
        ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, name)
        actual = ref.status if ref is not None else None
        exp = "PAUSED" if op == "pause_campaign" else "ENABLED"
        return _verify_cmp("status", exp, actual)

    if op in ("pause_ad_group", "resume_ad_group"):
        ag = await asyncio.to_thread(
            resolve.find_ad_group_by_name, client, cid, name, params.get("ad_group", "")
        )
        actual = ag.status if ag is not None else None
        exp = "PAUSED" if op == "pause_ad_group" else "ENABLED"
        return _verify_cmp("status", exp, actual)

    if op in ("pause_ad", "resume_ad"):
        matches = await asyncio.to_thread(
            resolve.find_ads_in_group,
            client,
            cid,
            name,
            params.get("ad_group", ""),
            params.get("ad", ""),
        )
        actual = matches[0].status if len(matches) == 1 else None  # неоднозначно ⇒ сверять нечем
        exp = "PAUSED" if op == "pause_ad" else "ENABLED"
        return _verify_cmp("status", exp, actual)

    if op == "update_campaign":
        # после переименования кампания зовётся new_name → ищем по нему
        new_name = params.get("new_name")
        ref = await asyncio.to_thread(resolve.find_campaign_by_name, client, cid, new_name)
        actual = ref.name if ref is not None else None
        return _verify_cmp("name", new_name or None, actual)

    if op == "set_campaign_network":
        info = await asyncio.to_thread(resolve.campaign_network_settings, client, cid, name)
        actual = bool(info["search_partners"]) if info is not None else None
        return _verify_cmp("network", bool(params.get("search_partners")), actual)

    if op == "set_campaign_display_network":
        info = await asyncio.to_thread(resolve.campaign_display_network, client, cid, name)
        actual = bool(info["display_network"]) if info is not None else None
        return _verify_cmp("display_network", bool(params.get("display_network")), actual)

    if op == "set_campaign_geo_target_type":
        info = await asyncio.to_thread(resolve.campaign_geo_target_type, client, cid, name)
        # actual "" = Google вернул UNSPECIFIED/UNKNOWN → сверить нечем (не флагуем)
        actual = (str(info["geo_target_type"] or "") or None) if info is not None else None
        exp = str(params.get("geo_target_type") or "") or None
        return _verify_cmp("geo_target_type", exp, actual)

    if op == "set_bidding_strategy":
        info = await asyncio.to_thread(resolve.campaign_bidding_strategy, client, cid, name)
        actual = (str(info["strategy"] or "").upper() or None) if info is not None else None
        # literal схемы (manual_cpc/…) → ENUM-имя (MANUAL_CPC/…) — прямой upper() совпадает 1:1
        exp = str(params.get("strategy") or "").upper() or None
        return _verify_cmp("bidding", exp, actual)

    # set_geo_location / set_geo_proximity: применённое гео Google нормализует (id/радиусы) —
    # надёжной поэлементной сверки нет, не флагуем (verified=None).
    return {"verified": None, "kind": "geo", "reason": "geo_not_verifiable"}

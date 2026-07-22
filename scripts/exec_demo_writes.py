"""Live-демо Блока A: новые мутации за двойным гейтом на ТЕСТ-кампании (PAUSED, аккаунт 7753643025).

Демонстрирует: для каждой операции — proposal → без «да» execute БЛОКИРУЕТСЯ (confirm-гейт) →
confirm → реальное выполнение → чтение результата обратно из API → audit-хвост.

Операции: update_bid (ставка на ad group), add_keywords (+2 ключа), add_negative_keywords (+1 минус),
pause_campaign → resume_campaign. БЮДЖЕТ НЕ ТРОГАЕМ (это путь scripts/exec_demo.py).

Ставка пройдёт только при ручной стратегии MANUAL_CPC — иначе будет видна понятная ошибка
(BID_TYPE_AND_BIDDING_STRATEGY_MISMATCH-гард), это ожидаемо. Запуск: python scripts/exec_demo_writes.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from ads.client import DRAFT_ACCOUNT_ID, build_client  # noqa: E402
from ads.read import list_campaigns  # noqa: E402
from ads.resolve import find_ad_groups, find_campaign_by_name  # noqa: E402
from _operator_turn import operator_turn  # noqa: E402
from ads.service import attach_freshness, execute_confirmed, read_state  # noqa: E402
from confirm.gate import Proposal  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import require_dev_env  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402


async def run_op(store: ConfirmStore, operation: str, params: dict, summary: str) -> None:
    """Полный путь одной операции: save → (без «да» блок) → confirm → execute."""
    print(f"\n── {operation} ──")
    # Аттестация свежести тем же хелпером, что и карточка бота: демо обязано ПРОХОДИТЬ гейт,
    # а не обходить его — dev-байпаса у freshness нет (Волна 1.1).
    params = attach_freshness(params, await read_state(operation, params))
    p = Proposal(
        operation=operation, summary=summary, params=params, chat_id=0, user_initiated=True
    )
    # Ставки/стратегия = деньги → нужны ОБА бита провенанса (Волна 1.4): `user_initiated`
    # аргументом, `origin_human_turn` — из контекста хода живого оператора.
    with operator_turn():
        await store.save_proposal(
            confirmation_id=p.confirmation_id,
            operation=operation,
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=summary,
            chat_id=0,
            user_initiated=True,
        )
    # 1) Без подтверждения — должно блокироваться (confirm-гейт).
    try:
        await execute_confirmed(store, p.confirmation_id)
        print("  ❌ выполнилось БЕЗ «да» — гейт сломан!")
    except PermissionError:
        print("  ✅ без «да» — заблокировано (confirm-гейт)")

    # 2) Подтверждение + реальное выполнение.
    await store.confirm(p.confirmation_id, chat_id=0)
    try:
        result = await execute_confirmed(store, p.confirmation_id)
        print(f"  ✅ выполнено: {result}")
    except Exception as e:  # ожидаемо для bid при автостратегии — показываем гард
        await store.record_failure(p.confirmation_id, error=str(e))
        print(f"  ⚠️ не выполнено ({type(e).__name__}): {e}")


async def main() -> None:
    require_dev_env()  # golden rule #10: прямая запись (минуя confirm-гейт) только при ENV=dev
    await init_db()
    client = build_client()

    camps = await asyncio.to_thread(list_campaigns, client, DRAFT_ACCOUNT_ID)
    if not camps:
        print("нет кампаний — сначала: python scripts/create_test_campaign.py")
        return
    name = camps[0]["name"]
    print(f"Тест-кампания: '{name}' (аккаунт {DRAFT_ACCOUNT_ID})")

    ad_groups = await asyncio.to_thread(find_ad_groups, client, DRAFT_ACCOUNT_ID, name)
    if ad_groups:
        ag = ad_groups[0]
        print(
            f"Группа объявлений: '{ag.name}' (id={ag.id}); текущая ставка: {ag.cpc_bid_micros / 1e6:.2f}"
        )
    else:
        print("⚠️ у кампании нет групп объявлений — bid/keywords покажут понятный отказ")

    store = ConfirmStore()

    # update_bid: +0.10 валюты аккаунта к текущей ставке группы.
    if ad_groups:
        new_bid = (ad_groups[0].cpc_bid_micros / 1e6) + 0.10
        await run_op(
            store,
            "update_bid",
            {"campaign": name, "mode": "set_to", "value": round(new_bid, 2), "currency": "AUD"},
            f"ставка '{name}': → {new_bid:.2f}",
        )

    # add_keywords: +2 ключа (phrase) в группы кампании.
    await run_op(
        store,
        "add_keywords",
        {
            "campaign": name,
            "keywords": ["aimash тест ключ", "aimash demo keyword"],
            "match_type": "phrase",
        },
        f"+2 ключа в '{name}' (phrase)",
    )

    # add_negative_keywords: +1 минус-слово на уровне кампании.
    await run_op(
        store,
        "add_negative_keywords",
        {"campaign": name, "keywords": ["бесплатно"], "match_type": "broad"},
        f"+1 минус-слово в '{name}'",
    )

    # pause → resume (статус кампании туда-обратно).
    await run_op(store, "pause_campaign", {"campaign": name}, f"пауза '{name}'")
    await run_op(store, "resume_campaign", {"campaign": name}, f"возобновить '{name}'")

    # Чтение результата обратно из API.
    print("\n── читаем обратно из Google Ads ──")
    ref = await asyncio.to_thread(find_campaign_by_name, client, DRAFT_ACCOUNT_ID, name)
    print(f"  статус кампании: {ref.status}")
    ags2 = await asyncio.to_thread(find_ad_groups, client, DRAFT_ACCOUNT_ID, name)
    for a in ags2:
        print(f"  ставка группы '{a.name}': {a.cpc_bid_micros / 1e6:.2f}")
    _print_criteria(client, name)

    # Audit-хвост.
    async with Session() as s:
        rows = (
            (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(12)))
            .scalars()
            .all()
        )
    print("\naudit (последние):")
    for r in reversed(rows):
        print(f"  [{r.status}] {r.operation} cid={r.confirmation_id[:8]} result={r.result}")


def _print_criteria(client, campaign_name: str) -> None:
    """Печать добавленных ключей (ad_group_criterion) и минус-слов (campaign_criterion)."""
    from ads.resolve import _gaql_escape

    ga = client.get_service("GoogleAdsService")
    safe = _gaql_escape(campaign_name)
    kw_q = (
        "SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type "
        "FROM ad_group_criterion "
        f"WHERE campaign.name = '{safe}' AND ad_group_criterion.type = KEYWORD LIMIT 20"
    )
    kws = [
        f"{r.ad_group_criterion.keyword.text} [{r.ad_group_criterion.keyword.match_type.name}]"
        for r in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=kw_q)
    ]
    print(f"  ключи ({len(kws)}): {kws}")
    neg_q = (
        "SELECT campaign_criterion.keyword.text FROM campaign_criterion "
        f"WHERE campaign.name = '{safe}' AND campaign_criterion.type = KEYWORD "
        "AND campaign_criterion.negative = true LIMIT 20"
    )
    negs = [
        r.campaign_criterion.keyword.text
        for r in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=neg_q)
    ]
    print(f"  минус-слова ({len(negs)}): {negs}")


if __name__ == "__main__":
    asyncio.run(main())

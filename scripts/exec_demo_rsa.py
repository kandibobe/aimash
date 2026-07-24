"""Live-демо фазы 2.C: создание RSA-объявления за двойным гейтом на ТЕСТ-аккаунте (7753643025).

Демонстрирует: proposal create_rsa → без «да» execute БЛОКИРУЕТСЯ (confirm-гейт) → confirm →
реальный AdGroupAdService.mutate_ad_group_ads (объявление создаётся СО СТАТУСОМ PAUSED — расход 0,
кампания и так на паузе) → чтение объявления обратно из API → audit-хвост.

Тексты фиксированные (без LLM-затрат). Запуск: python scripts/exec_demo_rsa.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

from sqlalchemy import select  # noqa: E402

from ads.client import DRAFT_ACCOUNT_ID, build_client  # noqa: E402
from ads.read import list_campaigns  # noqa: E402
from ads.resolve import find_ad_groups  # noqa: E402
from ads.service import execute_confirmed  # noqa: E402
from confirm.gate import Proposal  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from _operator_turn import operator_turn  # noqa: E402
from core.config import require_dev_env  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402

# Фиксированные валидные тексты (≤30 / ≤90, кириллица = 1). Длину перепроверит КОД в apply_create_rsa.
_HEADLINES = [
    "Aimash — тест RSA",
    "Доставка за час",
    "Гибкие тарифы",
    "Закажи онлайн",
]
_DESCRIPTIONS = [
    "Демо-объявление Aimash на тест-аккаунте. Создано на паузе — расход нулевой.",
    "Поэлементное подтверждение текстов RSA: в объявление идут только одобренные.",
]
_FINAL_URL = "https://example.com/"


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
    if not ad_groups:
        print("⚠️ у кампании нет групп — сначала: python scripts/create_test_ad_group.py")
        return
    ag = ad_groups[0]
    print(f"Группа объявлений: '{ag.name}' (id={ag.id})")

    store = ConfirmStore()
    params = {
        "ad_group_id": str(ag.id),
        "campaign": name,
        "final_url": _FINAL_URL,
        "headlines": _HEADLINES,
        "descriptions": _DESCRIPTIONS,
    }
    summary = f"create_rsa в '{ag.name}': {len(_HEADLINES)} загол. / {len(_DESCRIPTIONS)} опис."
    p = Proposal(
        operation="create_rsa", summary=summary, params=params, chat_id=0, user_initiated=True
    )
    # RSA деньгами не считается, но провенанс объявляем честно везде, где скрипт создаёт черновик:
    # иначе следующая денежная операция в демо сломается молча (Волна 1.4).
    with operator_turn():
        await store.save_proposal(
            confirmation_id=p.confirmation_id,
            operation="create_rsa",
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=summary,
            chat_id=0,
            user_initiated=True,
        )

    print("\n── create_rsa ──")
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
    except Exception as e:
        await store.record_failure(p.confirmation_id, error=str(e))
        print(f"  ⚠️ не выполнено ({type(e).__name__}): {e}")

    # Чтение объявления обратно из API.
    print("\n── читаем объявление обратно из Google Ads ──")
    _print_rsa(client, ag.id)

    # Audit-хвост.
    async with Session() as s:
        rows = (
            (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(8)))
            .scalars()
            .all()
        )
    print("\naudit (последние):")
    for r in reversed(rows):
        print(f"  [{r.status}] {r.operation} cid={r.confirmation_id[:8]} result={r.result}")


def _print_rsa(client, ad_group_id) -> None:
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.status, ad_group_ad.ad.final_urls "
        f"FROM ad_group_ad WHERE ad_group.id = {int(ad_group_id)} LIMIT 10"
    )
    found = 0
    for row in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=q):
        found += 1
        rsa = row.ad_group_ad.ad.responsive_search_ad
        h = [a.text for a in rsa.headlines]
        d = [a.text for a in rsa.descriptions]
        print(
            f"  объявление: статус={row.ad_group_ad.status.name}, final_urls={list(row.ad_group_ad.ad.final_urls)}"
        )
        print(f"    заголовки ({len(h)}): {h}")
        print(f"    описания ({len(d)}): {d}")
    if not found:
        print("  объявлений в группе не найдено")


if __name__ == "__main__":
    asyncio.run(main())

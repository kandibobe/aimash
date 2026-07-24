"""Live-демо A-geo: радиус-таргетинг (set_geo_proximity) за двойным гейтом на ТЕСТ-аккаунте.

Демонстрирует: proposal set_geo_proximity → без «да» execute БЛОКИРУЕТСЯ (confirm-гейт) → confirm →
реальный CampaignCriterionService.mutate (proximity вокруг города, Google геокодит сам — клиентский
геокодинг не нужен) на PAUSED-кампании (0 расхода) → чтение criterion обратно → audit-хвост.

Адрес структурный (city_name + country_code), как требует SDK. Запуск: python scripts/exec_demo_geo.py
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
from _operator_turn import operator_turn  # noqa: E402
from ads.service import attach_freshness, execute_confirmed, read_state  # noqa: E402
from confirm.gate import Proposal  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import require_dev_env  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402

_CITY = "Киев"
_COUNTRY = "UA"
_RADIUS_KM = 10.0


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

    store = ConfirmStore()
    params = {
        "campaign": name,
        "radius_km": _RADIUS_KM,
        "city_name": _CITY,
        "country_code": _COUNTRY,
    }
    summary = f"set_geo_proximity '{name}': радиус {_RADIUS_KM} км вокруг {_CITY} ({_COUNTRY})"
    # Аттестация свежести тем же хелпером, что и карточка бота (Волна 1.1); dev-байпаса нет.
    params = attach_freshness(params, await read_state("set_geo_proximity", params))
    p = Proposal(
        operation="set_geo_proximity",
        summary=summary,
        params=params,
        chat_id=0,
        user_initiated=True,
    )
    # Гео деньгами не считается, но провенанс объявляем честно везде, где скрипт создаёт черновик:
    # иначе следующая денежная операция в демо сломается молча (Волна 1.4).
    with operator_turn():
        await store.save_proposal(
            confirmation_id=p.confirmation_id,
            operation="set_geo_proximity",
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=summary,
            chat_id=0,
            user_initiated=True,
        )

    print("\n── set_geo_proximity ──")
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

    # Чтение criterion обратно из API.
    print("\n── читаем proximity-criterion обратно из Google Ads ──")
    _print_proximity(client, name)

    # Audit-хвост.
    async with Session() as s:
        rows = (
            (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(6)))
            .scalars()
            .all()
        )
    print("\naudit (последние):")
    for r in reversed(rows):
        print(f"  [{r.status}] {r.operation} cid={r.confirmation_id[:8]} result={r.result}")


def _print_proximity(client, campaign_name) -> None:
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign.name, campaign_criterion.proximity.radius, "
        "campaign_criterion.proximity.radius_units, campaign_criterion.proximity.address.city_name, "
        "campaign_criterion.proximity.address.country_code, campaign_criterion.type "
        "FROM campaign_criterion WHERE campaign_criterion.type = 'PROXIMITY' "
        f"AND campaign.name = '{campaign_name}'"
    )
    found = 0
    for row in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=q):
        found += 1
        prox = row.campaign_criterion.proximity
        print(
            f"  proximity: радиус={prox.radius} {prox.radius_units.name}, "
            f"город={prox.address.city_name}, страна={prox.address.country_code}"
        )
    if not found:
        print("  proximity-criterion не найден")


if __name__ == "__main__":
    asyncio.run(main())

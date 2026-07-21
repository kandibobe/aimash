"""Live-тест Фазы 1: реально сменить бюджет тестовой (PAUSED) кампании по «да».

Поток: init_db → найти тест-кампанию → proposal(update_budget) →
  (без confirm execute должен упасть — гейт) → confirm → execute → прочитать бюджет → audit.
Безопасно: кампания на паузе, аккаунт тестовый. Запуск:  python scripts/exec_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from ads.client import DRAFT_ACCOUNT_ID, build_client  # noqa: E402
from ads.read import list_campaigns  # noqa: E402
from ads.resolve import find_campaign_by_name  # noqa: E402
from ads.service import attach_freshness, execute_confirmed, read_state  # noqa: E402
from confirm.gate import Proposal  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import require_dev_env  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402


async def main() -> None:
    require_dev_env()  # golden rule #10: прямая запись (минуя confirm-гейт) только при ENV=dev
    await init_db()
    client = build_client()

    camps = await asyncio.to_thread(list_campaigns, client, DRAFT_ACCOUNT_ID)
    if not camps:
        print("нет кампаний — сначала: python scripts/create_test_campaign.py")
        return
    name = camps[0]["name"]
    ref = await asyncio.to_thread(find_campaign_by_name, client, DRAFT_ACCOUNT_ID, name)
    print(f"Кампания '{name}' (id={ref.id}); текущий бюджет: {ref.budget_micros / 1e6:.2f}")

    store = ConfirmStore()
    target = (ref.budget_micros // 1_000_000) + 2  # +2 единицы валюты аккаунта — видимая смена
    raw = {"campaign": name, "mode": "set_to", "value": target, "currency": "AUD"}
    # update_budget — STRICT: без аттестации свежести исполнение откажет. Демо проходит гейт тем же
    # хелпером, что и карточка бота; dev-байпаса нет (Волна 1.1).
    raw = attach_freshness(raw, await read_state("update_budget", raw))
    p = Proposal(
        operation="update_budget",
        summary=f"бюджет '{name}': {ref.budget_micros / 1e6:.2f} → {target:.2f}",
        params=raw,
        chat_id=0,
        user_initiated=True,
    )
    await store.save_proposal(
        confirmation_id=p.confirmation_id,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params=p.params,
        summary=p.summary,
        chat_id=0,
        user_initiated=True,
    )

    # 1) Без подтверждения — должно быть запрещено (confirm-гейт).
    try:
        await execute_confirmed(store, p.confirmation_id)
        print("❌ выполнилось БЕЗ «да» — гейт сломан!")
    except PermissionError:
        print("✅ без «да» — заблокировано (confirm-гейт)")

    # 2) Подтверждение + реальное выполнение.
    await store.confirm(p.confirmation_id, chat_id=0)
    result = await execute_confirmed(store, p.confirmation_id)
    print("✅ выполнено:", result)

    ref2 = await asyncio.to_thread(find_campaign_by_name, client, DRAFT_ACCOUNT_ID, name)
    print(f"Новый бюджет (из Google Ads): {ref2.budget_micros / 1e6:.2f}")

    async with Session() as s:
        rows = (
            (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(5)))
            .scalars()
            .all()
        )
    print("audit (последние):")
    for r in rows:
        print(f"  [{r.status}] {r.operation} cid={r.confirmation_id[:8]} result={r.result}")


if __name__ == "__main__":
    asyncio.run(main())

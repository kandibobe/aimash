"""Live-демо §11: создание GDN-кампании из фото за двойным гейтом на ТЕСТ-аккаунте (7753643025).

Прогоняет ПРОДАКШН-путь execute_confirmed: подготовка изображений (Pillow) → временное хранилище
медиа по media_id → proposal create_gdn_campaign → без «да» execute БЛОКИРУЕТСЯ (confirm-гейт) →
confirm → реальная цепочка AssetService→Budget→Campaign(DISPLAY)→AdGroup→ResponsiveDisplayAd, всё
PAUSED (0 расхода) → чтение кампании обратно → audit-хвост.

Фото генерим Pillow (без внешних файлов). Запуск: python scripts/exec_demo_gdn.py
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

from PIL import Image  # noqa: E402
from sqlalchemy import select  # noqa: E402

from ads.assets import prepare_display_images, save_pending_media  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID, build_client  # noqa: E402
from ads.service import execute_confirmed  # noqa: E402
from confirm.gate import Proposal  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from _operator_turn import operator_turn  # noqa: E402
from core.config import require_dev_env  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402


def _demo_photo() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (900, 600), (40, 110, 170)).save(buf, format="JPEG")
    return buf.getvalue()


async def main() -> None:
    require_dev_env()  # golden rule #10: прямая запись (минуя confirm-гейт) только при ENV=dev
    await init_db()
    client = build_client()
    stamp = str(int(time.time()))[-6:]

    # Готовим медиа (как делает бот при приёме фото).
    landscape, square = prepare_display_images(_demo_photo())
    media_id = f"demo{stamp}"
    save_pending_media(media_id, landscape, square)
    print(f"медиа подготовлено: {media_id} (landscape+square)")

    name = f"Aimash GDN Demo {stamp}"
    params = {
        "campaign_name": name,
        "headlines": ["Доставка цветов", "Букеты от 299 грн"],
        "long_headline": "Свежие букеты с доставкой по городу за час — закажи онлайн",
        "descriptions": ["Большой выбор букетов на любой повод. Доставка за час."],
        "business_name": "Aimash",
        "final_url": "https://example.com/",
        "budget_daily_micros": 50_000_000,
        "media_id": media_id,
    }
    summary = f"create_gdn_campaign «{name}» — DISPLAY, PAUSED, бюджет 50/день"
    store = ConfirmStore()
    p = Proposal(
        operation="create_gdn_campaign",
        summary=summary,
        params=params,
        chat_id=0,
        user_initiated=True,
    )
    # Создание кампании = деньги → нужны ОБА бита провенанса (Волна 1.4): `user_initiated`
    # аргументом, `origin_human_turn` — из контекста хода живого оператора.
    with operator_turn():
        await store.save_proposal(
            confirmation_id=p.confirmation_id,
            operation="create_gdn_campaign",
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=summary,
            chat_id=0,
            user_initiated=True,
        )

    print("\n── create_gdn_campaign ──")
    # 1) Без подтверждения — блок (confirm-гейт).
    try:
        await execute_confirmed(store, p.confirmation_id)
        print("  ❌ выполнилось БЕЗ «да» — гейт сломан!")
    except PermissionError:
        print("  ✅ без «да» — заблокировано (confirm-гейт)")

    # 2) Подтверждение + реальное создание.
    await store.confirm(p.confirmation_id, chat_id=0)
    try:
        result = await execute_confirmed(store, p.confirmation_id)
        print(f"  ✅ выполнено: кампания={result.get('campaign')}, статус={result.get('status')}")
        print(f"     ad={result.get('ad')}, assets={result.get('image_assets')}")
    except Exception as e:
        await store.record_failure(p.confirmation_id, error=str(e))
        print(f"  ⚠️ не выполнено ({type(e).__name__}): {e}")

    # Чтение кампании обратно из API.
    print("\n── читаем кампанию обратно из Google Ads ──")
    _print_campaign(client, name)

    # Audit-хвост.
    async with Session() as s:
        rows = (
            (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(4)))
            .scalars()
            .all()
        )
    print("\naudit (последние):")
    for r in reversed(rows):
        print(f"  [{r.status}] {r.operation} cid={r.confirmation_id[:8]}")


def _print_campaign(client, campaign_name) -> None:
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign.name, campaign.status, campaign.advertising_channel_type, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.name = '{campaign_name}'"
    )
    found = 0
    for row in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=q):
        found += 1
        c = row.campaign
        print(
            f"  кампания: '{c.name}', статус={c.status.name}, "
            f"тип={c.advertising_channel_type.name}, бюджет_micros={row.campaign_budget.amount_micros}"
        )
    if not found:
        print("  кампания не найдена")


if __name__ == "__main__":
    asyncio.run(main())

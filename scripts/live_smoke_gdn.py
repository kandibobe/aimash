"""Живой smoke-тест GDN-кампании (§11) через ПОЛНЫЙ confirm-гейт (ACCEPTANCE §18, дыра live-GDN).

Запуск:  python scripts/live_smoke_gdn.py
         python scripts/live_smoke_gdn.py --keep    # не удалять созданное (останется PAUSED)

Что делает (ТОЛЬКО на разрешённом тест-аккаунте Aimash Draft 7753643025, замок в ads.client):
1. Готовит плейсхолдер-изображения (landscape 1200×628 + square 1200×1200) во временном хранилище
   (как визард §11 после приёма фото) и создаёт GDN-кампанию через РЕАЛЬНЫЙ путь продукта:
   save_proposal(user_initiated=True) → confirm → execute_confirmed → apply_create_gdn_campaign
   (двойной гейт: ensure_allowed + одноразовый claim) → finalize + audit. ВСЁ создаётся PAUSED —
   расход $0 до запуска.
2. Перечитывает кампанию из API и сверяет: существует, status=PAUSED, channel_type=DISPLAY.
3. Cleanup: по умолчанию удаляет созданную кампанию прямым SDK-remove — это запись МИМО
   confirm-гейта, поэтому cleanup выполняется ТОЛЬКО при ENV=dev; вне dev (или с --keep)
   кампания остаётся PAUSED ($0) с пометкой «удали вручную» (это НЕ провал).

exit 0 — все шаги ок; exit 1 — любой сбой. НЕ импортирует bot.* (только ads/confirm/db/agent.tools)
— не зависит от UI-слоя (зеркалит scripts/live_smoke_video_dg.py).
"""

from __future__ import annotations

import asyncio
import io
import sys
import uuid
from pathlib import Path

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _operator_turn import operator_turn  # noqa: E402
from ads.assets import clear_pending_media, save_pending_media  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID, build_client, ensure_allowed  # noqa: E402
from ads.service import execute_confirmed  # noqa: E402
from agent.tools.schemas import SCHEMAS  # noqa: E402
from confirm.store import ConfirmStore, list_recent_audit  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402


def _placeholder_jpeg(w: int, h: int, label: str) -> bytes:
    """Плейсхолдер-изображение w×h (JPEG): рамка + подпись. GDN требует marketing images."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), (32, 96, 160))
    d = ImageDraw.Draw(img)
    d.rectangle([w // 12, h // 12, w - w // 12, h - h // 12], outline=(255, 255, 255), width=10)
    d.text((w // 2 - 40, h // 2 - 8), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _find_campaign(client, name: str) -> dict | None:
    """GAQL-сверка созданного: имя/статус/тип канала (перечитываем ИЗ API, не верим result)."""
    from ads.resolve import gaql_escape

    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign.id, campaign.status, campaign.advertising_channel_type "
        f"FROM campaign WHERE campaign.name = '{gaql_escape(name)}' LIMIT 1"
    )
    for row in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=q):
        return {
            "id": str(row.campaign.id),
            "status": row.campaign.status.name,
            "channel": row.campaign.advertising_channel_type.name,
        }
    return None


def _cleanup_campaign(client, campaign_id: str) -> None:
    """Прямой SDK-remove созданной smoke-кампании (МИМО confirm-гейта → только ENV=dev)."""
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.remove = svc.campaign_path(DRAFT_ACCOUNT_ID, campaign_id)
    svc.mutate_campaigns(customer_id=DRAFT_ACCOUNT_ID, operations=[op])


async def _gated_create(store: ConfirmStore, params_in: dict) -> dict:
    """Полный confirm-гейт: схема → proposal(user_initiated) → confirm → execute_confirmed →
    apply_create_gdn_campaign → finalize + audit."""
    params = SCHEMAS["create_gdn_campaign"](**params_in).model_dump()
    cid = uuid.uuid4().hex
    # Создание кампании = деньги → нужны ОБА бита провенанса (Волна 1.4): `user_initiated`
    # аргументом, `origin_human_turn` — из контекста хода живого оператора.
    with operator_turn():
        await store.save_proposal(
            confirmation_id=cid,
            operation="create_gdn_campaign",
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=f"[smoke] GDN «{params_in.get('campaign_name')}»",
            chat_id=0,
            user_initiated=True,
        )
    if not await store.confirm(cid, chat_id=0, actor_username="smoke"):
        raise RuntimeError(f"confirm не прошёл для GDN (cid={cid})")
    result = await execute_confirmed(store, cid)
    print(
        f"   ✅ create_gdn_campaign: applied={result.get('applied')} status={result.get('status')}"
    )
    return result


async def main() -> int:
    keep = "--keep" in sys.argv
    ensure_allowed(DRAFT_ACCOUNT_ID)  # замок аккаунта (golden rule #9) — до любого обращения
    await init_db()
    client = build_client()
    store = ConfirmStore()

    suffix = uuid.uuid4().hex[:6]
    name = f"Aimash Smoke GDN {suffix}"
    media_id = uuid.uuid4().hex
    save_pending_media(
        media_id,
        _placeholder_jpeg(1200, 628, "GDN"),  # landscape 1.91:1
        _placeholder_jpeg(1200, 1200, "GDN"),  # square 1:1
    )
    params = {
        "campaign_name": name,
        "headlines": ["Смоук-тест", "Тестовый GDN", "Не для показа"],
        "long_headline": "Технический смоук-тест SDK-цепочки GDN-кампании",
        "descriptions": ["Смоук-тест SDK-цепочки GDN.", "Кампания PAUSED, $0."],
        "business_name": "Aimash Smoke",
        "final_url": "https://example.com",
        "budget_daily_micros": 10_000_000,  # PAUSED → фактический расход $0 в любом случае
        "media_id": media_id,
        "geo_locations": ["Украина"],
        "geo_country_code": "UA",
        "geo_locale": "ru",
    }

    print(f"=== Live smoke GDN · аккаунт {DRAFT_ACCOUNT_ID} · «{name}» ===")
    ok = True
    try:
        await _gated_create(store, params)
        found = _find_campaign(client, name)
        if found is None:
            print("   ❌ кампания НЕ найдена в API после создания")
            ok = False
        else:
            ok_status = found["status"] == "PAUSED"
            ok_channel = found["channel"] == "DISPLAY"
            print(
                f"   перечитано из API: id={found['id']} status={found['status']} "
                f"{'✅' if ok_status else '❌ ожидался PAUSED'} · channel={found['channel']} "
                f"{'✅' if ok_channel else '❌ ожидался DISPLAY'}"
            )
            ok = ok_status and ok_channel
            if keep or settings.env != "dev":
                print(f"   ℹ️ кампания оставлена PAUSED ($0) — удали вручную (id={found['id']}).")
            else:
                try:
                    _cleanup_campaign(client, found["id"])
                    print(
                        "   🧹 cleanup: кампания удалена (REMOVED, прямой SDK-remove при ENV=dev)."
                    )
                except Exception as e:  # noqa: BLE001 — сирота PAUSED безвредна; смоук не валим
                    print(f"   ⚠️ cleanup не удался ({type(e).__name__}) — осталась PAUSED.")
    finally:
        clear_pending_media(media_id)  # чистим временные кадры (как визард на завершении)

    print("\n=== Журнал (audit_log, последние записи) ===")
    for ev in await list_recent_audit(6):
        when = ev.created_at.strftime("%H:%M:%S") if ev.created_at else "—"
        print(f"  {ev.status:9} {ev.operation:26} {when}  cid={ev.confirmation_id[:8]}")

    print(
        "\nГотово ✅ — SDK-цепочка GDN сверена live (§11, ACCEPTANCE §18)."
        if ok
        else "\n❌ Смоук GDN провален — см. вывод выше."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

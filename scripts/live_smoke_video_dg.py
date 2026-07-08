"""Живой smoke-тест Video/Demand Gen кампаний через ПОЛНЫЙ confirm-гейт (§18#5, ACCEPTANCE ⚠️).

Запуск:  python scripts/live_smoke_video_dg.py --video-id <YouTube id|url>
         python scripts/live_smoke_video_dg.py --dg-only | --video-only
         python scripts/live_smoke_video_dg.py --keep      # не удалять созданное (останется PAUSED)

Что делает (ТОЛЬКО на разрешённом тест-аккаунте Aimash Draft 7753643025, замок в ads.client):
1. Создаёт Demand Gen и/или Video кампанию из YouTube-видео через РЕАЛЬНЫЙ путь продукта:
   save_proposal(user_initiated=True) → confirm → execute_confirmed → apply_create_*_campaign
   (двойной гейт: ensure_allowed + одноразовый claim) → finalize + audit. ВСЁ создаётся PAUSED —
   расход $0 до запуска.
2. Перечитывает кампанию из API и сверяет: существует, status=PAUSED, правильный channel_type.
3. Cleanup: по умолчанию удаляет созданную кампанию прямым SDK-remove — это запись МИМО
   confirm-гейта, поэтому cleanup выполняется ТОЛЬКО при ENV=dev; вне dev (или с --keep)
   кампания остаётся PAUSED ($0) с пометкой «удали вручную» (это НЕ провал).

video-id: своё/доступное видео (env SMOKE_YT_VIDEO_ID или --video-id) — чужое публичное может
быть отклонено политиками Google. exit 0 — все шаги ок; exit 1 — любой сбой.
НЕ импортирует bot.* (только ads/confirm/db/agent.tools) — не зависит от UI-слоя.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.assets import parse_youtube_video_id, save_pending_media  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID, build_client, ensure_allowed  # noqa: E402
from ads.service import execute_confirmed  # noqa: E402
from agent.tools.schemas import SCHEMAS  # noqa: E402
from confirm.store import ConfirmStore, list_recent_audit  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402


def _arg(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def _find_campaign(client, name: str) -> dict | None:
    """GAQL-сверка созданного: имя/статус/тип канала (перечитываем ИЗ API, не верим result)."""
    ga = client.get_service("GoogleAdsService")
    from ads.resolve import gaql_escape

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


def _placeholder_logo_png() -> bytes:
    """Плейсхолдер-логотип 1200×1200 (JPEG) для DG-смоука: live API требует logo_images."""
    import io as _io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 1200), (32, 96, 160))
    d = ImageDraw.Draw(img)
    d.rectangle([200, 200, 1000, 1000], outline=(255, 255, 255), width=24)
    d.text((420, 560), "SMOKE", fill=(255, 255, 255))
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _cleanup_campaign(client, campaign_id: str) -> None:
    """Прямой SDK-remove созданной smoke-кампании (МИМО confirm-гейта → только ENV=dev)."""
    svc = client.get_service("CampaignService")
    op = client.get_type("CampaignOperation")
    op.remove = svc.campaign_path(DRAFT_ACCOUNT_ID, campaign_id)
    svc.mutate_campaigns(customer_id=DRAFT_ACCOUNT_ID, operations=[op])


async def _gated_create(store: ConfirmStore, operation: str, params_in: dict) -> dict:
    """Полный confirm-гейт (как _gated_op базового смоука): схема → proposal(user_initiated) →
    confirm → execute_confirmed → apply_create_*_campaign → finalize + audit."""
    params = SCHEMAS[operation](**params_in).model_dump()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=DRAFT_ACCOUNT_ID,
        params=params,
        summary=f"[smoke] {operation} «{params_in.get('campaign_name')}»",
        chat_id=0,
        user_initiated=True,  # создание кампании = деньги → требуется прямая команда
    )
    if not await store.confirm(cid, chat_id=0, actor_username="smoke"):
        raise RuntimeError(f"confirm не прошёл для {operation} (cid={cid})")
    result = await execute_confirmed(store, cid)
    print(f"   ✅ {operation}: applied={result.get('applied')} status={result.get('status')}")
    return result


async def _smoke_one(client, store: ConfirmStore, kind: str, video_id: str, keep: bool) -> bool:
    """Один прогон (dg|video): создать PAUSED → сверить из API → cleanup (dev). True = ок."""
    suffix = uuid.uuid4().hex[:6]
    name = f"Aimash Smoke {'DG' if kind == 'dg' else 'Video'} {suffix}"
    expected_channel = "DEMAND_GEN" if kind == "dg" else "VIDEO"
    common = {
        "campaign_name": name,
        "youtube_video_id": video_id,
        "headlines": ["Смоук-тест", "Тестовое видео", "Не для показа"],
        "long_headline": "Технический смоук-тест SDK-цепочки кампании из видео",
        "descriptions": ["Смоук-тест SDK-цепочки.", "Кампания PAUSED, $0."],
        "business_name": "Aimash Smoke",
        "final_url": "https://example.com",
        # Живой минимум Demand Gen на AUD-аккаунте = 8 единиц/день (BUDGET TOO_LOW при меньшем,
        # сверено live 2026-07-03). Кампания PAUSED — фактический расход $0 в любом случае.
        "budget_daily_micros": 10_000_000,
    }
    logo_mid = ""
    if kind == "dg":
        operation = "create_demand_gen_campaign"
        # Live-сверка 2026-07-03: DG-объявление ТРЕБУЕТ ≥1 logo_images (TOO_FEW без него) —
        # генерируем плейсхолдер-квадрат 1200×1200 и кладём во временное хранилище (как визард).
        logo_mid = uuid.uuid4().hex
        logo_png = _placeholder_logo_png()
        save_pending_media(logo_mid, logo_png, logo_png)  # (landscape, square) — DG берёт square
        params = {**common, "goal": "clicks", "logo_media_id": logo_mid}
    else:
        operation = "create_video_campaign"
        params = dict(common)

    print(f"\n=== {expected_channel}: «{name}» ===")
    await _gated_create(store, operation, params)

    found = _find_campaign(client, name)
    if found is None:
        print("   ❌ кампания НЕ найдена в API после создания")
        return False
    ok_status = found["status"] == "PAUSED"
    ok_channel = found["channel"] == expected_channel
    print(
        f"   перечитано из API: id={found['id']} status={found['status']} "
        f"{'✅' if ok_status else '❌ ожидался PAUSED'} · channel={found['channel']} "
        f"{'✅' if ok_channel else f'❌ ожидался {expected_channel}'}"
    )

    if keep or settings.env != "dev":
        print(
            f"   ℹ️ кампания оставлена PAUSED ($0) — удали вручную при желании (id={found['id']})."
        )
    else:
        try:
            _cleanup_campaign(client, found["id"])
            print("   🧹 cleanup: кампания удалена (REMOVED, прямой SDK-remove при ENV=dev).")
        except Exception as e:  # noqa: BLE001 — сирота PAUSED безвредна; смоук не проваливаем
            print(f"   ⚠️ cleanup не удался ({type(e).__name__}) — осталась PAUSED, удали вручную.")
    return ok_status and ok_channel


async def main() -> int:
    keep = "--keep" in sys.argv
    dg_only = "--dg-only" in sys.argv
    video_only = "--video-only" in sys.argv
    raw_vid = _arg("--video-id") or os.environ.get("SMOKE_YT_VIDEO_ID") or ""
    video_id = parse_youtube_video_id(raw_vid)
    if not video_id:
        print(
            "❌ Нужен YouTube-видео id: --video-id <id|url> или env SMOKE_YT_VIDEO_ID.\n"
            "   Используй СВОЁ/доступное видео — чужое может быть отклонено политиками."
        )
        return 1

    ensure_allowed(DRAFT_ACCOUNT_ID)  # замок аккаунта (golden rule #9) — до любого обращения
    await init_db()
    client = build_client()
    store = ConfirmStore()
    print(f"=== Live smoke Video/Demand Gen · аккаунт {DRAFT_ACCOUNT_ID} · видео {video_id} ===")

    ok = True
    if not video_only:
        ok = await _smoke_one(client, store, "dg", video_id, keep) and ok
    if not dg_only:
        ok = await _smoke_one(client, store, "video", video_id, keep) and ok

    print("\n=== Журнал (audit_log, последние записи) ===")
    for ev in await list_recent_audit(6):
        when = ev.created_at.strftime("%H:%M:%S") if ev.created_at else "—"
        print(f"  {ev.status:9} {ev.operation:26} {when}  cid={ev.confirmation_id[:8]}")

    print(
        "\nГотово ✅ — SDK-цепочки Video/Demand Gen сверены live (ACCEPTANCE §18#5)."
        if ok
        else "\n❌ Смоук провален — см. вывод выше (ACCEPTANCE §18#5 остаётся ⚠️)."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

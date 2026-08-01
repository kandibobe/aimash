"""Live smoke App campaign through the production confirm/audit path.

Run only against the allowlisted Aimash Draft account:
    python scripts/live_smoke_app.py --app-id com.example.real
    python scripts/live_smoke_app.py --app-id 123456789 --store apple_app_store --keep

The campaign, ad group and ad are created PAUSED. In ENV=dev the campaign is removed after readback;
otherwise it stays PAUSED and the script prints its id. This script is intentionally not part of the
offline test suite because it performs a real Google Ads mutation.
"""

from __future__ import annotations

import asyncio
import io
import sys
import uuid
from pathlib import Path

from _win_console import enable_utf8

enable_utf8()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _operator_turn import operator_turn  # noqa: E402
from ads.assets import clear_pending_media, save_pending_media  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID, build_client, ensure_allowed  # noqa: E402
from ads.service import execute_confirmed  # noqa: E402
from agent.tools.schemas import SCHEMAS  # noqa: E402
from confirm.store import ConfirmStore, list_recent_audit  # noqa: E402
from core.config import require_dev_env, settings  # noqa: E402
from db.session import init_db  # noqa: E402


def _arg(flag: str) -> str | None:
    if flag not in sys.argv:
        return None
    index = sys.argv.index(flag)
    return sys.argv[index + 1] if index + 1 < len(sys.argv) else None


def _placeholder_jpeg(width: int, height: int) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (32, 96, 160))
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [width // 10, height // 10, width - width // 10, height - height // 10],
        outline=(255, 255, 255),
        width=10,
    )
    draw.text((width // 2 - 50, height // 2 - 8), "APP SMOKE", fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _find_campaign(client, name: str) -> dict | None:
    from ads.resolve import gaql_escape

    query = (
        "SELECT campaign.id, campaign.status, campaign.advertising_channel_type, "
        "campaign.advertising_channel_sub_type FROM campaign "
        f"WHERE campaign.name = '{gaql_escape(name)}' LIMIT 1"
    )
    service = client.get_service("GoogleAdsService")
    for row in service.search(customer_id=DRAFT_ACCOUNT_ID, query=query):
        return {
            "id": str(row.campaign.id),
            "status": row.campaign.status.name,
            "channel": row.campaign.advertising_channel_type.name,
            "sub_type": row.campaign.advertising_channel_sub_type.name,
        }
    return None


def _cleanup_campaign(client, campaign_id: str) -> None:
    require_dev_env()  # direct SDK cleanup is allowed only when ENV=dev was set explicitly
    service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    operation.remove = service.campaign_path(DRAFT_ACCOUNT_ID, campaign_id)
    service.mutate_campaigns(customer_id=DRAFT_ACCOUNT_ID, operations=[operation])


async def _gated_create(store: ConfirmStore, params_in: dict) -> dict:
    params = SCHEMAS["create_app_campaign"](**params_in).model_dump()
    confirmation_id = uuid.uuid4().hex
    with operator_turn():
        await store.save_proposal(
            confirmation_id=confirmation_id,
            operation="create_app_campaign",
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=f"[smoke] App campaign «{params['campaign_name']}»",
            chat_id=0,
            user_initiated=True,
        )
    if not await store.confirm(confirmation_id, chat_id=0, actor_username="smoke"):
        raise RuntimeError(f"confirm failed for App campaign (cid={confirmation_id})")
    result = await execute_confirmed(store, confirmation_id)
    print(
        f"   ✅ create_app_campaign: applied={result.get('applied')} status={result.get('status')}"
    )
    return result


async def main() -> int:
    app_id = (_arg("--app-id") or "").strip()
    app_store = (_arg("--store") or "google_play").strip()
    keep = "--keep" in sys.argv
    if not app_id:
        print("❌ --app-id is required; use a real Google Play package id or Apple numeric app id.")
        return 2

    ensure_allowed(DRAFT_ACCOUNT_ID)
    await init_db()
    client = build_client(DRAFT_ACCOUNT_ID)
    store = ConfirmStore()
    name = f"Aimash Smoke App {uuid.uuid4().hex[:6]}"
    media_id = uuid.uuid4().hex
    save_pending_media(
        media_id,
        _placeholder_jpeg(1200, 628),
        _placeholder_jpeg(1200, 1200),
    )
    params = {
        "campaign_name": name,
        "app_id": app_id,
        "app_store": app_store,
        "headlines": ["Install the app", "App campaign smoke"],
        "descriptions": ["Aimash Google Ads API smoke test.", "Created PAUSED for verification."],
        "budget_daily_micros": 10_000_000,
        "target_cpa_micros": 1_000_000,
        "image_media_ids": [media_id],
    }

    print(f"=== Live smoke App · account {DRAFT_ACCOUNT_ID} · «{name}» ===")
    ok = True
    try:
        await _gated_create(store, params)
        found = _find_campaign(client, name)
        if found is None:
            print("   ❌ campaign not found in API after creation")
            ok = False
        else:
            ok = (
                found["status"] == "PAUSED"
                and found["channel"] == "MULTI_CHANNEL"
                and found["sub_type"] == "APP_CAMPAIGN"
            )
            print(
                "   readback: "
                f"id={found['id']} status={found['status']} channel={found['channel']} "
                f"sub_type={found['sub_type']} {'✅' if ok else '❌'}"
            )
            if keep or settings.env != "dev":
                print(f"   ℹ️ left PAUSED ($0); remove manually if needed (id={found['id']}).")
            else:
                try:
                    _cleanup_campaign(client, found["id"])
                    print("   🧹 cleanup: campaign removed (direct SDK remove, ENV=dev only).")
                except Exception as error:  # noqa: BLE001
                    print(f"   ⚠️ cleanup failed ({type(error).__name__}); campaign remains PAUSED.")
    finally:
        clear_pending_media(media_id)

    print("\n=== Recent audit_log ===")
    for event in await list_recent_audit(6):
        when = event.created_at.strftime("%H:%M:%S") if event.created_at else "—"
        print(f"  {event.status:9} {event.operation:26} {when}  cid={event.confirmation_id[:8]}")
    print("\n✅ App campaign live smoke passed." if ok else "\n❌ App campaign live smoke failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Живой smoke-тест лид-формы (§19.7.1) через ПОЛНЫЙ confirm-гейт — для сверки на реальном аккаунте.

Запуск (ТОЛЬКО на разрешённом тест-аккаунте Aimash Draft 7753643025, замок в ads.client):
    python scripts/live_smoke_lead_form.py --privacy-url https://example.com/privacy
    python scripts/live_smoke_lead_form.py --privacy-url ... --keep   # не удалять созданное

Что делает:
1. Создаёт минимальную Search-кампанию (PAUSED, $0) С lead_form-ассетом через РЕАЛЬНЫЙ путь продукта:
   save_proposal(user_initiated=True) → confirm → execute_confirmed → apply_create_search_campaign →
   _attach_asset_specs_via_sdk → _add_lead_form_via_sdk (создаёт lead_form_asset → линк LEAD_FORM).
2. Перечитывает из API: (а) кампания PAUSED; (б) есть CampaignAsset с field_type=LEAD_FORM.
3. Печатает result.assets_added / assets_skipped: если аккаунт НЕ принял Lead Form ToS, ассет
   попадёт в assets_skipped (это НЕ провал скрипта — кампания создаётся, ассет пропущен; см. вывод).
4. Cleanup: удаляет кампанию прямым SDK-remove (запись МИМО confirm-гейта) — ТОЛЬКО при ENV=dev
   или без --keep; иначе кампания остаётся PAUSED ($0) с пометкой «удали вручную».

privacy-url — валидный URL политики конфиденциальности (обязателен для лид-форм Google).
exit 0 — кампания создана и сверена (ассет добавлен ЛИБО осознанно пропущен по ToS); exit 1 — сбой.
НЕ импортирует bot.* (только ads/confirm/db) — не зависит от UI-слоя.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

# Windows-консоль (cp1251) роняет emoji/кириллицу в выводе → UTF-8 (общий хелпер).
from _win_console import enable_utf8  # noqa: E402

enable_utf8()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _operator_turn import operator_turn  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID, build_client, ensure_allowed  # noqa: E402
from ads.service import execute_confirmed  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402


def _arg(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def _campaign_status(client, name: str) -> dict | None:
    from ads.resolve import gaql_escape

    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign.id, campaign.status FROM campaign "
        f"WHERE campaign.name = '{gaql_escape(name)}' LIMIT 1"
    )
    for row in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=q):
        return {"id": str(row.campaign.id), "status": row.campaign.status.name}
    return None


def _has_lead_form_link(client, campaign_id: str) -> bool:
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign_asset.field_type FROM campaign_asset "
        f"WHERE campaign.id = {int(campaign_id)} AND campaign_asset.field_type = 'LEAD_FORM'"
    )
    return any(True for _ in ga.search(customer_id=DRAFT_ACCOUNT_ID, query=q))


async def main() -> int:
    privacy_url = _arg("--privacy-url") or "https://example.com/privacy"
    keep = "--keep" in sys.argv
    await init_db()
    # Замок: Draft обязан быть в мутационном allow-list (иначе ensure_allowed откажет).
    ensure_allowed(DRAFT_ACCOUNT_ID)
    client = build_client(DRAFT_ACCOUNT_ID)
    store = ConfirmStore()

    name = f"SMOKE LeadForm {uuid.uuid4().hex[:8]}"
    params = {
        "campaign_name": name,
        "final_url": "https://example.com/",
        "headlines": ["Заголовок раз", "Заголовок два", "Заголовок три"],
        "descriptions": ["Описание первое.", "Описание второе."],
        "budget_daily_micros": 10_000_000,
        "asset_specs": [
            {
                "family": "lead_form",
                "params": {
                    "business_name": "Smoke Co",
                    "headline": "Оставьте заявку",
                    "description": "Свяжемся с вами в ближайшее время.",
                    "call_to_action_description": "Заявка",
                    "privacy_policy_url": privacy_url,
                    "cta_type": "GET_QUOTE",
                    "field_types": ["FULL_NAME", "EMAIL", "PHONE_NUMBER"],
                },
            }
        ],
    }

    cid = uuid.uuid4().hex
    # Деньги/создание → нужны ОБА бита провенанса (Волна 1.4): `user_initiated` аргументом,
    # `origin_human_turn` — из контекста хода живого оператора.
    with operator_turn():
        await store.save_proposal(
            confirmation_id=cid,
            operation="create_search_campaign",
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=name,
            chat_id=1,
            user_initiated=True,
        )
    assert await store.confirm(cid, chat_id=1), "confirm не прошёл"
    print(f"→ создаю Search-кампанию с лид-формой: {name}")
    result = await execute_confirmed(store, cid)

    added = result.get("assets_added") or []
    skipped = result.get("assets_skipped") or []
    print(f"  assets_added={added}  assets_skipped={skipped}")

    status = _campaign_status(client, name)
    if not status or status["status"] != "PAUSED":
        print(f"✗ кампания не найдена/не PAUSED: {status}")
        return 1
    print(f"  кампания создана: id={status['id']} status={status['status']}")

    if "lead_form" in added:
        if _has_lead_form_link(client, status["id"]):
            print("✓ LEAD_FORM-ассет создан и привязан к кампании — СВЕРЕНО LIVE")
        else:
            print("✗ assets_added содержит lead_form, но CampaignAsset LEAD_FORM не найден в API")
            return 1
    elif any(s.get("family") == "lead_form" for s in skipped):
        print(
            "⚠️ lead_form ПРОПУЩЕН (assets_skipped) — вероятно, аккаунт не принял Lead Form ToS в "
            "Google Ads. Это НЕ дефект кода: код собрал ассет, API отклонил из-за настройки аккаунта. "
            "Прими условия Lead Form в интерфейсе Google Ads и повтори."
        )
    else:
        print("✗ lead_form не в added и не в skipped — неожиданно")
        return 1

    # Cleanup (dev-only): удаление — прямая запись мимо confirm-гейта.
    if not keep and settings.env == "dev":
        from ads.mutations import _remove_campaign_via_sdk

        _remove_campaign_via_sdk(client, DRAFT_ACCOUNT_ID, status["id"])
        print("  cleanup: кампания удалена (ENV=dev)")
    else:
        print(f"  cleanup пропущен — кампания {status['id']} осталась PAUSED ($0), удали вручную")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

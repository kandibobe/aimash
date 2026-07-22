"""Живой smoke-тест Google Ads через ПОЛНЫЙ confirm-гейт (read + обратимая мутация).

Запуск:  python scripts/live_smoke_test.py            # read + обратимый pause↔resume round-trip
         python scripts/live_smoke_test.py --read-only # только чтение, без мутаций

Что делает (ТОЛЬКО на разрешённом тест-аккаунте Aimash Draft 7753643025, замок в ads.client):
1. ensure_allowed + чтение: статистика за 30 дн., валюта, список кампаний.
2. Обратимый round-trip ОДНОЙ кампании через РЕАЛЬНЫЙ путь продукта:
   save_proposal(user_initiated=True) → confirm → execute_confirmed → apply_* (двойной гейт) →
   finalize + audit. Кампанию возвращаем в ИСХОДНЫЙ статус (ENABLED→pause→resume или
   PAUSED→resume→pause). 0 чистого изменения. Сверяем статус ДО/ПОСЛЕ перечитыванием из API.

НЕ импортирует bot.* (только ads/confirm/db) — не зависит от UI-слоя и его правок.
Деньги не трогаются (pause/resume — не бюджет/ставка). Всё за тем же гейтом, что прод.
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
from ads.read import account_currency, account_stats, list_campaigns  # noqa: E402
from ads.service import attach_freshness, execute_confirmed, read_state  # noqa: E402
from agent.tools.schemas import SCHEMAS  # noqa: E402
from confirm.store import ConfirmStore, list_recent_audit  # noqa: E402
from db.session import init_db  # noqa: E402


def _campaign_status(client, name: str) -> str | None:
    """Перечитать актуальный статус кампании по имени из API (для сверки ДО/ПОСЛЕ)."""
    for c in list_campaigns(client, DRAFT_ACCOUNT_ID):
        if c["name"] == name:
            return c["status"]
    return None


async def _gated_op(store: ConfirmStore, operation: str, campaign: str) -> dict:
    """Один проход полного confirm-гейта (как бот на «да»): валидация схемой → proposal
    (user_initiated) → confirm → execute_confirmed → apply_* → finalize + audit."""
    params = SCHEMAS[operation](campaign=campaign).model_dump()
    # Снимок + аттестация свежести ровно тем же хелпером, что и боевой путь карточки: смоук обязан
    # проходить freshness-гейт, а не обходить его (dev-байпаса нет — Волна 1.1).
    params = attach_freshness(params, await read_state(operation, params))
    cid = uuid.uuid4().hex
    # Оба бита провенанса, как у боевой карточки: `user_initiated` аргументом, `origin_human_turn` —
    # из контекста хода (Волна 1.4). Смоук запустил живой оператор, и это единственное место, где
    # скрипт вправе так сказать: см. scripts/_operator_turn.py.
    with operator_turn():
        await store.save_proposal(
            confirmation_id=cid,
            operation=operation,
            customer_id=DRAFT_ACCOUNT_ID,
            params=params,
            summary=f"[smoke] {operation} «{campaign}»",
            chat_id=0,
            user_initiated=True,
        )
    ok = await store.confirm(cid, chat_id=0, actor_username="smoke")
    if not ok:
        raise RuntimeError(f"confirm не прошёл для {operation} (cid={cid})")
    result = await execute_confirmed(store, cid)
    print(f"   ✅ {operation}: applied={result.get('applied')} status={result.get('status')}")
    return result


async def main() -> None:
    read_only = "--read-only" in sys.argv
    ensure_allowed(DRAFT_ACCOUNT_ID)  # замок аккаунта (golden rule #9) — до любого обращения
    await init_db()
    client = build_client()

    print(f"=== Live smoke-тест · аккаунт {DRAFT_ACCOUNT_ID} (Aimash Draft) ===\n")

    # 1) READ: статистика + валюта + кампании.
    cur = account_currency(client, DRAFT_ACCOUNT_ID)
    st = account_stats(client, DRAFT_ACCOUNT_ID, 30)
    print(f"Валюта аккаунта: {cur or '—'}")
    print(
        f"Статистика 30 дн.: показы={st.impressions} клики={st.clicks} "
        f"расход={st.cost:.2f} {cur} конв.={st.conversions:g}"
    )
    camps = list_campaigns(client, DRAFT_ACCOUNT_ID)
    print(f"\nКампаний: {len(camps)}")
    for c in camps:
        print(f"  • [{c['status']}] {c['name']} (id={c['id']})")

    if read_only:
        print("\n--read-only: мутации пропущены. Чтение OK ✅")
        return
    if not camps:
        print("\n⚠️ Кампаний нет — обратимый round-trip пропущен (чтение OK ✅).")
        return

    # 2) Выбор цели round-trip. Предпочитаем ENABLED (pause→resume = 0 риска расхода). Если таких
    #    нет — берём PAUSED (resume→pause; кратко включит кампанию, поэтому громко предупреждаем).
    target = next((c for c in camps if c["status"] == "ENABLED"), None)
    if target is None:
        target = next((c for c in camps if c["status"] == "PAUSED"), None)
    if target is None:
        print(
            "\n⚠️ Нет ENABLED/PAUSED кампании для обратимого round-trip (только REMOVED). Чтение OK ✅."
        )
        return

    name, original = target["name"], target["status"]
    if original == "ENABLED":
        first, second = "pause_campaign", "resume_campaign"
    else:  # PAUSED
        first, second = "resume_campaign", "pause_campaign"
        print(
            "\n⚠️ Нет включённой кампании — round-trip на PAUSED: на доли секунды кампания будет "
            "ВКЛЮЧЕНА и сразу возвращена в PAUSED."
        )

    print(f"\n=== Обратимый round-trip: «{name}» (исходно {original}) ===")
    store = ConfirmStore()
    expect_after_first = "PAUSED" if first == "pause_campaign" else "ENABLED"

    try:
        print(f"1) {first} (ожидаем → {expect_after_first})")
        await _gated_op(store, first, name)
        got = _campaign_status(client, name)
        print(
            f"   перечитано из API: {got}  {'✅' if got == expect_after_first else '❌ НЕ СОВПАЛО'}"
        )
    finally:
        # ВСЕГДА возвращаем исходный статус (даже если сверка выше не совпала / упало после мутации).
        print(f"2) {second} (восстанавливаем → {original})")
        try:
            await _gated_op(store, second, name)
            got = _campaign_status(client, name)
            mark = "✅" if got == original else "❌ НЕ ВОССТАНОВЛЕНО — проверь вручную!"
            print(f"   перечитано из API: {got}  {mark}")
        except Exception as e:  # noqa: BLE001 — восстановление обязано попытаться; громко сообщаем
            print(
                f"   ❌ ВОССТАНОВЛЕНИЕ УПАЛО ({type(e).__name__}: {e}) — кампания «{name}» может "
                f"остаться в {expect_after_first}! Верни вручную в {original}."
            )
            raise

    # 3) Журнал: подтверждаем, что обе операции записаны в audit_log (что/когда/кто/результат).
    print("\n=== Журнал (audit_log, последние записи) ===")
    for ev in await list_recent_audit(6):
        when = ev.created_at.strftime("%H:%M:%S") if ev.created_at else "—"
        print(f"  {ev.status:9} {ev.operation:16} {when}  cid={ev.confirmation_id[:8]}")

    print("\nГотово ✅ — read + обратимая мутация прошли полный confirm-гейт, статус восстановлен.")


if __name__ == "__main__":
    asyncio.run(main())

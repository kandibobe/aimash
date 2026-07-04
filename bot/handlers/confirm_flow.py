"""Confirm-гейт: ✅/❌ (ConfirmCB + легаси ok:/no:) → bm._do_confirm/_do_cancel

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.callback_query(bm.ConfirmCB.filter(bm.F.action == "ok"))
async def on_confirm(cq: bm.CallbackQuery, callback_data: bm.ConfirmCB) -> None:
    await bm._do_confirm(cq, callback_data.cid)


@bm.dp.callback_query(bm.ConfirmCB.filter(bm.F.action == "del1"))
async def on_confirm_stage1(cq: bm.CallbackQuery, callback_data: bm.ConfirmCB) -> None:
    """P1-6: первый шаг двойного подтверждения удаления (необратимо) — показать финальную кнопку."""
    await bm._do_confirm_stage1(cq, callback_data.cid)


@bm.dp.callback_query(bm.ConfirmCB.filter(bm.F.action == "no"))
async def on_cancel(cq: bm.CallbackQuery, callback_data: bm.ConfirmCB) -> None:
    await bm._do_cancel(cq, callback_data.cid)


# Legacy-fallback: старые сообщения с "ok:/no:" (до рестарта). После переходного периода удалить.
@bm.dp.callback_query(bm.F.data.startswith("ok:"))
async def on_confirm_legacy(cq: bm.CallbackQuery) -> None:
    await bm._do_confirm(cq, (cq.data or "")[3:])


@bm.dp.callback_query(bm.F.data.startswith("no:"))
async def on_cancel_legacy(cq: bm.CallbackQuery) -> None:
    await bm._do_cancel(cq, (cq.data or "")[3:])

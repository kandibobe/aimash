"""Агент-цикл: русская команда → правильный tool-call → результат.

Логика безопасности:
- read-инструмент (get_stats) → выполняется (здесь — заглушка; в фазе 0 пойдёт в ads/read.py);
- mutation-инструмент → НЕ выполняется: валидируем аргументы (Pydantic) и создаём Proposal
  (сводка «было→станет» + confirmation_id). Выполнение — только после «да» (confirm-гейт);
- ask_clarification → возвращаем вопрос пользователю (не угадываем).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import ValidationError

from agent.router import chat
from agent.tools.schemas import MUTATION_TOOLS, READ_TOOLS, SCHEMAS, TOOLS
from confirm.gate import Proposal, build_summary

SYSTEM = (
    "Ты — исполнитель команд для Google Ads (агент Aimash). По команде пользователя вызови "
    "ПОДХОДЯЩУЮ функцию с точными аргументами. Различай 'на N%' (изменить НА процент) и "
    "'до N' (установить В значение). Если не указана кампания, сумма или направление — вызови "
    "ask_clarification, НЕ угадывай. Ничего не выполняй сам — только предложи вызов функции. "
    "Деньги/ставки не трогаются без явного подтверждения пользователя."
)


async def handle_command(text: str, *, chat_id: int = 0) -> dict[str, Any]:
    """Возвращает структуру результата: clarify | read | proposal | text.

    proposal НЕ выполнен — это черновик для показа и подтверждения «да».
    """
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]
    msg = await chat(messages, role="parsing", tools=TOOLS)

    # Надёжность: если модель не вызвала инструмент и не дала текст — одна повторная попытка.
    if not getattr(msg, "tool_calls", None) and not (msg.content or "").strip():
        messages.append({
            "role": "user",
            "content": "Вызови подходящую функцию для этой команды; если данных не хватает — ask_clarification.",
        })
        msg = await chat(messages, role="parsing", tools=TOOLS)

    if not getattr(msg, "tool_calls", None):
        return {"type": "text", "text": (msg.content or "").strip() or "Не удалось распознать команду — переформулируй."}

    call = msg.tool_calls[0]
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return {"type": "text", "text": "не удалось разобрать аргументы инструмента"}

    if name == "ask_clarification":
        return {"type": "clarify", "question": args.get("question", "Уточните, пожалуйста, команду.")}

    if name in READ_TOOLS:
        return await _do_read(name, args)

    if name in MUTATION_TOOLS:
        # Валидация диапазонов В КОДЕ (не доверяем модели)
        try:
            validated = SCHEMAS[name](**args)
        except ValidationError as e:
            return {"type": "text", "text": f"некорректные аргументы для {name}: {e.errors()}"}

        # Черновик: «было» возьмётся из Google Ads в Фазе 1; сейчас плейсхолдер.
        after = validated.model_dump()
        summary = build_summary(name, before="[текущее значение из Google Ads]", after=after)
        proposal = Proposal(
            operation=name,
            summary=summary,
            params=after,
            chat_id=chat_id,
            user_initiated=True,
        )
        return {
            "type": "proposal",
            "operation": name,
            "summary": proposal.summary,
            "confirmation_id": proposal.confirmation_id,
            "confirm_prompt": "Показать в Telegram с кнопками ✅ Подтвердить / ❌ Отмена. "
            "Выполнить только после «да».",
        }

    return {"type": "text", "text": f"неизвестный инструмент: {name}"}


async def _do_read(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Живое чтение Google Ads (read-only). google-ads SDK синхронный → через to_thread.

    Пока белый список = 1 тестовый аккаунт, читаем его. Когда список расширится —
    добавить резолв 'account' → customer_id из allowed.
    """
    from core.config import settings

    allowed = sorted(settings.allowed_customer_ids)
    if not allowed:
        return {"type": "text", "text": "нет разрешённых аккаунтов (allowed_customer_ids пуст)"}
    cid = allowed[0]
    days = int(args.get("period_days") or 30)
    try:
        from ads.client import build_client
        from ads.read import account_stats

        client = build_client()
        st = await asyncio.to_thread(account_stats, client, cid, days)
        return {
            "type": "read", "tool": name, "account": cid, "days": days,
            "stats": {
                "impressions": st.impressions, "clicks": st.clicks,
                "cost": round(st.cost, 2), "conversions": st.conversions,
                "conv_value": round(st.conv_value, 2),
            },
        }
    except Exception as e:  # сеть/доступ/SDK
        return {"type": "text", "text": f"ошибка чтения Google Ads: {type(e).__name__}: {e}"}

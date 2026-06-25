"""Изменяющие операции Google Ads. ЕДИНСТВЕННОЕ место, где код реально меняет аккаунт.

ЖЁСТКО: каждая функция требует валидный confirmation_id (подтверждённый proposal в audit_log).
Без него — PermissionError. Модель/агент сюда не ходит напрямую — только через confirm-гейт.
См. skill confirm-gate-audit и golden rules в CLAUDE.md.
"""
from __future__ import annotations

from typing import Protocol


class ConfirmStore(Protocol):
    async def get_confirmed(self, confirmation_id: str) -> "ConfirmedProposal | None": ...
    async def finalize(self, confirmation_id: str, *, result: object) -> None: ...


class ConfirmedProposal(Protocol):
    operation: str
    status: str          # "confirmed" | "rejected" | "pending"
    user_initiated: bool  # True только если изменение пришло прямой командой пользователя


async def _require_confirmation(
    confirm_store: ConfirmStore, confirmation_id: str, operation: str
) -> ConfirmedProposal:
    proposal = await confirm_store.get_confirmed(confirmation_id)
    if proposal is None or proposal.operation != operation or proposal.status != "confirmed":
        raise PermissionError(
            f"мутация '{operation}' без валидного confirmation_id — отклонено"
        )
    return proposal


# ── Пример: изменение бюджета (фаза 1) ─────────────────────────────────────────
async def apply_update_budget(
    *,
    campaign_id: str,
    new_budget_micros: int,
    confirmation_id: str,
    confirm_store: ConfirmStore,
    ads_client: object,
) -> dict:
    proposal = await _require_confirmation(confirm_store, confirmation_id, "update_budget")

    # Бюджет — ТОЛЬКО по прямой команде пользователя (никогда из scheduler/anomaly)
    if not proposal.user_initiated:
        raise PermissionError("изменение бюджета должно быть прямой командой пользователя")

    # Валидация диапазонов В КОДЕ (не доверять модели)
    if new_budget_micros <= 0:
        raise ValueError("бюджет должен быть > 0")

    # TODO(фаза 1): реальный вызов google-ads SDK (CampaignBudgetService) только на TEST MCC
    result = {"campaign_id": campaign_id, "new_budget_micros": new_budget_micros, "applied": True}

    await confirm_store.finalize(confirmation_id, result=result)
    return result


# Другие apply_* (ставки, ключи, ГЕО, статус) добавлять по шаблону через skill `new-mutation`.

"""Confirm-гейт: разделяет ПРЕДЛОЖЕНИЕ изменения и его ВЫПОЛНЕНИЕ.

Поток: агент создаёт Proposal (diff «было→станет») → бот показывает + кнопки →
пользователь жмёт «да» → статус proposal = confirmed → код выполняет apply_* по confirmation_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass
class Proposal:
    """Черновик изменения. НЕ выполняется до подтверждения."""

    operation: str  # напр. "update_budget"
    summary: str  # человекочитаемое «было → станет»
    params: dict  # валидируемые параметры (Pydantic на уровне tools)
    chat_id: int
    user_initiated: bool = True  # пришло прямой командой пользователя?
    confirmation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"  # pending | confirmed | rejected
    big_list_attachment: str | None = None  # ключи/минус-слова — ссылкой, не текстом


def build_summary(operation: str, before: object, after: object) -> str:
    """Единая сводка «было → станет». Большие списки — НЕ текстом, а вложением."""
    return f"{operation}: {before} → {after}"


def confirm(proposal: Proposal) -> Proposal:
    proposal.status = "confirmed"
    return proposal


def reject(proposal: Proposal) -> Proposal:
    proposal.status = "rejected"
    return proposal


# TODO(фаза 1): персистить Proposal/статус в БД (db.proposals) + писать audit_log
# (кто/когда/что/результат, без секретов) при confirm и reject.

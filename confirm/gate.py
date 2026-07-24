"""Confirm-гейт: разделяет ПРЕДЛОЖЕНИЕ изменения и его ВЫПОЛНЕНИЕ.

Поток: агент создаёт Proposal (diff «было→станет») → бот показывает + кнопки →
пользователь жмёт «да» → статус proposal = confirmed → код выполняет apply_* по confirmation_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from confirm.render import fmt_mutation_summary


@dataclass
class Proposal:
    """Черновик изменения. НЕ выполняется до подтверждения."""

    operation: str  # напр. "update_budget"
    summary: str  # человекочитаемое «было → станет»
    params: dict  # валидируемые параметры (Pydantic на уровне tools)
    chat_id: int
    # fail-closed: True ставит ТОЛЬКО доверенный вход (Telegram-команда человека). Дефолт False,
    # чтобы автоматический создатель (scheduler/anomaly), забывший флаг, был заблокирован гейтом
    # бюджета/ставки (golden rule #3), а не молча разрешён.
    user_initiated: bool = False
    confirmation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"  # pending | confirmed | executing | applied | failed | rejected
    big_list_attachment: str | None = None  # ключи/минус-слова — ссылкой, не текстом


def build_summary(operation: str, before: object, after: object, lang: str = "ru") -> str:
    """Единая сводка «было → станет». Большие списки — НЕ текстом, а вложением.

    Текст рисует `confirm.render` — ТОТ ЖЕ, что человек видит в Telegram-карточке. Раньше здесь
    возвращался сырой dict параметров: в headless-контуре человек подтверждал не то, что читал.
    Сырой формат остался запасным — для не-dict `after` и для операций со своей богатой карточкой
    (create_rsa/GDN), у которых рендер честно отдаёт ''."""
    if isinstance(after, dict):
        human = fmt_mutation_summary(operation, after, lang)
        if human:
            return human
    return f"{operation}: {before} → {after}"


# Жизненный цикл черновика (pending→confirmed→executing→applied/failed/rejected) + audit
# живёт в confirm/store.py (ConfirmStore, БД). Здесь — только транспорт Proposal + build_summary.

"""Провенанс ХОДА: что именно этот ход запустило — живой человек или машина (Волна 1.4, И3).

Зачем отдельный бит, когда есть `user_initiated`. `user_initiated` — это *аргумент* `save_proposal`,
и сегодня он верен только по построению: три точки создания черновика лежат внутри aiogram-хендлеров,
куда не попасть иначе как входящим апдейтом человека из whitelist. В headless-контуре (Hermes) точка
создания станет вызываемой из MCP-инструмента, из cron-джобы и из self-improvement-форка — и тогда
`user_initiated=True` окажется ровно тем, что напишет вызывающий. Бит, который можно передать
аргументом, золотое правило 3 не охраняет: он охраняет только аккуратных.

Поэтому второй бит НЕ передаётся вовсе. Он лежит в contextvar, поднять его может ТОЛЬКО
`human_turn(...)`, и открывают этот scope исключительно доверенные входы (список — в
`tests/test_provenance_gate.py`, там же мета-гард на новые call-site'ы). У функции нет параметра
«человек ли это»: `human_turn()` ставит True, `machine_turn()` — False; выбрать значение
вычислением нельзя, можно только выбрать функцию, а это видно в диффе.

Дефолт — `False` (правило 10). Код, до которого доверенный вход не дотянулся (cron, скрипт,
перезапущенный процесс, `asyncio.to_thread` с новым контекстом), получает машинный ход и денежную
операцию не проведёт. Забыть поднять бит = отказ; забыть опустить = невозможно.

contextvars изолированы по asyncio-таске — как `core.context`, чей `request_id` мы и берём в
`run_id` (одна корреляция на ход, не вторая нумерация).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class TurnProvenance:
    """Происхождение текущего хода. Неизменяемый: подправить поле «на месте» нельзя."""

    human_turn: bool = False  # ход триггернуло входящее сообщение/действие живого человека
    actor_user_id: int | None = None  # кто именно (Telegram user_id), не chat_id
    run_id: str = "-"  # корреляция хода; совпадает с request_id доверенного слоя


_MACHINE = TurnProvenance()

_PROV: contextvars.ContextVar[TurnProvenance] = contextvars.ContextVar(
    "aimash_turn_provenance", default=_MACHINE
)


def get_provenance() -> TurnProvenance:
    """Провенанс ТЕКУЩЕГО хода. Вне доверенного scope — машинный (fail-closed)."""
    return _PROV.get()


@contextmanager
def human_turn(*, actor_user_id: int | None = None, run_id: str | None = None) -> Iterator[None]:
    """Пометить ход человеческим. Открывать ТОЛЬКО из доверенного входа — того, который сам
    установил, что это входящее сообщение/нажатие живого человека из whitelist по доверенному каналу.

    `run_id` по умолчанию берётся из `core.context` (request_id доверенного слоя), чтобы черновик
    можно было сшить с логами хода, в котором он родился.

    Не принимает флага «человек ли»: значение нельзя вычислить из данных, пришедших снаружи, — можно
    только вызвать эту функцию или `machine_turn`, и выбор виден в диффе."""
    from core.context import get_context

    prov = TurnProvenance(
        human_turn=True,
        actor_user_id=actor_user_id,
        run_id=(get_context().request_id if run_id is None else run_id),
    )
    token = _PROV.set(prov)
    try:
        yield
    finally:
        _PROV.reset(token)


@contextmanager
def machine_turn() -> Iterator[None]:
    """Явно машинный ход (cron, self-improvement, реконсиляция). Тот же дефолт, что и вне scope, —
    нужен затем, чтобы вложенный вызов НЕ унаследовал человеческий бит внешнего хода: джоба,
    запущенная из хендлера (`/audit` → фоновая пересборка), человеком не является."""
    token = _PROV.set(_MACHINE)
    try:
        yield
    finally:
        _PROV.reset(token)

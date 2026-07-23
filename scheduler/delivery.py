"""Порт доставки: клавиатуры к сообщениям планировщика — ОПЦИОНАЛЬНЫ.

Планировщик обязан подниматься отдельным процессом, без `bot/` (SPEC.md §5.3, мина C4): кнопочный
слой архивируется, а джобы — нет. Но пока bot-процесс жив, его джобы шлют те же карточки с
👍/👎/🙈/«применить», что и интерактивный /advise, — иначе развязка молча откатила бы дайджест к
голому тексту.

Развязка: джоба СПРАШИВАЕТ клавиатуру у порта и работает с `None` как с нормой (text-only).
Заполняет порт тот процесс, у которого кнопочный слой есть, — `bot/main.py` на старте.
Standalone-планировщик порт не заполняет и шлёт текст; ни одна джоба от этого не падает.

Fail-SOFT, и это НЕ исключение из правила 10: порт ничего не разрешает. Кнопка лишь СТАРТУЕТ
confirm-гейт по тапу человека — proposal из scheduler не создаётся никогда (golden rule #1/#3), а
права проверяются на минтинге черновика и заново на исполнении. Отсутствие кнопки убирает удобство,
а не проверку; наличие — ничего не открывает. Денежного смысла у порта нет, потому он и может
молчать.

Имена — закрытый список: опечатка в строковом ключе дала бы «кнопок нет, и никто не знает почему»
(тот же класс, что К10 у Hermes-конфига). Неизвестное имя роняет вызов сразу, на проводке.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

ADVISE_FEEDBACK = "advise_feedback"  # 👍/👎/🙈 + «применить» под рекомендацией
THRESHOLD_TUNE = "threshold_tune"  # «Принять/Отклонить» предложение порогов аномалий

KNOWN = frozenset({ADVISE_FEEDBACK, THRESHOLD_TUNE})

_BUILDERS: dict[str, Callable[..., Any]] = {}


def register(name: str, builder: Callable[..., Any]) -> None:
    """Отдать порту построитель клавиатуры. Зовёт процесс, у которого кнопочный слой есть."""
    if name not in KNOWN:
        raise ValueError(f"неизвестное имя клавиатуры {name!r}; известны: {sorted(KNOWN)}")
    _BUILDERS[name] = builder


def registered() -> frozenset[str]:
    """Что уже заполнено — для гардов и диагностики старта."""
    return frozenset(_BUILDERS)


def markup(name: str, *args: Any, **kwargs: Any) -> Any | None:
    """Клавиатура или None: порт не заполнен (standalone-планировщик) либо построитель упал.

    Сбой построителя гасим: карточка с цифрами важнее кнопки под ней, а джобы рассылки и так
    держат недоставку одного получателя как норму."""
    if name not in KNOWN:
        raise ValueError(f"неизвестное имя клавиатуры {name!r}; известны: {sorted(KNOWN)}")
    builder = _BUILDERS.get(name)
    if builder is None:
        return None
    try:
        return builder(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — кнопка-довесок, сообщение важнее
        log.warning("scheduler.delivery: клавиатура %s не собрана: %s", name, type(e).__name__)
        return None

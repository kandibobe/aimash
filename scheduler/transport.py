"""Тонкий Telegram-транспорт планировщика — единственное место в фоновом контуре, где aiogram
разрешён.

Почему модуль вообще есть. Джобы шлют не только текст: недельный дайджест уходит коротким
сообщением ПЛЮС .txt-вложением с деталями. Текст отправляется дак-тайпингом (`bot.send_message`) и
никакого импорта не требует, а вложение — требует: `bot.send_document` принимает `InputFile`, и
собрать его без aiogram нельзя (голая строка пути трактуется как file_id/URL). Раньше эту функцию
джоба брала из `bot/ux.py` — то есть тащила за собой ВЕСЬ кнопочный слой ради одной обёртки над
временным файлом (SPEC.md §5.3, мина C4).

Почему это не дыра в развязке. Топология (три процесса) даёт планировщику «БД + тонкий Bot-клиент
для отправки» — Telegram-транспорт ему положен по определению, архивируется же `bot/` (кнопки, FSM,
хендлеры), а не факт умения отправить сообщение. Граница проходит по `bot/`, и она здесь целая:
модуль не импортирует ни одного имени из `bot/`. Гард `tests/test_scheduler_decoupled.py` держит обе
части раздельно — запрет `bot/` без исключений, запрет aiogram с этим единственным.

BZ-3 (2026-07-30): сюда же переехала отправка ТЕКСТА — `send_bot_message` с ретраем на флуд-лимите
(порт `bot/ux.py::answer_with_flood_retry` на `bot.send_message`). Раньше каждая джоба слала
`bot.send_message` напрямую: первый же TelegramRetryAfter превращался в «недоставлено» — терпимо
для аномалий (повтор через 6 ч), фатально для error-алертов и .xlsx-курьера (state='failed'
навсегда). Прямые `.send_message`/`.send_document` вне этого модуля запрещены AST-гардом
`tests/test_transport_retry.py` — новая точка отправки получает ретрай по построению, а не по
памяти автора.

`bot/ux.py` реэкспортирует `send_bot_document` — вызывающие в боте не менялись.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

_RETRY_AFTER_MAX_ATTEMPTS = 3  # как bot/ux.py: флуд пережидаем дважды, третью неудачу отдаём наружу


async def _with_flood_retry(send):
    """Пережить TelegramRetryAfter (flood control): подождать retry_after и повторить, до
    _RETRY_AFTER_MAX_ATTEMPTS попыток. Прочие исключения — сразу наружу (блокировка чата/сеть
    ожиданием не лечатся, per-recipient решение остаётся за вызывающим). `send` — zero-arg
    фабрика корутины: каждой попытке нужна СВЕЖАЯ (повторный await исчерпанной — RuntimeError)."""
    for attempt in range(_RETRY_AFTER_MAX_ATTEMPTS):
        try:
            return await send()
        except TelegramRetryAfter as e:
            if attempt == _RETRY_AFTER_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(float(getattr(e, "retry_after", 1.0)) + 0.1)
    return None  # недостижимо (return или raise выше)


async def send_bot_message(bot: object, chat_id: int, text: str, **kw: object) -> None:
    """Отправить текст с флуд-ретраем — единственная дорога `send_message` фонового контура (BZ-3).
    `kw` — прозрачный проброс (parse_mode, reply_markup): транспорт — слой доставки, содержимое и
    формат — забота вызывающего."""
    await _with_flood_retry(lambda: bot.send_message(chat_id, text, **kw))  # type: ignore[attr-defined]


async def send_bot_file(bot: object, chat_id: int, *, path: str, filename: str) -> None:
    """Отправить УЖЕ СУЩЕСТВУЮЩИЙ файл с диска. Жизненным циклом файла НЕ владеет.

    Разделение намеренное: у .txt-дайджеста файл существует ровно ради отправки и умирает здесь же,
    а .xlsx-вложение черновика курьер собирает сам (openpyxl вне event loop) и сам же удаляет —
    отдать его удаление транспорту значило бы, что при исключении на отправке владелец не знает,
    остался файл или нет. Кто создал, тот и удаляет; транспорт умеет ровно один трюк — завернуть
    путь в `FSInputFile`, потому что голая строка трактуется Telegram как file_id/URL."""
    await _with_flood_retry(
        lambda: bot.send_document(chat_id, FSInputFile(path, filename=filename))  # type: ignore[attr-defined]
    )


async def send_bot_document(bot: object, chat_id: int, *, text: str, filename: str) -> None:
    """Отправить ТЕКСТ .txt-вложением из SCHEDULER-контекста (нет message, только bot). Аналог
    send_text_document для плановых джоб (еженедельный дайджест). Временный файл — в finally.
    Вызывающий сам ловит исключения per-recipient (один недоступный чат не валит рассылку)."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="aimash_digest_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        await send_bot_file(bot, chat_id, path=path, filename=filename)
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

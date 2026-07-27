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

`bot/ux.py` реэкспортирует `send_bot_document` — вызывающие в боте не менялись.
"""

from __future__ import annotations

import os
import tempfile

from aiogram.types import FSInputFile


async def send_bot_file(bot: object, chat_id: int, *, path: str, filename: str) -> None:
    """Отправить УЖЕ СУЩЕСТВУЮЩИЙ файл с диска. Жизненным циклом файла НЕ владеет.

    Разделение намеренное: у .txt-дайджеста файл существует ровно ради отправки и умирает здесь же,
    а .xlsx-вложение черновика курьер собирает сам (openpyxl вне event loop) и сам же удаляет —
    отдать его удаление транспорту значило бы, что при исключении на отправке владелец не знает,
    остался файл или нет. Кто создал, тот и удаляет; транспорт умеет ровно один трюк — завернуть
    путь в `FSInputFile`, потому что голая строка трактуется Telegram как file_id/URL."""
    await bot.send_document(chat_id, FSInputFile(path, filename=filename))  # type: ignore[attr-defined]


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

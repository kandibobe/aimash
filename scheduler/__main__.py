"""Дом процесса планировщика: `python -m scheduler`.

Зачем отдельный процесс. До сих пор единственный старт APScheduler жил внутри aiogram-петли
(`bot/main.py`), поэтому при архивации кнопочного слоя (SPEC.md §5.3) умирали ВСЕ джобы разом —
включая `reconcile_stale_executing`, который поднимает зависшие мутации в `needs_review`. Это
денежный путь, а не дайджесты: черновик, застрявший в `executing`, без этой джобы остаётся так
навсегда и никого не уведомляет. Топология (три процесса) даёт планировщику собственный дом:
«БД + тонкий Bot-клиент для отправки».

Что здесь ЕСТЬ и чего НЕТ. Есть: БД, ads-слой через общий `app/bootstrap.py`, APScheduler, тонкий
`aiogram.Bot` только для отправки, heartbeat для честного HEALTHCHECK. Нет: Dispatcher, хендлеров,
middleware, порта клавиатур `scheduler/delivery.py`. Последнее — не забывчивость: кнопки живут в
архивируемом слое, и здесь их взять неоткуда. Следствие описано в самом порту — дайджесты уходят
текстом (цифры самоценны), а thr-tune молчит целиком (его сообщение целиком — предложение, принять
которое можно только тапом).

Кто владеет джобами. Кандидатов два — этот процесс и бот (`SCHEDULER_IN_BOT`, дефолт `true` =
историческое поведение). Намерение задаёт env, но энфорсмент — advisory-lock роли `scheduler`
(`db/session.py`): забыли снять флаг у бота и подняли этот контейнер ⇒ каждая джоба шла бы ДВАЖДЫ.
Здесь мы падаем ГРОМКО и с ненулевым кодом, а не деградируем в тихий standby: молчащий контейнер
планировщика неотличим от работающего, и это ровно тот класс ошибки, который проект уже ловил
(«выглядит рабочим и молча не действует»).

⚠️ На SQLite (dev) advisory-lock — no-op (`acquire_single_instance_lock` возвращает True всем):
двойного запуска этот гард в dev не поймает. Прод — Postgres.

Миграции: `docker-entrypoint.sh` гоняет `alembic upgrade head` только для команды `python -m
bot.main`, поэтому здесь схему проверяет `assert_schema_at_head` внутри `bootstrap_ads_layer()` —
fail-closed, а не тихий старт на отставшей схеме. Одновременный `alembic upgrade` двумя сервисами
не возникает по построению compose (`depends_on: bot: service_healthy`) — полное решение C6
(entrypoint-гейт под многосервисность) сюда не входит.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from aiogram import Bot

from app.bootstrap import bootstrap_ads_layer
from core.config import settings
from core.logging import log, setup_logging
from db.session import (
    acquire_single_instance_lock,
    dispose_engine,
    release_single_instance_lock,
)

_ROLE = "scheduler"


async def main() -> int:
    """Поднять планировщик и крутиться до SIGTERM/SIGINT. Возвращает код выхода процесса."""
    setup_logging()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        # Без токена джобы соберут отчёты и не смогут их доставить — тихая работа вхолостую.
        log.error("TELEGRAM_BOT_TOKEN пуст — планировщику нечем доставлять результаты, выхожу")
        return 2

    # Владение джобами: занято ⇒ выходим ненулевым кодом. Сбой САМОГО захвата (БД мигнула) — тоже
    # отказ, а не «стартуем без гарда»: у бота такая деградация безопасна (в худшем случае 409 от
    # Telegram), здесь она означает второй набор джоб, то есть двойные уведомления и двойной
    # reconcile денежных черновиков.
    try:
        if not await acquire_single_instance_lock(_ROLE):
            log.error(
                "advisory-lock роли `scheduler` уже занят — джобы кто-то планирует (скорее всего "
                "бот с SCHEDULER_IN_BOT=true). Два владельца = каждая джоба дважды. Выхожу."
            )
            return 3
    except Exception as e:  # noqa: BLE001 — правило 5: наружу только тип
        log.error("захват lock роли `scheduler` не удался: %s — выхожу", type(e).__name__)
        return 4

    bot: Bot | None = None
    sched = None
    hb_task: asyncio.Task | None = None
    try:
        # Тот же bootstrap, что у бота и у MCP: один набор глобалов `ads/client.py` на все контуры.
        # Сбой критичен — джобы читают Google Ads (raise уже санитизирован, правило 5).
        try:
            await bootstrap_ads_layer()
        except Exception as e:  # noqa: BLE001
            log.error("ads-слой не поднят — планировщик не стартует: %s", type(e).__name__)
            return 5
        try:  # §4: языки интерфейса из user_settings — дайджесты локализованы, как в боте
            from core import i18n

            await i18n.load_langs()
        except Exception as e:  # noqa: BLE001 — не критично, дефолт RU
            log.warning("языки интерфейса не загружены из БД: %s", type(e).__name__)

        from core.heartbeat import heartbeat_loop
        from scheduler.service import register_user_report_schedules, setup_scheduler

        bot = Bot(token)
        sched = setup_scheduler(bot)
        try:
            await register_user_report_schedules(sched, bot)
        except Exception as e:  # noqa: BLE001 — персональные расписания опциональны (как в боте)
            log.warning("per-user report schedules не зарегистрированы: %s", type(e).__name__)

        # B9: heartbeat на ТОМ ЖЕ loop, что джобы — завис loop ⇒ файл протухает ⇒ HEALTHCHECK
        # краснеет. Без него контейнер вечно unhealthy (Dockerfile проверяет свежесть файла,
        # а писал его только `bot/main.py`) и ломает `depends_on: service_healthy`.
        hb_task = asyncio.create_task(heartbeat_loop())

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: сигналы в ProactorEventLoop не вешаются
                signal.signal(sig, lambda *_a: stop.set())
        log.info(
            "планировщик запущен отдельным процессом (кнопочного слоя нет — дайджесты текстом)"
        )
        await stop.wait()
        log.info("получен сигнал остановки — гашу планировщик")
        return 0
    finally:
        if hb_task is not None:
            hb_task.cancel()
        if sched is not None:
            # wait=True: дожидаемся работающих джоб (read-only, ограничены ADS_TIMEOUT_S), чтобы
            # SIGTERM не оборвал джобу на полузаписи в БД — как в teardown бота.
            try:
                sched.shutdown(wait=True)
            except Exception as e:  # noqa: BLE001 — выключение не должно ронять teardown
                log.warning("scheduler.shutdown(wait=True) сбой: %s", type(e).__name__)
        await release_single_instance_lock(_ROLE)
        await dispose_engine()
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as e:  # noqa: BLE001
                log.warning("закрытие сессии Telegram: %s", type(e).__name__)
        log.info("планировщик остановлен (ресурсы освобождены).")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

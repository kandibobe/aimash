"""app/bootstrap.py — headless-инициализация ads-слоя без импорта bot/aiogram.

Вынесено из `bot/main.py` (шов bootstrap перед стартом polling). Причина существования
одна и не косметическая: модульные глобалы `ads/client.py` (`_CLIENT_CACHE` /
`_OAUTH_RUNTIME` / `_READ_DISCOVERED`) на импорте ПУСТЫ и засеваются только этим bootstrap.
В боте их сеет `bot.main.main()`. MCP-сервер (Контур A) НЕ импортирует `bot`, поэтому обязан
поднимать ads-слой ЭТИМ модулем — иначе в prod-мультиаккаунте `build_client(child)` под чужим
MCC не найдёт per-account OAuth и тихо деградирует: read-discovery уходит в fail-closed,
mutation-путь → `PERMISSION_DENIED`. В Draft (единый .env-токен) эта деградация НЕ видна —
поэтому coupling невидим при разработке и стреляет только на боевом мультиаккаунте.

Fail-soft семантика сохранена дословно из `bot.main`:
  • `init_db` — критичен: без БД нет `ConfirmStore`/`proposals`, поднимать ads-слой бессмысленно.
    Здесь (в отличие от бота, который делает `return`) — **raise**: MCP-сервер не должен стартовать
    в полу-инициализированном состоянии (fail-closed, правило 10).
  • три сидера OAuth/клиента/дочерних — опциональны: Draft и тест-MCC работают на едином
    `.env`-токене (`ads.client._cfg_for`), их сбой логируется и не роняет старт.

Правило 5: наружу и в лог — только `type(e).__name__`, НИКОГДА `str(e)` (исключение google-ads/
OpenRouter может нести токен). `init_db`-ошибку пробрасываем санитизированной, без цепочки
(`from None`), чтобы DSN с паролем не всплыл у вызывающего.

Bot-специфику сюда НЕ переносим: single-instance polling-lock, `i18n.load_langs`,
`_load_model_override`, middleware — живут в `bot.main` и к headless-пути отношения не имеют.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


async def bootstrap_ads_layer() -> None:
    """Засеять модульное состояние ads-слоя для headless-использования (MCP, скрипты).

    Порядок и обработка ошибок — как в `bot.main.main()`, но без bot-специфики. Идемпотентно
    в пределах процесса: `build_client` под `@lru_cache`, сидеры перезаписывают свои глобалы.
    Вызывать один раз на старте MCP-сервера, до обслуживания инструментов.

    Raises:
        RuntimeError: если `init_db` не удался (санитизировано, без DSN). Всё остальное — fail-soft.
    """
    # БД: критично. Ловим, чтобы дефолтный excepthook не напечатал DSN с паролем, и пробрасываем
    # санитизированную ошибку (fail-closed: сервер не поднимаем в полу-инициализированном виде).
    from db.session import init_db

    try:
        await init_db()
    except Exception as e:  # noqa: BLE001 — редактируем ДО пробрасывания (правило 5)
        log.error("init_db не удалось — ads-слой не поднят: %s", type(e).__name__, exc_info=e)
        raise RuntimeError(f"init_db failed: {type(e).__name__}") from None

    # §8/мультиаккаунт: расшифровать per-account OAuth в рантайм-кэш (_OAUTH_RUNTIME), чтобы
    # build_client(child) под другим MCC брал его refresh-токен/login_customer_id. Сбой не
    # критичен — Draft/тест-MCC покрыт единым .env-токеном.
    try:
        from ads.client import load_oauth_cache

        await load_oauth_cache()
    except Exception as e:  # noqa: BLE001 — per-account креды опциональны (Draft на .env)
        log.warning("oauth: per-account токены не загружены: %s", type(e).__name__)

    # Прогрев Google Ads клиента off-loop: тяжёлый импорт SDK + OAuth на старте, не на первом
    # интерактивном read (иначе первый read морозит event loop на ~0.5–2 с).
    try:
        from ads.client import build_client

        await asyncio.to_thread(build_client)  # @lru_cache → последующие вызовы мгновенны
    except Exception as e:  # noqa: BLE001 — cred-сбой на старте не критичен, реальный вызов проверит
        log.warning("прогрев build_client не удался: %s", type(e).__name__)

    # §8: обойти настроенные MCC и запомнить дочерние как read-allow-list (_READ_DISCOVERED).
    # READ-ONLY, под замком ensure_manager_allowed; сбой не критичен для старта (без обхода читаем
    # мутационный аккаунт + env read-list). Мутации этим НЕ затрагиваются.
    try:
        from ads.client import discover_read_children

        await discover_read_children()
    except Exception as e:  # noqa: BLE001 — обход дочерних опционален (Draft читается и без него)
        log.warning("mcc discover: обход дочерних не выполнен: %s", type(e).__name__)

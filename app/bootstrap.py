"""app/bootstrap.py — ЕДИНСТВЕННАЯ инициализация ads-слоя, без импорта bot/aiogram.

Вынесено из `bot/main.py` (шов bootstrap перед стартом polling). Причина существования
одна и не косметическая: модульные глобалы `ads/client.py` (`_CLIENT_CACHE` /
`_OAUTH_RUNTIME` / `_READ_DISCOVERED`) на импорте ПУСТЫ и засеваются только этим bootstrap.
MCP-сервер (Контур A) НЕ импортирует `bot`, поэтому обязан поднимать ads-слой ЭТИМ модулем —
иначе в prod-мультиаккаунте `build_client(child)` под чужим MCC не найдёт per-account OAuth и
тихо деградирует: read-discovery уходит в fail-closed, mutation-путь → `PERMISSION_DENIED`.
В Draft (единый .env-токен) эта деградация НЕ видна — поэтому coupling невидим при разработке
и стреляет только на боевом мультиаккаунте.

Копия этих же шагов какое-то время жила в `bot.main.main()` — и копии успели разойтись (на сбое
`init_db` здесь был raise, там `return`). Теперь бот зовёт эту функцию (`bot/main.py`), а не свой
дубль: два разных набора глобалов `ads/client.py` в двух контурах ловятся только на боевом
мультиаккаунте, где цена ошибки максимальна.

Fail-soft семантика сохранена дословно из прежнего `bot.main`:
  • `init_db` — критичен: без БД нет `ConfirmStore`/`proposals`, поднимать ads-слой бессмысленно.
    Здесь **raise** (fail-closed, правило 10): ни MCP-сервер, ни бот не должны стартовать
    полу-инициализированными. Бот ловит это и выходит `return`'ом — чтобы на недоступной БД в лог
    шла одна понятная строка, а не трасса с DSN.
  • три сидера OAuth/клиента/дочерних — опциональны: Draft и тест-MCC работают на едином
    `.env`-токене (`ads.client._cfg_for`), их сбой логируется и не роняет старт.

Сверх прежнего `bot.main` здесь ОДИН шаг — `assert_schema_at_head`: у бота эквивалент стоит раньше
и сильнее (`docker-entrypoint.sh` гоняет `alembic upgrade head` до старта), но навешен на команду
`python -m bot.main`, а headless-вход идёт мимо. Подробности — в докстринге самой функции.

Правило 5: наружу и в лог — только `type(e).__name__`, НИКОГДА `str(e)` (исключение google-ads/
OpenRouter может нести токен). `init_db`-ошибку пробрасываем санитизированной, без цепочки
(`from None`), чтобы DSN с паролем не всплыл у вызывающего.

Bot-специфику сюда НЕ переносим: single-instance polling-lock, `i18n.load_langs`,
`_load_model_override`, middleware — живут в `bot.main` и к headless-пути отношения не имеют.
Polling-lock отдельно важен: он берётся в `bot.main` ДО этого вызова, чтобы лишний инстанс выходил,
не потратив обход MCC. Переносить его сюда нельзя без ролевых ключей — см. `db/session.py`.
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
        RuntimeError: если `init_db` не удался или схема БД отстала от кода (санитизировано, без
            DSN). Всё остальное — fail-soft.
    """
    # БД: критично. Ловим, чтобы дефолтный excepthook не напечатал DSN с паролем, и пробрасываем
    # санитизированную ошибку (fail-closed: сервер не поднимаем в полу-инициализированном виде).
    from db.session import assert_schema_at_head, init_db

    try:
        await init_db()
    except Exception as e:  # noqa: BLE001 — редактируем ДО пробрасывания (правило 5)
        log.error("init_db не удалось — ads-слой не поднят: %s", type(e).__name__, exc_info=e)
        raise RuntimeError(f"init_db failed: {type(e).__name__}") from None

    # Схема-гейт: `init_db` ловит только ОТСУТСТВИЕ таблиц, дрейф на одну миграцию проходит молча.
    # У бота гейт сильнее и стоит раньше (`docker-entrypoint.sh` → `alembic upgrade head` до старта),
    # но он навешен на команду `python -m bot.main`; headless-вход (`python -m mcp_server`, скрипты)
    # идёт мимо него. Текст ошибки уже санитизирован в `assert_schema_at_head` (правило 5).
    await assert_schema_at_head()

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

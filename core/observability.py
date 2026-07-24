"""Опциональный мониторинг ошибок (Sentry). ВЫКЛ без SENTRY_DSN — ноль накладных расходов и сети.

Зачем: бот трогает рекламные деньги — нужно знать о падениях на денежном пути сразу, а не из логов
постфактум. Sentry ловит исключения с контекстом и шлёт алерт.

Безопасность (золотое правило #5 — секреты НИКУДА наружу):
  • before_send РЕКУРСИВНО редактирует все строки события (сообщение, traceback, breadcrumbs, extra)
    через core.logging.redact_text + точные значения известных SecretStr — RedactionFilter висит на
    лог-хендлере и до Sentry-события не доходит, поэтому это ЕДИНСТВЕННЫЙ барьер для Sentry;
  • include_local_variables=False — локалы в трейсбеке могут нести расшифрованный токен;
  • send_default_pii=False — не собирать IP/идентификаторы пользователя.

Производительность/невмешательство (требование: не мешать боту, не снижать эффективность):
  • DSN пуст → функция выходит ДО импорта sentry_sdk: на горячем пути нет ни импорта, ни сети;
  • traces_sample_rate из env (по умолчанию 0.0 → перф-трейсинг выключен, нет оверхеда на запросах);
  • ошибки ловятся через уже существующие log.error(exc_info=...) (LoggingIntegration), новые
    хендлеры/вызовы в коде НЕ добавляются — поведение бота не меняется;
  • init обёрнут в try/except → телеметрия НИКОГДА не валит старт бота.
"""

from __future__ import annotations

from typing import Any

from core.logging import log, redact_text


def _scrub(value: Any) -> Any:
    """Рекурсивно редактировать секрето-подобные строки в произвольной структуре события."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _before_send(event: Any, _hint: Any) -> Any:
    """Редакция всего события перед отправкой + per-account тег (§8). Никогда не роняет отправку."""
    try:
        scrubbed = _scrub(event)
        # Тег аккаунта из контекста запроса — фильтрация инцидентов по аккаунту на масштабе ~10.
        # customer_id — цифры (не секрет). Ставим ПОСЛЕ scrub, чтобы тег не потёрся редакцией.
        try:
            from core.context import get_context

            cid = get_context().customer_id
            if cid and isinstance(scrubbed, dict):
                scrubbed.setdefault("tags", {})["customer_id"] = cid
        except Exception:  # noqa: BLE001 — тег не критичен
            pass
        return scrubbed
    except Exception:  # noqa: BLE001 — редакция не должна ломать телеметрию
        return event


def init_observability() -> None:
    """Поднять Sentry, если задан SENTRY_DSN. Иначе — no-op (без импорта sentry в рантайме бота)."""
    from core.config import settings

    dsn = settings.sentry_dsn.get_secret_value()
    if not dsn:  # выключено — горячий путь не платит ничего (нет импорта sentry, нет сети)
        return
    try:
        import logging

        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        log.warning("SENTRY_DSN задан, но пакет sentry-sdk не установлен — мониторинг выключен")
        return
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.env,
            send_default_pii=False,  # не собирать PII
            include_local_variables=False,  # локалы трейсбека могут нести секреты (§5)
            max_request_body_size="never",
            traces_sample_rate=settings.sentry_traces_sample_rate,  # 0.0 => без перф-трейсинга
            before_send=_before_send,
            before_send_transaction=_before_send,
            # Ловим уже существующие log.error(exc_info=...) на денежном пути — без правок хендлеров.
            integrations=[
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        # Статические теги для удобной фильтрации инцидентов (без секретов).
        sentry_sdk.set_tag("gads_api_version", settings.google_ads_api_version)
        log.info("Sentry-мониторинг включён (env=%s)", settings.env)
    except Exception as e:  # noqa: BLE001 — телеметрия не должна валить старт бота
        log.warning("Sentry init не удался (%s) — продолжаю без мониторинга", type(e).__name__)

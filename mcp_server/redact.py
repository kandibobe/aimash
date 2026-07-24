"""Наружная редакция ошибок для MCP-слоя — БЕЗ aiogram (правило 5).

`bot.ux.err_text` тянет aiogram (и весь bot-слой) — в headless-контуре A использовать нельзя
(сломало бы гард изоляции). Собираем аналог из bot-free `core/`:
  • `GoogleAdsException` и вообще любое исключение → `core.ads_errors.humanize_google_ads_error`
    (оно уже редактирует секреты и корректно деградирует на не-Google исключениях: fallback
    `type(e).__name__: <redacted msg>`);
  • сам `humanize_*` обёрнут в try/except — если он вдруг бросит, отдаём `type(e).__name__`
    (никогда не сырой `str(e)`).

Наружу и в лог идёт ТОЛЬКО результат этой функции. Сырой `str(e)` не показываем никогда: исключение
от google-ads/OpenRouter может нести токен (три рубежа редакции — docs/SECURITY.md).
"""

from __future__ import annotations

from core.ads_errors import humanize_google_ads_error
from core.logging import redact_text


def redact_error(exc: BaseException) -> str:
    """Безопасный человекочитаемый текст ошибки для клиента/лога. Секреты вычищены, трасса не течёт."""
    try:
        return humanize_google_ads_error(exc)
    except Exception:  # noqa: BLE001 — сама редакция не должна ронять инструмент и не течёт наружу
        # Последний рубеж: даже имя типа прогоняем через redact_text (перестраховка, не str(exc)).
        return redact_text(type(exc).__name__)

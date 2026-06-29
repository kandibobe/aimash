"""Типизированные callback_data (aiogram CallbackData factory).

Заменяет ручные строки 'ok:<id>'/'no:<id>' на типобезопасные фабрики с парсингом.
Лимит callback_data — 64 байта: кладём короткие коды/индексы/confirmation_id
(hex uuid = 32 символа — влезает). Имена кампаний в callback_data НЕ кладём (могут не влезть
и содержать спецсимволы) — только индекс в текущем списке (резолв по chat_id в bot.main).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class ConfirmCB(CallbackData, prefix="cfm"):
    """Подтверждение/отмена черновика мутации (confirm-гейт)."""

    action: str  # "ok" | "no"
    cid: str  # confirmation_id (hex uuid)


class CampCB(CallbackData, prefix="camp"):
    """Действия в меню /campaigns. idx — позиция в последнем списке кампаний (по chat_id)."""

    action: str  # "menu" | "pause" | "resume" | "audience" | "back"
    idx: int


class AudienceCB(CallbackData, prefix="aud"):
    """Прикрепление аудитории к кампании (§3). camp_idx — кампания в _CAMP_CACHE (выбрана на
    предыдущем шаге), idx — аудитория в _AUD_CACHE (оба резолвятся по chat_id в bot.main)."""

    action: str  # "pick"
    camp_idx: int = -1
    idx: int = -1


class PeriodCB(CallbackData, prefix="per"):
    """Выбор периода для отчёта/статистики (пресеты ТЗ §9)."""

    target: str  # "report" | "export" | "status"
    code: str  # "7" | "30" | "90" | "MTD"


class RsaCB(CallbackData, prefix="rsa"):
    """Поэлементная курация RSA-текстов (ТЗ §10). cid — session_id (hex uuid). kind/idx
    указывают элемент: kind 'h'|'d', idx — позиция в сессии. Для массовых действий
    (approveall/finalize/cancel) kind/idx не используются (дефолты)."""

    action: str  # "approve" | "reject" | "refine" | "approveall" | "finalize" | "cancel"
    cid: str
    kind: str = ""  # "h" | "d" | ""
    idx: int = -1


class RsaPickCB(CallbackData, prefix="rsap"):
    """Визард /rsa: выбор кампании/группы по индексу в кэше (как CampCB). idx — позиция в
    последнем показанном списке (резолв по chat_id в bot.main)."""

    what: str  # "camp" | "ag"
    idx: int


class LangCB(CallbackData, prefix="lang"):
    """Выбор языка интерфейса (RU/EN) через /lang."""

    code: str  # "ru" | "en"


class ModelCB(CallbackData, prefix="mdl"):
    """Переключатель модели ИИ (/model). idx — позиция в router.MODEL_CHOICES (slug в
    callback_data не кладём: длинный и с '/'). Для reset/custom idx не используется."""

    action: str  # "set" | "reset" | "custom"
    idx: int = -1


class KwAddCB(CallbackData, prefix="kwadd"):
    """§7: добавить подобранные ключи (из результатов /keywords) в кампанию — только по команде,
    после показа списка + типа соответствия и «да» (confirm-гейт). token — ключ серверной сессии
    (список ключей не влезает в callback_data, 64 байта). mt — тип соответствия (для action='match')."""

    action: str  # "start" | "match" | "cancel"
    token: str  # hex-токен сессии _KW_ADD (bot.main)
    mt: str = ""  # "broad" | "phrase" | "exact" (только для action='match')

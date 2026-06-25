"""Клавиатуры и меню-команды бота Aimash (aiogram 3.x).

Чистый слой представления: НИКАКИХ обращений к Google Ads/БД. Тексты кнопок — здесь,
шаблоны сообщений — в bot/texts.py. Confirm-гейт: кнопки лишь ФОРМИРУЮТ ввод/черновик,
исполнение мутации — только после ✅ через ads.service (см. bot/main.py).
"""

from __future__ import annotations

from aiogram.types import BotCommand, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from bot.callbacks import CampCB, ConfirmCB, LangCB, PeriodCB, RsaCB, RsaPickCB

_NAME_LIMIT = 40


def _ellipsize(s: str, limit: int = _NAME_LIMIT) -> str:
    """Обрезать имя с видимым многоточием (иначе непонятно, что текст усечён)."""
    s = s or ""
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# ── Меню-кнопка Telegram (список команд в «/») ──────────────────────────────────
# Показываем только то, что бот реально умеет, + честные пометки «скоро» для фаз 2-3.
BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Запуск и меню"),
    BotCommand(command="help", description="Что я умею"),
    BotCommand(command="status", description="Статистика аккаунта (30 дн.)"),
    BotCommand(command="campaigns", description="Кампании: список и быстрые действия"),
    BotCommand(command="report", description="Сводка за период (7/30/90/MTD)"),
    BotCommand(command="export", description="Глубокий отчёт .xlsx"),
    BotCommand(command="rsa", description="Сгенерировать тексты объявления (RSA)"),
    BotCommand(command="cancel", description="Отменить текущий черновик"),
    BotCommand(command="keywords", description="Подбор ключевых слов (скоро)"),
    BotCommand(command="lang", description="Язык интерфейса / interface language"),
]


def lang_kb() -> InlineKeyboardMarkup:
    """Выбор языка интерфейса (RU/EN)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data=LangCB(code="ru"))
    kb.button(text="🇬🇧 English", callback_data=LangCB(code="en"))
    kb.adjust(2)
    return kb.as_markup()


# ── Reply-меню (постоянное нижнее) — только навигация/чтение, НЕ мутации ─────────
BTN_STATUS = "📊 Статистика"
BTN_CAMPAIGNS = "📋 Кампании"
BTN_REPORT = "📈 Отчёт"
BTN_HELP = "❓ Помощь"


def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_STATUS)
    kb.button(text=BTN_CAMPAIGNS)
    kb.button(text=BTN_REPORT)
    kb.button(text=BTN_HELP)
    kb.adjust(2, 2)
    return kb.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Команда или текст…",
    )


# ── Inline: подтверждение мутации (главный — confirm-гейт) ───────────────────────
def confirm_kb(cid: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=ConfirmCB(action="ok", cid=cid))
    kb.button(text="❌ Отмена", callback_data=ConfirmCB(action="no", cid=cid))
    kb.adjust(2)
    return kb.as_markup()


# ── Inline: /campaigns — список с быстрыми действиями ────────────────────────────
def campaigns_kb(camps: list[dict]) -> InlineKeyboardMarkup:
    """По кнопке на кампанию (раскрывает меню действий). idx = позиция в списке."""
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(camps):
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}", callback_data=CampCB(action="menu", idx=i)
        )
    kb.adjust(1)
    return kb.as_markup()


def campaign_actions_kb(idx: int, status: str) -> InlineKeyboardMarkup:
    """Действия для одной кампании. pause/resume зависят от статуса; мутации идут через
    confirm-гейт (кнопка лишь создаёт черновик, не исполняет)."""
    kb = InlineKeyboardBuilder()
    if status == "ENABLED":
        kb.button(text="⏸ Поставить на паузу", callback_data=CampCB(action="pause", idx=idx))
    elif status == "PAUSED":
        kb.button(text="▶️ Возобновить", callback_data=CampCB(action="resume", idx=idx))
    kb.button(text="‹ Назад к списку", callback_data=CampCB(action="back", idx=idx))
    kb.adjust(1)
    return kb.as_markup()


# ── Inline: выбор периода (отчёт) ────────────────────────────────────────────────
def period_kb(target: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, code in [("7 дней", "7"), ("30 дней", "30"), ("90 дней", "90"), ("MTD", "MTD")]:
        kb.button(text=label, callback_data=PeriodCB(target=target, code=code))
    kb.adjust(2, 2)
    return kb.as_markup()


# ── RSA-курация (ТЗ §10): поэлементное подтверждение + массовые действия ──────────
def rsa_item_kb(cid: str, kind: str, idx: int) -> InlineKeyboardMarkup:
    """Кнопки одного элемента (заголовок/описание): одобрить/доработать/отклонить + к итогу."""
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Одобрить", callback_data=RsaCB(action="approve", cid=cid, kind=kind, idx=idx)
    )
    kb.button(
        text="✏️ Доработать", callback_data=RsaCB(action="refine", cid=cid, kind=kind, idx=idx)
    )
    kb.button(
        text="❌ Отклонить", callback_data=RsaCB(action="reject", cid=cid, kind=kind, idx=idx)
    )
    kb.button(text="📋 К итогу", callback_data=RsaCB(action="overview", cid=cid))
    kb.adjust(3, 1)
    return kb.as_markup()


def rsa_overview_kb(cid: str, can_finalize: bool, has_pending: bool = True) -> InlineKeyboardMarkup:
    """Итог курации: массовое одобрение/просмотр по одному (если есть pending),
    создание объявления (если набран минимум ≥3 загол./≥2 опис.), отмена."""
    kb = InlineKeyboardBuilder()
    if has_pending:
        kb.button(
            text="✅ Применить все валидные", callback_data=RsaCB(action="approveall", cid=cid)
        )
        kb.button(text="🔍 Смотреть по одному", callback_data=RsaCB(action="review", cid=cid))
    if can_finalize:
        kb.button(text="➡️ Создать объявление", callback_data=RsaCB(action="finalize", cid=cid))
    kb.button(text="❌ Отмена", callback_data=RsaCB(action="cancel", cid=cid))
    kb.adjust(1)
    return kb.as_markup()


def rsa_pick_campaigns_kb(camps: list[dict]) -> InlineKeyboardMarkup:
    """Визард /rsa: выбор кампании (idx → имя из кэша)."""
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(camps):
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}", callback_data=RsaPickCB(what="camp", idx=i)
        )
    kb.adjust(1)
    return kb.as_markup()


def rsa_pick_adgroups_kb(groups: list[dict]) -> InlineKeyboardMarkup:
    """Визард /rsa: выбор группы объявлений (idx → имя из кэша)."""
    kb = InlineKeyboardBuilder()
    for i, g in enumerate(groups):
        kb.button(text=f"• {_ellipsize(g['name'])}", callback_data=RsaPickCB(what="ag", idx=i))
    kb.adjust(1)
    return kb.as_markup()

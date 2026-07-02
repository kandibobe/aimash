"""Клавиатуры и меню-команды бота Aimash (aiogram 3.x).

Чистый слой представления: НИКАКИХ обращений к Google Ads/БД. Тексты кнопок — здесь,
шаблоны сообщений — в bot/texts.py. Confirm-гейт: кнопки лишь ФОРМИРУЮТ ввод/черновик,
исполнение мутации — только после ✅ через ads.service (см. bot/main.py).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import BotCommand, InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from adcopy.validate import STRUCTURED_SNIPPET_HEADERS
from bot.callbacks import (
    AudienceCB,
    CampCB,
    CcCB,
    ClientCB,
    ConfirmCB,
    ExtCB,
    GeoCB,
    KwAddCB,
    LangCB,
    ModelCB,
    NavCB,
    PeriodCB,
    RecentCB,
    ReportAcctCB,
    ReportCampCB,
    RsaCB,
    RsaPickCB,
    TemplateCB,
    VideoCB,
)

_NAME_LIMIT = 40


def _ellipsize(s: str, limit: int = _NAME_LIMIT) -> str:
    """Обрезать имя с видимым многоточием (иначе непонятно, что текст усечён)."""
    s = s or ""
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _lang(lang: str | None) -> str:
    """Язык клавиатуры: явный lang → он; None → язык текущего запроса (contextvar). i18n
    импортируем ЛЕНИВО — bot.i18n тянет bot.texts, верхнеуровневый импорт ради цикла не нужен."""
    if lang is None:
        from bot import i18n

        return i18n.current_lang()
    return lang if lang in ("ru", "en") else "ru"


# ── Меню-кнопка Telegram (список команд в «/») ──────────────────────────────────
# Показываем только то, что бот реально умеет, + честные пометки «скоро» для фаз 2-3.
BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start", description="Запуск и меню"),
    BotCommand(command="help", description="Что я умею"),
    BotCommand(command="status", description="Статистика аккаунта (30 дн.)"),
    BotCommand(command="campaigns", description="Кампании: список и быстрые действия"),
    BotCommand(command="newcampaign", description="Создание кампании: пошаговый визард (§19)"),
    BotCommand(command="clients", description="ℹ️ Информация про клиентов: профили и сайты (§20)"),
    BotCommand(command="client", description="Карточка клиента: /client <id>"),
    BotCommand(command="pause", description="Пауза кампании: /pause Название"),
    BotCommand(command="resume", description="Возобновить кампанию: /resume Название"),
    BotCommand(command="report", description="Сводка за период (7/30/90/MTD)"),
    BotCommand(command="export", description="Глубокий отчёт .xlsx"),
    BotCommand(command="sheets", description="Глубокий отчёт в Google Sheets (ссылка)"),
    BotCommand(command="mcc", description="Сводка по всем дочерним аккаунтам MCC (§8)"),
    BotCommand(command="account", description="Аккаунт отчётов (чтение): /account <id> | reset"),
    BotCommand(command="refresh", description="Обновить аккаунты/кэши без рестарта"),
    BotCommand(command="quota", description="Дневная квота Google Ads API"),
    BotCommand(command="rsa", description="Сгенерировать тексты объявления (RSA)"),
    BotCommand(command="newsearch", description="Создать поисковую кампанию (RSA + ключи)"),
    BotCommand(command="newvideo", description="Кампания из видео: Demand Gen / Video (YouTube)"),
    BotCommand(command="templates", description="Шаблоны кампаний: список и создание"),
    BotCommand(
        command="savetemplate", description="Сохранить шаблон: /savetemplate имя [from Кампания]"
    ),
    BotCommand(command="recent", description="Недавние действия: повторить"),
    BotCommand(command="cancel", description="Отменить текущий черновик"),
    BotCommand(command="keywords", description="Подбор ключевых слов"),
    BotCommand(command="model", description="Модель ИИ (OpenRouter)"),
    BotCommand(command="balance", description="Бюджет ИИ: баланс OpenRouter и траты"),
    BotCommand(command="journal", description="Журнал изменений (что/когда/кто)"),
    BotCommand(command="diag", description="Журнал ошибок (диагностика)"),
    BotCommand(command="lang", description="Язык интерфейса / interface language"),
]

# EN-вариант меню команд (Telegram отдаёт его клиентам с language_code='en'; RU — дефолтный fallback).
BOT_COMMANDS_EN: list[BotCommand] = [
    BotCommand(command="start", description="Launch and menu"),
    BotCommand(command="help", description="What I can do"),
    BotCommand(command="status", description="Account stats (30 days)"),
    BotCommand(command="campaigns", description="Campaigns: list and quick actions"),
    BotCommand(command="newcampaign", description="Create campaign: step-by-step wizard (§19)"),
    BotCommand(command="clients", description="ℹ️ Client info: profiles and sites (§20)"),
    BotCommand(command="client", description="Client card: /client <id>"),
    BotCommand(command="pause", description="Pause a campaign: /pause Name"),
    BotCommand(command="resume", description="Resume a campaign: /resume Name"),
    BotCommand(command="report", description="Period summary (7/30/90/MTD)"),
    BotCommand(command="export", description="Deep report .xlsx"),
    BotCommand(command="sheets", description="Deep report in Google Sheets (link)"),
    BotCommand(command="mcc", description="All MCC child-accounts summary (§8)"),
    BotCommand(command="account", description="Reports account (read): /account <id> | reset"),
    BotCommand(command="refresh", description="Refresh accounts/caches without a restart"),
    BotCommand(command="quota", description="Google Ads API daily quota"),
    BotCommand(command="rsa", description="Generate ad copy (RSA)"),
    BotCommand(command="newsearch", description="Create a search campaign (RSA + keywords)"),
    BotCommand(command="newvideo", description="Campaign from video: Demand Gen / Video (YouTube)"),
    BotCommand(command="templates", description="Campaign templates: list and create"),
    BotCommand(
        command="savetemplate", description="Save a template: /savetemplate name [from Campaign]"
    ),
    BotCommand(command="recent", description="Recent actions: repeat"),
    BotCommand(command="cancel", description="Cancel the current draft"),
    BotCommand(command="keywords", description="Keyword research"),
    BotCommand(command="model", description="AI model (OpenRouter)"),
    BotCommand(command="balance", description="AI budget: OpenRouter balance and spend"),
    BotCommand(command="journal", description="Change journal (what/when/who)"),
    BotCommand(command="diag", description="Error log (diagnostics)"),
    BotCommand(command="lang", description="Interface language / язык интерфейса"),
]


def lang_kb() -> InlineKeyboardMarkup:
    """Выбор языка интерфейса (RU/EN)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data=LangCB(code="ru"))
    kb.button(text="🇬🇧 English", callback_data=LangCB(code="en"))
    kb.adjust(2)
    return kb.as_markup()


def model_kb(
    choices: list[str],
    active: str | None,
    labels: dict[str, str] | None = None,
    lang: str | None = None,
) -> InlineKeyboardMarkup:
    """Выбор модели ИИ (/model): пресеты (✓ у активной) + своя модель + сброс на дефолт.
    idx указывает на позицию в choices (slug в callback_data не кладём — длинный и с '/').
    labels — дружелюбные подписи slug→текст (неизвестный slug показываем как есть)."""
    labels = labels or {}
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, slug in enumerate(choices):
        mark = "✅ " if slug == active else ""
        text = labels.get(slug) or _ellipsize(slug)
        kb.button(text=f"{mark}{text}", callback_data=ModelCB(action="set", idx=i))
    kb.button(
        text="✏️ Custom model" if en else "✏️ Своя модель",
        callback_data=ModelCB(action="custom"),
    )
    if active is not None:
        kb.button(
            text="↩️ Reset (default)" if en else "↩️ Сбросить (дефолт)",
            callback_data=ModelCB(action="reset"),
        )
    kb.adjust(1)
    return kb.as_markup()


# ── Reply-меню (постоянное нижнее) — навигация/чтение + запуск визардов, НЕ прямые мутации ─
# Мутации (бюджет/ставка/ключи/пауза) идут через /campaigns или свободный текст → confirm-гейт:
# им нужна цель, поэтому прямых кнопок-мутаций тут нет. Кнопки лишь открывают экран/визард.
# Локализация (§4): каждая подпись — {lang: текст}; main_menu(lang) рендерит для языка, а хендлеры
# матчат по BTN_*_ALL (frozenset обоих языков) — иначе EN-пользователь прислал бы EN-подпись, а
# `F.text == BTN_*` (RU-литерал) её не поймал бы → кнопка «мёртвая».
BTN_NEWCAMPAIGN = {"ru": "➕ Создание кампании", "en": "➕ Create campaign"}
BTN_CLIENTS = {"ru": "ℹ️ Клиенты", "en": "ℹ️ Clients"}  # §20: инфо про клиентов (профили/сайты)
BTN_STATUS = {"ru": "📊 Статистика", "en": "📊 Stats"}
BTN_CAMPAIGNS = {"ru": "📋 Кампании", "en": "📋 Campaigns"}
BTN_REPORT = {"ru": "📈 Отчёт", "en": "📈 Report"}
BTN_EXPORT = {"ru": "📄 Экспорт .xlsx", "en": "📄 Export .xlsx"}
BTN_SHEETS = {"ru": "🟢 Sheets", "en": "🟢 Sheets"}
BTN_MCC = {"ru": "🏢 MCC (все аккаунты)", "en": "🏢 MCC (all accounts)"}  # §8: сводка по дочерним
BTN_KEYWORDS = {"ru": "🔑 Ключевые слова", "en": "🔑 Keywords"}
BTN_RSA = {"ru": "✍️ Тексты (RSA)", "en": "✍️ Ad copy (RSA)"}
BTN_MODEL = {"ru": "🧠 Модель", "en": "🧠 Model"}
BTN_BALANCE = {"ru": "💳 Бюджет ИИ", "en": "💳 AI budget"}
BTN_JOURNAL = {"ru": "📜 Журнал", "en": "📜 Journal"}
BTN_LANG = {"ru": "🌐 Язык", "en": "🌐 Language"}
BTN_HELP = {"ru": "❓ Помощь", "en": "❓ Help"}

# Множества всех языковых вариантов для матчинга в хендлерах (F.text.in_(BTN_*_ALL)).
BTN_NEWCAMPAIGN_ALL = frozenset(BTN_NEWCAMPAIGN.values())
BTN_CLIENTS_ALL = frozenset(BTN_CLIENTS.values())
BTN_STATUS_ALL = frozenset(BTN_STATUS.values())
BTN_CAMPAIGNS_ALL = frozenset(BTN_CAMPAIGNS.values())
BTN_REPORT_ALL = frozenset(BTN_REPORT.values())
BTN_EXPORT_ALL = frozenset(BTN_EXPORT.values())
BTN_SHEETS_ALL = frozenset(BTN_SHEETS.values())
BTN_MCC_ALL = frozenset(BTN_MCC.values())
BTN_KEYWORDS_ALL = frozenset(BTN_KEYWORDS.values())
BTN_RSA_ALL = frozenset(BTN_RSA.values())
BTN_MODEL_ALL = frozenset(BTN_MODEL.values())
BTN_BALANCE_ALL = frozenset(BTN_BALANCE.values())
BTN_JOURNAL_ALL = frozenset(BTN_JOURNAL.values())
BTN_LANG_ALL = frozenset(BTN_LANG.values())
BTN_HELP_ALL = frozenset(BTN_HELP.values())


def main_menu(lang: str | None = None) -> ReplyKeyboardMarkup:
    """Полное нижнее меню: все основные функции одним тапом (мутации — через цель в /campaigns)."""
    lng = _lang(lang)
    kb = ReplyKeyboardBuilder()
    for btn in (
        BTN_NEWCAMPAIGN,  # §19: guided-визард создания кампании — отдельной первой строкой
        BTN_CLIENTS,  # §20: информация про клиентов (профили/сайты)
        BTN_STATUS,
        BTN_CAMPAIGNS,
        BTN_REPORT,
        BTN_EXPORT,
        BTN_SHEETS,
        BTN_MCC,  # §8: сводка по всем дочерним аккаунтам MCC
        BTN_KEYWORDS,
        BTN_RSA,
        BTN_MODEL,
        BTN_BALANCE,
        BTN_JOURNAL,
        BTN_LANG,
        BTN_HELP,
    ):
        kb.button(text=btn[lng])
    kb.adjust(1, 1, 2, 3, 3, 2, 3)
    placeholder = "Command or text…" if lng == "en" else "Команда или текст…"
    return kb.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


# ── Inline: универсальная навигация мастеров (Назад + Отмена) ────────────────────
def nav_kb(back_cb: CallbackData | None = None, lang: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки навигации для шага FSM-визарда: «‹ Назад» (если передан back_cb — родительский
    callback flow, breadcrumb) + «✖ Отмена» (всегда NavCB cancel → выход в главное меню).
    back_cb=None → только Отмена (первый шаг без inline-родителя). Чистая функция: никаких
    обращений к БД/Ads. «✖» намеренно ≠ «❌» (последняя — отмена ЧЕРНОВИКА в ConfirmCB/KwAddCB):
    Nav-Отмена = «выйти из мастера», а не «отменить подтверждаемую мутацию»."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if back_cb is not None:
        kb.button(text="‹ Back" if en else "‹ Назад", callback_data=back_cb)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(2 if back_cb is not None else 1)
    return kb.as_markup()


# ── Inline: подтверждение мутации (главный — confirm-гейт) ───────────────────────
def kw_add_kb(token: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """§7: под сводкой keyword-research — предложить ДОБАВИТЬ подобранные ключи в кампанию.
    Кнопка лишь СТАРТУЕТ флоу (кампания → тип соответствия → confirm-гейт), ничего не меняет."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Add keywords to a campaign" if en else "➕ Добавить ключи в кампанию",
        callback_data=KwAddCB(action="start", token=token),
    )
    kb.adjust(1)
    return kb.as_markup()


def match_type_kb(token: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """§7: выбор типа соответствия (broad/phrase/exact) при добавлении ключей. Закрывает зазор
    «тип определяет только LLM» — пользователь выбирает явно. Выбор → черновик add_keywords."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for mt, text in (
        ("broad", "Broad" if en else "Широкое"),
        ("phrase", "Phrase" if en else "Фразовое"),
        ("exact", "Exact" if en else "Точное"),
    ):
        kb.button(text=text, callback_data=KwAddCB(action="match", token=token, mt=mt))
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=KwAddCB(action="cancel", token=token)
    )
    kb.adjust(3, 1)
    return kb.as_markup()


def confirm_kb(cid: str, lang: str | None = None) -> InlineKeyboardMarkup:
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Confirm" if en else "✅ Подтвердить", callback_data=ConfirmCB(action="ok", cid=cid)
    )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=ConfirmCB(action="no", cid=cid)
    )
    kb.adjust(2)
    return kb.as_markup()


# ── §20: «Информация про клиентов» ───────────────────────────────────────────────
def clients_accounts_kb(
    rows: list, with_profile: set[str], lang: str | None = None, page: int = 0
) -> InlineKeyboardMarkup:
    """§20.2: список аккаунтов MCC для выбора клиента, ПОСТРАНИЧНО (B7). У аккаунтов с заполненным
    профилем — ✅. idx — ГЛОБАЛЬНАЯ позиция в _CLI_ACCT_CACHE[chat_id]; customer_id не кладём (как cc)."""
    kb = InlineKeyboardBuilder()
    total = len(rows)
    page, pages, start = _acct_page(total, page)
    shown = 0
    for i in range(start, min(start + _ACCT_PAGE, total)):
        r = rows[i]
        name = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        cid = getattr(r, "id", "")
        mark = "✅ " if cid in with_profile else "▫️ "
        kb.button(text=f"{mark}{name} · {cid}", callback_data=ClientCB(action="acct", idx=i))
        shown += 1
    nav: list[tuple[str, ClientCB]] = []
    if page > 0:
        nav.append(("‹", ClientCB(action="page", sub=str(page - 1))))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", ClientCB(action="page", sub=str(page))))  # индикатор
    if page < pages - 1:
        nav.append(("›", ClientCB(action="page", sub=str(page + 1))))
    for text, cb in nav:
        kb.button(text=text, callback_data=cb)
    sizes = [1] * shown
    if nav:
        sizes.append(len(nav))
    kb.adjust(*(sizes or [1]))
    return kb.as_markup()


def client_card_kb(
    has_profile: bool, has_website: bool = False, lang: str | None = None
) -> InlineKeyboardMarkup:
    """§20.2: кнопки карточки клиента. Есть профиль → Обновить/Очистить (+Перекраулить, если есть
    сайт); нет → Добавить. Краулинг/изменения памяти — фоново/через confirm-гейт (см. bot.main)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if has_profile:
        kb.button(
            text="✏️ Update info" if en else "✏️ Обновить инфу",
            callback_data=ClientCB(action="update"),
        )
        if has_website:
            kb.button(
                text="🔄 Re-crawl (full)" if en else "🔄 Перекраулить полностью",
                callback_data=ClientCB(action="recrawl"),
            )
            kb.button(
                text="🆕 Re-crawl (new only)" if en else "🆕 Перекраулить только новое",
                callback_data=ClientCB(action="recrawl", sub="incr"),
            )
        kb.button(
            text="🗑 Clear profile" if en else "🗑 Очистить профиль",
            callback_data=ClientCB(action="clear"),
        )
    else:
        kb.button(
            text="➕ Add info" if en else "➕ Добавить информацию",
            callback_data=ClientCB(action="add"),
        )
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=ClientCB(action="back"))
    kb.adjust(1)
    return kb.as_markup()


def client_input_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """§20.3: во время приёма текста профиля — «💾 Сохранить» (извлечь+показать «было→станет» и
    confirm) / «✖ Отмена». Менеджер может прислать несколько сообщений подряд до сохранения."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="💾 Save" if en else "💾 Сохранить", callback_data=ClientCB(action="save"))
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


def client_save_kb(cid: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """§20.3: черновик профиля из текста, где указан сайт → предложить «🕷 Сохранить и краулить»
    рядом с «✅ Сохранить как есть». Оба подтверждают ОДИН и тот же save-proposal (confirm-гейт);
    «🕷» дополнительно запускает краулинг ПОСЛЕ сохранения (текст не теряется — краул мёржит поверх
    уже сохранённого профиля). sub несёт confirmation_id (32 hex — влезает в 64 байта callback_data)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Save as is" if en else "✅ Сохранить как есть",
        callback_data=ConfirmCB(action="ok", cid=cid),
    )
    kb.button(
        text="🕷 Save & crawl" if en else "🕷 Сохранить и краулить",
        callback_data=ClientCB(action="save_crawl", sub=cid),
    )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=ConfirmCB(action="no", cid=cid)
    )
    kb.adjust(1)
    return kb.as_markup()


# ── §19: визард «Создание кампании» ──────────────────────────────────────────────
_ACCT_PAGE = (
    8  # аккаунтов на страницу пикера (§19/§20): с запасом под лимит inline-клавиатуры Telegram
)


def _acct_page(total: int, page: int) -> tuple[int, int, int]:
    """Нормализовать номер страницы пикера аккаунтов → (page, pages, start). B7: >100 кнопок не
    влезают в один inline-markup (REPLY_MARKUP_TOO_LONG) — режем на страницы по _ACCT_PAGE."""
    pages = max(1, (total + _ACCT_PAGE - 1) // _ACCT_PAGE)
    page = max(0, min(page, pages - 1))
    return page, pages, page * _ACCT_PAGE


def cc_accounts_kb(rows: list, lang: str | None = None, page: int = 0) -> InlineKeyboardMarkup:
    """Этап 0: выбор аккаунта клиента (read-only превью), ПОСТРАНИЧНО (B7). idx — ГЛОБАЛЬНАЯ позиция в
    _CC_ACCT_CACHE[chat_id]; customer_id в callback_data НЕ кладём. rows — ChildAccount (.name/.id)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    total = len(rows)
    page, pages, start = _acct_page(total, page)
    shown = 0
    for i in range(start, min(start + _ACCT_PAGE, total)):
        r = rows[i]
        name = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        cid = getattr(r, "id", "")
        kb.button(text=f"🏢 {name} · {cid}", callback_data=CcCB(action="acct", idx=i))
        shown += 1
    nav: list[tuple[str, CcCB]] = []
    if page > 0:
        nav.append(("‹", CcCB(action="page", sub=str(page - 1))))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", CcCB(action="page", sub=str(page))))  # индикатор (no-op)
    if page < pages - 1:
        nav.append(("›", CcCB(action="page", sub=str(page + 1))))
    for text, cb in nav:
        kb.button(text=text, callback_data=cb)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    sizes = [1] * shown
    if nav:
        sizes.append(len(nav))
    sizes.append(1)  # ряд Cancel
    kb.adjust(*sizes)
    return kb.as_markup()


def cc_accounts_search_kb(
    rows: list, indices: list[int], lang: str | None = None
) -> InlineKeyboardMarkup:
    """Этап 0 (§19.2 «поиск по названию»): результаты текстового поиска аккаунта. indices —
    ГЛОБАЛЬНЫЕ позиции совпадений в _CC_ACCT_CACHE[chat_id] (→ cc_account_cb работает без
    изменений). Без пагинации: >_ACCT_PAGE совпадений = «уточните запрос» (запрос в callback
    64 байта не влезает — уточнение и есть пагинация)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i in indices[:_ACCT_PAGE]:
        r = rows[i]
        name = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        kb.button(
            text=f"🏢 {name} · {getattr(r, 'id', '')}", callback_data=CcCB(action="acct", idx=i)
        )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(*([1] * min(len(indices), _ACCT_PAGE)), 1)
    return kb.as_markup()


def cc_settings_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 1 (§19.3): ✅ Подтвердить / ✏️ Изменить / ❌ Отмена — как в ТЗ. «Изменить» лишь
    подсказывает формат правки (правка — свободным текстом в состоянии, см. bot.main)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Confirm settings" if en else "✅ Подтвердить настройки",
        callback_data=CcCB(action="accept", sub="settings"),
    )
    kb.button(
        text="✏️ Edit" if en else "✏️ Изменить",
        callback_data=CcCB(action="edit", sub="settings"),
    )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 2)
    return kb.as_markup()


def cc_kw_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 2: «🔎 Генерация» (CcCB kw_generate) или прислать свои ключи текстом/файлом/ссылкой;
    «⏭ Пропустить» / «✖ Отмена»."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔎 Generate keywords" if en else "🔎 Генерация ключевых слов",
        callback_data=CcCB(action="kw_generate"),
    )
    kb.button(text="⏭ Skip" if en else "⏭ Пропустить", callback_data=CcCB(action="skip"))
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 2)
    return kb.as_markup()


def cc_assets_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 5: «✅ Использовать текущие» / «➕ Добавить новый» / «✅ Готово» / «✖ Отмена».
    Готово ведёт к Этапу 6 (URL-опции); добавленные/переиспользованные ассеты — в черновике."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Use current account assets" if en else "✅ Использовать текущие ассеты",
        callback_data=CcCB(action="use_assets"),
    )
    kb.button(
        text="➕ Add a new asset" if en else "➕ Добавить новый ассет",
        callback_data=CcCB(action="add_assets"),
    )
    kb.button(
        text="✅ Done / Skip" if en else "✅ Готово / Пропустить", callback_data=CcCB(action="skip")
    )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 2)
    return kb.as_markup()


# Семейства ассетов с автогенерацией текста (§19.7.2) — подписи для пикера типов.
_CC_ASSET_TYPE_LABELS = {
    "ru": {
        "sitelinks": "🔗 Доп. ссылки (Sitelinks)",
        "callouts": "🏷 Уточнения (Callouts)",
        "structured_snippets": "📑 Структурные описания",
        "business_name": "🏢 Название бизнеса",
        "business_logo": "🖼 Логотип (Business logo)",  # §19.7.1: фото 1:1 → BUSINESS_LOGO
        # §19.7.2: ФАКТ-семейства — из профиля клиента (§20), без выдумывания.
        "call": "📞 Телефон (Call)",
        "price": "💲 Цены (Price)",
        "promotion": "🎁 Акция (Promotion)",
    },
    "en": {
        "sitelinks": "🔗 Sitelinks",
        "callouts": "🏷 Callouts",
        "structured_snippets": "📑 Structured snippets",
        "business_name": "🏢 Business name",
        "business_logo": "🖼 Business logo",
        "call": "📞 Call (phone)",
        "price": "💲 Price",
        "promotion": "🎁 Promotion",
    },
}


def cc_asset_types_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 5: выбор типа нового ассета для автогенерации (§19.7.2)."""
    lng = _lang(lang)
    kb = InlineKeyboardBuilder()
    for fam, label in _CC_ASSET_TYPE_LABELS[lng].items():
        kb.button(text=label, callback_data=CcCB(action="asset_type", sub=fam))
    kb.button(text="‹ Back" if lng == "en" else "‹ Назад", callback_data=CcCB(action="assets_back"))
    kb.adjust(1)
    return kb.as_markup()


def cc_final_kb(
    can_launch: bool = False, lang: str | None = None, *, launch_cid: str = ""
) -> InlineKeyboardMarkup:
    """Этап 7: «✅ Создать черновик» (CcCB create) + «🚀 Запустить» (CcCB launch, только после
    создания) + «✖ Отмена». Правка — свободным текстом в состоянии. launch_cid — confirmation_id
    применённого create-proposal: кнопка запуска резолвится из БД и переживает рестарт
    (32-hex в sub: «cc:launch:-1:<hex>» ≈ 45 байт < лимита 64)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if can_launch:
        kb.button(
            text="🚀 Launch campaign" if en else "🚀 Запустить кампанию",
            callback_data=CcCB(action="launch", sub=launch_cid),
        )
    else:
        kb.button(
            text="✅ Create draft" if en else "✅ Создать черновик",
            callback_data=CcCB(action="create"),
        )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1)
    return kb.as_markup()


def cc_skip_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 4/6: «⏭ Пропустить» (CcCB skip) + «✖ Отмена». Прикрепление (фото) — отдельным
    сообщением, не кнопкой."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Skip" if en else "⏭ Пропустить", callback_data=CcCB(action="skip"))
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


def cc_kw_confirm_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """§19.4: явный гейт «✅ Подтвердить ключевые слова» перед Этапом 3. Замена списка — просто
    прислать новый (state остаётся на Этапе 2); «✖ Отмена» — выход из визарда."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Confirm keywords" if en else "✅ Подтвердить ключевые слова",
        callback_data=CcCB(action="kw_confirm"),
    )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1)
    return kb.as_markup()


def video_type_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """§11: выбор типа кампании из видео — Demand Gen (рекомендуется) или Video (охват, CPM).
    Кнопки лишь двигают визард; мутация — только через confirm-гейт."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🎯 Demand Gen (recommended)" if en else "🎯 Demand Gen (рекомендую)",
        callback_data=VideoCB(action="dg"),
    )
    kb.button(
        text="▶️ Video (reach, CPM)" if en else "▶️ Video (охват, CPM)",
        callback_data=VideoCB(action="video"),
    )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def video_logo_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """§11 Demand Gen: логотип (опц.) — прислать фото или пропустить."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Skip" if en else "⏭ Пропустить", callback_data=VideoCB(action="logo_skip"))
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


def cc_resume_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Вход в визард при наличии незавершённого черновика: продолжить / начать заново / выйти."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Resume" if en else "▶️ Продолжить", callback_data=CcCB(action="resume"))
    kb.button(text="🆕 Start over" if en else "🆕 Начать заново", callback_data=CcCB(action="new"))
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(2, 1)
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


def campaign_actions_kb(idx: int, status: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """Действия для одной кампании. pause/resume зависят от статуса; мутации идут через
    confirm-гейт (кнопка лишь создаёт черновик, не исполняет)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if status == "ENABLED":
        kb.button(
            text="⏸ Pause" if en else "⏸ Поставить на паузу",
            callback_data=CampCB(action="pause", idx=idx),
        )
    elif status == "PAUSED":
        kb.button(
            text="▶️ Resume" if en else "▶️ Возобновить",
            callback_data=CampCB(action="resume", idx=idx),
        )
    kb.button(
        text="🎯 Audiences" if en else "🎯 Аудитории",
        callback_data=CampCB(action="audience", idx=idx),
    )
    kb.button(
        text="📍 Geo targeting" if en else "📍 Гео-таргетинг",
        callback_data=CampCB(action="geo", idx=idx),
    )
    kb.button(
        text="🧩 Extensions" if en else "🧩 Расширения",
        callback_data=CampCB(action="ext", idx=idx),
    )
    kb.button(
        text="‹ Back to list" if en else "‹ Назад к списку",
        callback_data=CampCB(action="back", idx=idx),
    )
    kb.adjust(1)
    return kb.as_markup()


def ext_menu_kb(idx: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """§3-assets: меню расширений кампании (sitelinks/callouts/structured snippets/показать) +
    «‹ Назад» в меню кампании (breadcrumb через CampCB menu). Выбор типа → ввод текста → черновик."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔗 Sitelinks" if en else "🔗 Быстрые ссылки",
        callback_data=ExtCB(action="sitelink", idx=idx),
    )
    kb.button(
        text="🏷 Callouts" if en else "🏷 Уточнения",
        callback_data=ExtCB(action="callout", idx=idx),
    )
    kb.button(
        text="📑 Structured snippets" if en else "📑 Структурные описания",
        callback_data=ExtCB(action="snippet", idx=idx),
    )
    kb.button(
        text="🖼 Image" if en else "🖼 Изображение",
        callback_data=ExtCB(action="image", idx=idx),
    )
    kb.button(
        text="👁 Show current" if en else "👁 Показать текущие",
        callback_data=ExtCB(action="show", idx=idx),
    )
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=CampCB(action="menu", idx=idx))
    kb.adjust(1)
    return kb.as_markup()


def ext_snippet_header_kb(idx: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """§3-assets: выбор канонического header структурного описания (кнопками — иначе HEADER_NOT_FOUND).
    После выбора — ввод значений текстом. «‹ Назад» возвращает к меню расширений."""
    kb = InlineKeyboardBuilder()
    for h in sorted(STRUCTURED_SNIPPET_HEADERS):
        kb.button(text=h, callback_data=ExtCB(action="snip_h", idx=idx, sub=h))
    kb.button(
        text="‹ Back" if _lang(lang) == "en" else "‹ Назад",
        callback_data=ExtCB(action="snippet", idx=idx),
    )
    kb.adjust(2)
    return kb.as_markup()


def ext_assets_list_kb(rows: list, camp_idx: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """§3-assets: текущие расширения кампании с кнопками удаления (🗑 idx). idx — строка в
    _EXT_CACHE[chat_id]; «‹ Назад» — к меню расширений кампании (camp_idx в _CAMP_CACHE)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows):
        kb.button(
            text=f"🗑 {_ellipsize(getattr(r, 'label', '') or getattr(r, 'field_type', ''))}",
            callback_data=ExtCB(action="remove", idx=i),
        )
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=CampCB(action="ext", idx=camp_idx))
    kb.adjust(1)
    return kb.as_markup()


def geo_mode_kb(idx: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """§3: выбор способа гео-таргетинга кампании. «По локации» (страна/город/регион через
    geoTargetConstants) или «Радиус вокруг точки» (proximity). Кнопка лишь выбирает способ —
    адрес/локации вводятся текстом, черновик собирается после ввода (confirm-гейт). idx — кампания
    в _CAMP_CACHE (резолв по chat_id в bot.main)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🌍 By location (city/region)" if en else "🌍 По локации (город/регион)",
        callback_data=GeoCB(action="loc", idx=idx),
    )
    kb.button(
        text="📍 Radius around a point" if en else "📍 Радиус вокруг точки",
        callback_data=GeoCB(action="prox", idx=idx),
    )
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=CampCB(action="menu", idx=idx))
    kb.adjust(1)
    return kb.as_markup()


def audiences_kb(auds: list, camp_idx: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """Выбор аудитории для прикрепления к кампании (§3). idx — позиция в списке аудиторий;
    camp_idx ведёт прикрепление к конкретной кампании и кнопку «назад» — к её меню."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, a in enumerate(auds):
        size = getattr(a, "size", 0) or 0
        suffix = f" · {size:,}".replace(",", " ") if size else ""
        kb.button(
            text=f"👥 {_ellipsize(a.name)}{suffix}",
            callback_data=AudienceCB(action="pick", camp_idx=camp_idx, idx=i),
        )
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=CampCB(action="menu", idx=camp_idx))
    kb.adjust(1)
    return kb.as_markup()


# ── Inline: именованные шаблоны кампаний (§2B) ───────────────────────────────────
def templates_kb(rows: list, lang: str | None = None) -> InlineKeyboardMarkup:
    """Список шаблонов: на каждый — «использовать» (создать кампанию из шаблона) и «удалить».
    idx = позиция в _TPL_CACHE[chat_id] (имя в callback_data не кладём). rows — TemplateRow."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows):
        kb.button(text=f"📋 {_ellipsize(r.name)}", callback_data=TemplateCB(action="use", idx=i))
        kb.button(text="🗑" if en else "🗑", callback_data=TemplateCB(action="del", idx=i))
    kb.adjust(2)
    return kb.as_markup()


# ── Inline: авто-память — повтор недавних действий (§2C) ─────────────────────────
def recent_kb(rows: list, lang: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки «↻ N» для повтора недавних применённых действий. idx = позиция в _RECENT_CACHE."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, _ in enumerate(rows):
        kb.button(
            text=f"↻ {i + 1}" if en else f"↻ {i + 1}",
            callback_data=RecentCB(action="repeat", idx=i),
        )
    kb.adjust(5)
    return kb.as_markup()


# ── Inline: выбор периода (отчёт) ────────────────────────────────────────────────
def period_kb(target: str, lang: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if _lang(lang) == "en":
        items = [("7 days", "7"), ("30 days", "30"), ("90 days", "90"), ("MTD", "MTD")]
    else:
        items = [("7 дней", "7"), ("30 дней", "30"), ("90 дней", "90"), ("MTD", "MTD")]
    for label, code in items:
        kb.button(text=label, callback_data=PeriodCB(target=target, code=code))
    kb.adjust(2, 2)
    return kb.as_markup()


def report_accounts_kb(rows: list, target: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """§8: выбор аккаунта для отчёта/экспорта. rows — ChildAccount-подобные (.name/.id/.currency);
    idx → позиция в _REPORT_ACCT_CACHE[chat_id] (customer_id в callback_data НЕ кладём). target —
    какой поток (report|export|sheets), чтобы после выбора продолжить именно его."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows):
        name = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        cid = getattr(r, "id", "")
        cur = getattr(r, "currency", "") or ""
        suffix = f" · {cur}" if cur else ""
        kb.button(
            text=f"🏢 {name} · {cid}{suffix}", callback_data=ReportAcctCB(target=target, idx=i)
        )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def report_campaigns_kb(
    camps: list[dict], target: str, lang: str | None = None
) -> InlineKeyboardMarkup:
    """§9: «Весь аккаунт» (idx=-1) + список кампаний (idx → _REPORT_CAMP_CACHE[chat_id]). Маркер
    статуса нейтрален (данные не переводятся). Следующий шаг — period_kb(target)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Whole account" if en else "📊 Весь аккаунт",
        callback_data=ReportCampCB(target=target, idx=-1),
    )
    for i, c in enumerate(camps):
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}",
            callback_data=ReportCampCB(target=target, idx=i),
        )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


# ── RSA-курация (ТЗ §10): поэлементное подтверждение + массовые действия ──────────
def _rsa_batch_row(kb: InlineKeyboardBuilder, cid: str, en: bool) -> None:
    """§19.5.2 (визард): батч-ряд «Доработать всё | Сгенерировать заново | Утвердить набор».
    editall → list-UX правка; regen → новый набор в pending; aslist → одобрить всё валидное."""
    kb.button(
        text="✏️ Edit all as list" if en else "✏️ Доработать всё",
        callback_data=RsaCB(action="editall", cid=cid),
    )
    kb.button(
        text="🔁 Regenerate" if en else "🔁 Сгенерировать заново",
        callback_data=RsaCB(action="regen", cid=cid),
    )
    kb.button(
        text="✅ Approve the set" if en else "✅ Утвердить набор",
        callback_data=RsaCB(action="aslist", cid=cid),
    )


def rsa_item_kb(
    cid: str, kind: str, idx: int, lang: str | None = None, *, wizard: bool = False
) -> InlineKeyboardMarkup:
    """Кнопки одного элемента (заголовок/описание): одобрить/доработать/отклонить + к итогу.
    wizard=True (§19.5.2, сессия визарда /newcampaign) — дополнительно батч-ряд."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Approve" if en else "✅ Одобрить",
        callback_data=RsaCB(action="approve", cid=cid, kind=kind, idx=idx),
    )
    kb.button(
        text="✏️ Refine" if en else "✏️ Доработать",
        callback_data=RsaCB(action="refine", cid=cid, kind=kind, idx=idx),
    )
    kb.button(
        text="❌ Reject" if en else "❌ Отклонить",
        callback_data=RsaCB(action="reject", cid=cid, kind=kind, idx=idx),
    )
    kb.button(
        text="📋 To summary" if en else "📋 К итогу",
        callback_data=RsaCB(action="overview", cid=cid),
    )
    if wizard:
        _rsa_batch_row(kb, cid, en)
        kb.adjust(3, 1, 1, 1, 1)
    else:
        kb.adjust(3, 1)
    return kb.as_markup()


def rsa_aslist_kb(cid: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """List-UX (§10): под редактируемым списком — «✅ Использовать как есть» (одобрить всё валидное
    без правок) и «❌ Отмена». Правка — присылается ТЕКСТОМ обратно (rsa_list_edited)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Use as is" if en else "✅ Использовать как есть",
        callback_data=RsaCB(action="aslist", cid=cid),
    )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=RsaCB(action="cancel", cid=cid)
    )
    kb.adjust(1)
    return kb.as_markup()


def rsa_overview_kb(
    cid: str,
    can_finalize: bool,
    has_pending: bool = True,
    lang: str | None = None,
    *,
    wizard: bool = False,
) -> InlineKeyboardMarkup:
    """Итог курации: массовое одобрение/просмотр по одному (если есть pending),
    создание объявления (если набран минимум ≥3 загол./≥2 опис.), отмена.
    wizard=True (§19.5.2) — батч-ряд вместо «Создать объявление» (finalize визарда = «Утвердить
    набор», мутации нет — утверждённые тексты уходят в черновик кампании)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if has_pending:
        kb.button(
            text="✅ Apply all valid" if en else "✅ Применить все валидные",
            callback_data=RsaCB(action="approveall", cid=cid),
        )
        kb.button(
            text="🔍 Review one by one" if en else "🔍 Смотреть по одному",
            callback_data=RsaCB(action="review", cid=cid),
        )
    if wizard:
        _rsa_batch_row(kb, cid, en)
    elif can_finalize:
        kb.button(
            text="➡️ Create ad" if en else "➡️ Создать объявление",
            callback_data=RsaCB(action="finalize", cid=cid),
        )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=RsaCB(action="cancel", cid=cid)
    )
    kb.adjust(1)
    return kb.as_markup()


def rsa_pick_campaigns_kb(camps: list[dict], lang: str | None = None) -> InlineKeyboardMarkup:
    """Визард /rsa: выбор кампании (idx → имя из кэша). lang принимаем для единообразия
    (подписи кампаний — данные, не переводятся; маркер статуса нейтрален)."""
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(camps):
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}", callback_data=RsaPickCB(what="camp", idx=i)
        )
    kb.adjust(1)
    return kb.as_markup()


def rsa_pick_adgroups_kb(groups: list[dict], lang: str | None = None) -> InlineKeyboardMarkup:
    """Визард /rsa: выбор группы объявлений (idx → имя из кэша). lang — для единообразия сигнатур."""
    kb = InlineKeyboardBuilder()
    for i, g in enumerate(groups):
        kb.button(text=f"• {_ellipsize(g['name'])}", callback_data=RsaPickCB(what="ag", idx=i))
    kb.adjust(1)
    return kb.as_markup()

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
    AdminCB,
    AdviseCB,
    AlertCB,
    AudienceCB,
    BugCB,
    CampCB,
    CcCB,
    ClientCB,
    ConfirmCB,
    DiagCB,
    ExtCB,
    GeoCB,
    JournalRollbackCB,
    KwAddCB,
    KwCfgCB,
    LangCB,
    ModelCB,
    MoreCB,
    MySchedCB,
    NavCB,
    PageCB,
    PeriodCB,
    PickSearchCB,
    RecentCB,
    ReportAcctCB,
    ReportCampCB,
    RollbackCB,
    RsaCB,
    RsaPickCB,
    SearchTermsCB,
    SlashMutCB,
    TemplateCB,
    ThrTuneCB,
    VideoCB,
)

_NAME_LIMIT = 40


def searchterms_kb(items: list[dict], gen: int) -> InlineKeyboardMarkup:
    """§7: клавиатура /searchterms — по кнопке «🚫 <запрос>» на каждый «мусорный» запрос (idx+gen
    анти-stale) + «Закрыть». Клик минтит черновик add_negative_keywords (confirm-гейт), сам SDK —
    только после «да». Имя запроса в callback_data НЕ кладём (idx резолвится по chat_id в bot.main)."""
    from bot import i18n

    b = InlineKeyboardBuilder()
    for i, it in enumerate(items):
        term = _ellipsize(str(it.get("term") or ""))
        b.button(text=f"🚫 {term}", callback_data=SearchTermsCB(action="neg", idx=i, gen=gen))
    b.button(text=i18n.t("searchterms_cancel_btn"), callback_data=SearchTermsCB(action="cancel"))
    b.adjust(1)
    return b.as_markup()


def harvest_kb(items: list[dict], gen: int) -> InlineKeyboardMarkup:
    """Ф4 «сбор урожая»: по кнопке «➕ <запрос>» на каждый конвертящий запрос без своего ключа.
    Клик минтит черновик add_keywords (EXACT, в ТУ группу, где запрос уже крутился) — исполнение
    только после «да». gen общий со списком «в минус»: обе клавиатуры живут одно поколение."""
    from bot import i18n

    b = InlineKeyboardBuilder()
    for i, it in enumerate(items):
        term = _ellipsize(str(it.get("term") or ""))
        b.button(text=f"➕ {term}", callback_data=SearchTermsCB(action="add", idx=i, gen=gen))
    b.button(text=i18n.t("searchterms_cancel_btn"), callback_data=SearchTermsCB(action="cancel"))
    b.adjust(1)
    return b.as_markup()


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
    BotCommand(command="newcampaign", description="Создание кампании: пошаговый визард"),
    BotCommand(command="clients", description="ℹ️ Информация про клиентов: профили и сайты"),
    BotCommand(command="client", description="Карточка клиента: /client <id>"),
    BotCommand(command="pause", description="Пауза кампании: /pause Название"),
    BotCommand(command="resume", description="Возобновить кампанию: /resume Название"),
    BotCommand(command="report", description="Сводка за период (7/30/90/MTD)"),
    BotCommand(command="export", description="Глубокий отчёт .xlsx"),
    BotCommand(command="sheets", description="Глубокий отчёт в Google Sheets (ссылка)"),
    BotCommand(command="mysheets", description="Мои Google-таблицы: ссылки на отчёты и ключи"),
    BotCommand(command="mcc", description="Сводка по всем дочерним аккаунтам MCC"),
    BotCommand(command="account", description="Аккаунт отчётов (чтение): /account <id> | reset"),
    BotCommand(command="accounts", description="Мои доступные аккаунты (чтение)"),
    BotCommand(command="whoami", description="Мой chat_id, активный аккаунт, режим доступа"),
    BotCommand(command="refresh", description="Обновить аккаунты/кэши без рестарта"),
    BotCommand(command="quota", description="Дневная квота Google Ads API"),
    BotCommand(command="advise", description="💡 Рекомендации по улучшению аккаунта"),
    BotCommand(command="audit", description="🩺 Аудит аккаунта: оценка 0-100 + что чинить"),
    BotCommand(command="bids", description="📈 Возможности по ставкам: какие ключи поднять"),
    BotCommand(command="competitors", description="🥊 Конкуренты: импорт CSV статистики аукционов"),
    BotCommand(command="target", description="🎯 Целевой CPA аккаунта (для правила 3× в /audit)"),
    BotCommand(command="alerts", description="Пороги алертов аномалий (расход/конверсии)"),
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
    BotCommand(command="addkeys", description="Добавить ключи в кампанию (файл/ссылка/текст)"),
    BotCommand(command="searchterms", description="Мусорные поисковые запросы → минус-слова"),
    BotCommand(command="model", description="Модель ИИ (OpenRouter)"),
    BotCommand(command="balance", description="Бюджет ИИ: баланс OpenRouter и траты"),
    BotCommand(command="journal", description="Журнал изменений (что/когда/кто)"),
    BotCommand(command="diag", description="Журнал ошибок (диагностика)"),
    BotCommand(command="reportbug", description="🐞 Сообщить об ошибке"),
    BotCommand(command="lang", description="Язык интерфейса / interface language"),
]

# EN-вариант меню команд (Telegram отдаёт его клиентам с language_code='en'; RU — дефолтный fallback).
BOT_COMMANDS_EN: list[BotCommand] = [
    BotCommand(command="start", description="Launch and menu"),
    BotCommand(command="help", description="What I can do"),
    BotCommand(command="status", description="Account stats (30 days)"),
    BotCommand(command="campaigns", description="Campaigns: list and quick actions"),
    BotCommand(command="newcampaign", description="Create campaign: step-by-step wizard"),
    BotCommand(command="clients", description="ℹ️ Client info: profiles and sites"),
    BotCommand(command="client", description="Client card: /client <id>"),
    BotCommand(command="pause", description="Pause a campaign: /pause Name"),
    BotCommand(command="resume", description="Resume a campaign: /resume Name"),
    BotCommand(command="report", description="Period summary (7/30/90/MTD)"),
    BotCommand(command="export", description="Deep report .xlsx"),
    BotCommand(command="sheets", description="Deep report in Google Sheets (link)"),
    BotCommand(command="mysheets", description="My Google Sheets: report and keyword links"),
    BotCommand(command="mcc", description="All MCC child-accounts summary"),
    BotCommand(command="account", description="Reports account (read): /account <id> | reset"),
    BotCommand(command="accounts", description="My accessible accounts (read)"),
    BotCommand(command="whoami", description="My chat_id, active account, access mode"),
    BotCommand(command="refresh", description="Refresh accounts/caches without a restart"),
    BotCommand(command="quota", description="Google Ads API daily quota"),
    BotCommand(command="advise", description="💡 Recommendations to improve the account"),
    BotCommand(command="audit", description="🩺 Account audit: 0-100 score + what to fix"),
    BotCommand(command="bids", description="📈 Bid opportunities: which keywords to raise"),
    BotCommand(command="competitors", description="🥊 Competitors: import auction insights CSV"),
    BotCommand(command="target", description="🎯 Account target CPA (for the 3× rule in /audit)"),
    BotCommand(command="alerts", description="Anomaly alert thresholds (spend/conversions)"),
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
    BotCommand(command="addkeys", description="Add keywords to a campaign (file/link/text)"),
    BotCommand(command="model", description="AI model (OpenRouter)"),
    BotCommand(command="balance", description="AI budget: OpenRouter balance and spend"),
    BotCommand(command="journal", description="Change journal (what/when/who)"),
    BotCommand(command="diag", description="Error log (diagnostics)"),
    BotCommand(command="reportbug", description="🐞 Report a bug"),
    BotCommand(command="lang", description="Interface language / язык интерфейса"),
]


def lang_kb() -> InlineKeyboardMarkup:
    """Выбор языка интерфейса (RU/EN)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 Русский", callback_data=LangCB(code="ru"))
    kb.button(text="🇬🇧 English", callback_data=LangCB(code="en"))
    kb.adjust(2)
    return kb.as_markup()


def adduser_access_kb(target_chat: int, lang: str | None = None) -> InlineKeyboardMarkup:
    """P0-A: после /adduser — выбор объёма доступа ЧТЕНИЯ для нового оператора (решение заказчика:
    «Все аккаунты / Выбрать аккаунты»). Кнопки лишь маршрутизируют (AdminCB) — гранты выдаёт
    хендлер; мутации этим НЕ открываются. Чистая презентация: список аккаунтов тянет хендлер."""
    from bot import i18n

    kb = InlineKeyboardBuilder()
    kb.button(
        text=i18n.t("adduser_btn_all", lang), callback_data=AdminCB(action="all", chat=target_chat)
    )
    kb.button(
        text=i18n.t("adduser_btn_pick", lang),
        callback_data=AdminCB(action="pick", chat=target_chat),
    )
    kb.button(
        text=i18n.t("adduser_btn_none", lang),
        callback_data=AdminCB(action="done", chat=target_chat),
    )
    kb.adjust(2, 1)
    return kb.as_markup()


def adduser_pick_kb(
    target_chat: int,
    accounts: list[tuple[str, str]],
    granted: set[str],
    lang: str | None = None,
    *,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """P0-A «Выбрать аккаунты»: тап-тогл гранта чтения аккаунта оператору (✅ = выдан). accounts —
    [(customer_id, name)] обнаруженных дочерних; granted — уже выданные оператору id. Постранично
    (защита от REPLY_MARKUP_TOO_LONG на больших MCC, как report_accounts_kb). Резолв — stateless:
    customer_id в самом callback_data (AdminCB.cid). «Готово» завершает."""
    from bot import i18n

    kb = InlineKeyboardBuilder()
    total = len(accounts)
    page, pages, start = _acct_page(total, page)
    for cid, name in accounts[start : start + _ACCT_PAGE]:
        mark = "✅ " if cid in granted else "▫️ "
        label = _ellipsize(f"{name} · {cid}" if name and name != cid else cid)
        kb.button(
            text=f"{mark}{label}", callback_data=AdminCB(action="grant", chat=target_chat, cid=cid)
        )
    kb.adjust(1)
    if pages > 1:
        nav = InlineKeyboardBuilder()
        if page > 0:
            nav.button(
                text="‹", callback_data=AdminCB(action="pick", chat=target_chat, cid=f"p{page - 1}")
            )
        nav.button(
            text=f"{page + 1}/{pages}",
            callback_data=AdminCB(action="pick", chat=target_chat, cid=f"p{page}"),
        )
        if page < pages - 1:
            nav.button(
                text="›", callback_data=AdminCB(action="pick", chat=target_chat, cid=f"p{page + 1}")
            )
        nav.adjust(3)
        kb.attach(nav)
    done = InlineKeyboardBuilder()
    done.button(
        text=i18n.t("adduser_btn_done", lang),
        callback_data=AdminCB(action="done", chat=target_chat),
    )
    done.adjust(1)
    kb.attach(done)
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
BTN_MORE = {"ru": "➕ Ещё", "en": "➕ More"}  # 3E: хаб вторичных флоу (обнаружимость)

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
BTN_MORE_ALL = frozenset(BTN_MORE.values())

# 3A: ВСЕ подписи кнопок главного меню (оба языка) — для гарда menu_guard: кнопка меню,
# нажатая во время активного визарда, не должна «съедаться» state-хендлером как ввод.
ALL_MENU_BUTTONS: frozenset[str] = (
    BTN_MORE_ALL
    | BTN_NEWCAMPAIGN_ALL
    | BTN_CLIENTS_ALL
    | BTN_STATUS_ALL
    | BTN_CAMPAIGNS_ALL
    | BTN_REPORT_ALL
    | BTN_EXPORT_ALL
    | BTN_SHEETS_ALL
    | BTN_MCC_ALL
    | BTN_KEYWORDS_ALL
    | BTN_RSA_ALL
    | BTN_MODEL_ALL
    | BTN_BALANCE_ALL
    | BTN_JOURNAL_ALL
    | BTN_LANG_ALL
    | BTN_HELP_ALL
)


def main_menu(lang: str | None = None) -> ReplyKeyboardMarkup:
    """Полное нижнее меню: все основные функции одним тапом (мутации — через цель в /campaigns).

    D1: кнопки сгруппированы по смыслу построчно (у reply-клавиатуры нет заголовков секций —
    группируем раскладкой adjust): создание/управление · быстрая статистика · отчёты · инструменты ·
    настройки · служебное. Порядок логичнее «плоской» сетки; набор кнопок и BTN_*_ALL не меняются
    (хендлеры матчат по тексту, а не по позиции)."""
    lng = _lang(lang)
    kb = ReplyKeyboardBuilder()
    for btn in (
        # ── создание/управление ──
        BTN_NEWCAMPAIGN,  # §19: guided-визард создания кампании
        BTN_CAMPAIGNS,  # список кампаний + быстрые действия
        # ── быстрый обзор ──
        BTN_STATUS,  # статистика за 30 дней (полная строка)
        # ── отчёты/экспорт ──
        BTN_REPORT,
        BTN_EXPORT,
        BTN_SHEETS,
        BTN_MCC,  # §8: сводка по всем дочерним аккаунтам MCC
        # ── инструменты ──
        BTN_KEYWORDS,
        BTN_RSA,
        BTN_CLIENTS,  # §20: информация про клиентов (профили/сайты)
        # ── настройки ИИ ──
        BTN_MODEL,
        BTN_BALANCE,
        # ── служебное ──
        BTN_JOURNAL,
        BTN_LANG,
        BTN_HELP,
        BTN_MORE,  # 3E: хаб вторичных флоу (/newsearch /newvideo /templates /recent /quota /alerts)
    ):
        kb.button(text=btn[lng])
    kb.adjust(
        2, 1, 4, 3, 2, 4
    )  # создание · статистика · отчёты · инструменты · настройки · служебное
    placeholder = "Command or text…" if lng == "en" else "Команда или текст…"
    return kb.as_markup(
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=placeholder,
    )


# 3F (§7): частые страны для пикера ГЕО подбора ключей (ISO → подпись). «Все страны» и ручной
# ввод — отдельными кнопками; полный резолв произвольной страны — ads.geo.country_iso.
_KW_GEO_PRESETS: tuple[tuple[str, str, str], ...] = (
    ("UA", "🇺🇦 Украина", "🇺🇦 Ukraine"),
    ("KE", "🇰🇪 Кения", "🇰🇪 Kenya"),
    ("PL", "🇵🇱 Польша", "🇵🇱 Poland"),
    ("DE", "🇩🇪 Германия", "🇩🇪 Germany"),
    ("US", "🇺🇸 США", "🇺🇸 USA"),
    ("KZ", "🇰🇿 Казахстан", "🇰🇿 Kazakhstan"),
)


def kw_params_kb(cfg: dict, lang: str | None = None) -> InlineKeyboardMarkup:
    """3F (§7): экран параметров research. cfg — state-data ({kw_geo_iso, kw_lang, kw_net,
    kw_months}). Тапы по строкам циклят/открывают выбор; «🚀 Подобрать» — запуск."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    # A1: показываемый дефолт гео — из settings.geo_default_country (пусто ⇒ «any»), без хардкода «UA».
    from core.config import settings

    geo = cfg.get("kw_geo_iso") or (settings.geo_default_country or "any")
    geo_label = {"any": "🌐 " + ("all countries" if en else "все страны")}.get(geo, geo)
    kb.button(
        text=("🌍 Geo: " if en else "🌍 ГЕО: ") + str(geo_label),
        callback_data=KwCfgCB(field="geo"),
    )
    lang_v = str(cfg.get("kw_lang") or "ru")
    lang_label = ("🌐 " + ("any" if en else "любой")) if lang_v == "any" else lang_v
    kb.button(
        text=("🗣 Language: " if en else "🗣 Язык: ") + lang_label,
        callback_data=KwCfgCB(field="lang"),
    )
    net = cfg.get("kw_net") or "GOOGLE_SEARCH"
    net_label = (
        ("Search + partners" if en else "Поиск + партнёры")
        if net == "GOOGLE_SEARCH_AND_PARTNERS"
        else "Search"
    )
    kb.button(
        text=("🔎 Network: " if en else "🔎 Сеть: ") + net_label, callback_data=KwCfgCB(field="net")
    )
    months = cfg.get("kw_months") or 0
    period_label = (
        ("auto" if en else "авто") if not months else f"{months} " + ("mo" if en else "мес")
    )
    kb.button(
        text=("📅 Period: " if en else "📅 Период: ") + period_label,
        callback_data=KwCfgCB(field="period"),
    )
    kb.button(
        text="🚀 " + ("Search ideas" if en else "Подобрать"), callback_data=KwCfgCB(field="run")
    )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 1, 1, 2)
    return kb.as_markup()


def kw_geo_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """3F: саб-пикер ГЕО — частые страны + «все страны» + ручной ввод."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for iso, ru_l, en_l in _KW_GEO_PRESETS:
        kb.button(text=en_l if en else ru_l, callback_data=KwCfgCB(field="geo_pick", value=iso))
    kb.button(
        text="🌐 " + ("All countries" if en else "Все страны"),
        callback_data=KwCfgCB(field="geo_pick", value="any"),
    )
    kb.button(
        text="✏️ " + ("Other…" if en else "Другая…"),
        callback_data=KwCfgCB(field="geo_pick", value="custom"),
    )
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


# §7: частые языки подбора (ISO 639-1, ru_label, en_label). Любой другой — через «✏️ Другой…».
_KW_LANG_PRESETS: tuple[tuple[str, str, str], ...] = (
    ("ru", "🇷🇺 Русский", "🇷🇺 Russian"),
    ("uk", "🇺🇦 Українська", "🇺🇦 Ukrainian"),
    ("en", "🇬🇧 English", "🇬🇧 English"),
    ("de", "🇩🇪 Немецкий", "🇩🇪 German"),
    ("pl", "🇵🇱 Польский", "🇵🇱 Polish"),
    ("es", "🇪🇸 Испанский", "🇪🇸 Spanish"),
    ("fr", "🇫🇷 Французский", "🇫🇷 French"),
    ("tr", "🇹🇷 Турецкий", "🇹🇷 Turkish"),
    ("it", "🇮🇹 Итальянский", "🇮🇹 Italian"),
    ("ar", "🇸🇦 Арабский", "🇸🇦 Arabic"),
)


def kw_lang_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """3F (§7): саб-пикер ЯЗЫКА подбора — частые языки + «🌐 Любой» (не фильтровать) + ручной ввод.
    Зеркало kw_geo_kb; ЛЮБОЙ язык из таблицы Google достижим через «✏️ Другой…»."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for iso, ru_l, en_l in _KW_LANG_PRESETS:
        kb.button(text=en_l if en else ru_l, callback_data=KwCfgCB(field="lang_pick", value=iso))
    kb.button(
        text="🌐 " + ("Any language" if en else "Любой язык"),
        callback_data=KwCfgCB(field="lang_pick", value="any"),
    )
    kb.button(
        text="✏️ " + ("Other…" if en else "Другой…"),
        callback_data=KwCfgCB(field="lang_pick", value="custom"),
    )
    kb.adjust(2, 2, 2, 2, 2, 2)
    return kb.as_markup()


def alerts_kb(cur: dict, lang: str | None = None) -> InlineKeyboardMarkup:
    """3H (M10): пресеты порогов аномалий. cur — эффективные пороги (дефолты ∪ per-chat);
    активное значение помечено ✅. «✏️» — ручной ввод (FSM), «↩️» — сброс к дефолтам."""
    en = _lang(lang) == "en"

    def _mark(field_key: str, preset: float) -> str:
        return "✅ " if abs(float(cur.get(field_key, -1)) - preset) < 1e-9 else ""

    kb = InlineKeyboardBuilder()
    for preset in (25, 50, 100):
        kb.button(
            text=f"{_mark('spend_spike_pct', preset)}📈 {preset}%",
            callback_data=AlertCB(field="spike", value=str(preset)),
        )
    kb.button(text="✏️", callback_data=AlertCB(field="spike", value="custom"))
    for preset in (25, 50, 75):
        kb.button(
            text=f"{_mark('conv_drop_pct', preset)}📉 {preset}%",
            callback_data=AlertCB(field="drop", value=str(preset)),
        )
    kb.button(text="✏️", callback_data=AlertCB(field="drop", value="custom"))
    for preset in (1, 10, 100):
        kb.button(
            text=f"{_mark('min_spend', preset)}💸 {preset}",
            callback_data=AlertCB(field="minspend", value=str(preset)),
        )
    kb.button(text="✏️", callback_data=AlertCB(field="minspend", value="custom"))
    kb.button(
        text="↩️ Reset to defaults" if en else "↩️ Сбросить (дефолты)",
        callback_data=AlertCB(field="reset"),
    )
    kb.adjust(4, 4, 4, 1)
    return kb.as_markup()


def diag_kb(rows, *, today: bool, is_admin: bool, lang: str | None = None) -> InlineKeyboardMarkup:
    """A3 (§15): кнопки под /diag. «🔄 Обновить» (перечитать), тумблер «⚠️ За сегодня»/«🗂 Все».
    Для админа — до 5 кнопок «🔍 <request_id>» (полный редактированный traceback инцидента). rows —
    показанные ErrorEvent (для detail-кнопок). Кнопки только читают/двигают UI (мутаций нет)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Refresh" if en else "🔄 Обновить",
        callback_data=DiagCB(action="today" if today else "refresh"),
    )
    if today:
        kb.button(text="🗂 All" if en else "🗂 Все", callback_data=DiagCB(action="all"))
    else:
        kb.button(text="⚠️ Today" if en else "⚠️ За сегодня", callback_data=DiagCB(action="today"))
    # A12: экспорт (.txt-дамп с трейсбеками+чужими customer_id) — только админу (как detail).
    # Не-админ кнопку не видит; на прямой callback on_diag_cb всё равно отдаст admin_only.
    counts = [2]
    if is_admin:
        # 1.2: экспорт журнала ошибок файлом (.txt) — вложением, читать удобнее длинной ленты в чате.
        kb.button(text="📎 Export" if en else "📎 Экспорт", callback_data=DiagCB(action="export"))
        counts = [3]
        # detail только админу (traceback — операционная деталь; /diag открыт всем whitelisted).
        seen: set[str] = set()
        n = 0
        for e in rows or []:
            rid = getattr(e, "request_id", "") or ""
            if not rid or rid in seen:
                continue
            seen.add(rid)
            kb.button(text=f"🔍 {rid}", callback_data=DiagCB(action="detail", rid=rid))
            n += 1
            if n >= 5:
                break
        if n:
            counts.append(1 if n == 1 else 2)
    kb.adjust(*counts)
    return kb.as_markup()


def bugs_kb(rows, lang: str | None = None) -> InlineKeyboardMarkup:
    """§6: кнопки под /bugs (админ-триаж). Для каждого НЕ закрытого репорта — «✅ В работу» (triaged)
    и «🗄 Закрыть» (closed); плюс «🔄 Обновить». Кнопки меняют только статус bug_reports (локальная
    БД), Google Ads/proposal не трогают. rows — показанные BugReport."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Refresh" if en else "🔄 Обновить", callback_data=BugCB(action="refresh"))
    counts = [1]
    shown = 0
    for r in rows or []:
        if getattr(r, "status", "") == "closed":
            continue
        bid = int(getattr(r, "id", 0) or 0)
        if not bid:
            continue
        kb.button(
            text=f"✅ #{bid} " + ("In work" if en else "В работу"),
            callback_data=BugCB(action="triaged", bid=bid),
        )
        kb.button(
            text=f"🗄 #{bid} " + ("Close" if en else "Закрыть"),
            callback_data=BugCB(action="closed", bid=bid),
        )
        counts.append(2)
        shown += 1
        if shown >= 8:  # не раздуваем клавиатуру (лимиты Telegram/читаемость)
            break
    kb.adjust(*counts)
    return kb.as_markup()


def more_menu_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """3E: inline-хаб «➕ Ещё» — вторичные флоу одним тапом (раньше — только слэш-командой)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for label_en, label_ru, action in (
        ("💡 Recommendations", "💡 Рекомендации", "advise"),
        ("🩺 Account audit", "🩺 Аудит аккаунта", "audit"),
        ("🔎 Search campaign (quick)", "🔎 Поисковая кампания (быстро)", "newsearch"),
        ("🎬 Campaign from video", "🎬 Кампания из видео", "newvideo"),
        ("📁 Campaign templates", "📁 Шаблоны кампаний", "templates"),
        # P3: добавление ключей в кампанию — свой файл/ссылка/текст (бывшая кнопка под отчётом)
        ("➕ Keywords to a campaign", "➕ Ключи в кампанию", "addkeys"),
        ("↻ Recent actions", "↻ Недавние действия", "recent"),
        ("📉 API quota", "📉 Квота API", "quota"),
        ("🔔 Alert thresholds", "🔔 Пороги алертов", "alerts"),
        ("🐞 Report a bug", "🐞 Сообщить об ошибке", "reportbug"),
        ("⚙️ Service / Accounts", "⚙️ Сервис / Аккаунты", "service"),
    ):
        kb.button(text=label_en if en else label_ru, callback_data=MoreCB(action=action))
    kb.adjust(2, 1, 1, 2, 2, 2, 1)
    return kb.as_markup()


def service_menu_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """E: inline суб-хаб «⚙️ Сервис/Аккаунты» — сервис/аккаунт-команды, у которых не было кнопки
    (обнаружимость: раньше только слэшем). Как more_menu_kb: кнопка лишь МАРШРУТИЗИРУЕТ в тот же
    entry, что и команда (account/accounts/whoami/refresh/savetemplate/diag); мутаций не создаёт."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for label_en, label_ru, action in (
        ("🏢 My accounts", "🏢 Мои аккаунты", "svc_accounts"),
        ("🔄 Switch account", "🔄 Сменить аккаунт", "svc_account"),
        ("👤 Who am I", "👤 Кто я", "svc_whoami"),
        ("🔃 Refresh accounts/caches", "🔃 Обновить аккаунты/кэши", "svc_refresh"),
        ("💾 Save template", "💾 Сохранить шаблон", "svc_savetemplate"),
        ("🩺 Diagnostics", "🩺 Диагностика", "svc_diag"),
    ):
        kb.button(text=label_en if en else label_ru, callback_data=MoreCB(action=action))
    kb.adjust(2, 1, 2, 1)
    return kb.as_markup()


def advise_header_kb(proactive_on: bool, lang: str | None = None) -> InlineKeyboardMarkup:
    """advisor: под заголовком /advise — тумблер проактивной подачи (ui_prefs.advise_proactive).
    Кнопка лишь меняет НАСТРОЙКУ БОТА (как /alerts), Google Ads/proposal не трогает. rec несёт
    ЖЕЛАЕМОЕ состояние ('on'|'off'); текст показывает текущее."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if proactive_on:
        label = "🔔 Auto-advice: ON (turn off)" if en else "🔔 Авто-советы: ВКЛ (выключить)"
        kb.button(text=label, callback_data=AdviseCB(action="auto", rec="off"))
    else:
        label = "🔕 Auto-advice: OFF (turn on)" if en else "🔕 Авто-советы: ВЫКЛ (включить)"
        kb.button(text=label, callback_data=AdviseCB(action="auto", rec="on"))
    kb.adjust(1)
    return kb.as_markup()


# §advisor #1: какие советы можно применить в ОДИН ТАП (только НЕ-денежные — pause/минус-слова).
# Деньги/ставки (update_budget/update_bid) НАМЕРЕННО не one-tap (golden rule #3) → нет метки → нет кнопки.
_ADVISE_APPLY_LABELS = {
    "pause_campaign": "advise_apply_btn_pause",
    "add_negative_keywords": "advise_apply_btn_negatives",
    # G12/G11 (2026-07-14): «быстрые победы» аудита, которые бот реально чинит одной кнопкой.
    # Направление зашито (КМС→off, гео→PRESENCE) в bot.main._advise_apply_params — не из находки.
    "set_campaign_display_network": "advise_apply_btn_display_off",
    "set_campaign_geo_target_type": "advise_apply_btn_geo_presence",
}
# ЕДИНЫЙ источник множества one-tap операций: bot.main (интерактивный /advise) и scheduler-дайджест
# импортируют ЭТО множество — дублирование списков разъехалось бы молча (гард денег #3).
ADVISE_APPLY_OPS = frozenset(_ADVISE_APPLY_LABELS)


def advise_feedback_kb(
    rec_uid: str, lang: str | None = None, apply_op: str | None = None
) -> InlineKeyboardMarkup:
    """advisor: 👍/👎 под одной рекомендацией (/advise) + (опц.) «применить» для НЕ-денежных советов.
    Фидбек-кнопки пишут только в recommendation_feedback; «применить» СТАРТУЕТ confirm-гейт (proposal),
    ничего не исполняя само. apply_op ∈ _ADVISE_APPLY_LABELS → кнопка; иначе (деньги/None) — нет."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    apply_key = _ADVISE_APPLY_LABELS.get(apply_op or "")
    if apply_key:
        from bot import i18n

        kb.button(text=i18n.t(apply_key, lang), callback_data=AdviseCB(action="apply", rec=rec_uid))
    kb.button(
        text="👍 Useful" if en else "👍 Полезно",
        callback_data=AdviseCB(action="up", rec=rec_uid),
    )
    kb.button(
        text="👎 Not useful" if en else "👎 Мимо",
        callback_data=AdviseCB(action="down", rec=rec_uid),
    )
    # 🙈 «Скрыть» — сигнал «показано и проигнорировано»: status='dismissed' + слабый негатив в
    # experience (пер-кампанийная усталость). Только локальная БД, как 👍/👎.
    kb.button(
        text="🙈 Hide" if en else "🙈 Скрыть",
        callback_data=AdviseCB(action="dismiss", rec=rec_uid),
    )
    if apply_key:
        kb.adjust(1, 3)
    else:
        kb.adjust(3)
    return kb.as_markup()


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
# P3 (фидбэк заказчика 2026-07-06): kw_add_kb (кнопка «➕ Добавить ключи» под отчётом research)
# УДАЛЕНА — вход теперь /addkeys и меню «➕ Ещё» (приём файла/ссылки/текста). Старые кнопки в
# истории чата деградируют в kw_add_stale (on_kw_add_start проверяет токен).


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


def confirm_kb(
    cid: str, lang: str | None = None, *, extra_top: tuple[str, object] | None = None
) -> InlineKeyboardMarkup:
    """✅/❌ карточки черновика. extra_top (A7): опц. кнопка ОТДЕЛЬНОЙ строкой над ✅/❌
    (напр. «✏️ Изменить ставку» для create_search_campaign) — (текст, callback_data)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if extra_top is not None:
        kb.button(text=extra_top[0], callback_data=extra_top[1])
    kb.button(
        text="✅ Confirm" if en else "✅ Подтвердить", callback_data=ConfirmCB(action="ok", cid=cid)
    )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=ConfirmCB(action="no", cid=cid)
    )
    kb.adjust(1, 2) if extra_top is not None else kb.adjust(2)
    return kb.as_markup()


def confirm_destructive_kb(cid: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """Первый шаг ДВОЙНОГО подтверждения необратимого удаления (P1-6): «🗑 Удалить» ведёт НЕ на
    исполнение, а на экран-предупреждение (ConfirmCB action=del1); реальное исполнение — только
    после второго тапа (confirm_final_kb → action=ok). Отмена — сразу."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Delete" if en else "🗑 Удалить", callback_data=ConfirmCB(action="del1", cid=cid)
    )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=ConfirmCB(action="no", cid=cid)
    )
    kb.adjust(2)
    return kb.as_markup()


def confirm_final_kb(cid: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """Второй (финальный) шаг подтверждения удаления: «⚠️ Да, удалить безвозвратно» = реальный
    confirm (action=ok, тот же confirmation_id) / «❌ Отмена»."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚠️ Yes, delete permanently" if en else "⚠️ Да, удалить безвозвратно",
        callback_data=ConfirmCB(action="ok", cid=cid),
    )
    kb.button(
        text="❌ Cancel" if en else "❌ Отмена", callback_data=ConfirmCB(action="no", cid=cid)
    )
    kb.adjust(1, 1)
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
    has_profile: bool,
    has_website: bool = False,
    lang: str | None = None,
    customer_id: str = "",
    has_dossier: bool = False,
) -> InlineKeyboardMarkup:
    """§20.2: кнопки карточки клиента. Есть профиль → Обновить/Очистить (+Перекраулить, если есть
    сайт); нет → Добавить. Краулинг/изменения памяти — фоново/через confirm-гейт (см. bot.main).

    C4: customer_id несём в sub у add/update (≤10 цифр — влезает в 64 байта callback_data), чтобы
    приём текста профиля выставлял FSM-состояние даже когда volatile FSM-данные потеряны (рестарт/
    idle-автосейв/suspend меню). Раньше пустой cli_customer_id → ранний return без set_state →
    следующий текст с URL уходил в агент-задачу, а не в накопление профиля (баг из живого теста)."""
    en = _lang(lang) == "en"
    sub = str(customer_id or "")
    kb = InlineKeyboardBuilder()
    # P1-11: подтянуть ФАКТЫ из Google Ads аккаунта (валюта/таймзона/гео/языки/домен) — без выдумок,
    # через тот же confirm-гейт. Доступно и для нового, и для существующего профиля.
    kb.button(
        text="🔎 Fill from account" if en else "🔎 Подтянуть из аккаунта",
        callback_data=ClientCB(action="autofill", sub=sub),
    )
    if (
        has_dossier
    ):  # §20: досье (map-reduce по сайту) отдаётся .md-файлом — кнопка только если есть
        kb.button(
            text="📄 Dossier" if en else "📄 Досье",
            callback_data=ClientCB(action="dossier", sub=sub),
        )
    if has_profile:
        kb.button(
            text="✏️ Update info" if en else "✏️ Обновить инфу",
            callback_data=ClientCB(action="update", sub=sub),
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
            callback_data=ClientCB(action="add", sub=sub),
        )
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=ClientCB(action="back"))
    kb.adjust(1)
    return kb.as_markup()


def client_show_card_kb(customer_id: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """§20.2 «📋 Карточка клиента»: один тап к карточке после add/update/краула — без Back→reselect.
    customer_id в sub (≤10 цифр — влезает в 64 байта callback_data): работает stateless после
    фонового краула/очистки FSM; доступ re-check'ается fail-closed в _cli_show_card."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📋 Client card" if en else "📋 Карточка клиента",
        callback_data=ClientCB(action="card", sub=str(customer_id)),
    )
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


def _cc_back_btn(kb: InlineKeyboardBuilder, en: bool) -> None:
    """3D: «‹ Назад» визарда §19 — навигация на предыдущий этап (данные черновика НЕ стираются;
    раньше единственным выходом была полная «✖ Отмена» с потерей маршрута)."""
    kb.button(text="‹ Back" if en else "‹ Назад", callback_data=CcCB(action="back"))


def _cc_nav_row(kb: InlineKeyboardBuilder, en: bool, can_forward: bool) -> int:
    """W4 (живой тест 2026-07-06): навигационный ряд визарда «‹ Назад [/ Вперёд ›] / ✖ Отмена».
    «Вперёд ›» показывается ТОЛЬКО когда следующий этап уже был пройден (high-water max_step) —
    после «Назад» пользователь видел тупик и не знал, что вперёд возвращает re-confirm.
    Возвращает ширину ряда для kb.adjust()."""
    _cc_back_btn(kb, en)
    if can_forward:
        kb.button(text="Forward ›" if en else "Вперёд ›", callback_data=CcCB(action="fwd"))
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    return 3 if can_forward else 2


def cc_settings_kb(lang: str | None = None, *, can_forward: bool = False) -> InlineKeyboardMarkup:
    """Этап 1 (§19.3): ✅ Подтвердить / ✏️ Изменить / ‹ Назад [/ Вперёд ›] / ✖ Отмена — как в ТЗ.
    «Изменить» лишь подсказывает формат правки (правка — свободным текстом в состоянии)."""
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
    nav = _cc_nav_row(kb, en, can_forward)
    kb.adjust(1, 1, nav)
    return kb.as_markup()


def mysched_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """2.11 (§14): /myschedule — пресеты персонального расписания планового отчёта. Настройка
    БОТА (UserSettings.report_schedule), confirm-гейт не нужен (как /alerts)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="Daily 09:00" if en else "Ежедневно 09:00", callback_data=MySchedCB(action="daily")
    )
    kb.button(
        text="Weekly, Mon 09:00" if en else "Еженедельно, пн 09:00",
        callback_data=MySchedCB(action="weekly"),
    )
    kb.button(
        text="✏️ Custom cron" if en else "✏️ Свой cron", callback_data=MySchedCB(action="custom")
    )
    kb.button(
        text="🔕 Off (use global)" if en else "🔕 Выключить (глобальное)",
        callback_data=MySchedCB(action="off"),
    )
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def thr_tune_kb(token: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """2.11 (§14): предложение авто-подстройки порогов аномалий. «Принять» пишет НАСТРОЙКУ БОТА
    (alert_thresholds.per_account) ТОЛЬКО по тапу человека; Google Ads не трогается."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Accept" if en else "✅ Принять", callback_data=ThrTuneCB(action="acc", token=token)
    )
    kb.button(
        text="✖ Keep current" if en else "✖ Оставить как есть",
        callback_data=ThrTuneCB(action="dec", token=token),
    )
    kb.adjust(2)
    return kb.as_markup()


def cc_kw_kb(lang: str | None = None, *, can_forward: bool = False) -> InlineKeyboardMarkup:
    """Этап 2: «🔎 Генерация» (CcCB kw_generate) / «📎 Загрузить свои» (2.10 §19.4: визуальная
    развилка из документа — кнопка показывает инструкцию по форматам, сам ввод — текст/файл/ссылка);
    «⏭ Пропустить» / навигация."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔎 Generate keywords" if en else "🔎 Генерация ключевых слов",
        callback_data=CcCB(action="kw_generate"),
    )
    kb.button(
        text="📎 Upload your own" if en else "📎 Загрузить свои",
        callback_data=CcCB(action="kw_own"),
    )
    kb.button(text="⏭ Skip" if en else "⏭ Пропустить", callback_data=CcCB(action="skip"))
    nav = _cc_nav_row(kb, en, can_forward)
    kb.adjust(1, 1, 1, nav)
    return kb.as_markup()


def cc_kw_verify_kb(lang: str | None = None, *, can_forward: bool = False) -> InlineKeyboardMarkup:
    """Этап 2 (после генерации в Google Sheets): «✅ Использовать эти ключи» (взять сгенерированный
    список без ручной правки таблицы — P0-2: ключи уже сохранены в черновик) ИЛИ прислать ссылку на
    отредактированную таблицу для верификации; навигация."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Use these keywords" if en else "✅ Использовать эти ключи",
        callback_data=CcCB(action="kw_use_generated"),
    )
    nav = _cc_nav_row(kb, en, can_forward)
    kb.adjust(1, nav)
    return kb.as_markup()


def cc_assets_kb(lang: str | None = None, *, can_forward: bool = False) -> InlineKeyboardMarkup:
    """Этап 5: «✅ Использовать текущие» / «➕ Добавить новый» / «✅ Готово» / навигация.
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
    nav = _cc_nav_row(kb, en, can_forward)
    kb.adjust(1, 1, 1, nav)
    return kb.as_markup()


def cc_assets_reuse_kb(
    counts: dict[str, int], excluded: set[str] | None = None, lang: str | None = None
) -> InlineKeyboardMarkup:
    """§19.7: ВЫБОР ПОДМНОЖЕСТВА переиспользуемых ассетов — тумблер на каждый тип (SITELINK×4 …).

    ТЗ требует выбрать, какие ассеты аккаунта переиспользовать; раньше бот линковал ВСЕ найденные
    без спроса (чужие уточнения и телефон другой услуги уезжали в новую кампанию молча). По
    умолчанию включены все — прежнее поведение сохраняется, если менеджер сразу жмёт «Готово»."""
    en = _lang(lang) == "en"
    off = excluded or set()
    kb = InlineKeyboardBuilder()
    for ft, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        mark = "⬜" if ft in off else "✅"
        kb.button(text=f"{mark} {ft} ×{n}", callback_data=CcCB(action="reuse_type", sub=ft))
    kb.button(
        text="✅ Done" if en else "✅ Готово",
        callback_data=CcCB(action="reuse_done"),
    )
    kb.adjust(1)
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
        # §19.7.1: типы, требующие ВНЕШНЕЙ настройки аккаунта (не автогенерируются). Показываем в
        # перечне честно (раньше молча отсутствовали), при выборе — объясняем, что нужно.
        "lead_form": "📝 Лид-форма (Lead form)",
        "location": "📍 Адрес (Location) ⚙️",
        "affiliate_location": "🏬 Адрес аффилиата 🚫",
        "app": "📱 Приложение (App) 🚫",
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
        "lead_form": "📝 Lead form",
        "location": "📍 Location ⚙️",
        "affiliate_location": "🏬 Affiliate location 🚫",
        "app": "📱 App 🚫",
    },
}

# §19.7.1: семейства ассетов, требующие внешней настройки аккаунта (location) или отсутствующие в
# API v24 / вне объёма (affiliate_location депрекирован Google, app=UAC исключён) — НЕ
# автогенерируются. Показаны в пикере честно (не молча опущены); при выборе бот объясняет причину
# (cc_asset_type). lead_form РЕАЛИЗОВАН (собирает privacy-URL) — в этот набор НЕ входит.
# ⚙️ = нужна доп. настройка аккаунта; 🚫 = недоступно в v24 / вне объёма.
CC_ASSET_GATED = ("location", "affiliate_location", "app")


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
        kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
        kb.adjust(1, 1)
        return kb.as_markup()
    kb.button(
        text="✅ Create draft" if en else "✅ Создать черновик",
        callback_data=CcCB(action="create"),
    )
    _cc_back_btn(kb, en)  # 3D: назад к URL-опциям (кампания ещё НЕ создана)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 2)
    return kb.as_markup()


def post_create_kb(launch_cid: str = "", lang: str | None = None) -> InlineKeyboardMarkup:
    """§UX «что дальше» после успешного создания кампании (PAUSED): 🚀 Запустить (существующий
    confirm-гейт cc_launch, sub=confirmation_id создания) · 📋 Кампании (read-only список) ·
    ➖ Минус-слова (текст-подсказка, proposal НЕ минтится). ВСЁ advisory — ни одна кнопка не
    выполняет мутацию без «да» (golden rule 1/3)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚀 Launch campaign" if en else "🚀 Запустить кампанию",
        callback_data=CcCB(action="launch", sub=launch_cid),
    )
    kb.button(
        text="📋 Campaigns" if en else "📋 Кампании",
        callback_data=CcCB(action="view_camps"),
    )
    kb.button(
        text="➖ Negative keywords" if en else "➖ Минус-слова",
        callback_data=CcCB(action="hint_neg", sub=launch_cid),
    )
    kb.adjust(1, 2)
    return kb.as_markup()


def kw_add_campaigns_kb(
    camps: list[dict], token: str, lang: str | None = None, *, gen: int = 0
) -> InlineKeyboardMarkup:
    """D3: пикер кампаний для /addkeys — кнопка на кампанию (idx → позиция в _KW_ADD_CAMP_CACHE).
    gen — поколение списка (N1.4-ревью): кэш может перезаписать fuzzy-подсказка, клик по старой
    клавиатуре обязан дать «список устарел», а не другую кампанию по тому же idx.
    Показываем первую страницу (до _CAMP_PAGE): текст-фолбэк (kw_add_campaign) всегда ловит имя,
    поэтому на крупном аккаунте остальные кампании доступны вводом названия — без REPLY_MARKUP_TOO_LONG."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(camps[:_CAMP_PAGE]):
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}",
            callback_data=KwAddCB(action="camp", token=token, idx=i, gen=gen),
        )
    kb.button(
        text="✖ Cancel" if en else "✖ Отмена", callback_data=KwAddCB(action="cancel", token=token)
    )
    kb.adjust(1)
    return kb.as_markup()


def slash_mutate_campaigns_kb(
    camps: list[dict], op: str, lang: str | None = None, *, gen: int = 0
) -> InlineKeyboardMarkup:
    """D4: пикер кампаний для /pause и /resume без аргумента. idx → позиция в _SLASH_MUT_CACHE.
    gen — поколение списка (N1.4-ревью): кэш может перезаписать fuzzy-подсказка, клик по старой
    клавиатуре обязан дать «список устарел», а не другую кампанию по тому же idx.
    Список уже отфильтрован по статусу (ENABLED для паузы / PAUSED для возобновления). Первая
    страница (до _CAMP_PAGE): ввод имени командой остаётся фолбэком на крупном аккаунте."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(camps[:_CAMP_PAGE]):
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}",
            callback_data=SlashMutCB(op=op, idx=i, gen=gen),
        )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def rollback_kb(token: str, lang: str | None = None) -> InlineKeyboardMarkup:
    """D2: одна кнопка «↩️ Откатить» под сообщением об успешной обратимой операции. Клик минтит
    ОБРАТНЫЙ черновик за confirm-гейтом (не исполняет) — см. on_rollback."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="↩️ Undo" if en else "↩️ Откатить",
        callback_data=RollbackCB(token=token),
    )
    kb.adjust(1)
    return kb.as_markup()


def journal_rollback_kb(rows, lang: str | None = None) -> InlineKeyboardMarkup | None:
    """Доп.2B: кнопки «↩️ Откатить» под /journal для СВОИХ применённых обратимых операций.
    rows — [(confirmation_id, label)], уже отфильтровано вызывающим (applied ∩ _ROLLBACKABLE_OPS ∩
    свой чат). Пусто → None (клавиатуры нет). Клик минтит ОБРАТНЫЙ черновик за confirm-гейтом
    (persistent: cid из БД, переживает рестарт — в отличие от in-memory rollback_kb)."""
    if not rows:
        return None
    en = _lang(lang) == "en"
    prefix = "↩️ Undo: " if en else "↩️ Откатить: "
    kb = InlineKeyboardBuilder()
    for cid, label in rows:
        kb.button(text=f"{prefix}{label}", callback_data=JournalRollbackCB(cid=cid))
    kb.adjust(1)
    return kb.as_markup()


def cc_exit_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """W5 (живой тест 2026-07-06): диалог выхода из визарда §19 с накопленной работой.
    «Сохранить» = soft-exit (черновик остаётся active, вернуться через /newcampaign);
    «Удалить» = прежний abandon; «Вернуться» = остаться на текущем этапе."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💾 Exit — keep the draft" if en else "💾 Выйти — черновик сохранится",
        callback_data=CcCB(action="exit_keep"),
    )
    kb.button(
        text="🗑 Delete the draft" if en else "🗑 Удалить черновик",
        callback_data=CcCB(action="exit_drop"),
    )
    kb.button(
        text="↩️ Return to the wizard" if en else "↩️ Вернуться",
        callback_data=CcCB(action="exit_stay"),
    )
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def cc_skip_kb(lang: str | None = None, *, can_forward: bool = False) -> InlineKeyboardMarkup:
    """Этап 4/6: «⏭ Пропустить» (CcCB skip) + навигация. Прикрепление (фото) — отдельным
    сообщением, не кнопкой."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Skip" if en else "⏭ Пропустить", callback_data=CcCB(action="skip"))
    nav = _cc_nav_row(kb, en, can_forward)
    kb.adjust(1, nav)
    return kb.as_markup()


def cc_kw_confirm_kb(lang: str | None = None, *, can_forward: bool = False) -> InlineKeyboardMarkup:
    """§19.4: явный гейт «✅ Подтвердить ключевые слова» перед Этапом 3. Замена списка — просто
    прислать новый (state остаётся на Этапе 2); «✖ Отмена» — выход из визарда."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Confirm keywords" if en else "✅ Подтвердить ключевые слова",
        callback_data=CcCB(action="kw_confirm"),
    )
    nav = _cc_nav_row(kb, en, can_forward)
    kb.adjust(1, nav)
    return kb.as_markup()


def video_type_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """§11: выбор типа кампании из видео — Demand Gen (рекомендуется) или Video (охват, CPM).
    Кнопки лишь двигают визард; мутация — только через confirm-гейт.

    B4: Video-кнопку показываем ТОЛЬКО если владелец включил её (GOOGLE_ADS_VIDEO_ENABLED) —
    аккаунт в allowlist Google. Иначе не предлагаем гарантированный тупик (Video → MUTATE_NOT_ALLOWED);
    остаётся Demand Gen (рабочий путь из того же видео). Импорт локальный — не связываем слой
    клавиатур с конфигом на уровне модуля."""
    from core.config import settings

    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🎯 Demand Gen (recommended)" if en else "🎯 Demand Gen (рекомендую)",
        callback_data=VideoCB(action="dg"),
    )
    if settings.google_ads_video_enabled:
        kb.button(
            text="▶️ Video (reach, CPM)" if en else "▶️ Video (охват, CPM)",
            callback_data=VideoCB(action="video"),
        )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def video_logo_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """§11 Demand Gen: логотип ОБЯЗАТЕЛЕН (live 2026-07: без logo_images Google отвергает create
    TOO_FEW → откат бюджета+кампании+группы). Кнопки «Пропустить» больше нет — только фото или
    отмена; старые «⏭»-кнопки в истории ловит video_logo_skip с объяснением."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1)
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
def campaigns_kb(camps: list[dict], page: int = 0, gen: int = 0) -> InlineKeyboardMarkup:
    """По кнопке на кампанию (раскрывает меню действий), ПОСТРАНИЧНО (3E: >100 кампаний давали
    REPLY_MARKUP_TOO_LONG → «код инцидента» вместо списка). idx = ГЛОБАЛЬНАЯ позиция в списке.
    gen — поколение снимка списка (bot.main._camp_store): хендлер сверяет его перед резолвом idx."""
    kb = InlineKeyboardBuilder()
    total = len(camps)
    pages = max(1, (total + _CAMP_PAGE - 1) // _CAMP_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _CAMP_PAGE
    shown = 0
    for i in range(start, min(start + _CAMP_PAGE, total)):
        c = camps[i]
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}",
            callback_data=CampCB(action="menu", idx=i, gen=gen),
        )
        shown += 1
    nav_n = _page_nav_row(kb, "camp", "", page, pages)
    search_n = _search_btn(kb, "campaigns", "", total, _lang(None) == "en")
    sizes = [1] * shown
    if nav_n:
        sizes.append(nav_n)
    if search_n:
        sizes.append(search_n)
    kb.adjust(*sizes)
    return kb.as_markup()


def campaign_actions_kb(
    idx: int, status: str, lang: str | None = None, gen: int = 0
) -> InlineKeyboardMarkup:
    """Действия для одной кампании. pause/resume зависят от статуса; мутации идут через
    confirm-гейт (кнопка лишь создаёт черновик, не исполняет). gen — поколение списка (см. CampCB)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if status == "ENABLED":
        kb.button(
            text="⏸ Pause" if en else "⏸ Поставить на паузу",
            callback_data=CampCB(action="pause", idx=idx, gen=gen),
        )
    elif status == "PAUSED":
        kb.button(
            text="▶️ Resume" if en else "▶️ Возобновить",
            callback_data=CampCB(action="resume", idx=idx, gen=gen),
        )
    kb.button(
        text="🎯 Audiences" if en else "🎯 Аудитории",
        callback_data=CampCB(action="audience", idx=idx, gen=gen),
    )
    kb.button(
        text="📍 Geo targeting" if en else "📍 Гео-таргетинг",
        callback_data=CampCB(action="geo", idx=idx, gen=gen),
    )
    kb.button(
        text="🧩 Extensions" if en else "🧩 Расширения",
        callback_data=CampCB(action="ext", idx=idx, gen=gen),
    )
    kb.button(
        text="🌐 Networks" if en else "🌐 Сети",
        callback_data=CampCB(action="network", idx=idx, gen=gen),
    )
    kb.button(
        text="🗑 Delete campaign" if en else "🗑 Удалить кампанию",
        callback_data=CampCB(action="delete", idx=idx, gen=gen),
    )
    kb.button(
        text="‹ Back to list" if en else "‹ Назад к списку",
        callback_data=CampCB(action="back", idx=idx, gen=gen),
    )
    kb.adjust(1)
    return kb.as_markup()


def campaign_network_kb(idx: int, lang: str | None = None, gen: int = 0) -> InlineKeyboardMarkup:
    """§19.3: тумблеры сетей кампании — поисковые ПАРТНЁРЫ и КМС (G12, аудит меряет расход КМС
    внутри Search-кампании). Кнопки лишь СОЗДАЮТ черновики (set_campaign_network /
    set_campaign_display_network) за confirm-гейтом; ограниченную партнёрскую сеть не трогаем."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚫 Partners OFF (recommended)" if en else "🚫 Партнёры ВЫКЛ (рекомендуется)",
        callback_data=CampCB(action="net_off", idx=idx, gen=gen),
    )
    kb.button(
        text="✅ Partners ON" if en else "✅ Партнёры ВКЛ",
        callback_data=CampCB(action="net_on", idx=idx, gen=gen),
    )
    kb.button(
        text="🚫 Display Network OFF" if en else "🚫 КМС ВЫКЛ (для поисковых — верно)",
        callback_data=CampCB(action="kms_off", idx=idx, gen=gen),
    )
    kb.button(
        text="✅ Display Network ON" if en else "✅ КМС ВКЛ",
        callback_data=CampCB(action="kms_on", idx=idx, gen=gen),
    )
    kb.button(
        text="‹ Back" if en else "‹ Назад",
        callback_data=CampCB(action="menu", idx=idx, gen=gen),
    )
    kb.adjust(1)
    return kb.as_markup()


def ext_menu_kb(idx: int, lang: str | None = None, gen: int = 0) -> InlineKeyboardMarkup:
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
    kb.button(
        text="‹ Back" if en else "‹ Назад", callback_data=CampCB(action="menu", idx=idx, gen=gen)
    )
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


def ext_assets_list_kb(
    rows: list, camp_idx: int, lang: str | None = None, gen: int = 0
) -> InlineKeyboardMarkup:
    """§3-assets: текущие расширения кампании с кнопками удаления (🗑 idx). idx — строка в
    _EXT_CACHE[chat_id]; «‹ Назад» — к меню расширений кампании (camp_idx в _CAMP_CACHE)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows):
        kb.button(
            text=f"🗑 {_ellipsize(getattr(r, 'label', '') or getattr(r, 'field_type', ''))}",
            callback_data=ExtCB(action="remove", idx=i),
        )
    kb.button(
        text="‹ Back" if en else "‹ Назад",
        callback_data=CampCB(action="ext", idx=camp_idx, gen=gen),
    )
    kb.adjust(1)
    return kb.as_markup()


def geo_mode_kb(idx: int, lang: str | None = None, gen: int = 0) -> InlineKeyboardMarkup:
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
    # G11: тип таргетинга — КОГО считать «в регионе». Дефолт Google (присутствие ИЛИ интерес)
    # пускает клики людей ФИЗИЧЕСКИ вне регионов; аудит меряет их расход, здесь — починка.
    kb.button(
        text="🎯 Presence only (recommended)" if en else "🎯 Только те, кто В регионе (реком.)",
        callback_data=CampCB(action="gt_p", idx=idx, gen=gen),
    )
    kb.button(
        text="🌐 Presence OR interest" if en else "🌐 Присутствие ИЛИ интерес",
        callback_data=CampCB(action="gt_pi", idx=idx, gen=gen),
    )
    kb.button(
        text="‹ Back" if en else "‹ Назад", callback_data=CampCB(action="menu", idx=idx, gen=gen)
    )
    kb.adjust(1)
    return kb.as_markup()


def audiences_kb(
    auds: list,
    camp_idx: int,
    lang: str | None = None,
    attached: list | None = None,
    page: int = 0,
    gen: int = 0,
) -> InlineKeyboardMarkup:
    """Выбор аудитории для прикрепления к кампании (§3). idx — ГЛОБАЛЬНАЯ позиция в _AUD_CACHE;
    camp_idx ведёт прикрепление к конкретной кампании и кнопку «назад» — к её меню.
    C7: attached — УЖЕ прикреплённые аудитории (idx в _AUD_DET_CACHE) с кнопкой 🗑, минтящей
    detach_audience за confirm-гейтом (раньше открепить из бота было нельзя вовсе).
    C8: доступные к прикреплению — ПОСТРАНИЧНО (много user_list → REPLY_MARKUP_TOO_LONG);
    прикреплённых обычно единицы — показываем всегда."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    rows = 0
    for i, a in enumerate(attached or []):
        kb.button(
            text=("🗑 Detach: " if en else "🗑 Открепить: ") + _ellipsize(a.name),
            callback_data=AudienceCB(action="det", camp_idx=camp_idx, idx=i),
        )
        rows += 1
    total = len(auds)
    pages = max(1, (total + _CAMP_PAGE - 1) // _CAMP_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _CAMP_PAGE
    for i in range(start, min(start + _CAMP_PAGE, total)):
        a = auds[i]
        size = getattr(a, "size", 0) or 0
        suffix = f" · {size:,}".replace(",", " ") if size else ""
        kb.button(
            text=f"👥 {_ellipsize(a.name)}{suffix}",
            callback_data=AudienceCB(action="pick", camp_idx=camp_idx, idx=i),
        )
        rows += 1
    # target несёт camp_idx — перелистывание пересобирает клавиатуру ТОЙ ЖЕ кампании.
    nav_n = _page_nav_row(kb, "aud", str(camp_idx), page, pages)
    sizes = [1] * rows
    if nav_n:
        sizes.append(nav_n)
    sizes.append(1)  # «‹ Назад»
    kb.button(
        text="‹ Back" if en else "‹ Назад",
        callback_data=CampCB(action="menu", idx=camp_idx, gen=gen),
    )
    kb.adjust(*sizes)
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
def period_kb(
    target: str, lang: str | None = None, *, last: str | None = None
) -> InlineKeyboardMarkup:
    """Выбор периода отчёта. last (§UX-память) — последний выбранный пресет: первой строкой
    добавляется «↻ N — как в прошлый раз» (тот же PeriodCB, read-only путь). Неизвестный last —
    игнорируется (fail-safe, обычная клавиатура)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    if en:
        items = [("7 days", "7"), ("30 days", "30"), ("90 days", "90"), ("MTD", "MTD")]
    else:
        items = [("7 дней", "7"), ("30 дней", "30"), ("90 дней", "90"), ("MTD", "MTD")]
    labels = dict((code, label) for label, code in items)
    has_last = bool(last and last in labels)
    if has_last:
        repeat = (
            f"↻ {labels[last]} — same as last time"
            if en
            else f"↻ {labels[last]} — как в прошлый раз"
        )
        kb.button(text=repeat, callback_data=PeriodCB(target=target, code=last))
    for label, code in items:
        kb.button(text=label, callback_data=PeriodCB(target=target, code=code))
    kb.adjust(*((1, 2, 2) if has_last else (2, 2)))
    return kb.as_markup()


def report_recall_kb(recall: dict, lang: str | None = None) -> InlineKeyboardMarkup:
    """§UX-память: одна кнопка «↻ Повторить прошлый отчёт» с деталями (аккаунт…хвост / кампания /
    период). idx=-2 — сентинел «повторить» для on_report_account (рядом с idx=-1 «весь аккаунт»)."""
    en = _lang(lang) == "en"
    acct = str(recall.get("account", ""))[-4:]
    camp = recall.get("campaign_name") or ("All" if en else "Вся")
    per = str(recall.get("period", ""))
    per_lbl = per if per == "MTD" else (f"{per}d" if en else f"{per} дн")
    label = f"↻ …{acct} · {_ellipsize(str(camp), 18)} · {per_lbl}"
    kb = InlineKeyboardBuilder()
    kb.button(text=label, callback_data=ReportAcctCB(target="report", idx=-2))
    kb.adjust(1)
    return kb.as_markup()


def _page_nav_row(kb: InlineKeyboardBuilder, kind: str, target: str, page: int, pages: int) -> int:
    """3E: ряд «‹ · N/M · ›» для PageCB-пагинации. Возвращает число добавленных кнопок (0 при
    одной странице)."""
    if pages <= 1:
        return 0
    n = 0
    if page > 0:
        kb.button(text="‹", callback_data=PageCB(kind=kind, target=target, page=page - 1))
        n += 1
    kb.button(text=f"{page + 1}/{pages}", callback_data=PageCB(kind=kind, target=target, page=page))
    n += 1
    if page < pages - 1:
        kb.button(text="›", callback_data=PageCB(kind=kind, target=target, page=page + 1))
        n += 1
    return n


def report_accounts_kb(
    rows: list,
    target: str,
    lang: str | None = None,
    *,
    last: str | None = None,
    page: int = 0,
    frequent: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """§8: выбор аккаунта для отчёта/экспорта, ПОСТРАНИЧНО (3E: раньше кнопка на каждую строку —
    >100 аккаунтов давали REPLY_MARKUP_TOO_LONG). rows — ChildAccount-подобные (.name/.id/.currency);
    idx → ГЛОБАЛЬНАЯ позиция в _REPORT_ACCT_CACHE[chat_id]. target — поток (report|export|sheets).
    last (§UX-память) — последний выбранный аккаунт: на СТРАНИЦЕ 0 первой кнопкой
    «↻ как в прошлый раз» (закреплена независимо от страницы, где живёт сам аккаунт).
    frequent (1.7) — «частые аккаунты» оператора (bm._frequent_accounts): до 3 закреплённых
    ⭐-кнопок на странице 0 (без дубля с last); при 7 дочерних срезает рутину переключения."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    total = len(rows)
    page, pages, start = _acct_page(total, page)
    extra = 0

    def _digits(v: object) -> str:
        return "".join(ch for ch in str(v) if ch.isdigit())

    last_idx = None
    if last and page == 0:
        last_n = _digits(last)
        for i, r in enumerate(rows):
            if _digits(getattr(r, "id", "")) == last_n:
                last_idx = i
                break
    if last_idx is not None:
        r = rows[last_idx]
        nm = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        repeat = f"↻ {nm} — same as last time" if en else f"↻ {nm} — как в прошлый раз"
        kb.button(text=repeat, callback_data=ReportAcctCB(target=target, idx=last_idx))
        extra += 1
    if target in ("report", "export", "sheets") and page == 0:
        # 2.2: «Все аккаунты (MCC)» — сводка/deep-xlsx по всем дочерним разом (idx=-3 сентинел;
        # -1/-2 заняты «весь аккаунт»/«повторить прошлый»).
        kb.button(
            text="📊 All accounts (MCC)" if en else "📊 Все аккаунты (MCC)",
            callback_data=ReportAcctCB(target=target, idx=-3),
        )
        extra += 1
    if frequent and page == 0:  # 1.7: ⭐-закрепы частых (только присутствующие в rows — замок цел)
        pinned: set[int] = {last_idx} if last_idx is not None else set()
        by_digits = {_digits(getattr(r, "id", "")): i for i, r in enumerate(rows)}
        for fcid in frequent[:3]:
            i = by_digits.get(_digits(fcid))
            if i is None or i in pinned:
                continue
            r = rows[i]
            nm = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
            kb.button(text=f"⭐ {nm}", callback_data=ReportAcctCB(target=target, idx=i))
            pinned.add(i)
            extra += 1
    shown = 0
    for i in range(start, min(start + _ACCT_PAGE, total)):
        r = rows[i]
        name = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        cid = getattr(r, "id", "")
        cur = getattr(r, "currency", "") or ""
        suffix = f" · {cur}" if cur else ""
        kb.button(
            text=f"🏢 {name} · {cid}{suffix}", callback_data=ReportAcctCB(target=target, idx=i)
        )
        shown += 1
    nav_n = _page_nav_row(kb, "rpta", target, page, pages)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    sizes = [1] * (extra + shown)
    if nav_n:
        sizes.append(nav_n)
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


_CAMP_PAGE = 10  # кампаний на страницу пикеров (3E)


def _search_btn(kb: InlineKeyboardBuilder, kind: str, target: str, total: int, en: bool) -> int:
    """D1: «🔎 Найти» в пикере кампаний — только когда список длиннее одной страницы (иначе
    искать нечего). Возвращает 1, если кнопка добавлена, иначе 0 (для kb.adjust)."""
    if total <= _CAMP_PAGE:
        return 0
    kb.button(
        text="🔎 Search by name" if en else "🔎 Найти по названию",
        callback_data=PickSearchCB(kind=kind, target=target, mode="start"),
    )
    return 1


def picker_search_kb(
    kind: str,
    target: str,
    camps: list[dict],
    indices: list[int],
    lang: str | None = None,
    gen: int = 0,
) -> InlineKeyboardMarkup:
    """D1: результаты поиска кампании по названию. indices — ГЛОБАЛЬНЫЕ позиции совпадений в
    кэше (_CAMP_CACHE/_REPORT_CAMP_CACHE/_RSA_CAMP_CACHE) → callback выбора работает без изменений.
    Без пагинации: >_CAMP_PAGE совпадений = «уточните запрос» (запрос в 64-байтный callback не
    влезает — уточнение и есть пагинация). Кнопка «Показать все» снимает фильтр."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    shown = indices[:_CAMP_PAGE]
    for i in shown:
        c = camps[i]
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        text = f"{mark} {_ellipsize(c['name'])}"
        if kind == "report":
            cb = ReportCampCB(target=target, idx=i)
        elif kind == "rsa":
            cb = RsaPickCB(what="camp", idx=i)
        else:  # campaigns
            cb = CampCB(action="menu", idx=i, gen=gen)
        kb.button(text=text, callback_data=cb)
    kb.button(
        text="↩︎ Show all" if en else "↩︎ Показать все",
        callback_data=PickSearchCB(kind=kind, target=target, mode="all"),
    )
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(*([1] * len(shown)), 2)
    return kb.as_markup()


def report_campaigns_kb(
    camps: list[dict], target: str, lang: str | None = None, *, page: int = 0
) -> InlineKeyboardMarkup:
    """§9: «Весь аккаунт» (idx=-1, закреплён на каждой странице) + кампании ПОСТРАНИЧНО (3E).
    idx → ГЛОБАЛЬНАЯ позиция в _REPORT_CAMP_CACHE[chat_id]. Следующий шаг — period_kb(target)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    total = len(camps)
    pages = max(1, (total + _CAMP_PAGE - 1) // _CAMP_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _CAMP_PAGE
    kb.button(
        text="📊 Whole account" if en else "📊 Весь аккаунт",
        callback_data=ReportCampCB(target=target, idx=-1),
    )
    shown = 0
    for i in range(start, min(start + _CAMP_PAGE, total)):
        c = camps[i]
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}",
            callback_data=ReportCampCB(target=target, idx=i),
        )
        shown += 1
    nav_n = _page_nav_row(kb, "rptc", target, page, pages)
    search_n = _search_btn(kb, "report", target, total, en)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    sizes = [1] * (1 + shown)
    if nav_n:
        sizes.append(nav_n)
    if search_n:
        sizes.append(search_n)
    sizes.append(1)
    kb.adjust(*sizes)
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
        text="✅ Apply the set" if en else "✅ Применить набор",  # §10 wording «Применить»
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
        text="✅ Apply" if en else "✅ Применить",  # §10 wording «✅ Применить»
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


def rsa_pick_campaigns_kb(
    camps: list[dict], lang: str | None = None, page: int = 0
) -> InlineKeyboardMarkup:
    """Визард /rsa: выбор кампании (idx → ГЛОБАЛЬНАЯ позиция в кэше), ПОСТРАНИЧНО (C8: раньше
    кнопка на каждую кампанию — на крупном аккаунте REPLY_MARKUP_TOO_LONG ронял весь флоу /rsa).
    lang принимаем для единообразия (подписи кампаний — данные, не переводятся)."""
    kb = InlineKeyboardBuilder()
    total = len(camps)
    pages = max(1, (total + _CAMP_PAGE - 1) // _CAMP_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _CAMP_PAGE
    shown = 0
    for i in range(start, min(start + _CAMP_PAGE, total)):
        c = camps[i]
        mark = {"ENABLED": "▶️", "PAUSED": "⏸"}.get(c.get("status", ""), "•")
        kb.button(
            text=f"{mark} {_ellipsize(c['name'])}", callback_data=RsaPickCB(what="camp", idx=i)
        )
        shown += 1
    nav_n = _page_nav_row(kb, "rsac", "", page, pages)
    search_n = _search_btn(kb, "rsa", "", total, _lang(lang) == "en")
    sizes = [1] * shown
    if nav_n:
        sizes.append(nav_n)
    if search_n:
        sizes.append(search_n)
    kb.adjust(*sizes)
    return kb.as_markup()


def rsa_pick_adgroups_kb(groups: list[dict], lang: str | None = None) -> InlineKeyboardMarkup:
    """Визард /rsa: выбор группы объявлений (idx → имя из кэша). lang — для единообразия сигнатур."""
    kb = InlineKeyboardBuilder()
    for i, g in enumerate(groups):
        kb.button(text=f"• {_ellipsize(g['name'])}", callback_data=RsaPickCB(what="ag", idx=i))
    kb.adjust(1)
    return kb.as_markup()

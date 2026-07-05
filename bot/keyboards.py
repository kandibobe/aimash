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
    KwAddCB,
    KwCfgCB,
    LangCB,
    ModelCB,
    MoreCB,
    NavCB,
    PageCB,
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
    BotCommand(command="newcampaign", description="Создание кампании: пошаговый визард"),
    BotCommand(command="clients", description="ℹ️ Информация про клиентов: профили и сайты"),
    BotCommand(command="client", description="Карточка клиента: /client <id>"),
    BotCommand(command="pause", description="Пауза кампании: /pause Название"),
    BotCommand(command="resume", description="Возобновить кампанию: /resume Название"),
    BotCommand(command="report", description="Сводка за период (7/30/90/MTD)"),
    BotCommand(command="export", description="Глубокий отчёт .xlsx"),
    BotCommand(command="sheets", description="Глубокий отчёт в Google Sheets (ссылка)"),
    BotCommand(command="mcc", description="Сводка по всем дочерним аккаунтам MCC"),
    BotCommand(command="account", description="Аккаунт отчётов (чтение): /account <id> | reset"),
    BotCommand(command="accounts", description="Мои доступные аккаунты (чтение)"),
    BotCommand(command="whoami", description="Мой chat_id, активный аккаунт, режим доступа"),
    BotCommand(command="refresh", description="Обновить аккаунты/кэши без рестарта"),
    BotCommand(command="quota", description="Дневная квота Google Ads API"),
    BotCommand(command="advise", description="💡 Рекомендации по улучшению аккаунта"),
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
    BotCommand(command="mcc", description="All MCC child-accounts summary"),
    BotCommand(command="account", description="Reports account (read): /account <id> | reset"),
    BotCommand(command="accounts", description="My accessible accounts (read)"),
    BotCommand(command="whoami", description="My chat_id, active account, access mode"),
    BotCommand(command="refresh", description="Refresh accounts/caches without a restart"),
    BotCommand(command="quota", description="Google Ads API daily quota"),
    BotCommand(command="advise", description="💡 Recommendations to improve the account"),
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
    geo = cfg.get("kw_geo_iso") or "UA"
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
    # 1.2: экспорт журнала ошибок файлом (.txt) — вложением, читать удобнее длинной ленты в чате.
    kb.button(text="📎 Export" if en else "📎 Экспорт", callback_data=DiagCB(action="export"))
    counts = [3]
    if is_admin:
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
        ("🔎 Search campaign (quick)", "🔎 Поисковая кампания (быстро)", "newsearch"),
        ("🎬 Campaign from video", "🎬 Кампания из видео", "newvideo"),
        ("📁 Campaign templates", "📁 Шаблоны кампаний", "templates"),
        ("↻ Recent actions", "↻ Недавние действия", "recent"),
        ("📉 API quota", "📉 Квота API", "quota"),
        ("🔔 Alert thresholds", "🔔 Пороги алертов", "alerts"),
        ("🐞 Report a bug", "🐞 Сообщить об ошибке", "reportbug"),
        ("⚙️ Service / Accounts", "⚙️ Сервис / Аккаунты", "service"),
    ):
        kb.button(text=label_en if en else label_ru, callback_data=MoreCB(action=action))
    kb.adjust(1, 1, 1, 2, 2, 1, 1)
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
}


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
    if apply_key:
        kb.adjust(1, 2)
    else:
        kb.adjust(2)
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


def cc_settings_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 1 (§19.3): ✅ Подтвердить / ✏️ Изменить / ‹ Назад / ✖ Отмена — как в ТЗ. «Изменить»
    лишь подсказывает формат правки (правка — свободным текстом в состоянии, см. bot.main)."""
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
    _cc_back_btn(kb, en)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 2)
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
    _cc_back_btn(kb, en)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def cc_kw_verify_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 2 (после генерации в Google Sheets): «✅ Использовать эти ключи» (взять сгенерированный
    список без ручной правки таблицы — P0-2: ключи уже сохранены в черновик) ИЛИ прислать ссылку на
    отредактированную таблицу для верификации; «‹ Назад» / «✖ Отмена»."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Use these keywords" if en else "✅ Использовать эти ключи",
        callback_data=CcCB(action="kw_use_generated"),
    )
    _cc_back_btn(kb, en)
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
    _cc_back_btn(kb, en)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 1, 1, 2)
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


def cc_skip_kb(lang: str | None = None) -> InlineKeyboardMarkup:
    """Этап 4/6: «⏭ Пропустить» (CcCB skip) + «✖ Отмена». Прикрепление (фото) — отдельным
    сообщением, не кнопкой."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Skip" if en else "⏭ Пропустить", callback_data=CcCB(action="skip"))
    _cc_back_btn(kb, en)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 2)
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
    _cc_back_btn(kb, en)
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    kb.adjust(1, 2)
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
def campaigns_kb(camps: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    """По кнопке на кампанию (раскрывает меню действий), ПОСТРАНИЧНО (3E: >100 кампаний давали
    REPLY_MARKUP_TOO_LONG → «код инцидента» вместо списка). idx = ГЛОБАЛЬНАЯ позиция в списке."""
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
            text=f"{mark} {_ellipsize(c['name'])}", callback_data=CampCB(action="menu", idx=i)
        )
        shown += 1
    nav_n = _page_nav_row(kb, "camp", "", page, pages)
    sizes = [1] * shown
    if nav_n:
        sizes.append(nav_n)
    kb.adjust(*sizes)
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
        text="🗑 Delete campaign" if en else "🗑 Удалить кампанию",
        callback_data=CampCB(action="delete", idx=idx),
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
    rows: list, target: str, lang: str | None = None, *, last: str | None = None, page: int = 0
) -> InlineKeyboardMarkup:
    """§8: выбор аккаунта для отчёта/экспорта, ПОСТРАНИЧНО (3E: раньше кнопка на каждую строку —
    >100 аккаунтов давали REPLY_MARKUP_TOO_LONG). rows — ChildAccount-подобные (.name/.id/.currency);
    idx → ГЛОБАЛЬНАЯ позиция в _REPORT_ACCT_CACHE[chat_id]. target — поток (report|export|sheets).
    last (§UX-память) — последний выбранный аккаунт: на СТРАНИЦЕ 0 первой кнопкой
    «↻ как в прошлый раз» (закреплена независимо от страницы, где живёт сам аккаунт)."""
    en = _lang(lang) == "en"
    kb = InlineKeyboardBuilder()
    total = len(rows)
    page, pages, start = _acct_page(total, page)
    extra = 0
    last_idx = None
    if last and page == 0:
        last_n = "".join(ch for ch in str(last) if ch.isdigit())
        for i, r in enumerate(rows):
            if "".join(ch for ch in str(getattr(r, "id", "")) if ch.isdigit()) == last_n:
                last_idx = i
                break
    if last_idx is not None:
        r = rows[last_idx]
        nm = _ellipsize(getattr(r, "name", "") or getattr(r, "id", ""))
        repeat = f"↻ {nm} — same as last time" if en else f"↻ {nm} — как в прошлый раз"
        kb.button(text=repeat, callback_data=ReportAcctCB(target=target, idx=last_idx))
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
    kb.button(text="✖ Cancel" if en else "✖ Отмена", callback_data=NavCB(action="cancel"))
    sizes = [1] * (1 + shown)
    if nav_n:
        sizes.append(nav_n)
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

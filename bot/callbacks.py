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

    target: str  # "report" | "export" | "status" | "sheets"
    code: str  # "7" | "30" | "90" | "MTD"


class ReportAcctCB(CallbackData, prefix="rpta"):
    """§8: выбор АККАУНТА для отчёта/экспорта (/report /export /sheets). idx — позиция в
    _REPORT_ACCT_CACHE[chat_id]; customer_id в callback_data НЕ кладём (64 байта + резолв по chat_id).
    Пикер перечисляет только read-allowed аккаунты (граница доступа), при выборе — авто-грант."""

    target: str  # "report" | "export" | "sheets"
    idx: int


class ReportCampCB(CallbackData, prefix="rptc"):
    """§8/§9: выбор КАМПАНИИ (или всего аккаунта) для отчёта. idx=-1 ⇒ весь аккаунт; иначе позиция
    в _REPORT_CAMP_CACHE[chat_id]. Следующий шаг — период (PeriodCB), отчёт скоупится campaign_id."""

    target: str  # "report" | "export" | "sheets"
    idx: int = -1


class KwCfgCB(CallbackData, prefix="kwc"):
    """3F (§7): экран параметров keyword research (/keywords). Тапы лишь меняют state-data
    (ГЕО/язык/сеть/период) и перерисовывают сводку; сам подбор — «🚀 Подобрать» (run). Read-only."""

    field: str  # "geo" | "geo_pick" | "lang" | "net" | "period" | "run"
    value: str = ""  # geo_pick: ISO страны | "any" | "custom"


class AlertCB(CallbackData, prefix="alr"):
    """3H (M10): настройка порогов аномалий (/alerts). Пороги — НАСТРОЙКИ БОТА (UserSettings.
    alert_thresholds), не Google Ads: confirm-гейт не нужен (как /lang, /model). field:
    spike|drop|minspend|reset; value — пресет («25»/«50»…) или 'custom' (ручной ввод через FSM)."""

    field: str
    value: str = ""


class MoreCB(CallbackData, prefix="more"):
    """3E: inline-хаб «➕ Ещё» — вторичные флоу, у которых не было входа из главного меню
    (обнаружимость: раньше только слэш-командой). Кнопка лишь МАРШРУТИЗИРУЕТ в тот же entry,
    что и команда; мутаций не создаёт."""

    action: str  # "newsearch" | "newvideo" | "templates" | "recent" | "quota" | "alerts"


class PageCB(CallbackData, prefix="pg"):
    """3E: перелистывание постраничных пикеров отчётов/кампаний (защита от
    REPLY_MARKUP_TOO_LONG на >100 строк — как B7-пагинация в визарде §19/§20).
    kind: 'rpta' (аккаунты отчёта) | 'rptc' (кампании отчёта) | 'camp' (/campaigns).
    target — поток report|export|sheets (для rpta/rptc; у camp пуст)."""

    kind: str
    target: str = ""
    page: int = 0


class RsaCB(CallbackData, prefix="rsa"):
    """Поэлементная курация RSA-текстов (ТЗ §10/§19.5.2). cid — session_id (hex uuid). kind/idx
    указывают элемент: kind 'h'|'d', idx — позиция в сессии. Для массовых действий
    (approveall/aslist/editall/regen/finalize/cancel) kind/idx не используются (дефолты).
    editall/regen — батч-ряд визарда §19 («Доработать всё» → list-UX, «Сгенерировать заново»)."""

    action: str  # "approve" | "reject" | "refine" | "review" | "overview" | "approveall" |
    # "aslist" | "editall" | "regen" | "finalize" | "cancel"
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


class GeoCB(CallbackData, prefix="geo"):
    """§3: гео-таргетинг кампании из меню /campaigns. idx — позиция кампании в _CAMP_CACHE (по
    chat_id, как CampCB). Кнопка лишь выбирает СПОСОБ; адрес/локации вводятся текстом следующим
    шагом (FSM), черновик set_geo_* собирается после ввода — confirm-гейт обязателен."""

    action: str  # "loc" | "prox" (кнопка «‹ Назад» возвращает через CampCB menu)
    idx: int = -1


class NavCB(CallbackData, prefix="nav"):
    """Универсальная навигация мастеров (FSM-визардов): только ОТМЕНА. «‹ Назад» НЕ здесь —
    он переиспользует родительский callback каждого flow (breadcrumb: CampCB/RsaPickCB/…),
    т.к. родитель-экран знает, как себя пере-отрисовать. Отмена контекст-свободна → один общий
    хендлер: state.clear() + возврат главного меню. Черновик НЕ создаётся (это чистый UI/FSM)."""

    action: str  # "cancel"


class TemplateCB(CallbackData, prefix="tpl"):
    """§2B: именованные шаблоны кампаний (/templates). idx — позиция в _TPL_CACHE[chat_id]
    (имя в callback_data не кладём: длинное/спецсимволы). use → спросить имя новой кампании и
    собрать черновик create_search_campaign (confirm-гейт); del → удалить шаблон."""

    action: str  # "use" | "del"
    idx: int = -1


class RecentCB(CallbackData, prefix="rcnt"):
    """§2C: авто-память — «↻ повторить» недавнее применённое действие. idx — позиция в
    _RECENT_CACHE[chat_id]. Повтор пере-собирает ТЕ ЖЕ params → ре-валидация → confirm-гейт
    (пользователь снова видит «было→станет» и жмёт ✅). Гейт не обходится."""

    action: str  # "repeat"
    idx: int = -1


class CcCB(CallbackData, prefix="cc"):
    """§19: визард «➕ Создание кампании». Контекст — активный черновик чата (campaign_drafts),
    session_id в callback_data НЕ кладём (резолвится по chat_id через CampaignDraftStore.get_active).
    Кнопки лишь двигают визард/накапливают черновик — РЕАЛЬНАЯ мутация только на «Создать черновик»
    (action='create') и «Запустить» (action='launch') через confirm-гейт. idx — индекс аккаунта в
    кэше (Этап 0) или под-индекс; sub — тег стадии/под-действие."""

    action: str  # "acct"|"page"|"accept"|"skip"|"edit"|"kw_generate"|"kw_confirm"|"use_assets"|
    # "add_assets"|"asset_type"|"assets_back"|"create"|"launch"|"resume"|"new"
    idx: int = -1
    sub: str = ""


class VideoCB(CallbackData, prefix="vid"):
    """§11: визард кампании из видео (YouTube). Кнопки лишь двигают визард: выбор типа кампании
    (Demand Gen / Video) и пропуск логотипа. РЕАЛЬНАЯ мутация — только через confirm-гейт
    (proposal create_demand_gen_campaign / create_video_campaign)."""

    action: str  # "dg" | "video" | "logo_skip"


class ClientCB(CallbackData, prefix="cli"):
    """§20: раздел «ℹ️ Информация про клиентов». Контекст (выбранный customer_id) держит FSM —
    в callback_data его НЕ кладём (резолв idx→аккаунт по chat_id через _CLI_ACCT_CACHE, как CcCB).
    Кнопки лишь двигают UI/накопление; РЕАЛЬНАЯ мутация памяти (save/update/clear) — только через
    confirm-гейт (proposal profile_* → ✅). action: acct|card|add|update|save|clear|recrawl|back;
    idx — индекс аккаунта в кэше; sub — под-действие (напр. режим краула full|incr)."""

    action: str  # acct|card|add|update|save|clear|recrawl|back
    idx: int = -1
    sub: str = ""


class ExtCB(CallbackData, prefix="ext"):
    """§3-assets: ассеты-расширения кампании из меню /campaigns. idx — кампания в _CAMP_CACHE
    (для remove — строка в _EXT_CACHE). action: type-меню/выбор типа/показать/удалить. sub —
    заголовок структурного описания (для snippet) или иной под-параметр. Кнопка лишь собирает
    ввод; черновик и confirm-гейт — после ввода (мутация только по «да»)."""

    action: str  # "sitelink" | "callout" | "snippet" | "snip_h" | "show" | "remove"
    idx: int = -1
    sub: str = ""  # header структурного описания (для action='snip_h')

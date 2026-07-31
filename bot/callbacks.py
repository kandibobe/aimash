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
    """Действия в меню /campaigns. idx — позиция в последнем списке кампаний (по chat_id).

    gen — ПОКОЛЕНИЕ списка, из которого нарисована кнопка (как у KwAddCB/SlashMutCB). Кэш
    `_CAMP_CACHE[chat_id]` перезаписывается при каждом /campaigns, в том числе на ДРУГОМ
    аккаунте: без gen старая клавиатура аккаунта A резолвила idx в список аккаунта B, и
    «🗑 Удалить» уезжало на чужую кампанию. Хендлер сверяет gen со своим снимком → «список устарел».
    """

    action: str  # "menu" | "pause" | "resume" | "delete" | "audience" | "geo" | "ext" | "back"
    idx: int
    gen: int = 0


class AudienceCB(CallbackData, prefix="aud"):
    """Прикрепление аудитории к кампании (§3). camp_idx — кампания в _CAMP_CACHE (выбрана на
    предыдущем шаге), idx — аудитория в _AUD_CACHE (оба резолвятся по chat_id в bot.main)."""

    action: str  # "pick"
    camp_idx: int = -1
    idx: int = -1


class PeriodCB(CallbackData, prefix="per"):
    """Выбор периода для отчёта/статистики (пресеты ТЗ §9; 3.1 — во всех отчётных командах).
    code='CUST' — «Свой период…»: одноразовое состояние PeriodCustom, диапазон текстом
    (parse_period_text). Все target read-only; диспатч — bot.main._dispatch_period_target."""

    target: (
        str  # "report" | "export" | "sheets" | "status" | "audit" | "bids" | "searchterms" | "mcc"
    )
    code: str  # "7" | "14" | "30" | "90" | "MTD" | "LM" | "CUST"


class AuditExportCB(CallbackData, prefix="auex"):
    """Выгрузка результата /audit файлом/ссылкой (кнопки под карточкой аудита). fmt — формат:
    'sheets' (Google Sheets, ссылка reader) | 'xlsx' (файл) | 'docx' (Word-документ). Аккаунт и сам
    AuditResult берём из _AUDIT_EXPORT_CACHE[chat_id] (пере-собирать аудит не нужно — он уже
    посчитан). GR3: экспорт — БУМАГА (нет колонки «применить»); Google Ads не мутирует, proposal
    не создаёт."""

    fmt: str  # "sheets" | "xlsx" | "docx"


class AuditQaCB(CallbackData, prefix="auqa"):
    """#6: выход из режима доп-вопросов (Q&A) по /audit кнопкой «✖ Выйти». Само состояние живёт в
    FSM (states.AuditQA.active), контекст — в _AUDIT_QA_CACHE[chat_id]. Кнопка — лишь явный выход;
    пассивно из режима выводит любая /команда или кнопка меню (middleware). Никаких мутаций."""

    action: str  # "exit"


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

    action: str  # newsearch|newvideo|templates|recent|quota|alerts|advise|reportbug|service|svc_*


class PageCB(CallbackData, prefix="pg"):
    """3E: перелистывание постраничных пикеров отчётов/кампаний (защита от
    REPLY_MARKUP_TOO_LONG на >100 строк — как B7-пагинация в визарде §19/§20).
    kind: 'rpta' (аккаунты отчёта) | 'rptc' (кампании отчёта) | 'camp' (/campaigns) |
    'rsac' (пикер /rsa, C8) | 'aud' (аудитории кампании, C8).
    target — поток report|export|sheets (rpta/rptc); camp_idx (aud); у camp/rsac пуст."""

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


class ClarifyCB(CallbackData, prefix="clq"):
    """Inline-ответ на ask_clarification. token защищает от stale-кнопок, idx указывает на
    позицию варианта в последнем clarify чата. action='other' просит ввести свой ответ текстом."""

    action: str  # "pick" | "other"
    token: str
    idx: int = -1


class KwAddCB(CallbackData, prefix="kwadd"):
    """§7: добавить подобранные ключи (из результатов /keywords) в кампанию — только по команде,
    после показа списка + типа соответствия и «да» (confirm-гейт). token — ключ серверной сессии
    (список ключей не влезает в callback_data, 64 байта). mt — тип соответствия (для action='match')."""

    action: str  # "start" | "match" | "cancel" | "camp"
    token: str  # hex-токен сессии _KW_ADD (bot.main)
    mt: str = ""  # "broad" | "phrase" | "exact" (только для action='match')
    idx: int = -1  # D3: индекс кампании в _KW_ADD_CAMP_CACHE (только для action='camp')
    gen: int = 0  # поколение списка кампаний для action='camp' (N1.4-ревью, как SlashMutCB.gen)


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


class PickSearchCB(CallbackData, prefix="psr"):
    """D1: «🔎 Найти» в пикерах кампаний. kind — какой пикер (campaigns|report|rsa);
    target — поток отчёта (report|export|sheets) для kind=report, иначе пусто. mode:
    'start' → войти в поиск (ждать текст); 'all' → снять фильтр, показать весь список."""

    kind: str  # "campaigns" | "report" | "rsa"
    target: str = ""
    mode: str = "start"  # "start" | "all"


class RollbackCB(CallbackData, prefix="rbk"):
    """D2: «↩️ Откатить» применённую обратимую операцию. token — ключ _ROLLBACK_CACHE[chat_id]
    (не confirmation_id: обратный черновик родится при клике). Откат НЕ исполняется сам —
    минтит ОБРАТНЫЙ proposal за тем же confirm-гейтом (бюджет-откат = user_initiated, правило 3)."""

    token: str


class JournalRollbackCB(CallbackData, prefix="rbj"):
    """Доп.2B: «↩️ Откатить» из /journal — ПЕРСИСТЕНТНЫЙ откат по confirmation_id (не in-memory
    token, как RollbackCB, гибнущий при рестарте). Применённую строку находим в БД, реверс собираем
    из proposals.params['_before']. Минтит ОБРАТНЫЙ черновик за confirm-гейтом (НЕ исполняет)."""

    cid: str


class SearchBidCB(CallbackData, prefix="sbid"):
    """A7: «✏️ Изменить ставку» на карточке черновика create_search_campaign (/newsearch). token —
    ключ _SEARCH_BID_EDIT[chat_id] (не confirmation_id: правка ставки минтит НОВЫЙ черновик). Не
    исполняет — только просит новое значение CPC и пере-показывает черновик за тем же confirm-гейтом."""

    token: str


class SlashMutCB(CallbackData, prefix="smut"):
    """D4: пикер кампаний для /pause и /resume без аргумента. op — pause_campaign|resume_campaign;
    idx — позиция кампании в _SLASH_MUT_CACHE[chat_id]. gen — поколение списка (N1.4-ревью):
    кэш перезаписывается вторым писателем (fuzzy-подсказка опечатки), клик по кнопке СТАРОЙ
    клавиатуры резолвил бы idx в ДРУГОЙ список — несовпадение gen ⇒ «список устарел».
    Клик минтит proposal (confirm-гейт)."""

    op: str
    idx: int
    gen: int = 0


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
    # "add_assets"|"asset_type"|"assets_back"|"reuse_type"|"reuse_done"|"create"|"launch"|"resume"|"new"
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
    confirm-гейт (proposal profile_* → ✅). action: acct|card|add|update|save|clear|recrawl|dossier|back;
    idx — индекс аккаунта в кэше; sub — под-действие (напр. режим краула full|incr).

    dossier — отдать .md-файл текущего досье (read-only: файл уже в БД, LLM не зовём)."""

    action: str  # acct|card|add|update|save|clear|recrawl|dossier|back
    idx: int = -1
    sub: str = ""


class AdminCB(CallbackData, prefix="adm"):
    """P0-A (§6/§12): выбор доступа после /adduser. Stateless — цель и аккаунт в самом callback_data
    (chat = целевой chat_id оператора; cid = customer_id для точечного гранта). Кнопки лишь выдают
    ГРАНТЫ ЧТЕНИЯ (account_access) — мутации НЕ открывают (замок ensure_allowed отдельный). action:
    all (грант всех дочерних) | pick (показать пикер) | grant (грант одного cid) | done (готово)."""

    action: str  # "all" | "pick" | "grant" | "done"
    chat: int = 0  # целевой chat_id оператора
    cid: str = ""  # customer_id (для action='grant')


class AdviseCB(CallbackData, prefix="adv"):
    """advisor: обратная связь на рекомендацию (/advise, утренний дайджест). rec — rec_uid (hex 32
    симв., влезает в 64-байт callback_data). Кнопки 👍/👎 пишут ТОЛЬКО в recommendation_feedback,
    🙈 (dismiss) — status='dismissed' (Слой B, «показано и проигнорировано»); apply СТАРТУЕТ
    confirm-гейт по тапу человека; auto — тумблер проактива. Google Ads НЕ трогают и proposal
    сами НЕ создают (инвариант test_advisor)."""

    action: str  # "up" | "down" | "dismiss" | "apply" | "auto"
    rec: str  # rec_uid (hex uuid); для auto — "on"|"off"


class MySchedCB(CallbackData, prefix="msch"):
    """2.11 (§14): /myschedule — персональное расписание планового отчёта (настройка БОТА,
    UserSettings.report_schedule; confirm-гейт не нужен, как /alerts). action:
    daily|weekly|custom|off."""

    action: str  # "daily" | "weekly" | "custom" | "off"


class ThrTuneCB(CallbackData, prefix="thr"):
    """2.11 (§14): предложение авто-подстройки порогов аномалий (scheduler.run_threshold_tuning).
    Кнопка «Принять» пишет UserSettings.alert_thresholds['per_account'] ТОЛЬКО по тапу человека
    (настройка бота, НЕ мутация Ads). token — hex12 из ui_prefs-блоба (анти-stale)."""

    action: str  # "acc" | "dec"
    token: str  # hex12 предложения


class DiagCB(CallbackData, prefix="diag"):
    """A3 (§15): кнопки под /diag (последние error_events). Read-only навигация — БД/Ads не мутирует.
    action: refresh (перечитать) | today (только за сегодня UTC) | all (снять фильтр) |
    detail (карточка инцидента — ТОЛЬКО админ) | export (журнал файлом).
    rid — request_id (≤16 симв., влезает в 64-байт callback_data) — подпись кнопки и фолбэк;
    eid — PK error_events (замечание 9: request_id НЕ уникален, detail открывал не тот инцидент)."""

    action: str  # "refresh" | "today" | "all" | "detail" | "export"
    rid: str = ""  # request_id (для detail)
    eid: int = 0  # error_events.id (0 — кнопка отрисована до апдейта, фолбэк по rid)


class BugCB(CallbackData, prefix="bug"):
    """§6: триаж баг-репорта (/bugs, ТОЛЬКО админ). Кнопки меняют статус строки bug_reports —
    Google Ads НЕ трогают, proposal НЕ создают. action: triaged|closed|refresh; bid — id репорта."""

    action: str  # "triaged" | "closed" | "refresh"
    bid: int = 0  # BugReport.id


class ExtCB(CallbackData, prefix="ext"):
    """§3-assets: ассеты-расширения кампании из меню /campaigns. idx — кампания в _CAMP_CACHE
    (для remove — строка в _EXT_CACHE). action: type-меню/выбор типа/показать/удалить. sub —
    заголовок структурного описания (для snippet) или иной под-параметр. Кнопка лишь собирает
    ввод; черновик и confirm-гейт — после ввода (мутация только по «да»)."""

    action: str  # "sitelink" | "callout" | "snippet" | "snip_h" | "show" | "remove"
    idx: int = -1
    sub: str = ""  # header структурного описания (для action='snip_h')


class SearchTermsCB(CallbackData, prefix="sterm"):
    """§7: /searchterms — топ «мусорных» поисковых запросов (клики без конверсий) → предложить
    в МИНУС-слова. idx — позиция запроса в _SEARCH_TERMS_CACHE[chat_id]; gen — поколение списка
    (анти-stale, как SlashMutCB.gen). SDK-вызов НЕ здесь — только после подтверждения (правило 1/2).

    3.2а (2026-07-17): БАТЧ чекбоксами вместо «кнопка = один proposal». Выбор/настройки живут
    server-side (_SEARCH_TERMS_SEL/_SEARCH_TERMS_OPTS по chat_id, ротация тем же gen — в callback
    только idx+gen, 64-байтный лимит Telegram):
      'toggle' — отметить/снять запрос (перерисовка только markup);
      'mt'     — цикл типа соответствия exact → phrase → broad;
      'lvl'    — цикл уровня: кампания → группа объявлений → общий список;
      'ss_open'/'ss_pick'/'ss_back' — пикер общего списка минус-слов (idx в opts['ss_choices'];
                 idx=-1 — новый список с дефолтным именем, создаст apply ПОСЛЕ подтверждения);
      'apply'  — минтит черновик(и) на ВЕСЬ выбор: один на пакет; уровень кампании/группы при
                 запросах из разных кампаний — по одному на кампанию (схема привязана к одной).
    'neg' (легаси-кнопки в истории чата) — обрабатывается как toggle: gen старый ⇒ «список устарел».

    Ф4 (2026-07-14): action='add' — ОБРАТНЫЙ ход («сбор урожая»): конвертящий запрос без своего
    ключа → черновик add_keywords (EXACT, в ту же группу). idx указывает в _SEARCH_TERMS_HARVEST —
    ДРУГОЙ кэш, поколение общее (обе клавиатуры минтит один /searchterms)."""

    action: str  # "toggle"|"mt"|"lvl"|"ss_open"|"ss_pick"|"ss_back"|"apply"|"add"|"cancel"|"neg"
    idx: int = -1
    gen: int = 0


class MccAcctCB(CallbackData, prefix="mccacct"):
    """3.5: тап по аккаунту под сводкой /mcc → закрепить его АКТИВНЫМ аккаунтом чтения (персист,
    как target='setacct' в пикере отчёта). Несёт САМ customer_id, а не индекс кэша: кнопка
    переживает рестарт и не съезжает, когда другой пикер перезапишет _REPORT_ACCT_CACHE.
    Замок перепроверяется в хендлере (ensure_read_allowed × пер-юзер грант, fail-closed)."""

    cid: str


class MccAuditCB(CallbackData, prefix="mccau"):
    """3.5: «Прогнать аудит по всем» под сводкой /mcc — ФОНОВЫЙ score-прогон читаемых дочерних
    (~30 GAQL/аккаунт — инлайн нельзя). READ-ONLY: только чтение + снапшот в локальную БД,
    мутаций/proposal нет. Список аккаунтов — в _MCC_AUDIT_CACHE (кэш потерян → stale-alert)."""

    action: str = "run"

"""Telegram-бот Aimash (aiogram 3.x).

- whitelist по chat_id (TELEGRAM_WHITELIST_CHAT_IDS);
- меню-кнопка (set_my_commands) + постоянная reply-клавиатура (bot/keyboards.py);
- свободный текст → агент-цикл (agent.loop.handle_command);
- read → статистика; mutation → черновик «было→станет» (в БД) + inline ✅/❌ (typed CallbackData);
- кнопки /campaigns (пауза/возобновление) лишь СОЗДАЮТ черновик — исполнение только после «да»;
- на «да» → реальное выполнение через ads.service за confirm-гейтом + audit; ничего без «да».
"""

from __future__ import annotations

import asyncio
import io
import re
import uuid
from typing import TypeGuard
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from adcopy.display_path import build_display_path
from adcopy.generate import CopyBrief
from adcopy.generate import generate_rsa as _generate_rsa
from adcopy.refine import refine_element
from adcopy.session import SessionStore
from adcopy.validate import (
    RSA_MAX_DESCRIPTIONS,
    RSA_MAX_HEADLINES,
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
)
from adcopy.validate import validate as rsa_validate
from ads.assets import (
    clear_pending_media,
    clear_pending_media_ids,
    prepare_display_images,
    save_pending_media,
)
from ads.client import DRAFT_ACCOUNT_ID, ensure_allowed, ensure_read_allowed
from ads.mutations import GDN_BUSINESS_NAME_MAX, VIDEO_DESCRIPTION_MAX
from ads.resolve import currency_mismatch, detect_currency_token, find_ad_groups
from ads.service import execute_confirmed, read_before
from clients import crawl_jobs, crawler
from clients.execute import MEMORY_OPERATIONS, execute_confirmed_memory
from clients.profile_extract import extract_profile, structure_crawl
from clients.store import ClientProfileStore, preview_merge
from agent import router
from agent.campaign_settings import (
    _valid_iso_date,
    assemble_settings,
    derive_bidding,
    extract_campaign_settings,
    parse_ad_schedule,
    schedule_human,
    units_to_micros,
)
from agent.loop import handle_command
from agent.tools.schemas import MAX_CAMPAIGN_KEYWORDS, SCHEMAS
from bot import i18n, texts, ux
from bot.campaign_wizard.store import CampaignDraftStore
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
    SlashMutCB,
    TemplateCB,
    ThrTuneCB,
    VideoCB,
)
from bot.keyboards import (
    _CAMP_PAGE,  # D1: размер страницы пикера (для «показано N из M» в поиске)
    ADVISE_APPLY_OPS,
    ALL_MENU_BUTTONS,
    BOT_COMMANDS,
    BOT_COMMANDS_EN,
    BTN_BALANCE_ALL,
    adduser_access_kb,
    adduser_pick_kb,
    advise_feedback_kb,
    advise_header_kb,
    alerts_kb,
    BTN_CAMPAIGNS_ALL,
    BTN_EXPORT_ALL,
    BTN_HELP_ALL,
    BTN_JOURNAL_ALL,
    BTN_CLIENTS_ALL,
    BTN_KEYWORDS_ALL,
    BTN_LANG_ALL,
    BTN_MCC_ALL,
    BTN_MODEL_ALL,
    BTN_MORE_ALL,
    BTN_NEWCAMPAIGN_ALL,
    BTN_REPORT_ALL,
    BTN_RSA_ALL,
    BTN_SHEETS_ALL,
    BTN_STATUS_ALL,
    audiences_kb,
    campaign_actions_kb,
    campaign_network_kb,
    campaigns_kb,
    cc_accounts_kb,
    client_card_kb,
    client_input_kb,
    client_save_kb,
    client_show_card_kb,
    clients_accounts_kb,
    cc_asset_types_kb,
    cc_assets_kb,
    cc_exit_kb,
    cc_final_kb,
    cc_kw_confirm_kb,
    cc_kw_kb,
    cc_kw_verify_kb,
    cc_resume_kb,
    bugs_kb,
    cc_settings_kb,
    cc_skip_kb,
    confirm_destructive_kb,
    confirm_final_kb,
    confirm_kb,
    diag_kb,
    ext_assets_list_kb,
    ext_menu_kb,
    ext_snippet_header_kb,
    geo_mode_kb,
    kw_add_campaigns_kb,
    kw_geo_kb,
    kw_lang_kb,
    kw_params_kb,
    lang_kb,
    main_menu,
    more_menu_kb,
    mysched_kb,
    match_type_kb,
    thr_tune_kb,
    model_kb,
    nav_kb,
    recent_kb,
    service_menu_kb,
    templates_kb,
    video_logo_kb,
    video_type_kb,
    period_kb,
    picker_search_kb,
    post_create_kb,
    report_accounts_kb,
    report_campaigns_kb,
    report_recall_kb,
    rollback_kb,
    slash_mutate_campaigns_kb,
    rsa_aslist_kb,
    rsa_item_kb,
    rsa_overview_kb,
    rsa_pick_adgroups_kb,
    rsa_pick_campaigns_kb,
)
from bot.throttle import ThrottleMiddleware
from confirm.gate import Proposal, build_summary
from confirm.store import ConfirmStore
from core import ingest
from core import twofa  # §12 2FA-гейт опасных операций (opt-in, дефолт OFF, fail-closed)
from core.access import ensure_account_allowed_for_user, is_whitelisted
from core.ads_errors import humanize_google_ads_error, is_account_access_error
from core.config import normalize_customer_id, settings
from core.context import new_request_id, request_scope, reset_context, set_context
from core.errors import capture_exception
from core.limits import MONEY_MAX_UNITS  # единый источник денежного потолка (defense-in-depth)
from core.logging import log, redact_text, setup_logging
from core.observability import init_observability
from core.resilience import run_ads_read_call
from db.session import (
    acquire_single_instance_lock,
    dispose_engine,
    init_db,
    release_single_instance_lock,
)

STORE = ConfirmStore()  # черновики + audit в БД (SQLite dev), вместо очереди в памяти
SESSIONS = SessionStore()  # сессии курации RSA (фаза 2.C), персист в proposals (rsa_curation)
CDRAFTS = (
    CampaignDraftStore()
)  # §19: персист черновика визарда «Создание кампании» (campaign_drafts)
CLIENTS = ClientProfileStore()  # §20: профили клиентов (client_profiles); чтение/запись per-account

# Приветственный баннер к /start (генерится scripts/make_welcome_image.py, закоммичен в репо).
# Кэш file_id после первой загрузки — чтобы не перезаливать PNG в Telegram на каждый /start.
WELCOME_IMG = Path(__file__).resolve().parent / "assets" / "welcome.png"
_welcome_file_id: str | None = None

# Операции со списком ключей — большой список в черновике уходит .xlsx-вложением (ТЗ §5).
# remove_negative_keywords ОБЯЗАН быть здесь: fmt_mutation_summary усекает его список до
# KW_INLINE_MAX и обещает «…полный список во вложении .xlsx» — без этой операции в наборе вложение
# не слалось, и менеджер жал ✅ на объёме, который не видел (нарушение §5/golden rule #1).
_KEYWORD_OPS = frozenset(
    {"add_keywords", "remove_keywords", "add_negative_keywords", "remove_negative_keywords"}
)

# P1-6: необратимые удаления — карточка подтверждения проходит ДВА шага (confirm_destructive_kb →
# confirm_final_kb). Замок аккаунта (ensure_allowed) и confirm-гейт неизменны; это доп. защита в UI.
_DESTRUCTIVE_OPS = frozenset(
    {"remove_campaign", "remove_ad_group", "remove_ad", "remove_keywords", "remove_asset_link"}
)

# Лёгкое in-memory состояние UI (теряется при рестарте — это ок, не источник истины):
_CAMP_CACHE: dict[int, list[dict]] = {}  # chat_id → последний список кампаний (резолв idx→имя)
# §8/мультиаккаунт: аккаунт, с которого прочитан текущий _CAMP_CACHE — ЯКОРЬ для чтений/мутаций
# меню /campaigns (читаю и мутирую ОДИН аккаунт, без mismatch). Дефолт (нет записи) = Draft.
# Замок мутаций (ensure_allowed) держится в _present_proposal: не-Draft вне allow-list → отказ.
_CAMP_ACCT: dict[int, str] = {}
_LAST_PENDING: dict[int, str] = {}  # chat_id → confirmation_id последнего черновика (для /cancel)
# §2B: params последнего черновика create_search_campaign на чат — материал для /savetemplate
# «сохранить как шаблон». В памяти (как _LAST_PENDING); секретов нет.
_LAST_SEARCH_PARAMS: dict[int, dict] = {}
# C1 (гибрид): пер-чат контекст диалога для разрешения ссылок-местоимений («эта кампания»).
# {chat_id: {"campaign": str, "customer_id": str, "history": [последние реплики пользователя]}}.
# В памяти (теряется при рестарте — это ок, не источник истины). Секретов нет.
_CHAT_CTX: dict[int, dict] = {}
_CHAT_CTX_HISTORY = 4  # сколько последних реплик пользователя держим для контекста
_TPL_CACHE: dict[int, list] = {}  # chat_id → последний показанный список шаблонов (резолв idx→имя)
_RECENT_CACHE: dict[
    int, list
] = {}  # §2C: chat_id → последние применённые действия (резолв idx→action)
_EXT_CACHE: dict[
    int, list
] = {}  # §3-assets: chat_id → текущие ассеты кампании (резолв idx→link rn)
# ingest: chat_id → {text, source} прочитанного файла, ждущего задачу (в памяти, без секретов).
_PENDING_CONTEXT: dict[int, dict] = {}

# §7: эфемерные сессии «добавить подобранные ключи» (token → {keywords, src}). Список ключей не
# влезает в callback_data (64 байта) → держим по короткому токену. Кап защищает от роста.
_KW_ADD: dict[str, dict] = {}
_KW_ADD_MAX = 200


def _kw_add_put(keywords: list[str], src: str) -> str:
    """Сохранить подобранные ключи под новым токеном; вернуть токен. Эвикт старейших при переполнении."""
    import uuid

    while len(_KW_ADD) >= _KW_ADD_MAX:
        _KW_ADD.pop(next(iter(_KW_ADD)), None)  # dict хранит порядок вставки → первый = старейший
    token = uuid.uuid4().hex
    _KW_ADD[token] = {"keywords": list(keywords), "src": src}
    return token


_RSA_CAMP_CACHE: dict[int, list[dict]] = {}  # chat_id → кампании для визарда /rsa
_RSA_AG_CACHE: dict[int, list[dict]] = {}  # chat_id → группы объявлений для визарда /rsa
_CC_ACCT_CACHE: dict[
    int, list
] = {}  # §19: chat_id → дочерние аккаунты MCC (резолв idx→ChildAccount)
# §19.8: chat_id → имя только что созданного PAUSED-черновика — LEGACY-фолбэк для кнопок
# «🚀 Запустить» без sub (сообщения, отправленные до деплоя restart-durability). Новые кнопки
# несут confirmation_id создания в callback_data и резолвятся из БД (переживают рестарт).
_CC_LAUNCH_CACHE: dict[int, str] = {}
# §19.8: одноразовость кнопки запуска ВНУТРИ процесса (confirmation_id создания). После рестарта
# набор пуст → кнопка сработает ещё раз; это желаемая живучесть: двойной запуск всё равно за
# confirm-гейтом, а resume уже ENABLED-кампании — no-op.
_CC_LAUNCH_DONE: set[str] = set()
# §19.8/§11: операции, создающие кампанию (после успеха предлагаем «🚀 Запустить»). Модульная
# константа: используется и в _do_confirm (успех create), и в cc_launch (валидация op по sub).
_CREATE_CAMPAIGN_OPS = frozenset(
    {
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    }
)
# §20: chat_id → аккаунты MCC для раздела «Клиенты» (резолв idx→ChildAccount); буфер накопления
# текста профиля до «💾 Сохранить» (несколько сообщений подряд, §20.3). В памяти (не источник истины).
_CLI_ACCT_CACHE: dict[int, list] = {}
_CLI_WITH_PROFILE: dict[
    int, set[str]
] = {}  # §20/B7: chat_id → аккаунты с профилем (для перелистывания)
_CLI_TEXT_BUF: dict[int, list[str]] = {}
# §20.3/B13: chat_id → таймер авто-сохранения накопленного текста профиля (сброс при новом сообщении).
_CLI_IDLE_TASK: dict[int, asyncio.Task] = {}
# §20.4: живые фоновые задачи краулинга по customer_id (держим ссылку, чтобы GC не собрал; pop в
# done-callback). Ключ по аккаунту — чтобы двойной клик «🔄 Перекраулить» не плодил параллельные обходы.
_CRAWL_INFLIGHT: dict[str, asyncio.Task] = {}
_AUD_CACHE: dict[
    int, list
] = {}  # chat_id → последний список аудиторий (§3, резолв idx→resource_name)
# C7: chat_id → ПРИКРЕПЛЁННЫЕ аудитории выбранной кампании (резолв idx→resource_name для 🗑).
_AUD_DET_CACHE: dict[int, list] = {}
# §8/§9: пикер отчётов (аккаунт → кампания → период) для /report /export /sheets. Всё в памяти
# (не источник истины): idx в callback резолвится по chat_id, выбранный аккаунт персистится отдельно
# (_save_selected_account, как /account). _REPORT_SEL держит текущий выбор до нажатия периода.
_REPORT_ACCT_CACHE: dict[int, list] = {}  # chat_id → read-allowed аккаунты (idx→ChildAccount)
_REPORT_CAMP_CACHE: dict[int, list[dict]] = {}  # chat_id → кампании выбранного аккаунта (idx→dict)
_REPORT_SEL: dict[int, dict] = {}  # chat_id → {"account", "campaign_id", "campaign_name"}
# 2.2: сентинел «Все аккаунты (MCC)» в пикере /export → deep-xlsx по всем дочерним. НЕ аккаунт:
# normalize_customer_id('') на нём пуст ⇒ _report_target fail-closed откатится на Draft, если
# сентинел каким-то образом доживёт до одиночного пути.
MCC_ALL = "__mcc__"
_ADVISE_TOPIC_CACHE: dict[int, str | None] = {}  # chat_id → тема /advise, переживает выбор в пикере


# §12: chat_id → ожидающий 2FA-код черновик {"cid", "cq" (исходный ✅-CallbackQuery), "attempts"}.
# Черновик при ожидании остаётся `pending` (не сжигается) — неверный код/отмена → повторить ✅ можно.
_TWOFA_PENDING: dict[int, dict] = {}
_TWOFA_MAX_ATTEMPTS = 3  # неверных вводов до отмены (черновик НЕ сжигается — повторить ✅ можно)


# 4A: FSM-состояния всех визардов вынесены в bot/states.py (декомпозиция god-module).
# Явный ре-импорт сохраняет имена атрибутами bot.main -> bm.<Wizard> в хендлерах и monkeypatch
# тестов работают без изменений (позднее связывание).
from bot.states import (  # noqa: E402
    AlertsWizard,
    BugReportWizard,
    ClientInfoWizard,
    CreateCampaignWizard,
    ExtWizard,
    Geo,
    GdnWizard,
    IngestWizard,
    KwAdd,
    KwWizard,
    ModelWizard,
    MyScheduleWizard,
    PickerSearch,
    RsaList,
    RsaRefine,
    RsaWizard,
    SearchWizard,
    TplWizard,
    TwoFactor,
    VideoWizard,
)

# Глобальные настройки бота (модель ИИ и т.п.) живут в одной строке user_settings с этим chat_id.
# Модель — общая на процесс (места вызова chat() в adcopy/keywords не знают chat_id), поэтому
# и персист глобальный, а не на пользователя.
GLOBAL_SETTINGS_CHAT_ID = 0


dp = Dispatcher()

# ── КРИТИЧНО: обход double-import при `python -m bot.main` (prod-инцидент 2026-07-03) ──────
# `python -m bot.main` исполняет ЭТОТ файл как модуль `__main__`. Хендлеры в bot/handlers/*
# делают `import bot.main as bm`; без этой строки Python НЕ находит 'bot.main' в sys.modules и
# ИСПОЛНЯЕТ файл ПОВТОРНО отдельным модулем → получаются ДВА разных Dispatcher (main() поллит
# пустой → «бот молчит») И ломается ПОРЯДОК регистрации (циклический ре-импорт ставит fallback/
# on_text ПЕРЕД командами → /start и др. проглатывает LLM-фолбэк). Регистрируем этот модуль под
# именем 'bot.main' ДО импорта хендлеров → `import bot.main` вернёт ЭТОТ объект (тот же dp, тот
# же порядок). При обычном импорте 'bot.main' уже в sys.modules → setdefault это no-op (тесты/
# скрипты, зовущие `import bot.main`, не затронуты). Инвариант закреплён tests/test_entrypoint_dp.py.
if __name__ == "__main__":  # срабатывает только при `python -m bot.main`
    import sys as _sys

    _sys.modules.setdefault("bot.main", _sys.modules["__main__"])


def _event_chat_id(event: object) -> int | None:
    """chat_id из Message- или CallbackQuery-подобного события (дакт-тайпинг, как bot.throttle —
    работает и с aiogram-объектами, и с фейками в тестах). None => входа без чата (блокируем)."""
    chat = getattr(event, "chat", None)  # Message-like
    if chat is not None:
        return getattr(chat, "id", None)
    msg = getattr(event, "message", None)  # CallbackQuery-like
    if msg is not None:
        c = getattr(msg, "chat", None)
        if c is not None:
            return getattr(c, "id", None)
    return None


def _event_chat_type(event: object) -> str | None:
    """Тип чата ('private'|'group'|'supergroup'|'channel') из Message- или CallbackQuery-события."""
    chat = getattr(event, "chat", None)  # Message-like
    if chat is None:
        msg = getattr(event, "message", None)  # CallbackQuery-like
        chat = getattr(msg, "chat", None)
    return getattr(chat, "type", None)


def _event_op(event: object) -> str:
    """Грубая метка операции для логов (§15): команда сообщения / префикс callback-data / тип события.
    Только для наблюдаемости — попадает в request-контекст, не влияет на маршрутизацию."""
    data = getattr(event, "data", None)  # CallbackQuery.data ('modelcb:set:0' → 'modelcb')
    if isinstance(data, str) and data:
        return data.split(":", 1)[0][:32]
    text = getattr(event, "text", None)  # Message.text
    if isinstance(text, str) and text.startswith("/"):
        return text.split()[0][:32]
    if isinstance(text, str):
        return "text"
    return type(event).__name__.lower()[:32]


class TraceMiddleware(BaseMiddleware):
    """Назначает корреляционный request_id (+ chat_id/operation) на время обработки апдейта (§15) —
    все логи и перехваченные ошибки этого апдейта сшиваются по request_id. Регистрируется САМЫМ
    внешним (до whitelist), чтобы даже отказ доступа логировался с request_id. Сброс в finally —
    contextvar не «протекает» в следующий апдейт (один event loop под APScheduler)."""

    async def __call__(self, handler, event: TelegramObject, data):
        token = set_context(
            request_id=new_request_id(), chat_id=_event_chat_id(event), operation=_event_op(event)
        )
        try:
            return await handler(event, data)
        finally:
            reset_context(token)


# Кому уже отправили вежливый отказ (один раз на chat_id за жизнь процесса) — чтобы не спамить.
# Множество, а не cooldown-словарь: проще и без импорта time. Cap — анти-DoS от ротации chat_id.
_WL_REFUSED: set[int] = set()
_WL_REFUSED_CAP = 10_000
# Про кого уже был WARNING «не в whitelist» (дедуп лога: упорный чужой флуд не зашумляет журнал —
# один WARNING на chat_id, повторы уходят в debug). Cap тот же (анти-DoS ротацией chat_id).
_WL_LOGGED: set[int | None] = set()


async def _maybe_refuse_unlisted(event: object, uid: int | None) -> None:
    """Один вежливый отказ не-whitelisted пользователю (private Message) с его chat_id — чтобы
    легитимный новый человек знал, что делать (передать ID админу), а не думал, что бот сломан.
    Не ослабляет замок: хендлер всё равно не вызывается (return в middleware). Молча для callback/
    не-private/без chat_id и при повторе. Cap множества — защита от флуда чужими chat_id."""
    if uid is None or getattr(event, "chat", None) is None:  # только Message-like (есть .answer)
        return
    if _event_chat_type(event) != "private":
        return
    answer = getattr(event, "answer", None)
    if not callable(answer) or uid in _WL_REFUSED or len(_WL_REFUSED) >= _WL_REFUSED_CAP:
        return
    _WL_REFUSED.add(uid)
    # LangMiddleware ещё не отработал (он ПОСЛЕ whitelist) → берём язык Telegram-клиента (или RU).
    tg_lang = getattr(getattr(event, "from_user", None), "language_code", None)
    try:
        await answer(i18n.t("access_denied", (tg_lang or "")[:2], chat_id=uid))
    except Exception:  # noqa: BLE001 — уведомление необязательно, не роняем обработку апдейта
        pass


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        # Fail-closed (как ads.client.ensure_allowed): пустое объединение => блок ВСЕХ, а не fail-open.
        # uid=None (callback без message / вход без чата) тоже не пройдёт => блок. Круг — env ∪ БД:
        # env TELEGRAM_WHITELIST_CHAT_IDS (бутстрап) ∪ таблица whitelist (рантайм /adduser, кэш TTL).
        uid = _event_chat_id(event)
        if not await is_whitelisted(uid):
            # Дедуп: один WARNING на chat_id (упорный флуд чужого — debug, не шум в журнале).
            if uid not in _WL_LOGGED and len(_WL_LOGGED) < _WL_REFUSED_CAP:
                _WL_LOGGED.add(uid)
                log.warning("заблокирован chat_id %s (не в whitelist)", uid)
            else:
                log.debug("заблокирован chat_id %s (не в whitelist, повтор)", uid)
            await _maybe_refuse_unlisted(event, uid)
            return
        # ТОЛЬКО private-чаты: whitelist — по chat_id, а в группе chat_id ОДИН на всех участников →
        # любой из них нажал бы ✅ и двинул деньги (актор берётся из from_user). В private chat_id ==
        # user_id, поэтому whitelisted id = конкретный человек. Группу/канал блокируем (golden rule #10).
        ctype = _event_chat_type(event)
        if ctype is not None and ctype != "private":
            log.warning("заблокирован не-private чат %s (тип %s)", uid, ctype)
            return
        return await handler(event, data)


class LangMiddleware(BaseMiddleware):
    """Ставит язык интерфейса (§4) в contextvar bot.i18n на время обработки апдейта — форматтеры
    (texts.fmt_*, summary_text, клавиатуры) сами берут язык, без проброса lang через ~80 call-site.
    Резолв по chat_id (как whitelist); сброс в finally, чтобы язык не «протёк» в следующий апдейт
    (один event loop под APScheduler). Дефолт RU при отсутствии выбора (i18n.get_lang)."""

    async def __call__(self, handler, event: TelegramObject, data):
        token = i18n.set_current_lang(i18n.get_lang(_event_chat_id(event) or 0))
        try:
            return await handler(event, data)
        finally:
            i18n.reset_current_lang(token)


# N4: /команда во время активного визарда сама управляет состоянием (свои хендлеры зарегистрированы
# раньше визард-стейтов и делают state.clear()/abandon) → НЕ сворачиваем их через middleware, иначе
# было бы двойное действие (напр. §20-буфер сброшен в черновик, а затем /cancel его отменил).
_MW_SUSPEND_EXEMPT = frozenset({"/cancel", "/start"})


class SlashCommandExitsWizardMiddleware(BaseMiddleware):
    """N4: любая `/команда`, набранная во время активного визарда, МЯГКО сворачивает визард (работа
    не теряется: §19-черновик жив, §20-буфер → confirm-черновик, лёгкие state — clear), чтобы
    сработал НАСТОЯЩИЙ Command-хендлер. Раньше brief-state хендлеры (StateFilter матчит ЛЮБОЙ текст,
    вкл. `/`) съедали `/templates`/`/savetemplate`/`/recent`/`/newvideo` (они зарегистрированы ПОЗЖЕ
    визард-стейта) и отвечали ошибкой формата /newsearch. Один гард класса — mirror menu_guard для
    кнопок меню. Обнуляем data['raw_state'], чтобы StateFilter визарда больше не матчил в этом апдейте."""

    async def __call__(self, handler, event: TelegramObject, data):
        text = getattr(event, "text", None)
        cc_step: int | None = None
        cli_flushed = False
        if isinstance(text, str) and text.startswith("/") and data.get("raw_state") is not None:
            cmd = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            state = data.get("state")
            if cmd not in _MW_SUSPEND_EXEMPT and state is not None:
                try:
                    # lossless-сворачивание визарда
                    cc_step, cli_flushed = await _suspend_active_flow_soft(event, state)
                except Exception:  # noqa: BLE001 — сбой сворачивания не должен ронять саму команду
                    await state.clear()
                data["raw_state"] = (
                    None  # визард-StateFilter больше не совпадёт → команда сработает
                )
        result = await handler(event, data)
        # B1 (живой тест 2026-07-07): раньше сворачивание было МОЛЧАЛИВЫМ — пользователь не знал,
        # что черновик §19 сохранён и как вернуться. Подсказка ПОСЛЕ ответа команды (mirror
        # menu_guard.btn_guard_menu). Сбой подсказки не роняет уже выполненную команду.
        try:
            if cc_step is not None:
                await event.answer(i18n.t("cc_wizard_suspended", step=max(1, min(int(cc_step), 7))))
            if cli_flushed:
                await event.answer(i18n.t("cli_buf_flushed"))
        except Exception:  # noqa: BLE001
            pass
        return result


def _valid_idx(seq: list | None, idx: int) -> TypeGuard[list]:
    """idx из callback_data указывает на реальный элемент кэша. Проверяем И нижнюю границу: дефолт
    -1 в callback-схемах (AudienceCB и др.) проходил бы `idx >= len` (−1 ≥ len ложно) и через
    отрицательную индексацию Python выбрал бы ПОСЛЕДНИЙ элемент — не ту кампанию/аудиторию.
    TypeGuard → mypy сужает seq к list после гарда (сохраняет сужение None, как было у inline-проверки)."""
    return seq is not None and 0 <= idx < len(seq)


def _cq_msg(cq: CallbackQuery) -> Message | None:
    """Доступное Message из callback или None. aiogram отдаёт Message|InaccessibleMessage|None:
    исходное сообщение может быть старше 48ч / удалено (InaccessibleMessage) или отсутствовать.
    Правки/ответы по кнопке — только на реальном Message; иначе .edit_text/.answer бросили бы
    AttributeError (его глотает глобальный errors-хендлер → «мёртвая» кнопка без ответа юзеру)."""
    m = cq.message
    # Исключаем недоступное/None (а НЕ требуем isinstance(Message)): дакт-фейки в тестах — не
    # настоящие aiogram Message, но и не InaccessibleMessage, поэтому корректно проходят дальше.
    if m is None or isinstance(m, InaccessibleMessage):
        return None
    return m


async def _safe_edit(cq: CallbackQuery, text: str, **kwargs) -> None:
    """Безопасно отредактировать сообщение под кнопкой: None/InaccessibleMessage (устарело/удалено)
    → тихо пропускаем; TelegramBadRequest («не изменено»/«не найдено») → тоже. Убирает повторяющийся
    try/except и снимает union-attr на cq.message (Message|InaccessibleMessage|None)."""
    m = _cq_msg(cq)
    if m is None:
        return
    try:
        await m.edit_text(text, **kwargs)
    except TelegramBadRequest:
        pass


async def _safe_edit_markup(cq: CallbackQuery, markup) -> None:
    """Обновить ТОЛЬКО inline-разметку под сообщением (постраничные пикеры B7): текст не трогаем.
    None/устаревшее сообщение и «не изменено»/«не найдено» — тихо пропускаем."""
    m = _cq_msg(cq)
    if m is None:
        return
    try:
        await m.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass


# ── Общие действия (чтобы команда и кнопка делали одно и то же) ─────────────────
async def _capture_cmd_error(e: Exception, where: str) -> None:
    """A2 (§15): залогировать+СОХРАНИТЬ ошибку интерактивного хендлера в error_events (→ /diag +
    проактивный алерт админам A1). Раньше сюда писали только глобальный on_error и scheduler —
    точечные `except` команд показывали юзеру err_text, но в /diag не попадали (картина инцидентов
    была неполной). Ожидаемые account-access ошибки (нет прав/деактивирован аккаунт) НЕ пишем —
    это конфиг оператора, а не дефект (как в scheduler.jobs, чтоб /diag не заваливало). Best-effort:
    capture_exception сам не бросает (наблюдаемость не роняет рантайм)."""
    if is_account_access_error(e):
        return
    await capture_exception(e, where=where)


async def _friendly_error(e: Exception, where: str, *, short: bool = False) -> str:
    """Текст ошибки для НЕтехнического менеджера БЕЗ имени класса исключения (P1-аудит
    2026-07-06: раньше cb_error показывал «Ошибка: ValidationError»). Валидация (Pydantic →
    локализованные правила ux.humanize_validation; ValueError валидаторов — их сообщение уже
    человекочитаемо) → шаблон err_validate. Прочее — фиксируем в error_events (/diag) и отдаём
    err_unexpected с КОДОМ ИНЦИДЕНТА (request_id): по коду инцидент находят в /diag; имя класса
    остаётся только в логах. short=True — для cq.answer(alert): одна строка ≤180 симв."""
    errs = getattr(e, "errors", None)
    if callable(errs) or isinstance(e, ValueError):
        body = ux.humanize_validation(e) if callable(errs) else redact_text(str(e))
        text = i18n.t("err_validate", err=body)
    else:
        code = await capture_exception(e, where=where)
        text = i18n.t("err_unexpected", code=code)
    if short:
        text = " ".join(text.split())  # alert: однострочно, без переносов
        if len(text) > 180:
            text = text[:179] + "…"
    return text


async def _load_error_events(today: bool = False, limit: int = 15) -> list:
    """A3 (§15): последние error_events для /diag (reverse-chron). today ⇒ только за сегодня (UTC).
    Фильтр «сегодня» — в Python (наивный created_at → UTC): избегаем строкового сравнения tz-aware/
    naive в SQL на SQLite. Read-only; message/traceback уже редактированы на записи (секретов нет).
    Экспорт (1.2) зовёт с большим limit → фетчим с запасом (для today-фильтра берём шире)."""
    from sqlalchemy import desc, select

    from db.models import ErrorEvent as _EE
    from db.session import Session

    fetch = max(50, int(limit) * 2 if today else int(limit))
    async with Session() as s:
        rows = (await s.execute(select(_EE).order_by(desc(_EE.id)).limit(fetch))).scalars().all()
    if today:
        from datetime import datetime, timezone

        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        def _ok(dt) -> bool:
            if dt is None:
                return False
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= start

        rows = [r for r in rows if _ok(r.created_at)]
    return list(rows[:limit])


async def _load_error_detail(rid: str):
    """A3 (§15): один error_event по request_id (свежайший) — для detail-кнопки /diag (полный
    редактированный traceback). Read-only. None — если инцидент устарел/не найден."""
    from sqlalchemy import desc, select

    from db.models import ErrorEvent as _EE
    from db.session import Session

    async with Session() as s:
        return (
            await s.execute(
                select(_EE).where(_EE.request_id == rid).order_by(desc(_EE.id)).limit(1)
            )
        ).scalar_one_or_none()


async def _llm_budget_or_reply(message) -> bool:
    """C3: пер-юзер дневной потолок LLM ПЕРЕД дорогим агент-вызовом. Над лимитом → отвечаем понятно и
    возвращаем True (вызывающий прекращает обработку, LLM не зовём — fail-closed, трат нет). В пределах —
    фиксируем вызов и возвращаем False. Гард выключен (LLM_DAILY_CALLS_PER_USER=0) → всегда False.
    Ставится в NL-точках входа (on_text / _run_task_with_context), а НЕ в call_llm — иначе отказ летел
    бы в глобальный on_error и засорял /diag «ошибкой» (блок бюджета — не дефект)."""
    from core import llm_budget

    try:
        llm_budget.consume(message.chat.id)
        return False
    except llm_budget.LLMBudgetExceededError as e:
        await message.answer(i18n.t("llm_budget_exceeded", used=e.used, limit=e.limit))
        return True


async def _notify_admins_started(bot) -> None:
    """B1 (§15): однократный readiness-пинг админам (ADMIN_CHAT_IDS) на УСПЕШНОМ старте — миграция
    HEAD, число аккаунтов на чтение, активная parse-модель. Живой сигнал деплоя: не пришёл после
    redeploy ⇒ бот не поднялся (крэш-луп) — видно сразу, не дожидаясь первого сообщения. Best-effort:
    нет админов → тихо пропускаем (не спамим операторов); сбой доставки не роняет старт."""
    from core.access import admin_ids_all

    admins = await admin_ids_all()  # P4: env ∪ рантайм-админы
    if not admins:
        return
    head = "—"
    try:  # alembic HEAD из БД (best-effort; на SQLite/dev таблицы alembic_version может не быть)
        from sqlalchemy import text as _sql_text

        from db.session import Session

        async with Session() as s:
            head = (
                await s.execute(_sql_text("SELECT version_num FROM alembic_version LIMIT 1"))
            ).scalar() or "—"
    except Exception:  # noqa: BLE001 — версия миграции необязательна для пинга
        head = "—"
    try:
        from ads.client import discovered_read_children

        n_children = len(discovered_read_children())
    except Exception:  # noqa: BLE001
        n_children = 0
    model = router.effective_model("parsing")
    for chat_id in admins:
        en = i18n.get_lang(chat_id) == "en"
        body = (
            (
                "✅ <b>Aimash started</b>\n"
                f"• migration: <code>{texts.esc(str(head))}</code>\n"
                f"• readable accounts: {n_children}\n"
                f"• model (parsing): <code>{texts.esc(model)}</code>"
            )
            if en
            else (
                "✅ <b>Aimash запущен</b>\n"
                f"• миграция: <code>{texts.esc(str(head))}</code>\n"
                f"• аккаунтов на чтение: {n_children}\n"
                f"• модель (parsing): <code>{texts.esc(model)}</code>"
            )
        )
        try:
            await bot.send_message(chat_id, body, parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001 — недоступный админ не роняет старт
            log.warning("startup-пинг не доставлен админу %s: %s", chat_id, type(e).__name__)


async def _send_help(message: Message) -> None:
    await message.answer(i18n.t("help"), parse_mode=ParseMode.HTML)


def _inactive_read_hint(acct: str) -> str:
    """2.3: если аккаунт — НЕАКТИВНЫЙ дочерний наших MCC (CANCELED/SUSPENDED), вернуть честную
    причину отказа чтения (Google отклоняет API-чтение таких аккаунтов) вместо generic-ошибки.
    Не из meta неактивных → '' (обычный err-путь)."""
    try:
        from ads.client import discovered_inactive_children_meta

        ch = discovered_inactive_children_meta().get(normalize_customer_id(str(acct)))
    except Exception:  # noqa: BLE001 — подсказка-косметика
        return ""
    if ch is None:
        return ""
    return i18n.t(
        "account_inactive_read_failed",
        name=texts.esc(getattr(ch, "name", "") or str(acct)),
        status=texts.esc(getattr(ch, "status", "") or "?"),
    )


def _account_name(cid: str) -> str:
    """2.1: имя аккаунта из meta обхода MCC («Башня») для заголовков «Имя · id» — как в пикере.
    Нет meta/имя совпадает с id → '' (вызывающий печатает голый id, как раньше). Косметика:
    сбой не критичен."""
    try:
        from ads.client import discovered_read_children_meta

        ch = discovered_read_children_meta().get(normalize_customer_id(str(cid)))
        if ch is not None and (ch.name or "") and str(ch.name) != str(ch.id):
            return str(ch.name)
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _render_status(message: Message, acct: str) -> None:
    """Показать статистику КОНКРЕТНОГО аккаунта за 30 дн. (read-only). acct уже прошёл замок чтения
    (пикер/резолв). Draft без живых данных → подсказка выбрать живой аккаунт (§8 F)."""
    try:
        from ads.client import build_client_async
        from ads.read import account_stats

        client = await build_client_async(acct)  # холодная сборка (после /refresh) — вне loop
        async with ux.typing_action(message):  # «печатает…» пока идёт чтение SDK
            st = await run_ads_read_call(account_stats, client, acct, 30, label="account_stats")
            cur = await _read_currency(client, acct)  # §9: валюта в денежных строках
    except Exception as e:  # сеть/доступ/SDK
        await _capture_cmd_error(e, "cmd:status")  # A2: в /diag + алерт админам
        hint = (
            _inactive_read_hint(acct) if is_account_access_error(e) else ""
        )  # 2.3: честная причина
        if hint:
            await message.answer(hint, parse_mode=ParseMode.HTML)
        else:
            await message.answer(i18n.t("err_stats", err=ux.err_text(e)))
        await _heal_if_stuck_global(message, acct)  # само-восстановление залипшего аккаунта
        return
    await message.answer(
        texts.fmt_stats(
            acct,
            30,
            {
                "impressions": st.impressions,
                "clicks": st.clicks,
                "cost": round(st.cost, 2),
                "conversions": st.conversions,
                "conv_value": round(st.conv_value, 2),
            },
            cur,
            name=_account_name(acct),  # 2.1: «Башня · …2039» как в пикере
        ),
        parse_mode=ParseMode.HTML,
    )
    hint = _live_account_hint(acct)  # §8 F: работаем на Draft, а есть живые → зовём выбрать
    if hint:
        await message.answer(hint, parse_mode=ParseMode.HTML)


async def _send_status(message: Message) -> None:
    """§6/§8: пикер аккаунта → статистика (30 дн.). Один доступный аккаунт → сразу его статистика
    (без клика). Переиспользует пикер /report (report_accounts_kb, target='status')."""
    chat_id = message.chat.id
    rows = await _read_account_rows(chat_id)
    _REPORT_ACCT_CACHE[chat_id] = rows
    if len(rows) <= 1:  # только Draft/один аккаунт — сразу статистика (пикер не нужен)
        await _render_status(message, await _active_read_account(chat_id))
        return
    await message.answer(
        i18n.t("status_pick_account"),
        reply_markup=report_accounts_kb(
            rows,
            "status",
            last=await _last_account(chat_id),
            frequent=await _frequent_accounts(chat_id),
        ),
        parse_mode=ParseMode.HTML,
    )


async def _start_advise_picker(message: Message, *, topic: str | None = None) -> None:
    """§6/§8: пикер аккаунта → рекомендации advisor. Один доступный аккаунт → сразу прогон (без
    клика). Тема живёт в _ADVISE_TOPIC_CACHE, переживает выбор аккаунта в пикере (topic-specific
    /advise через пикер). Переиспользует пикер /report (report_accounts_kb, target='advise')."""
    chat_id = message.chat.id
    rows = await _read_account_rows(chat_id)
    _REPORT_ACCT_CACHE[chat_id] = rows
    _ADVISE_TOPIC_CACHE[chat_id] = topic
    if len(rows) <= 1:  # только Draft/один аккаунт — сразу рекомендации (пикер не нужен)
        await _advise_run(message, chat_id, topic=topic)
        return
    await message.answer(
        i18n.t("advise_pick_account"),
        reply_markup=report_accounts_kb(
            rows,
            "advise",
            last=await _last_account(chat_id),
            frequent=await _frequent_accounts(chat_id),
        ),
        parse_mode=ParseMode.HTML,
    )


_AUDIT_PERIOD_CACHE: dict[int, int] = {}  # период /audit переживает выбор аккаунта в пикере
_AUDIT_MAX_FINDINGS = 8  # сколько находок показываем отдельными сообщениями (анти-спам)


def _target_key(cid: str) -> str:
    """Ключ ui_prefs для целевого CPA per-account (namespaced, чтобы не мешать anomaly-порогам)."""
    return f"target_cpa::{cid}"


async def _load_target_cpa(chat_id: int, cid: str) -> float | None:
    """Целевой CPA аккаунта (для 3×-Kill в /audit), заданный /target. Нет/некорректно → None
    (правило молчит — не фабрикуем «×цель»). Значение — в валюте аккаунта (FX не выдумываем)."""
    raw = await _load_ui_pref(chat_id, _target_key(cid))
    try:
        v = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


async def _target_cmd(m: Message) -> None:
    """/target [CPA] — целевой CPA АКТИВНОГО аккаунта чтения (разблокирует правило 3×-Kill в /audit:
    кампания с CPA ≥ 3× цели → кандидат на паузу). Без аргумента — показать; /target reset — сбросить.
    Значение в валюте аккаунта (FX не выдумываем). НАСТРОЙКА БОТА — Google Ads не трогает."""
    import re

    chat_id = m.chat.id
    lang = i18n.get_lang(chat_id)
    en = lang == "en"
    acct = await _active_read_account(chat_id)
    key = _target_key(acct)
    arg = ""
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip().lower()

    if arg in ("reset", "off", "clear", "сброс", "0"):
        await _save_ui_pref(chat_id, key, "")
        await m.answer("🎯 Target CPA cleared." if en else "🎯 Цель CPA сброшена.")
        return
    if arg:
        mm = re.search(r"\d+(?:[.,]\d+)?", arg.replace("cpa", ""))
        val = float(mm.group().replace(",", ".")) if mm else 0.0
        if val <= 0:
            await m.answer(
                "Format: /target 68 (or /target reset)."
                if en
                else "Формат: /target 68 (или /target reset)."
            )
            return
        await _save_ui_pref(chat_id, key, f"{val:.2f}")
        await m.answer(
            f"🎯 Target CPA for {acct}: {val:.2f}. /audit will now flag campaigns with CPA ≥ 3× target for pausing."
            if en
            else f"🎯 Цель CPA для {acct}: {val:.2f}. Теперь /audit предложит паузу для кампаний с CPA ≥ 3× цели."
        )
        return

    cur = await _load_ui_pref(chat_id, key)
    if cur:
        await m.answer(
            f"🎯 Target CPA for {acct}: {cur}. Clear — /target reset."
            if en
            else f"🎯 Цель CPA для {acct}: {cur}. Сбросить — /target reset."
        )
    else:
        await m.answer(
            "🎯 No target CPA set. Set it — /target 68 — then /audit flags campaigns with CPA ≥ 3× target."
            if en
            else "🎯 Цель CPA не задана. Задай — /target 68 — тогда /audit предложит паузу для кампаний с CPA ≥ 3× цели."
        )


async def _start_audit_picker(message: Message, *, period_days: int | None = None) -> None:
    """Пикер аккаунта → аудит (read-only). Один доступный аккаунт → сразу прогон (без клика). Период
    живёт в _AUDIT_PERIOD_CACHE (переживает выбор). Переиспользует пикер /report (target='audit')."""
    chat_id = message.chat.id
    rows = await _read_account_rows(chat_id)
    _REPORT_ACCT_CACHE[chat_id] = rows
    if period_days:
        _AUDIT_PERIOD_CACHE[chat_id] = period_days
    else:
        _AUDIT_PERIOD_CACHE.pop(chat_id, None)
    if len(rows) <= 1:  # только Draft/один аккаунт — сразу аудит (пикер не нужен)
        await _audit_run(message, chat_id, period_days=period_days)
        return
    await message.answer(
        "🩺 " + i18n.t("advise_pick_account"),
        reply_markup=report_accounts_kb(
            rows,
            "audit",
            last=await _last_account(chat_id),
            frequent=await _frequent_accounts(chat_id),
        ),
        parse_mode=ParseMode.HTML,
    )


async def _start_setacct_picker(message: Message) -> None:
    """E/§8 F: пикер выбора АКТИВНОГО аккаунта ЧТЕНИЯ (персист per-chat, переживает рестарт) —
    ключ к «данные со всего аккаунта, а не только Draft». Переиспользует пикер /report
    (report_accounts_kb, target='setacct'); один доступный аккаунт → просто показываем текущий."""
    chat_id = message.chat.id
    rows = await _read_account_rows(chat_id)
    _REPORT_ACCT_CACHE[chat_id] = rows
    if len(rows) <= 1:  # выбирать не из чего — сообщаем текущий (как /account без аргумента)
        cur = await _active_read_account(chat_id)
        draft_mark = " (Draft)" if cur == DRAFT_ACCOUNT_ID else ""
        await message.answer(
            i18n.t("account_current", cid=cur, draft=draft_mark), parse_mode=ParseMode.HTML
        )
        return
    await message.answer(
        i18n.t("setacct_pick_account"),
        reply_markup=report_accounts_kb(
            rows,
            "setacct",
            last=await _last_account(chat_id),
            frequent=await _frequent_accounts(chat_id),
        ),
        parse_mode=ParseMode.HTML,
    )


async def _send_balance(message: Message) -> None:
    """Бюджет ИИ (read-only): баланс/траты OpenRouter (источник истины) + разбивка процесса по
    ролям «с запуска». Баланс берём из OpenRouter (переживает рестарты), а живую разбивку — из
    core.usage (накопитель текущего процесса). Секретов нет — только числа."""
    from agent.openrouter_account import fetch_account
    from core.usage import snapshot

    try:
        async with ux.typing_action(message):  # «печатает…» пока идёт запрос к OpenRouter
            acct = await fetch_account()
    except Exception as e:  # нет ключа / сеть
        await _capture_cmd_error(e, "cmd:balance")  # A2: в /diag + алерт админам
        await message.answer(i18n.t("err_balance", err=ux.err_text(e)))
        return
    text = texts.fmt_balance(acct, snapshot())
    from core import llm_budget

    cap = llm_budget.snapshot(message.chat.id)  # 2.12: видимость пер-юзер лимита (C3)
    if cap.get("limit", 0) > 0:
        text += "\n" + i18n.t("balance_llm_cap", used=cap["used"], limit=cap["limit"])
    await message.answer(text, parse_mode=ParseMode.HTML)


async def _send_journal(message: Message) -> None:
    """Журнал последних изменений (ТЗ §12/§18): что/когда/кто/результат из audit_log. Read-only,
    без секретов (result редактируется на записи). «Видно, что и когда изменилось» (обещание /start)."""
    from confirm.store import list_recent_audit

    try:
        events = await list_recent_audit(15)
    except Exception as e:  # БД недоступна
        await _capture_cmd_error(e, "cmd:journal")  # A2: в /diag + алерт админам
        await message.answer(i18n.t("err_journal", err=ux.err_text(e)))
        return
    await message.answer(texts.fmt_journal(events), parse_mode=ParseMode.HTML)


def _camp_account(chat_id: int) -> str:
    """Аккаунт-якорь меню /campaigns (с которого прочитан _CAMP_CACHE). Нет записи → Draft.
    Все чтения/мутации меню идут на него — чтение и запись согласованы (мультиаккаунт-готовность).
    Замок ensure_allowed в _present_proposal отсекает не-Draft вне allow-list (fail-closed)."""
    return _CAMP_ACCT.get(chat_id, DRAFT_ACCOUNT_ID)


async def _send_campaigns(message: Message, chat_id: int) -> None:
    """Список кампаний АКТИВНОГО аккаунта чтения + inline-кнопки. Активный резолвится через
    read-замок × грант (дефолт Draft); при НЕвыбранном аккаунте и нескольких живых — пикер
    вместо тихого пустого Draft (§8, _require_read_account). Мутации по кнопкам всё равно
    упрутся в ensure_allowed."""
    acct = await _require_read_account(message, "campaigns", chat_id=chat_id)
    if acct is None:
        return
    await _send_campaigns_for(message, chat_id, acct)


async def _send_campaigns_for(message: Message, chat_id: int, acct: str) -> None:
    """Тело /campaigns для ЯВНОГО аккаунта (из активного или из пикера target='campaigns').
    Кэшируем список и аккаунт-якорь по chat_id (мультиаккаунт: меню читает и мутирует ОДИН
    аккаунт)."""
    try:
        from ads.client import build_client_async
        from ads.read import list_campaigns

        client = await build_client_async(acct)
        async with ux.typing_action(message):
            camps = await run_ads_read_call(list_campaigns, client, acct, label="list_campaigns")
    except Exception as e:  # сеть/доступ/SDK
        await _capture_cmd_error(e, "cmd:campaigns")  # A2: в /diag + алерт админам
        await message.answer(i18n.t("err_campaigns", err=ux.err_text(e)))
        return
    if not camps:
        await message.answer(i18n.t("no_campaigns"))
        return
    _CAMP_CACHE[chat_id] = camps
    _CAMP_ACCT[chat_id] = acct
    await message.answer(
        texts.campaigns_title(acct, name=_account_name(acct)),  # 2.1: «Башня · …2039»
        reply_markup=campaigns_kb(camps),
        parse_mode=ParseMode.HTML,
    )


# ── D1: поиск кампании по названию в пикерах (/campaigns, отчёт, /rsa) ────────────
# Выбор совпадения идёт по ГЛОБАЛЬНОМУ индексу в кэше — те же callback-и (CampCB/ReportCampCB/
# RsaPickCB), что и без поиска; фильтр лишь подмешивает подмножество индексов. Read-only.
def _picker_camps(kind: str, chat_id: int) -> list[dict] | None:
    """Кэш кампаний нужного пикера по chat_id (None → кэш потерян, рестарт/устарело)."""
    cache = {
        "campaigns": _CAMP_CACHE,
        "report": _REPORT_CAMP_CACHE,
        "rsa": _RSA_CAMP_CACHE,
    }.get(kind)
    return cache.get(chat_id) if cache is not None else None


def _picker_full_kb(kind: str, target: str, camps: list[dict]):
    """Полный (постраничный) пикер нужного вида — для «↩︎ Показать все» и пустого результата."""
    if kind == "report":
        return report_campaigns_kb(camps, target)
    if kind == "rsa":
        return rsa_pick_campaigns_kb(camps)
    return campaigns_kb(camps)


def _picker_match_indices(camps: list[dict], query: str) -> list[int]:
    """Глобальные индексы кампаний, чьё имя (или id) содержит запрос (casefold, регистронезав.)."""
    q = query.strip().casefold()
    if not q:
        return list(range(len(camps)))
    return [
        i
        for i, c in enumerate(camps)
        if q in (c.get("name") or "").casefold() or q in str(c.get("id") or "")
    ]


def _picker_rest_state(kind: str):
    """Состояние-«покой» пикера после поиска: /rsa живёт в RsaWizard.picking (гард on_text),
    /campaigns и отчёт — без состояния (их вход stateless). Возврат в него = одноразовость поиска."""
    return RsaWizard.picking if kind == "rsa" else None


# ── D3: пикер кампаний для /addkeys (best-effort; текст-ввод названия остаётся) ──────
_KW_ADD_CAMP_CACHE: dict[int, list[dict]] = {}


async def _kw_add_load_campaigns(chat_id: int) -> list[dict]:
    """Список кампаний активного read-аккаунта для пикера /addkeys. Best-effort: сбой/пусто/
    неоднозначный аккаунт ⇒ [] (флоу спокойно падает на ввод названия текстом, kw_add_campaign).
    НЕ форсит выбор аккаунта (в отличие от отчётов): пикер — удобство, а не обязательный шаг."""
    try:
        acct = await _active_read_account(chat_id)
        from ads.client import build_client_async
        from ads.read import list_campaigns

        client = await build_client_async(acct)
        camps = await run_ads_read_call(list_campaigns, client, acct, label="kw_add_campaigns")
        return camps or []
    except Exception:  # noqa: BLE001 — пикер опционален; текст-фолбэк всегда работает
        return []


# Денежные операции (UI-слой): для них при внешнем контенте (файл/ссылка) в сводку добавляется
# предупреждение (см. _present_proposal). Зеркалит реестр _EXPECTED_MONEY_OPS в
# tests/test_invariants_core.py (имена op без префикса apply_) — дрейф ловит тест.
_MONEY_OPS_UI: frozenset[str] = frozenset(
    {
        "update_budget",
        "update_bid",
        "set_bidding_strategy",
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    }
)


def _build_proposal(operation: str, **args) -> tuple[str, str, dict, str]:
    """Детерминированно собрать черновик мутации (как agent.loop, но БЕЗ LLM) — для кнопок.
    Валидация схемой обязательна. Возвращает (confirmation_id, operation, params, summary)."""
    validated = SCHEMAS[operation](**args)
    params = validated.model_dump()
    summary = build_summary(operation, before="[текущее значение из Google Ads]", after=params)
    p = Proposal(operation=operation, summary=summary, params=params, chat_id=0)
    return p.confirmation_id, operation, params, summary


# ── D2: «↩️ Откатить» — обратная операция из снимка _before (за confirm-гейтом) ──────
# Только детерминированно обратимые операции (снимок _before достаточен для одной обратной
# мутации). update_bid откатывается лишь при ОДИНАКОВЫХ ставках групп (иначе один set_to не
# восстановит разнородные ставки — честно НЕ предлагаем). Геостратегия/bidding_strategy —
# не откатываются одной операцией (в backlog). Обратный черновик минтится как ОБЫЧНЫЙ proposal
# (confirm-гейт + user_initiated=True в _present_proposal → бюджет-откат легитимен, правило 3).
_ROLLBACKABLE_OPS = frozenset(
    {
        "update_budget",
        "update_bid",
        "pause_campaign",
        "resume_campaign",
        "launch_campaign",
        "update_campaign",
        "set_campaign_network",
        "pause_ad_group",
        "resume_ad_group",
        "pause_ad",
        "resume_ad",
    }
)
# chat_id → {token, operation, params, customer_id}: последняя откатываемая операция (одноразово).
_ROLLBACK_CACHE: dict[int, dict] = {}


def _reverse_spec(operation: str, params: dict, before) -> tuple[str, dict] | None:
    """Собрать (обратная_операция, params) из снимка _before. None — необратимо/нет снимка/
    неоднозначно. Восстанавливаем прежнее значение/статус ПРОТИВОПОЛОЖНОЙ операцией."""
    if not isinstance(before, dict) or not isinstance(params, dict):
        return None
    camp = params.get("campaign")
    if not camp:
        return None
    kind = before.get("kind")
    if operation == "update_budget" and kind == "budget":
        micros = before.get("before_micros")
        if micros is None:
            return None
        return ("update_budget", {"campaign": camp, "mode": "set_to", "value": int(micros) / 1e6})
    if operation == "update_bid" and kind == "bid":
        uniq = {int(x) for x in (before.get("before_micros") or [])}
        if len(uniq) != 1:  # разные ставки по группам — одним set_to не вернуть (честно skip)
            return None
        return ("update_bid", {"campaign": camp, "mode": "set_to", "value": next(iter(uniq)) / 1e6})
    if operation == "set_campaign_network" and kind == "network":
        return (
            "set_campaign_network",
            {"campaign": camp, "search_partners": bool(before.get("before_search_partners"))},
        )
    if operation == "update_campaign" and kind == "name":
        old, new = before.get("before_name"), params.get("new_name")
        if not old or not new:
            return None
        return (
            "update_campaign",
            {"campaign": new, "new_name": old},
        )  # текущее имя new → назад в old
    if kind == "status":  # восстановить before_status противоположной операцией
        bs = (before.get("before_status") or "").upper()
        if operation in ("pause_campaign", "resume_campaign", "launch_campaign"):
            if bs == "PAUSED":
                return ("pause_campaign", {"campaign": camp})
            if bs == "ENABLED":
                return ("resume_campaign", {"campaign": camp})
        elif operation in ("pause_ad_group", "resume_ad_group"):
            ag = params.get("ad_group")
            if ag and bs == "PAUSED":
                return ("pause_ad_group", {"campaign": camp, "ad_group": ag})
            if ag and bs == "ENABLED":
                return ("resume_ad_group", {"campaign": camp, "ad_group": ag})
        elif operation in ("pause_ad", "resume_ad"):
            ag, ad = params.get("ad_group"), params.get("ad")
            if ag and ad and bs == "PAUSED":
                return ("pause_ad", {"campaign": camp, "ad_group": ag, "ad": ad})
            if ag and ad and bs == "ENABLED":
                return ("resume_ad", {"campaign": camp, "ad_group": ag, "ad": ad})
    return None


# ── C1/C3 (гибрид): пер-чат контекст диалога для разрешения ссылок-местоимений ──────
def _campaign_name_from_params(operation: str, params: dict) -> str:
    """Извлечь имя кампании из params черновика (разные операции — разные ключи). '' если нет."""
    if not isinstance(params, dict):
        return ""
    for key in ("campaign", "campaign_name", "name", "source_campaign", "new_name"):
        v = params.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _chat_ctx_note(
    chat_id: int,
    *,
    campaign: str | None = None,
    customer_id: str | None = None,
    user_text: str | None = None,
) -> None:
    """C1: обновить пер-чат контекст диалога (последняя кампания/аккаунт + история реплик)."""
    ctx = _CHAT_CTX.setdefault(chat_id, {"campaign": "", "customer_id": "", "history": []})
    if campaign and campaign.strip():
        ctx["campaign"] = campaign.strip()
    if customer_id and str(customer_id).strip():
        ctx["customer_id"] = str(customer_id).strip()
    if user_text and user_text.strip():
        hist = ctx.setdefault("history", [])
        hist.append(user_text.strip())
        del hist[:-_CHAT_CTX_HISTORY]  # держим только последние N реплик


def _build_agent_context(chat_id: int) -> dict:
    """C1/C3: собрать context для handle_command — последняя кампания/аккаунт + история реплик.
    Fallback last_campaign: пер-чат ctx → params последнего create_search_campaign."""
    ctx = _CHAT_CTX.get(chat_id) or {}
    last_campaign = ctx.get("campaign") or ""
    if not last_campaign:
        sp = _LAST_SEARCH_PARAMS.get(chat_id) or {}
        last_campaign = sp.get("campaign_name") or sp.get("name") or ""
    return {
        "last_campaign": last_campaign,
        "last_account": ctx.get("customer_id") or _LAST_ACCOUNT.get(chat_id) or "",
        "history": list(ctx.get("history") or []),
    }


def _account_label(cid: str) -> str:
    """Человекочитаемая метка аккаунта для карточки/сообщений: «Имя · id» или id. Draft — явно."""
    cid = normalize_customer_id(cid)
    if cid == DRAFT_ACCOUNT_ID:
        return f"Aimash (Draft) · {cid}"
    from ads.client import discovered_read_children_meta

    meta = discovered_read_children_meta().get(cid)
    name = (getattr(meta, "name", "") or "").strip() if meta else ""
    return f"{name} · {cid}" if name else cid


async def _present_proposal(
    message: Message,
    *,
    chat_id: int,
    operation: str,
    params: dict,
    summary: str,
    cid: str,
    external_context: bool = False,
    customer_id: str | None = None,
) -> None:
    """Сохранить черновик и показать с кнопками ✅/❌. user_initiated=True ставит ДОВЕРЕННЫЙ слой
    (входящее действие whitelisted-человека), НЕ агент про себя (golden rule #3, fail-closed).

    customer_id — аккаунт МУТАЦИИ, штампуемый в черновик (authoritative: execute_confirmed исполняет
    именно его, с повторным ensure_allowed). G2/G3: не задан → DRAFT (базовый). АКТИВНЫЙ аккаунт
    передаётся ЯВНО путями, где чтение и запись идут на ОДИН аккаунт (coherent «мутируем то, что
    видим»): agent-loop NL, меню /campaigns (аккаунт-якорь _CAMP_ACCT), RSA/шаблоны/media, §19 визард.
    §8: get_active_account по умолчанию отдаёт ЕДИНСТВЕННЫЙ живой read-аккаунт (не пустой Draft) →
    эти флоу целятся в него, НО ensure_allowed (fail-closed) пропускает мутацию ТОЛЬКО на аккаунт из
    allowed_customer_ids: не включённый живой аккаунт даёт внятный отказ «только чтение», БЕЗ тихой
    подмены; карточка несёт баннер аккаунта (менеджер ВИДИТ, на чьи деньги идёт правка, до ✅). Так
    «читать реальный аккаунт целиком» НЕ ослабляет замок — только делает отказ на не-Draft строже.

    external_context=True — предложение родилось при наличии СПРАВОЧНОГО контента из файла/ссылки
    (prompt-injection поверхность): для ДЕНЕЖНЫХ операций префиксуем сводку предупреждением
    «сумма могла быть предложена внешним контентом» (попадает и в audit-summary). Механику
    user_initiated НЕ меняем — последний гейт всё равно человек с diff и ✅."""
    # G2/G3: дефолт — Draft (базовый). Не-Draft приходит ЯВНО (agent-loop/меню/визард на активном
    # аккаунте, §8). Не-Draft обязан быть включён на мутации (allowed_customer_ids), иначе отказ ДО карточки.
    if customer_id is None:
        customer_id = DRAFT_ACCOUNT_ID
    if customer_id != DRAFT_ACCOUNT_ID:
        try:
            ensure_allowed(customer_id)
        except PermissionError:
            await message.answer(
                i18n.t("mutation_account_read_only", acct=_account_label(customer_id))
            )
            return
    # §5: читаем ТЕКУЩЕЕ значение (бюджет/ставку/статус) ДО показа → реальный diff «было → станет»
    # и снимок-база для оптимистичной сверки при исполнении (TOCTOU). read_before fail-safe (None).
    async with ux.typing_action(message):
        before = await read_before(operation, params, customer_id=customer_id)
        # P0 (golden rule #4): денежная команда в валюте ≠ валюте аккаунта → отказ с уточнением ДО
        # показа кнопок (FX не делаем; иначе «было→станет» соврал бы про сумму). Валюта — best-effort:
        # неизвестна (нет клиента/сбой read) ⇒ не блокируем (и чужую валюту на показе не печатаем).
        if operation in ("update_budget", "update_bid"):
            # P0-1: голая цифра без валютного слова. Модель порой всё равно ставит currency (обычно
            # 'USD') — детерминированно снимаем её, если пользователь валюту НЕ писал, чтобы сумма
            # трактовалась в валюте аккаунта, а не рождала ложный mismatch (петля «переформулируй в
            # AUD»). Источник текста — сообщение пользователя (NL-путь). Символ/код валюты в тексте →
            # уважаем выбор модели (честный mismatch, если валюта ≠ аккаунтной; FX не делаем).
            claimed_cur = params.get("currency")
            if claimed_cur and claimed_cur != "percent":
                user_text = (
                    getattr(message, "text", None) or getattr(message, "caption", None) or ""
                )
                if not detect_currency_token(user_text):
                    params = {**params, "currency": None}
            acct_cur = ""
            try:
                from ads.client import build_client_async

                # Валюта именно аккаунта МУТАЦИИ (customer_id), а не всегда Draft: при включённом
                # не-Draft аккаунте (управляемый список) команда «бюджет 100 UAH» на UAH-child не
                # должна ложно отклоняться по валюте USD-Draft, а «было→станет» — врать про сумму.
                acct_cur = await _read_currency(await build_client_async(customer_id), customer_id)
            except Exception:  # noqa: BLE001 — валюту не определить → без FX-сверки, не роняем показ
                acct_cur = ""
            mismatch = currency_mismatch(operation, params, acct_cur)
            if mismatch:
                await message.answer("⚠️ " + mismatch)
                return
    if before is not None:
        params = {**params, "_before": before}  # инертно для execute (apply_* читают свои ключи)
    # Человекочитаемая сводка по operation+params (деньги — реальное «40.00 → 48.00 (+20%)»).
    # Для create_rsa/create_gdn у вызывающего свой богатый summary → fmt вернёт "".
    display = texts.fmt_mutation_summary(operation, params) or summary
    # AD.2: баннер аккаунта — на КАЖДОЙ карточке (и в audit), включая Draft. Раз выбрано «одно
    # подтверждение везде», всегда-видимый ярлык — единственная страховка от мутации не того
    # аккаунта: менеджер видит, на ЧЬИ деньги идёт правка, до ✅. Боевой — ⚠️, Draft — 🧪 (спокойнее),
    # так что «реальные деньги» остаётся отличимым сигналом; тихий фолбэк на Draft больше не невидим.
    _banner_key = (
        "mutation_account_banner_draft"
        if customer_id == DRAFT_ACCOUNT_ID
        else "mutation_account_banner"
    )
    display = i18n.t(_banner_key, acct=_account_label(customer_id)) + "\n\n" + display
    if external_context and operation in _MONEY_OPS_UI:
        # Денежное предложение при внешнем контенте — усиленное предупреждение В СВОДКЕ (и в audit).
        display = i18n.t("external_context_money_warn") + "\n\n" + display
    await STORE.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=customer_id,  # штамп аккаунта мутации (authoritative для execute_confirmed)
        params=params,
        summary=display,
        chat_id=chat_id,
        user_initiated=True,
    )
    _LAST_PENDING[chat_id] = cid
    # C1: запоминаем кампанию/аккаунт этого черновика — чтобы следующий ход «измени гео ЭТОЙ
    # кампании» резолвился по контексту (детерминированная подстановка в agent.loop).
    _chat_ctx_note(
        chat_id, campaign=_campaign_name_from_params(operation, params), customer_id=customer_id
    )
    # §2B: запоминаем params последнего create_search_campaign (клон/новая кампания) — для
    # /savetemplate «сохранить как шаблон». _before инертен для шаблона → исключаем.
    if operation == "create_search_campaign":
        _LAST_SEARCH_PARAMS[chat_id] = {k: v for k, v in params.items() if k != "_before"}
    # Большой список ключей/минус-слов (ТЗ §5) → полный список .xlsx-вложением, кнопки на коротком
    # сообщении; в самой сводке список усечён до KW_INLINE_MAX с пометкой «…ещё N во вложении».
    # P1-6: необратимые удаления → двойное подтверждение (первый шаг), остальное — обычная ✅/❌.
    confirm_markup = (
        confirm_destructive_kb(cid) if operation in _DESTRUCTIVE_OPS else confirm_kb(cid)
    )
    kws = params.get("keywords") if isinstance(params, dict) else None
    # C4 (аудит 2026-07): create_search_campaign тоже несёт список ключей — раньше на финальном
    # гейте создания он усекался до KW_INLINE_MAX БЕЗ вложения, и менеджер жал ✅ на объёме,
    # который не видел (тот же класс, что и remove_negative_keywords выше).
    if (
        (operation in _KEYWORD_OPS or operation == "create_search_campaign")
        and isinstance(kws, list)
        and len(kws) > texts.KW_INLINE_MAX
    ):
        await ux.send_proposal_keywords_xlsx(
            message,
            keywords=kws,
            match_type=params.get("match_type", ""),
            action=texts.keyword_action_label(operation),
            header_html=i18n.t(
                "proposal_pending", summary=texts.esc(display), ttl_h=settings.proposal_ttl_hours
            ),
            reply_markup=confirm_markup,
            parse_mode=ParseMode.HTML,
        )
        return
    rendered = i18n.t(
        "proposal_pending", summary=texts.esc(display), ttl_h=settings.proposal_ttl_hours
    )
    if ux.proposal_fits(rendered):
        await message.answer(rendered, reply_markup=confirm_markup, parse_mode=ParseMode.HTML)
    else:
        # Длинный черновик (напр. RSA с 15 заголовками) не влезает в одно сообщение Telegram →
        # полный текст .txt-вложением, а кнопки ✅/❌ на коротком сообщении (его правит _do_confirm).
        await ux.send_proposal_text(
            message,
            full_text=display,
            header_html=i18n.t("proposal_long_header"),
            reply_markup=confirm_markup,
            parse_mode=ParseMode.HTML,
        )


# AD.3: отложенные мутации, ждущие выбора аккаунта (неоднозначно: активный не закреплён + живых >1).
# Стэш пер-чат; колбэк пикера аккаунта (target='mutate', bot/handlers/reports.py) резолвит аккаунт и
# доигрывает _present_proposal на выбранном. TTL — на случай, если пикер бросили (stale → «повтори»).
_PENDING_MUT: dict[int, dict] = {}
_PENDING_MUT_TTL_S = 600.0


def _pop_pending_mut(chat_id: int) -> dict | None:
    """Снять отложенную мутацию (одноразово). None, если её нет или протухла по TTL."""
    import time as _time

    p = _PENDING_MUT.pop(chat_id, None)
    if not p:
        return None
    if (_time.monotonic() - float(p.get("ts", 0.0))) > _PENDING_MUT_TTL_S:
        return None
    return p


async def _present_proposal_active(
    message: Message,
    *,
    chat_id: int,
    operation: str,
    params: dict,
    summary: str,
    cid: str,
    external_context: bool = False,
) -> None:
    """AD.3: мята мутации на АКТИВНОМ аккаунте чата (NL/клон/видео/ключи). Если аккаунт НЕ закреплён,
    а живых несколько (core.access.account_choice_pending) — НЕ угадываем (мутация не того аккаунта —
    чужие деньги): стэшим черновик и показываем ПИКЕР аккаунта (target='mutate'); после тапа
    _present_proposal доигрывается на выбранном (и он пинится активным). Закреплён / единственный
    живой / ноль живых — сразу _present_proposal (прежнее поведение). Мутационный замок ensure_allowed
    срабатывает уже внутри _present_proposal на итоговом аккаунте."""
    import time as _time

    acct = await _active_read_account(chat_id)
    if acct == DRAFT_ACCOUNT_ID:
        from core.access import account_choice_pending

        if await account_choice_pending(chat_id):  # внутри fail-closed к False (Draft, как раньше)
            _PENDING_MUT[chat_id] = {
                "operation": operation,
                "params": params,
                "summary": summary,
                "cid": cid,
                "external_context": external_context,
                "ts": _time.monotonic(),
            }
            rows = await _read_account_rows(chat_id)
            _REPORT_ACCT_CACHE[chat_id] = rows
            await message.answer(
                i18n.t("pick_account_before_mutation"),
                reply_markup=report_accounts_kb(
                    rows,
                    "mutate",
                    last=await _last_account(chat_id),
                    frequent=await _frequent_accounts(chat_id),
                ),
                parse_mode=ParseMode.HTML,
            )
            return
    await _present_proposal(
        message,
        chat_id=chat_id,
        operation=operation,
        params=params,
        summary=summary,
        cid=cid,
        external_context=external_context,
        customer_id=acct,
    )


async def _abandon_active_flow(chat_id: int, state: FSMContext) -> bool:
    """§19/§20: свернуть активный визард/сбор ввода (Создание кампании / Клиенты / KW / RSA):
    abandon черновика §19 + чистка его временных image-медиа, сброс GDN/ассет-медиа, буфера текста
    профиля и ingest-контекста, затем state.clear(). Возвращает True, если что-то было свёрнуто.
    Общая логика для inline-«✖ Отмена» (on_nav_cancel) и команды /cancel (B14)."""
    data = await state.get_data()
    had_state = await state.get_state() is not None
    media_id = data.get("gdn_media_id") or data.get("ext_media_id")
    if media_id:
        await asyncio.to_thread(clear_pending_media, media_id)
    cc_session = data.get("cc_session")
    if cc_session:
        snap = await CDRAFTS.get(cc_session, expected_chat_id=chat_id)
        if snap is not None:
            for mid in (snap.wizard_state.get("images") or {}).get("media_ids") or []:
                await asyncio.to_thread(clear_pending_media, mid)
        await CDRAFTS.abandon(cc_session, expected_chat_id=chat_id)
    _PENDING_CONTEXT.pop(chat_id, None)  # ingest: бросаем недоиспользованный контент файла
    _CLI_TEXT_BUF.pop(chat_id, None)  # §20.3: бросаем накопленный буфер текста профиля
    _cli_cancel_idle(chat_id)  # B13: гасим таймер авто-сохранения
    await state.clear()
    return had_state or bool(cc_session)


def _cc_draft_has_content(draft) -> bool:
    """W5: есть ли в черновике §19 накопленная работа, которую жалко терять при отмене
    (настройки/ключи/объявление/медиа/ассеты/URL-опции). Пустой черновик — нечего спасать."""
    if draft is None:
        return False
    st = draft.wizard_state or {}
    ad = st.get("ad") or {}
    assets = st.get("assets") or {}
    return bool(
        st.get("settings")
        or (st.get("keywords") or {}).get("list")
        or ad.get("final_url")
        or ad.get("headlines")
        or (st.get("images") or {}).get("media_ids")
        or assets.get("new")
        or assets.get("reuse_links")
        or any((st.get("url_options") or {}).values())
    )


async def _maybe_cc_exit_dialog(target: Message, chat_id: int, state: FSMContext) -> bool:
    """W5 (живой тест 2026-07-06): «✖ Отмена»/'/cancel' в визарде §19 БЕЗВОЗВРАТНО бросали
    черновик (status=abandoned выпадает из всех резюм-путей). Если активный черновик из FSM
    содержит работу — вместо abandon показываем диалог «сохранить / удалить / вернуться».
    Пустой черновик (первый экран без ввода) — прежний быстрый путь без диалога.
    Возвращает True, если диалог показан (вызыватель НЕ должен abandon-ить)."""
    data = await state.get_data()
    cc_session = data.get("cc_session")
    if not cc_session:
        return False
    draft = await CDRAFTS.get(cc_session, expected_chat_id=chat_id)
    if draft is None or draft.status != "active" or not _cc_draft_has_content(draft):
        return False
    await target.answer(
        i18n.t(
            "cc_exit_confirm",
            step=max(1, int(draft.current_step)),
            ttl_h=int(settings.campaign_draft_ttl_hours),
        ),
        reply_markup=cc_exit_kb(),
        parse_mode=ParseMode.HTML,
    )
    return True


async def _cc_exit_drop_flow(chat_id: int, state: FSMContext) -> None:
    """W5: «🗑 Удалить черновик» из диалога выхода. _abandon_active_flow закрывает черновик из
    FSM-данных; если FSM уже очищен (menu-guard между диалогом и кнопкой) — добиваем активный
    черновик чата напрямую, с той же чисткой временных image-медиа."""
    await _abandon_active_flow(chat_id, state)
    draft = await CDRAFTS.get_active(chat_id)
    if draft is not None:
        for mid in (draft.wizard_state.get("images") or {}).get("media_ids") or []:
            await asyncio.to_thread(clear_pending_media, mid)
        await CDRAFTS.abandon(draft.session_id, expected_chat_id=chat_id)


async def _suspend_active_flow_soft(m: Message, state: FSMContext) -> tuple[int | None, bool]:
    """3A: МЯГКО свернуть активный флоу при нажатии кнопки главного меню — работа НЕ теряется
    (в отличие от _abandon_active_flow для «✖ Отмена»/cancel):
      • черновик визарда §19 остаётся active (НЕ abandon, медиа НЕ чистим) — возврат через
        «➕ Создание кампании» → «▶️ Продолжить»;
      • непустой буфер текста профиля §20 СБРАСЫВАЕТСЯ в confirm-черновик (ничего не пишется
        без ✅ — тот же путь, что «💾 Сохранить»);
      • лёгкие состояния (GDN/ext-медиа, ingest) закрываются как обычно.
    Возвращает (шаг активного черновика §19 или None, был ли буфер §20 сброшен в черновик)."""
    chat_id = m.chat.id
    data = await state.get_data()
    media_id = data.get("gdn_media_id") or data.get("ext_media_id")
    if media_id:
        await asyncio.to_thread(clear_pending_media, media_id)
    cc_step: int | None = None
    cc_session = data.get("cc_session")
    if cc_session:
        snap = await CDRAFTS.get(cc_session, expected_chat_id=chat_id)
        if snap is not None and snap.status == "active":
            cc_step = int(snap.current_step)  # черновик жив — только подсказка о возврате
    _PENDING_CONTEXT.pop(chat_id, None)
    _cli_cancel_idle(chat_id)
    cli_flushed = False
    buf = _CLI_TEXT_BUF.pop(chat_id, None)
    if buf:
        cust = str(data.get("cli_customer_id") or DRAFT_ACCOUNT_ID)
        try:
            cli_flushed = await _cli_extract_and_propose(m.bot, chat_id, cust, buf)
        except Exception:  # noqa: BLE001 — сбой извлечения не должен блокировать кнопку меню
            log.warning("menu-guard: буфер §20 не сброшен в черновик (chat=%s)", chat_id)
    await state.clear()
    return cc_step, cli_flushed


# ── Модель ИИ (/model): рантайм-переключатель OpenRouter-модели (глобально на процесс) ─────
def _valid_model_slug(s: str) -> str | None:
    """OpenRouter-slug вида vendor/model (до 128 — лимит колонки user_settings.model_override),
    без пробелов. Не валидируем существование/tool use — это покажет первый реальный вызов."""
    s = (s or "").strip()
    if not s or len(s) > 128 or "/" not in s or any(c.isspace() for c in s):
        return None
    return s


async def _save_selected_account(chat_id: int, customer_id: str | None) -> None:
    """Upsert выбранного аккаунта ЧТЕНИЯ чата (None = сброс на Draft). Переживает рестарт.
    Тонкий делегат core.access.set_active_account — единая точка персиста (2B)."""
    from core.access import set_active_account

    await set_active_account(chat_id, customer_id)


# §UX-память: последний выбранный период отчётов (chat_id → код пресета "7"|"30"|"90"|"MTD").
# In-memory кэш поверх user_settings.ui_prefs (персист переживает рестарт).
_LAST_PERIOD_CODE: dict[int, str] = {}

# 2.11: живой планировщик (ставится в main() после setup_scheduler) — /myschedule применяет
# персональное расписание без рестарта; None (тесты/ранний старт) → «применится после рестарта».
SCHED = None


async def _save_report_schedule(chat_id: int, cron: str | None) -> None:
    """2.11 (§14): персист персонального расписания отчёта (UserSettings.report_schedule).
    None → NULL (оператор вернётся на глобальное). Валидацию crontab делает вызывающий UI
    (CronTrigger.from_crontab); здесь только запись (переживает рестарт)."""
    from sqlalchemy import select as _select

    from db.models import UserSettings as _US
    from db.session import Session as _S

    val = (cron or "").strip()[:128] or None
    async with _S() as s:
        row = (
            await s.execute(_select(_US).where(_US.chat_id == int(chat_id)))
        ).scalar_one_or_none()
        if row is None:
            s.add(_US(chat_id=int(chat_id), report_schedule=val))
        else:
            row.report_schedule = val
        await s.commit()


async def _apply_report_schedule_live(bot, chat_id: int, cron: str | None) -> bool:
    """2.11: применить персональное расписание в ЖИВОМ планировщике (без рестарта). SCHED нет
    (тесты/ранний старт) → False («после рестарта»). off → снять per-chat джобу."""
    if SCHED is None:
        return False
    try:
        from scheduler.service import register_user_report_schedules

        if not cron:
            try:  # register только добавляет — снятую джобу убираем явно
                SCHED.remove_job(f"scheduled_report_{int(chat_id)}")
            except Exception:  # noqa: BLE001 — джобы могло не быть
                pass
            return True
        await register_user_report_schedules(SCHED, bot)
        return True
    except Exception:  # noqa: BLE001 — live-применение best-effort, персист уже сделан
        return False


async def _save_per_account_thresholds(chat_id: int, acct: str, values: dict) -> None:
    """2.11 (§14): записать per-account оверлей порогов аномалий (alert_thresholds['per_account']).
    НАСТРОЙКА БОТА (как /alerts) — пишется ТОЛЬКО по тапу человека («✅ Принять» в предложении
    тюнера); Google Ads не трогается. JSON переприсваиваем целиком (конвенция SQLAlchemy)."""
    from sqlalchemy import select as _select

    from db.models import UserSettings as _US
    from db.session import Session as _S

    acct = normalize_customer_id(acct)
    async with _S() as s:
        row = (
            await s.execute(_select(_US).where(_US.chat_id == int(chat_id)))
        ).scalar_one_or_none()
        base = dict((row.alert_thresholds if row is not None else None) or {})
        per = dict(base.get("per_account") or {})
        per[acct] = {k: float(v) for k, v in values.items()}
        base["per_account"] = per
        if row is None:
            s.add(_US(chat_id=int(chat_id), alert_thresholds=base))
        else:
            row.alert_thresholds = base
        await s.commit()


async def _load_ui_pref(chat_id: int, key: str) -> str | None:
    """Значение ui_prefs[key] чата из user_settings (JSON). Нет строки/ключа/сбой → None."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    try:
        async with Session() as s:
            row = (
                await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
            ).scalar_one_or_none()
            val = (row.ui_prefs or {}).get(key) if row else None
            return str(val) if val is not None else None
    except Exception:  # noqa: BLE001 — UX-настройка не критична, отчёт не роняем
        return None


async def _save_ui_pref(chat_id: int, key: str, value: str) -> None:
    """Upsert ui_prefs[key] чата (переживает рестарт). JSON-колонку переприсваиваем целиком
    (SQLAlchemy не отслеживает мутацию вложенного dict). Best-effort: сбой не роняет отчёт."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    try:
        async with Session() as s:
            row = (
                await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
            ).scalar_one_or_none()
            if row is None:
                s.add(UserSettings(chat_id=chat_id, ui_prefs={key: value}))
            else:
                row.ui_prefs = {**(row.ui_prefs or {}), key: value}
            await s.commit()
    except Exception:  # noqa: BLE001 — UX-настройка не критична
        log.debug("ui_pref не сохранён chat=%s key=%s", chat_id, key)


# 3H (M10): пороги аномалий per-chat (UserSettings.alert_thresholds, JSON). Ключи и дефолты —
# scheduler.anomaly.DEFAULT_THRESHOLDS; здесь только персист/валидация. НАСТРОЙКА БОТА (не Ads).
_ALERT_FIELD_KEYS = {"spike": "spend_spike_pct", "drop": "conv_drop_pct", "minspend": "min_spend"}


def _alert_value_ok(key: str, value: float) -> bool:
    """Диапазоны считает КОД: проценты 1–1000, min_spend 0–MONEY_MAX_UNITS."""
    from core.limits import MONEY_MAX_UNITS

    if key in ("spend_spike_pct", "conv_drop_pct"):
        return 1.0 <= value <= 1000.0
    return 0.0 <= value <= float(MONEY_MAX_UNITS)


async def _load_alert_thresholds(chat_id: int) -> dict:
    """Эффективные пороги чата: DEFAULT_THRESHOLDS ∪ сохранённые per-chat."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session
    from scheduler.anomaly import DEFAULT_THRESHOLDS

    saved: dict = {}
    try:
        async with Session() as s:
            row = (
                await s.execute(
                    select(UserSettings.alert_thresholds).where(UserSettings.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            saved = dict(row or {})
    except Exception:  # noqa: BLE001 — настройка не критична, показываем дефолты
        saved = {}
    return {**DEFAULT_THRESHOLDS, **saved}


async def _save_alert_threshold(chat_id: int, key: str, value: float | None) -> None:
    """Upsert одного порога (value=None при key='' — полный сброс на дефолты). JSON-колонку
    переприсваиваем целиком (SQLAlchemy не отслеживает мутацию вложенного dict)."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
        ).scalar_one_or_none()
        if key == "" or value is None:  # полный сброс
            if row is not None:
                row.alert_thresholds = None
                await s.commit()
            return
        merged = {**(row.alert_thresholds or {} if row else {}), key: float(value)}
        if row is None:
            s.add(UserSettings(chat_id=chat_id, alert_thresholds=merged))
        else:
            row.alert_thresholds = merged
        await s.commit()


async def _remember_period(chat_id: int, code: str) -> None:
    """§UX-память: запомнить выбранный ПРЕСЕТ периода (7/30/90/MTD) для кнопки «↻ как в прошлый
    раз». Произвольные диапазоны дат не запоминаем (разовые)."""
    from reports.period import PRESET_DAYS

    c = (code or "").strip()
    if not (c in PRESET_DAYS or c.upper() == "MTD"):
        return
    c = c.upper() if c.upper() == "MTD" else c
    _LAST_PERIOD_CODE[chat_id] = c
    await _save_ui_pref(chat_id, "last_report_period", c)


async def _last_period(chat_id: int) -> str | None:
    """Последний пресет периода чата: кэш процесса → user_settings.ui_prefs (после рестарта)."""
    return _LAST_PERIOD_CODE.get(chat_id) or await _load_ui_pref(chat_id, "last_report_period")


_LAST_ACCOUNT: dict[int, str] = {}


async def _remember_account(chat_id: int, customer_id: str) -> None:
    """§UX-память: запомнить последний ВЫБРАННЫЙ в пикере аккаунт отчёта (для кнопки «↻ как в
    прошлый раз»). Только валидный нормализованный id (Draft тоже помним — частый одно-акк. кейс)."""
    cid = normalize_customer_id(customer_id)
    if not cid:
        return
    _LAST_ACCOUNT[chat_id] = cid
    await _save_ui_pref(chat_id, "last_account", cid)


async def _last_account(chat_id: int) -> str | None:
    """Последний выбранный аккаунт чата: кэш процесса → ui_prefs (после рестарта)."""
    return _LAST_ACCOUNT.get(chat_id) or await _load_ui_pref(chat_id, "last_account")


# 1.7 (аудит 2026-07-06): «частые аккаунты» per-chat — TTL-кэш агрегата активности (10 мин).
_FREQ_CACHE: dict[int, tuple[float, list[str]]] = {}
_FREQ_TTL_S = 600.0


async def _frequent_accounts(chat_id: int, n: int = 3, days: int = 30) -> list[str]:
    """Частые аккаунты оператора за N дней — по его же активности (audit_log ∪ proposals ∪
    recommendation, GROUP BY customer_id, count DESC). Читает только ЛОКАЛЬНУЮ БД (не Ads) и
    ничего не открывает: результат пересекается с read-allowed rows на месте вызова (пикер и так
    строится из _read_account_rows — замок × грант не обходятся). Draft не считаем «частым»
    (звёздочки — про живые). TTL-кэш 10 мин (агрегат горячий: каждый пикер). Сбой БД → []."""
    import time as _time

    hit = _FREQ_CACHE.get(chat_id)
    if hit and (_time.monotonic() - hit[0]) < _FREQ_TTL_S:
        return hit[1][:n]
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select as _select

    from db.models import AuditLog as _AL
    from db.models import Proposal as _P
    from db.models import Recommendation as _R
    from db.session import Session as _S

    counts: Counter[str] = Counter()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    try:
        async with _S() as s:
            for model in (_AL, _P, _R):
                rows = (
                    await s.execute(
                        _select(model.customer_id, model.created_at).where(
                            model.chat_id == int(chat_id)
                        )
                    )
                ).all()
                for cid, created in rows:
                    # tz-нейтральная конвенция проекта: фильтр по дате в Python (naive → UTC)
                    if created is not None and created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created is not None and created < cutoff:
                        continue
                    ncid = normalize_customer_id(str(cid or ""))
                    if ncid and ncid != DRAFT_ACCOUNT_ID:
                        counts[ncid] += 1
    except Exception:  # noqa: BLE001 — «частые» — косметика, сбой БД не ломает пикеры
        return []
    top = [cid for cid, _cnt in counts.most_common(max(1, int(n)) * 3)]
    _FREQ_CACHE[chat_id] = (_time.monotonic(), top)
    return top[:n]


async def _save_report_recall(
    chat_id: int, account: str, campaign_id: str | None, campaign_name: str | None, period_code: str
) -> None:
    """§UX-память: запомнить последний ПОСТРОЕННЫЙ отчёт (аккаунт+кампания+период) для кнопки
    «↻ повторить прошлый отчёт». Только пресетный период (произвольные диапазоны — разовые). Аккаунт
    ПЕРЕ-проверяется на чтении при повторе (не тут). JSON-блоб в ui_prefs (переживает рестарт)."""
    import json

    from reports.period import PRESET_DAYS

    code = (period_code or "").strip()
    norm = code.upper() if code.upper() == "MTD" else code
    if not (norm in PRESET_DAYS or norm == "MTD"):
        return
    acct = normalize_customer_id(account)
    if not acct:
        return
    payload = json.dumps(
        {
            "account": acct,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "period": norm,
        }
    )
    await _save_ui_pref(chat_id, "last_report_sel", payload)


async def _load_report_recall(chat_id: int) -> dict | None:
    """Последний построенный отчёт чата (аккаунт+кампания+период) из ui_prefs — для «↻ повторить».
    None, если ничего не сохранено / блоб битый / нет обязательных полей."""
    import json

    raw = await _load_ui_pref(chat_id, "last_report_sel")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001 — битый блоб → как будто памяти нет
        return None
    return d if isinstance(d, dict) and d.get("account") and d.get("period") else None


async def _active_read_account(chat_id: int) -> str:
    """Активный аккаунт чата для /status /report /export /sheets И для мут-мят (NL/клон/видео/ключи
    через _present_proposal_active): выбранный через /account или Draft. Делегат
    core.access.get_active_account — ЕДИНСТВЕННАЯ точка резолва (2B): перепроверяет И глобальный
    read-замок, И пер-пользовательский грант (fail-closed → Draft при сужении списков ИЛИ отзыве
    гранта). Мутация на этом аккаунте всё равно проходит ensure_allowed (замок мутаций) + confirm-гейт;
    при неоднозначности (не закреплён + живых >1) мут-мяты форсят выбор аккаунта (AD.3)."""
    from core.access import get_active_account

    try:
        return await get_active_account(chat_id)
    except Exception:  # noqa: BLE001 — сбой чтения настройки не должен ломать отчёты
        return DRAFT_ACCOUNT_ID


def _live_account_hint(acct: str) -> str:
    """§8 F: подсказка, если работаем на пустом Draft, а у бота есть ЖИВЫЕ read-аккаунты (обход MCC
    или env read-list). Иначе '' — не шумим. Чинит «вижу только черновик»: зовём выбрать живой."""
    if str(acct) != str(DRAFT_ACCOUNT_ID):
        return ""
    from ads.client import discovered_read_children

    live = {c for c in discovered_read_children() if str(c) != str(DRAFT_ACCOUNT_ID)}
    live |= {c for c in settings.read_customer_ids if str(c) != str(DRAFT_ACCOUNT_ID)}
    return i18n.t("live_account_hint") if live else ""


async def _require_read_account(m: Message, flow: str, *, chat_id: int | None = None) -> str | None:
    """Аккаунт ЧТЕНИЯ для быстрого пути (/report 30, /campaigns, NL-статистика). Если оператор
    НЕ выбрал аккаунт, а живых read-аккаунтов НЕСКОЛЬКО (core.access.account_choice_pending) —
    быстрый путь молча читал бы пустой Draft: вместо этого шлём подсказку + flow-пикер и
    возвращаем None (вызывающий выходит). Пин Draft / единственный живой (авто-дефолт) / ноль
    живых — прежнее одношаговое поведение. Только ЧТЕНИЕ: мутационный замок ensure_allowed не
    затрагивается (golden rule 9)."""
    cid = chat_id if chat_id is not None else m.chat.id
    acct = await _active_read_account(cid)
    if acct != DRAFT_ACCOUNT_ID:
        return acct
    from core.access import account_choice_pending

    if not await account_choice_pending(cid):  # внутри fail-closed к False (Draft, как раньше)
        return acct
    await m.answer(i18n.t("pick_live_account_first"), parse_mode=ParseMode.HTML)
    if flow == "campaigns":
        await _start_campaigns_picker(m)
    elif flow in ("report", "export", "sheets"):
        await _start_report_picker(m, flow)
    else:  # generic: выбрать АКТИВНЫЙ аккаунт (персист, /account)
        await _start_setacct_picker(m)
    return None


async def _start_campaigns_picker(m: Message) -> None:
    """Пикер аккаунта для /campaigns (target='campaigns'): после тапа — _send_campaigns_for на
    выбранном, глобальный активный аккаунт НЕ трогаем (паттерн _present_report_campaigns)."""
    chat_id = m.chat.id
    rows = await _read_account_rows(chat_id)
    _REPORT_ACCT_CACHE[chat_id] = rows
    await m.answer(
        i18n.t("campaigns_pick_account"),
        reply_markup=report_accounts_kb(
            rows,
            "campaigns",
            last=await _last_account(chat_id),
            frequent=await _frequent_accounts(chat_id),
        ),
        parse_mode=ParseMode.HTML,
    )


async def _keyword_metrics_account(chat_id: int) -> tuple[str, bool]:
    """P1-7: аккаунт для Keyword Planner-метрик (READ-ONLY). Идеи ключей — рыночные (агрегат Google,
    не данные аккаунта), поэтому годится ЛЮБОЙ живой аккаунт; тест/Draft отдаёт ПУСТЫЕ метрики →
    мало слов и мёртвая сортировка. Берём: (1) активный живой read-аккаунт оператора; иначе (2)
    первый живой дочерний из обхода MCC (read-allowed); иначе (3) Draft (деградация). Возвращает
    (customer_id, is_live). Замок мутаций НЕ трогается — это чтение; generate_keyword_ideas сам
    зовёт ensure_read_allowed на выбранном аккаунте."""
    from ads.client import discovered_read_children

    acct = await _active_read_account(chat_id)
    if acct and str(acct) != str(DRAFT_ACCOUNT_ID):
        return acct, True
    for cid in sorted(discovered_read_children()):
        if str(cid) != str(DRAFT_ACCOUNT_ID):
            return cid, True
    return (acct or DRAFT_ACCOUNT_ID), False


async def _heal_if_stuck_global(m: Message, acct: str) -> None:
    """Само-восстановление «залипшего» аккаунта чтения. Если read упал на НЕ-Draft аккаунте, который
    сейчас выбран ГЛОБАЛЬНО (/account), — сбрасываем выбор на Draft и сообщаем. Так один недоступный
    аккаунт (обычно «customer not enabled»: не под настроенным MCC/деактивирован) не «залипает» и не
    ломает следующие /status /report /keywords. Разовый выбор в пикере (не глобальный) НЕ трогаем."""
    if acct == DRAFT_ACCOUNT_ID:
        return
    if acct != await _active_read_account(
        m.chat.id
    ):  # это не глобальный выбор (пик в /report) — не трогаем
        return
    try:
        # ПИНим Draft явно (не None): при авто-дефолте «единственный живой аккаунт» сброс на None
        # вернул бы нас на ТОТ ЖЕ сломанный аккаунт (если он единственный живой). Пин Draft escape'ит.
        await _save_selected_account(m.chat.id, DRAFT_ACCOUNT_ID)
    except Exception:  # noqa: BLE001 — сброс best-effort, не роняем обработку ошибки
        pass
    await m.answer(i18n.t("acct_reset_auto", acct=texts.esc(acct)), parse_mode=ParseMode.HTML)


async def _read_account_rows(chat_id: int) -> list:
    """§8: аккаунты, доступные ЭТОМУ оператору на ЧТЕНИЕ (для пикеров /report /export /sheets и
    Этапа-0 §19 / §20): Draft + мутационный список + env read-list + обнаруженные дочерние (с
    именами/валютой из meta). Дедуп по нормализованному id; КАЖДЫЙ прогоняется через
    ensure_read_allowed И пер-пользовательский грант (core.access, 2B) ⇒ список доказуемо ⊆ обоих
    замков (пикер = граница доступа). В legacy-проходе (auto + пустая таблица грантов) пер-юзер
    фильтр пропускает всё — прежнее одно-операторное поведение. Пустой обход MCC ⇒ как минимум
    Draft (список НИКОГДА не пуст)."""
    from ads.client import (
        discovered_read_children,
        discovered_read_children_meta,
        ensure_read_children_discovered,
    )
    from ads.read import ChildAccount
    from core.access import ensure_account_allowed_for_user

    # Само-починка (2026-07): если обход MCC на старте не прошёл (транзиентный сбой/таймаут), набор
    # дочерних пуст и пикер деградировал бы на Draft+env read-list до суточного re-discovery/ручного
    # /refresh («часть аккаунтов пропала ВЕЗДЕ»). Обойти MCC СЕЙЧАС (no-op при непустом наборе —
    # нулевая латентность здорового пути) ⇒ этот же тап показывает ВСЕ видимые аккаунты. Один
    # чокпойнт лечит все пикеры (/report /export /sheets /campaigns /status /advise /account, §19/§20).
    await ensure_read_children_discovered()
    meta = discovered_read_children_meta()
    candidate = [DRAFT_ACCOUNT_ID] + sorted(
        settings.allowed_customer_ids | settings.read_customer_ids | discovered_read_children()
    )
    rows: list = []
    seen: set[str] = set()
    for raw in candidate:
        cid = normalize_customer_id(raw)
        if not cid or cid in seen:
            continue
        try:
            ensure_read_allowed(cid)  # доказуемо ⊆ read-замка (fail-closed)
            await ensure_account_allowed_for_user(chat_id, cid)  # пер-юзер грант (2B)
        except PermissionError:
            continue
        seen.add(cid)
        if cid in meta:
            rows.append(meta[cid])
        elif cid == DRAFT_ACCOUNT_ID:
            rows.append(_cc_draft_account_row())
        else:  # id без meta (env read-list / не обойден) — минимальная строка
            rows.append(
                ChildAccount(
                    id=cid, name=cid, currency="", manager=False, level=0, status="ENABLED"
                )
            )

    # D2: предсказуемый для человека порядок — Draft всегда первым, затем активные (ENABLED), затем
    # по имени (раньше был sorted() по числовому id → «случайный» для оператора). Сортируем ГОТОВЫЕ
    # строки перед возвратом → idx-кэши пикеров согласованы с показанным порядком (callback idx→row).
    def _acct_sort_key(row):
        is_draft = 0 if normalize_customer_id(str(row.id)) == DRAFT_ACCOUNT_ID else 1
        active = 0 if (getattr(row, "status", "") or "").upper() == "ENABLED" else 1
        return (is_draft, active, (getattr(row, "name", "") or "").casefold())

    rows.sort(key=_acct_sort_key)
    return rows


async def _report_target(chat_id: int) -> tuple[str, str | None, str | None]:
    """Аккаунт + (опц.) кампания для отчёта: из выбора пикера (_REPORT_SEL) или активный аккаунт
    целиком (быстрый путь /report 30). Аккаунт ПЕРЕ-проверяется ensure_read_allowed (fail-closed →
    Draft без кампании: НИКОГДА не строим отчёт по неразрешённому аккаунту)."""
    sel = _REPORT_SEL.get(chat_id)
    if sel and sel.get("account"):
        acct = normalize_customer_id(sel["account"])
        try:
            ensure_read_allowed(acct)
            from core.access import ensure_account_allowed_for_user

            await ensure_account_allowed_for_user(chat_id, acct)  # пер-юзер грант (2B, TOCTOU)
        except PermissionError:
            return DRAFT_ACCOUNT_ID, None, None
        return acct, sel.get("campaign_id"), sel.get("campaign_name")
    return await _active_read_account(chat_id), None, None


async def _start_report_picker(m: Message, target: str) -> None:
    """Показать выбор аккаунта для отчёта/экспорта (target = report|export|sheets). Аккаунты — все
    read-allowed (граница доступа). Один аккаунт (только Draft) ⇒ сразу к выбору кампании."""
    rows = await _read_account_rows(m.chat.id)
    _REPORT_ACCT_CACHE[m.chat.id] = rows
    _REPORT_SEL.pop(m.chat.id, None)  # начинаем выбор заново
    if len(rows) == 1:
        await _present_report_campaigns(m, target, rows[0])
        return
    await m.answer(
        i18n.t("report_pick_account"),
        # §UX-память: последний выбранный аккаунт — первой кнопкой «↻ как в прошлый раз»
        reply_markup=report_accounts_kb(
            rows,
            target,
            last=await _last_account(m.chat.id),
            frequent=await _frequent_accounts(m.chat.id),
        ),
    )


async def _present_report_campaigns(m: Message, target: str, acct_row, *, cq=None) -> None:
    """После выбора аккаунта: запомнить его ТОЛЬКО для ЭТОГО отчёта (_REPORT_SEL, в памяти) и показать
    «Весь аккаунт» + список кампаний. ВАЖНО: НЕ трогаем глобальный активный аккаунт (_save_selected_
    account) — иначе выбор аккаунта для разового отчёта «залипал» бы на /keywords и /status и один
    недоступный аккаунт ломал бы всё. Глобальный аккаунт чтения переключает только команда /account.

    cq!=None (тап в пикере) ⇒ РЕДАКТИРУЕМ сообщение под кнопкой вместо нового (P0-3: без дублей при
    ходьбе аккаунт→кампания→период). cq=None (вход по команде/reply-кнопке) ⇒ обычный .answer."""
    cid = normalize_customer_id(getattr(acct_row, "id", "") or DRAFT_ACCOUNT_ID)
    try:
        ensure_read_allowed(cid)  # TOCTOU: обход мог измениться между рендером и тапом
        from core.access import ensure_account_allowed_for_user

        await ensure_account_allowed_for_user(m.chat.id, cid)  # пер-юзер грант (2B, TOCTOU)
    except PermissionError:
        await m.answer(i18n.t("account_denied", cid=texts.esc(cid)), parse_mode=ParseMode.HTML)
        return
    _REPORT_SEL[m.chat.id] = {"account": cid, "campaign_id": None, "campaign_name": None}
    await _remember_account(m.chat.id, cid)  # §UX-память: «↻ аккаунт как в прошлый раз»
    camps: list[dict] = []
    try:
        from ads.client import build_client_async
        from ads.read import list_campaigns

        camps = await run_ads_read_call(
            list_campaigns, await build_client_async(cid), cid, label="report_campaigns"
        )
    except Exception:  # noqa: BLE001 — нет кампаний/сбой чтения/аккаунт недоступен → только «Весь аккаунт»
        camps = []
    _REPORT_CAMP_CACHE[m.chat.id] = camps
    if cq is not None:
        await _safe_edit(
            cq, i18n.t("report_pick_campaign"), reply_markup=report_campaigns_kb(camps, target)
        )
    else:
        await m.answer(
            i18n.t("report_pick_campaign"), reply_markup=report_campaigns_kb(camps, target)
        )


async def _load_model_override() -> str | None:
    """Сохранённая активная модель из глобальной строки user_settings (None — дефолты по ролям)."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    async with Session() as s:
        row = (
            await s.execute(
                select(UserSettings).where(UserSettings.chat_id == GLOBAL_SETTINGS_CHAT_ID)
            )
        ).scalar_one_or_none()
        return row.model_override if row else None


async def _save_model_override(model: str | None) -> None:
    """Upsert активной модели в глобальную строку user_settings (переживает рестарт)."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    async with Session() as s:
        row = (
            await s.execute(
                select(UserSettings).where(UserSettings.chat_id == GLOBAL_SETTINGS_CHAT_ID)
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(UserSettings(chat_id=GLOBAL_SETTINGS_CHAT_ID, model_override=model))
        else:
            row.model_override = model
        await s.commit()


async def _persist_and_set_model(model: str | None) -> None:
    """Применить модель в рантайме (router) + персист в БД. Сбой БД не мешает применению."""
    router.set_active_model(model)
    try:
        await _save_model_override(model)
    except Exception as e:  # БД недоступна — модель всё равно активна до рестарта
        log.warning("model_override не сохранён в БД: %s", type(e).__name__)


# D4: пикер кампаний для /pause и /resume без аргумента — chat_id → отфильтрованный по статусу список.
_SLASH_MUT_CACHE: dict[int, list[dict]] = {}


async def _slash_mutate_present(message: Message, chat_id: int, operation: str, name: str) -> None:
    """Мятие proposal паузы/возобновления по ИМЕНИ кампании (общий хвост текст-команды и пикера
    D4). На АКТИВНОМ аккаунте (AD.4); при неоднозначности — форс-пикер аккаунта. Confirm-гейт."""
    try:
        cid, op, params, summary = _build_proposal(operation, campaign=name)
    except Exception as e:  # валидация схемы
        await message.answer(f"⚠️ {ux.err_text(e)}")
        return
    await _present_proposal_active(
        message, chat_id=chat_id, operation=op, params=params, summary=summary, cid=cid
    )


async def _slash_mutate(m: Message, command: CommandObject, operation: str) -> None:
    """Слэш-команда паузы/возобновления по имени кампании → черновик за confirm-гейтом
    (тот же путь, что inline-кнопка и текстовая команда). Без имени — D4: пикер подходящих
    кампаний (ENABLED для паузы / PAUSED для возобновления); ввод имени командой остаётся."""
    name = (command.args or "").strip()
    if not name:
        # D4: вместо только текст-подсказки — пикер кампаний нужного статуса (best-effort).
        want = "ENABLED" if operation == "pause_campaign" else "PAUSED"
        camps = [c for c in await _kw_add_load_campaigns(m.chat.id) if c.get("status") == want]
        if camps:
            _SLASH_MUT_CACHE[m.chat.id] = camps
            key = "slash_pause_pick" if operation == "pause_campaign" else "slash_resume_pick"
            await m.answer(i18n.t(key), reply_markup=slash_mutate_campaigns_kb(camps, operation))
            return
        key = "slash_pause_hint" if operation == "pause_campaign" else "slash_resume_hint"
        await m.answer(i18n.t(key), parse_mode=ParseMode.HTML)
        return
    # AD.4: пауза/возобновление — на АКТИВНОМ аккаунте (не хардкод Draft). При неоднозначности
    # (не закреплён + живых >1) — форс-пикер; иначе _present_proposal на активном (кампания
    # резолвится на исполнении на итоговом аккаунте).
    await _slash_mutate_present(m, m.chat.id, operation, name)


async def _read_currency(client, customer_id: str | None = None) -> str:
    """Код валюты аккаунта (§9) для денежных метрик. '' при сбое чтения — отчёт/статистику
    показываем и без валюты (не блокируем). Кэш — в ads.read.account_currency.
    customer_id — активный аккаунт чтения (§6 /account); None ⇒ Draft (прежнее поведение)."""
    from ads.read import account_currency

    try:
        return await run_ads_read_call(
            account_currency, client, customer_id or DRAFT_ACCOUNT_ID, label="account_currency"
        )
    except Exception:  # noqa: BLE001 — валюта необязательна, не роняем отчёт
        return ""


def _period_from_arg(arg: str | None):
    """Аргумент команды → Period (§9). Поддержка: пресет 7/30/90/MTD; произвольный диапазон или
    день в ISO ГГГГ-ММ-ДД (одна дата → день, две → диапазон). По умолчанию 30 дн. Бросает ValueError."""
    import re
    from datetime import date

    from reports.period import custom, from_preset

    s = (arg or "").strip()
    if not s:
        return from_preset("30")
    iso = re.findall(r"\d{4}-\d{2}-\d{2}", s)
    if iso:
        try:
            ds = [date.fromisoformat(d) for d in iso[:2]]
        except ValueError as e:
            raise ValueError("дата в формате ГГГГ-ММ-ДД, напр. 2026-06-01") from e
        return custom(ds[0], ds[0]) if len(ds) == 1 else custom(min(ds), max(ds))
    return from_preset(s)


def _scope_note(campaign_name: str | None) -> str:
    """Суффикс к сводке/имени файла, если отчёт скоупнут по кампании (иначе пусто = весь аккаунт)."""
    return f" · {campaign_name}" if campaign_name else ""


async def _run_report(
    m: Message, period, acct: str, campaign_id: str | None, campaign_name: str | None
) -> None:
    """Read-only сводка по аккаунту (или одной кампании) за период. Общий код команды/пикера."""
    try:
        from ads.client import build_client_async
        from reports.service import build_account_report_async, summary_text

        client = await build_client_async(acct)  # холодная сборка — вне loop
        async with ux.typing_action(m):
            # build_account_report_async параллелит 9 GAQL-запросов под семафором (≈2.5-3x быстрее)
            report = await build_account_report_async(
                client, acct, period, campaign_id=campaign_id, account_name=_account_name(acct)
            )
            report.currency = await _read_currency(client, acct)  # §9: валюта денежных метрик
    except Exception as e:  # сеть/доступ/SDK
        hint = _inactive_read_hint(acct) if is_account_access_error(e) else ""  # 2.3
        if hint:
            await m.answer(hint, parse_mode=ParseMode.HTML)
        else:
            await m.answer(i18n.t("err_report", err=ux.err_text(e)))
        await _heal_if_stuck_global(m, acct)  # само-восстановление залипшего аккаунта
        return
    t = report.totals
    if not (t.impressions or t.clicks or t.cost_micros):  # 2.7: не «стена нулей», а внятный ответ
        hint = _live_account_hint(acct)
        await m.answer(
            i18n.t("report_empty_state") + (("\n\n" + hint) if hint else ""),
            parse_mode=ParseMode.HTML,
        )
        return
    body = summary_text(report) + _scope_note(campaign_name)
    # Строка «здоровья» для отчёта ПО АККАУНТУ (не по одной кампании): движок аудита по УЖЕ собранному
    # отчёту — engine-only, без единого доп-чтения (крит-фикс C11). Косметика: сбой не роняет отчёт.
    if campaign_id is None:
        try:
            from audit.engine import build_audit
            from audit.render import audit_headline

            hl = audit_headline(build_audit(report), i18n.get_lang(m.chat.id))
            if hl:
                body = hl + "\n\n" + body
        except Exception:  # noqa: BLE001 — health-строка необязательна
            pass
    await m.answer(body)


async def _run_export(
    m: Message, period, acct: str, campaign_id: str | None = None, campaign_name: str | None = None
) -> None:
    """Построить .xlsx-отчёт за период (аккаунт или одна кампания) и прислать вложением. Read-only."""
    import os
    import tempfile

    await m.answer(i18n.t("report_preparing_xlsx"))
    path: str | None = None
    try:
        from ads.client import build_client_async
        from reports.service import build_account_report_async
        from reports.xlsx import write_report_xlsx

        client = await build_client_async(acct)  # холодная сборка — вне loop
        async with ux.upload_action(m):  # «отправляет документ…» пока строим .xlsx
            report = await build_account_report_async(
                client, acct, period, campaign_id=campaign_id, account_name=_account_name(acct)
            )
            report.currency = await _read_currency(client, acct)  # §9: валюта денежных метрик
            fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_report_")
            os.close(fd)
            await asyncio.to_thread(write_report_xlsx, report, path)
        scope = f"_{campaign_id}" if campaign_id else ""
        fname = f"aimash_{acct}{scope}_{period.date_from}_{period.date_to}.xlsx"
        await m.answer_document(FSInputFile(path, filename=fname))
    except Exception as e:  # сеть/доступ/SDK/openpyxl
        # A4: аккаунт деактивирован/нет прав → честная причина (не общее «не удалось сформировать»)
        await _capture_cmd_error(
            e, "cmd:report_xlsx"
        )  # A2: в /diag + алерт (access-ошибки пропустит)
        key = "err_account_inactive" if is_account_access_error(e) else "err_report_make"
        await m.answer(i18n.t(key, err=ux.err_text(e)))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def _run_sheets(
    m: Message, period, acct: str, campaign_id: str | None = None, campaign_name: str | None = None
) -> None:
    """Выгрузить отчёт за период (аккаунт или одна кампания) в Google Sheets, прислать ссылку. Read-only."""
    await m.answer(i18n.t("report_preparing_sheets"))
    try:
        from ads.client import build_client_async
        from reports.service import build_account_report_async
        from reports.sheets import publish_report_to_sheets

        client = await build_client_async(acct)  # холодная сборка — вне loop
        async with ux.typing_action(m):
            report = await build_account_report_async(
                client, acct, period, campaign_id=campaign_id, account_name=_account_name(acct)
            )
            report.currency = await _read_currency(client, acct)  # §9: валюта денежных метрик
            url, shared = await asyncio.to_thread(publish_report_to_sheets, report)
    except Exception as e:  # сеть/доступ/SDK/нет OAuth-scope Sheets
        # A4: если корень — деактивированный/недоступный аккаунт (ошибка Ads, НЕ Sheets-scope),
        # не показываем сбивающую подсказку про drive.file — даём честную причину.
        await _capture_cmd_error(e, "cmd:sheets")  # A2: в /diag + алерт (access-ошибки пропустит)
        key = "err_account_inactive" if is_account_access_error(e) else "err_sheets"
        await m.answer(i18n.t(key, err=ux.err_text(e)))
        return
    note = "" if shared else "\n" + i18n.t("sheets_share_failed_note")
    await m.answer(i18n.t("sheets_ready", url=url) + note)


def _mcc_period_factory(arg: str | None):
    """§8: фабрика Period в таймзоне дочернего аккаунта — из ТОГО ЖЕ пресета, что запросил оператор
    (7/30/90/MTD), но с локальным «сегодня». Для произвольных ISO-дат TZ-нормализация не применяется
    (абсолютные даты) → None (build_mcc_summary_async откатится на общий period)."""
    from reports.period import from_preset

    s = (arg or "30").strip()

    def factory(tz_name: str):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        try:
            today = datetime.now(ZoneInfo(tz_name)).date()
        except Exception:  # noqa: BLE001 — неизвестная TZ → host-дата (from_preset today=None)
            today = None
        try:
            return from_preset(s, today=today)
        except ValueError:  # произвольный диапазон / не пресет → без TZ-нормализации
            return None

    return factory


# P1-8: NL «экспорт статистики всех аккаунтов [за N дней]» → детерминированно роутим в /mcc (полный
# xlsx по всем дочерним). get_stats одно-аккаунтный, поэтому агент раньше отдавал только один аккаунт.
_EXPORT_ALL_RE = re.compile(
    r"(?=.*(?:всех?\s+аккаунт|по\s+всем\s+аккаунт|all\s+accounts|каждому\s+аккаунт|"
    r"всем\s+акк|все\s+акк|все[хй]\s+кампани|all\s+campaigns))"
    r"(?=.*(?:экспорт|эскпорт|выгруз|статистик|отч[её]т|report|export|сводк|данны|stats))",
    re.IGNORECASE | re.DOTALL,
)
_PERIOD_DAYS_RE = re.compile(
    r"(?:за|for|last|последн\w*)\s+(\d{1,3})\s*(?:дн\w*|day|день)|(\d{1,3})\s*(?:дн\w*|day)",
    re.IGNORECASE,
)


def is_export_all_accounts(text: str) -> tuple[bool, str | None]:
    """NL «экспорт статистики всех аккаунтов [за N дней]» → (True, период-arg для _send_mcc).
    Детерминированный роутинг в сводный MCC-экспорт (одно-аккаунтный get_stats это не покрывал)."""
    t = text or ""
    if not _EXPORT_ALL_RE.search(t):
        return (False, None)
    md = _PERIOD_DAYS_RE.search(t)
    days = (md.group(1) or md.group(2)) if md else None
    return (True, days)


async def _send_mcc(m: Message, arg: str | None) -> None:
    """§8: сводный отчёт по ВСЕМ дочерним аккаунтам ВСЕХ настроенных MCC (2F: раньше — только
    основной login_customer_id; вторичные из GOOGLE_ADS_LOGIN_CUSTOMER_IDS не попадали в /mcc,
    хотя scheduler и пикеры их видели). Подытоги по валютам без FX; окно каждого дочернего — в
    его таймзоне. READ-only. Сбой одного MCC → предупреждение в дайджесте, остальные живут
    (зеркало discover_read_children)."""
    try:
        period = _period_from_arg(arg)
    except ValueError:
        await m.answer(i18n.t("err_period"))
        return
    managers = sorted(settings.login_customer_id_set)
    if not managers:
        await m.answer(i18n.t("mcc_no_manager"))
        return
    await m.answer(i18n.t("mcc_preparing"))
    from ads.client import build_client_async
    from ads.read import account_timezone
    from reports.mcc import build_mcc_summary_async
    from reports.service import summary_text_mcc

    parts: list[str] = []
    summaries: list = []  # P1-8: копим сводки для xlsx-выгрузки по всем аккаунтам
    async with ux.typing_action(m):
        for manager_id in managers:
            try:
                client = await build_client_async(manager_id)  # холодная сборка — вне loop
                summary = await build_mcc_summary_async(
                    client,
                    manager_id,
                    period,
                    tz_of=account_timezone,
                    period_for=_mcc_period_factory(arg),
                )
                parts.append(summary_text_mcc(summary))
                summaries.append((manager_id, summary))
            except Exception as e:  # сеть/доступ/SDK — один MCC не валит остальные
                await capture_exception(e, where=f"mcc:{manager_id}")
                parts.append(i18n.t("mcc_manager_failed", mid=texts.esc(manager_id)))
    if not parts:
        await m.answer(i18n.t("err_mcc", err=""))
        return
    # HTML + деление по строкам: у большого MCC сводка длиннее лимита Telegram (полная — в /export).
    await ux.send_html_chunks(m, "\n\n———\n\n".join(parts))
    # P1-8: полная таблица по ВСЕМ дочерним аккаунтам — .xlsx-вложением (раньше был только текст;
    # build_mcc_workbook/write_mcc_xlsx существовали, но не вызывались ни из одной команды).
    await _send_mcc_xlsx(m, summaries, period)


async def _send_mcc_xlsx(m: Message, summaries: list, period) -> None:
    """P1-8: приложить .xlsx по каждой MCC-сводке (лист «Аккаунты» — по строке на дочерний). Сбой
    выгрузки не роняет уже отправленный текстовый дайджест (best-effort)."""
    import os
    import tempfile

    if not summaries:
        return
    from reports.xlsx import write_mcc_xlsx

    async with ux.upload_action(m):
        for manager_id, summary in summaries:
            path: str | None = None
            try:
                fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_mcc_")
                os.close(fd)
                await asyncio.to_thread(write_mcc_xlsx, summary, path)
                fname = f"aimash_mcc_{manager_id}_{period.date_from}_{period.date_to}.xlsx"
                await m.answer_document(FSInputFile(path, filename=fname))
            except Exception as e:  # noqa: BLE001 — xlsx best-effort, текст уже ушёл
                log.warning("mcc xlsx не сформирован mid=%s: %s", manager_id, type(e).__name__)
            finally:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


async def _mutready_check(cid: str) -> dict:
    """2.5: чек-лист готовности аккаунта к ВКЛЮЧЕНИЮ МУТАЦИЙ (диагностика для /mutready).
    READ-ONLY и только membership-проверки: НИЧЕГО не меняет — ни settings, ни env, ни
    oauth-рантайм; ensure_allowed «на пропуск» не вызывается (инвариант test_mutready).
    Финальный шаг (GOOGLE_ADS_ALLOWED_CUSTOMER_IDS) всегда за владельцем, руками."""
    from ads.client import (
        allowed_ceiling,
        discovered_inactive_children_meta,
        discovered_read_children,
        discovered_read_children_meta,
        has_oauth_runtime,
    )
    from core import twofa
    from core.access import list_account_operators

    ncid = normalize_customer_id(cid)
    meta = discovered_read_children_meta().get(ncid)
    imeta = discovered_inactive_children_meta().get(ncid)
    ch = meta or imeta
    r: dict = {
        "cid": ncid,
        "name": (getattr(ch, "name", "") or "") if ch is not None else "",
        "status": ((getattr(ch, "status", "") or "") if ch is not None else "")
        or ("ENABLED" if ncid == DRAFT_ACCOUNT_ID else "?"),
        "visible": ncid in allowed_ceiling(),
        "enabled": bool(meta) or ncid == DRAFT_ACCOUNT_ID,
        "oauth_runtime": has_oauth_runtime(ncid),
    }
    # OAuth-покрытие: per-account токен ИЛИ дочерний настроенного MCC (env-токен MCC покрывает).
    r["oauth"] = bool(
        r["oauth_runtime"] or ncid in discovered_read_children() or ncid == DRAFT_ACCOUNT_ID
    )
    r["probe"], r["probe_error"] = False, ""
    try:  # живой probe: 1 лёгкий GAQL (валюта) — честный ❌ с причиной, если чтение не работает
        from ads.client import build_client_async
        from ads.read import account_currency

        client = await build_client_async(ncid)
        r["currency"] = await run_ads_read_call(
            account_currency, client, ncid, label="mutready_probe"
        )
        r["probe"] = True
    except Exception as e:  # noqa: BLE001 — диагностика, не падаем
        r["probe_error"] = ux.err_text(e)
    try:
        r["operators"] = await list_account_operators(ncid)
    except Exception:  # noqa: BLE001
        r["operators"] = []
    r["twofa"] = twofa.is_ready()
    # AD.1: мутации включены для аккаунта ⇔ он ВИДИМ и (сентинел «all» ИЛИ он в явном списке) —
    # зеркалим логику ensure_allowed БЕЗ её вызова (инвариант test_mutready: команда «на пропуск»
    # ensure_allowed не зовёт). allow_all_visible — прод-дефолт (решение владельца 2026-07).
    r["all_visible"] = settings.allow_all_visible
    r["mutations_enabled"] = r["visible"] and (
        settings.allow_all_visible
        or ncid in {normalize_customer_id(x) for x in settings.allowed_customer_ids}
    )
    return r


async def _mutready_all() -> list[dict]:
    """AD.5: чек-лист готовности по ВСЕМ видимым аккаунтам (потолок мутаций allowed_ceiling()).
    READ-ONLY (каждый _mutready_check — только membership + лёгкий probe, ничего не меняет). Для
    /mutready all — сводка перед включением мутаций на всё видимое (Draft — первым)."""
    from ads.client import allowed_ceiling

    ids = sorted(allowed_ceiling(), key=lambda x: (x != DRAFT_ACCOUNT_ID, x))
    return [await _mutready_check(c) for c in ids]


async def _run_mcc_deep_export(m: Message, period, arg_code: str | None) -> None:
    """2.2: ГЛУБОКИЙ .xlsx по ВСЕМ дочерним всех настроенных MCC — лист на аккаунт (итоги+
    сравнение+кампании), «Сводка» и «Пропущено и ошибки». READ-ONLY. ~7 акк. × ~10 GAQL —
    прогресс-сообщение + общий wait_for(300с) на MCC; сбой одного MCC не валит остальные."""
    import os
    import tempfile

    managers = sorted(settings.login_customer_id_set)
    if not managers:
        await m.answer(i18n.t("mcc_no_manager"))
        return
    await m.answer(i18n.t("mcc_deep_preparing"), parse_mode=ParseMode.HTML)
    from ads.client import build_client_async
    from ads.read import account_timezone
    from reports.mcc import build_mcc_deep_async
    from reports.xlsx import write_mcc_deep_xlsx

    sent_any = False
    async with ux.upload_action(m):
        for manager_id in managers:
            path: str | None = None
            try:
                client = await build_client_async(manager_id)
                deep = await asyncio.wait_for(
                    build_mcc_deep_async(
                        client,
                        manager_id,
                        period,
                        tz_of=account_timezone,
                        period_for=_mcc_period_factory(arg_code),
                    ),
                    timeout=300,
                )
                if not deep.items and not (deep.inactive or deep.skipped or deep.errors):
                    continue  # пустой MCC — нет смысла слать пустую книгу
                fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_mcc_deep_")
                os.close(fd)
                await asyncio.to_thread(write_mcc_deep_xlsx, deep, path)
                fname = f"aimash_mcc_deep_{manager_id}_{period.date_from}_{period.date_to}.xlsx"
                await m.answer_document(FSInputFile(path, filename=fname))
                sent_any = True
            except Exception as e:  # noqa: BLE001 — один MCC не валит остальные
                await _capture_cmd_error(e, f"mcc_deep:{manager_id}")
                await m.answer(
                    i18n.t("mcc_deep_failed", mid=texts.esc(manager_id), err=ux.err_text(e))
                )
            finally:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
    if not sent_any:
        await m.answer(i18n.t("mcc_deep_empty"))


# ── RSA-генерация (фаза 2.C): /rsa визард → курация → confirm-гейт create_rsa ─────
def _rsa_is_wizard(session) -> bool:
    """Сессия курации принадлежит визарду §19 (brief.cc_session)? Тогда клавиатуры получают
    батч-ряд §19.5.2 («Доработать всё | Сгенерировать заново | Утвердить набор»); /rsa-флоу
    без cc_session рендерится как раньше."""
    return bool((session.brief or {}).get("cc_session"))


def _rsa_render(session) -> tuple[str, InlineKeyboardMarkup]:
    """По состоянию сессии: следующий pending-элемент с кнопками либо итоговый экран."""
    wizard = _rsa_is_wizard(session)
    nxt = session.next_pending()
    if nxt is None:
        h, d = session.counts()
        text = texts.fmt_rsa_overview(h, d, len(session.headlines), len(session.descriptions))
        return text, rsa_overview_kb(
            session.session_id, session.can_finalize(), has_pending=False, wizard=wizard
        )
    kind, idx = nxt
    items = session.items(kind)
    text = texts.fmt_rsa_element(
        kind, idx, len(items), items[idx], session.campaign, session.ad_group_name
    )
    return text, rsa_item_kb(session.session_id, kind, idx, wizard=wizard)


def _rsa_overview(session) -> tuple[str, InlineKeyboardMarkup]:
    h, d = session.counts()
    has_pending = session.next_pending() is not None
    text = texts.fmt_rsa_overview(h, d, len(session.headlines), len(session.descriptions))
    return text, rsa_overview_kb(
        session.session_id, session.can_finalize(), has_pending, wizard=_rsa_is_wizard(session)
    )


async def _rsa_present_list(target: Message, session, state: FSMContext) -> None:
    """§10 list-UX (ДЕФОЛТ вместо кликанья по одному): показать ВЕСЬ набор заголовков/описаний
    редактируемым списком + кнопку «✅ Использовать как есть». Менеджер правит и присылает обратно —
    ловит rsa_list_edited (состояние RsaList.awaiting_edited). Confirm-гейт — в конце, без изменений."""
    await state.set_state(RsaList.awaiting_edited)
    await state.update_data(cid=session.session_id)
    await target.answer(
        i18n.t("rsa_list_prompt"),
        reply_markup=rsa_aslist_kb(session.session_id),
        parse_mode=ParseMode.HTML,
    )
    await target.answer(texts.fmt_rsa_list_block(session))  # плейн-текст — копируется как есть


async def _cc_rsa_present_curation(target: Message, session) -> None:
    """§19.5.2 (визард, Этап 3): показать сгенерированный набор ПОЭЛЕМЕНТНО — (1) весь список с
    длинами обзорным плейн-текстом, (2) карточка первого pending-элемента с ✅/✏️/❌ + батч-ряд
    «Доработать всё | Сгенерировать заново | Утвердить набор». Мутаций нет — только курация;
    утверждённые элементы уйдут в черновик визарда через _cc_finalize_ad."""
    await target.answer(texts.fmt_rsa_list_block(session))  # обзор всего набора (как в ТЗ §19.5.2)
    text, kb = _rsa_render(session)  # первый pending-элемент (или итог) — батч-ряд по wizard-флагу
    await target.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _rsa_present_final(target: Message, chat_id: int, session, state: FSMContext) -> None:
    """Общий хвост завершения курации (list-UX и «как есть»): курация визарда (cc_session) → в
    черновик кампании; иначе — минтуем черновик create_rsa за confirm-гейтом (мутация только после «да»)."""
    if session.brief.get("cc_session"):
        await _cc_finalize_ad(target, chat_id, session, state)
        return
    try:
        cid, op, params, summary = _build_rsa_proposal(session)
    except Exception as e:  # валидация схемы (мин/макс/длины/URL)
        await target.answer(await _friendly_error(e, "rsa:final"))
        return
    await _present_proposal(
        target,
        chat_id=chat_id,
        operation=op,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=getattr(
            session, "customer_id", None
        ),  # §8: create_rsa на аккаунте-якоре сессии
    )


def _parse_brief_text(text: str) -> tuple[str, str | None]:
    """Из «тематика | url» (или «тематика url») вытащить (topic, url)."""
    import re

    m = re.search(r"https?://\S+", text or "")
    url = m.group(0).rstrip(".,;)") if m else None
    topic = text or ""
    if "|" in topic:
        topic = topic.split("|", 1)[0]
    elif url:
        topic = topic.replace(url, "")
    return topic.strip(" |"), url


def _build_rsa_proposal(session) -> tuple[str, str, dict, str]:
    """Черновик create_rsa из одобренных элементов (валидация схемой). (cid, op, params, summary)."""
    headlines = session.approved_texts("h")
    descriptions = session.approved_texts("d")
    params = {
        "ad_group_id": session.ad_group_id,
        "campaign": session.campaign,
        "final_url": session.final_url,
        "headlines": headlines,
        "descriptions": descriptions,
    }
    validated = SCHEMAS["create_rsa"](**params)  # код-валидация мин/макс/длины/URL
    params = validated.model_dump()
    summary = texts.fmt_rsa_proposal_summary(
        session.ad_group_name, headlines, descriptions, session.final_url
    )
    p = Proposal(operation="create_rsa", summary=summary, params=params, chat_id=session.chat_id)
    return p.confirmation_id, "create_rsa", params, summary


async def _rsa_after_adgroup(target: Message, chat_id: int, state: FSMContext) -> None:
    """Группа известна: если есть topic+валидный url — генерим; иначе спрашиваем бриф."""
    data = await state.get_data()
    if (data.get("topic") or "").strip() and data.get("final_url"):
        await _rsa_generate_and_start(target, chat_id, state)
        return
    await state.set_state(RsaWizard.awaiting_brief)
    await target.answer(i18n.t("rsa_ask_brief"), reply_markup=nav_kb(), parse_mode=ParseMode.HTML)


async def _rsa_resolve_after_campaign(
    target: Message, chat_id: int, campaign: str, state: FSMContext
) -> None:
    """Кампания выбрана/задана: резолвим группы. 1 → дальше; >1 → выбор; 0 → ошибка."""
    from ads.client import build_client_async

    await state.update_data(campaign=campaign)
    try:
        acct = await _active_read_account(
            chat_id
        )  # §8: группы активного аккаунта (не хардкод Draft)
        client = await build_client_async(acct)
        groups = await run_ads_read_call(
            find_ad_groups, client, acct, campaign, label="find_ad_groups"
        )
    except Exception as e:  # сеть/доступ/SDK
        await target.answer(i18n.t("err_adgroups", err=ux.err_text(e)))
        return
    if not groups:
        await target.answer(i18n.t("rsa_no_adgroups"))
        await state.clear()
        return
    # B2: RSA валиден ТОЛЬКО в Search-стандартной группе. Не-Search (DSA/Display/Video/PMax) группы
    # отсеиваем ДО создания — иначе Google отвергал бы «operation not allowed for the given context».
    search_groups = [g for g in groups if g.accepts_rsa()]
    if not search_groups:
        await target.answer(i18n.t("rsa_not_search"))
        await state.clear()
        return
    groups = search_groups
    if len(groups) == 1:
        g = groups[0]
        await state.update_data(ad_group_id=str(g.id), ad_group_name=g.name)
        await _rsa_after_adgroup(target, chat_id, state)
        return
    _RSA_AG_CACHE[chat_id] = [{"id": str(g.id), "name": g.name} for g in groups]
    # N5: пикер группы — кнопочный экран; ставим state, чтобы текст/URL тут не утекал в агента (гард on_text).
    await state.set_state(RsaWizard.picking)
    await target.answer(
        i18n.t("rsa_pick_adgroup"), reply_markup=rsa_pick_adgroups_kb(_RSA_AG_CACHE[chat_id])
    )


async def _rsa_generate_and_start(target: Message, chat_id: int, state: FSMContext) -> None:
    """Сгенерировать тексты по брифу из state, создать сессию курации, показать итог."""
    data = await state.get_data()
    acct = await _active_read_account(
        chat_id
    )  # §8: RSA на активном аккаунте (сессия читает+мутирует его)
    # §20→§10: профиль клиента активного аккаунта как контекст генерации (не хардкод Draft).
    _prof = await _cc_profile_ctx_account(acct)
    brief = CopyBrief(
        topic=data.get("topic", ""),
        keywords=list(data.get("keywords") or []),
        usp=data.get("usp"),
        profile=(_prof or None),
        tone=data.get("tone"),
        geo=data.get("geo"),
        language=data.get("language", "ru"),
        n_headlines=int(data.get("n_headlines") or 15),
        n_descriptions=int(data.get("n_descriptions") or 4),
    )
    await target.answer(i18n.t("rsa_generating"))
    try:
        async with ux.typing_action(target):
            draft = await _generate_rsa(brief)
    except Exception as e:  # LLM/сеть
        await target.answer(i18n.t("err_gen", err=ux.err_text(e)))
        await state.clear()
        return
    # Диагностика: сколько набрано/отброшено КОДОМ за длину (golden rule #4) — раньше терялось.
    await target.answer(ux.fmt_rsa_diagnostics(draft, brief.n_headlines, brief.n_descriptions))
    if len(draft.headlines) < RSA_MIN_HEADLINES or len(draft.descriptions) < RSA_MIN_DESCRIPTIONS:
        await target.answer(i18n.t("rsa_gen_empty"))
        await state.clear()
        return
    session_id = await SESSIONS.create(
        chat_id=chat_id,
        customer_id=acct,  # §8: аккаунт-якорь RSA-сессии (create_rsa минтится на него)
        campaign=data.get("campaign", ""),
        ad_group_id=data.get("ad_group_id", ""),
        ad_group_name=data.get("ad_group_name", ""),
        final_url=data.get("final_url", ""),
        headlines=draft.headlines,
        descriptions=draft.descriptions,
        brief=brief.model_dump(),
    )
    await state.clear()
    session = await SESSIONS.get(session_id)
    await _rsa_present_list(
        target, session, state
    )  # §10 list-UX: правка списком, без кликов по одному


async def _rsa_start_from_intent(m: Message, brief: dict, state: FSMContext) -> None:
    """NL-вход: generate_rsa-намерение агента. Доуточняем кампанию/группу/URL визардом."""
    await state.clear()
    await state.update_data(
        topic=brief.get("topic", ""),
        keywords=list(brief.get("keywords") or []),
        usp=brief.get("usp"),
        tone=brief.get("tone"),
        geo=brief.get("geo"),
        language=brief.get("language", "ru"),
        n_headlines=brief.get("n_headlines", 15),
        n_descriptions=brief.get("n_descriptions", 4),
    )
    if brief.get("final_url"):
        await state.update_data(final_url=brief["final_url"])
    if brief.get("campaign"):
        await _rsa_resolve_after_campaign(m, m.chat.id, brief["campaign"], state)
        return
    try:  # кампания не указана — показать выбор (бриф уже в state)
        from ads.client import build_client_async
        from ads.read import list_campaigns

        acct = await _active_read_account(
            m.chat.id
        )  # §8: кампании АКТИВНОГО аккаунта, не хардкод Draft
        client = await build_client_async(acct)
        # как остальной read-слой: таймаут+ретрай транзиентных под семафором Google Ads
        # (а не «голый» to_thread — иначе зависший SearchStream не капается и копит in-flight).
        camps = await run_ads_read_call(list_campaigns, client, acct, label="list_campaigns")
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(i18n.t("err_campaigns", err=ux.err_text(e)))
        return
    if not camps:
        await m.answer(i18n.t("no_campaigns"))
        return
    _RSA_CAMP_CACHE[m.chat.id] = camps
    await m.answer(
        i18n.t("rsa_pick_campaign"),
        reply_markup=rsa_pick_campaigns_kb(camps),
        parse_mode=ParseMode.HTML,
    )


# ── Keyword research (Фаза 3, БЛОК E): подбор + кластеризация + .xlsx ─────────────
def _parse_kw_input(text: str) -> tuple[list[str], str | None]:
    """Ввод → (сиды, url). URL извлекаем ПОДСТРОКОЙ (regex до первого пробела), остаток — сиды через
    запятую/перенос (многословный сид, напр. «интернет магазин», остаётся ОДНИМ сидом).

    Баг, который это чинит: раньше сплит шёл только по запятой/переносу, поэтому ввод «URL слова»
    без запятой (напр. `https://rozetka.com.ua интернет магазин`) целиком попадал в url → Google Ads
    отвечал `URL is malformed`. Теперь url = `https://rozetka.com.ua`, сиды = [`интернет магазин`]."""
    raw = text or ""
    m = re.search(r"https?://\S+", raw)  # URL до первого пробела (образец: _parse_brief_text)
    url = m.group(0).rstrip(".,;)") if m else None
    rest = raw.replace(m.group(0), " ", 1) if m else raw  # вырезаем URL, чтобы не попал в сиды
    seeds = [p.strip() for p in rest.replace("\n", ",").split(",") if p.strip()]
    return seeds, url


def _parse_geo_locations(text: str) -> list[str]:
    """Ввод → список локаций (страна/город/регион): токены через запятую/перенос, без пустых.
    Диапазоны/длину валидирует схема SetGeoLocation (макс. 20, ≤80 симв.) при сборке черновика."""
    return [p.strip() for p in (text or "").replace("\n", ",").split(",") if p.strip()]


def _parse_geo_proximity(text: str) -> tuple[str, float] | None:
    """Ввод «город, радиус_км» → (city, radius_km) или None при нераспознанном формате. Разделитель —
    ПОСЛЕДНЯЯ запятая/'|' (название города может содержать пробелы; десятичный радиус — через точку).
    Границы радиуса (0, 2000] и длину города валидирует схема SetGeoProximity (после сборки)."""
    raw = (text or "").strip()
    sep = max(raw.rfind(","), raw.rfind("|"))
    if sep < 0:
        return None
    city = raw[:sep].strip()
    rad_s = raw[sep + 1 :].strip()
    if not city or not rad_s:
        return None
    try:
        radius = float(rad_s)
    except ValueError:
        return None
    return city, radius


async def _kw_run(
    target: Message,
    chat_id: int,
    seeds: list[str],
    url: str | None,
    language: str,
    *,
    geo_ids: tuple[int, ...] | None = None,
    network: str = "GOOGLE_SEARCH",
    months: int | None = None,
) -> None:
    """Подобрать идеи → кластеризовать по интенту → сводка + .xlsx. READ-ONLY (advisory).
    3F (§7): geo_ids/network/months — параметры research (раньше зашиты); None/дефолт =
    прежнее поведение (Украина / Search / окно API)."""
    import os
    import tempfile

    # K: РФ/РБ не обслуживаются Keyword Planner — честно сообщаем ДО запроса (сам запрос всё равно
    # выкинет их, generate_keyword_ideas страхует), чтобы менеджер понимал, почему без гео.
    from ads.geo import has_non_serviceable_geo

    if geo_ids and has_non_serviceable_geo(geo_ids):
        await target.answer(i18n.t("kw_geo_dropped"))
    await target.answer(i18n.t("kw_searching"))
    # §8: идеи берём на АКТИВНОМ read-аккаунте (на боевом Keyword Planner даёт реальные метрики; на
    # Draft — нули). Замок чтения держит generate_keyword_ideas (ensure_read_allowed); если активный
    # аккаунт вышел из read-list, _active_read_account сам откатывается на Draft (fail-closed).
    from ads.client import build_client_async
    from ads.keyword_plan import DEFAULT_GEO_IDS, generate_keyword_ideas

    eff_geo = tuple(geo_ids) if geo_ids else DEFAULT_GEO_IDS

    async def _gen(cid: str):
        client = await build_client_async(cid)  # холодная сборка — вне loop
        return await asyncio.to_thread(
            generate_keyword_ideas,
            client,
            cid,
            seeds=seeds,
            url=url,
            language=language,
            geo_ids=eff_geo,
            network=network,
            months=months,
        )

    # P1-7: метрики берём с ЖИВОГО аккаунта (тест/Draft отдаёт нули → мало слов и мёртвая сортировка).
    acct, is_live = await _keyword_metrics_account(chat_id)
    if not is_live:
        await target.answer(i18n.t("kw_metrics_test_note"))
    try:
        ideas = await _gen(acct)
    except Exception as e:  # сеть/доступ/SDK/валидация ввода
        # РЕЗИЛЬЕНТНОСТЬ: если активный (не Draft) аккаунт недоступен для Keyword Planner (напр.
        # «customer not enabled» — он не под настроенным MCC/деактивирован), НЕ роняем подбор, а
        # честно откатываемся на Draft, чтобы менеджер всё равно получил идеи. Ошибку показываем
        # только если и Draft не сработал.
        if acct != DRAFT_ACCOUNT_ID:
            await target.answer(
                i18n.t("kw_acct_fallback", acct=texts.esc(acct)), parse_mode=ParseMode.HTML
            )
            try:  # сбрасываем залипший глобальный выбор, чтобы дальше не долбить недоступный аккаунт
                await _save_selected_account(chat_id, None)
            except Exception:  # noqa: BLE001 — сброс best-effort
                pass
            acct = DRAFT_ACCOUNT_ID
            try:
                ideas = await _gen(acct)
            except Exception as e2:
                await target.answer(i18n.t("err_kw", err=ux.err_text(e2)))
                return
        else:
            await target.answer(i18n.t("err_kw", err=ux.err_text(e)))
            return
    if not ideas:
        await target.answer(i18n.t("kw_empty"))
        return

    from keywords.cluster import (
        Cluster,
        cluster_keywords,
        rank_clusters,
        suggest_negative_keywords,
    )
    from keywords.filter import filter_relevance

    idea_texts = [i.text for i in ideas]
    src = ", ".join(seeds) or (url or "")
    # §7: кластеризация по интенту, предложение минус-слов и AI-релевантность (§19.4.2) независимы →
    # параллельно (без наценки латентности к 2 уже идущим). Все advisory с внутренним fallback;
    # return_exceptions страхует от пробрасывания (фича не падает). topic = сиды/URL пользователя.
    clusters_res, negatives, relevance = await asyncio.gather(
        cluster_keywords(idea_texts, language),
        suggest_negative_keywords(src, idea_texts, language=language),
        filter_relevance(texts=idea_texts, topic=src, language=language),
        return_exceptions=True,
    )
    clusters = (
        clusters_res
        if isinstance(clusters_res, list) and clusters_res
        else [Cluster(name="Все ключи", keywords=idea_texts)]
    )
    if not isinstance(negatives, list):
        negatives = []
    if not isinstance(relevance, dict):  # fail-open: сбой релевантности → всё релевантно
        relevance = {}

    by_text = {i.text: i.avg_monthly_searches for i in ideas}
    by_idea = {i.text: i for i in ideas}  # §7: конкуренция/ставки/сезон для чат-таблицы
    # §19.4.2: нерелевантные идеи (модель fail-open ⇒ отсутствующее = релевантно) НЕ теряем из
    # таблицы/.xlsx (менеджер видит всё), но исключаем из набора «добавить в кампанию».
    off_topic = {t for t in idea_texts if relevance.get(t, True) is False}
    clusters = rank_clusters(
        clusters, by_text
    )  # §7: приоритезация (объём × интент) — порядок показа
    currency = await _read_currency(await build_client_async(acct), acct)  # §9: валюта аккаунта
    summary = texts.fmt_keywords_summary(
        clusters,
        by_text,
        len(ideas),
        src,
        by_idea=by_idea,
        currency=currency,
        irrelevant=len(off_topic),
    )
    if (
        negatives
    ):  # §7 «предложение минус-слов» (advisory; добавление — отдельной командой за гейтом)
        shown = ", ".join(texts.esc(x) for x in negatives[:15])
        more = i18n.t("list_more", n=len(negatives) - 15) if len(negatives) > 15 else ""
        summary += i18n.t("kw_negatives_advisory", shown=shown, more=more)
    # §7 (P3, фидбэк заказчика 2026-07-06): кнопку «➕ Добавить ключи в кампанию» из отчёта
    # УБРАЛИ — сгенерированный список редко идёт в кампанию как есть. Добавление ключей (своих
    # файлом/ссылкой/текстом или из этого отчёта) — отдельный вход /addkeys и меню «➕ Ещё».
    summary += "\n" + i18n.t("kw_addkeys_hint")
    await target.answer(summary, parse_mode=ParseMode.HTML)

    path: str | None = None
    try:
        from keywords.export import write_keywords_xlsx

        fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_keywords_")
        os.close(fd)
        await asyncio.to_thread(
            write_keywords_xlsx,
            clusters,
            ideas,
            path,
            seeds=seeds,
            url=url,
            language=language,
            negatives=negatives,
            currency=currency,  # D8: валюта аккаунта в колонках ставок
        )
        await target.answer_document(FSInputFile(path, filename="aimash_keywords.xlsx"))
    except Exception as e:  # openpyxl/IO
        await target.answer(i18n.t("err_kw_xlsx", err=ux.err_text(e)))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    # §7 «CSV/таблица» / §19.4.2 «выгрузка идей — .CSV»: тот же набор плоским CSV (utf-8-sig,
    # Excel-совместимый). Раньше write_keywords_csv был недостижим из Telegram (аудит M4.3).
    csv_path: str | None = None
    try:
        from keywords.export import write_keywords_csv

        fd, csv_path = tempfile.mkstemp(suffix=".csv", prefix="aimash_keywords_")
        os.close(fd)
        await asyncio.to_thread(
            write_keywords_csv,
            clusters,
            ideas,
            csv_path,
            seeds=seeds,
            url=url,
            language=language,
            currency=currency,  # D8: валюта аккаунта в колонках ставок
        )
        await target.answer_document(FSInputFile(csv_path, filename="aimash_keywords.csv"))
    except Exception:  # noqa: BLE001 — CSV — дубль данных .xlsx; сбой не критичен, не спамим
        log.warning("keywords: CSV-экспорт не удался", exc_info=True)
    finally:
        if csv_path and os.path.exists(csv_path):
            try:
                os.remove(csv_path)
            except OSError:
                pass


async def _kw_start_from_intent(m: Message, brief: dict, state: FSMContext) -> None:
    """NL-вход: keyword_research-намерение агента. Есть сиды/URL — сразу; иначе спросить.
    3F: дефолт языка — язык интерфейса (keyword_ideas_lang), а не хардкод 'ru' (EN-пользователь
    получал русские идеи, нерелевантные его рынку)."""
    from ads.geo import (
        country_iso,
        geo_id_for_country,
        keyword_ideas_lang,
        language_for_country,
    )

    await state.clear()
    seeds = [s for s in (brief.get("seeds") or []) if s]
    url = brief.get("url")
    # P1-F (§7): гео/сеть/период из NL-брифа → те же параметры research, что и экран /keywords.
    geo_ids: tuple[int, ...] | None = None
    geo_iso: str | None = None
    geo_raw = (brief.get("geo") or "").strip()
    if geo_raw:
        geo_iso = country_iso(geo_raw) or geo_raw.upper()
        gid = geo_id_for_country(geo_iso)
        geo_ids = (gid,) if gid else None  # неизвестная страна → дефолт (_kw_run подставит Украину)
    # §7: язык — из брифа; иначе ВЫВОДИМ ИЗ СТРАНЫ (Германия→de, а не язык интерфейса — иначе идеи на
    # чужом рынку языке → «мало слов»); иначе язык интерфейса. keyword_ideas_lang нормализует/опускает.
    language = keyword_ideas_lang(
        brief.get("language")
        or (language_for_country(geo_iso) if geo_iso else None)
        or i18n.current_lang()
    )
    network = (
        "GOOGLE_SEARCH_AND_PARTNERS"
        if brief.get("network") == "search_partners"
        else "GOOGLE_SEARCH"
    )
    months = brief.get("months")
    if seeds or url:
        await _kw_run(
            m, m.chat.id, seeds, url, language, geo_ids=geo_ids, network=network, months=months
        )
        return
    await state.set_state(KwWizard.awaiting_seeds)
    await m.answer(i18n.t("kw_ask"), reply_markup=nav_kb(), parse_mode=ParseMode.HTML)


# ── §3: гео-таргетинг кампании из меню (локации/радиус → текст → черновик set_geo_* → «да») ──
async def _geo_nav_kb(state: FSMContext):
    """nav_kb для retry-шага гео: «‹ Назад» → меню кампании (idx из state-data geo_idx,
    положен в on_geo_mode), иначе только «✖ Отмена». Держит кнопки и после невалидного ввода."""
    idx = (await state.get_data()).get("geo_idx", -1)
    back = CampCB(action="menu", idx=idx) if isinstance(idx, int) and idx >= 0 else None
    return nav_kb(back)


# ── §3 Создание поисковой (Search) кампании: /newsearch → бриф → RSA → черновик ─────
def _parse_search_brief(text: str) -> tuple[str, str, float, str, list[str]] | None:
    """«Название | url | бюджет [| тематика [| ключи через запятую]]» →
    (name, url, budget_units, topic, keywords). None при неверном формате (golden rule #4 —
    границы бюджета считает КОД до генерации, а не Pydantic после)."""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) < 3:
        return None
    name, url, budget_s = parts[0], parts[1], parts[2]
    if not name or not url.startswith(("http://", "https://")):
        return None
    try:
        budget = float(budget_s.replace(",", "."))
    except ValueError:
        return None
    if budget <= 0 or budget > MONEY_MAX_UNITS:  # потолок из core.limits — отвергаем сразу
        return None
    topic = parts[3] if len(parts) >= 4 and parts[3] else name
    kw_raw = parts[4] if len(parts) >= 5 else ""
    keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
    return (name, url, budget, topic, keywords)


# ── §19: guided-визард «Создание кампании» (Этапы 0–1; 2–7 — в следующих фазах) ─────
def _cc_draft_account_row():
    """Фолбэк-«аккаунт» для Этапа 0, когда MCC недоступен: только Draft (мутации всё равно лишь
    на нём). Объект совместим с cc_accounts_kb (есть .id/.name/.manager)."""
    from ads.read import ChildAccount

    return ChildAccount(
        id=DRAFT_ACCOUNT_ID,
        name="Aimash (Draft)",
        currency="",
        manager=False,
        level=0,
        status="ENABLED",
    )


async def _cc_account_currency(customer_id: str | None) -> str:
    """Валюта выбранного на Этапе-0 аккаунта для валютных дефолтов §19 (0.50 JPY-класс бага).
    Сначала мета обхода MCC (ChildAccount.currency — 0 лишних API-вызовов), затем GAQL
    account_currency (свой кэш). Любой сбой/запрет чтения → '' (дефолты без валюты, не падаем)."""
    from ads.client import build_client_async, discovered_read_children_meta
    from ads.read import account_currency

    cid = normalize_customer_id(customer_id or DRAFT_ACCOUNT_ID)
    try:
        meta = discovered_read_children_meta().get(cid)
        cur = ((getattr(meta, "currency", "") or "") if meta else "").strip().upper()
        if cur:
            return cur
    except Exception:  # noqa: BLE001 — мета не критична
        pass
    try:
        client = await build_client_async(cid)
        cur = await run_ads_read_call(account_currency, client, cid, label="account_currency")
        return (cur or "").strip().upper()
    except Exception:  # noqa: BLE001 — валюта не критична (деградация на фолбэк-дефолты)
        return ""


async def _cc_read_medians(customer_id: str):
    """Медианы прошлых Search-кампаний аккаунта (§19.3 «по аналогии»). Любой сбой/запрет чтения →
    пустые медианы (визард деградирует на дефолты, не падает)."""
    from ads.client import build_client_async
    from ads.read import AccountMedians, search_campaign_medians

    try:
        client = await build_client_async(customer_id)
        return await run_ads_read_call(
            search_campaign_medians, client, customer_id, label="search_campaign_medians"
        )
    except Exception:  # noqa: BLE001 — read не разрешён/сбой → дефолты (см. fmt summary)
        return AccountMedians(None, None, None)


async def _cc_profile_ctx_account(customer_id: str) -> str:
    """§20→§19/§10: компактный контекст профиля клиента по аккаунту как вход генераторов
    (seed-ключи/релевантность/тексты/ассеты). Нет профиля → '' (прежнее поведение, аддитивно).
    Любой сбой чтения не роняет генерацию."""
    try:
        return await CLIENTS.profile_context_text(customer_id or DRAFT_ACCOUNT_ID)
    except Exception:  # noqa: BLE001 — контекст не критичен
        return ""


async def _cc_profile_ctx(draft) -> str:
    """Профиль клиента выбранного на Этапе-0 §19-аккаунта (preview_customer_id)."""
    return await _cc_profile_ctx_account(draft.preview_customer_id or DRAFT_ACCOUNT_ID)


async def _cc_profile_site(draft) -> str | None:
    """§19.4.2 (seed + URL): сайт клиента из §20-профиля выбранного аккаунта. Нет профиля/сайта или
    сбой чтения → None (генерация идёт только по seeds — прежнее поведение, аддитивно)."""
    try:
        prof = await CLIENTS.get_by_account(draft.preview_customer_id or DRAFT_ACCOUNT_ID)
        site = (prof or {}).get("website") or ""
        return site if site.startswith(("http://", "https://")) else None
    except Exception:  # noqa: BLE001 — сайт не критичен
        return None


def _cc_reseed_currency_defaults(settings_d: dict, currency: str) -> dict | None:
    """Ревью 2026-07-07 (смена аккаунта на Этапе 0 при собранных настройках): валюта и ДЕФОЛТНЫЕ
    деньги старого аккаунта не должны пережить переключение — сводка показывала бы «1 500 JPY
    (по умолчанию)» для USD-аккаунта. Пересеивает ТОЛЬКО поля с тегом by_default (bюджет/CPC —
    значения пользователя и «по аналогии» не трогаем) + метку currency. None ⇒ менять нечего
    (валюта неизвестна или совпадает)."""
    cur = (currency or "").strip().upper()
    if not cur or (settings_d.get("currency") or "").strip().upper() == cur:
        return None
    from core.limits import wizard_default_money_units

    s = dict(settings_d)
    s["currency"] = cur
    bd = set(s.get("by_default") or [])
    def_budget, def_cpc = wizard_default_money_units(cur)
    if "budget_daily_micros" in bd:
        s["budget_daily_micros"] = units_to_micros(def_budget)
    if "cpc_bid_micros" in bd:
        s["cpc_bid_micros"] = units_to_micros(def_cpc)
    return s


def _cc_max_step(draft) -> int:
    """W4: максимально достигнутый этап черновика (high-water nav.max_step из store.set_step;
    старые черновики без nav → current_step). Управляет показом «Вперёд ›» после «Назад»."""
    if draft is None:
        return 0
    nav = (draft.wizard_state or {}).get("nav") or {}
    return max(int(nav.get("max_step") or 0), int(draft.current_step or 0))


def _cc_apply_settings_patch(cur: dict, patch) -> dict:
    """Наложить пред-confirm правку («поставь бюджет 60») на собранные настройки. Изменённые поля
    выходят из ОБОИХ тегов источника (by_analogy И by_default — теперь заданы пользователем).
    match_type правкой текста не трогаем."""
    s = dict(cur)
    by = set(s.get("by_analogy") or [])
    bd = set(s.get("by_default") or [])
    if patch.budget_daily_units is not None:
        s["budget_daily_micros"] = units_to_micros(patch.budget_daily_units)
        by.discard("budget_daily_micros")
        bd.discard("budget_daily_micros")
    # «максимальная цена за клик 75» — раньше ветки НЕ было: CPC был нередактируем, правка
    # молча терялась (живой тест 2026-07-06). <=0 игнорируем (не даём занулить бид).
    if patch.max_cpc_units is not None and patch.max_cpc_units > 0:
        s["cpc_bid_micros"] = units_to_micros(patch.max_cpc_units)
        by.discard("cpc_bid_micros")
        bd.discard("cpc_bid_micros")
    if patch.geo_locations:
        s["geo_locations"] = list(patch.geo_locations)
    if patch.geo_country_code:
        s["geo_country_code"] = patch.geo_country_code
    if patch.languages:
        s["languages"] = list(patch.languages)
    if patch.campaign_name:
        s["campaign_name"] = patch.campaign_name
    if patch.currency:
        s["currency"] = patch.currency
    # P0-4: даты/сети/расписание правкой РАНЬШЕ терялись (веток не было — только язык/бюджет/гео).
    start = _valid_iso_date(patch.start_date)
    if start:
        s["start_date"] = start
    end = _valid_iso_date(patch.end_date)
    if end:
        s["end_date"] = end
    if s.get("start_date") and s.get("end_date") and s["end_date"] < s["start_date"]:
        s["end_date"] = None  # конец раньше старта — мусор, не несём в SDK
    if patch.networks:
        s["networks"] = patch.networks
        bd.discard("networks")
    if patch.ad_schedule:
        blocks = parse_ad_schedule(patch.ad_schedule)
        if blocks is not None:  # нераспознанное расписание не трогает текущее
            s["ad_schedule_blocks"] = blocks
            s["ad_schedule"] = schedule_human(blocks, patch.ad_schedule)
            bd.discard("ad_schedule")
    if patch.bidding_strategy or patch.goal:
        strat, tcpa, payment = derive_bidding(patch)
        s["bidding_strategy"] = strat
        if tcpa is not None:
            s["target_cpa_micros"] = tcpa
        if payment:
            s["payment_model"] = payment
        by.discard("bidding_strategy")
        bd.discard("bidding_strategy")
    elif patch.target_cpa_units is not None:
        s["target_cpa_micros"] = units_to_micros(patch.target_cpa_units)
    if patch.payment_model:
        s["payment_model"] = patch.payment_model
    s["by_analogy"] = sorted(by)
    s["by_default"] = sorted(bd)
    return s


async def _cc_present_stage0(target: Message, chat_id: int) -> None:
    """Этап 0: аккаунты, доступные ЭТОМУ оператору на чтение (2F: из discovered-meta ВСЕХ
    настроенных MCC + пер-юзер фильтр, как пикер /report — раньше живой обход только основного
    MCC: вторичные не попадали + лишний SDK-вызов на каждый вход). Сбой/пусто → деградация на
    единственный Draft, чтобы визард не падал (в этом фолбэке доступен только Draft; создание всё
    равно за confirm-гейтом)."""
    rows: list = []
    try:
        rows = [r for r in await _read_account_rows(chat_id) if not getattr(r, "manager", False)]
    except Exception as e:  # noqa: BLE001 — сбой перечисления → деградация на Draft
        await target.answer(i18n.t("cc_accounts_error", err=ux.err_text(e)))
    if not rows:
        rows = [_cc_draft_account_row()]
    _CC_ACCT_CACHE[chat_id] = rows
    try:  # B7: пикер постраничный, но на всякий — фолбэк, если Telegram отверг разметку (не падаем)
        await target.answer(
            i18n.t("cc_pick_account"), reply_markup=cc_accounts_kb(rows), parse_mode=ParseMode.HTML
        )
    except Exception as e:  # noqa: BLE001
        log.warning("cc: пикер аккаунтов не отрисован (%s)", type(e).__name__)
        await target.answer(await _friendly_error(e, "cc:accounts_picker"))


async def _cc_begin(target: Message, chat_id: int, state: FSMContext) -> None:
    """Создать свежий черновик (гасит прежние активные) и показать Этап 0. Перед сменой — чистим
    временные изображения прежнего активного черновика (иначе осиротеют при supersede)."""
    prev = await CDRAFTS.get_active(chat_id)
    if prev is not None:
        await asyncio.to_thread(
            clear_pending_media_ids, (prev.wizard_state.get("images") or {}).get("media_ids") or []
        )
    # §8: аккаунт МУТАЦИИ визарда = активный аккаунт чтения (дефолт Draft); preview_customer_id
    # (медианы «по аналогии») выбирается отдельно на Этапе 0. Замок — в _present_proposal на создании.
    session_id = await CDRAFTS.create(
        chat_id=chat_id, customer_id=await _active_read_account(chat_id)
    )
    await state.set_state(CreateCampaignWizard.account_select)
    await state.update_data(cc_session=session_id)
    await _cc_present_stage0(target, chat_id)


def _cc_crumb(step: int) -> str:
    """Хлебная крошка этапа визарда «🆕 Кампания · шаг N/7\\n\\n» — единый префикс к промптам
    этапов (§UX: пользователь видит, где он и сколько осталось). step клампится в 1–7."""
    return i18n.t("cc_step_crumb", step=max(1, min(step, 7)))


async def _cc_render_stage(target: Message, chat_id: int, draft, state: FSMContext) -> None:
    """Отрисовать текущий этап черновика (вход после рестарта/возобновления): диспетчер по
    current_step 0–7 — все восемь этапов §19 реализованы (аккаунт → настройки → ключи → RSA →
    изображения → ассеты → URL-опции → финал)."""
    await state.update_data(cc_session=draft.session_id)
    step = draft.current_step
    if step <= 0:
        await state.set_state(CreateCampaignWizard.account_select)
        await _cc_present_stage0(target, chat_id)
        return
    if step == 1:
        s = draft.wizard_state.get("settings") or {}
        if s:
            await state.set_state(CreateCampaignWizard.settings_confirm)
            await target.answer(
                _cc_crumb(1) + texts.fmt_cc_settings_summary(s),
                reply_markup=cc_settings_kb(can_forward=_cc_max_step(draft) > 1),
                parse_mode=ParseMode.HTML,
            )
        else:
            await state.set_state(CreateCampaignWizard.settings_desc)
            await target.answer(
                _cc_crumb(1) + i18n.t("cc_ask_description"),
                reply_markup=nav_kb(),
                parse_mode=ParseMode.HTML,
            )
        return
    if step == 2:
        await _cc_present_stage2(target, chat_id, draft.session_id, state)
        return
    if step == 3:
        # Если уже есть живая сессия курации — вернём её итог; иначе спросим URL заново.
        rsa_sid = (draft.wizard_state.get("ad") or {}).get("rsa_session_id")
        session = await SESSIONS.get(rsa_sid, expected_chat_id=chat_id) if rsa_sid else None
        if session is not None:
            await state.clear()
            text, kb = _rsa_overview(session)
            await target.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await _cc_present_stage3(target, chat_id, draft.session_id, state)
        return
    if step == 4:
        await _cc_present_stage4(target, chat_id, draft.session_id, state)
        return
    if step == 5:
        await _cc_present_stage5(target, chat_id, draft.session_id, state)
        return
    if step == 6:
        await _cc_present_stage6(target, chat_id, draft.session_id, state)
        return
    if step == 7:
        await _cc_present_stage7(target, chat_id, draft.session_id, state)
        return
    # set_step пишет только 0–7, сюда попасть нельзя; safety-net — финальная сводка (как cc_skip)
    await _cc_present_stage7(target, chat_id, draft.session_id, state)


async def _offer_wizard_resume(m: Message) -> None:
    """§UX-подсказка на /start: если у чата есть незавершённый черновик визарда — мягко предложить
    продолжить (advisory, read-only; НИЧЕГО не создаёт — только кнопки возобновления/старта заново).
    Реюз cc_resume_kb (те же CcCB-хендлеры, что в _cc_entry). Сбой БД — молча пропускаем."""
    try:
        draft = await CDRAFTS.get_active(m.chat.id)
    except Exception:  # noqa: BLE001 — подсказка необязательна, /start не должен падать из-за неё
        return
    if draft is None:
        return
    await m.answer(
        i18n.t("start_resume_hint", step=max(1, draft.current_step)),
        reply_markup=cc_resume_kb(),
    )


async def _cc_entry(m: Message, state: FSMContext) -> None:
    """Вход в визард: незавершённый черновик → предложить продолжить/начать заново; иначе Этап 0."""
    await state.clear()
    existing = await CDRAFTS.get_active(m.chat.id)
    if existing is not None:
        await m.answer(
            i18n.t("cc_resume_prompt", step=max(1, existing.current_step)),
            reply_markup=cc_resume_kb(),
        )
        return
    await _cc_begin(m, m.chat.id, state)


# ── §20: «Информация про клиентов» (профиль клиента + текстовый ввод) ─────────────
async def _present_memory_proposal(
    bot,
    *,
    chat_id: int,
    operation: str,
    customer_id: str,
    params: dict,
    summary: str,
    with_crawl: bool = False,
) -> None:
    """§20: показать черновик memory-операции профиля (save/update/clear) с кнопками ✅/❌.
    user_initiated=True — действие whitelisted-человека. Исполнение — clients.execute за гейтом
    (bot.main._do_confirm маршрутизирует по operation ∈ MEMORY_OPERATIONS). Аккаунт — дочерний
    (это ЛОКАЛЬНАЯ БД, не Google Ads; замок Draft к профилям неприменим). with_crawl=True (в тексте
    указан сайт, §20.3) → добавляет «🕷 Сохранить и краулить» рядом с «✅ Сохранить как есть».
    Через bot.send_message (а не message.answer): вызывается и из фонового авто-сохранения B13."""
    cid = uuid.uuid4().hex
    await STORE.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=customer_id,
        params=params,
        summary=summary,
        chat_id=chat_id,
        user_initiated=True,
    )
    _LAST_PENDING[chat_id] = cid
    kb = client_save_kb(cid) if with_crawl else confirm_kb(cid)
    await bot.send_message(chat_id, summary, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _cli_extract_and_propose(bot, chat_id: int, customer_id: str, buf: list[str]) -> bool:
    """§20.3/20.5: из накопленного текста профиля извлечь поля (LLM) → показать «было→станет» +
    confirm-гейт. Общий путь для «💾 Сохранить» и авто-сохранения по таймауту (B13). Возвращает
    False, если извлечь нечего (пустой буфер/пустое извлечение). Буфер чистит вызыватель."""
    if not buf:
        return False
    extracted = await extract_profile("\n".join(buf), language=i18n.current_lang())
    if extracted.is_empty():
        return False
    patch = extracted.to_patch()
    before = await CLIENTS.get_by_account(customer_id)
    operation = "profile_update" if before is not None else "profile_save"
    # §20.5: preview_merge = ТА ЖЕ семантика, что исполнит apply_upsert (merge по ключу, notes-append)
    # — «было→станет» не может разойтись с фактическим апдейтом.
    after = preview_merge(before, patch)
    summary = texts.fmt_client_diff(before, after, customer_id, operation=operation)
    await _present_memory_proposal(
        bot,
        chat_id=chat_id,
        operation=operation,
        customer_id=customer_id,
        params={"customer_id": customer_id, "patch": patch, "source": "text"},
        summary=summary,
        with_crawl=bool(patch.get("website")),
    )
    return True


def _cli_cancel_idle(chat_id: int) -> None:
    """§20.3/B13: погасить таймер авто-сохранения текста профиля, если он взведён."""
    t = _CLI_IDLE_TASK.pop(chat_id, None)
    if t is not None and not t.done():
        t.cancel()


# 3G: замок «одно извлечение за раз» per-chat — гонка «idle-таймер стрельнул во время ручного
# „Сохранить“» давала ДВА proposal из одного буфера. Под замком буфер потребляется ровно один раз.
_CLI_SAVE_LOCK: dict[int, asyncio.Lock] = {}


def _cli_lock(chat_id: int) -> asyncio.Lock:
    return _CLI_SAVE_LOCK.setdefault(chat_id, asyncio.Lock())


async def _cli_idle_autosave(bot, chat_id: int, customer_id: str, idle: int, state=None) -> None:
    """§20.3/B13: по idle секунд тишины извлекаем накопленный буфер и показываем «было→станет» +
    confirm-гейт (как «💾 Сохранить»). Ничего не сохраняется без ✅ (тот же гейт §5). Фон не роняет
    loop. 3G: после автосейва РЕЖИМ НАКОПЛЕНИЯ ЗАКРЫВАЕТСЯ (state.clear — раньше FSM оставался в
    awaiting_text и следующий текст молча минтил ВТОРОЙ proposal); гонка с ручным «Сохранить»
    закрыта общим _cli_lock (буфер потребляется один раз)."""
    try:
        await asyncio.sleep(idle)
    except asyncio.CancelledError:
        return
    _CLI_IDLE_TASK.pop(chat_id, None)
    async with _cli_lock(chat_id):
        buf = _CLI_TEXT_BUF.pop(chat_id, None) or []
        if not buf:
            return  # ручное «Сохранить» успело первым (замок) — no-op
        try:
            if await _cli_extract_and_propose(bot, chat_id, customer_id, buf):
                if state is not None:  # закрыть режим накопления (возврат — «✏️ Обновить инфу»)
                    try:
                        await state.clear()
                    except Exception:  # noqa: BLE001 — best-effort (storage мог смениться)
                        pass
                await bot.send_message(chat_id, i18n.t("cli_autosaved"))
        except Exception:  # noqa: BLE001 — фон не должен ронять event loop
            log.warning(
                "§20.3 авто-сохранение профиля не удалось (chat=%s)", chat_id, exc_info=True
            )


def _cli_arm_idle(bot, chat_id: int, customer_id: str, state=None) -> None:
    """§20.3/B13: (пере)взвести таймер авто-сохранения. Каждое новое сообщение сбрасывает отсчёт;
    client_text_idle_s ≤ 0 → авто-сохранение отключено (только ручное «💾 Сохранить»).
    state (3G) — FSMContext чата: автосейв закроет режим накопления."""
    _cli_cancel_idle(chat_id)
    idle = int(getattr(settings, "client_text_idle_s", 0) or 0)
    if idle <= 0:
        return
    _CLI_IDLE_TASK[chat_id] = asyncio.create_task(
        _cli_idle_autosave(bot, chat_id, customer_id, idle, state)
    )


async def _cli_read_accounts(target: Message, chat_id: int) -> list:
    """Аккаунты для раздела «Клиенты» (как §19 Этап 0): из discovered-meta всех MCC + пер-юзер
    фильтр (2F, единый перечислитель _read_account_rows). Сбой/пусто → Draft."""
    rows: list = []
    try:
        rows = [r for r in await _read_account_rows(chat_id) if not getattr(r, "manager", False)]
    except Exception as e:  # noqa: BLE001 — сбой перечисления → деградация на Draft
        await target.answer(i18n.t("cli_accounts_error", err=ux.err_text(e)))
    if not rows:
        rows = [_cc_draft_account_row()]
    return rows


async def _cli_check_access(chat_id: int, customer_id: str) -> bool:
    """Композитный fail-closed доступ к аккаунту (глобальный read-замок + пер-пользователь). Draft
    доступен всем whitelisted. Профиль — локальная память, но доступ к аккаунту тот же, что и чтение."""
    try:
        ensure_read_allowed(customer_id)
        await ensure_account_allowed_for_user(chat_id, customer_id)
        return True
    except PermissionError:
        return False


async def _cli_present_accounts(m: Message) -> None:
    """§20.2: список аккаунтов MCC с отметкой ✅ у заполненных профилей."""
    chat_id = m.chat.id
    rows = await _cli_read_accounts(m, chat_id)
    _CLI_ACCT_CACHE[chat_id] = rows
    with_profile = await CLIENTS.accounts_with_profile([getattr(r, "id", "") for r in rows])
    _CLI_WITH_PROFILE[chat_id] = with_profile  # для перелистывания страниц (B7)
    try:  # B7: пикер постраничный; фолбэк на случай, если Telegram отверг разметку — не падаем
        await m.answer(
            i18n.t("cli_pick_account"),
            reply_markup=clients_accounts_kb(rows, with_profile),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("cli: пикер аккаунтов не отрисован (%s)", type(e).__name__)
        await m.answer(i18n.t("cli_pick_account"))


async def _cli_show_card(target: Message, chat_id: int, customer_id: str) -> None:
    """Показать карточку клиента + кнопки действий (Добавить/Обновить/Очистить). Нет доступа → отказ."""
    if not await _cli_check_access(chat_id, customer_id):
        await target.answer(i18n.t("cli_access_denied"))
        return
    profile = await CLIENTS.get_by_account(customer_id)
    has_website = bool(profile and profile.get("website"))
    await target.answer(
        texts.fmt_client_card(profile, customer_id),
        # C4: customer_id в кнопки add/update → приём текста профиля restart-safe (не зависит от FSM)
        reply_markup=client_card_kb(profile is not None, has_website, customer_id=customer_id),
        parse_mode=ParseMode.HTML,
    )


async def _cli_selected_account(state: FSMContext) -> str | None:
    data = await state.get_data()
    return data.get("cli_customer_id")


# ── §20.4: краулинг сайта клиента (фоновая задача) ────────────────────────────────
def _crawl_patch_from_result(extract, result) -> dict:
    """Слить LLM-профиль (structure_crawl) с код-извлечёнными краулером контактами/соцсетями.
    Краул НИКОГДА не заменяет категории целиком (replace-флаги снимаем принудительно) — сайт не
    вправе стереть введённое менеджером руками (§20.5: краул только дополняет/обновляет)."""
    patch = extract.to_patch()
    patch.pop("replace_services", None)
    patch.pop("replace_contacts", None)
    socials = {**(patch.get("socials") or {}), **result.socials}
    if socials:
        patch["socials"] = socials
    contacts = list(patch.get("contacts") or [])
    have = {c.get("value") for c in contacts}
    for ph in result.phones[:5]:
        if ph not in have:
            contacts.append({"kind": "phone", "value": ph})
    for em in result.emails[:5]:
        if em not in have:
            contacts.append({"kind": "email", "value": em})
    if contacts:
        patch["contacts"] = contacts
    return patch


def _crawl_findings(result, patch: dict) -> dict:
    """§20.4: «что нашли» для сводки краула — разделы (заголовки страниц), услуги, цены,
    телефоны, соцсети. Из result (карта страниц) + patch (структурированный профиль)."""
    sections: list[str] = []
    for p in result.pages:
        t = (getattr(p, "title", "") or "").strip()
        if t and t not in sections:
            sections.append(t)
    services = [s.get("name", "") for s in (patch.get("services") or []) if s.get("name")]
    prices = [
        f"{s.get('name', '')} {s.get('price', '')}".strip()
        for s in (patch.get("services") or [])
        if s.get("price")
    ]
    phones = [
        c.get("value", "")
        for c in (patch.get("contacts") or [])
        if c.get("kind") == "phone" and c.get("value")
    ]
    socials = list((patch.get("socials") or {}).keys())
    return {
        "sections": sections,
        "services": services,
        "prices": prices,
        "phones": phones,
        "socials": socials,
    }


async def _run_client_crawl(
    bot, chat_id: int, customer_id: str, url: str, *, mode: str = "full"
) -> None:
    """Фоновый обход сайта клиента: crawl_jobs running→done/failed; профиль пуст → auto-save,
    иначе черновик profile_update («было→станет») с confirm-гейтом (§20.4/20.5). Любой сбой не
    роняет event loop — ошибки редактируются (redact_text) и уходят пользователю понятным текстом.

    mode='incremental' (§20.5 «только новое»): сравниваем обойденные страницы с прошлым краулом по
    content_hash. Ничего не изменилось → НЕ трогаем профиль (сообщаем «сайт не изменился»); есть
    новые/изменённые → обычный update-proposal, но со сводкой diff (сколько новых/изменённых)."""
    from urllib.parse import urlparse

    domain = urlparse(url).netloc or url
    job_id = await crawl_jobs.create_running(
        customer_id=customer_id, chat_id=chat_id, domain=domain, mode=mode
    )
    with request_scope(f"crawl:{job_id}"):
        try:
            can_fetch = await crawler.load_robots(url)
            sitemap = await crawler.fetch_sitemap(url)
            result = await asyncio.wait_for(
                crawler.crawl_site(
                    url,
                    fetcher=crawler.fetch_url_html,
                    can_fetch=can_fetch,
                    sitemap_xml=sitemap,
                    max_pages=settings.crawl_max_pages,
                    max_depth=settings.crawl_max_depth,
                    delay_s=settings.crawl_delay_s,
                    max_text_chars=settings.crawl_max_text_chars,
                ),
                timeout=settings.crawl_time_budget_s,
            )
            if not result.pages:
                await crawl_jobs.mark_failed(job_id, error="no pages")
                await bot.send_message(chat_id, i18n.t("cli_crawl_empty", domain=texts.esc(domain)))
                return
            extract = await structure_crawl(
                pages_text=result.combined_text(), website=url, language=i18n.current_lang()
            )
            patch = _crawl_patch_from_result(extract, result)
            crawl_extra = {
                "website": url,
                "last_crawled_at_now": True,
                "site_pages": result.site_pages_payload(),
            }
            before = await CLIENTS.get_by_account(customer_id)
            # §20.5: инкрементальный перекраул — сравнить с прошлым краулом по content_hash.
            diff_prefix = ""
            if mode == "incremental" and before is not None:
                prev_hashes = await CLIENTS.site_page_hashes(customer_id)
                new_urls, changed_urls = result.diff_against(prev_hashes)
                if prev_hashes and not new_urls and not changed_urls:
                    await crawl_jobs.mark_done(job_id, pages_crawled=result.pages_count)
                    await bot.send_message(
                        chat_id,
                        i18n.t(
                            "cli_crawl_unchanged",
                            domain=texts.esc(domain),
                            pages=result.pages_count,
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=client_show_card_kb(customer_id),  # §20.2: карточка в 1 тап
                    )
                    return
                diff_prefix = (
                    i18n.t("cli_crawl_diff", new=len(new_urls), changed=len(changed_urls)) + "\n\n"
                )
            await crawl_jobs.mark_done(job_id, pages_crawled=result.pages_count)
            # §20.4: богатая сводка «что нашли» (разделы/услуги/цены/контакты/соцсети).
            crawl_msg = diff_prefix + texts.fmt_crawl_summary(
                domain, pages=result.pages_count, **_crawl_findings(result, patch)
            )
            if before is None:
                # свежий профиль + явное действие пользователя (нажал краул) → авто-сохранение
                crawl_cid = uuid.uuid4().hex  # связывает audit-строку с profile_history
                await CLIENTS.apply_upsert(
                    customer_id,
                    patch,
                    operation="crawl_save",
                    confirmation_id=crawl_cid,
                    source="crawl",
                    crawl_extra=crawl_extra,
                )
                # §20.7/§12: авто-сохранение — тоже изменение профиля → пишем audit-строку
                # (кто/когда/что/результат), а не только history+crawl_jobs. Без гейта осознанно:
                # краул запущен явным действием пользователя, prior-данных не перезаписывает.
                try:
                    from db.models import AuditLog
                    from db.session import Session as _Session

                    async with _Session() as _s:
                        _s.add(
                            AuditLog(
                                confirmation_id=crawl_cid,
                                operation="crawl_save",
                                customer_id=str(customer_id),
                                chat_id=chat_id,
                                status="applied",
                                result={
                                    "pages": result.pages_count,
                                    "domain": domain,
                                    "services": len(patch.get("services") or []),
                                    "contacts": len(patch.get("contacts") or []),
                                },
                            )
                        )
                        await _s.commit()
                except Exception:  # noqa: BLE001 — сбой audit-строки не роняет сам краул-сейв
                    log.exception("crawl_save: audit-строка не записана (job %s)", job_id)
                await bot.send_message(
                    chat_id,
                    crawl_msg + "\n\n" + i18n.t("cli_crawl_profile_updated"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=client_show_card_kb(customer_id),  # §20.2: карточка в 1 тап
                )
            else:
                # профиль существует → показать «было→станет» и ждать ✅ (не перезаписываем молча).
                # preview_merge — та же merge-семантика, что исполнит apply_upsert (§20.5).
                after = preview_merge(before, patch)
                summary = texts.fmt_client_diff(
                    before, after, customer_id, operation="profile_update"
                )
                cid = uuid.uuid4().hex
                await STORE.save_proposal(
                    confirmation_id=cid,
                    operation="profile_update",
                    customer_id=customer_id,
                    params={
                        "customer_id": customer_id,
                        "patch": patch,
                        "source": "crawl",
                        "crawl_extra": crawl_extra,
                    },
                    summary=summary,
                    chat_id=chat_id,
                    user_initiated=True,
                )
                _LAST_PENDING[chat_id] = cid
                await bot.send_message(
                    chat_id,
                    crawl_msg + "\n\n" + i18n.t("cli_crawl_confirm_update") + "\n\n" + summary,
                    reply_markup=confirm_kb(cid),
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:  # noqa: BLE001 — фон не должен ронять loop; ошибку редактируем
            log.warning("crawl job %s failed: %s", job_id, type(e).__name__, exc_info=e)
            await crawl_jobs.mark_failed(job_id, error=str(e))
            try:
                # Без имени класса (P1-аудит): причина — редактированный текст ошибки; пустой →
                # generic (класс остаётся в логе строкой выше).
                reason = redact_text(str(e)).strip() or "?"
                await bot.send_message(
                    chat_id,
                    i18n.t(
                        "cli_crawl_failed",
                        domain=texts.esc(domain),
                        err=texts.esc(reason),
                    ),
                )
            except Exception:  # noqa: BLE001 — чат недоступен; уже залогировали
                pass


def _spawn_crawl(bot, chat_id: int, customer_id: str, url: str, *, mode: str = "full") -> bool:
    """Запустить фоновую задачу краула и удержать ссылку (иначе GC соберёт незавершённую задачу).
    Дедуп по customer_id: если обход этого аккаунта уже идёт — второй не плодим (возврат False),
    чтобы двойной клик не создал два параллельных краула, два profile_update-черновика и два job'а."""
    key = str(customer_id)
    existing = _CRAWL_INFLIGHT.get(key)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(_run_client_crawl(bot, chat_id, customer_id, url, mode=mode))
    _CRAWL_INFLIGHT[key] = task
    task.add_done_callback(lambda _t, k=key: _CRAWL_INFLIGHT.pop(k, None))
    return True


async def _cc_after_settings(target: Message, chat_id: int, session_id: str, state) -> None:
    """После принятия настроек (Этап 1) → Этап 2 (ключевые слова)."""
    await _cc_present_stage2(target, chat_id, session_id, state)


# ── Этап 3: объявление (URL → display path → курация RSA, отдаётся в черновик) ─────
async def _cc_present_stage3(target: Message, chat_id: int, session_id: str, state) -> None:
    await CDRAFTS.set_step(session_id, 3, expected_chat_id=chat_id)
    await state.set_state(CreateCampaignWizard.ad_url)
    await state.update_data(cc_session=session_id)
    await target.answer(
        _cc_crumb(3) + i18n.t("cc_ask_url"), reply_markup=nav_kb(), parse_mode=ParseMode.HTML
    )


async def _cc_finalize_ad(target: Message, chat_id: int, session, state) -> None:
    """Handoff из rsa_finalize, когда курация принадлежит визарду (brief.cc_session): утверждённые
    заголовки/описания → в черновик, переход к Этапу 4 (изображения). Мутации НЕТ (proposal не минтуем)."""
    session_id = session.brief.get("cc_session")
    draft = await CDRAFTS.get(session_id, expected_chat_id=chat_id) if session_id else None
    if draft is None:
        await target.answer(i18n.t("cc_draft_stale"))
        return
    headlines = session.approved_texts("h")
    descriptions = session.approved_texts("d")

    def _save(st: dict) -> None:
        st["ad"]["headlines"] = headlines
        st["ad"]["descriptions"] = descriptions

    await CDRAFTS.patch(session_id, _save, expected_chat_id=chat_id)
    await target.answer(i18n.t("cc_ad_saved"))
    await _cc_present_stage4(target, chat_id, session_id, state)


# ── Этап 4: изображения объявления (image assets) — прикрепить или пропустить ──────
async def _cc_present_stage4(target: Message, chat_id: int, session_id: str, state) -> None:
    snap = await CDRAFTS.set_step(session_id, 4, expected_chat_id=chat_id)
    await state.set_state(CreateCampaignWizard.images)
    await state.update_data(cc_session=session_id)
    await target.answer(
        _cc_crumb(4) + i18n.t("cc_images_prompt"),
        reply_markup=cc_skip_kb(can_forward=_cc_max_step(snap) > 4),
        parse_mode=ParseMode.HTML,
    )


async def _cc_after_images(target: Message, chat_id: int, session_id: str, state) -> None:
    """После изображений — Этап 5 (ассеты)."""
    await _cc_present_stage5(target, chat_id, session_id, state)


async def _cc_image_from_photo(m: Message, state: FSMContext, bot: Bot) -> None:
    """Этап 4: фото → нарезка 1.91:1 + 1:1 → во временное медиа-хранилище по media_id; id кладём в
    черновик (бинарь НЕ в JSON/логах, golden rule #5). Используется на финальном composite-создании."""
    if not m.photo:
        return
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(i18n.t("cc_draft_stale"))
        return
    try:
        buf = io.BytesIO()
        await bot.download(m.photo[-1], destination=buf)
        landscape, square = await asyncio.to_thread(prepare_display_images, buf.getvalue())
    except Exception as e:  # сеть/битый файл/не картинка
        await m.answer(i18n.t("err_photo", err=ux.err_text(e)))
        return
    media_id = uuid.uuid4().hex
    await asyncio.to_thread(save_pending_media, media_id, landscape, square)
    snap = await CDRAFTS.patch(
        session_id,
        lambda st: st["images"]["media_ids"].append(media_id),
        expected_chat_id=m.chat.id,
    )
    n = len(((snap.wizard_state if snap else {}).get("images") or {}).get("media_ids") or [])
    # Остаёмся на Этапе 4: можно прислать ещё фото; «Готово/Пропустить» (cc_skip) → Этап 5. НЕ
    # продвигаем здесь — иначе второе фото попадёт в чужой стейт и перехватится GDN-веткой on_photo.
    await m.answer(
        i18n.t("cc_image_saved", n=n),
        reply_markup=cc_skip_kb(can_forward=_cc_max_step(snap) > 4),
    )


# ── Этап 2: ключевые слова (свои текстом/ссылкой ИЛИ генерация → Sheets → верификация) ─
async def _cc_present_stage2(target: Message, chat_id: int, session_id: str, state) -> None:
    """Этап 2 с учётом сохранённого подсостояния (B3-resume): (а) выгруженная и НЕ верифицированная
    таблица → вернуться в kw_verify с той же ссылкой; (б) верифицированный список → обзор с гейтом
    «✅ Подтвердить ключевые слова»; (в) иначе — свежий промпт ввода ключей."""
    await CDRAFTS.set_step(session_id, 2, expected_chat_id=chat_id)
    await state.update_data(cc_session=session_id)
    draft = await CDRAFTS.get(session_id, expected_chat_id=chat_id)
    kw = (draft.wizard_state.get("keywords") or {}) if draft else {}
    fwd = _cc_max_step(draft) > 2  # W4: этап 3+ уже был пройден → показываем «Вперёд ›»
    if kw.get("sheet_id") and not kw.get("verified"):
        # (а) round-trip в полёте: пере-показать ссылку на таблицу и ждать её обратно (kw_verify).
        # Fallback-URL для черновиков, созданных до появления sheet_url.
        url = kw.get("sheet_url") or f"https://docs.google.com/spreadsheets/d/{kw['sheet_id']}/edit"
        await state.set_state(CreateCampaignWizard.kw_verify)
        await target.answer(i18n.t("cc_kw_sheet_ready", url=url))
        # P0-2: если сгенерированный список уже сохранён — предлагаем «Использовать эти ключи»
        # (без обязательной правки таблицы); старые черновики без list — прежний промпт со ссылкой.
        if kw.get("list"):
            await target.answer(
                i18n.t("cc_kw_verify_prompt_v2", n=len(kw.get("list") or [])),
                reply_markup=cc_kw_verify_kb(can_forward=fwd),
                parse_mode=ParseMode.HTML,
            )
        else:
            await target.answer(i18n.t("cc_kw_verify_prompt"), reply_markup=nav_kb())
        return
    if kw.get("list") and kw.get("verified"):
        # (б) список уже верифицирован: обзор + гейт подтверждения (как в _cc_save_keywords).
        kw_list = list(kw.get("list") or [])
        mt_label = (
            i18n.t("cc_kw_mixed_mt")
            if kw.get("match_types")
            else texts.match_type_human(kw.get("match_type") or "phrase")
        )
        preview = "\n".join(f"  • {texts.esc(k)}" for k in kw_list[:10])
        if len(kw_list) > 10:
            preview += "\n " + i18n.t("list_more", n=len(kw_list) - 10)
        await state.set_state(CreateCampaignWizard.keywords)
        await target.answer(
            i18n.t("cc_kw_review", n=len(kw_list), mt=texts.esc(mt_label), preview=preview),
            reply_markup=cc_kw_confirm_kb(can_forward=fwd),
            parse_mode=ParseMode.HTML,
        )
        return
    await state.set_state(CreateCampaignWizard.keywords)
    await target.answer(
        _cc_crumb(2) + i18n.t("cc_kw_prompt"),
        reply_markup=cc_kw_kb(can_forward=fwd),
        parse_mode=ParseMode.HTML,
    )


def _cc_default_match_type(draft) -> str:
    """§19.4.1: тип соответствия по умолчанию для ключей без явного маркера — подтверждённый на
    Этапе 1 (settings.match_type: «по аналогии» частый в аккаунте либо phrase-дефолт). Раньше
    sheet/генерация молча зашивали phrase → менеджер подтверждал exact, а получал phrase (B6)."""
    from keywords.ingest import DEFAULT_MATCH_TYPE

    return ((draft.wizard_state.get("settings") or {}).get("match_type")) or DEFAULT_MATCH_TYPE


def _cc_sanitize_keywords(raw: list[str]) -> tuple[list[str], int]:
    """§19.4.1: отфильтровать сырые ключи из Google Sheets через assert_keyword_ok (длина/число слов/
    символы) — как уже делает текстовый путь. Возвращает (валидные, сколько отброшено): иначе «Создать
    черновик» падал бы безликим ValidationError на 11-словном заголовке-ключе из колонки A (B5)."""
    from keywords.ingest import assert_keyword_ok

    clean: list[str] = []
    dropped = 0
    for k in raw:
        try:
            clean.append(assert_keyword_ok(k))
        except Exception:  # noqa: BLE001 — мусорный/слишком длинный ключ отбрасываем, не роняя флоу
            dropped += 1
    return clean, dropped


async def _cc_save_keywords(
    target: Message,
    chat_id: int,
    session_id: str,
    state,
    kw_list: list[str],
    mt: str,
    source: str,
    per_kw_mts: list[str] | None = None,
) -> None:
    """Сохранить верифицированный список ключей в черновик и перейти к Этапу 3 (объявление).

    §19.4.1: per_kw_mts — типы соответствия 1:1 к kw_list для СМЕШАННОГО списка ([exact] + "phrase"
    + broad). Дедуп ПАРАМИ (первый выигрывает вместе со своим типом) — иначе дедуп только текстов
    порвал бы склейку 1:1 в схеме. None/однородный ⇒ скалярный mt (поведение прежнее)."""
    if not kw_list:
        await target.answer(i18n.t("cc_kw_empty"), reply_markup=cc_kw_kb())
        return
    mixed: list[str] | None = None
    if per_kw_mts and len(per_kw_mts) == len(kw_list) and len(set(per_kw_mts)) > 1:
        # B11: дедуп по ПАРЕ (текст, тип) — strip() как в normalize_keywords/assert_keyword_ok.
        # Google Ads допускает один текст с разными типами; дедуп только по тексту терял бы второй.
        seen: set[tuple[str, str]] = set()
        dd_kw: list[str] = []
        dd_mt: list[str] = []
        for k, kmt in zip(kw_list, per_kw_mts):
            key = (k or "").strip()
            pair = (key, str(kmt))
            if key and pair not in seen:
                seen.add(pair)
                dd_kw.append(key)
                dd_mt.append(kmt)
        kw_list, mixed = dd_kw, dd_mt

    def _save(st: dict) -> None:
        st["keywords"].update(
            {
                "list": kw_list,
                "match_type": mt,
                "match_types": mixed,  # None ⇒ однородный список
                "source": source,
                "verified": True,
            }
        )

    snap = await CDRAFTS.patch(session_id, _save, expected_chat_id=chat_id)
    if snap is None:
        # B3: черновик уже не active (TTL-abandon за время Sheets round-trip / заменён) — patch — no-op.
        # Не рапортуем ложный «список готов»: сообщаем, что черновик устарел, и выходим.
        await target.answer(i18n.t("cc_draft_stale"))
        return
    mt_label = i18n.t("cc_kw_mixed_mt") if mixed else texts.match_type_human(mt)
    # §19.4: явный гейт «✅ Подтвердить ключевые слова» ПЕРЕД Этапом 3 (обзор финального списка).
    # Замена = прислать новый список (state остаётся на Этапе 2). Превью — первые 10, без простыни.
    preview = "\n".join(f"  • {texts.esc(k)}" for k in kw_list[:10])
    if len(kw_list) > 10:
        preview += "\n " + i18n.t("list_more", n=len(kw_list) - 10)
    await state.set_state(CreateCampaignWizard.keywords)
    await state.update_data(cc_session=session_id)
    await target.answer(
        i18n.t("cc_kw_review", n=len(kw_list), mt=texts.esc(mt_label), preview=preview),
        reply_markup=cc_kw_confirm_kb(can_forward=_cc_max_step(snap) > 2),
        parse_mode=ParseMode.HTML,
    )


# ── Этап 5: ассеты (переиспользовать текущие аккаунта / пропустить) ───────────────
async def _cc_present_stage5(target: Message, chat_id: int, session_id: str, state) -> None:
    snap = await CDRAFTS.set_step(session_id, 5, expected_chat_id=chat_id)
    await state.set_state(CreateCampaignWizard.assets)
    await state.update_data(cc_session=session_id)
    await target.answer(
        _cc_crumb(5) + i18n.t("cc_assets_prompt"),
        reply_markup=cc_assets_kb(can_forward=_cc_max_step(snap) > 5),
        parse_mode=ParseMode.HTML,
    )


async def _cc_asset_logo_from_photo(m: Message, state: FSMContext, bot: Bot) -> None:
    """§19.7.1: фото логотипа (Этап 5, Business logo) → квадратный кадр 1:1 → временное хранилище →
    спек {family: business_logo, media_id} в набор ассетов черновика. Бинарь НЕ в params/логах."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(i18n.t("cc_draft_stale"))
        return
    try:
        buf = io.BytesIO()
        await bot.download(m.photo[-1], destination=buf)
        _, square = await asyncio.to_thread(prepare_display_images, buf.getvalue())
    except Exception as e:  # сеть/битый файл/не картинка
        await m.answer(i18n.t("err_photo", err=ux.err_text(e)))
        return
    media_id = uuid.uuid4().hex
    await asyncio.to_thread(save_pending_media, media_id, square, square)
    name = (draft.wizard_state.get("settings") or {}).get("campaign_name") or "logo"
    spec = {"family": "business_logo", "params": {"media_id": media_id, "name": f"{name}_logo"}}
    snap = await CDRAFTS.patch(
        session_id, lambda st: st["assets"]["new"].append(spec), expected_chat_id=m.chat.id
    )
    await state.set_state(CreateCampaignWizard.assets)
    await state.update_data(cc_session=session_id)
    await m.answer(i18n.t("cc_asset_logo_added"))
    await m.answer(
        _cc_crumb(5) + i18n.t("cc_assets_prompt"),
        reply_markup=cc_assets_kb(can_forward=_cc_max_step(snap or draft) > 5),
        parse_mode=ParseMode.HTML,
    )


# ── Этап 6: Ad URL options (tracking/suffix или пропустить) ───────────────────────
async def _cc_present_stage6(target: Message, chat_id: int, session_id: str, state) -> None:
    snap = await CDRAFTS.set_step(session_id, 6, expected_chat_id=chat_id)
    await state.set_state(CreateCampaignWizard.url_options)
    await state.update_data(cc_session=session_id)
    await target.answer(
        _cc_crumb(6) + i18n.t("cc_url_prompt"),
        reply_markup=cc_skip_kb(can_forward=_cc_max_step(snap) > 6),
        parse_mode=ParseMode.HTML,
    )


# ── Этап 7: финальная сводка → правка командой → ✅ Создать черновик (composite proposal) ──
def _cc_build_create_params(draft) -> dict:
    """Собрать params create_search_campaign из накопленного черновика (§19.8). Без секретов
    (image_media_ids — это id, не бинарь). Глубокую валидацию выполнит SCHEMAS + mutations."""
    st = draft.wizard_state
    s = st.get("settings") or {}
    ad = st.get("ad") or {}
    kw = st.get("keywords") or {}
    imgs = st.get("images") or {}
    assets = st.get("assets") or {}
    url = st.get("url_options") or {}
    # пустой скелет url_options (все поля пусты) → None: не шлём «пустые» опции в SDK/proposal
    url_clean = {k: v for k, v in url.items() if v} or None
    bidding = {"strategy": s.get("bidding_strategy") or "manual_cpc"}
    if s.get("target_cpa_micros"):
        bidding["target_cpa_micros"] = int(s["target_cpa_micros"])
    from ads import geo as adsgeo

    # Страна-хинт для резолва гео: из настроек → из названий локаций → D7 конфиг-дефолт (env
    # DEFAULT_GEO_COUNTRY_CODE, деплой Уганды → UG), НЕ захардкоженная Украина.
    geo_cc = s.get("geo_country_code") or adsgeo.resolve_country(s) or settings.geo_default_country
    # §19: верифицированный список может быть большим — обрежем до потолка схемы, чтобы «Создать
    # черновик» не падал ValidationError (напр. 81 ключ > прежнего max_length=50). Обрезку не
    # молчим (см. правило «no silent caps») — логируем, сколько ключей отброшено.
    kw_all = list(kw.get("list") or [])
    kw_list = kw_all[:MAX_CAMPAIGN_KEYWORDS]
    if len(kw_all) > MAX_CAMPAIGN_KEYWORDS:
        log.warning(
            "§19: список ключей %d > потолка %d — обрезан (отброшено %d)",
            len(kw_all),
            MAX_CAMPAIGN_KEYWORDS,
            len(kw_all) - MAX_CAMPAIGN_KEYWORDS,
        )
    # §19.4.1: per-keyword типы (смешанный список) — режем СИНХРОННО с keywords (1:1 к схеме).
    kw_mts_all = list(kw.get("match_types") or [])
    kw_mts = kw_mts_all[: len(kw_list)] if kw_mts_all else []
    return {
        "campaign_name": s.get("campaign_name") or "Search",
        "final_url": ad.get("final_url") or "",
        "headlines": list(ad.get("headlines") or []),
        "descriptions": list(ad.get("descriptions") or []),
        "budget_daily_micros": int(s.get("budget_daily_micros") or 0),
        "keywords": kw_list,
        "match_type": kw.get("match_type") or "phrase",
        "keyword_match_types": kw_mts,
        "cpc_bid_micros": int(s.get("cpc_bid_micros") or 500_000),
        "geo_locations": list(s.get("geo_locations") or []),
        "geo_country_code": geo_cc,
        "geo_locale": s.get("geo_locale") or "ru",
        "languages": list(s.get("languages") or []),
        "bidding": bidding,
        "path1": ad.get("path1") or None,
        "path2": ad.get("path2") or None,
        "url_options": url_clean,
        "asset_specs": list(assets.get("new") or []),
        "existing_asset_links": list(assets.get("reuse_links") or []),
        "image_media_ids": list(imgs.get("media_ids") or []),
        # §19.3: сети / расписание / даты (None/[] ⇒ дефолты: Search-only, 24/7, старт сегодня)
        "networks": s.get("networks"),
        "ad_schedule_blocks": list(s.get("ad_schedule_blocks") or []),
        "start_date": s.get("start_date"),
        "end_date": s.get("end_date"),
    }


async def _cc_present_stage7(target: Message, chat_id: int, session_id: str, state) -> None:
    await CDRAFTS.set_step(session_id, 7, expected_chat_id=chat_id)
    await state.set_state(CreateCampaignWizard.final)
    await state.update_data(cc_session=session_id)
    draft = await CDRAFTS.get(session_id, expected_chat_id=chat_id)
    if draft is None:  # гонка с истечением/abandon черновика (mypy-находка аудита) — не падаем
        await target.answer(i18n.t("cc_draft_stale"))
        return
    await target.answer(
        _cc_crumb(7) + texts.fmt_cc_final_summary(draft.wizard_state),
        reply_markup=cc_final_kb(),
        parse_mode=ParseMode.HTML,
    )


async def _cc_resummarize(target: Message, session_id: str, chat_id: int) -> None:
    snap = await CDRAFTS.get(session_id, expected_chat_id=chat_id)
    if snap is None:  # черновик истёк/брошен между правкой и пере-сводкой — не падаем
        await target.answer(i18n.t("cc_draft_stale"))
        return
    await target.answer(i18n.t("cc_edit_applied"))
    await target.answer(
        _cc_crumb(7) + texts.fmt_cc_final_summary(snap.wizard_state),
        reply_markup=cc_final_kb(),
        parse_mode=ParseMode.HTML,
    )


# ── GDN из фото (§11): приём фото → бриф → черновик за confirm-гейтом ─────────────
def _parse_gdn_brief(text: str) -> tuple[str, str, float, list[str]] | None:
    """«название | url | бюджет [| гео]» → (name, url, budget_units, geo_locations). None при неверном
    формате. Гео (§11) — опциональное 4-е поле: локации через запятую (пусто ⇒ без гео, как раньше)."""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) not in (3, 4):
        return None
    name, url, budget_s = parts[0], parts[1], parts[2]
    if not name or not url.startswith(("http://", "https://")):
        return None
    try:
        budget = float(budget_s.replace(",", "."))
    except ValueError:
        return None
    # Верхняя граница (core.limits.MONEY_MAX_UNITS) — отвергаем СРАЗУ, без «1e9» и путаного
    # Pydantic-сообщения после генерации текстов (golden rule #4: считает КОД).
    if budget <= 0 or budget > MONEY_MAX_UNITS:
        return None
    geo = [g.strip() for g in parts[3].split(",")] if len(parts) == 4 else []
    geo = [g for g in geo if g][:50]  # чистим пустые, потолок как в схеме
    return (name, url, budget, geo)


async def _gdn_cleanup(state: FSMContext, media_id: str | None) -> None:
    """Сброс визарда GDN + удаление временных файлов медиа (без утечки на путях ошибок)."""
    await state.clear()
    if media_id:
        await asyncio.to_thread(clear_pending_media, media_id)


async def _video_mint_proposal(
    target: Message, chat_id: int, state: FSMContext, *, logo_media_id: str | None
) -> None:
    """Собрать params из state, провалидировать схемой (defense-in-depth) и показать confirm-гейт.
    Общий хвост для «логотип прислан» / «пропущен» / Video-ветки."""
    data = await state.get_data()
    kind = data.get("video_kind") or "dg"
    params = dict(data.get("video_params") or {})
    if not params or not data.get("video_yt"):
        await state.clear()
        await target.answer(i18n.t("video_session_stale"))
        return
    op = "create_demand_gen_campaign" if kind == "dg" else "create_video_campaign"
    acct = await _active_read_account(
        chat_id
    )  # §8: DG/Video на активном аккаунте (не хардкод Draft)
    if kind == "dg" and logo_media_id:
        params["logo_media_id"] = logo_media_id
    try:  # defense-in-depth: схема ещё раз проверит длины/составы/URL/бюджет/YouTube id
        validated = SCHEMAS[op](**params).model_dump()
    except Exception as e:  # noqa: BLE001 — валидация
        await state.clear()
        if logo_media_id:
            await asyncio.to_thread(clear_pending_media, logo_media_id)
        await target.answer(i18n.t("err_validate", err=ux.err_text(e)))
        return
    # P1-9: апфронт-предупреждение, если дневной бюджет ниже типичного минимума DG/Video для валюты
    # аккаунта мутации (Draft) — иначе Google отклонит с per_day_minimum уже ПОСЛЕ подтверждения.
    try:
        from ads.client import build_client_async
        from core.limits import dg_video_min_daily_units

        acct_cur = await _read_currency(await build_client_async(acct), acct)
        min_units = dg_video_min_daily_units(acct_cur)
        have_units = validated["budget_daily_micros"] / 1_000_000
        if have_units < min_units:
            await target.answer(
                i18n.t(
                    "dg_budget_below_min",
                    cur=acct_cur or "",
                    minv=f"{min_units:g}",
                    have=f"{have_units:g}",
                )
            )
    except Exception:  # noqa: BLE001 — предупреждение best-effort, не роняем показ черновика
        pass
    summary = texts.fmt_video_proposal_summary(
        kind,
        params["campaign_name"],
        params["final_url"],
        params["youtube_video_id"],
        params["budget_daily_micros"] / 1_000_000,
        params["headlines"],
        params["descriptions"],
        params["business_name"],
        params.get("geo_locations") or [],
        goal=params.get("goal", "clicks"),
        with_logo=bool(logo_media_id),
    )
    p = Proposal(operation=op, summary=summary, params=validated, chat_id=chat_id)
    await state.clear()
    # AD.3: DG/Video на активном аккаунте; при неоднозначности (не закреплён + живых >1) — форс-пикер
    # (замок ensure_allowed — внутри _present_proposal на итоговом аккаунте).
    await _present_proposal_active(
        target,
        chat_id=chat_id,
        operation=op,
        params=validated,
        summary=summary,
        cid=p.confirmation_id,
    )


async def _video_logo_from_photo(m: Message, state: FSMContext, bot: Bot) -> None:
    """§11 DG: фото → квадратный кадр 1:1 (логотип) → временное хранилище → confirm-гейт.
    Бинарь НЕ в proposal/логах — в params идёт только logo_media_id."""
    try:
        buf = io.BytesIO()
        await bot.download(m.photo[-1], destination=buf)
        _, square = await asyncio.to_thread(prepare_display_images, buf.getvalue())
    except Exception as e:  # сеть/битый файл/не картинка
        await m.answer(i18n.t("err_photo", err=ux.err_text(e)))
        return
    media_id = uuid.uuid4().hex
    await asyncio.to_thread(save_pending_media, media_id, square, square)
    await _video_mint_proposal(m, m.chat.id, state, logo_media_id=media_id)


# ── §2A: клон кампании «как в кампании X» (read live → черновик create_search_campaign) ──
# ── §3-assets: ассеты-расширения кампании (sitelinks/callouts/snippets/показать/удалить) ──
def _parse_sitelinks(text: str) -> list[dict]:
    """Построчно «Текст | url [| описание1 [| описание2]]» → list[dict]. Пустые строки/поля
    пропускаем. Длину/состав валидирует схема AddSitelinks при сборке черновика."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        sl: dict = {"link_text": parts[0], "final_url": parts[1]}
        if len(parts) >= 3 and parts[2]:
            sl["description1"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            sl["description2"] = parts[3]
        out.append(sl)
    return out


def _parse_csv_lines(text: str) -> list[str]:
    """Запятые/переносы строк → список непустых токенов (для callouts/значений сниппета)."""
    return [p.strip() for p in (text or "").replace("\n", ",").split(",") if p.strip()]


async def _ext_nav_kb(state: FSMContext):
    """nav_kb для шага мастера расширений: «‹ Назад» → меню расширений кампании (idx из state)."""
    idx = (await state.get_data()).get("ext_idx", -1)
    back = CampCB(action="ext", idx=idx) if isinstance(idx, int) and idx >= 0 else None
    return nav_kb(back)


async def _ext_image_from_photo(m: Message, state: FSMContext, bot: Bot) -> None:
    """Фото → подготовка (1.91:1) → ЧЕРНОВИК attach_image_asset (PAUSED-семантика не нужна — это
    расширение). Бинарь во временном хранилище по media_id (НЕ в proposal). Исполнение после ✅."""
    data = await state.get_data()
    campaign = (data.get("ext_campaign") or "").strip()
    if not campaign:
        await state.clear()
        await m.answer(i18n.t("ext_stale"))
        return
    try:
        buf = io.BytesIO()
        await bot.download(m.photo[-1], destination=buf)
        landscape, square = await asyncio.to_thread(prepare_display_images, buf.getvalue())
    except Exception as e:  # сеть/битый файл/не картинка
        await m.answer(i18n.t("err_photo", err=ux.err_text(e)))
        return
    media_id = uuid.uuid4().hex
    await asyncio.to_thread(save_pending_media, media_id, landscape, square)
    await state.clear()
    try:
        cid, operation, params, summary = _build_proposal(
            "attach_image_asset", campaign=campaign, media_id=media_id, name=f"img{media_id[:12]}"
        )
    except Exception as e:  # noqa: BLE001 — валидация схемы → понятный ответ + чистим медиа
        await asyncio.to_thread(clear_pending_media, media_id)
        await m.answer(await _friendly_error(e, "media:attach_image"))
        return
    # AD.4: картинка-расширение — на АКТИВНОМ аккаунте (не хардкод Draft); форс-пикер при неоднозначности.
    await _present_proposal_active(
        m, chat_id=m.chat.id, operation=operation, params=params, summary=summary, cid=cid
    )


# ── §2B: именованные шаблоны кампаний (/savetemplate, /templates, создание из шаблона) ──
def _parse_savetemplate_arg(arg: str) -> tuple[str, str | None]:
    """«<имя> [from <кампания>]» → (name, source|None). Разделитель « from » — регистронезависимо,
    по ПОСЛЕДНЕМУ вхождению (имя шаблона может содержать слово from)."""
    idx = arg.lower().rfind(" from ")
    if idx < 0:
        return arg.strip(), None
    name = arg[:idx].strip()
    source = arg[idx + 6 :].strip()
    return name, (source or None)


async def _send_templates(message: Message, chat_id: int) -> None:
    from db.templates import list_templates

    rows = await list_templates(chat_id)
    if not rows:
        await message.answer(i18n.t("tpl_list_empty"))
        return
    _TPL_CACHE[chat_id] = rows
    await message.answer(i18n.t("tpl_list_title", n=len(rows)), reply_markup=templates_kb(rows))


# ── §2C: авто-память — /recent (повторить недавнее применённое действие) ────────────
async def _send_recent(message: Message, chat_id: int) -> None:
    from db.history import list_recent_applied

    rows = await list_recent_applied(chat_id, limit=5)
    if not rows:
        await message.answer(i18n.t("recent_empty"))
        return
    _RECENT_CACHE[chat_id] = rows
    lines = []
    for i, a in enumerate(rows):
        label = texts.fmt_mutation_summary(a.operation, a.params) or a.summary or a.operation
        lines.append(f"{i + 1}. {texts.esc(label.splitlines()[0][:120])}")
    body = i18n.t("recent_title") + "\n\n" + "\n".join(lines)
    await message.answer(body, reply_markup=recent_kb(rows), parse_mode=ParseMode.HTML)


class _SearchBuildError(Exception):
    """Жёсткий отказ сборки create_search_campaign из конфига источника — несёт i18n-ключ (+kw)
    для понятного ответа пользователю. Перехватывается вызывающим (clone / шаблон-из-кампании)."""

    def __init__(self, key: str, **kw):
        super().__init__(key)
        self.key = key
        self.kw = kw


async def _search_params_from_cfg(
    cfg,
    *,
    campaign_name: str,
    budget_units: float | None = None,
    final_url: str | None = None,
) -> tuple[dict, float, int, bool]:
    """Из CampaignConfig источника собрать ВАЛИДИРОВАННЫЕ params create_search_campaign под новым
    именем (общая логика клона §2A и шаблона-из-кампании §2B). Берём ad_group[0] (схема делает одну
    группу — ограничение v1). RSA-длины считает КОД (кириллица=1): длинные отбрасываем, при нехватке
    регенерируем. Возвращает (validated_params, budget_units, dropped_texts, regenerated). Жёсткие
    отказы → _SearchBuildError(key)."""
    if cfg.channel_type != "SEARCH":
        raise _SearchBuildError("clone_not_search", name=texts.esc(cfg.name))
    if not cfg.ad_groups:
        raise _SearchBuildError("clone_empty", name=texts.esc(cfg.name))
    ag = cfg.ad_groups[0]
    url = (final_url or ag.final_url or "").strip()
    if not url:
        raise _SearchBuildError("clone_no_url", name=texts.esc(cfg.name))
    budget = budget_units or (cfg.budget_micros / 1_000_000)
    if not budget or budget <= 0:
        raise _SearchBuildError("clone_no_budget")

    headlines = [h for h in ag.headlines if rsa_validate(h, "headline")[0]]
    descriptions = [d for d in ag.descriptions if rsa_validate(d, "description")[0]]
    dropped = (len(ag.headlines) - len(headlines)) + (len(ag.descriptions) - len(descriptions))
    regenerated = False
    if len(headlines) < RSA_MIN_HEADLINES or len(descriptions) < RSA_MIN_DESCRIPTIONS:
        try:
            draft = await _generate_rsa(
                CopyBrief(topic=campaign_name, n_headlines=15, n_descriptions=4)
            )
        except Exception as e:  # LLM/сеть
            raise _SearchBuildError("err_text_gen", err=ux.err_text(e)) from e
        headlines = list(draft.headlines or [])[:RSA_MAX_HEADLINES]
        descriptions = list(draft.descriptions or [])[:RSA_MAX_DESCRIPTIONS]
        regenerated = True
        if len(headlines) < RSA_MIN_HEADLINES or len(descriptions) < RSA_MIN_DESCRIPTIONS:
            raise _SearchBuildError("search_gen_empty")
    else:
        headlines = headlines[:RSA_MAX_HEADLINES]
        descriptions = descriptions[:RSA_MAX_DESCRIPTIONS]

    params = {
        "campaign_name": campaign_name,
        "final_url": url,
        "headlines": headlines,
        "descriptions": descriptions,
        "budget_daily_micros": int(round(budget * 1_000_000)),
        "keywords": [k.text for k in ag.keywords][:50],
        "match_type": ag.keywords[0].match_type if ag.keywords else "phrase",
        "cpc_bid_micros": ag.cpc_bid_micros or 500_000,
    }
    try:  # defense-in-depth: схема ещё раз проверит длины/составы/URL/бюджет + нормализует ключи
        validated: dict = SCHEMAS["create_search_campaign"](**params).model_dump()
    except Exception as e:
        raise _SearchBuildError("err_validate", err=ux.humanize_validation(e)) from e
    return validated, float(budget), dropped, regenerated


async def _clone_from_intent(m: Message, brief: dict) -> None:
    """«сделай кампанию N с настройками как в кампании X» → читаем живой конфиг источника и
    собираем ЧЕРНОВИК create_search_campaign (PAUSED, тот же confirm-гейт, что /newsearch). Гео/
    минус-слова/стратегия/аудитории НЕ переносятся (честно в сводке). Источник не найден /
    имя-дубль → понятный стоп БЕЗ черновика. Сборку params (включая RSA-валидацию) делает общий
    _search_params_from_cfg."""
    new_name = (brief.get("new_name") or "").strip()
    source = (brief.get("source_campaign") or "").strip()
    if not new_name or not source:
        await m.answer(i18n.t("clone_bad_args"))
        return
    from ads.client import build_client_async
    from ads.read import read_campaign_config
    from ads.resolve import find_campaign_by_name

    # AD.3: клон ЧИТАЕТ источник и СОЗДАЁТ клон на ОДНОМ аккаунте — форс-пикер ставим ДО чтения
    # источника (пикер при неоднозначности пинит активный аккаунт; иначе повтор клона был бы
    # некогерентен: источник с акка A, клон на B). Не-Draft/единственный живой/Draft — как раньше.
    acct = await _require_read_account(m, "clone")
    if acct is None:  # показан пикер — оператор выберет аккаунт и повторит команду клона
        return
    try:
        client = await build_client_async(acct)
        cfg = await run_ads_read_call(
            read_campaign_config, client, acct, source, label="read_campaign_config"
        )
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(i18n.t("clone_read_error", err=ux.err_text(e)))
        return
    if cfg is None:
        await m.answer(i18n.t("clone_source_not_found", name=texts.esc(source)))
        return
    # Имя-дубль ломает резолв по имени (find_campaign_by_name LIMIT 1) → стоп ДО показа черновика.
    try:
        existing = await run_ads_read_call(
            find_campaign_by_name, client, acct, new_name, label="find_campaign_by_name"
        )
    except Exception:  # noqa: BLE001 — дубль-проверка best-effort, не роняем клон из-за сбоя read
        existing = None
    if existing is not None:
        await m.answer(i18n.t("clone_name_taken", name=texts.esc(new_name)))
        return

    try:
        validated, budget_units, dropped, regenerated = await _search_params_from_cfg(
            cfg,
            campaign_name=new_name,
            budget_units=brief.get("budget_daily_units"),
            final_url=brief.get("final_url"),
        )
    except _SearchBuildError as e:
        await m.answer(i18n.t(e.key, **e.kw))
        return
    params = validated
    summary = texts.fmt_clone_proposal_summary(
        new_name, source, budget_units, validated, dropped, regenerated
    )
    p = Proposal(
        operation="create_search_campaign", summary=summary, params=params, chat_id=m.chat.id
    )
    await _present_proposal(
        m,
        chat_id=m.chat.id,
        operation="create_search_campaign",
        params=params,
        summary=summary,
        cid=p.confirmation_id,
        customer_id=acct,  # §8: клон создаётся на том же аккаунте, с которого читали источник
    )


# ── advisor: рекомендации (advisory, read-only) ───────────────────────────────────
# §advisor #1: what a recommendation can apply in ONE TAP — ТОЛЬКО НЕ-денежные операции.
# update_budget/update_bid НАМЕРЕННО исключены: деньги/ставки только прямой командой (golden rule #3),
# никогда one-tap. Гард дублируется в _advise_apply (defense-in-depth) + тест test_advise_apply_*.
# Единый источник — bot.keyboards.ADVISE_APPLY_OPS (его же импортирует scheduler-дайджест):
# два списка one-tap операций разъехались бы молча (гард денег, golden rule #3).
_ADVISE_APPLY_OPS = ADVISE_APPLY_OPS


async def _advise_run(
    target: Message,
    chat_id: int,
    *,
    topic: str | None = None,
    account: str | None = None,
    period_days: int | None = None,
    source: str = "advise",
) -> None:
    """Показать РЕКОМЕНДАЦИИ по аккаунту. READ-ONLY: собирает отчёт + правила (advisor.service),
    НИЧЕГО не меняет и proposal НЕ создаёт — исполнение любого совета идёт ОТДЕЛЬНОЙ командой через
    confirm-гейт (golden rule #1/#3). Каждая рекомендация — своим сообщением с 👍/👎 (per-rec фидбек,
    Слой B). account/period_days — из NL-запроса («улучшить аккаунт X за 7 дней»); account резолвится
    через композитный read-замок (запрещённый → внятный отказ). Сбой чтения → ошибка, не мутация."""
    from advisor import service as advisor_service
    from core.access import resolve_read_account

    try:  # тот же резолв, что /report и _do_read: read-замок × пер-юзер грант (fail-closed)
        acct = await resolve_read_account(chat_id, account)
    except PermissionError:
        await target.answer(i18n.t("loop_account_denied", account=str(account or "")))
        return
    except LookupError as e:
        await target.answer(i18n.t("loop_account_not_found", detail=str(e)))
        return
    except Exception:  # noqa: BLE001 — сбой резолва настройки не должен ломать /advise
        acct = await _active_read_account(chat_id)
    lang = i18n.get_lang(chat_id)
    topics = None if not topic or topic == "all" else [topic]
    days = period_days if isinstance(period_days, int) and period_days > 0 else 30
    async with ux.typing_action(target):
        try:
            rec_set = await advisor_service.build_recommendations(
                chat_id, acct, topics=topics, source=source, lang=lang, period_days=days
            )
        except Exception as e:  # сеть/доступ/SDK — не роняем денежный путь, показываем ошибку
            await target.answer(i18n.t("advise_error", err=ux.err_text(e)))
            return
    proactive = str(await _load_ui_pref(chat_id, "advise_proactive")).lower() in (
        "1",
        "true",
        "on",
        "yes",
    )
    if not rec_set.recs:
        # Внятный empty-state: нет активности (пустой Draft/тест) ≠ «всё в норме». В первом случае
        # подсказываем выбрать живой аккаунт (частая причина — активен пустой Draft, F).
        if not rec_set.has_activity:
            hint = _live_account_hint(acct)
            body = i18n.t("advise_empty_no_data", lang)
            await target.answer(
                body + (("\n\n" + hint) if hint else ""),
                reply_markup=advise_header_kb(proactive, lang),
            )
            return
        await target.answer(
            i18n.t("advise_empty", lang), reply_markup=advise_header_kb(proactive, lang)
        )
        return
    await target.answer(
        i18n.t("advise_header", lang, account=rec_set.account, period=rec_set.period_label),
        reply_markup=advise_header_kb(proactive, lang),
    )
    for r in rec_set.recs:
        # §advisor #1: «применить» показываем ТОЛЬКО для не-денежных советов (pause/минус-слова);
        # деньги/ставки — вне one-tap (golden rule #3), advise_feedback_kb сам отфильтрует.
        apply_op = r.suggested_operation if r.suggested_operation in _ADVISE_APPLY_OPS else None
        await target.answer(
            r.body or "", reply_markup=advise_feedback_kb(r.rec_uid, lang, apply_op=apply_op)
        )
    for extra in getattr(rec_set, "extras", []):  # #3: advisory-довески (LLM-минус-слова)
        await target.answer(extra)
    await target.answer(i18n.t("advise_disclaimer", lang))


async def _audit_run(
    target: Message,
    chat_id: int,
    *,
    account: str | None = None,
    period_days: int | None = None,
    source: str = "audit",
) -> None:
    """/audit — health-аудит аккаунта: НАШ score (0-100) + семьи находок + топ-3 действий + нативный
    Google optimization_score («второе мнение»). READ-ONLY: собирает данные и СОВЕТУЕТ; ничего не
    меняет и proposal НЕ создаёт — исполнение любого совета идёт ОТДЕЛЬНОЙ командой через confirm-гейт
    (golden rule #1/#3). Резолв аккаунта — тот же композитный read-замок × грант, что /report/_advise
    (fail-closed). Числа считает КОД (движок audit/), не модель."""
    from ads.client import build_client_async
    from audit.collect import gather_audit
    from audit.render import render_audit
    from core.access import resolve_read_account
    from reports.period import last_n_days

    try:  # тот же резолв, что /report и _advise: read-замок × пер-юзер грант (fail-closed)
        acct = await resolve_read_account(chat_id, account)
    except PermissionError:
        await target.answer(i18n.t("loop_account_denied", account=str(account or "")))
        return
    except LookupError as e:
        await target.answer(i18n.t("loop_account_not_found", detail=ux.err_text(e)))
        return
    except Exception:  # noqa: BLE001
        # Явный аккаунт + сбой резолва (напр. грант-БД недоступна) = ОТКАЗ (fail-closed, rule #9/#10):
        # НЕ даунгрейдим на активный (иначе тихо ответим по ДРУГОМУ аккаунту, чем просили). None → ок.
        if account:
            await target.answer(i18n.t("loop_account_denied", account=str(account)))
            return
        acct = await _active_read_account(chat_id)
    lang = i18n.get_lang(chat_id)
    days = period_days if isinstance(period_days, int) and period_days > 0 else 30
    target_cpa = await _load_target_cpa(
        chat_id, acct
    )  # /target: разблокирует 3×-Kill (пауза дорогих)
    async with ux.typing_action(target):
        try:
            client = await build_client_async(acct)
            result = await gather_audit(client, acct, last_n_days(days), target_cpa=target_cpa)
        except Exception as e:  # сеть/доступ/SDK — не роняем денежный путь, показываем ошибку
            await target.answer(i18n.t("advise_error", err=ux.err_text(e)))
            return
    # Нет активности → карточка «—» + подсказка про живой аккаунт (как раньше). parse_mode=None:
    # карточка — чистый текст (имена кампаний от клиента), HTML-парсер не нужен.
    if not result.has_activity:
        hint = _live_account_hint(acct)
        text = render_audit(result, lang)
        await target.answer(text + (("\n\n" + hint) if hint else ""), parse_mode=None)
        return
    # Обзор (score + семьи + Google-балл) БЕЗ топ-3 — действия идут отдельными сообщениями с кнопками.
    await target.answer(render_audit(result, lang, actions=False), parse_mode=None)
    # Находки → Recommendation (advisor.store, source='audit') → per-finding сообщение с 👍/👎/🙈
    # (+ «применить» ТОЛЬКО для не-денежных op из _ADVISE_APPLY_OPS). apply переиспользует существующий
    # _advise_apply → confirm-гейт; rec.customer_id = ПРОАУДИРОВАННЫЙ аккаунт (минтит proposal на него,
    # не на чужой). Сам аудит proposal НЕ создаёт (record_recommendations пишет только в локальную БД).
    findings = result.findings[:_AUDIT_MAX_FINDINGS]
    if findings:
        from advisor import store as advisor_store
        from advisor.rules import Recommendation
        from audit.render import finding_text

        recs = [
            Recommendation(
                kind=f"audit_{f.check_id}",
                topic=f.family,
                severity=f.severity,
                target_campaign=f.target_campaign,
                suggested_operation=f.suggested_operation,
                facts=f.facts,
                evidence=f.evidence,
                body=finding_text(f, lang, result.currency),
            )
            for f in findings
        ]
        await advisor_store.record_recommendations(chat_id, acct, recs, source="audit")
        for f, r in zip(findings, recs):
            apply_op = f.suggested_operation if f.suggested_operation in _ADVISE_APPLY_OPS else None
            await target.answer(
                r.body,
                reply_markup=advise_feedback_kb(r.rec_uid, lang, apply_op=apply_op),
                parse_mode=None,
            )
    # C10 onboarding: есть дорогая кампания (high_cpa), но цель не задана → 3×-Kill молчит и бот НЕ
    # может предложить паузу. Подсказываем /target, иначе флагман «ничего не нашёл» на дорогом аккаунте.
    if target_cpa is None and any(f.check_id == "high_cpa" for f in findings):
        await target.answer(
            "💡 Set /target <CPA> so the bot can flag pricey campaigns (CPA ≥ 3× target) for pausing."
            if lang == "en"
            else "💡 Задай /target <CPA> — тогда бот предложит паузу для дорогих кампаний (CPA ≥ 3× цели)."
        )
    await target.answer(i18n.t("advise_disclaimer", lang))


def _advise_apply_params(rec) -> dict | None:
    """Собрать params мутации из рекомендации для one-tap «применить». None — нельзя собрать.
    pause_campaign → {campaign}; add_negative_keywords → {campaign, keywords:[ключ], match_type}."""
    campaign = rec.target_campaign
    if not campaign:
        return None
    if rec.suggested_operation == "pause_campaign":
        return {"campaign": campaign}
    if rec.suggested_operation == "add_negative_keywords":
        kw = (rec.evidence or {}).get("keyword")
        if not kw:
            return None
        return {"campaign": campaign, "keywords": [kw], "match_type": "broad"}
    return None


async def _advise_apply(cq: CallbackQuery, rec_uid: str) -> None:
    """§advisor #1: применить совет в один тап → СТАРТ confirm-гейта (proposal), НЕ исполнение.
    ЖЁСТКИЙ гард (golden rule #3): только не-денежные операции (_ADVISE_APPLY_OPS); деньги/ставки
    НИКОГДА не one-tap. Аккаунт мутации = rec.customer_id (тот, о котором был совет) → _present_proposal
    заново проходит ensure_allowed (не-Draft вне allow-list → внятный отказ, не тихая подмена)."""
    from advisor import store as advisor_store
    from agent.tools.schemas import SCHEMAS

    chat_id = _cq_chat_id(cq)
    msg = _cq_msg(cq)
    rec = await advisor_store.get_recommendation(rec_uid)
    if rec is None or msg is None:
        await cq.answer(i18n.t("advise_apply_stale"), show_alert=True)
        return
    op = rec.suggested_operation
    if op not in _ADVISE_APPLY_OPS:  # деньги/ставки/структура — только вручную командой
        await cq.answer(i18n.t("advise_apply_not_actionable"), show_alert=True)
        return
    params = _advise_apply_params(rec)
    if params is None:
        await cq.answer(i18n.t("advise_apply_stale"), show_alert=True)
        return
    try:  # defense-in-depth: схема мутации ещё раз валидирует состав/длины/нормализует ключи
        validated: dict = SCHEMAS[op](**params).model_dump()
    except Exception:  # noqa: BLE001 — кривой ключ/имя → не минтим, просим /advise заново
        await cq.answer(i18n.t("advise_apply_stale"), show_alert=True)
        return
    await cq.answer()
    p = Proposal(operation=op, summary="", params=validated, chat_id=chat_id)
    await _present_proposal(
        msg,
        chat_id=chat_id,
        operation=op,
        params=validated,
        summary=rec.body or "",
        cid=p.confirmation_id,
        customer_id=rec.customer_id,  # аккаунт совета; ensure_allowed на минтинге (fail-closed)
    )


# ── Свободный текст → агент ───────────────────────────────────────────────────────
# ── ingest: приём файла → чтение → задача (бриф/ключи/данные для агента) ─────────────
async def _cc_keywords_from_document(m: Message, state: FSMContext, text: str, name: str) -> None:
    """§19.4.1 Ввод A (файл): XLSX/CSV/TXT с ключами внутри визарда «Создание кампании» (Этап 2).
    Текст файла → parse_keywords_text (маркеры типов соответствия работают) → черновик → Этап 3.
    Раньше присланный файл падал в общий ingest и СБРАСЫВАЛ визард (state.clear) — теряя черновик."""
    data = await state.get_data()
    session_id = data.get("cc_session")
    draft = await CDRAFTS.get(session_id, expected_chat_id=m.chat.id) if session_id else None
    if draft is None:
        await state.clear()
        await m.answer(i18n.t("cc_draft_stale"))
        return
    from keywords.ingest import parse_keywords_text

    default_mt = _cc_default_match_type(draft)  # B6: подтверждённый на Этапе 1, не хардкод phrase
    parsed = parse_keywords_text(text, default_match_type=default_mt)
    if not parsed:
        await m.answer(i18n.t("cc_kw_empty"), reply_markup=cc_kw_kb())
        return
    await m.answer(i18n.t("cc_kw_file_accepted", name=texts.esc(name)), parse_mode=ParseMode.HTML)
    kw_list = [k.text for k in parsed]
    mt = parsed[0].match_type if parsed else default_mt
    per_kw = [k.match_type for k in parsed]
    await _cc_save_keywords(m, m.chat.id, session_id, state, kw_list, mt, "file", per_kw_mts=per_kw)


async def _dispatch_command_result(
    m: Message, res: dict, state: FSMContext, *, external_context: bool = False
) -> None:
    """Единый роутинг исхода handle_command (используют on_text, ingest-флоу файла/ссылки).
    external_context=True — команда шла со СПРАВОЧНЫМ контентом (файл/ссылка): денежные черновики
    получают предупреждение в сводке (см. _present_proposal)."""
    t = res.get("type")
    if t == "proposal":
        # G2/G3/AD.3: NL-команда изменения нацелена на АКТИВНЫЙ аккаунт чата. Если он НЕ закреплён,
        # а живых несколько — _present_proposal_active заставит выбрать аккаунт ДО черновика (не
        # угадываем, чьи деньги). Не-Draft аккаунт обязан быть включён на мутации (иначе внятный отказ).
        await _present_proposal_active(
            m,
            chat_id=m.chat.id,
            operation=res["operation"],
            params=res.get("params", {}),
            summary=res["summary"],
            cid=res["confirmation_id"],
            external_context=external_context,
        )
    elif t == "rsa_intent":
        await _rsa_start_from_intent(m, res.get("brief", {}), state)
    elif t == "keywords_intent":
        await _kw_start_from_intent(m, res.get("brief", {}), state)
    elif t == "clone_intent":
        await _clone_from_intent(m, res.get("brief", {}))
    elif t == "advise_intent":
        # «Что улучшить (аккаунт X за N дней)?» → рекомендации (advisory, read-only). Ничего не
        # создаёт и не меняет — исполнение любого совета идёт отдельной командой через confirm-гейт.
        brief = res.get("brief", {})
        await _advise_run(
            m,
            m.chat.id,
            topic=brief.get("topic"),
            account=brief.get("account"),
            period_days=brief.get("period_days"),
        )
    elif t == "clarify":
        await m.answer("❓ " + res["question"])
    elif t == "need_account":
        # §8: NL-чтение без аккаунта при нескольких живых — не угадываем и не показываем пустой
        # Draft. Пикер target='status': после тапа сразу придёт статистика (_render_status).
        rows = await _read_account_rows(m.chat.id)
        _REPORT_ACCT_CACHE[m.chat.id] = rows
        await m.answer(
            i18n.t("pick_live_account_first"),
            reply_markup=report_accounts_kb(
                rows,
                "status",
                last=await _last_account(m.chat.id),
                frequent=await _frequent_accounts(m.chat.id),
            ),
            parse_mode=ParseMode.HTML,
        )
    elif t == "read":
        # C5: явный диапазон дат в ответе агента → честная подпись периода вместо «N дн.»
        pf, pt = res.get("date_from"), res.get("date_to")
        period_label = ""
        if pf and pt:
            period_label = pf if pf == pt else f"{pf} — {pt}"
        await m.answer(
            texts.fmt_stats(
                res.get("account", ""),
                res.get("days", 30),
                res.get("stats", {}),
                res.get("currency", ""),
                name=res.get("account_name", ""),  # 2.1: имя кладёт agent/loop из meta
                period_label=period_label,
            ),
            parse_mode=ParseMode.HTML,
        )
    else:
        text = res.get("text")
        if not text:  # пустой ответ агента — не показываем «(пусто)», даём локализованную подсказку
            log.debug("agent-loop: пустой text в ответе (op=%s)", res.get("type"))
            text = i18n.t("loop_unrecognized")
        await m.answer(text)


async def _run_task_with_context(
    m: Message, *, instruction: str, context_text: str, source: str, state: FSMContext
) -> None:
    """Задача + СПРАВОЧНЫЙ КОНТЕНТ (из файла/ссылки) → агент → роутинг исхода (как on_text).
    Мутации всё равно за confirm-гейтом — контент это данные, не команды."""
    if await _llm_budget_or_reply(m):  # C3: пер-юзер дневной потолок LLM (fail-closed)
        return
    ctx = _build_agent_context(m.chat.id)  # C1/C3: последняя кампания/аккаунт + история реплик
    async with ux.typing_action(m):
        res = await handle_command(
            instruction, chat_id=m.chat.id, context_text=context_text, context=ctx
        )
    _chat_ctx_note(m.chat.id, user_text=instruction)  # текущая инструкция → история для след. хода
    await m.answer(i18n.t("ingest_used", source=texts.esc(source)), parse_mode=ParseMode.HTML)
    # Внешний контент = поверхность prompt-injection → денежные черновики получат предупреждение.
    await _dispatch_command_result(m, res, state, external_context=True)


# ── Inline: выбор кампании и быстрые действия ─────────────────────────────────────
def _cq_chat_id(cq: CallbackQuery) -> int:
    return cq.message.chat.id if cq.message else cq.from_user.id


def _actor(event: object) -> tuple[int | None, str | None]:
    """(user_id, username) нажавшего/написавшего — «кто» для audit (§12). chat_id в группе —
    это чат, не человек; актора берём из from_user. None при отсутствии (системный вход)."""
    fu = getattr(event, "from_user", None)
    if fu is None:
        return None, None
    return getattr(fu, "id", None), getattr(fu, "username", None)


async def _camp_mutate(cq: CallbackQuery, idx: int, operation: str, **extra) -> None:
    """Кнопка пауза/возобновление/сети: СОЗДАЁТ черновик (как текстовая команда) → confirm-гейт.
    НЕ исполняет мутацию напрямую — только после ✅ через ту же ветку, что и on_text.
    extra — доп. параметры схемы операции (напр. search_partners для set_campaign_network)."""
    chat_id = _cq_chat_id(cq)
    camps = _CAMP_CACHE.get(chat_id)
    if not _valid_idx(camps, idx):
        await cq.answer(i18n.t("camp_list_stale"), show_alert=True)
        return
    name = camps[idx]["name"]
    try:
        cid, op, params, summary = _build_proposal(operation, campaign=name, **extra)
    except Exception as e:  # валидация схемы
        await cq.answer(await _friendly_error(e, "camp:menu", short=True), show_alert=True)
        return
    await cq.answer()
    msg = _cq_msg(cq)
    if msg is None:
        return
    await _present_proposal(
        msg,
        chat_id=chat_id,
        operation=op,
        params=params,
        summary=summary,
        cid=cid,
        customer_id=_camp_account(chat_id),  # §8: мутируем аккаунт, с которого читали /campaigns
    )


# ── Inline: подтверждение/отмена черновика (confirm-гейт) ─────────────────────────
async def _do_confirm_stage1(cq: CallbackQuery, cid: str) -> None:
    """P1-6: первый шаг ДВОЙНОГО подтверждения необратимого удаления. НЕ исполняем — только меняем
    клавиатуру на финальную (⚠️ Да, удалить безвозвратно / Отмена) и предупреждаем. Черновик
    остаётся pending; реальное исполнение — второй тап (action=ok → _do_confirm)."""
    await cq.answer(i18n.t("delete_confirm_alert"), show_alert=True)
    await _safe_edit_markup(cq, confirm_final_kb(cid))


async def _twofa_begin(
    cq: CallbackQuery, cid: str, chat_id: int, state: FSMContext | None, operation: str
) -> bool:
    """§12: опасная операция при включённом 2FA — ОТЛОЖИТЬ исполнение и запросить PIN. Черновик
    остаётся `pending` (не confirmed): неверный код/отмена → повторить ✅ можно, ничего не сожжено.
    Fail-closed: 2FA включён без PIN → блок (не открываем op); без FSMContext (нет канала приёма
    кода) → тоже блок. Возвращает False (мутация НЕ применена в этом заходе)."""
    if not twofa.is_ready():  # включён, но PIN не задан → fail-closed: НЕ открываем опасную op
        await _safe_edit(cq, i18n.t("twofa_not_configured"))
        return False
    if state is None:  # нет FSM-канала для приёма кода (редкий вызов) → fail-closed блок
        await cq.answer(i18n.t("twofa_need_button"), show_alert=True)
        return False
    _TWOFA_PENDING[chat_id] = {"cid": cid, "cq": cq, "attempts": 0}
    await state.set_state(TwoFactor.awaiting_code)
    await cq.answer()
    await _safe_edit(
        cq, i18n.t("twofa_prompt", op=texts.esc(str(operation))), parse_mode=ParseMode.HTML
    )
    return False


async def _do_confirm(
    cq: CallbackQuery, cid: str, *, state: FSMContext | None = None, twofa_ok: bool = False
) -> bool:
    """Подтвердить и исполнить черновик. Возвращает True только если мутация реально применена
    (finalize записан); False на stale/сбое execute. Вызыватели, у которых от исхода зависит
    следующий шаг (§20 «Сохранить и краулить»), должны проверять возврат.

    §12 2FA: ДО перевода в confirmed — если операция опасна и 2FA включён, отложить исполнение и
    запросить код (черновик остаётся `pending`). `twofa_ok=True` — повторный вход после верного
    кода (гейт пройден). `state` нужен для FSM-ожидания кода; без него опасную op не открываем
    (fail-closed)."""
    chat_id = _cq_chat_id(cq)
    # §12: 2FA-гейт стоит ПЕРЕД confirm — неверный код/отмена оставляют черновик pending (повторить
    # ✅ можно), ничего не сжигая. get_confirmed читает черновик в любом статусе (нужна operation).
    if not twofa_ok:
        peek = await STORE.get_confirmed(cid)
        if peek is not None and twofa.required_for(peek.operation):
            return await _twofa_begin(cq, cid, chat_id, state, peek.operation)
    actor_id, actor_name = _actor(cq)
    if not await STORE.confirm(
        cid, chat_id=chat_id, actor_user_id=actor_id, actor_username=actor_name
    ):
        await cq.answer(i18n.t("stale"), show_alert=True)
        return False
    _LAST_PENDING.pop(chat_id, None)
    await cq.answer(i18n.t("cb_working"))
    await _safe_edit(cq, i18n.t("executing"))  # убирает кнопки
    # ВАЖНО (денежный путь): успешное исполнение и косметический edit_text РАЗДЕЛЕНЫ. execute_confirmed
    # уже применил мутацию и записал finalize → если бы пост-успешный edit_text (часто бросает
    # TelegramBadRequest: «message is not modified»/«to edit not found» после >48ч) попал в этот же
    # except — record_failure пометил бы УЖЕ применённую операцию как failed (лишняя audit-строка +
    # юзеру FAILED). Поэтому ловим только сбой самого execute_confirmed, а APPLIED-edit — отдельно.
    try:
        # §20: развилка доменов — memory-операции профиля (не Google Ads) идут в отдельный
        # исполнитель (clients.execute), минуя ads.mutations и замок аккаунта. Всё остальное —
        # прежний ads-путь. get_confirmed уже используется ниже в _do_cancel — паттерн знаком.
        snap = await STORE.get_confirmed(cid)
        if snap is not None and snap.operation in MEMORY_OPERATIONS:
            result = await execute_confirmed_memory(STORE, cid)
        else:
            result = await execute_confirmed(STORE, cid)
    except Exception as e:  # доступ/резолв/SDK — мутация НЕ применена
        # Денежный путь: пишем в лог С traceback (RedactionFilter чистит секреты) — раньше молчал;
        # в audit_log кладём УЖЕ редактированный текст (str(e) от SDK/google.auth может нести креды).
        log.error(
            "исполнение мутации провалено cid=%s chat=%s: %s",
            cid,
            chat_id,
            type(e).__name__,
            exc_info=e,
        )
        # §15: человекочитаемая ошибка Google Ads (сообщения+коды+request_id) вместо «GoogleAdsException».
        # humanize_google_ads_error сам редактирует секреты (golden rule #5) — кладём в audit И юзеру.
        human = humanize_google_ads_error(e)
        # record_failure в своём try: если БД недоступна, audit-строку не записали, но пользователю
        # ВСЁ РАВНО сообщим о провале (иначе он навсегда остался бы с «executing…», а исключение
        # ушло бы в глобальный errors-хендлер). Полнота уведомления важнее полноты audit при сбое БД.
        try:
            await STORE.record_failure(cid, error=human)
        except Exception:  # noqa: BLE001 — БД недоступна; логируем и продолжаем к ответу юзеру
            log.exception("record_failure не записан cid=%s (БД недоступна?)", cid)
        await _safe_edit(
            cq,
            i18n.t("failed", err=texts.esc(human)),  # 3C: без технического имени класса
            parse_mode=ParseMode.HTML,
        )
        return False
    # Успех: мутация применена и finalize записан. Косметический сбой UI-edit НЕ должен пометить
    # успешную операцию как failed — отдельный try/except, вне ветки record_failure.
    log.info("мутация применена cid=%s chat=%s", cid, chat_id)  # денежный путь — успех в лог
    # Слой B (advisor): связать applied-мутацию с открытой рекомендацией → замер результата позже.
    # Best-effort, только ЛОКАЛЬНАЯ БД (не мутация Ads): успех операции от этого не зависит.
    if snap is not None:
        try:
            from advisor.outcome import link_applied_mutation

            await link_applied_mutation(
                chat_id, snap.operation, snap.params or {}, snap.customer_id, cid
            )
        except Exception:  # noqa: BLE001 — Слой B необязателен, денежный путь не роняем
            log.debug("advise outcome link не выполнен cid=%s", cid)
    # 3C: человекочитаемый итог вместо сырого dict; fmt_mutation_result отдаёт ГОТОВЫЙ HTML
    # (эскейп внутри) — texts.esc здесь дал бы двойное экранирование.
    human_result = texts.fmt_mutation_result(snap.operation if snap else "", result)
    await _safe_edit(cq, i18n.t("applied", result=human_result), parse_mode=ParseMode.HTML)
    # D2: применена обратимая операция → предложить «↩️ Откатить» (мятие ОБРАТНОГО черновика за
    # confirm-гейтом; прямого исполнения нет). Только если снимок _before достаточен для реверса.
    if snap is not None and snap.operation in _ROLLBACKABLE_OPS:
        rev = _reverse_spec(snap.operation, snap.params or {}, (snap.params or {}).get("_before"))
        msg = _cq_msg(cq)
        if rev is not None and msg is not None:
            token = uuid.uuid4().hex
            _ROLLBACK_CACHE[chat_id] = {
                "token": token,
                "operation": rev[0],
                "params": rev[1],
                "customer_id": snap.customer_id,
            }
            await msg.answer(i18n.t("rollback_offer"), reply_markup=rollback_kb(token))
    # partial_failure (батчи ключей/минус-слов): часть позиций отклонена сервером — честно
    # перечисляем причины (не молчим об «усечённом» успехе). Причины уже отредактированы.
    if isinstance(result, dict) and result.get("rejected"):
        rej = result["rejected"]
        reasons = "\n".join(
            f"• {texts.esc(str(r.get('keyword', '')))} — {texts.esc(str(r.get('reason', '')))}"
            for r in rej[:3]
        )
        msg = _cq_msg(cq)
        if msg is not None:
            await msg.answer(
                i18n.t("kw_partial_rejected", ok=result.get("count", 0), bad=len(rej))
                + "\n"
                + reasons,
                parse_mode=ParseMode.HTML,
            )
    # §20.2: профиль сохранён/обновлён → кнопка «📋 Карточка клиента» в 1 тап (кроме clear —
    # карточки больше нет). Read-only довесок, мутаций не создаёт.
    if (
        snap is not None
        and snap.operation in MEMORY_OPERATIONS
        and snap.operation != "profile_clear"
    ):
        msg = _cq_msg(cq)
        if msg is not None:
            await msg.answer(
                i18n.t("cli_view_card_hint"),
                reply_markup=client_show_card_kb(snap.customer_id),
            )
    # §19.8/§11: черновик кампании создан (PAUSED) → предлагаем «🚀 Запустить» ОТДЕЛЬНОЙ командой.
    # Кнопка НЕ запускает сама: минтит resume_campaign proposal (тот же confirm-гейт «PAUSED→ENABLED»).
    # Распространено на ВСЕ создающие кампанию операции (Search/GDN/Demand Gen/Video) — аудит §11.
    if snap is not None and snap.operation in _CREATE_CAMPAIGN_OPS:
        name = (snap.params or {}).get("campaign_name") or ""
        msg = _cq_msg(cq)
        if name and msg is not None:
            _CC_LAUNCH_CACHE[chat_id] = name  # legacy-фолбэк для кнопок без sub (до деплоя)
            prompt = i18n.t("cc_created_launch_prompt") + "\n\n" + i18n.t("cc_created_next_steps")
            # §19.3: если конверс-стратегия понижена (аккаунт без отслеживания конверсий) — честно
            # сообщаем, а не молча меняем то, что менеджер подтвердил.
            if isinstance(result, dict) and result.get("bidding_note"):
                prompt = i18n.t("cc_bidding_downgraded") + "\n\n" + prompt
            # §UX «что дальше»: запуск/кампании/минус-слова — advisory-кнопки; запуск несёт
            # confirmation_id создания (sub) → работает и после рестарта (резолв из БД).
            await msg.answer(prompt, reply_markup=post_create_kb(cid))
    # B9: черновик визарда §19 не гасили в cc_create — гасим ТОЛЬКО теперь, после успешного создания.
    # При ❌ (reject) он остаётся active → менеджер возобновляет «▶️ Продолжить» и не теряет всю работу
    # визарда (RSA, ключи, настройки) из-за одного нажатия «Отмена» на финальном гейте.
    # Связка cid→draft — из params proposal (БД): finish работает и после рестарта процесса.
    if snap is not None and snap.operation == "create_search_campaign":
        sid = (snap.params or {}).get("_cc_draft")
        if sid:
            await CDRAFTS.finish(sid, expected_chat_id=chat_id)
    return True


async def _do_cancel(cq: CallbackQuery, cid: str) -> None:
    chat_id = _cq_chat_id(cq)
    actor_id, actor_name = _actor(cq)
    # §19/§11: отклонённый черновик create_search/gdn/demand_gen_campaign несёт временные медиа по
    # media_id — чистим их (на success их чистит execute_confirmed; на reject — здесь), иначе осиротеют.
    snap = await STORE.get_confirmed(cid)
    if snap is not None and snap.operation == "create_search_campaign":
        from ads.assets import collect_search_campaign_media_ids

        # изображения Этапа 4 + логотипы business_logo из asset-спеков (§19.7.1) — единый сборщик
        await asyncio.to_thread(
            clear_pending_media_ids, collect_search_campaign_media_ids(snap.params)
        )
    if snap is not None and snap.operation == "create_gdn_campaign":
        mid = (snap.params or {}).get("media_id")
        if mid:
            await asyncio.to_thread(clear_pending_media, mid)
    if snap is not None and snap.operation == "create_demand_gen_campaign":
        lmid = (snap.params or {}).get("logo_media_id")
        if lmid:
            await asyncio.to_thread(clear_pending_media, lmid)
    await STORE.reject(cid, chat_id=chat_id, actor_user_id=actor_id, actor_username=actor_name)
    _LAST_PENDING.pop(chat_id, None)
    # B9: отклонение финального proposal НЕ гасит черновик визарда (остаётся active) — менеджер
    # возобновляет «▶️ Продолжить» и не теряет работу. Связка cid→draft в params отклонённого
    # proposal инертна (finish читает её ТОЛЬКО на успехе create в _do_confirm).
    await _safe_edit(cq, i18n.t("rejected"))
    await cq.answer(i18n.t("cb_cancelled"))


# ── Inline: курация RSA (поэлементно + массово) ───────────────────────────────────
async def _rsa_edit(cq: CallbackQuery, session) -> None:
    """Перерисовать текущий шаг курации в том же сообщении (следующий pending или итог)."""
    text, kb = _rsa_render(session)
    await _safe_edit(cq, text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _rsa_edit_overview(cq: CallbackQuery, session) -> None:
    text, kb = _rsa_overview(session)
    await _safe_edit(cq, text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ── 4A: регистрация хендлеров по доменам — ЕДИНСТВЕННЫЙ источник порядка теперь
# bot/handlers/__init__.py::HANDLER_MODULES (menu_guard первым, fallback/on_text последним;
# инвариант закреплён tests/test_handler_order.py). Раньше порядок держали 12 хрупких
# star-импортов: перестановка строк (IDE/мерж/автофикс) тихо скрамблила диспатч.
# Позднее связывание: модули читают имена main через bm.<name> — сохранено. ──
def _reexport_handlers(mods: list) -> None:
    """Ре-экспорт публичных функций хендлер-модулей в globals() bot.main: тесты/скрипты зовут
    их как bot.main.<handler> (прежняя семантика `from ... import *`). setdefault — имя main
    всегда побеждает при конфликте (и логируем: тень хендлера — почти наверняка ошибка)."""
    g = globals()
    for mod in mods:
        for name, obj in vars(mod).items():
            if name.startswith("_") or getattr(obj, "__module__", None) != mod.__name__:
                continue
            if name in g and g[name] is not obj:
                log.warning(
                    "реэкспорт хендлеров: имя %s из %s затенено — пропущено", name, mod.__name__
                )
                continue
            g[name] = obj


from bot import handlers as _handlers  # noqa: E402

_reexport_handlers(_handlers.register_all())


async def main() -> None:
    setup_logging()
    init_observability()  # Sentry — no-op без SENTRY_DSN (core.observability)
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN пуст — добавь в .env (токен у @BotFather).")
        return
    if not settings.whitelist:
        log.warning(
            "whitelist пуст — бот НИКОМУ не ответит (fail-closed). "
            "Добавь TELEGRAM_WHITELIST_CHAT_IDS в .env (хотя бы свой chat_id)."
        )
    try:
        await init_db()
    except Exception as e:  # БД недоступна: НЕ даём дефолтному excepthook напечатать DSN с паролем
        log.error("init_db не удалось — бот не стартует: %s", type(e).__name__, exc_info=e)
        return
    # B2: гард одного polling-инстанса (Postgres advisory lock; на SQLite no-op). Занят другим
    # инстансом → чисто выходим, НЕ лезем polling'ом (иначе Telegram 409 Conflict у обоих). Сбой
    # самого захвата (БД мигнула) не должен ронять старт — тогда работаем как раньше (без гарда).
    try:
        if not await acquire_single_instance_lock():
            log.warning(
                "другой инстанс Aimash уже держит polling-lock — этот процесс выходит "
                "(защита от 409 Conflict). Убей дубль или дождись его остановки."
            )
            return
    except Exception as e:  # noqa: BLE001 — сбой захвата lock не критичен (деградируем без гарда)
        log.warning("single-instance lock не захвачен (%s) — стартую без гарда", type(e).__name__)
    try:  # §8/мультиаккаунт: расшифровать per-account OAuth-токены (oauth_tokens) в рантайм-кэш,
        # чтобы build_client(child) для дочерних под другими MCC брал их refresh-токен/login_customer_id.
        # Сбой не критичен — Draft/тест-MCC покрыт единым .env-токеном (см. ads.client._cfg_for).
        from ads.client import load_oauth_cache

        await load_oauth_cache()
    except Exception as e:  # noqa: BLE001 — per-account креды опциональны (Draft работает на .env)
        log.warning("oauth: per-account токены не загружены: %s", type(e).__name__)
    try:  # §4: восстановить сохранённые языки интерфейса (user_settings.language), переживает рестарт
        await i18n.load_langs()
    except Exception as e:  # настройка не критична — стартуем на дефолтах (RU)
        log.warning("языки интерфейса не загружены из БД: %s", type(e).__name__)
    try:  # восстановить выбранную в боте модель (/model), переживает рестарт
        saved_model = await _load_model_override()
        if saved_model:
            router.set_active_model(saved_model)
            log.info("модель ИИ: активна %s (из user_settings)", saved_model)
    except Exception as e:  # настройка не критична — стартуем на дефолтах из .env
        log.warning("model_override не загружен из БД: %s", type(e).__name__)
    try:  # прогрев Google Ads клиента: тяжёлый импорт SDK + OAuth выполняем на старте off-loop,
        # а не на первом /status — иначе первый интерактивный read морозит event loop на ~0.5-2с.
        from ads.client import build_client

        await asyncio.to_thread(build_client)  # @lru_cache → все последующие вызовы мгновенны
    except Exception as e:  # cred-сбой на старте не валит бота — реальные вызовы всё равно проверят
        log.warning("прогрев build_client не удался: %s", type(e).__name__)
    try:  # §8: обойти настроенные MCC и запомнить дочерние как read-allow-list (полный мульти-
        # аккаунт ЧТЕНИЕ). READ-ONLY, под замком ensure_manager_allowed; сбой не критичен для старта
        # (без обхода читаем только мутационный аккаунт + env read-list). Мутации не затрагиваются.
        from ads.client import discover_read_children

        await discover_read_children()
    except Exception as e:  # noqa: BLE001 — обход дочерних опционален (Draft читается и без него)
        log.warning("mcc discover: обход дочерних не выполнен: %s", type(e).__name__)
    # Корреляция (§15): request_id ДО whitelist — даже отказ доступа логируется с request_id.
    dp.message.outer_middleware(TraceMiddleware())
    dp.callback_query.outer_middleware(TraceMiddleware())
    dp.message.outer_middleware(WhitelistMiddleware())
    dp.callback_query.outer_middleware(WhitelistMiddleware())
    # Язык интерфейса (§4): ставит contextvar до хендлеров (порядок относительно whitelist не важен
    # функционально — оба outer; язык нужен лишь когда хендлер уже формирует ответ).
    dp.message.outer_middleware(LangMiddleware())
    dp.callback_query.outer_middleware(LangMiddleware())
    dp.message.outer_middleware(ThrottleMiddleware())  # анти-спам (ТЗ §12), после whitelist
    # N4: /команда во время визарда сворачивает визард → срабатывает её Command-хендлер (после Lang,
    # чтобы возможные сообщения сворачивания были локализованы; после Whitelist — только для своих).
    dp.message.outer_middleware(SlashCommandExitsWizardMiddleware())
    bot = Bot(token)
    # Меню-команды + профиль бота (about/description в @BotFather). Всё косметика — ставим при
    # каждом старте, чтобы правки текстов подхватывались без ручного BotFather. ПАРАЛЛЕЛЬНО
    # (3 независимых round-trip к Telegram): раньше последовательно задерживало приём апдейтов.
    # return_exceptions=True сохраняет «не критично», но КАЖДУЮ ошибку логируем (иначе потеряли бы).
    for r in await asyncio.gather(
        bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats()),
        bot.set_my_commands(
            BOT_COMMANDS_EN, scope=BotCommandScopeAllPrivateChats(), language_code="en"
        ),
        bot.set_my_short_description(texts.BOT_SHORT_DESCRIPTION),
        bot.set_my_description(texts.BOT_DESCRIPTION),
        bot.set_my_short_description(texts.BOT_SHORT_DESCRIPTION_EN, language_code="en"),
        bot.set_my_description(texts.BOT_DESCRIPTION_EN, language_code="en"),
        return_exceptions=True,
    ):
        if isinstance(r, Exception):  # не критично — бот поднимется и без меню-команд/описаний
            log.warning("set_my_* (команды/описание) не удалось: %s: %s", type(r).__name__, r)
    global SCHED
    sched = None
    try:
        from scheduler.service import register_user_report_schedules, setup_scheduler

        # Плановые отчёты/аномалии/очистка просроченных черновиков. READ-ONLY: планировщик
        # НИКОГДА не меняет аккаунт (golden rule #3) — только чтение и уведомления.
        sched = setup_scheduler(bot)
        SCHED = sched  # 2.11: /myschedule применяет персональное расписание БЕЗ рестарта
        # §14 (P1-I): персональные расписания отчёта операторов (UserSettings.report_schedule) —
        # per-chat cron поверх глобального (оживляет ранее «мёртвую» колонку). Опционально, не роняем.
        try:
            await register_user_report_schedules(sched, bot)
        except Exception as e:  # noqa: BLE001 — персональные расписания опциональны
            log.warning("per-user report schedules не зарегистрированы: %s", type(e).__name__)
    except Exception as e:  # планировщик опционален — бот работает и без него
        log.warning("scheduler не запущен: %s: %s", type(e).__name__, e)
    # Fail-fast против double-import gotcha (см. алиас sys.modules у dp и блок __main__): поллить
    # dp без хендлеров = молча глотать ВСЕ апдейты; неверный порядок = catch-all on_text проглотит
    # команды/визарды. Лучше ГРОМКО упасть на старте, чем «работать» неправильно (prod-инцидент
    # 2026-07-03). Инвариант зеркалит tests/test_handler_order.py + tests/test_entrypoint_dp.py.
    _msg_handlers = [h.callback.__name__ for h in dp.message.handlers]
    if not _msg_handlers:
        raise RuntimeError(
            "dp.message без хендлеров — сломана регистрация (вероятно double-import при "
            "`python -m bot.main`: второй модуль с пустым dp). См. алиас sys.modules у dp."
        )
    if _msg_handlers[-1] != "on_text":
        _pos = _msg_handlers.index("on_text") if "on_text" in _msg_handlers else "ОТСУТСТВУЕТ"
        raise RuntimeError(
            f"catch-all on_text НЕ последний message-хендлер (позиция {_pos} из {len(_msg_handlers)}): "
            "команды/визарды будут проглочены LLM-фолбэком. Обычно это скрамбл порядка от "
            "циклического double-import при `python -m bot.main`. См. алиас sys.modules у dp."
        )
    log.info("Aimash bot запущен (polling).")
    await _notify_admins_started(bot)  # B1: readiness-пинг админам (живой сигнал успешного деплоя)
    # start_polling сам ставит обработчики SIGINT/SIGTERM и завершается штатно по сигналу;
    # finally гарантирует graceful-освобождение ресурсов (P2 lifecycle), что бы ни остановило polling.
    try:
        # Офлайн-бэклог НЕ переигрываем (drop_pending_updates): NL-команды многочасовой давности
        # на денежном пути опасны («подними бюджет», отправленный вчера, не должен ожить после
        # рестарта). Потерянный ✅-callback безопасен: claim одноразовый, пользователь нажмёт снова.
        # В aiogram 3.x у start_polling нет параметра — канонично через delete_webhook.
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:  # noqa: BLE001 — сбой очистки бэклога не должен ронять старт
            log.warning("drop_pending_updates не выполнен: %s", type(e).__name__)
        await dp.start_polling(bot)
    finally:
        if sched is not None:
            # wait=True: ДОЖИДАЕМСЯ завершения работающих джоб (они read-only и ограничены
            # ADS_TIMEOUT_S — ждать безопасно и недолго), чтобы SIGTERM не оборвал джобу на полу-
            # записи в БД (недописанный audit / висящие row-locks на Postgres). try/except — чтобы
            # зависший shutdown не заблокировал освобождение остальных ресурсов.
            try:
                sched.shutdown(wait=True)
            except Exception as e:  # noqa: BLE001 — выключение не должно ронять teardown
                log.warning("scheduler.shutdown(wait=True) сбой: %s", type(e).__name__)
        await release_single_instance_lock()  # B2: отпустить polling-lock (до закрытия пула)
        await (
            dispose_engine()
        )  # закрыть пул соединений БД (иначе на остановке висят коннекты asyncpg)
        await bot.session.close()  # закрыть HTTP-сессию Telegram
        log.info("Aimash bot остановлен (ресурсы освобождены).")


if __name__ == "__main__":
    # dp/хендлеры уже корректны: алиас sys.modules у `dp` (см. выше) регистрирует ЭТОТ модуль как
    # 'bot.main', поэтому `import bot.main` в bot/handlers/* видит ТОТ ЖЕ dp — один Dispatcher,
    # правильный порядок хендлеров. main() поллит именно его (гарды в main() это подстрахуют).
    asyncio.run(main())

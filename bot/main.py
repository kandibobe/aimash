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
import uuid

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    Message,
    TelegramObject,
)

from adcopy.generate import CopyBrief
from adcopy.generate import generate_rsa as _generate_rsa
from adcopy.refine import refine_element
from adcopy.session import SessionStore
from adcopy.validate import RSA_MIN_DESCRIPTIONS, RSA_MIN_HEADLINES
from adcopy.validate import validate as rsa_validate
from ads.assets import clear_pending_media, prepare_display_images, save_pending_media
from ads.client import DRAFT_ACCOUNT_ID
from ads.mutations import GDN_BUSINESS_NAME_MAX
from ads.resolve import find_ad_groups
from ads.service import execute_confirmed, read_before
from agent.loop import handle_command
from agent.tools.schemas import SCHEMAS
from bot import i18n, texts, ux
from bot.callbacks import CampCB, ConfirmCB, LangCB, PeriodCB, RsaCB, RsaPickCB
from bot.keyboards import (
    BOT_COMMANDS,
    BTN_CAMPAIGNS,
    BTN_HELP,
    BTN_REPORT,
    BTN_STATUS,
    campaign_actions_kb,
    campaigns_kb,
    confirm_kb,
    lang_kb,
    main_menu,
    period_kb,
    rsa_item_kb,
    rsa_overview_kb,
    rsa_pick_adgroups_kb,
    rsa_pick_campaigns_kb,
)
from bot.throttle import ThrottleMiddleware
from confirm.gate import Proposal, build_summary
from confirm.store import ConfirmStore
from core.config import settings
from core.logging import log, redact_text, setup_logging
from db.session import init_db

STORE = ConfirmStore()  # черновики + audit в БД (SQLite dev), вместо очереди в памяти
SESSIONS = SessionStore()  # сессии курации RSA (фаза 2.C), персист в proposals (rsa_curation)

# Операции со списком ключей — большой список в черновике уходит .xlsx-вложением (ТЗ §5).
_KEYWORD_OPS = frozenset({"add_keywords", "remove_keywords", "add_negative_keywords"})

# Лёгкое in-memory состояние UI (теряется при рестарте — это ок, не источник истины):
_CAMP_CACHE: dict[int, list[dict]] = {}  # chat_id → последний список кампаний (резолв idx→имя)
_LAST_PENDING: dict[int, str] = {}  # chat_id → confirmation_id последнего черновика (для /cancel)
_RSA_CAMP_CACHE: dict[int, list[dict]] = {}  # chat_id → кампании для визарда /rsa
_RSA_AG_CACHE: dict[int, list[dict]] = {}  # chat_id → группы объявлений для визарда /rsa


class RsaWizard(StatesGroup):
    awaiting_brief = State()  # ждём «тематика | url» для генерации


class RsaRefine(StatesGroup):
    awaiting_text = State()  # ждём правку для доработки одного элемента


class KwWizard(StatesGroup):
    awaiting_seeds = State()  # ждём сид-слова и/или URL для подбора ключей


class GdnWizard(StatesGroup):
    awaiting_brief = State()  # ждём «название | url | бюджет» после приёма фото


dp = Dispatcher()


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


class WhitelistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data):
        # Fail-closed (как ads.client.ensure_allowed): пустой whitelist => блок ВСЕХ, а не fail-open.
        # uid=None (callback без message / вход без чата) тоже не в наборе => блок. Круг — через .env.
        uid = _event_chat_id(event)
        wl = settings.whitelist
        if uid not in wl:
            log.warning("заблокирован chat_id %s (не в whitelist)", uid)
            return
        return await handler(event, data)


# ── Общие действия (чтобы команда и кнопка делали одно и то же) ─────────────────
async def _send_help(message: Message) -> None:
    await message.answer(texts.HELP, parse_mode=ParseMode.HTML)


async def _send_status(message: Message) -> None:
    """Быстрая сводка по аккаунту за 30 дн. (read-only, без подтверждения)."""
    try:
        from ads.client import build_client
        from ads.read import account_stats

        client = build_client()
        async with ux.typing_action(message):  # «печатает…» пока идёт чтение SDK
            st = await asyncio.to_thread(account_stats, client, DRAFT_ACCOUNT_ID, 30)
            cur = await _read_currency(client)  # §9: валюта в денежных строках
    except Exception as e:  # сеть/доступ/SDK
        await message.answer(f"⚠️ Не удалось получить статистику: {ux.err_text(e)}")
        return
    await message.answer(
        texts.fmt_stats(
            DRAFT_ACCOUNT_ID,
            30,
            {
                "impressions": st.impressions,
                "clicks": st.clicks,
                "cost": round(st.cost, 2),
                "conversions": st.conversions,
                "conv_value": round(st.conv_value, 2),
            },
            cur,
        ),
        parse_mode=ParseMode.HTML,
    )


async def _send_campaigns(message: Message, chat_id: int) -> None:
    """Список кампаний + inline-кнопки выбора. Кэшируем список по chat_id для резолва idx→имя."""
    try:
        from ads.client import build_client
        from ads.read import list_campaigns

        client = build_client()
        async with ux.typing_action(message):
            camps = await asyncio.to_thread(list_campaigns, client, DRAFT_ACCOUNT_ID)
    except Exception as e:  # сеть/доступ/SDK
        await message.answer(f"⚠️ Не удалось получить кампании: {ux.err_text(e)}")
        return
    if not camps:
        await message.answer(texts.NO_CAMPAIGNS)
        return
    _CAMP_CACHE[chat_id] = camps
    await message.answer(
        texts.campaigns_title(DRAFT_ACCOUNT_ID),
        reply_markup=campaigns_kb(camps),
        parse_mode=ParseMode.HTML,
    )


def _build_proposal(operation: str, **args) -> tuple[str, str, dict, str]:
    """Детерминированно собрать черновик мутации (как agent.loop, но БЕЗ LLM) — для кнопок.
    Валидация схемой обязательна. Возвращает (confirmation_id, operation, params, summary)."""
    validated = SCHEMAS[operation](**args)
    params = validated.model_dump()
    summary = build_summary(operation, before="[текущее значение из Google Ads]", after=params)
    p = Proposal(operation=operation, summary=summary, params=params, chat_id=0)
    return p.confirmation_id, operation, params, summary


async def _present_proposal(
    message: Message, *, chat_id: int, operation: str, params: dict, summary: str, cid: str
) -> None:
    """Сохранить черновик и показать с кнопками ✅/❌. user_initiated=True ставит ДОВЕРЕННЫЙ слой
    (входящее действие whitelisted-человека), НЕ агент про себя (golden rule #3, fail-closed)."""
    # §5: читаем ТЕКУЩЕЕ значение (бюджет/ставку/статус) ДО показа → реальный diff «было → станет»
    # и снимок-база для оптимистичной сверки при исполнении (TOCTOU). read_before fail-safe (None).
    async with ux.typing_action(message):
        before = await read_before(operation, params)
    if before is not None:
        params = {**params, "_before": before}  # инертно для execute (apply_* читают свои ключи)
    # Человекочитаемая сводка по operation+params (деньги — реальное «40.00 → 48.00 (+20%)»).
    # Для create_rsa/create_gdn у вызывающего свой богатый summary → fmt вернёт "".
    display = texts.fmt_mutation_summary(operation, params) or summary
    await STORE.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=DRAFT_ACCOUNT_ID,
        params=params,
        summary=display,
        chat_id=chat_id,
        user_initiated=True,
    )
    _LAST_PENDING[chat_id] = cid
    # Большой список ключей/минус-слов (ТЗ §5) → полный список .xlsx-вложением, кнопки на коротком
    # сообщении; в самой сводке список усечён до KW_INLINE_MAX с пометкой «…ещё N во вложении».
    kws = params.get("keywords") if isinstance(params, dict) else None
    if operation in _KEYWORD_OPS and isinstance(kws, list) and len(kws) > texts.KW_INLINE_MAX:
        await ux.send_proposal_keywords_xlsx(
            message,
            keywords=kws,
            match_type=params.get("match_type", ""),
            action=texts.keyword_action_label(operation),
            header_html=texts.PROPOSAL_PENDING.format(summary=texts.esc(display)),
            reply_markup=confirm_kb(cid),
            parse_mode=ParseMode.HTML,
        )
        return
    rendered = texts.PROPOSAL_PENDING.format(summary=texts.esc(display))
    if ux.proposal_fits(rendered):
        await message.answer(rendered, reply_markup=confirm_kb(cid), parse_mode=ParseMode.HTML)
    else:
        # Длинный черновик (напр. RSA с 15 заголовками) не влезает в одно сообщение Telegram →
        # полный текст .txt-вложением, а кнопки ✅/❌ на коротком сообщении (его правит _do_confirm).
        await ux.send_proposal_text(
            message,
            full_text=display,
            header_html="📝 <b>Черновик изменения</b> — полный текст во вложении ⬆️\n\nПодтвердить?",
            reply_markup=confirm_kb(cid),
            parse_mode=ParseMode.HTML,
        )


# ── Команды ──────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def start(m: Message) -> None:
    await m.answer(texts.START, reply_markup=main_menu(), parse_mode=ParseMode.HTML)


@dp.message(Command("help"))
async def help_(m: Message) -> None:
    await _send_help(m)


@dp.message(Command("status"))
async def status_(m: Message) -> None:
    await _send_status(m)


@dp.message(Command("campaigns"))
async def campaigns_(m: Message) -> None:
    """Read-only список кампаний с быстрыми действиями (точные имена нужны для ставки/ключей)."""
    await _send_campaigns(m, m.chat.id)


@dp.message(Command("keywords"))
async def keywords_(m: Message, state: FSMContext, command: CommandObject) -> None:
    """Подбор ключевых слов (read-only, advisory). С аргументами — сразу; иначе спросить."""
    await state.clear()
    seeds, url = _parse_kw_input(command.args or "")
    if seeds or url:
        await _kw_run(m, m.chat.id, seeds, url, "ru")
        return
    await state.set_state(KwWizard.awaiting_seeds)
    await m.answer(texts.KW_ASK, parse_mode=ParseMode.HTML)


@dp.message(Command("cancel"))
async def cancel_cmd(m: Message) -> None:
    cid = _LAST_PENDING.get(m.chat.id)
    if not cid:
        await m.answer(texts.NO_PROPOSAL)
        return
    actor_id, actor_name = _actor(m)
    await STORE.reject(cid, chat_id=m.chat.id, actor_user_id=actor_id, actor_username=actor_name)
    _LAST_PENDING.pop(m.chat.id, None)
    await m.answer(texts.REJECTED)


@dp.message(Command("lang"))
async def lang_cmd(m: Message, command: CommandObject) -> None:
    """Язык интерфейса RU/EN. С аргументом (/lang en) — сразу; иначе кнопки выбора."""
    arg = (command.args or "").strip().lower()
    if arg in i18n.LANGS:
        lang = i18n.set_lang(m.chat.id, arg)
        await m.answer(i18n.t("lang_set", lang))
        return
    await m.answer(i18n.t("lang_pick", i18n.get_lang(m.chat.id)), reply_markup=lang_kb())


@dp.callback_query(LangCB.filter())
async def on_lang(cq: CallbackQuery, callback_data: LangCB) -> None:
    lang = i18n.set_lang(_cq_chat_id(cq), callback_data.code)
    await cq.answer()
    try:
        await cq.message.edit_text(i18n.t("lang_set", lang))
    except TelegramBadRequest:
        pass


async def _slash_mutate(m: Message, command: CommandObject, operation: str) -> None:
    """Слэш-команда паузы/возобновления по имени кампании → черновик за confirm-гейтом
    (тот же путь, что inline-кнопка и текстовая команда). Без имени — подсказка."""
    name = (command.args or "").strip()
    if not name:
        cmd = "pause" if operation == "pause_campaign" else "resume"
        verb = "приостановить" if operation == "pause_campaign" else "возобновить"
        await m.answer(
            f"Укажи кампанию: <code>/{cmd} Название кампании</code> — чтобы {verb}.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        cid, op, params, summary = _build_proposal(operation, campaign=name)
    except Exception as e:  # валидация схемы
        await m.answer(f"⚠️ {ux.err_text(e)}")
        return
    await _present_proposal(
        m, chat_id=m.chat.id, operation=op, params=params, summary=summary, cid=cid
    )


@dp.message(Command("pause"))
async def pause_(m: Message, command: CommandObject) -> None:
    """ТЗ §6: /pause <кампания> — поставить на паузу (через confirm-гейт)."""
    await _slash_mutate(m, command, "pause_campaign")


@dp.message(Command("resume"))
async def resume_(m: Message, command: CommandObject) -> None:
    """ТЗ §6: /resume <кампания> — возобновить (через confirm-гейт)."""
    await _slash_mutate(m, command, "resume_campaign")


async def _read_currency(client) -> str:
    """Код валюты аккаунта (§9) для денежных метрик. '' при сбое чтения — отчёт/статистику
    показываем и без валюты (не блокируем). Кэш — в ads.read.account_currency."""
    from ads.read import account_currency

    try:
        return await asyncio.to_thread(account_currency, client, DRAFT_ACCOUNT_ID)
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


@dp.message(Command("report"))
async def report_(m: Message, command: CommandObject) -> None:
    """Read-only сводка по аккаунту за период (итоги + сравнение + топ-кампании)."""
    try:
        period = _period_from_arg(command.args)
    except ValueError as e:
        await m.answer(f"⚠️ {e}")
        return
    try:
        from ads.client import build_client
        from reports.service import build_account_report, summary_text

        client = build_client()
        async with ux.typing_action(m):
            report = await asyncio.to_thread(build_account_report, client, DRAFT_ACCOUNT_ID, period)
            report.currency = await _read_currency(client)  # §9: валюта денежных метрик
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(f"⚠️ Не удалось построить отчёт: {ux.err_text(e)}")
        return
    await m.answer(summary_text(report))


@dp.message(Command("export"))
async def export_(m: Message, command: CommandObject) -> None:
    """Глубокий отчёт .xlsx (разбивки ТЗ §9) вложением. Read-only."""
    import os
    import tempfile

    try:
        period = _period_from_arg(command.args)
    except ValueError as e:
        await m.answer(f"⚠️ {e}")
        return
    await m.answer("Готовлю .xlsx-отчёт…")
    path: str | None = None
    try:
        from ads.client import build_client
        from reports.service import build_account_report
        from reports.xlsx import write_report_xlsx

        client = build_client()
        async with ux.upload_action(m):  # «отправляет документ…» пока строим .xlsx
            report = await asyncio.to_thread(build_account_report, client, DRAFT_ACCOUNT_ID, period)
            report.currency = await _read_currency(client)  # §9: валюта денежных метрик
            fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_report_")
            os.close(fd)
            await asyncio.to_thread(write_report_xlsx, report, path)
        fname = f"aimash_{DRAFT_ACCOUNT_ID}_{period.date_from}_{period.date_to}.xlsx"
        await m.answer_document(FSInputFile(path, filename=fname))
    except Exception as e:  # сеть/доступ/SDK/openpyxl
        await m.answer(f"⚠️ Не удалось сформировать отчёт: {ux.err_text(e)}")
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@dp.message(Command("sheets"))
async def sheets_(m: Message, command: CommandObject) -> None:
    """ТЗ §9: глубокий отчёт в Google Sheets (новая таблица + вкладка на разбивку, ссылка). Read-only."""
    try:
        period = _period_from_arg(command.args)
    except ValueError as e:
        await m.answer(f"⚠️ {e}")
        return
    await m.answer("Готовлю Google Sheets-отчёт…")
    try:
        from ads.client import build_client
        from reports.service import build_account_report
        from reports.sheets import publish_report_to_sheets

        client = build_client()
        async with ux.typing_action(m):
            report = await asyncio.to_thread(build_account_report, client, DRAFT_ACCOUNT_ID, period)
            report.currency = await _read_currency(client)  # §9: валюта денежных метрик
            url = await asyncio.to_thread(publish_report_to_sheets, report)
    except Exception as e:  # сеть/доступ/SDK/нет OAuth-scope Sheets
        await m.answer(
            f"⚠️ Не удалось выгрузить в Google Sheets: {ux.err_text(e)}\n"
            "Возможно, у OAuth-токена нет scope drive.file — см. docs/DEPLOYMENT.md (Google Sheets)."
        )
        return
    await m.answer(f"✅ Google Sheets готов: {url}")


# ── Reply-кнопки (ОБЯЗАТЕЛЬНО до общего F.text-хендлера — иначе перехватит on_text) ─
@dp.message(F.text == BTN_STATUS)
async def btn_status(m: Message) -> None:
    await _send_status(m)


@dp.message(F.text == BTN_CAMPAIGNS)
async def btn_campaigns(m: Message) -> None:
    await _send_campaigns(m, m.chat.id)


@dp.message(F.text == BTN_HELP)
async def btn_help(m: Message) -> None:
    await _send_help(m)


@dp.message(F.text == BTN_REPORT)
async def btn_report(m: Message) -> None:
    await m.answer("📈 За какой период построить сводку?", reply_markup=period_kb("report"))


# ── RSA-генерация (фаза 2.C): /rsa визард → курация → confirm-гейт create_rsa ─────
def _rsa_render(session) -> tuple[str, object]:
    """По состоянию сессии: следующий pending-элемент с кнопками либо итоговый экран."""
    nxt = session.next_pending()
    if nxt is None:
        h, d = session.counts()
        text = texts.fmt_rsa_overview(h, d, len(session.headlines), len(session.descriptions))
        return text, rsa_overview_kb(session.session_id, session.can_finalize(), has_pending=False)
    kind, idx = nxt
    items = session.items(kind)
    text = texts.fmt_rsa_element(
        kind, idx, len(items), items[idx], session.campaign, session.ad_group_name
    )
    return text, rsa_item_kb(session.session_id, kind, idx)


def _rsa_overview(session) -> tuple[str, object]:
    h, d = session.counts()
    has_pending = session.next_pending() is not None
    text = texts.fmt_rsa_overview(h, d, len(session.headlines), len(session.descriptions))
    return text, rsa_overview_kb(session.session_id, session.can_finalize(), has_pending)


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
    await target.answer(texts.RSA_ASK_BRIEF, parse_mode=ParseMode.HTML)


async def _rsa_resolve_after_campaign(
    target: Message, chat_id: int, campaign: str, state: FSMContext
) -> None:
    """Кампания выбрана/задана: резолвим группы. 1 → дальше; >1 → выбор; 0 → ошибка."""
    from ads.client import build_client

    await state.update_data(campaign=campaign)
    try:
        client = build_client()
        groups = await asyncio.to_thread(find_ad_groups, client, DRAFT_ACCOUNT_ID, campaign)
    except Exception as e:  # сеть/доступ/SDK
        await target.answer(f"⚠️ Не удалось получить группы: {ux.err_text(e)}")
        return
    if not groups:
        await target.answer(texts.RSA_NO_ADGROUPS)
        await state.clear()
        return
    if len(groups) == 1:
        g = groups[0]
        await state.update_data(ad_group_id=str(g.id), ad_group_name=g.name)
        await _rsa_after_adgroup(target, chat_id, state)
        return
    _RSA_AG_CACHE[chat_id] = [{"id": str(g.id), "name": g.name} for g in groups]
    await target.answer(
        texts.RSA_PICK_ADGROUP, reply_markup=rsa_pick_adgroups_kb(_RSA_AG_CACHE[chat_id])
    )


async def _rsa_generate_and_start(target: Message, chat_id: int, state: FSMContext) -> None:
    """Сгенерировать тексты по брифу из state, создать сессию курации, показать итог."""
    data = await state.get_data()
    brief = CopyBrief(
        topic=data.get("topic", ""),
        keywords=list(data.get("keywords") or []),
        usp=data.get("usp"),
        tone=data.get("tone"),
        geo=data.get("geo"),
        language=data.get("language", "ru"),
        n_headlines=int(data.get("n_headlines") or 15),
        n_descriptions=int(data.get("n_descriptions") or 4),
    )
    await target.answer(texts.RSA_GENERATING)
    try:
        async with ux.typing_action(target):
            draft = await _generate_rsa(brief)
    except Exception as e:  # LLM/сеть
        await target.answer(f"⚠️ Генерация не удалась: {ux.err_text(e)}")
        await state.clear()
        return
    # Диагностика: сколько набрано/отброшено КОДОМ за длину (golden rule #4) — раньше терялось.
    await target.answer(ux.fmt_rsa_diagnostics(draft, brief.n_headlines, brief.n_descriptions))
    if len(draft.headlines) < RSA_MIN_HEADLINES or len(draft.descriptions) < RSA_MIN_DESCRIPTIONS:
        await target.answer(texts.RSA_GEN_EMPTY)
        await state.clear()
        return
    session_id = await SESSIONS.create(
        chat_id=chat_id,
        customer_id=DRAFT_ACCOUNT_ID,
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
    text, kb = _rsa_overview(session)  # старт с итога: «Применить все валидные» / «по одному»
    await target.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


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
        from ads.client import build_client
        from ads.read import list_campaigns

        client = build_client()
        camps = await asyncio.to_thread(list_campaigns, client, DRAFT_ACCOUNT_ID)
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(f"⚠️ Не удалось получить кампании: {ux.err_text(e)}")
        return
    if not camps:
        await m.answer(texts.NO_CAMPAIGNS)
        return
    _RSA_CAMP_CACHE[m.chat.id] = camps
    await m.answer(
        texts.RSA_PICK_CAMPAIGN,
        reply_markup=rsa_pick_campaigns_kb(camps),
        parse_mode=ParseMode.HTML,
    )


# ── Keyword research (Фаза 3, БЛОК E): подбор + кластеризация + .xlsx ─────────────
def _parse_kw_input(text: str) -> tuple[list[str], str | None]:
    """Ввод → (сиды, url): токены через запятую/перенос — сиды; первый http(s)-токен — url."""
    parts = [p.strip() for p in (text or "").replace("\n", ",").split(",") if p.strip()]
    url: str | None = None
    seeds: list[str] = []
    for p in parts:
        if url is None and p.startswith(("http://", "https://")):
            url = p
        else:
            seeds.append(p)
    return seeds, url


async def _kw_run(
    target: Message, chat_id: int, seeds: list[str], url: str | None, language: str
) -> None:
    """Подобрать идеи → кластеризовать по интенту → сводка + .xlsx. READ-ONLY (advisory)."""
    import os
    import tempfile

    await target.answer(texts.KW_SEARCHING)
    try:
        from ads.client import build_client
        from ads.keyword_plan import generate_keyword_ideas

        client = build_client()
        ideas = await asyncio.to_thread(
            generate_keyword_ideas,
            client,
            DRAFT_ACCOUNT_ID,
            seeds=seeds,
            url=url,
            language=language,
        )
    except Exception as e:  # сеть/доступ/SDK/валидация ввода
        await target.answer(f"⚠️ Не удалось подобрать ключи: {ux.err_text(e)}")
        return
    if not ideas:
        await target.answer(texts.KW_EMPTY)
        return

    from keywords.cluster import Cluster, cluster_keywords

    try:
        clusters = await cluster_keywords([i.text for i in ideas], language)
    except Exception:  # кластеризация не критична — fallback на одну группу
        clusters = [Cluster(name="Все ключи", keywords=[i.text for i in ideas])]

    by_text = {i.text: i.avg_monthly_searches for i in ideas}
    src = ", ".join(seeds) or (url or "")
    await target.answer(
        texts.fmt_keywords_summary(clusters, by_text, len(ideas), src),
        parse_mode=ParseMode.HTML,
    )

    path: str | None = None
    try:
        from keywords.export import write_keywords_xlsx

        fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_keywords_")
        os.close(fd)
        await asyncio.to_thread(
            write_keywords_xlsx, clusters, ideas, path, seeds=seeds, url=url, language=language
        )
        await target.answer_document(FSInputFile(path, filename="aimash_keywords.xlsx"))
    except Exception as e:  # openpyxl/IO
        await target.answer(f"⚠️ Таблицу .xlsx сформировать не удалось: {ux.err_text(e)}")
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def _kw_start_from_intent(m: Message, brief: dict, state: FSMContext) -> None:
    """NL-вход: keyword_research-намерение агента. Есть сиды/URL — сразу; иначе спросить."""
    await state.clear()
    seeds = [s for s in (brief.get("seeds") or []) if s]
    url = brief.get("url")
    language = brief.get("language", "ru")
    if seeds or url:
        await _kw_run(m, m.chat.id, seeds, url, language)
        return
    await state.set_state(KwWizard.awaiting_seeds)
    await m.answer(texts.KW_ASK, parse_mode=ParseMode.HTML)


@dp.message(Command("rsa"))
async def rsa_cmd(m: Message, state: FSMContext) -> None:
    """Визард генерации RSA: выбор кампании → группы → бриф → курация → создание."""
    await state.clear()
    try:
        from ads.client import build_client
        from ads.read import list_campaigns

        client = build_client()
        camps = await asyncio.to_thread(list_campaigns, client, DRAFT_ACCOUNT_ID)
    except Exception as e:  # сеть/доступ/SDK
        await m.answer(f"⚠️ Не удалось получить кампании: {ux.err_text(e)}")
        return
    if not camps:
        await m.answer(texts.NO_CAMPAIGNS)
        return
    _RSA_CAMP_CACHE[m.chat.id] = camps
    await m.answer(
        texts.RSA_PICK_CAMPAIGN,
        reply_markup=rsa_pick_campaigns_kb(camps),
        parse_mode=ParseMode.HTML,
    )


@dp.callback_query(RsaPickCB.filter(F.what == "camp"))
async def rsa_pick_camp(cq: CallbackQuery, callback_data: RsaPickCB, state: FSMContext) -> None:
    camps = _RSA_CAMP_CACHE.get(_cq_chat_id(cq))
    if not camps or callback_data.idx >= len(camps):
        await cq.answer(texts.CAMP_LIST_STALE, show_alert=True)
        return
    await cq.answer()
    await _rsa_resolve_after_campaign(
        cq.message, _cq_chat_id(cq), camps[callback_data.idx]["name"], state
    )


@dp.callback_query(RsaPickCB.filter(F.what == "ag"))
async def rsa_pick_ag(cq: CallbackQuery, callback_data: RsaPickCB, state: FSMContext) -> None:
    groups = _RSA_AG_CACHE.get(_cq_chat_id(cq))
    if not groups or callback_data.idx >= len(groups):
        await cq.answer(texts.CAMP_LIST_STALE, show_alert=True)
        return
    await cq.answer()
    g = groups[callback_data.idx]
    await state.update_data(ad_group_id=str(g["id"]), ad_group_name=g["name"])
    await _rsa_after_adgroup(cq.message, _cq_chat_id(cq), state)


# FSM-хендлеры сообщений — ОБЯЗАТЕЛЬНО до общего on_text (иначе перехватит свободный текст).
@dp.message(RsaWizard.awaiting_brief)
async def rsa_brief(m: Message, state: FSMContext) -> None:
    topic, url = _parse_brief_text(m.text or "")
    if not url or not topic:
        await m.answer(texts.RSA_BAD_URL)
        return
    await state.update_data(topic=topic, final_url=url)
    await _rsa_generate_and_start(m, m.chat.id, state)


@dp.message(RsaRefine.awaiting_text)
async def rsa_refine_text(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cid, kind, idx = data.get("cid"), data.get("kind"), data.get("idx")
    session = await SESSIONS.get(cid) if cid else None
    if session is None or kind is None or idx is None:
        await state.clear()
        await m.answer(texts.RSA_SESSION_STALE)
        return
    try:
        new_text = await refine_element(session.brief, kind, m.text or "")
    except Exception as e:  # LLM/сеть
        await m.answer(f"⚠️ Доработка не удалась: {ux.err_text(e)}")
        return
    valid_kind = "headline" if kind == "h" else "description"
    ok, n = rsa_validate(new_text, valid_kind)
    if not ok:
        limit = 30 if kind == "h" else 90
        await m.answer(texts.RSA_REFINE_TOO_LONG.format(n=n, limit=limit))
        return  # остаёмся в состоянии — пользователь пришлёт другую правку
    await state.clear()
    session = await SESSIONS.replace_element(cid, kind, idx, new_text)
    items = session.items(kind)
    await m.answer(
        texts.fmt_rsa_element(
            kind, idx, len(items), items[idx], session.campaign, session.ad_group_name
        ),
        reply_markup=rsa_item_kb(cid, kind, idx),
        parse_mode=ParseMode.HTML,
    )


@dp.message(KwWizard.awaiting_seeds)
async def kw_seeds(m: Message, state: FSMContext) -> None:
    seeds, url = _parse_kw_input(m.text or "")
    if not seeds and not url:
        await m.answer(texts.KW_BAD_INPUT, parse_mode=ParseMode.HTML)
        return  # остаёмся в состоянии — пользователь пришлёт сиды/URL ещё раз
    await state.clear()
    await _kw_run(m, m.chat.id, seeds, url, "ru")


# ── GDN из фото (§11): приём фото → бриф → черновик за confirm-гейтом ─────────────
def _parse_gdn_brief(text: str) -> tuple[str, str, float] | None:
    """«название | url | бюджет» → (name, url, budget_units). None при неверном формате."""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) != 3:
        return None
    name, url, budget_s = parts
    if not name or not url.startswith(("http://", "https://")):
        return None
    try:
        budget = float(budget_s.replace(",", "."))
    except ValueError:
        return None
    # Верхняя граница (1e6 единиц = 1e12 micros, лимит схемы) — отвергаем СРАЗУ, без «1e9» и
    # путаного Pydantic-сообщения после генерации текстов (golden rule #4: считает КОД).
    if budget <= 0 or budget > 1_000_000:
        return None
    return (name, url, budget)


async def _gdn_cleanup(state: FSMContext, media_id: str | None) -> None:
    """Сброс визарда GDN + удаление временных файлов медиа (без утечки на путях ошибок)."""
    await state.clear()
    if media_id:
        await asyncio.to_thread(clear_pending_media, media_id)


@dp.message(F.photo)
async def on_photo(m: Message, state: FSMContext, bot: Bot) -> None:
    """Приём фото для GDN-кампании (§11): скачиваем, режем в 1.91:1 + 1:1, запускаем визард.
    Бинарь НЕ в proposal/логах — подготовленные кадры кладём во временное хранилище по media_id."""
    prev = await state.get_data()  # новое фото отменяет прежнее → чистим его медиа (без утечки)
    old_id = prev.get("gdn_media_id")
    if old_id:
        await asyncio.to_thread(clear_pending_media, old_id)
    await state.clear()
    try:
        buf = io.BytesIO()
        await bot.download(m.photo[-1], destination=buf)  # самое большое разрешение
        landscape, square = await asyncio.to_thread(prepare_display_images, buf.getvalue())
    except Exception as e:  # сеть/битый файл/не картинка
        await m.answer(f"⚠️ Не удалось обработать фото: {ux.err_text(e)}")
        return
    media_id = uuid.uuid4().hex
    await asyncio.to_thread(save_pending_media, media_id, landscape, square)
    await state.update_data(gdn_media_id=media_id)
    await state.set_state(GdnWizard.awaiting_brief)
    await m.answer(texts.GDN_ASK_BRIEF, parse_mode=ParseMode.HTML)


@dp.message(GdnWizard.awaiting_brief)
async def gdn_brief(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    media_id = data.get("gdn_media_id")
    if not media_id:
        await state.clear()
        await m.answer(texts.GDN_SESSION_STALE)
        return
    parsed = _parse_gdn_brief(m.text or "")
    if parsed is None:
        await m.answer(texts.GDN_BAD_BRIEF, parse_mode=ParseMode.HTML)
        return  # остаёмся в состоянии — пользователь пришлёт бриф ещё раз
    name, url, budget_units = parsed
    await m.answer(texts.GDN_GENERATING)
    try:
        draft = await _generate_rsa(CopyBrief(topic=name, n_headlines=5, n_descriptions=4))
    except Exception as e:  # LLM/сеть
        await _gdn_cleanup(state, media_id)
        await m.answer(f"⚠️ Генерация текстов не удалась: {ux.err_text(e)}")
        return
    # Нужно ≥1 заголовка и ≥2 описаний: первое описание идёт в long_headline, остальные — в
    # descriptions (иначе long_headline дублировал бы единственное описание).
    if not draft.headlines or len(draft.descriptions) < 2:
        await _gdn_cleanup(state, media_id)
        await m.answer(texts.GDN_GEN_EMPTY)
        return
    headlines = draft.headlines[:5]
    all_desc = draft.descriptions[:5]
    long_headline = all_desc[0]  # ≤90, уже провалидировано генератором
    descriptions = all_desc[1:]  # ≥1 (len(all_desc) ≥ 2) — без дубля long_headline
    business_name = name[:GDN_BUSINESS_NAME_MAX]
    budget_micros = int(round(budget_units * 1_000_000))
    params = {
        "campaign_name": name,
        "headlines": headlines,
        "long_headline": long_headline,
        "descriptions": descriptions,
        "business_name": business_name,
        "final_url": url,
        "budget_daily_micros": budget_micros,
        "media_id": media_id,
    }
    try:  # defense-in-depth: схема ещё раз проверит длины/составы/URL/бюджет/media_id
        SCHEMAS["create_gdn_campaign"](**params)
    except Exception as e:
        await _gdn_cleanup(state, media_id)
        await m.answer(f"⚠️ Параметры не прошли валидацию: {e}")
        return
    summary = texts.fmt_gdn_proposal_summary(
        name, url, budget_units, headlines, descriptions, business_name
    )
    p = Proposal(operation="create_gdn_campaign", summary=summary, params=params, chat_id=m.chat.id)
    await state.clear()
    await _present_proposal(
        m,
        chat_id=m.chat.id,
        operation="create_gdn_campaign",
        params=params,
        summary=summary,
        cid=p.confirmation_id,
    )


# ── Свободный текст → агент ───────────────────────────────────────────────────────
@dp.message(F.text)
async def on_text(m: Message, state: FSMContext) -> None:
    async with ux.typing_action(m):  # «печатает…» пока модель парсит команду
        res = await handle_command(m.text, chat_id=m.chat.id)
    t = res.get("type")
    if t == "proposal":
        await _present_proposal(
            m,
            chat_id=m.chat.id,
            operation=res["operation"],
            params=res.get("params", {}),
            summary=res["summary"],
            cid=res["confirmation_id"],
        )
    elif t == "rsa_intent":
        await _rsa_start_from_intent(m, res.get("brief", {}), state)
    elif t == "keywords_intent":
        await _kw_start_from_intent(m, res.get("brief", {}), state)
    elif t == "clarify":
        await m.answer("❓ " + res["question"])
    elif t == "read":
        await m.answer(
            texts.fmt_stats(
                res.get("account", ""),
                res.get("days", 30),
                res.get("stats", {}),
                res.get("currency", ""),
            ),
            parse_mode=ParseMode.HTML,
        )
    else:
        await m.answer(res.get("text", "(пусто)"))


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


@dp.callback_query(CampCB.filter(F.action == "menu"))
async def camp_menu(cq: CallbackQuery, callback_data: CampCB) -> None:
    camps = _CAMP_CACHE.get(_cq_chat_id(cq))
    if not camps or callback_data.idx >= len(camps):
        await cq.answer(texts.CAMP_LIST_STALE, show_alert=True)
        return
    await cq.answer()
    c = camps[callback_data.idx]
    try:
        await cq.message.edit_text(
            texts.fmt_campaign_header(c),
            reply_markup=campaign_actions_kb(callback_data.idx, c.get("status", "")),
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(CampCB.filter(F.action == "back"))
async def camp_back(cq: CallbackQuery, callback_data: CampCB) -> None:
    camps = _CAMP_CACHE.get(_cq_chat_id(cq))
    if not camps:
        await cq.answer(texts.CAMP_LIST_STALE, show_alert=True)
        return
    await cq.answer()
    try:
        await cq.message.edit_text(
            texts.campaigns_title(DRAFT_ACCOUNT_ID),
            reply_markup=campaigns_kb(camps),
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass


async def _camp_mutate(cq: CallbackQuery, idx: int, operation: str) -> None:
    """Кнопка пауза/возобновление: СОЗДАЁТ черновик (как текстовая команда) → confirm-гейт.
    НЕ исполняет мутацию напрямую — только после ✅ через ту же ветку, что и on_text."""
    chat_id = _cq_chat_id(cq)
    camps = _CAMP_CACHE.get(chat_id)
    if not camps or idx >= len(camps):
        await cq.answer(texts.CAMP_LIST_STALE, show_alert=True)
        return
    name = camps[idx]["name"]
    try:
        cid, op, params, summary = _build_proposal(operation, campaign=name)
    except Exception as e:  # валидация схемы
        await cq.answer(f"Ошибка: {type(e).__name__}", show_alert=True)
        return
    await cq.answer()
    await _present_proposal(
        cq.message, chat_id=chat_id, operation=op, params=params, summary=summary, cid=cid
    )


@dp.callback_query(CampCB.filter(F.action == "pause"))
async def camp_pause(cq: CallbackQuery, callback_data: CampCB) -> None:
    await _camp_mutate(cq, callback_data.idx, "pause_campaign")


@dp.callback_query(CampCB.filter(F.action == "resume"))
async def camp_resume(cq: CallbackQuery, callback_data: CampCB) -> None:
    await _camp_mutate(cq, callback_data.idx, "resume_campaign")


# ── Inline: выбор периода для отчёта ──────────────────────────────────────────────
@dp.callback_query(PeriodCB.filter(F.target == "report"))
async def period_report(cq: CallbackQuery, callback_data: PeriodCB) -> None:
    await cq.answer()
    try:
        period = _period_from_arg(callback_data.code)
    except ValueError as e:
        await cq.message.answer(f"⚠️ {e}")
        return
    try:
        from ads.client import build_client
        from reports.service import build_account_report, summary_text

        client = build_client()
        async with ux.typing_action(cq.message):
            report = await asyncio.to_thread(build_account_report, client, DRAFT_ACCOUNT_ID, period)
            report.currency = await _read_currency(client)  # §9: валюта денежных метрик
    except Exception as e:  # сеть/доступ/SDK
        await cq.message.answer(f"⚠️ Не удалось построить отчёт: {ux.err_text(e)}")
        return
    await cq.message.answer(summary_text(report))


# ── Inline: подтверждение/отмена черновика (confirm-гейт) ─────────────────────────
async def _do_confirm(cq: CallbackQuery, cid: str) -> None:
    chat_id = _cq_chat_id(cq)
    actor_id, actor_name = _actor(cq)
    if not await STORE.confirm(
        cid, chat_id=chat_id, actor_user_id=actor_id, actor_username=actor_name
    ):
        await cq.answer(texts.STALE, show_alert=True)
        return
    _LAST_PENDING.pop(chat_id, None)
    await cq.answer("Выполняю…")
    try:
        await cq.message.edit_text(texts.EXECUTING)  # убирает кнопки
    except TelegramBadRequest:
        pass
    try:
        result = await execute_confirmed(STORE, cid)
        log.info("мутация применена cid=%s chat=%s", cid, chat_id)  # денежный путь — успех в лог
        await cq.message.edit_text(
            texts.APPLIED.format(result=texts.esc(result)), parse_mode=ParseMode.HTML
        )
    except Exception as e:  # доступ/резолв/SDK
        # Денежный путь: пишем в лог С traceback (RedactionFilter чистит секреты) — раньше молчал;
        # в audit_log кладём УЖЕ редактированный текст (str(e) от SDK/google.auth может нести креды).
        log.error(
            "исполнение мутации провалено cid=%s chat=%s: %s",
            cid,
            chat_id,
            type(e).__name__,
            exc_info=e,
        )
        await STORE.record_failure(cid, error=redact_text(str(e)))
        try:
            await cq.message.edit_text(
                texts.FAILED.format(kind=type(e).__name__, err=texts.esc(redact_text(str(e)))),
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest:
            pass


async def _do_cancel(cq: CallbackQuery, cid: str) -> None:
    chat_id = _cq_chat_id(cq)
    actor_id, actor_name = _actor(cq)
    await STORE.reject(cid, chat_id=chat_id, actor_user_id=actor_id, actor_username=actor_name)
    _LAST_PENDING.pop(chat_id, None)
    try:
        await cq.message.edit_text(texts.REJECTED)
    except TelegramBadRequest:
        pass
    await cq.answer("Отменено")


@dp.callback_query(ConfirmCB.filter(F.action == "ok"))
async def on_confirm(cq: CallbackQuery, callback_data: ConfirmCB) -> None:
    await _do_confirm(cq, callback_data.cid)


@dp.callback_query(ConfirmCB.filter(F.action == "no"))
async def on_cancel(cq: CallbackQuery, callback_data: ConfirmCB) -> None:
    await _do_cancel(cq, callback_data.cid)


# Legacy-fallback: старые сообщения с "ok:/no:" (до рестарта). После переходного периода удалить.
@dp.callback_query(F.data.startswith("ok:"))
async def on_confirm_legacy(cq: CallbackQuery) -> None:
    await _do_confirm(cq, cq.data[3:])


@dp.callback_query(F.data.startswith("no:"))
async def on_cancel_legacy(cq: CallbackQuery) -> None:
    await _do_cancel(cq, cq.data[3:])


# ── Inline: курация RSA (поэлементно + массово) ───────────────────────────────────
async def _rsa_edit(cq: CallbackQuery, session) -> None:
    """Перерисовать текущий шаг курации в том же сообщении (следующий pending или итог)."""
    text, kb = _rsa_render(session)
    try:
        await cq.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass


async def _rsa_edit_overview(cq: CallbackQuery, session) -> None:
    text, kb = _rsa_overview(session)
    try:
        await cq.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass


@dp.callback_query(RsaCB.filter(F.action == "approve"))
async def rsa_approve(cq: CallbackQuery, callback_data: RsaCB) -> None:
    session = await SESSIONS.set_state(
        callback_data.cid, callback_data.kind, callback_data.idx, "approved"
    )
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    await cq.answer("Одобрено")
    await _rsa_edit(cq, session)


@dp.callback_query(RsaCB.filter(F.action == "reject"))
async def rsa_reject(cq: CallbackQuery, callback_data: RsaCB) -> None:
    session = await SESSIONS.set_state(
        callback_data.cid, callback_data.kind, callback_data.idx, "rejected"
    )
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    await cq.answer("Отклонено")
    await _rsa_edit(cq, session)


@dp.callback_query(RsaCB.filter(F.action == "approveall"))
async def rsa_approveall(cq: CallbackQuery, callback_data: RsaCB) -> None:
    session = await SESSIONS.approve_all_valid(callback_data.cid)
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    await cq.answer("Одобрены все валидные")
    await _rsa_edit_overview(cq, session)


@dp.callback_query(RsaCB.filter(F.action == "review"))
async def rsa_review(cq: CallbackQuery, callback_data: RsaCB) -> None:
    session = await SESSIONS.get(callback_data.cid)
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    await cq.answer()
    await _rsa_edit(cq, session)  # _rsa_render покажет следующий pending


@dp.callback_query(RsaCB.filter(F.action == "overview"))
async def rsa_to_overview(cq: CallbackQuery, callback_data: RsaCB) -> None:
    session = await SESSIONS.get(callback_data.cid)
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    await cq.answer()
    await _rsa_edit_overview(cq, session)


@dp.callback_query(RsaCB.filter(F.action == "refine"))
async def rsa_refine(cq: CallbackQuery, callback_data: RsaCB, state: FSMContext) -> None:
    session = await SESSIONS.get(callback_data.cid)
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    await state.set_state(RsaRefine.awaiting_text)
    await state.update_data(cid=callback_data.cid, kind=callback_data.kind, idx=callback_data.idx)
    await cq.answer()
    await cq.message.answer(texts.RSA_REFINE_PROMPT)


@dp.callback_query(RsaCB.filter(F.action == "finalize"))
async def rsa_finalize(cq: CallbackQuery, callback_data: RsaCB) -> None:
    session = await SESSIONS.get(callback_data.cid)
    if session is None:
        await cq.answer(texts.RSA_SESSION_STALE, show_alert=True)
        return
    if not session.can_finalize():
        h, d = session.counts()
        await cq.answer(texts.RSA_BELOW_MIN.format(h=h, d=d), show_alert=True)
        return
    try:
        cid, op, params, summary = _build_rsa_proposal(session)
    except Exception as e:  # валидация схемы
        await cq.answer(f"Ошибка: {type(e).__name__}", show_alert=True)
        return
    await cq.answer()
    await _present_proposal(
        cq.message, chat_id=_cq_chat_id(cq), operation=op, params=params, summary=summary, cid=cid
    )


@dp.callback_query(RsaCB.filter(F.action == "cancel"))
async def rsa_cancel(cq: CallbackQuery, callback_data: RsaCB) -> None:
    await cq.answer("Отменено")
    try:
        await cq.message.edit_text(texts.REJECTED)
    except TelegramBadRequest:
        pass


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Глобальная сеть безопасности: необработанное исключение в любом хендлере логируется
    (через RedactionFilter — без секретов в сообщении И в traceback), а не падает молча в stderr.
    Пользователю сырой текст НЕ шлём (мог бы содержать креды) — только пишем в лог."""
    log.error(
        "необработанная ошибка в хендлере: %s",
        type(event.exception).__name__,
        exc_info=event.exception,
    )
    return True  # «обработано» — aiogram не пробрасывает дальше


async def main() -> None:
    setup_logging()
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
    dp.message.outer_middleware(WhitelistMiddleware())
    dp.callback_query.outer_middleware(WhitelistMiddleware())
    dp.message.outer_middleware(ThrottleMiddleware())  # анти-спам (ТЗ §12), после whitelist
    bot = Bot(token)
    try:
        await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:  # не критично — бот поднимется и без меню-команд
        log.warning("set_my_commands не удалось: %s: %s", type(e).__name__, e)
    sched = None
    try:
        from scheduler.service import setup_scheduler

        # Плановые отчёты/аномалии/очистка просроченных черновиков. READ-ONLY: планировщик
        # НИКОГДА не меняет аккаунт (golden rule #3) — только чтение и уведомления.
        sched = setup_scheduler(bot)
    except Exception as e:  # планировщик опционален — бот работает и без него
        log.warning("scheduler не запущен: %s: %s", type(e).__name__, e)
    log.info("Aimash bot запущен (polling).")
    try:
        await dp.start_polling(bot)
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())

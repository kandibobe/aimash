"""Ф5б: /competitors — импорт CSV «Статистика аукционов» (Auction Insights) + карточка конкурентов.

Метрики аукционов (`metrics.auction_insight_*`) в API ЕСТЬ, но доступ к ним закрыт вайтлистом
Google (программа набора закрыта; источник — docstring reports/auction_insights.py) — файл из
веб-интерфейса это ЕДИНСТВЕННЫЙ легальный путь. Суррогат по своим данным («давят или нет») уже даёт
чек competitive_pressure в /audit (Ф5а); здесь — «кто именно».

ЧУЖОЙ ФАЙЛ В ИИ НЕ ИДЁТ: разбирает КОД (reports/auction_insights.py), схему проверяет КОД. Поэтому
документ ловится ПО СОСТОЯНИЮ и раньше catch-all bot/handlers/fallback.py::on_document, который
скармливает содержимое агенту (модуль зарегистрирован до fallback — HANDLER_MODULES).

Ничего не мутирует в Google Ads и не создаёт proposal — только ЧТЕНИЕ аккаунта (пикер) и запись в
локальную таблицу auction_insight_row. Позднее связывание bm.<name>; CompetitorsWizard берём прямо
из bot.states (bot.main его тоже ре-экспортирует, но прямой импорт не зависит от порядка загрузки).
"""

from __future__ import annotations

import bot.main as bm
from bot.states import CompetitorsWizard

MAX_CSV_BYTES = 2_000_000  # выгрузка аукциона — десятки КБ; больше = не тот файл (анти-DoS)


async def _card(m: bm.Message, acct: str) -> bool:
    """Показать последний импортированный срез (+ дельта к предыдущему). False → импортов не было."""
    from db.competitors import latest_snapshot

    snap = await latest_snapshot(acct)
    if snap is None:
        return False
    prev = await latest_snapshot(acct, before_date=snap.snapshot_date)
    await m.answer(
        bm.texts.fmt_competitors(snap, prev, customer_id=acct, series=await _series(acct)),
        parse_mode=bm.ParseMode.HTML,
    )
    return True


async def _series(acct: str):
    """Хвост срезов для мини-тренда. Best-effort: тренд — довесок, карточка важнее сбоя БД."""
    try:
        from db.competitors import snapshot_series

        return await snapshot_series(acct, limit=4)
    except Exception as e:  # noqa: BLE001
        bm.log.warning("competitors: серия срезов не прочитана: %s", type(e).__name__)
        return []


@bm.dp.message(bm.Command("competitors"))
async def competitors_cmd(m: bm.Message, state: bm.FSMContext) -> None:
    """/competitors — кто рядом в аукционе: последний импортированный срез + приём нового файла."""
    acct = await bm._require_read_account(m, "competitors")
    if acct is None:
        return  # показан пикер аккаунта — оператор выберет и повторит
    try:
        had = await _card(m, acct)
    except Exception as e:  # noqa: BLE001 — БД недоступна: показываем инструкцию, не роняем команду
        bm.log.warning("competitors: срез не прочитан: %s", type(e).__name__)
        had = False
    if not had:
        await m.answer(bm.i18n.t("competitors_empty"))
    await state.set_state(CompetitorsWizard.awaiting_file)
    await state.update_data(comp_customer_id=acct)  # аккаунт зафиксирован на момент запроса файла
    await m.answer(
        bm.i18n.t("competitors_ask_file"),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(CompetitorsWizard.awaiting_file, bm.F.document)
async def competitors_file(m: bm.Message, state: bm.FSMContext, bot: bm.Bot) -> None:
    """CSV из интерфейса → разбор КОДОМ → срез в БД → карточка. Кривой файл: остаёмся в состоянии
    (пользователь пришлёт правильный), а НЕ молчим и не отдаём файл агенту."""
    from db.competitors import latest_snapshot, save_snapshot
    from reports.auction_insights import AuctionInsightsError, parse_auction_insights

    doc = m.document
    if doc is None:
        return
    if (doc.file_size or 0) > MAX_CSV_BYTES:
        await m.answer(bm.i18n.t("ingest_too_big", mb=MAX_CSV_BYTES // 1_000_000))
        return

    data = await state.get_data()
    acct = data.get("comp_customer_id") or await bm._active_read_account(m.chat.id)
    try:
        buf = bm.io.BytesIO()
        await bot.download(doc, destination=buf)
        ins = await bm.asyncio.to_thread(parse_auction_insights, buf.getvalue())
    except AuctionInsightsError:
        await m.answer(bm.i18n.t("competitors_bad_file"), parse_mode=bm.ParseMode.HTML)
        return  # остаёмся в состоянии: следующий файл разберём
    except Exception as e:  # сеть/битый файл — наружу только редактированный текст (rule #5)
        await m.answer(bm.i18n.t("err_competitors", err=bm.ux.err_text(e)))
        return

    await state.clear()
    snap_date = await _snapshot_date(acct)
    saved = True
    try:
        await save_snapshot(acct, snap_date, ins)
        snap = await latest_snapshot(acct)
        prev = await latest_snapshot(acct, before_date=snap_date) if snap else None
    except Exception as e:  # noqa: BLE001 — БД: карточку покажем, но о несохранении СКАЖЕМ (не молча)
        bm.log.warning("competitors: срез не сохранён: %s", type(e).__name__)
        saved, snap, prev = False, None, None

    from db.competitors import Snapshot

    card = snap or Snapshot(
        snapshot_date=snap_date,
        period_label=ins.period_label,
        you=ins.you,
        competitors=ins.competitors,
    )
    await m.answer(
        bm.texts.fmt_competitors(card, prev, customer_id=acct, series=await _series(acct)),
        parse_mode=bm.ParseMode.HTML,
    )
    if not saved:
        await m.answer(bm.i18n.t("competitors_not_saved"))


@bm.dp.message(CompetitorsWizard.awaiting_file)
async def competitors_wait(m: bm.Message) -> None:
    """В состоянии приёма файла пришёл текст: напоминаем, чего ждём (иначе on_text уведёт в ИИ)."""
    await m.answer(
        bm.i18n.t("competitors_wait_file"),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


async def _snapshot_date(acct: str) -> str:
    """Дата среза — СЕГОДНЯ В ТАЙМЗОНЕ АККАУНТА (как снапшот /audit): срезы разных дней должны
    сравниваться по календарю аккаунта, а не хоста. Сбой чтения TZ → дата хоста (best-effort)."""
    try:
        from ads.client import build_client_async

        client = await build_client_async()
        return await bm._account_local_date(client, acct)
    except Exception as e:  # noqa: BLE001 — TZ не прочитана: срез важнее точной границы суток
        bm.log.warning("competitors: TZ аккаунта не прочитана (%s) — дата хоста", type(e).__name__)
        from datetime import date

        return date.today().isoformat()

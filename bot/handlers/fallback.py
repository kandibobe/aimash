"""Хвост диспатча: документы, ingest-задача, гард визарда и CATCH-ALL on_text (строго последним)

Хендлеры вынесены из bot/main.py (декомпозиция god-module, предсдаточный аудит 2026-07).
ВСЕ имена из bot.main берутся через `bm.<name>` (ПОЗДНЕЕ связывание): monkeypatch тестов на
bot.main продолжает влиять на эти хендлеры, а регистрация происходит при импорте модуля —
порядок задаёт хвост bot/main.py (инвариант порядка — tests/test_handler_order.py).
"""

from __future__ import annotations

import bot.main as bm


@bm.dp.message(bm.F.document)
async def on_document(m: bm.Message, state: bm.FSMContext, bot: bm.Bot) -> None:
    """Файл (txt/csv/json/.docx/.xlsx) → текст. С подписью — сразу задача; без подписи — спросим
    задачу (контент в _PENDING_CONTEXT). Это ДАННЫЕ для агента; мутации — за confirm-гейтом.
    §19.4.1: файл в состоянии Этапа-2 визарда — это СПИСОК КЛЮЧЕЙ (не задача агенту)."""
    doc = m.document
    if doc is None:
        return
    if (doc.file_size or 0) > bm.ingest.MAX_FILE_BYTES:
        await m.answer(bm.i18n.t("ingest_too_big", mb=bm.ingest.MAX_FILE_BYTES // 1_000_000))
        return
    try:
        buf = bm.io.BytesIO()
        await bot.download(doc, destination=buf)
        text = await bm.asyncio.to_thread(
            bm.ingest.extract_file_text, doc.file_name or "", buf.getvalue()
        )
    except bm.ingest.IngestError as e:
        await m.answer(
            bm.i18n.t("ingest_file_failed", err=bm.texts.esc(str(e))), parse_mode=bm.ParseMode.HTML
        )
        return
    except Exception as e:  # сеть/скачивание
        await m.answer(
            bm.i18n.t("ingest_file_failed", err=bm.texts.esc(type(e).__name__)),
            parse_mode=bm.ParseMode.HTML,
        )
        return
    source = doc.file_name or bm.i18n.t("file_fallback")
    # §19.4.1: Этап 2 визарда ждёт ключи — файл кормим в черновик, НЕ в общий ingest (и НЕ чистим
    # состояние: сбой парса оставляет менеджера на Этапе 2 с подсказкой).
    if await state.get_state() == bm.CreateCampaignWizard.keywords:
        await bm._cc_keywords_from_document(m, state, text, source)
        return
    caption = (m.caption or "").strip()
    await state.clear()
    if caption:
        await bm._run_task_with_context(
            m, instruction=caption, context_text=text, source=source, state=state
        )
        return
    bm._PENDING_CONTEXT[m.chat.id] = {"text": text, "source": source}
    await state.set_state(bm.IngestWizard.awaiting_task)
    await m.answer(
        bm.i18n.t("ingest_ask_task", source=bm.texts.esc(source)),
        reply_markup=bm.nav_kb(),
        parse_mode=bm.ParseMode.HTML,
    )


@bm.dp.message(bm.IngestWizard.awaiting_task)
async def ingest_task(m: bm.Message, state: bm.FSMContext) -> None:
    """Задача к ранее принятому файлу получена → агент с контентом как СПРАВОЧНЫМ КОНТЕКСТОМ."""
    ctx = bm._PENDING_CONTEXT.get(m.chat.id)
    if not ctx:
        await state.clear()
        await m.answer(bm.i18n.t("ingest_stale"))
        return
    instruction = (m.text or "").strip()
    if not instruction:
        await m.answer(
            bm.i18n.t("ingest_empty_task"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
        return  # остаёмся в состоянии (retry с навигацией)
    bm._PENDING_CONTEXT.pop(m.chat.id, None)
    await state.clear()
    await bm._run_task_with_context(
        m,
        instruction=instruction,
        context_text=ctx["text"],
        source=ctx.get("source", ""),
        state=state,
    )


@bm.dp.message(
    bm.StateFilter(
        bm.CreateCampaignWizard.account_select,
        bm.CreateCampaignWizard.images,
        bm.CreateCampaignWizard.assets,
        bm.CreateCampaignWizard.asset_logo,
    ),
    bm.F.text,
)
async def cc_stage_expects_button(m: bm.Message) -> None:
    """B8: callback/фото-этапы визарда (выбор аккаунта, изображения, ассеты, логотип) текст не ждут.
    Раньше он проваливался в общий on_text → LLM-агент минтил ПОСТОРОННИЙ proposal (напр. update_budget
    на живую кампанию). Подсказываем нажать кнопку шага; фото на images/asset_logo — отдельные хендлеры,
    сюда не попадают (F.text). Зарегистрирован ДО on_text, поэтому перехватывает раньше catch-all."""
    await m.answer(bm.i18n.t("cc_stage_expects_button"))


@bm.dp.errors()
async def on_error(event: bm.ErrorEvent) -> bool:
    """Глобальная сеть безопасности (§15): необработанное исключение в любом хендлере ловится,
    логируется (РЕДАКТИРОВАННО — без секретов в сообщении И traceback) с request_id, СОХРАНЯЕТСЯ
    в error_events (триаж/`/diag`) и уходит в Sentry — а не падает молча в stderr.

    Пользователю сырой текст НЕ шлём (мог бы нести креды) — показываем понятное сообщение с «кодом
    инцидента» (request_id), по которому ошибку находят в /diag и логах."""
    code = await bm.capture_exception(event.exception, where="handler")
    try:  # уведомление пользователя — best-effort (ошибка уже зафиксирована)
        upd = event.update
        target = getattr(upd, "message", None) or getattr(
            getattr(upd, "callback_query", None), "message", None
        )
        if target is not None:
            await target.answer(bm.i18n.t("err_unexpected", code=code))
    except Exception:  # noqa: BLE001 — не даём сбою уведомления перекрыть фиксацию ошибки
        pass
    return True  # «обработано» — aiogram не пробрасывает дальше


# ВАЖНО: catch-all свободного текста регистрируется ПОСЛЕДНИМ message-хендлером (aiogram —
# «первый совпавший в порядке регистрации»): всё, что окажется ниже, никогда не получит текст.
# Регрессия этого инварианта уже случалась (/diag и rsa_list_edited были зарегистрированы после
# catch-all → мертвы в диспатче) — теперь порядок закреплён tests/test_handler_order.py.
@bm.dp.message(bm.F.text)
async def on_text(m: bm.Message, state: bm.FSMContext) -> None:
    text = m.text or ""
    # ingest: если в сообщении есть ссылка — прочитаем её и передадим агенту как СПРАВОЧНЫЙ КОНТЕНТ
    # (best-effort: сбой чтения не ломает обычную команду). Текст на callback/фото-этапах визарда сюда
    # НЕ доходит — его раньше перехватывает cc_stage_expects_button (B8); прочие визарды — свой стейт.
    urls = bm.ingest.extract_urls(text)
    if urls:
        try:
            async with bm.ux.typing_action(m):
                content = await bm.ingest.fetch_url_text(urls[0])
        except bm.ingest.IngestError as e:
            await m.answer(
                bm.i18n.t("ingest_link_failed", err=bm.texts.esc(str(e))),
                parse_mode=bm.ParseMode.HTML,
            )
            content = ""
        if content:
            await bm._run_task_with_context(
                m, instruction=text, context_text=content, source=urls[0], state=state
            )
            return
    async with bm.ux.typing_action(m):  # «печатает…» пока модель парсит команду
        res = await bm.handle_command(text, chat_id=m.chat.id)
    await bm._dispatch_command_result(m, res, state)

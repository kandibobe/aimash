"""Хвост диспатча: документы, legacy ingest, неизвестные команды и error boundary.

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
    # P1-C (§11): изображение, присланное ДОКУМЕНТОМ (несжатый креатив) — частый способ отправки
    # HD-картинок. Раньше падало в text-ingest → «ingest_file_failed». Теперь на «чистом» апдейте
    # (нет активного визарда) запускаем GDN-визард ровно как F.photo (download → prepare_display_images
    # → save_pending_media → GdnWizard.awaiting_brief). В активном флоу изображение-документ не трогаем
    # (текст-ветка ниже вернёт понятную ошибку) — фото-этапы визардов читают m.photo, не документ.
    mime = (getattr(doc, "mime_type", None) or "").lower()
    name_l = (getattr(doc, "file_name", None) or "").lower()
    is_image = mime.startswith("image/") or name_l.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
    )
    if is_image and await state.get_state() is None:
        try:
            buf = bm.io.BytesIO()
            await bot.download(doc, destination=buf)
            landscape, square = await bm.asyncio.to_thread(
                bm.prepare_display_images, buf.getvalue()
            )
        except Exception as e:  # сеть/битый файл/не картинка
            await m.answer(bm.i18n.t("err_photo", err=bm.ux.err_text(e)))
            return
        media_id = bm.uuid.uuid4().hex
        await bm.asyncio.to_thread(bm.save_pending_media, media_id, landscape, square)
        await state.update_data(gdn_media_id=media_id)
        await state.set_state(bm.GdnWizard.awaiting_brief)
        await bm.prompt_account_if_ambiguous(m, m.chat.id)  # AD.3: пикер аккаунта ДО брифа
        await m.answer(
            bm.i18n.t("gdn_ask_brief"), reply_markup=bm.nav_kb(), parse_mode=bm.ParseMode.HTML
        )
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
    except Exception as e:  # сеть/скачивание — без имени класса, с кодом инцидента (/diag)
        await m.answer(await bm._friendly_error(e, "ingest:file_download"))
        return
    source = doc.file_name or bm.i18n.t("file_fallback")
    # §19.4.1: Этап 2 визарда ждёт ключи — файл кормим в черновик, НЕ в общий ingest (и НЕ чистим
    # состояние: сбой парса оставляет менеджера на Этапе 2 с подсказкой).
    if await state.get_state() == bm.CreateCampaignWizard.keywords:
        await bm._cc_keywords_from_document(m, state, text, source)
        return
    # P3: /addkeys ждёт ключи — файл это СПИСОК КЛЮЧЕЙ для добавления в кампанию (не задача агенту).
    if await state.get_state() == bm.KwAdd.awaiting_keywords:
        await bm.kw_add_from_document(m, state, text, source)
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


# D9: нераспознанная слэш-команда → понятная подсказка, НЕ отправка в LLM. Известные команды
# перехватывают свои Command-хендлеры ВЫШЕ (порядок HANDLER_MODULES: commands/reports/… раньше
# fallback) — сюда доходит только неизвестное «/…». Agent-first on_text зарегистрирован раньше,
# но его фильтр явно исключает строки, начинающиеся с `/`.
@bm.dp.message(bm.F.text.startswith("/"))
async def on_unknown_command(m: bm.Message) -> None:
    cmd = (m.text or "").split(maxsplit=1)[0][:32]
    await m.answer(
        bm.i18n.t("unknown_command", cmd=bm.texts.esc(cmd)), parse_mode=bm.ParseMode.HTML
    )

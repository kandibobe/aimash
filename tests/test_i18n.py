"""Офлайн-тесты bot/i18n.py: каталог/мост к texts, фолбэк языка, in-memory выбор языка."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.i18n as i18n  # noqa: E402
from bot import texts  # noqa: E402


def test_t_returns_lang_variant():
    assert i18n.t("executing", "en") == "⏳ Working…"
    assert i18n.t("executing", "ru") == "⏳ Выполняю…"


def test_t_unknown_lang_falls_back_to_ru():
    assert i18n.t("rejected", "de") == "❌ Отменено"  # неизвестный язык → RU


def test_t_bridges_to_texts_for_unmigrated_key():
    # 'applied' нет в CATALOG → мост к texts.APPLIED
    assert i18n.t("applied", "ru") == texts.APPLIED


def test_t_unknown_key_returns_key_itself():
    assert i18n.t("definitely_unknown_key_xyz", "ru") == "definitely_unknown_key_xyz"


def test_t_formats_kwargs():
    # 'applied' не в CATALOG → texts.APPLIED с подстановкой
    assert i18n.t("applied", "ru", result="ok") == texts.APPLIED.format(result="ok")


def test_get_set_lang_in_memory():
    assert i18n.get_lang(424242) == "ru"  # дефолт
    assert i18n.set_lang(424242, "en") == "en"
    assert i18n.get_lang(424242) == "en"
    assert i18n.set_lang(424242, "xx") == "ru"  # нормализация неизвестного


# ── Языковой contextvar (§4): t() и форматтеры читают язык текущего запроса ───────────
def test_current_lang_default_is_ru():
    assert i18n.current_lang() == "ru"


def test_set_current_lang_switches_t_and_formatter():
    tok = i18n.set_current_lang("en")
    try:
        # t() без явного lang берёт contextvar
        assert i18n.t("help") == i18n.CATALOG["help"]["en"]
        # форматтер тоже берёт contextvar (без проброса lang)
        st = texts.fmt_stats("7753643025", 30, {"impressions": 10, "clicks": 2}, "USD")
        assert "Impressions:" in st and "Показы:" not in st
    finally:
        i18n.reset_current_lang(tok)
    # после reset — снова RU
    assert i18n.current_lang() == "ru"
    st_ru = texts.fmt_stats("7753643025", 30, {"impressions": 10, "clicks": 2}, "USD")
    assert "Показы:" in st_ru


def test_catalog_en_differs_and_nonempty():
    for key in ("start", "help", "rsa_ask_brief", "kw_ask", "model_set", "search_bad_brief"):
        entry = i18n.CATALOG[key]
        assert entry["ru"] and entry["en"], key
        assert entry["en"] != entry["ru"], key


def test_catalog_every_key_has_both_languages():
    """Мета-гард (Phase 4): КАЖДЫЙ ключ CATALOG имеет непустые ru И en — иначе t() на одном языке
    вернёт пусто/мост/сам ключ. Раньше проверялась лишь горстка ключей; новый ключ в одном языке
    (частый класс бага) проходил незамеченным до рантайма. Проверяем ВЕСЬ каталог явно."""
    missing = sorted(
        k
        for k, v in i18n.CATALOG.items()
        if not isinstance(v, dict)
        or not (v.get("ru") or "").strip()
        or not (v.get("en") or "").strip()
    )
    assert not missing, f"ключи CATALOG без ru или en: {missing}"


def test_ru_default_unchanged_start_help():
    # RU дефолт байт-в-байт совпадает с исходными константами texts.* (EN — чисто аддитивно)
    assert i18n.t("start", "ru") == texts.START
    assert i18n.t("help", "ru") == texts.HELP
    # узнаваемая RU-подстрока сохранена
    assert "Aimash на связи" in texts.START
    assert "Что я умею сейчас" in texts.HELP


def test_help_mentions_every_bot_command():
    """Гард от дрейфа: КАЖДАЯ команда из меню (BOT_COMMANDS) упомянута в /help на обоих языках.
    Иначе флагманские фичи (визард §19, клиенты §20) становятся невидимыми — уже случалось."""
    from bot.keyboards import BOT_COMMANDS

    for help_text in (texts.HELP, i18n.CATALOG["help"]["en"]):
        for bc in BOT_COMMANDS:
            if bc.command in ("start", "help"):
                continue
            # /client не должен засчитываться за счёт /clients → граница слова
            assert re.search(rf"/{bc.command}(?![a-z])", help_text), (
                f"/{bc.command} отсутствует в /help"
            )


def test_help_fits_telegram_limit_via_chunking():
    """Регресс-гард класса: /help давно перерос лимит Telegram (4096) — одиночный answer падал
    'message is too long' и юзер видел карточку err_unexpected вместо справки. _send_help обязан
    слать справку ЧАНКАМИ; здесь гоняем реальный чокпойнт и проверяем инвариант «каждое
    отправленное сообщение ≤ 4096» на обоих языках (help растёт с каждой командой — тест ловит
    возврат бага, даже если текст ещё подрастёт)."""
    import asyncio

    import bot.main as bm
    from bot import ux

    class _FakeMsg:
        def __init__(self):
            self.sent: list[str] = []

        async def answer(self, text: str = "", **kw):  # noqa: ANN003
            self.sent.append(text)
            return self

    assert len(texts.HELP) > ux.TG_LIMIT, (
        "предпосылка теста устарела: HELP влезает в одно сообщение — но гард всё равно валиден"
    )

    async def _drive(lang: str) -> list[str]:
        tok = i18n.set_current_lang(lang)
        try:
            m = _FakeMsg()
            await bm._send_help(m)
            return m.sent
        finally:
            i18n.reset_current_lang(tok)

    orig_pause = ux.CHUNK_SEND_PAUSE_S
    ux.CHUNK_SEND_PAUSE_S = 0  # тест не должен спать между чанками
    try:
        for lang in ("ru", "en"):
            sent = asyncio.run(_drive(lang))
            assert sent, f"_send_help({lang}) не отправил ни одного сообщения"
            assert len(sent) >= 2, (
                f"справка {lang} > лимита, но ушла одним сообщением (не чанкована)"
            )
            for i, chunk in enumerate(sent):
                assert len(chunk) <= ux.TG_LIMIT, (
                    f"чанк /help[{lang}][{i}] = {len(chunk)} символов > {ux.TG_LIMIT} — Telegram отклонит"
                )
    finally:
        ux.CHUNK_SEND_PAUSE_S = orig_pause


def test_bot_command_descriptions_have_no_internal_markers():
    """Описания команд в меню Telegram (то, что видит пользователь под «/») НЕ должны содержать
    внутренние ссылки на разделы ТЗ (§19/§20/§8) — это выглядело как черновик разработчика."""
    from bot.keyboards import BOT_COMMANDS, BOT_COMMANDS_EN

    for menu in (BOT_COMMANDS, BOT_COMMANDS_EN):
        for bc in menu:
            assert "§" not in bc.description, (
                f"/{bc.command}: §-маркер в пользовательском описании ({bc.description!r})"
            )


def test_formatter_en_smoke_journal_and_mutation():
    # fmt_mutation_summary под EN-contextvar — узнаваемо английский
    tok = i18n.set_current_lang("en")
    try:
        s = texts.fmt_mutation_summary("pause_campaign", {"campaign": "Brand"})
        assert s == "Campaign “Brand” — pause."
        # fmt_journal (пустой) под EN
        j = texts.fmt_journal([])
        assert "Change journal" in j and "Журнал" not in j
    finally:
        i18n.reset_current_lang(tok)
    # RU по умолчанию неизменен
    assert texts.fmt_mutation_summary("pause_campaign", {"campaign": "Brand"}).startswith(
        "Кампания «Brand»"
    )


async def test_save_lang_load_langs_roundtrip():
    """Персист языка переживает «рестарт»: save_lang пишет в user_settings.language, а load_langs
    поднимает кэш заново. Реальный temp-SQLite (conftest); чистим кэш, чтобы проверить именно БД."""
    from db.session import init_db

    await init_db()
    chat = 991_002_003
    await i18n.save_lang(chat, "en")
    # эмулируем рестарт: затираем in-memory кэш и грузим из БД
    i18n._CHAT_LANG.pop(chat, None)
    assert i18n.get_lang(chat) == "ru"  # кэш пуст → дефолт
    await i18n.load_langs()
    assert i18n.get_lang(chat) == "en"  # поднят из user_settings.language
    # upsert: смена на ru обновляет ту же строку
    await i18n.save_lang(chat, "ru")
    i18n._CHAT_LANG.pop(chat, None)
    await i18n.load_langs()
    assert i18n.get_lang(chat) == "ru"


# ── Полнота каталога (харденинг §4): каждая строка переведена ──────────────────────
_CYRILLIC = re.compile("[А-Яа-яЁё]")
_PLACEHOLDER = re.compile(r"{[^}]+}")


def test_catalog_every_key_has_ru_and_en():
    for k, v in i18n.CATALOG.items():
        assert set(v) == {"ru", "en"}, k
        assert v["ru"] and v["en"], k


def test_catalog_en_has_no_cyrillic():
    """Полный харденинг: ни одна EN-строка каталога не содержит кириллицы. Ловит забытый перевод
    при добавлении нового ключа (EN-сторона осталась русской) — фейлит до того, как увидит юзер."""
    leaked = sorted(k for k, v in i18n.CATALOG.items() if _CYRILLIC.search(v["en"]))
    assert not leaked, f"EN-строки с кириллицей: {leaked}"


def test_catalog_placeholders_match_ru_en():
    """{placeholder}-набор обязан совпадать между RU и EN — иначе .format(**kw) даст KeyError
    на одном из языков (напр. ключ ждёт {err}, а перевод его потерял)."""
    bad = sorted(
        k
        for k, v in i18n.CATALOG.items()
        if set(_PLACEHOLDER.findall(v["ru"])) != set(_PLACEHOLDER.findall(v["en"]))
    )
    assert not bad, f"рассинхрон плейсхолдеров RU/EN: {bad}"


def test_every_t_literal_lives_in_catalog():
    """2.7: каждый литеральный вызов i18n.t('ключ') в рантайм-коде обязан бить в CATALOG.
    Ключ вне каталога тихо мостится к texts.<KEY> (всегда RU) или возвращает сам ключ — латентная
    RU-утечка в EN, которую глазами не поймать. Скан — по bot/, scheduler/, clients/, agent/."""
    import pathlib

    pat = re.compile(r"""\bt\(\s*["']([a-z0-9_]+)["']""")
    root = pathlib.Path(i18n.__file__).resolve().parents[1]
    used: set[str] = set()
    for d in ("bot", "scheduler", "clients", "agent"):
        for p in (root / d).rglob("*.py"):
            used |= set(pat.findall(p.read_text(encoding="utf-8")))
    missing = sorted(k for k in used if k not in i18n.CATALOG)
    assert not missing, f"ключи i18n.t вне CATALOG (RU-утечка в EN): {missing}"


def test_hardened_keys_render_both_langs():
    """Новые «вторичные» ключи (callback-тосты, ошибки, хинты, agent.loop) отдают перевод, а не
    мост к texts.* и не сам ключ. Проверяем оба языка + подстановку kw."""
    assert i18n.t("cb_done", "en") == "Done"
    assert i18n.t("cb_done", "ru") == "Готово"
    assert i18n.t("err_stats", "en", err="X") == "⚠️ Couldn't fetch the stats: X"
    assert i18n.t("loop_bad_args", "en", name="update_budget", errors="[...]").startswith(
        "invalid arguments for update_budget"
    )
    assert "minute" in i18n.t("hint_timeout", "en")


# ── Изоляция языка по asyncio-таске (§4): параллельные апдейты не «перекрашивают» друг друга ──
async def test_langmiddleware_contextvar_isolation():
    """Модель LangMiddleware: set_current_lang в начале апдейта, reset в finally. aiogram гонит
    апдейты разными asyncio-тасками → contextvar изолирован: EN-юзер и RU-юзер, обрабатываемые
    одновременно, видят КАЖДЫЙ свой язык. Без изоляции один перекрасил бы другого."""
    import asyncio

    seen: dict[str, tuple[str, str]] = {}

    async def worker(lang: str) -> None:
        tok = i18n.set_current_lang(lang)
        try:
            await asyncio.sleep(0)  # отдать управление другим воркерам (интерлив)
            await asyncio.sleep(0)
            seen[lang] = (i18n.current_lang(), i18n.t("cb_done"))
        finally:
            i18n.reset_current_lang(tok)

    await asyncio.gather(worker("en"), worker("ru"), worker("en"))
    assert seen["en"] == ("en", "Done")
    assert seen["ru"] == ("ru", "Готово")


def test_fmt_rsa_diagnostics_follows_contextvar():
    """ux.fmt_rsa_diagnostics без явного lang берёт язык запроса (contextvar), как форматтеры texts.*."""
    from bot import ux

    class _Draft:
        headlines = [1] * 12
        descriptions = [1] * 4
        dropped_headlines = 0
        dropped_descriptions = 0

    tok = i18n.set_current_lang("en")
    try:
        assert ux.fmt_rsa_diagnostics(_Draft(), 15, 4).startswith("✅ Generated")
    finally:
        i18n.reset_current_lang(tok)
    assert ux.fmt_rsa_diagnostics(_Draft(), 15, 4).startswith("✅ Сгенерировано")

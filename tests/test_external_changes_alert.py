"""Р6: плановый алерт о правках, сделанных в аккаунте МИМО бота (`change_event`).

Дыра, которую закрывает джоба (deploy/hermes/RISK_REGISTER.md, Р6): Google НЕ уведомляет о правках
из веб-интерфейса/Editor/чужого API-клиента, а «отмена возвращает настройку, но не потраченные
деньги» — значит опрашивать журнал обязан наш код по расписанию, а не агент по просьбе.

Здесь проверяется поведение джобы (базлайн, дедуп по курсору, BZ-3-правило «курсор двигается только
после доставки И только на ПОКАЗАННОЕ», справедливое деление бюджета строк между аккаунтами,
C2-фильтр доступа, отсечка своего API-канала) и форматтер. Всё офлайн: SQLite + подменённый ридер,
без сети и SDK.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class DeadBot:
    """Чат недоступен (блокировка/сеть) — не TelegramRetryAfter, значит наружу сразу."""

    async def send_message(self, chat_id, text, **kw):
        raise RuntimeError("chat unavailable")


def _ev(changed_at: str, *, client_type: str = "GOOGLE_ADS_WEB_CLIENT", **kw):
    """Строка журнала в том виде, в каком её отдаёт reports.queries.fetch_change_events."""
    from reports.queries import ChangeEventRow

    return ChangeEventRow(
        changed_at=changed_at,
        resource_type=kw.get("resource_type", "CAMPAIGN_BUDGET"),
        operation=kw.get("operation", "UPDATE"),
        client_type=client_type,
        user_email=kw.get("user_email", "manager@agency.example"),
        resource_name=kw.get("resource_name", "customers/7753643025/campaignBudgets/1"),
        changed_fields=kw.get("changed_fields", ("campaign_budget.amount_micros",)),
    )


@pytest.fixture
def wired(monkeypatch):
    """Джоба с подменённым внешним миром: получатели, аккаунты, клиент, ридер, TZ.

    Возвращает мутируемый список событий — тест меняет его между прогонами, имитируя новые правки.
    """
    from scheduler import jobs

    events: list = []
    recipients: set[int] = {901}
    accounts = ["7753643025"]
    # Мутируемая: тест смены таймзоны аккаунта правит её между циклами.
    tz = {"value": "Europe/Kyiv"}

    async def _recipients():
        return set(recipients)

    async def _build_client_async(acct):
        return SimpleNamespace(customer_id=acct)

    async def _account_today(client, customer_id, **kw):
        from datetime import date

        return date(2026, 7, 30)

    async def _run_ads_read_call(fn, *args, **kw):
        # Через один и тот же исполнитель идут два разных чтения — ридер журнала и таймзона.
        if getattr(fn, "__name__", "") == "account_timezone":
            return tz["value"]
        return list(events)

    monkeypatch.setattr(jobs, "_recipients", _recipients)
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: list(accounts))
    monkeypatch.setattr(jobs, "build_client_async", _build_client_async)
    monkeypatch.setattr(jobs, "run_ads_read_call", _run_ads_read_call)
    monkeypatch.setattr("reports.tz.account_today", _account_today)
    return SimpleNamespace(
        jobs=jobs,
        events=events,
        recipients=recipients,
        accounts=accounts,
        acct=accounts[0],
        tz=tz,
    )


def _at(seen: dict | None, acct: str) -> str:
    """Отметка курсора из блоба (запись — {'at': …, 'tz': …})."""
    return str((seen or {}).get(acct, {}).get("at") or "")


async def _fresh_db():
    from db.session import init_db

    await init_db()


# ── дедуп и базлайн ────────────────────────────────────────────────────────────────
async def test_first_run_only_sets_baseline(wired):
    """Первый прогон по (чат, аккаунт) НЕ рассылает историю окна — только запоминает курсор.

    Иначе включение фичи на живом аккаунте даёт недельную простыню на старте, и оператор учится не
    читать эти сообщения — Р6 закрылась бы формально."""
    await _fresh_db()
    wired.events[:] = [_ev("2026-07-29 10:00:00.000000"), _ev("2026-07-28 09:00:00.000000")]

    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 0
    assert bot.sent == []

    seen = await wired.jobs._ui_pref_blob(901, wired.jobs._EXT_CHANGES_SEEN_KEY)
    # Курсор = САМОЕ СВЕЖЕЕ событие + таймзона, в которой отметка снята (без неё сравнение отметок
    # после смены зоны аккаунта молча сдвигает всю шкалу — см. test_timezone_change_replays_window).
    assert seen == {wired.acct: {"at": "2026-07-29 10:00:00.000000", "tz": "Europe/Kyiv"}}


async def test_new_change_alerts_then_goes_quiet(wired):
    """После базлайна новая правка уходит алертом, повторный прогон без новых — тишина."""
    await _fresh_db()
    wired.events[:] = [_ev("2026-07-29 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    wired.events.insert(0, _ev("2026-07-30 11:30:00.000000", user_email="ivan@client.example"))
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 1
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 901
    # Почта маскирована: получатель алерта — менеджер, а не владелец этих персональных данных.
    assert "i***@client.example" in text
    assert "ivan@client.example" not in text
    assert "2026-07-29" not in text  # старое событие второй раз не показываем

    bot2 = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot2) == 0
    assert bot2.sent == []


async def test_cursor_boundary_is_strict(wired):
    """Событие с ТЕМ ЖЕ `changed_at`, что курсор, повтором не считается (строгое `>`)."""
    await _fresh_db()
    wired.events[:] = [_ev("2026-07-30 08:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 0
    assert bot.sent == []


# ── что считаем «чужой правкой» ────────────────────────────────────────────────────
async def test_api_channel_is_not_reported(wired):
    """Правки по каналу API не показываем: свои мутации репортит К7 из audit-row, а отличить их от
    чужого API-клиента нечем — иначе алерт приходил бы на каждую собственную операцию."""
    await _fresh_db()
    wired.events[:] = [_ev("2026-07-29 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    wired.events.insert(0, _ev("2026-07-30 12:00:00.000000", client_type="GOOGLE_ADS_API"))
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 0
    assert bot.sent == []


async def test_no_external_events_no_cursor_write(wired):
    """Аккаунт без внешних правок вообще не доходит до курсора — джоба тихая и БД не трогает."""
    await _fresh_db()
    wired.recipients.clear()  # свой chat_id: БД в прогоне общая, чужой курсор сюда не подмешиваем
    wired.recipients.add(904)
    wired.events[:] = [_ev("2026-07-30 12:00:00.000000", client_type="GOOGLE_ADS_API")]

    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 0
    assert bot.sent == []
    assert await wired.jobs._ui_pref_blob(904, wired.jobs._EXT_CHANGES_SEEN_KEY) is None


# ── BZ-3: курсор двигается только после доставки ───────────────────────────────────
async def test_failed_delivery_keeps_cursor(wired, monkeypatch):
    """Не доставили — курсор НЕ двигаем: правка бюджета мимо бота не должна потеряться навсегда
    из-за одной недоступности чата (BZ-3, то же правило, что у error-алертов)."""
    await _fresh_db()
    wired.recipients.clear()
    wired.recipients.add(902)
    wired.events[:] = [_ev("2026-07-29 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    wired.events.insert(0, _ev("2026-07-30 11:00:00.000000"))
    assert await wired.jobs.run_external_change_alerts(DeadBot()) == 0  # доставки не было

    seen = await wired.jobs._ui_pref_blob(902, wired.jobs._EXT_CHANGES_SEEN_KEY)
    assert _at(seen, wired.acct) == "2026-07-29 10:00:00.000000"  # курсор остался на базлайне

    bot = FakeBot()  # следующий цикл — та же правка доезжает
    assert await wired.jobs.run_external_change_alerts(bot) == 1
    assert "2026-07-30 11:00" in bot.sent[0][1]


# ── C2: доступ к аккаунту ──────────────────────────────────────────────────────────
async def test_recipient_without_account_access_gets_nothing(wired, monkeypatch):
    """Правки аккаунта не уходят оператору без доступа к нему (C2) — и курсор ему не пишется."""
    await _fresh_db()
    wired.recipients.clear()
    wired.recipients.add(903)

    async def _no_access(chat_id, candidates):
        return []

    monkeypatch.setattr("core.access.accessible_accounts_for_user", _no_access)
    wired.events[:] = [_ev("2026-07-30 10:00:00.000000")]

    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 0
    assert bot.sent == []
    assert await wired.jobs._ui_pref_blob(903, wired.jobs._EXT_CHANGES_SEEN_KEY) is None


async def test_no_recipients_is_noop(wired):
    """Некому слать — джоба не ходит в Google Ads вовсе (никаких лишних запросов квоты)."""
    await _fresh_db()
    wired.recipients.clear()
    wired.events[:] = [_ev("2026-07-30 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0


async def test_account_read_failure_does_not_break_others(wired, monkeypatch):
    """Сбой чтения одного аккаунта не отменяет алерт по остальным (как в аномалиях)."""
    await _fresh_db()
    wired.recipients.clear()  # свой chat_id: БД в прогоне общая
    wired.recipients.add(910)
    wired.accounts[:] = ["1111111111", "7753643025"]

    async def _run_ads_read_call(fn, client, customer_id, **kw):
        if customer_id == "1111111111":
            raise RuntimeError("boom")
        if getattr(fn, "__name__", "") == "account_timezone":
            return wired.tz["value"]
        return list(wired.events)

    monkeypatch.setattr(wired.jobs, "run_ads_read_call", _run_ads_read_call)
    wired.events[:] = [_ev("2026-07-29 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    wired.events.insert(0, _ev("2026-07-30 10:00:00.000000"))
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 1


# ── бюджет строк: ни один аккаунт не голодает ──────────────────────────────────────
async def test_busy_account_does_not_starve_the_quiet_one(wired, monkeypatch):
    """Аккаунт с сотней правок не должен съедать весь дайджест, вытесняя соседний.

    `_scheduled_accounts()` возвращает отсортированный список ⇒ при глобальном потолке голодал бы
    детерминированно ОДИН И ТОТ ЖЕ аккаунт, и его правка бюджета мимо бота не показалась бы никогда.
    """
    await _fresh_db()
    from core.texts import EXT_CHANGES_MAX_LINES

    wired.recipients.clear()
    wired.recipients.add(905)
    wired.accounts[:] = ["1111111111", "7753643025"]
    old = [_ev("2026-07-29 09:00:00.000000")]
    noisy = old + [
        _ev(f"2026-07-30 11:{i:02d}:00.000000") for i in range(EXT_CHANGES_MAX_LINES + 5)
    ]
    quiet = old + [_ev("2026-07-30 12:00:00.000000")]
    per_account = {"1111111111": list(old), "7753643025": list(old)}

    async def _run_ads_read_call(fn, client, customer_id, **kw):
        if getattr(fn, "__name__", "") == "account_timezone":
            return "Europe/Kyiv"
        return list(per_account[customer_id])

    async def _all_access(chat_id, candidates):  # БД в прогоне общая: гранты соседних тестов
        return list(candidates)  # сузили бы набор и тест мерил бы не то

    monkeypatch.setattr("core.access.accessible_accounts_for_user", _all_access)
    monkeypatch.setattr(wired.jobs, "run_ads_read_call", _run_ads_read_call)
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн на общем старом

    per_account["1111111111"] = noisy
    per_account["7753643025"] = quiet
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 1
    text = "".join(t for _, t in bot.sent)
    assert "7753643025" in text and "2026-07-30 12:00" in text  # тихий аккаунт показан
    assert "… и ещё" in text  # шумному хвосту сказано «в следующий раз»


async def test_unshown_tail_arrives_next_cycle(wired):
    """Курсор двигается на ПОКАЗАННОЕ, а не на максимум всех прочитанных событий.

    Иначе непоказанный хвост оказался бы «старше курсора» и выпал бы навсегда — это второй способ
    потерять правку, помимо недоставки (BZ-3)."""
    await _fresh_db()
    from core.texts import EXT_CHANGES_MAX_LINES

    wired.recipients.clear()
    wired.recipients.add(906)
    wired.events[:] = [_ev("2026-07-30 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    tail = EXT_CHANGES_MAX_LINES + 4
    wired.events[:0] = [_ev(f"2026-07-30 11:{i:02d}:00.000000") for i in range(tail)]
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 1
    first = "".join(t for _, t in bot.sent)
    assert "… и ещё" in first

    bot2 = FakeBot()  # следующий цикл БЕЗ новых правок — хвост доезжает, а не пропадает
    assert await wired.jobs.run_external_change_alerts(bot2) == 1
    second = "".join(t for _, t in bot2.sent)
    assert f"2026-07-30 11:{tail - 1:02d}" in second  # самая свежая из хвоста показана
    assert second.count("• ") + first.count("• ") == tail


async def test_long_digest_is_split_not_dropped(wired, monkeypatch):
    """Дайджест длиннее лимита Telegram режется на части.

    Без нарезки один отказ по длине означал бы «молчит навсегда»: курсор не двинулся ⇒ следующий
    цикл соберёт тот же текст той же длины."""
    await _fresh_db()
    wired.recipients.clear()
    wired.recipients.add(907)
    monkeypatch.setattr("core.texts.EXT_CHANGES_MAX_LINES", 60)
    wired.events[:] = [_ev("2026-07-30 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн

    long_field = "campaign_budget.amount_micros_" + "x" * 90
    wired.events[:0] = [
        _ev(f"2026-07-30 11:{i:02d}:00.000000", changed_fields=(long_field,)) for i in range(50)
    ]
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 1  # доставка засчитана один раз
    assert len(bot.sent) > 1  # но сообщений ушло несколько
    assert all(len(t) <= 4096 for _, t in bot.sent)


def test_split_line_budget_gives_everyone_a_share():
    """Дележ бюджета: каждому минимум строка, берутся САМЫЕ СТАРЫЕ из свежих."""
    from scheduler.jobs import _split_line_budget

    fresh = [
        (f"{i}" * 10, [_ev(f"2026-07-30 1{j}:00:00.000000") for j in range(3)]) for i in range(1, 6)
    ]
    batch, pending = _split_line_budget(fresh)
    assert len(batch) == len(fresh)
    assert all(len(head) >= 1 for _, head in batch)
    assert pending == sum(len(evs) for _, evs in fresh) - sum(len(h) for _, h in batch)
    assert batch[0][1][0].changed_at == "2026-07-30 10:00:00.000000"  # старейшее — первым
    assert _split_line_budget([]) == ([], 0)


def test_split_line_budget_never_starves_a_tiny_share():
    """Аккаунтов больше, чем строк в бюджете, — каждый всё равно получает одну."""
    from core.texts import EXT_CHANGES_MAX_LINES
    from scheduler.jobs import _split_line_budget

    fresh = [
        (f"acct{i}", [_ev("2026-07-30 10:00:00.000000")]) for i in range(EXT_CHANGES_MAX_LINES * 2)
    ]
    batch, pending = _split_line_budget(fresh)
    assert len(batch) == len(fresh) and pending == 0


# ── смена таймзоны аккаунта ────────────────────────────────────────────────────────
async def test_timezone_change_replays_window(wired, caplog):
    """`changed_at` приходит в зоне АККАУНТА и без указания зоны — при её смене отметка курсора
    несравнима с новыми. Показываем окно заново: дубль дешевле пропуска (то же правило, что в BZ-3).
    """
    await _fresh_db()
    wired.recipients.clear()
    wired.recipients.add(908)
    wired.events[:] = [_ev("2026-07-30 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн в Europe/Kyiv

    wired.tz["value"] = "America/New_York"
    bot = FakeBot()
    assert await wired.jobs.run_external_change_alerts(bot) == 1
    assert "2026-07-30 10:00" in "".join(t for _, t in bot.sent)  # событие показано повторно

    seen = await wired.jobs._ui_pref_blob(908, wired.jobs._EXT_CHANGES_SEEN_KEY)
    assert seen[wired.acct]["tz"] == "America/New_York"  # курсор перевыставлен в новой зоне

    bot2 = FakeBot()  # зона больше не меняется — тишина
    assert await wired.jobs.run_external_change_alerts(bot2) == 0


def test_cursor_entry_parses_both_shapes():
    """Разбор записи курсора: новый вид — словарь с зоной, любой другой — отметка без сверки зоны."""
    from scheduler.jobs import _cursor_entry

    assert _cursor_entry(
        {"at": "2026-07-30 10:00:00.000000", "tz": "Europe/Kyiv"}, "Europe/Kyiv"
    ) == (
        "2026-07-30 10:00:00.000000",
        True,
    )
    assert _cursor_entry({"at": "x", "tz": "Europe/Kyiv"}, "America/New_York") == ("x", False)
    assert _cursor_entry("2026-07-30 10:00:00.000000", "Europe/Kyiv") == (
        "2026-07-30 10:00:00.000000",
        True,
    )


async def test_unchanged_cursor_is_not_rewritten(wired, monkeypatch):
    """Цикл без движения курсора не пишет в БД: джоба крутится каждые N часов на каждом чате."""
    await _fresh_db()
    wired.recipients.clear()
    wired.recipients.add(909)
    wired.events[:] = [_ev("2026-07-30 10:00:00.000000")]
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0  # базлайн записан

    writes: list = []
    orig = wired.jobs._save_ui_pref_blob

    async def _spy(chat_id, key, value):
        writes.append(chat_id)
        return await orig(chat_id, key, value)

    monkeypatch.setattr(wired.jobs, "_save_ui_pref_blob", _spy)
    assert await wired.jobs.run_external_change_alerts(FakeBot()) == 0
    assert writes == []


# ── чистые помощники ───────────────────────────────────────────────────────────────
def test_fresh_changes_uses_lexicographic_order():
    """`changed_at` — строка фиксированной ширины, поэтому лексикографика = хронология."""
    from scheduler.jobs import _fresh_changes

    evs = [_ev("2026-07-30 09:00:00.000000"), _ev("2026-07-29 23:59:59.999999")]
    assert len(_fresh_changes(evs, "2026-07-30 00:00:00.000000")) == 1
    assert _fresh_changes(evs, "") == []  # пустой курсор = базлайн, ничего не свежо


def test_is_external_change():
    from scheduler.jobs import _is_external_change

    assert _is_external_change(_ev("2026-07-30 09:00:00.000000")) is True
    assert _is_external_change(_ev("2026-07-30 09:00:00.000000", client_type="GOOGLE_ADS_API")) is (
        False
    )


# ── форматтер ──────────────────────────────────────────────────────────────────────
def test_fmt_external_changes_shape():
    from core.texts import fmt_external_changes

    text = fmt_external_changes([("7753643025", [_ev("2026-07-30 11:30:12.345678")])])
    assert "Правки мимо бота: 1" in text
    assert "7753643025" in text
    assert "бюджет кампании" in text and "изменено" in text and "веб-интерфейс" in text
    # Домен и первая буква остаются: «правил кто-то из агентства» vs «из компании клиента» — это и
    # есть сигнал алерта, а полный адрес получателю не нужен (см. mask_email).
    assert "m***@agency.example" in text and "manager@agency.example" not in text
    assert "campaign_budget.amount_micros" in text
    assert "2026-07-30 11:30" in text and "12.345678" not in text  # секунды не показываем


def test_fmt_external_changes_names_the_object():
    """Без id объекта строка «бюджет кампании изменено» неотличима от такой же по СОСЕДНЕЙ кампании:
    получатель не может ни проверить правку, ни отличить две подряд."""
    from core.texts import fmt_external_changes

    ev = _ev("2026-07-30 11:00:00.000000", resource_name="customers/7753643025/campaignBudgets/456")
    assert "456" in fmt_external_changes([("7753643025", [ev])])


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("manager@agency.example", "m***@agency.example"),
        ("a@client.example", "a***@client.example"),  # локальная часть в одну букву
        ("", ""),  # почты в строке журнала может не быть вовсе
        ("not-an-email", "n***"),  # мусор не должен утечь целиком
        ("@agency.example", "***@agency.example"),  # пустая локальная часть
    ],
)
def test_mask_email_keeps_the_signal_not_the_identity(raw, masked):
    """Домен остаётся (агентство vs компания клиента — это и есть сигнал), личность — нет.

    Одна функция на оба выхода наружу: дайджест джобы и `change_event_dict` в конверте MCP."""
    from core.texts import mask_email

    assert mask_email(raw) == masked


def test_fmt_external_changes_unknown_enum_passes_through():
    """Новый канал Google не должен превращаться в «прочее»: единственный факт алерта — ОТКУДА
    пришла правка, и терять его на незнакомом enum нельзя."""
    from core.texts import fmt_external_changes

    text = fmt_external_changes(
        [("7753643025", [_ev("2026-07-30 11:00:00.000000", client_type="SOME_NEW_CHANNEL")])]
    )
    assert "SOME_NEW_CHANNEL" in text


def test_fmt_external_changes_renders_everything_it_was_given():
    """Форматтер НИЧЕГО не режет сам: бюджет строк делит джоба (`_split_line_budget`).

    Это не стилистика. Глобальный потолок здесь означал бы «первые аккаунты съели квоту, последние
    не показаны», а курсор «уже показано» джоба двигала бы всё равно — правки хвостовых аккаунтов
    исчезали бы навсегда, ровно тот отказ, ради которого Р6 и заведена."""
    from core.texts import EXT_CHANGES_MAX_LINES, fmt_external_changes

    evs = [_ev(f"2026-07-30 11:{i:02d}:00.000000") for i in range(EXT_CHANGES_MAX_LINES + 10)]
    text = fmt_external_changes([("7753643025", evs)])
    assert text.count("• ") == len(evs)


def test_fmt_external_changes_shows_pending_tail():
    """Непоказанный хвост — счётчиком, а не обрывом: получатель должен знать, что это не всё."""
    from core.texts import fmt_external_changes

    text = fmt_external_changes([("7753643025", [_ev("2026-07-30 11:00:00.000000")])], pending=6)
    assert "… и ещё 6" in text
    assert "покажу следующим циклом" in text


def test_fmt_external_changes_keeps_every_account_visible():
    """Каждый аккаунт, чьи строки переданы, получает заголовок — иначе «тихо ничего не показали»
    выглядит как «в аккаунте не было правок»."""
    from core.texts import fmt_external_changes

    text = fmt_external_changes(
        [
            ("7753643025", [_ev(f"2026-07-30 11:{i:02d}:00.000000") for i in range(10)]),
            ("1111111111", [_ev("2026-07-30 12:00:00.000000")]),
        ]
    )
    assert "1111111111" in text and "7753643025" in text
    assert "Правки мимо бота: 11" in text


def test_fmt_external_changes_en():
    from core.texts import fmt_external_changes

    text = fmt_external_changes([("7753643025", [_ev("2026-07-30 11:00:00.000000")])], lang="en")
    assert "Changes made outside the bot: 1" in text
    assert "campaign budget" in text and "web UI" in text


# ── настройка окна ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [0, -1, 30, 400])
def test_window_out_of_range_falls_back_to_default(bad, monkeypatch):
    """Опечатка в env не должна превращаться в «алертов нет»: расписание — не гейт безопасности,
    поэтому здесь fail-safe (откат на дефолт + громкий лог), а не отказ на каждом прогоне."""
    from core.config import Settings

    monkeypatch.setenv("EXTERNAL_CHANGES_WINDOW_DAYS", str(bad))
    assert Settings().external_changes_window_days == 7


def test_config_window_ceiling_matches_reader():
    """Потолок в конфиге продублирован литералом (импорт reports.queries оттуда = цикл) — держим
    совпадение тестом, иначе значение разъедется молча."""
    from core.config import Settings
    from reports.queries import CHANGE_EVENT_MAX_DAYS

    at_ceiling = Settings(external_changes_window_days=CHANGE_EVENT_MAX_DAYS)
    assert at_ceiling.external_changes_window_days == CHANGE_EVENT_MAX_DAYS  # 29 ещё допустимо
    over = Settings(external_changes_window_days=CHANGE_EVENT_MAX_DAYS + 1)
    # 30 = ровно ретенция: на аккаунте, чья дата на сутки впереди хостовой, такое окно вылезает за
    # неё и сервер отвергает запрос ЦЕЛИКОМ — алерт молчал бы каждый цикл (см. reports/queries.py).
    assert over.external_changes_window_days == 7

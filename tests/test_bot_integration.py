"""E2E-тесты бот-слоя: on_text → proposal → store → confirm/cancel → execute → audit.

Закрывает дыру из аудита: «нет интеграционных тестов confirm OK/NO роундтрипа на уровне
хендлеров». Импортируем реальные хендлеры bot.main, реальный ConfirmStore на temp SQLite
(conftest), а LLM (handle_command) и исполнение (execute_confirmed) подменяем. Фейки aiogram —
локальные (Message/CallbackQuery/FSM/Bot), без сети/Telegram.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import ads.mutations as mut  # noqa: E402
import ads.resolve as resolve  # noqa: E402
import ads.service as svc  # noqa: E402
import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import ConfirmCB  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from core.config import settings  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


async def _fake_client_async(*a, **k):
    # execute_confirmed зовёт await build_client_async() (сборка SDK вне loop) — фейк-заглушка.
    return object()


# ── Локальные фейки aiogram ──────────────────────────────────────────────────────
class FakeBot:
    async def send_chat_action(self, *a, **k):
        pass


class FakeMessage:
    def __init__(self, text: str = "", chat_id: int = 100, bot=None):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = bot
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def answer_document(self, doc, **kw):
        self.answers.append(("<doc>", kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, data: str = "", uid: int = 100):
        self.message = message
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeFSM:
    def __init__(self):
        self._d: dict = {}
        self._state = None

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, s=None, *a, **k):
        self._state = s

    async def get_state(self):  # N5: on_text спрашивает активный state (гард против утечки визарда)
        return self._state

    async def clear(self):
        self._d = {}
        self._state = None


def _proposal_handler(cid: str):
    async def _h(text, chat_id, **kwargs):  # **kwargs: терпим к новому context (C1/C3)
        return {
            "type": "proposal",
            "operation": "resume_campaign",
            "params": {"campaign": "X"},
            "summary": "Возобновить кампанию X",
            "confirmation_id": cid,
        }

    return _h


async def _audit_statuses(cid: str) -> list[str]:
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.confirmation_id == cid).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    return [r.status for r in rows]


# ── confirm OK роундтрип ─────────────────────────────────────────────────────────
async def test_on_text_proposal_then_confirm_ok():
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()

    msg = FakeMessage("возобнови X", chat_id=101, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "pending"
    assert snap.user_initiated is True  # доверенный слой ставит True
    assert bm._LAST_PENDING.get(101) == cid

    async def fake_exec(s, c):  # имитирует контракт execute_confirmed: claim → finalize
        assert await s.claim(c, operation="resume_campaign") is not None
        await s.finalize(c, result={"applied": True})
        return {"applied": True}

    cq = FakeCallbackQuery(FakeMessage(chat_id=101, bot=FakeBot()))
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert (await store.get_confirmed(cid)).status == "applied"
    statuses = await _audit_statuses(cid)
    assert "confirmed" in statuses and "applied" in statuses
    assert cq.message.edits  # сообщение отредактировано (кнопки убраны → «Готово»)


# ── confirm NO ───────────────────────────────────────────────────────────────────
async def test_on_text_proposal_then_cancel():
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    msg = FakeMessage("возобнови X", chat_id=102, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())

    cq = FakeCallbackQuery(FakeMessage(chat_id=102))
    await bm.on_cancel(cq, ConfirmCB(action="no", cid=cid))

    assert (await store.get_confirmed(cid)).status == "rejected"
    assert "rejected" in await _audit_statuses(cid)
    assert bm._LAST_PENDING.get(102) is None  # очищен


# ── устаревший/неизвестный cid: алерт, execute НЕ вызывается ──────────────────────
async def test_confirm_stale_cid_alerts_and_skips_execute():
    await init_db()
    calls = {"n": 0}

    async def fake_exec(s, c):
        calls["n"] += 1
        return {}

    cq = FakeCallbackQuery(FakeMessage(chat_id=103))
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid="bogus-unknown"))
    assert calls["n"] == 0  # без подтверждённого черновика SDK-путь не трогаем
    assert cq.answers and cq.answers[-1][1] is True  # show_alert=True


# ── capability-guard: текстовый ответ агента → НЕ создаём черновик/кнопки ─────────
async def test_on_text_text_decline_creates_no_proposal():
    await init_db()

    async def _decline(text, chat_id, **kwargs):  # **kwargs: терпим к новому context (C1/C3)
        return {"type": "text", "text": "Операция «X» пока не поддерживается."}

    msg = FakeMessage("сделай X", chat_id=104, bot=FakeBot())
    with patched(bm, "handle_command", _decline):
        await bm.on_text(msg, FakeFSM())
    assert msg.answers and "не поддерживается" in msg.answers[-1][0]
    # ответ без inline-кнопок (reply_markup не передан)
    assert "reply_markup" not in msg.answers[-1][1]
    assert bm._LAST_PENDING.get(104) is None  # черновик не создан


@contextmanager
def _mutation_account_env(*, allowed: str, read: str, admin_chat: int):
    """G2: временно настроить видимость/включённость аккаунта + сделать чат админом (обходит
    пер-юзер грант, чтобы get_active_account вернул выбранный аккаунт), затем вернуть как было."""
    pa = settings.google_ads_allowed_customer_ids
    pr = settings.google_ads_read_customer_ids
    padm = settings.admin_chat_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read
    settings.admin_chat_ids = str(admin_chat)
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr
        settings.admin_chat_ids = padm


_CHILD = "1112223334"


async def test_agentloop_mutation_targets_enabled_active_account_with_banner():
    """G2: активный НЕ-Draft аккаунт, включённый на мутации → NL-черновик штампует его + баннер."""
    await init_db()
    from ads.client import set_discovered_read_children, set_discovered_read_children_meta
    from ads.read import ChildAccount
    from core.access import set_active_account

    chat = 9301
    set_discovered_read_children([_CHILD])
    set_discovered_read_children_meta(
        [
            ChildAccount(
                id=_CHILD, name="DARIAL", currency="JPY", manager=False, level=1, status="ENABLED"
            )
        ]
    )
    msg = FakeMessage("поставь на паузу Brand", chat_id=chat, bot=FakeBot())
    try:
        with _mutation_account_env(
            allowed=f"{DRAFT_ACCOUNT_ID},{_CHILD}", read=_CHILD, admin_chat=chat
        ):
            await set_active_account(chat, _CHILD)
            with patched(bm, "handle_command", _proposal_handler(uuid.uuid4().hex)):
                await bm.on_text(msg, FakeFSM())
    finally:
        set_discovered_read_children([])
        set_discovered_read_children_meta([])
        await set_active_account(chat, None)
    cid = bm._LAST_PENDING.get(chat)
    assert cid is not None  # черновик создан
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.customer_id == _CHILD  # штамп аккаунта мутации = активный аккаунт
    assert "DARIAL" in snap.summary and "1112223334" in snap.summary  # баннер аккаунта в карточке


async def test_agentloop_mutation_refused_on_read_only_active_account():
    """G2: активный НЕ-Draft аккаунт НЕ включён на мутации (только чтение) → отказ, черновик НЕ создан."""
    await init_db()
    from ads.client import set_discovered_read_children, set_discovered_read_children_meta
    from ads.read import ChildAccount
    from core.access import set_active_account

    chat = 9302
    set_discovered_read_children([_CHILD])
    set_discovered_read_children_meta(
        [
            ChildAccount(
                id=_CHILD, name="DARIAL", currency="JPY", manager=False, level=1, status="ENABLED"
            )
        ]
    )
    bm._LAST_PENDING.pop(chat, None)
    msg = FakeMessage("поставь на паузу Brand", chat_id=chat, bot=FakeBot())
    try:
        # allowed = ТОЛЬКО Draft → _CHILD виден (read), но НЕ мутируем
        with _mutation_account_env(allowed=DRAFT_ACCOUNT_ID, read=_CHILD, admin_chat=chat):
            await set_active_account(chat, _CHILD)
            with patched(bm, "handle_command", _proposal_handler(uuid.uuid4().hex)):
                await bm.on_text(msg, FakeFSM())
    finally:
        set_discovered_read_children([])
        set_discovered_read_children_meta([])
        await set_active_account(chat, None)
    assert bm._LAST_PENDING.get(chat) is None  # черновик НЕ создан (только чтение)
    assert msg.answers and "только" in msg.answers[-1][0].lower()  # сообщение «только для чтения»


async def test_on_text_with_active_wizard_state_does_not_reach_agent():
    """N5: если активен визард-state без своего текст-хендлера (напр. RSA-пикер), on_text НЕ уводит
    текст/URL в агента (раньше URL давал «Прочитал… контекст»), а подсказывает завершить шаг."""
    await init_db()
    called = {"n": 0}

    async def _spy(text, chat_id, **kwargs):
        called["n"] += 1
        return {"type": "text", "text": "не должно вызваться"}

    state = FakeFSM()
    await state.set_state("RsaWizard:picking")  # активный пикер-экран
    msg = FakeMessage("https://example.com/foo", chat_id=888, bot=FakeBot())
    with patched(bm, "handle_command", _spy):
        await bm.on_text(msg, state)
    assert called["n"] == 0  # агент НЕ вызван (URL не утёк в ingest/агента)
    assert msg.answers  # показана подсказка «заверши шаг»
    assert bm._LAST_PENDING.get(888) is None


# ── ошибка исполнения: failed + audit failed ─────────────────────────────────────
async def test_confirm_execute_failure_records_failed():
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    msg = FakeMessage("возобнови X", chat_id=105, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())

    async def fake_exec_fail(s, c):
        raise RuntimeError("sdk boom")

    cq = FakeCallbackQuery(FakeMessage(chat_id=105))
    with patched(bm, "execute_confirmed", fake_exec_fail):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert (await store.get_confirmed(cid)).status == "failed"
    assert "failed" in await _audit_statuses(cid)


# ── Денежная безопасность: успех + сбой пост-успешного edit НЕ помечает failed ─────
async def test_confirm_applied_then_edit_fails_stays_applied():
    """execute_confirmed применил мутацию (status=applied, finalize записан), но пост-успешный
    edit_text бросил TelegramBadRequest («message is not modified»/«to edit not found»). Это НЕ
    должно пометить операцию failed (лишняя audit-строка failed + юзеру FAILED). Находка аудита:
    APPLIED-edit лежал в той же try-ветке, что и record_failure."""
    from aiogram.exceptions import TelegramBadRequest

    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    msg = FakeMessage("возобнови X", chat_id=106, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())

    async def fake_exec(s, c):  # успех: claim → finalize → applied
        assert await s.claim(c, operation="resume_campaign") is not None
        await s.finalize(c, result={"applied": True})
        return {"applied": True}

    class EditFailsMessage(FakeMessage):
        async def edit_text(self, text: str = "", **kw):
            raise TelegramBadRequest(method=None, message="message is not modified")

    cq = FakeCallbackQuery(EditFailsMessage(chat_id=106))
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert (await store.get_confirmed(cid)).status == "applied"  # успех остался успехом
    statuses = await _audit_statuses(cid)
    assert "applied" in statuses
    assert "failed" not in statuses  # косметический сбой edit НЕ дописал failed


# ── ТЗ §5: большой список ключей в черновике → .xlsx-вложение, маленький → инлайн ──
async def test_big_keyword_proposal_attaches_xlsx():
    await init_db()
    cid = uuid.uuid4().hex
    msg = FakeMessage(bot=FakeBot())
    params = {
        "campaign": "Search",
        "keywords": [f"kw{i}" for i in range(30)],  # > KW_INLINE_MAX
        "match_type": "phrase",
    }
    await bm._present_proposal(
        msg, chat_id=201, operation="add_keywords", params=params, summary="raw dict", cid=cid
    )
    assert any(a[0] == "<doc>" for a in msg.answers)  # .xlsx вложение
    assert any(a[1].get("reply_markup") for a in msg.answers)  # сообщение с кнопками ✅/❌
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap is not None and "фразовое" in snap.summary and "{" not in snap.summary


@contextmanager
def _allowed_draft():
    orig = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = DRAFT_ACCOUNT_ID
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = orig


# ── Шов кнопка → РЕАЛЬНЫЙ execute_confirmed → gated apply_* (раньше всегда мокался) ─
async def test_on_confirm_runs_real_execute_confirmed_gates():
    """Интеграция самого важного шва денежного пути: ✅ → реальный execute_confirmed → реальные
    ensure_allowed/claim/user_initiated/drift → gated apply_update_budget. Патчим ТОЛЬКО нижний
    SDK-исполнитель и резолв кампании. Находка аудита: execute_confirmed всегда подменялся фейком."""
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    # Сохраняем подтверждаемый черновик так же, как доверенный _present_proposal (user_initiated=True).
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "Search", "mode": "set_to", "value": 50},
        summary="бюджет Search → 50",
        chat_id=300,
        user_initiated=True,
    )

    fake_ref = SimpleNamespace(id="77", budget_micros=40_000_000, status="ENABLED")
    sdk: dict = {"n": 0}

    def fake_sdk(client, customer_id, campaign_id, micros):  # нижний SDK-исполнитель
        sdk["n"] += 1
        sdk["args"] = (customer_id, campaign_id, micros)
        return {"customer_id": customer_id, "campaign_id": campaign_id, "applied": True}

    cq = FakeCallbackQuery(FakeMessage(chat_id=300, bot=FakeBot()))
    with (
        _allowed_draft(),
        patched(svc, "build_client_async", _fake_client_async),
        patched(resolve, "find_campaign_by_name", lambda *a, **k: fake_ref),
        patched(mut, "_apply_budget_via_sdk", fake_sdk),
    ):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert sdk["n"] == 1  # реальный нижний SDK-исполнитель вызван ровно раз (replay/гонки нет)
    # реальный compute_new_micros: set_to 50 → 50e6 micros; замок отдал DRAFT; резолв дал id=77
    assert sdk["args"] == (DRAFT_ACCOUNT_ID, "77", 50_000_000)
    assert (await store.get_confirmed(cid)).status == "applied"
    assert "applied" in await _audit_statuses(cid)


# ── ТЗ §5/§10: в create_rsa идут ТОЛЬКО одобренные тексты (отклонённый НЕ попадает) ─
async def test_rsa_proposal_excludes_rejected_elements():
    """Поэлементное подтверждение: отклонённый заголовок не должен попасть в объявление. Раньше
    e2e-инвариант (approved-подмножество → create_rsa) не проверялся (находка аудита)."""
    from adcopy.session import SessionStore

    await init_db()
    sstore = SessionStore()
    sid = await sstore.create(
        chat_id=400,
        customer_id=DRAFT_ACCOUNT_ID,
        campaign="Search",
        ad_group_id="1",
        ad_group_name="AG",
        final_url="https://example.com/",
        headlines=["Заголовок один", "Заголовок два", "Заголовок три", "Плохой заголовок"],
        descriptions=["Описание первое достаточно содержательное.", "Описание второе тоже норм."],
    )
    for i in range(3):  # одобряем 3 заголовка
        await sstore.set_state(sid, "h", i, "approved")
    await sstore.set_state(sid, "h", 3, "rejected")  # ОТКЛОНЯЕМ 4-й
    for i in range(2):  # одобряем 2 описания
        await sstore.set_state(sid, "d", i, "approved")

    session = await sstore.get(sid)
    assert session.can_finalize()
    _cid, op, params, _summary = bm._build_rsa_proposal(session)
    assert op == "create_rsa"
    assert params["headlines"] == ["Заголовок один", "Заголовок два", "Заголовок три"]
    assert "Плохой заголовок" not in params["headlines"]  # отклонённый НЕ дошёл до объявления
    assert len(params["descriptions"]) == 2


async def test_small_keyword_proposal_inline_no_doc():
    await init_db()
    cid = uuid.uuid4().hex
    msg = FakeMessage(bot=FakeBot())
    params = {
        "campaign": "Search",
        "keywords": ["купить телефон", "смартфон"],
        "match_type": "broad",
    }
    await bm._present_proposal(
        msg, chat_id=202, operation="add_keywords", params=params, summary="raw", cid=cid
    )
    assert all(a[0] != "<doc>" for a in msg.answers)  # маленький список — без вложения
    assert any(a[1].get("reply_markup") for a in msg.answers)


# ── golden rule #5 E2E: секрет в ошибке исполнения редактируется и в audit, и в сообщении юзеру ─
async def test_confirm_failure_redacts_secret_in_audit_and_user_msg():
    """Сбой execute_confirmed с токено-подобной строкой в тексте → проверяем редакцию на ОБОИХ
    исходящих рубежах: (а) audit_log.result (путь confirm.store.record_failure → redact_text);
    (б) сообщение юзеру под кнопкой (путь humanize_google_ads_error → redact_text). Существующий
    test_confirm_execute_failure_records_failed гоняет безобидный 'sdk boom' и редакцию НЕ ловит;
    test_logging_redaction.py тестирует redact_text в вакууме, но не живые границы."""
    from core.logging import REDACTED

    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    secret = (
        "1//0gLEAKEDrefreshTOKEN_must_not_surface_123"  # синтетика под паттерн '1//' (не реальный)
    )

    msg = FakeMessage("возобнови X", chat_id=130, bot=FakeBot())
    with patched(bm, "handle_command", _proposal_handler(cid)):
        await bm.on_text(msg, FakeFSM())

    async def fake_exec_leak(s, c):  # ошибка SDK/google.auth несёт «секрет» в тексте
        raise RuntimeError(f"google.auth refresh failed: token={secret}")

    cq = FakeCallbackQuery(FakeMessage(chat_id=130))
    with patched(bm, "execute_confirmed", fake_exec_leak):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    # (а) audit: строка failed есть, секрет редактирован (а не записан/не пусто)
    assert (await store.get_confirmed(cid)).status == "failed"
    async with Session() as s:
        row = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == "failed"
                    )
                )
            )
            .scalars()
            .first()
        )
    assert row is not None
    audit_text = str(row.result)
    assert secret not in audit_text  # ⛔ секрет в audit_log — утечка (golden rule #5)
    assert REDACTED in audit_text  # именно редакция, а не потеря текста

    # (б) сообщение юзеру (edit_text под кнопкой) санитизировано тем же маркером
    user_text = " ".join(t for t, _ in cq.message.edits)
    assert secret not in user_text  # ⛔ секрет ушёл в Telegram — утечка
    assert REDACTED in user_text


# ── golden rule #3 E2E: бюджет с user_initiated=False сквозь РЕАЛЬНЫЙ execute_confirmed заблокирован ─
async def test_scheduler_style_budget_blocked_through_execute_confirmed():
    """Черновик update_budget с user_initiated=False (как создал бы scheduler/anomaly) проходит
    РЕАЛЬНЫЙ execute_confirmed → упирается в гейт user_initiated → audit failed, нижний SDK НЕ вызван
    ни разу. Зеркало test_on_confirm_runs_real_execute_confirmed_gates (там True) — негативная ветка
    §3 на сквозном шве бот→execute_confirmed (раньше покрыто только apply_*-юнитом)."""
    await init_db()
    cid = uuid.uuid4().hex
    store = ConfirmStore()
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "Search", "mode": "set_to", "value": 50},
        summary="бюджет Search → 50 (как от scheduler)",
        chat_id=310,
        user_initiated=False,  # НЕ прямая команда пользователя
    )

    fake_ref = SimpleNamespace(id="77", budget_micros=40_000_000, status="ENABLED")
    sdk: dict = {"n": 0}

    def fake_sdk(client, customer_id, campaign_id, micros):  # нижний SDK-исполнитель
        sdk["n"] += 1
        return {"applied": True}

    cq = FakeCallbackQuery(FakeMessage(chat_id=310, bot=FakeBot()))
    with (
        _allowed_draft(),
        patched(svc, "build_client_async", _fake_client_async),
        patched(resolve, "find_campaign_by_name", lambda *a, **k: fake_ref),
        patched(mut, "_apply_budget_via_sdk", fake_sdk),
    ):
        await bm.on_confirm(cq, ConfirmCB(action="ok", cid=cid))

    assert sdk["n"] == 0  # бюджет НЕ тронут — гейт user_initiated отсёк ДО SDK (§3)
    assert (await store.get_confirmed(cid)).status == "failed"  # черновик помечен failed
    assert "failed" in await _audit_statuses(cid)

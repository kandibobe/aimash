"""§6 «сообщить об ошибке»: /reportbug (пользователь) + /bugs (админ-триаж) + core.bugs store.

Реальная БД на temp SQLite (init_db). Проверяем: текст РЕДАКТИРУЕТСЯ перед записью (golden rule #5),
статусы триажа, что хендлер сохраняет + отвечает тикет-кодом + форвардит админам, и что /bugs
гейтится админом. Ничего не мутирует в Google Ads (локальная таблица bug_reports).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from core import bugs  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

_SECRET = "1//0SECRETrefreshTOKENvalue123"  # gitleaks:allow — форма refresh-токена


@contextmanager
def _admins(value: str):
    prev = settings.admin_chat_ids
    settings.admin_chat_ids = value
    try:
        yield
    finally:
        settings.admin_chat_ids = prev


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text: str = "", **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id))


class FakeMessage:
    def __init__(self, chat_id: int = 100, text: str = "", username: str | None = "op"):
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.caption = None
        self.from_user = SimpleNamespace(id=chat_id, username=username)
        self.bot = FakeBot()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self


class FakeState:
    def __init__(self):
        self._data: dict = {}
        self._state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self._state = s

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


# ── core.bugs: редакция + статусы ─────────────────────────────────────────────────
async def test_add_bug_report_redacts_secret_before_store():
    """golden rule #5: секрето-подобный текст сохраняется РЕДАКТИРОВАННО (не как есть)."""
    await init_db()
    bug_id = await bugs.add_bug_report(
        555, f"бот упал, вот токен refresh_token={_SECRET} извините", username="tester"
    )
    row = await bugs.get_bug_report(bug_id)
    assert row is not None
    assert _SECRET not in row.text, "секрет утёк в bug_reports (golden rule #5)"
    assert "REDACTED" in row.text
    assert row.status == "new" and row.username == "tester"


async def test_bug_status_transitions():
    await init_db()
    bug_id = await bugs.add_bug_report(556, "кнопка не работает")
    assert await bugs.set_bug_status(bug_id, "triaged", triaged_by=999) is True
    row = await bugs.get_bug_report(bug_id)
    assert row.status == "triaged" and row.triaged_by == 999
    assert await bugs.set_bug_status(bug_id, "closed") is True
    assert (await bugs.get_bug_report(bug_id)).status == "closed"
    assert await bugs.set_bug_status(10_000_000, "closed") is False  # нет строки
    with pytest.raises(ValueError):
        await bugs.set_bug_status(bug_id, "bogus")


# ── хендлер: сохранение + тикет-код + форвард админам ─────────────────────────────
async def test_reportbug_text_stores_and_forwards_to_admins():
    await init_db()
    m = FakeMessage(chat_id=100, text=f"всё сломалось refresh_token={_SECRET}", username="petya")
    state = FakeState()
    await state.set_state(bm.BugReportWizard.awaiting_text)
    with _admins("999,100"):  # 999 — сторонний админ; 100 — сам автор (не должен получить форвард)
        await bm.reportbug_text(m, state)
    # автору — подтверждение с тикет-кодом; состояние очищено
    assert any("✅" in t or "Спасибо" in t or "Thanks" in t for t, _ in m.answers)
    assert await state.get_state() is None
    # форвард ушёл СТОРОННЕМУ админу (999), не автору (100); секрет отредактирован
    fwd = m.bot.sent
    assert any(chat == 999 for chat, _ in fwd)
    assert all(chat != 100 for chat, _ in fwd)  # автор не дублируется форвардом
    assert all(_SECRET not in text for _, text in fwd), "секрет утёк в форвард админам"
    # запись реально сохранена (редактированно)
    rows = await bugs.list_bug_reports(limit=5)
    assert rows and _SECRET not in rows[0].text


async def test_reportbug_empty_stays_in_state():
    await init_db()
    m = FakeMessage(chat_id=101, text="   ")
    state = FakeState()
    await state.set_state(bm.BugReportWizard.awaiting_text)
    await bm.reportbug_text(m, state)
    assert await state.get_state() == bm.BugReportWizard.awaiting_text  # остаёмся ждать текст
    assert m.answers  # подсказка про пустой ввод


async def test_bugs_cmd_admin_only():
    await init_db()
    non_admin = FakeMessage(chat_id=100)
    with _admins("999"):
        await bm.bugs_cmd(non_admin)
    assert non_admin.answers and (
        "admin" in non_admin.answers[0][0].lower() or "админ" in non_admin.answers[0][0].lower()
    )

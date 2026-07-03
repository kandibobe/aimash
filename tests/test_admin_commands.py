"""2C: админ-команды грантов (/grant /revoke) + самодиагностика (/accounts /whoami).

Fail-closed: пустой ADMIN_CHAT_IDS ⇒ /grant и /revoke недоступны никому; /grant принимает
только read-allowed аккаунт. Реальная temp-SQLite (conftest), SDK/сеть не трогаем.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ⚠️ Хендлеры берём через bot.main (ре-экспорт star-import), НЕ прямым импортом
# bot.handlers.commands: прямой импорт первым (напр. на pytest-коллекции) запускает
# циклический double-import — commands регистрируется ПОСЛЕ fallback, on_text перестаёт
# быть последним и глотает слэш-команды других тестов (см. гард в bot/main.py у dp).
import bot.main as bm  # noqa: E402
from core.access import list_user_account_ids, revoke_account_access  # noqa: E402
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

accounts_cmd, grant_cmd = bm.accounts_cmd, bm.grant_cmd
revoke_cmd, whoami_cmd = bm.revoke_cmd, bm.whoami_cmd

DRAFT = "7753643025"
ACCT = "5556667778"


class FakeMessage:
    def __init__(self, chat_id: int):
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)


def _cmd(args: str | None):
    return SimpleNamespace(args=args)


async def test_grant_denied_when_admins_empty(monkeypatch):
    """Пустой ADMIN_CHAT_IDS ⇒ отказ ВСЕМ (fail-closed), грант не создан."""
    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "")
    m = FakeMessage(chat_id=800)
    await grant_cmd(m, _cmd(f"801 {ACCT}"))
    assert (
        m.answers and "администратор" in m.answers[-1].lower() or "admin" in m.answers[-1].lower()
    )
    assert ACCT not in await list_user_account_ids(801)


async def test_grant_denied_for_non_admin(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "999")
    m = FakeMessage(chat_id=800)  # не админ
    await grant_cmd(m, _cmd(f"801 {ACCT}"))
    assert ACCT not in await list_user_account_ids(801)


async def test_grant_requires_read_allowed_account(monkeypatch):
    """Грант только на реально читаемый аккаунт (read-замок); иначе подсказка, грант не создан."""
    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "800")
    # conftest: allow/read-list пусты → ACCT не читаем
    m = FakeMessage(chat_id=800)
    await grant_cmd(m, _cmd(f"801 {ACCT}"))
    assert ACCT not in await list_user_account_ids(801)
    assert any("refresh" in a.lower() or "не входит" in a.lower() for a in m.answers)


async def test_grant_and_revoke_roundtrip(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "800")
    monkeypatch.setattr(settings, "google_ads_read_customer_ids", ACCT)  # читаемый
    m = FakeMessage(chat_id=800)
    try:
        await grant_cmd(m, _cmd("801 555-666-7778"))  # дефис-форма нормализуется
        assert ACCT in await list_user_account_ids(801)
        await revoke_cmd(m, _cmd(f"801 {ACCT}"))
        assert ACCT not in await list_user_account_ids(801)
    finally:
        await revoke_account_access(801, ACCT)


async def test_grant_bad_args(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "800")
    m = FakeMessage(chat_id=800)
    await grant_cmd(m, _cmd("только-один-аргумент"))
    assert m.answers and "/grant" in m.answers[-1]


async def test_accounts_and_whoami_render(monkeypatch):
    """/accounts перечисляет доступные (как минимум Draft); /whoami показывает chat_id/режим."""
    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT)
    m = FakeMessage(chat_id=802)
    await accounts_cmd(m)
    assert m.answers and DRAFT in m.answers[-1]

    w = FakeMessage(chat_id=802)
    await whoami_cmd(w)
    out = w.answers[-1]
    assert "802" in out and DRAFT in out

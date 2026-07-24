"""#6: режим доп-вопросов (Q&A) по /audit — bot-слой (хендлеры reports.on_audit_qa_*).

Инварианты (каждый — про безопасность/честность, не про красоту):
• вопрос маршрутизируется ТОЛЬКО в run_analysis_agent (READ-ONLY аналитик), НИКОГДА в агент-исполнитель
  с мутациями — question прокидывается, drill строится из кэша (аккаунт залочен);
• тёплый кэш → ответ, остаёмся в режиме (можно спросить ещё); холодный (рестарт) → снимаем FSM + подсказка;
• None от аналитика (fact-guard/сбой/бюджет-стоп) → мягкий фолбэк, НЕ выдуманный ответ, режим сохраняется;
• «✖ Выйти» — снимает FSM и контекст-кэш;
• kill-switch settings.audit_qa_enabled существует и по умолчанию включён (независим от narrative).
"""

from __future__ import annotations

import inspect
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot.handlers.reports as reports  # noqa: E402
import bot.main as bm  # noqa: E402

CHAT = 555


class _FakeChat:
    id = CHAT


class _FakeMessage:
    def __init__(self, text: str = ""):
        self.chat = _FakeChat()
        self.text = text
        self.bot = None  # ux.typing_action → тихий no-op без бота
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class _FakeState:
    def __init__(self, state: str | None = "AuditQA:active"):
        self._state = state
        self.cleared = False

    async def get_state(self):
        return self._state

    async def set_state(self, s):
        self._state = s

    async def clear(self):
        self.cleared = True
        self._state = None


class _FakeCq:
    def __init__(self, msg):
        self.message = msg
        self.from_user = SimpleNamespace(id=CHAT)
        self.answered = False

    async def answer(self, *a, **k):
        self.answered = True


# ── вопрос → run_analysis_agent (read-only), question проброшен, остаёмся в режиме ──────
async def test_qa_question_routes_to_analysis_agent(monkeypatch):
    facts = {"score": 62, "money_at_risk": 8900.0}
    monkeypatch.setattr(bm, "_AUDIT_QA_CACHE", {CHAT: (facts, "7753643025", object())})
    seen: dict = {}

    async def fake_client(acct):
        seen["acct"] = acct
        return "CLIENT"

    monkeypatch.setattr("ads.client.build_client_async", fake_client)
    monkeypatch.setattr(bm, "_make_audit_drill", lambda c, a, p: "DRILL")

    async def fake_agent(f, *, chat_id, lang, question, drill):
        seen["facts"], seen["question"], seen["drill"] = f, question, drill
        return "Под риском 8900 USD — спенд без конверсий. Приоритет: пауза."

    monkeypatch.setattr("agent.loop.run_analysis_agent", fake_agent)

    m = _FakeMessage("Почему так много слито?")
    st = _FakeState()
    await reports.on_audit_qa_question(m, st)

    assert seen["question"] == "Почему так много слито?"  # вопрос проброшен как есть
    assert seen["facts"] is facts  # кэшированные факты (аудит не пересобираем)
    assert (
        seen["drill"] == "DRILL" and seen["acct"] == "7753643025"
    )  # drill из кэша, аккаунт залочен
    assert m.answers and "8900" in m.answers[0][0]
    assert st.cleared is False  # остаёмся в режиме — можно спросить ещё


async def test_qa_cold_cache_clears_state(monkeypatch):
    """Холодный кэш (рестарт/новый /audit затёр) → снимаем FSM + подсказка, аналитик НЕ зван."""
    monkeypatch.setattr(bm, "_AUDIT_QA_CACHE", {})
    called: list = []

    async def fake_agent(*a, **k):
        called.append(1)
        return "x"

    monkeypatch.setattr("agent.loop.run_analysis_agent", fake_agent)
    m = _FakeMessage("вопрос")
    st = _FakeState()
    await reports.on_audit_qa_question(m, st)
    assert st.cleared is True
    assert called == []  # аналитик не звался (нет контекста)
    assert m.answers  # stale-подсказка показана


async def test_qa_none_answer_shows_fallback_stays_in_mode(monkeypatch):
    """None от аналитика (fact-guard/сбой) → мягкий фолбэк (не выдуманный ответ), режим сохраняется."""
    monkeypatch.setattr(bm, "_AUDIT_QA_CACHE", {CHAT: ({"score": 1}, "acct", None)})

    async def fake_client(a):
        return "C"

    monkeypatch.setattr("ads.client.build_client_async", fake_client)
    monkeypatch.setattr(bm, "_make_audit_drill", lambda *a: "D")

    async def fake_agent(*a, **k):
        return None

    monkeypatch.setattr("agent.loop.run_analysis_agent", fake_agent)
    m = _FakeMessage("q")
    st = _FakeState()
    await reports.on_audit_qa_question(m, st)
    assert st.cleared is False  # остаёмся в режиме
    assert m.answers  # фолбэк-сообщение показано


async def test_qa_agent_exception_is_soft_fallback(monkeypatch):
    """Сбой сборки клиента/агента → мягкий фолбэк, сырой текст ошибки наружу НЕ идёт (GR5)."""
    monkeypatch.setattr(bm, "_AUDIT_QA_CACHE", {CHAT: ({"score": 1}, "acct", None)})

    async def boom(a):
        raise RuntimeError("secret-token-leak-9x8")

    monkeypatch.setattr("ads.client.build_client_async", boom)
    m = _FakeMessage("q")
    st = _FakeState()
    await reports.on_audit_qa_question(m, st)
    assert m.answers  # фолбэк показан
    assert "secret-token-leak-9x8" not in m.answers[0][0]  # сырой str(e) не утёк
    assert st.cleared is False


async def test_qa_exit_clears_state_and_cache(monkeypatch):
    """«✖ Выйти» — снимает FSM и контекст-кэш."""
    monkeypatch.setattr(bm, "_AUDIT_QA_CACHE", {CHAT: ({}, "acct", None)})
    m = _FakeMessage()
    cq = _FakeCq(m)
    st = _FakeState()
    await reports.on_audit_qa_exit(cq, st)
    assert st.cleared is True
    assert CHAT not in bm._AUDIT_QA_CACHE
    assert cq.answered is True
    assert m.answers  # сообщение о выходе


# ── статический гард: маршрут ТОЛЬКО в read-only аналитик, без пути к мутациям ───────────
def test_qa_handler_is_readonly_no_command_path():
    src = inspect.getsource(reports.on_audit_qa_question)
    assert "run_analysis_agent" in src  # единственный маршрут — read-only аналитик
    assert "handle_command" not in src  # НЕ агент-исполнитель
    assert "MUTATION" not in src and "_present_proposal" not in src  # ни намёка на мутации


def test_qa_killswitch_default_on():
    """kill-switch независим от narrative и по умолчанию включён (см. core.config)."""
    from core.config import Settings

    s = Settings()
    assert s.audit_qa_enabled is True

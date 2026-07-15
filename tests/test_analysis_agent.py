"""Юнит-тесты агентного НАРРАТИВА аудита (P3) — fact-guard, S4-инвариант, multi-turn, fallback'ы.

Без сети/SDK: chat() и drill монкейпатчатся. Доказывают ключевые свойства безопасности:
- нарратив с ВЫДУМАННЫМ числом отвергается (S3 fact-guard) → детерминированный fallback (None);
- ANALYSIS_TOOLS не содержат мутаций и не дают модели выбрать аккаунт (S4/GR9);
- неизвестный тул от модели drill НЕ исполняет (allow-list, defense-in-depth);
- любой сбой/timeout/бюджет-стоп → None (карточка остаётся у пользователя).
"""

from __future__ import annotations

from agent.loop import _assistant_message_dict, run_analysis_agent
from audit.factguard import collect_numbers, narrative_facts_preserved

FACTS = {
    "customer_id": "1234567890",
    "currency": "USD",
    "period_days": 30,
    "score": 62,
    "grade": "C",
    "total_spend": 42500.0,
    "money_at_risk": 8900.0,
    "findings_count": 1,
    "findings": [
        {
            "issue": "spend_no_conv",
            "family": "waste",
            "campaign": "Brand-RU",
            "at_risk": 8900.0,
            "cost": 8900.0,
            "clicks": 412,
        }
    ],
}


# ── фейки SDK-сообщений (chat() отдаёт объект с .content/.tool_calls/.model_dump()) ──────
class _FakeFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _FakeFn(name, arguments)


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        return d


def _scripted_chat(responses):
    state = {"n": 0, "turns": []}

    async def _chat(messages, *, role="parsing", tools=None, **kw):
        i = state["n"]
        state["n"] += 1
        state["turns"].append({"role": role, "tools_present": tools is not None})
        return responses[min(i, len(responses) - 1)]

    return _chat, state


# ── fact-guard ─────────────────────────────────────────────────────────────────────────
def test_factguard_collects_and_matches():
    nums = collect_numbers(FACTS)
    assert 8900.0 in nums and 412.0 in nums and 62.0 in nums
    assert narrative_facts_preserved("Слил 8900 USD за 412 кликов, 0 конверсий.", nums)
    # порядковые/знаменатель — не факты (allowlist)
    assert narrative_facts_preserved("Топ-3 приоритета, оценка 62 из 100.", nums)


def test_factguard_rejects_fabricated_and_empty():
    nums = collect_numbers(FACTS)
    assert not narrative_facts_preserved("Потеряно 55000 USD в никуда.", nums)  # выдуманное
    assert not narrative_facts_preserved("", nums)  # пусто → нечего показывать


def test_factguard_thousands_separators():
    nums = collect_numbers({"cost": 8900.0})
    assert narrative_facts_preserved("Слил 8 900.00 USD.", nums)  # пробел-разделитель тысяч
    assert narrative_facts_preserved("Спущено 8,900 USD.", nums)  # запятая-разделитель тысяч


def test_factguard_allows_year_tokens():
    nums = collect_numbers({"cost": 8900.0})
    assert narrative_facts_preserved("С начала 2026 года кампания слила 8900 USD.", nums)


# ── S4 / GR9: инструменты аналитика — read-only, без выбора аккаунта ─────────────────────
def test_analysis_tools_no_mutation_no_account():
    from agent.tools.schemas import ANALYSIS_TOOL_NAMES, ANALYSIS_TOOLS, MUTATION_TOOLS

    assert ANALYSIS_TOOL_NAMES.isdisjoint(MUTATION_TOOLS)  # S4: ноль мутаций в цикле
    for t in ANALYSIS_TOOLS:
        props = t["function"]["parameters"].get("properties", {})
        assert "account" not in props  # аккаунт залочен bot-слоем (GR9)


# ── _assistant_message_dict: и SDK-объект (model_dump), и простой фейк ───────────────────
def test_assistant_message_dict_serializes_tool_calls():
    msg = _FakeMsg(tool_calls=[_FakeToolCall("c1", "get_campaign_detail", '{"campaign":"X"}')])
    d = _assistant_message_dict(msg)
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "get_campaign_detail"
    assert d["tool_calls"][0]["id"] == "c1"


# ── run_analysis_agent: multi-turn happy path ───────────────────────────────────────────
async def test_multiturn_drills_then_narrates(monkeypatch):
    drill_calls = []

    async def drill(name, args):
        drill_calls.append((name, args))
        return {"found": True, "campaign": "Brand-RU", "daily_budget": 300.0}

    resp0 = _FakeMsg(
        tool_calls=[_FakeToolCall("c1", "get_campaign_detail", '{"campaign":"Brand-RU"}')]
    )
    resp1 = _FakeMsg(
        content="Кампания «Brand-RU» слила 8900 USD при бюджете 300 USD и 0 конверсий. Приоритет: пауза."
    )
    chat_fn, state = _scripted_chat([resp0, resp1])
    monkeypatch.setattr("agent.loop.chat", chat_fn)

    out = await run_analysis_agent(FACTS, chat_id=1, lang="ru", drill=drill)
    assert out is not None and "Brand-RU" in out
    assert drill_calls == [("get_campaign_detail", {"campaign": "Brand-RU"})]
    assert state["turns"][0]["tools_present"] is True  # первый ход — с инструментами
    assert state["turns"][0]["role"] == "analyst"


async def test_narrative_with_fabricated_number_is_rejected(monkeypatch):
    resp = _FakeMsg(content="Аккаунт потерял 777777 USD — полная катастрофа.")  # нет в фактах
    chat_fn, _ = _scripted_chat([resp])
    monkeypatch.setattr("agent.loop.chat", chat_fn)
    assert await run_analysis_agent(FACTS, chat_id=1, lang="ru", drill=None) is None


async def test_no_drill_single_shot(monkeypatch):
    resp = _FakeMsg(content="Здоровье 62 из 100, под риском 8900 USD. Приоритет: пауза Brand-RU.")
    chat_fn, state = _scripted_chat([resp])
    monkeypatch.setattr("agent.loop.chat", chat_fn)
    out = await run_analysis_agent(FACTS, chat_id=1, lang="ru", drill=None)
    assert out and "62" in out
    assert state["turns"][0]["tools_present"] is False  # без drill → без инструментов


async def test_network_error_falls_back_to_none(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr("agent.loop.chat", boom)
    assert await run_analysis_agent(FACTS, chat_id=1, drill=None) is None


async def test_budget_exceeded_skips_narrative(monkeypatch):
    from core import llm_budget

    def _raise(cid):
        raise llm_budget.LLMBudgetExceededError(500, 500)

    async def _chat_never(*a, **k):
        raise AssertionError("chat не должен вызываться при исчерпанном бюджете")

    monkeypatch.setattr("core.llm_budget.consume", _raise)
    monkeypatch.setattr("agent.loop.chat", _chat_never)
    assert await run_analysis_agent(FACTS, chat_id=1, drill=None) is None


async def test_unknown_tool_from_model_is_not_executed(monkeypatch):
    """Модель эмитит несуществующий (мутационно-звучащий) тул → drill НЕ вызывается (allow-list)."""
    called = []

    async def drill(name, args):
        called.append(name)
        return {"ok": True}

    resp0 = _FakeMsg(tool_calls=[_FakeToolCall("c1", "remove_campaign", "{}")])
    resp1 = _FakeMsg(content="Всё разобрано, под риском 8900 USD. Приоритет: пауза.")
    chat_fn, _ = _scripted_chat([resp0, resp1])
    monkeypatch.setattr("agent.loop.chat", chat_fn)

    out = await run_analysis_agent(FACTS, chat_id=1, lang="ru", drill=drill)
    assert out is not None  # цикл не упал
    assert called == []  # неизвестный тул к drill не ушёл


# ── #6: режим доп-вопросов (Q&A) — question=... переключает промпт/сид, fact-guard сохраняется ──
def _capturing_chat(response):
    """chat-фейк, запоминающий messages первого хода (system + seed) для инспекции промпта."""
    seen: dict = {}

    async def _chat(messages, *, role="parsing", tools=None, **kw):
        if not seen:
            seen["system"] = messages[0]["content"]
            seen["seed"] = messages[1]["content"]
            seen["role"] = role
        return response

    return _chat, seen


async def test_qa_uses_qa_prompt_and_embeds_question(monkeypatch):
    """question=... → используется Q&A-системный промпт (не обзорный) и вопрос попадает в сид."""
    from agent.loop import _ANALYST_QA_SYSTEM, _ANALYST_SYSTEM

    resp = _FakeMsg(content="Под риском 8900 USD в кампании Brand-RU — это спенд без конверсий.")
    chat_fn, seen = _capturing_chat(resp)
    monkeypatch.setattr("agent.loop.chat", chat_fn)

    q = "Почему у меня так много слитых денег?"
    out = await run_analysis_agent(FACTS, chat_id=1, lang="ru", drill=None, question=q)
    assert out is not None and "8900" in out
    assert seen["system"] == _ANALYST_QA_SYSTEM["ru"]  # Q&A-промпт, НЕ обзорный
    assert seen["system"] != _ANALYST_SYSTEM["ru"]
    assert q in seen["seed"]  # вопрос клиента в сиде
    assert seen["role"] == "analyst"  # роль читателя (read-only цикл)


async def test_qa_factguard_still_rejects_fabricated(monkeypatch):
    """Fact-guard действует и в Q&A: выдуманное число → None (не показываем непроверенный ответ)."""
    resp = _FakeMsg(content="На самом деле ты потерял 500000 USD, всё пропало.")  # нет в фактах
    chat_fn, _ = _capturing_chat(resp)
    monkeypatch.setattr("agent.loop.chat", chat_fn)
    out = await run_analysis_agent(
        FACTS, chat_id=1, lang="ru", drill=None, question="Сколько слито?"
    )
    assert out is None


async def test_qa_blank_question_is_overview_mode(monkeypatch):
    """Пустой/пробельный question ⇒ обычный обзорный режим (не падаем, промпт обзорный)."""
    from agent.loop import _ANALYST_SYSTEM

    resp = _FakeMsg(content="Здоровье 62 из 100, под риском 8900 USD. Приоритет: пауза Brand-RU.")
    chat_fn, seen = _capturing_chat(resp)
    monkeypatch.setattr("agent.loop.chat", chat_fn)
    out = await run_analysis_agent(FACTS, chat_id=1, lang="ru", drill=None, question="   ")
    assert out is not None
    assert seen["system"] == _ANALYST_SYSTEM["ru"]  # blank → обзорный, не Q&A

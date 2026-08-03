"""Read-only audit narrative agent used by the Hermes MCP surface."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from core.logging import redact_text
from llm.router import chat

log = logging.getLogger(__name__)


# ── P3: агентный НАРРАТИВ аудита (multi-turn, READ-ONLY) ────────────────────────────────
# Отдельный цикл — НЕ трогает handle_command/_do_read (денежный путь не регрессирует). Генерит
# ЧЕЛОВЕЧЕСКИЙ разбор ПОВЕРХ уже посчитанного КОДОМ аудита (числа даёт движок audit/, не модель).
# Инструменты — только ANALYSIS_TOOLS (read-only, аккаунт залочен bot-слоем; S4-инвариант: ноль
# мутаций). На выходе — fact-guard (S3): любое выдуманное число ⇒ нарратив отвергнут, bot-слой
# показывает детерминированную карточку. Любой сбой/timeout/бюджет-стоп ⇒ None (тот же fallback).
ANALYSIS_MAX_ITERS = 4  # ходов с инструментами + финальный текстовый (защита TTFT/дневного бюджета)
ANALYSIS_TIMEOUT_S = 45.0  # общий бюджет времени на нарратив; превышение ⇒ детерминир. карточка

_ANALYST_SYSTEM = {
    "ru": (
        "Ты — эксперт-аналитик Google Ads. По ГОТОВЫМ данным аудита объясни клиенту простым "
        "человеческим языком: диагноз здоровья аккаунта, где утекают деньги, что чинить первым и "
        "почему. ЖЁСТКИЕ ПРАВИЛА: (1) Все числа (суммы, CPA, проценты, клики) бери ТОЛЬКО из данных "
        "аудита и выводов инструментов — НИКОГДА не считай и не выдумывай новые. (2) Ты НИЧЕГО не "
        "меняешь в аккаунте — только советуешь; любое изменение пользователь запускает отдельной "
        "командой. (3) Коротко (5–9 предложений), по делу, без воды, без таблиц и markdown-разметки. "
        "(4) Заверши 2–3 приоритетами «что сделать первым». Отвечай ПО-РУССКИ."
    ),
    "en": (
        "You are an expert Google Ads analyst. Using the READY audit data, explain to the client in "
        "plain human language: the account's health diagnosis, where money is leaking, what to fix "
        "first and why. HARD RULES: (1) Take every number (amounts, CPA, percentages, clicks) ONLY "
        "from the audit data and tool outputs — NEVER compute or invent new ones. (2) You change "
        "NOTHING in the account — you only advise; the user runs any change with a separate command. "
        "(3) Keep it short (5–9 sentences), concrete, no fluff, no tables or markdown. (4) Finish "
        "with 2–3 priorities of what to do first. Answer in ENGLISH."
    ),
}
_ANALYST_SEED = {
    "ru": (
        "Вот РЕЗУЛЬТАТ аудита аккаунта (числа уже посчитаны кодом — бери их как есть). Объясни его "
        "клиенту. При желании уточни детали инструментами. ДАННЫЕ АУДИТА:\n"
    ),
    "en": (
        "Here is the account AUDIT RESULT (numbers already computed by code — use them as-is). "
        "Explain it to the client. Optionally drill for detail via tools. AUDIT DATA:\n"
    ),
}
# Q&A-режим: пользователь задаёт КОНКРЕТНЫЙ вопрос по уже показанному аудиту. Те же жёсткие правила
# (числа только из данных/инструментов, read-only), плюс: (а) отвечать на заданный вопрос, не пересказывать
# весь аудит; (б) если просят ЧТО-ТО ИЗМЕНИТЬ (пауза/бюджет/ставка/ключи) — объяснить, что режим только
# читает, и подсказать КОМАНДУ (/pause, /newsearch, /addkeys, /rsa …); изменение исполняется отдельно
# через подтверждение. Нет данных для ответа — честно сказать «в этом аудите таких данных нет».
_ANALYST_QA_SYSTEM = {
    "ru": (
        "Ты — эксперт-аналитик Google Ads. Клиенту уже показан аудит его аккаунта; теперь он задаёт "
        "ВОПРОС по нему. ЖЁСТКИЕ ПРАВИЛА: (1) Все числа (суммы, CPA, проценты, клики) бери ТОЛЬКО из "
        "данных аудита и выводов инструментов — НИКОГДА не считай и не выдумывай новые. (2) Ты НИЧЕГО "
        "не меняешь в аккаунте — только читаешь и советуешь. Если вопрос — это просьба ИЗМЕНИТЬ "
        "(поставить на паузу, поднять/снизить бюджет или ставку, добавить ключи/объявления), объясни, "
        "что здесь только разбор, и подскажи подходящую КОМАНДУ бота (например /pause, /resume, "
        "/newsearch, /addkeys, /rsa) — сам менеджер запустит её, изменение пройдёт через подтверждение. "
        "(3) Отвечай КОНКРЕТНО на заданный вопрос, коротко (2–6 предложений), без воды, без таблиц и "
        "markdown-разметки. (4) Прежде чем сказать «нет данных» — попробуй ИНСТРУМЕНТ: про конкурентов "
        "(домены, доли показов) вызови get_competitors; про ставки/позиции — get_bid_landscape; про "
        "поисковые запросы — get_search_terms; про кампанию — get_campaign_detail. Только если "
        "инструмент вернул has_data:false или пусто — тогда честно скажи, чего не хватает: срез "
        "конкурентов загружают командой /competitors (Google имён соперников через API не отдаёт). "
        "(5) Отличай «нет ДАННЫХ» от «нет СОВЕТА»: на автоматических стратегиях (Smart Bidding / tCPA / "
        "Maximize) ставки задаёт алгоритм Google — ручной ставки, которую можно «поднять», там нет; так "
        "и скажи, не выдумывай число. Ручную ставку показывает get_bid_landscape (strategy_type). "
        "(6) Если в данных и после инструментов нужного нет — честно скажи об этом, не выдумывай. "
        "Отвечай ПО-РУССКИ."
    ),
    "en": (
        "You are an expert Google Ads analyst. The client has already seen the audit of their account "
        "and now asks a QUESTION about it. HARD RULES: (1) Take every number (amounts, CPA, "
        "percentages, clicks) ONLY from the audit data and tool outputs — NEVER compute or invent new "
        "ones. (2) You change NOTHING in the account — you only read and advise. If the question is a "
        "request to CHANGE something (pause, raise/lower a budget or bid, add keywords/ads), explain "
        "that this is analysis only and point to the right bot COMMAND (e.g. /pause, /resume, "
        "/newsearch, /addkeys, /rsa) — the manager runs it and the change goes through a confirmation. "
        "(3) Answer the specific question CONCRETELY, briefly (2–6 sentences), no fluff, no tables or "
        "markdown. (4) Before saying 'no data' — try a TOOL: for competitors (domains, impression "
        "share) call get_competitors; for bids/positions call get_bid_landscape; for search queries "
        "get_search_terms; for a campaign get_campaign_detail. Only if the tool returns has_data:false "
        "or empty, say honestly what's missing: the competitor snapshot is loaded via /competitors "
        "(Google does not expose rival names through the API). (5) Distinguish 'no DATA' from 'no "
        "ADVICE': on automated strategies (Smart Bidding / tCPA / Maximize) Google's algorithm sets "
        "the bids — there is no manual bid to 'raise'; say so, don't invent a number. Manual bids show "
        "in get_bid_landscape (strategy_type). (6) If the data and tools still lack what's needed, say "
        "so honestly — do not invent. Answer in ENGLISH."
    ),
}
_ANALYST_QA_SEED = {
    "ru": "Данные последнего аудита аккаунта (числа посчитаны кодом — бери как есть):\n",
    "en": "Data from the account's latest audit (numbers computed by code — use as-is):\n",
}
_ANALYST_QA_Q = {"ru": "\n\nВОПРОС КЛИЕНТА: ", "en": "\n\nCLIENT'S QUESTION: "}


def _assistant_message_dict(msg: Any) -> dict[str, Any]:
    """Сериализовать assistant-ход (с tool_calls) обратно в messages. chat() отдаёт объект SDK —
    model_dump() даёт корректный dict; для тест-фейков (SimpleNamespace) — ручной fallback."""
    dump = getattr(msg, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001 — нестандартный объект → ручная сборка ниже
            pass
    out: dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None)}
    tcs = getattr(msg, "tool_calls", None) or []
    if tcs:
        out["tool_calls"] = [
            {
                "id": getattr(tc, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(getattr(tc, "function", None), "name", ""),
                    "arguments": getattr(getattr(tc, "function", None), "arguments", "") or "{}",
                },
            }
            for tc in tcs
        ]
    return out


async def run_analysis_agent(
    audit_facts: dict[str, Any],
    *,
    chat_id: int = 0,
    lang: str = "ru",
    drill: Any = None,
    question: str | None = None,
    max_iters: int = ANALYSIS_MAX_ITERS,
    timeout_s: float = ANALYSIS_TIMEOUT_S,
) -> str | None:
    """Публичная обёртка: открыть per-run scope наблюдаемости (#10) на время нарратива, затем
    делегировать реализации. run_scope сшивает LLM-шаги хода (router.record_event 'llm') и их
    стоимость в строку agent_runs; origin/chat_id/customer_id — из core.context/provenance (человек
    /audit ⇒ origin='human'), модель проставит note_model на первом вызове. Запись fail-open —
    наблюдаемость НЕ роняет нарратив (сбой БД → warning, разбор возвращается как есть)."""
    from core import observe

    async with observe.run_scope("analysis"):
        return await _run_analysis_agent_impl(
            audit_facts,
            chat_id=chat_id,
            lang=lang,
            drill=drill,
            question=question,
            max_iters=max_iters,
            timeout_s=timeout_s,
        )


async def _run_analysis_agent_impl(
    audit_facts: dict[str, Any],
    *,
    chat_id: int = 0,
    lang: str = "ru",
    drill: Any = None,
    question: str | None = None,
    max_iters: int = ANALYSIS_MAX_ITERS,
    timeout_s: float = ANALYSIS_TIMEOUT_S,
) -> str | None:
    """Агентный нарратив поверх УЖЕ посчитанного аудита. READ-ONLY: модель рассуждает, при желании
    вызывает drill-инструменты (async callback `drill(name, args)->dict`, задаёт bot-слой — он же
    держит замок аккаунта). Возвращает человеческий разбор (fact-guarded) ИЛИ None → bot показывает
    детерминированную карточку (сбой/timeout/бюджет-стоп/выдуманное число — все ведут к None).

    audit_facts — компактный dict из AuditResult (числа = КОД). drill=None ⇒ нарратив только по seed
    (без живых уточнений). max_iters включает финальный текстовый ход (последний — БЕЗ инструментов,
    чтобы гарантированно получить текст, а не зациклиться на tool-call'ах).

    question — режим Q&A (#6): вместо обзорного пересказа модель отвечает на КОНКРЕТНЫЙ вопрос клиента
    по уже показанному аудиту (тот же fact-guard, read-only; просьбу «изменить» она переводит в
    подсказку команды). None ⇒ обычный обзорный нарратив."""
    from llm.schemas import ANALYSIS_TOOL_NAMES, ANALYSIS_TOOLS
    from audit.factguard import collect_numbers, narrative_facts_preserved
    from core import llm_budget

    lang = "en" if lang == "en" else "ru"
    has_tools = drill is not None
    code_numbers = collect_numbers(
        audit_facts
    )  # множество дозволенных чисел (пополняется drill'ами)
    qa = bool(question and question.strip())
    if qa:
        system = _ANALYST_QA_SYSTEM[lang]
        seed = (
            _ANALYST_QA_SEED[lang]
            + json.dumps(audit_facts, ensure_ascii=False)
            + _ANALYST_QA_Q[lang]
            + question.strip()
        )
    else:
        system = _ANALYST_SYSTEM[lang]
        seed = _ANALYST_SEED[lang] + json.dumps(audit_facts, ensure_ascii=False)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": seed},
    ]

    started = time.monotonic()
    narrative = ""
    iters = max(1, int(max_iters))
    for it in range(iters):
        # На последнем ходу инструменты ОТКЛЮЧАЕМ — вынуждаем текстовый ответ (иначе зацикливание).
        turn_tools = ANALYSIS_TOOLS if (has_tools and it < iters - 1) else None
        try:  # пер-итерация: дневной LLM-потолок (fail-closed → нарратив пропускаем, карточка остаётся)
            llm_budget.consume(chat_id)
        except llm_budget.LLMBudgetError:
            log.warning("analysis: бюджет LLM исчерпан — нарратив пропущен, карточка не страдает")
            return None
        try:
            msg = await chat(messages, role="analyst", tools=turn_tools)
        except llm_budget.LLMBudgetError:
            # Долларовый потолок BZ-4 прилетает ИЗ chat. Здесь глушим ОСОЗНАННО (в отличие от досье,
            # где молчание портило бы сохранённые данные): карточка аудита детерминированна и уже
            # собрана — теряется только текстовое пояснение. Но след в логе обязателен.
            log.warning("analysis: бюджет LLM исчерпан на вызове — нарратив пропущен")
            return None
        except Exception:  # noqa: BLE001 — сеть/таймаут OpenRouter → детерминированный fallback
            return None
        tcs = getattr(msg, "tool_calls", None) or []
        if not tcs or turn_tools is None:
            narrative = (getattr(msg, "content", "") or "").strip()
            break
        messages.append(_assistant_message_dict(msg))
        for tc in tcs:
            name = getattr(getattr(tc, "function", None), "name", "") or ""
            try:
                args = json.loads(getattr(getattr(tc, "function", None), "arguments", "") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if (
                name not in ANALYSIS_TOOL_NAMES
            ):  # allow-list (defense-in-depth: tools и так ограничены)
                out: dict[str, Any] = {"error": "unknown tool"}
            else:
                try:
                    out = await drill(name, args)
                except Exception as e:  # noqa: BLE001 — сбой одного чтения не роняет нарратив
                    out = {"error": redact_text(str(e))}
            code_numbers |= collect_numbers(
                out
            )  # числа из живого чтения тоже дозволены в нарративе
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", "") or "",
                    "content": json.dumps(out, ensure_ascii=False),
                }
            )
        if time.monotonic() - started > timeout_s:  # общий бюджет времени исчерпан → fallback
            return None

    # S3: любое ЗНАЧИМОЕ число нарратива обязано быть КОД-числом (факты ∪ drill), иначе отвергаем.
    if not narrative or not narrative_facts_preserved(narrative, code_numbers):
        return None
    return narrative

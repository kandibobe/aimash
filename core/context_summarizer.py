"""Контекст-саммаризатор для длинных Telegram-тредов.

Проблема: в топиках (#google-ads, #general) за неделю набираются сотни сообщений.
Если весь контекст попадает в промпт агента — модель «забывает» изначальную задачу
и сжигает лимит токенов.

Решение (фаза 1 — без внешнего LLM):
  1. Скользящее окно: последние N сообщений всегда в контексте
  2. Всё, что старше — сжимается в краткое саммари (эвристически)
  3. Саммари сохраняется в memory с тегом [summary:YYYY-MM-DD]
  4. При запросе агент видит: N сообщений + 3 последних саммари + memory-правила

Фаза 2 (ждёт интеграции с LLM): фоновый cron → LLM-саммаризатор.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

# Сколько последних сообщений держать в «живом» контексте
LIVE_MESSAGE_WINDOW = 50
# Сколько саммари подклеивать в system prompt
SUMMARY_COUNT = 3
# Максимальный возраст саммари (дни) — старше удаляем
MAX_SUMMARY_AGE_DAYS = 30
# Максимальная длина саммари в символах
MAX_SUMMARY_LEN = 500


@dataclass
class ThreadMessage:
    """Одно сообщение треда для саммаризации."""

    timestamp: datetime
    sender: str  # "user" | "assistant" | "system"
    text: str
    message_id: int


@dataclass
class ThreadSummary:
    """Саммари одного временного окна."""

    date: str  # "YYYY-MM-DD"
    topic: str  # название топика
    message_count: int
    text: str  # сжатый текст (≤MAX_SUMMARY_LEN)
    key_decisions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def extract_window(
    messages: list[ThreadMessage], window_size: int = LIVE_MESSAGE_WINDOW
) -> tuple[list[ThreadMessage], list[ThreadMessage]]:
    """Разделить сообщения на «живое окно» и «архив».

    Возвращает (live, archive). Live — последние window_size сообщений.
    """
    if len(messages) <= window_size:
        return messages, []
    return messages[-window_size:], messages[:-window_size]


def summarize_archive(
    messages: list[ThreadMessage], *, topic: str = "general"
) -> ThreadSummary:
    """Эвристическое саммари архивных сообщений (без LLM, фаза 1).

    Стратегия:
      - Группировка по типу отправителя: user-запросы vs assistant-ответы
      - Выделение ключевых действий: «изменён бюджет», «запущена кампания»
      - Ошибки собираются отдельно
      - Итоговый текст ≤ MAX_SUMMARY_LEN символов
    """
    if not messages:
        return ThreadSummary(
            date=datetime.now().strftime("%Y-%m-%d"), topic=topic, message_count=0, text=""
        )

    user_queries: list[str] = []
    bot_actions: list[str] = []
    error_reports: list[str] = []

    for m in messages:
        if m.sender == "user":
            # Берём первые 100 символов запроса
            short = m.text[:100].replace("\n", " ")
            user_queries.append(short)
        elif m.sender == "assistant":
            # Ищем действия бота
            action = _extract_action(m.text)
            if action:
                bot_actions.append(action)
            # Ищем ошибки
            if _has_error(m.text):
                error_reports.append(_extract_error_snippet(m.text))

    # Строим текст
    parts: list[str] = []

    if user_queries:
        unique_queries = list({q for q in user_queries})[:5]
        parts.append("Запросы: " + "; ".join(unique_queries))

    if bot_actions:
        unique_actions = list({a for a in bot_actions})[:5]
        parts.append("Действия: " + "; ".join(unique_actions))

    if error_reports:
        parts.append("Ошибки: " + "; ".join(error_reports[:3]))

    text = " | ".join(parts)

    # Обрезаем до MAX_SUMMARY_LEN
    if len(text) > MAX_SUMMARY_LEN:
        text = text[:MAX_SUMMARY_LEN] + "…"

    return ThreadSummary(
        date=messages[0].timestamp.strftime("%Y-%m-%d"),
        topic=topic,
        message_count=len(messages),
        text=text,
        key_decisions=bot_actions[:5],
        errors=error_reports[:3],
    )


def format_summary_for_prompt(
    summaries: list[ThreadSummary], *, max_summaries: int = SUMMARY_COUNT
) -> str:
    """Форматировать список саммари для вставки в system prompt.

    Возвращает строку вида:

    [История треда]
    2026-07-24 (general, 42 msg): запросы X, Y; действия A, B
    2026-07-23 (google-ads, 67 msg): запросы Z; действия C; ошибки E1
    2026-07-22 (general, 31 msg): запросы W; действия D
    """
    if not summaries:
        return ""

    recent = summaries[-max_summaries:]
    lines = ["[История треда]"]
    for s in recent:
        lines.append(f"{s.date} ({s.topic}, {s.message_count} msg): {s.text}")
    return "\n".join(lines)


def memory_tag_for_summary(date_str: str, topic: str) -> str:
    """Тег memory для сохранения саммари: [summary:2026-07-24:general]."""
    return f"[summary:{date_str}:{topic}]"


def parse_summary_tag(tag: str) -> tuple[str, str] | None:
    """Разобрать тег memory в (date_str, topic) или None."""
    prefix = "[summary:"
    if not tag.startswith(prefix) or not tag.endswith("]"):
        return None
    inner = tag[len(prefix) : -1]
    parts = inner.split(":", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def prune_old_summaries(tags: list[str]) -> list[str]:
    """Удалить теги саммари старше MAX_SUMMARY_AGE_DAYS дней."""
    cutoff = datetime.now() - timedelta(days=MAX_SUMMARY_AGE_DAYS)
    kept: list[str] = []
    for tag in tags:
        parsed = parse_summary_tag(tag)
        if parsed is None:
            kept.append(tag)
            continue
        date_str, _ = parsed
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if dt >= cutoff:
                kept.append(tag)
        except ValueError:
            kept.append(tag)  # не можем разобрать дату — оставляем
    return kept


def _extract_action(text: str) -> str | None:
    """Извлечь ключевое действие из ответа бота."""
    triggers = [
        "бюджет изменён", "budget changed",
        "кампания запущена", "campaign launched",
        "кампания на паузе", "campaign paused",
        "ключи добавлены", "keywords added",
        "минус-слова", "negatives added",
        "ставка изменена", "bid adjusted",
        "аудит завершён", "audit completed",
        "отчёт сформирован", "report generated",
    ]
    text_lower = text.lower()
    for t in triggers:
        if t in text_lower:
            return t
    # Fallback: первое предложение
    first_line = text.strip().split("\n")[0]
    if len(first_line) <= 100:
        return first_line[:80]
    return None


def _has_error(text: str) -> bool:
    """Проверить, содержит ли сообщение признаки ошибки."""
    error_signals = ["ошибка", "error", "failed", "не удалось", "отказ", "denied", "🚨"]
    text_lower = text.lower()
    return any(s in text_lower for s in error_signals)


def _extract_error_snippet(text: str) -> str:
    """Извлечь короткое описание ошибки."""
    # Ищем строки с ошибками
    for line in text.split("\n"):
        if _has_error(line):
            return line.strip()[:120]
    return text.strip()[:80] + "…"
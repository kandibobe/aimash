"""Тесты контекст-саммаризатора (core.context_summarizer)."""

from datetime import datetime, timedelta
from core.context_summarizer import (
    ThreadMessage,
    ThreadSummary,
    extract_window,
    summarize_archive,
    format_summary_for_prompt,
    memory_tag_for_summary,
    parse_summary_tag,
    prune_old_summaries,
)


def make_msg(timestamp: datetime, sender: str, text: str, msg_id: int) -> ThreadMessage:
    return ThreadMessage(timestamp=timestamp, sender=sender, text=text, message_id=msg_id)


class TestExtractWindow:
    def test_below_threshold(self):
        msgs = [make_msg(datetime.now(), "user", f"msg {i}", i) for i in range(5)]
        live, archive = extract_window(msgs, window_size=50)
        assert len(live) == 5
        assert len(archive) == 0

    def test_above_threshold(self):
        msgs = [make_msg(datetime.now(), "user", f"msg {i}", i) for i in range(100)]
        live, archive = extract_window(msgs, window_size=50)
        assert len(live) == 50
        assert len(archive) == 50
        # live содержит последние 50
        assert live[-1].message_id == 99
        assert archive[-1].message_id == 49


class TestSummarizeArchive:
    def test_empty(self):
        result = summarize_archive([])
        assert result.message_count == 0
        assert result.text == ""

    def test_user_queries_collected(self):
        now = datetime.now()
        msgs = [
            make_msg(now, "user", "Проверь кампанию 123", 1),
            make_msg(now, "assistant", "📊 Анализ: CPA $14.20. Рекомендую снизить бюджет.", 2),
        ]
        result = summarize_archive(msgs, topic="google-ads")
        assert "Проверь кампанию 123" in result.text
        assert result.topic == "google-ads"
        assert result.message_count == 2

    def test_actions_detected(self):
        now = datetime.now()
        msgs = [
            make_msg(now, "user", "Подними бюджет", 1),
            make_msg(now, "assistant", "бюджет изменён: 400→500", 2),
            make_msg(now, "assistant", "ключи добавлены: 3 новых", 3),
        ]
        result = summarize_archive(msgs)
        assert len(result.key_decisions) >= 1

    def test_errors_detected(self):
        now = datetime.now()
        msgs = [
            make_msg(now, "assistant", "🚨 ошибка API", 1),
            make_msg(now, "assistant", "не удалось обновить бюджет", 2),
        ]
        result = summarize_archive(msgs)
        assert len(result.errors) >= 1


class TestFormatSummaryForPrompt:
    def test_empty(self):
        assert format_summary_for_prompt([]) == ""

    def test_single_summary(self):
        s = ThreadSummary(
            date="2026-07-24", topic="general", message_count=42, text="запросы; действия"
        )
        result = format_summary_for_prompt([s])
        assert "[История треда]" in result
        assert "2026-07-24" in result
        assert "general" in result
        assert "42 msg" in result

    def test_max_summaries(self):
        summaries = [
            ThreadSummary(date=f"2026-07-{20+i:02d}", topic="t", message_count=10, text=f"day{i}")
            for i in range(10)
        ]
        result = format_summary_for_prompt(summaries, max_summaries=3)
        # Должны быть только последние 3
        assert "day7" in result
        assert "day8" in result
        assert "day9" in result
        assert "day0" not in result


class TestMemoryTags:
    def test_roundtrip(self):
        tag = memory_tag_for_summary("2026-07-24", "general")
        parsed = parse_summary_tag(tag)
        assert parsed == ("2026-07-24", "general")

    def test_invalid_tag(self):
        assert parse_summary_tag("not a tag") is None
        assert parse_summary_tag("[summary:2026-07-24]") is None


class TestPruneOldSummaries:
    def test_keeps_recent(self):
        today = datetime.now().strftime("%Y-%m-%d")
        tags = [f"[summary:{today}:general]"]
        result = prune_old_summaries(tags)
        assert tags == result

    def test_removes_old(self):
        old_date = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        tags = [f"[summary:{old_date}:general]", "not-a-summary-tag"]
        result = prune_old_summaries(tags)
        assert len(result) == 1
        assert result[0] == "not-a-summary-tag"
"""Тесты компактора API-ответов (mcp_server.compact)."""

import pytest
from mcp_server.compact import (
    compact_envelope,
    should_compact,
    _strip_nulls,
    _trunc_strings,
    _estimate_bytes,
)


class TestStripNulls:
    def test_flat_dict(self):
        assert _strip_nulls({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}

    def test_nested_dict(self):
        data = {"a": {"b": None, "c": 2}, "d": None}
        assert _strip_nulls(data) == {"a": {"c": 2}}

    def test_list_inside(self):
        data = {"rows": [{"x": None, "y": 1}, {"x": 2, "y": None}]}
        expected = {"rows": [{"y": 1}, {"x": 2}]}
        assert _strip_nulls(data) == expected

    def test_empty_after_strip(self):
        assert _strip_nulls({"a": None, "b": None}) == {}


class TestTruncStrings:
    def test_short_string_unchanged(self):
        assert _trunc_strings("hello", max_len=10) == "hello"

    def test_long_string_truncated(self):
        result = _trunc_strings("a" * 300, max_len=200)
        assert len(result) == 200 + len("…[trunc]")
        assert result.endswith("…[trunc]")

    def test_nested_truncation(self):
        data = {"name": "short", "desc": "x" * 250}
        result = _trunc_strings(data, max_len=200)
        assert result["name"] == "short"
        assert len(result["desc"]) == 200 + len("…[trunc]")


class TestCompactEnvelope:
    def test_removes_null_fields(self):
        data = {
            "rows": [{"id": 1, "name": None, "cost": 10.0}],
            "total_rows": 1,
            "error": None,
            "error_code": None,
        }
        result = compact_envelope(data)
        assert "error" not in result
        assert "error_code" not in result
        # nulls в rows сохраняются (семантически значимы: метрика отсутствует)
        assert result["rows"][0] == {"id": 1, "name": None, "cost": 10.0}

    def test_truncates_strings(self):
        data = {
            "rows": [{"id": 1, "text": "x" * 500}],
            "total_rows": 1,
            "error": None,
            "error_code": None,
        }
        result = compact_envelope(data)
        row_text = result["rows"][0]["text"]
        assert len(row_text) <= 220  # 200 + suffix
        assert row_text.endswith("…[trunc]")

    def test_limits_rows(self):
        data = {
            "rows": [{"id": i} for i in range(150)],
            "total_rows": 150,
            "returned": 150,
            "error": None,
            "error_code": None,
        }
        result = compact_envelope(data, max_rows=100)
        assert len(result["rows"]) == 100
        assert result["truncated"] is True
        assert "note" in result

    def test_none_preserved_in_rows(self):
        """None ВНУТРИ rows остаётся — это семантически значимо (метрика отсутствует)."""
        data = {
            "rows": [{"id": 1, "cpa": None}],
            "total_rows": 1,
            "error": None,
            "error_code": None,
        }
        result = compact_envelope(data)
        assert result["rows"][0]["cpa"] is None  # внутри rows не трогаем null

    def test_does_not_mutate_original(self):
        original = {
            "rows": [{"id": 1, "name": None}],
            "total_rows": 1,
            "error": None,
            "error_code": None,
        }
        _ = compact_envelope(original)
        assert "error" in original  # оригинал не тронут


class TestShouldCompact:
    def test_large_response(self):
        data = {"rows": [{"id": i, "data": "x" * 200} for i in range(300)]}
        assert should_compact(data) is True

    def test_small_response(self):
        data = {"rows": [{"id": 1}], "total_rows": 1}
        assert should_compact(data) is False


class TestEstimateBytes:
    def test_small_dict(self):
        assert _estimate_bytes({"a": 1}) > 0

    def test_large_list(self):
        size = _estimate_bytes({"rows": [{"id": i} for i in range(1000)]})
        assert size > 1000
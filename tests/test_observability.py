"""Sentry-мониторинг (core.observability): редакция секретов в событии + безопасный no-op.

Главное — проверить, что before_send РЕДАКТИРУЕТ секрето-подобные строки во всех частях события
(сообщение, traceback, breadcrumbs), т.к. RedactionFilter висит на лог-хендлере и до Sentry не
доходит (golden rule #5). Без сети и без реального DSN.
"""

from __future__ import annotations

from core import observability as obs


def test_before_send_redacts_nested_secrets():
    event = {
        "logentry": {"message": "oauth refresh 1//ABCDEFGHIJKLMNOPQRSTU"},
        "exception": {"values": [{"value": "boom api_key=supersecretvalue123 trailing"}]},
        "breadcrumbs": [{"message": "Authorization: Bearer abc.def.ghijklmno"}],
    }
    out = obs._before_send(event, {})
    flat = str(out)
    assert "1//ABCDEFGHIJKLMNOPQRSTU" not in flat
    assert "supersecretvalue123" not in flat
    assert "abc.def.ghijklmno" not in flat
    assert "REDACTED" in out["exception"]["values"][0]["value"]


def test_before_send_never_raises_on_weird_values():
    # Нестроковые значения проходят насквозь; редакция не должна ломать отправку события.
    out = obs._before_send({"obj": object(), "n": 5, "nested": ["a token=zzz123456"]}, {})
    assert out is not None
    assert "zzz123456" not in str(out["nested"])


def test_init_observability_noop_without_dsn():
    # В dev/тестах SENTRY_DSN пуст → init тихий no-op (без исключений, без импорта sentry).
    obs.init_observability()

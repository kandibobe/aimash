"""P1-аудит 2026-07-06: менеджер не должен видеть имена классов исключений («Ошибка: ValidationError»).

bot.main._friendly_error — единая точка: Pydantic-валидация → err_validate (локализованные правила),
ValueError валидаторов → err_validate (их сообщение уже человекочитаемо), прочее → err_unexpected с
кодом инцидента (request_id) + фиксация в error_events. short=True — однострочный вариант ≤180 симв.
для cq.answer(show_alert=True). Плюс гард класса: в bot/ не осталось ни одного user-facing текста с
type(e).__name__.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
from pydantic import BaseModel, Field, ValidationError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402

BOT_DIR = pathlib.Path(__file__).resolve().parents[1] / "bot"


class _Schema(BaseModel):
    radius_km: float = Field(gt=0, le=2000)
    name: str = Field(max_length=5)


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as ei:
        _Schema(radius_km=5000, name="слишком длинное имя")
    return ei.value


async def test_pydantic_error_has_rules_not_class_name():
    text = await bm._friendly_error(_validation_error(), "test:pydantic")
    assert "ValidationError" not in text
    assert "radius_km" in text  # поле названо
    assert "2000" in text  # лимит из правила виден


async def test_value_error_keeps_human_message():
    text = await bm._friendly_error(ValueError("радиус — в (0, 2000]"), "test:value")
    assert "ValueError" not in text
    assert "радиус" in text


async def test_unexpected_error_gets_incident_code(monkeypatch):
    import core.errors as ce

    captured: dict = {}

    async def _fake_capture(exc, *, where):
        captured["where"] = where
        return "abc123def"

    # _friendly_error импортирует capture_exception из core.errors при вызове? Нет — bot.main
    # импортировал его на модуль. Патчим оба имени (module-level import в bot.main).
    monkeypatch.setattr(ce, "capture_exception", _fake_capture)
    monkeypatch.setattr(bm, "capture_exception", _fake_capture, raising=False)
    text = await bm._friendly_error(RuntimeError("secret internals"), "test:boom")
    assert "RuntimeError" not in text
    assert "secret internals" not in text  # сырой текст не утекает
    assert "abc123def" in text  # код инцидента для поиска в /diag
    assert captured["where"] == "test:boom"


async def test_short_variant_is_single_line_and_capped():
    e = _validation_error()
    text = await bm._friendly_error(e, "test:short", short=True)
    assert "\n" not in text
    assert len(text) <= 180


def test_no_exception_class_names_in_user_texts():
    """Гард класса: в bot/ нет user-facing i18n-вызова с type(e).__name__ (cb_error удалён)."""
    class_leak = re.compile(r"i18n\.t\([^)]*type\(e\)\.__name__")
    cb_call = re.compile(r"\bt\(\s*[\"']cb_error[\"']")
    offenders = []
    for p in BOT_DIR.rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        if cb_call.search(src):
            offenders.append(f"{p.name}: вызов t('cb_error')")
        if class_leak.search(src):
            offenders.append(f"{p.name}: type(e).__name__ в i18n.t")
    assert not offenders, offenders
    from bot.i18n import CATALOG

    assert "cb_error" not in CATALOG  # ключ удалён — «Ошибка: {kind}» больше не существует

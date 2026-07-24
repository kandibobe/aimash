"""Инвариант источника истины: `ТЗ.md` не отстаёт от `.docx` заказчика.

Класс бага. Оригиналы ТЗ — три `.docx` в корне репозитория, то есть бинарники: расхождение с
рабочим текстом никак не проявляется — ни в диффе, ни в ревью. Заказчик присылает новую редакцию,
её кладут поверх старой, и весь проект продолжает опираться на текст, которого в ТЗ уже нет.

Сверяем sha256 источников, а не содержимое `ТЗ.md`: вычитка markdown-разметки (таблицы и
заголовки после Word) законна и хэши источников не трогает, а подмена `.docx` — трогает.

Чинится одной командой: `python scripts/docx_to_tz.py`.
"""

from __future__ import annotations

import hashlib
import importlib.util

import pytest

from tests._docs_paths import ROOT

_SPEC = importlib.util.spec_from_file_location("docx_to_tz", ROOT / "scripts" / "docx_to_tz.py")


def _load_script():
    if _SPEC is None or _SPEC.loader is None:  # pragma: no cover — файл на месте
        pytest.skip("scripts/docx_to_tz.py недоступен")
    module = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(module)
    return module


docx = pytest.importorskip("docx", reason="python-docx нужен только для сверки с .docx")
_MOD = _load_script()


def _require_sources():
    """Документы заказчика не коммитятся (`.gitignore:64-65`) — в CI сверять нечего."""
    missing = [name for name, _, _ in _MOD.SOURCES if not (ROOT / name).exists()]
    if len(missing) == len(_MOD.SOURCES):
        pytest.skip("документы заказчика (*.docx) локальные — в этом окружении их нет")
    return missing


def test_no_source_docx_disappeared():
    """Либо все три оригинала на месте, либо ни одного. Пропажа ОДНОГО — потеря куска ТЗ."""
    missing = _require_sources()
    assert not missing, f"часть оригиналов пропала: {missing} — ТЗ.md собран не из всего"


def test_tz_md_matches_sources():
    """sha256 каждого `.docx` записан в шапке `ТЗ.md` — иначе текст отстал от оригинала."""
    _require_sources()
    tz = ROOT / "ТЗ.md"
    assert tz.exists(), "ТЗ.md отсутствует — запусти `python scripts/docx_to_tz.py`"
    text = tz.read_text(encoding="utf-8")
    stale = [
        tag
        for name, tag, _ in _MOD.SOURCES
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() not in text
    ]
    assert not stale, (
        f"ТЗ.md отстал от оригиналов {stale} — перегенерируй: `python scripts/docx_to_tz.py`"
    )


def test_all_three_sources_are_included():
    """Все три документа заказчика попадают в сводный текст — ТЗ-2 и ТЗ-3 теряются легче всего."""
    _require_sources()
    text = (ROOT / "ТЗ.md").read_text(encoding="utf-8")
    for _name, tag, _ in _MOD.SOURCES:
        assert f"# {tag} —" in text, f"{tag} отсутствует в ТЗ.md"

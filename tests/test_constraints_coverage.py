"""A11: каждая зависимость из pyproject запиннена в constraints.txt (воспроизводимый деплой).

Раньше рантайм-зависимости — открытые диапазоны (>=), lock-файла не было: каждый деплой ресолвил
новейшие версии заново (прод менялся без изменения кода; офлайн-CI несовместимость SDK не ловил).
constraints.txt (uv pip compile) пиннит точные версии; Dockerfile/CI ставят с -c. Гард: добавление
зависимости без перегенерации constraints.txt валит CI (напоминание перегенерировать lock).
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def _norm(name: str) -> str:
    """PEP 503 нормализация имени пакета: lower + не-alnum → '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _project_deps() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out = []
    deps = data["project"]["dependencies"] + data["project"]["optional-dependencies"]["dev"]
    for spec in deps:
        # имя до первого спецификатора версии/маркера/экстры
        name = re.split(r"[<>=!~;\[ ]", spec.strip(), maxsplit=1)[0]
        if name:
            out.append(_norm(name))
    return out


def _pinned_names() -> set[str]:
    pins = set()
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "==" in s:
            pins.add(_norm(s.split("==", 1)[0]))
    return pins


def test_constraints_file_exists_and_pins():
    assert (ROOT / "constraints.txt").exists(), "нет constraints.txt (uv pip compile)"
    assert len(_pinned_names()) >= 20  # реальный lock, не заглушка


def test_every_declared_dependency_is_pinned():
    """Каждая зависимость pyproject (runtime + dev) присутствует в constraints.txt с точным ==."""
    pinned = _pinned_names()
    missing = [d for d in _project_deps() if d not in pinned]
    assert not missing, (
        f"зависимости без пина в constraints.txt: {missing}. "
        "Перегенерируй: uv pip compile pyproject.toml --extra dev --python-version 3.12 "
        "-o constraints.txt"
    )

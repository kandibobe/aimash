"""B12: текст исключения наружу (в чат) — только через ux.err_text (редактирует секрето-подобное),
а не сырым str(e). _advise_run был последним швом класса. Греп-гард ловит регресс.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]


def test_no_bare_str_exception_to_user_in_main():
    """В bot/main.py нет `detail=str(e)`/`err=str(e)` (сырой текст исключения в i18n.t → чат):
    такой текст обязан идти через ux.err_text/redact_text/humanize (редакция секретов, gr #5)."""
    src = (ROOT / "bot" / "main.py").read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        # анти-паттерн: сырой str(e) в user-facing тексте (i18n.t → чат) БЕЗ обёртки редакции.
        # DB-записи (mark_failed(error=str(e))) редактируют внутри себя — их не считаем.
        if "i18n.t(" in s and re.search(r"=\s*str\(e\)", s):
            if not any(w in s for w in ("err_text", "redact_text", "humanize")):
                offenders.append(f"bot/main.py:{i}: {s}")
    assert not offenders, "сырой текст исключения в чат мимо err_text/redact_text:\n" + "\n".join(
        offenders
    )

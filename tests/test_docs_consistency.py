"""Дрейф доков ↔ репо: docs/DATABASE.md обязан упоминать текущий head миграций.

Класс бага (аудит 2026-07-08): цепочка ревизий в доке отстала (док говорил `0017`, факт —
`0021`, +0018_recommendations…0021_admins_runtime) → следуя доку, берёшь неверный
`down_revision` для новой миграции. Тест закрывает класс: голова цепочки в migrations/versions/
должна присутствовать в доке. Офлайн, без БД — читает только файлы.

Второй заход того же класса (2026-07-27): врал не номер ревизии, а СОСТАВ таблиц. Заголовок
раздела «Все таблицы» говорил «(15)», строк было 19, а в `db/models.py` — 27: восемь таблиц
отсутствовали в доке годами. Читающий делал вывод «такой таблицы нет» и заводил дубль.
`test_database_doc_lists_every_table` сверяет перечень с `db.models.Base.metadata` В ОБЕ
СТОРОНЫ — тоже офлайн: метадата собирается импортом моделей, живая БД не нужна.
"""

from __future__ import annotations

import re
from pathlib import Path

from db.models import Base

_ROOT = Path(__file__).resolve().parents[1]
_DATABASE_DOC = _ROOT / "docs" / "DATABASE.md"


def _migration_head_num() -> int:
    versions = list((_ROOT / "migrations" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
    assert versions, "не найдены файлы миграций migrations/versions/NNNN_*.py"
    return max(int(re.match(r"(\d+)", p.name).group(1)) for p in versions)


def test_database_doc_mentions_current_migration_head():
    head = f"{_migration_head_num():04d}"
    doc = (_ROOT / "docs" / "DATABASE.md").read_text(encoding="utf-8")
    assert f"`{head}`" in doc, (
        f"docs/DATABASE.md не упоминает текущий head миграций `{head}` "
        "(последняя ревизия в migrations/versions/). Обнови цепочку ревизий и метку **head** "
        "в разделе «Миграции (Alembic)»."
    )


# «## Все таблицы (27)» — заголовок перечня; число в скобках необязательно, но если написано,
# оно тоже обязано быть правдой (см. ниже).
_TABLES_HEADING = re.compile(r"^##[ \t]+Все таблицы(?:[ \t]*\((\d+)\))?[ \t]*$", re.M)
# первое имя в бэктиках из первой колонки строки markdown-таблицы
_ROW_NAME = re.compile(r"`([^`]+)`")


def _tables_section() -> tuple[str, int | None]:
    """Текст раздела «Все таблицы» + число из его заголовка (None — числа нет).

    Скоуп по разделу обязателен: в docs/DATABASE.md есть и другие markdown-таблицы, «все строки,
    начинающиеся с `|`» собрали бы их тоже. Раздел = от заголовка до следующего `#`/`##`.
    """
    doc = _DATABASE_DOC.read_text(encoding="utf-8")
    m = _TABLES_HEADING.search(doc)
    assert m, (
        "в docs/DATABASE.md не найден заголовок «## Все таблицы» — раздел с перечнем таблиц "
        "переименован или удалён. Верни заголовок (перечень сверяется с db.models.Base.metadata) "
        "либо поправь _TABLES_HEADING в этом тесте."
    )
    rest = doc[m.end() :]
    nxt = re.search(r"^#{1,2}[ \t]", rest, re.M)
    return rest[: nxt.start()] if nxt else rest, int(m.group(1)) if m.group(1) else None


def _doc_table_names(section: str) -> list[str]:
    """Имена таблиц из первой колонки строк раздела, в порядке дока.

    Строки без имени в бэктиках (шапка `| Таблица |`, разделитель `|---|`) пропускаются молча:
    строку с настоящей таблицей, но без бэктиков, поймает сверка множеств ниже.
    """
    names = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1]
        m = _ROW_NAME.search(first_cell)
        if m:
            names.append(m.group(1))
    return names


def test_database_doc_lists_every_table():
    """Раздел «Все таблицы» ≡ db.models.Base.metadata.tables (в обе стороны) + число в заголовке.

    Без БД: metadata собирается импортом моделей. Односторонняя сверка не закрывает класс —
    «нет в доке» и «нет в моделях» одинаково врут читателю, поэтому проверяются оба направления.
    """
    section, declared = _tables_section()
    listed = _doc_table_names(section)

    dupes = sorted({n for n in listed if listed.count(n) > 1})
    assert not dupes, (
        f"в разделе «Все таблицы» docs/DATABASE.md таблица описана дважды: {', '.join(dupes)}. "
        "Оставь одну строку на таблицу — иначе число в заголовке и сверка с моделями расходятся "
        "молча."
    )

    actual = set(Base.metadata.tables)
    # Сверка множеств молча зелена, если ОБА пусты, а число в заголовке необязательно — значит без
    # этой строки «модели не зарегистрировались» даёт зелёный тест вместо отказа (проверено
    # отрицательным контролем 2026-07-27). Реалистичный путь к пустой/усечённой метадате —
    # разъезд моделей по модулям при пивоте: `db.models` перестаёт регистрировать часть таблиц,
    # и требование «таблица обязана быть в доке» тихо перестаёт на них распространяться.
    assert actual, (
        "db.models.Base.metadata.tables пуста — сверять перечень docs/DATABASE.md не с чем. "
        "Модели не зарегистрировались (разъехались по модулям? переименован Base?), а не «таблиц "
        "нет»: молча зелёный тест здесь хуже отсутствующего."
    )
    missing = sorted(actual - set(listed))
    extra = sorted(set(listed) - actual)
    assert not missing and not extra, (
        "раздел «Все таблицы» docs/DATABASE.md разошёлся с db/models.py:\n"
        + (f"  нет в доке (есть в моделях): {', '.join(missing)}\n" if missing else "")
        + (f"  нет в моделях (есть в доке): {', '.join(extra)}\n" if extra else "")
        + "Добавь строку «| `имя` | назначение | ключевые поля | миграция | статус |» на каждую "
        "недостающую таблицу, убери строки удалённых и обнови число в заголовке "
        "«## Все таблицы (N)»."
    )

    if declared is not None:
        assert declared == len(listed), (
            f"заголовок «## Все таблицы ({declared})» в docs/DATABASE.md не совпадает с числом "
            f"строк перечня ({len(listed)}). Раз число написано — оно обязано быть правдой: "
            f"поправь заголовок на ({len(listed)})."
        )

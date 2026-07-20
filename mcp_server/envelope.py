"""Единый конверт ответа MCP-инструментов + `code_numbers` (§8.1 «числа только из кода»).

Ни один существующий ридер не отдаёт `offset`/`total_rows` единообразно (усечение сигналят лишь
некоторые). MCP-слой нормализует это поверх результата ридера — форма конверта одна на все READ:

    {rows, total_rows, returned, offset, truncated, code_numbers, error}

`code_numbers` наполняется `audit.factguard.collect_numbers` по ВСЕЙ отдаваемой полезной нагрузке
(rows + extra: score/итоги) — это множество чисел, которые агент вправе процитировать в нарративе;
финальный текст потребитель сверяет через `factguard.narrative_facts_preserved`. Отдаём отсортированным
list[float] (JSON-сериализуемо; set — нет).

`error` — редактированный текст при сбое (`mcp_server.redact.redact_error`), НИКОГДА сырой `str(e)`.
На успехе `error=None`; при ошибке rows пуст (fail-closed: инструмент не отдаёт частичных данных).
"""

from __future__ import annotations

from typing import Any

# Дефолтный размер страницы конверта. Отдельный от GAQL-LIMIT ридеров: конверт не убирает их лимиты,
# а нормализует сигнал усечения/пагинации поверх уже полученного результата.
DEFAULT_LIMIT = 50


def paginate(rows: list, *, offset: int = 0, limit: int = DEFAULT_LIMIT) -> tuple[list, int, bool]:
    """(страница, total, truncated). offset<0 → 0; limit<1 → 1. total — по ПОЛНОМУ списку до среза."""
    total = len(rows)
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    page = rows[offset : offset + limit]
    truncated = offset + len(page) < total
    return page, total, truncated


def ok(
    rows: list,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Успешный конверт. `extra` — доп. поля верхнего уровня (score/grade/currency для аудита);
    его числа тоже попадают в `code_numbers`."""
    from audit.factguard import collect_numbers

    page, total, truncated = paginate(rows, offset=offset, limit=limit)
    payload: dict[str, Any] = {"rows": page}
    if extra:
        payload.update(extra)
    env = {
        **payload,
        "total_rows": total,
        "returned": len(page),
        "offset": max(0, int(offset)),
        "truncated": truncated,
        "code_numbers": sorted(collect_numbers(payload)),
        "error": None,
    }
    return env


def err(exc: BaseException) -> dict[str, Any]:
    """Ошибочный конверт: пустые rows + редактированный текст (правило 5). Форма совпадает с ok()
    по ключам-скелету, чтобы клиент не различал ветки структурно."""
    from mcp_server.redact import redact_error

    return {
        "rows": [],
        "total_rows": 0,
        "returned": 0,
        "offset": 0,
        "truncated": False,
        "code_numbers": [],
        "error": redact_error(exc),
    }

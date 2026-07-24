"""Единый конверт ответа MCP-инструментов + `code_numbers` (§8.1 «числа только из кода»).

Ни один существующий ридер не отдаёт `offset`/`total_rows` единообразно (усечение сигналят лишь
некоторые). MCP-слой нормализует это поверх результата ридера — форма конверта одна на все READ:

    {rows, total_rows, returned, offset, truncated, reader_capped, code_numbers, error}

Два РАЗНЫХ усечения, их нельзя путать:
  • `truncated` — пагинация КОНВЕРТА: в списке, что вернул ридер, есть строки за пределами этой
    страницы (offset/limit). Обходится следующим offset — данные не потеряны.
  • `reader_capped` — ридер упёрся в СВОЙ GAQL-LIMIT (search_terms LIMIT 200, top-N по расходу):
    истинный размер набора БОЛЬШЕ `total_rows` и следующим offset НЕ добирается. Без этого сигнала
    `get_search_terms` на аккаунте с 5000 запросов отдавал `total_rows=200, truncated=false`, и
    модель читала это как «поисковых запросов ровно 200» и строила на этом минус-слова. Когда
    `reader_capped=true`, конверт добавляет `reader_limit` (значение кэпа). Breakdown-ридеры сигналят
    то же усечение текстом в `note` (top-N по расходу) — там `reader_capped` не дублируем.

`code_numbers` наполняется `audit.factguard.collect_numbers` по ВСЕЙ отдаваемой полезной нагрузке
(rows + extra: score/итоги) — это множество чисел, которые агент вправе процитировать в нарративе;
финальный текст потребитель сверяет через `factguard.narrative_facts_preserved`. Отдаём отсортированным
list[float] (JSON-сериализуемо; set — нет).

`error` — редактированный текст при сбое (`mcp_server.redact.redact_error`), НИКОГДА сырой `str(e)`.
На успехе `error=None`; при ошибке rows пуст (fail-closed: инструмент не отдаёт частичных данных).

`error_code` — машиночитаемая ПРИЧИНА отказа. Появился затем, что редактированный `error` разбирать
нельзя (текст локализуем и намеренно обеднён), а без причины конверт байт-в-байт одинаков для любого
исключения: тест «чужой customer_id ⇒ отказ» тогда тавтологичен и остаётся зелёным при полностью
снятом замке чтения. Код различает «отказал НАШ замок» (`forbidden_account`) и «отказал Google»
(`upstream_denied`) — на живом аккаунте это два разных инцидента с разными действиями оператора.
"""

from __future__ import annotations

from typing import Any

# Стабильный контракт кодов. Расширять можно, ПЕРЕИМЕНОВЫВАТЬ — нет: на них ассертят инварианты.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "forbidden_account",  # наш замок (ensure_read_allowed/ensure_manager_allowed/ensure_allowed)
        "quota_exhausted",  # core.quota — суточный потолок операций
        "timeout",  # таймаут вызова Google Ads
        "invalid_argument",  # кривой вход инструмента (даты, типы) — вина вызывающего
        "upstream_denied",  # Google отказал в доступе к аккаунту (нет линковки/токен без прав)
        "upstream_error",  # прочая ошибка Google Ads API
        "internal",  # всё остальное, включая сбой самой классификации
        "refused",  # propose-гейт отказал ДО создания черновика (И8-лимит/замок/валюта/снижение) —
        # это НЕ сбой, а штатный отказ: причина в `error` (текст сформирован КОДОМ). Google Ads не тронут.
    }
)

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
    reader_limit: int | None = None,
) -> dict[str, Any]:
    """Успешный конверт. `extra` — доп. поля верхнего уровня (score/grade/currency для аудита);
    его числа тоже попадают в `code_numbers`.

    `reader_limit` — GAQL-LIMIT ридера, если он есть (search_terms 200, negatives 5000, budgets 500,
    change_history 20). Когда `total >= reader_limit`, ридер упёрся в свой потолок → `reader_capped=
    true` и конверт эхо-несёт `reader_limit`: истинный размер набора БОЛЬШЕ `total_rows`, следующим
    offset НЕ добирается (в отличие от `truncated`). None ⇒ у ридера нет потолка либо он выражен
    текстом в `note` (Breakdown) ⇒ `reader_capped=false`, без дублирования сигнала."""
    from audit.factguard import collect_numbers

    page, total, truncated = paginate(rows, offset=offset, limit=limit)
    reader_capped = reader_limit is not None and total >= int(reader_limit)
    payload: dict[str, Any] = {"rows": page}
    if extra:
        payload.update(extra)
    env = {
        **payload,
        "total_rows": total,
        "returned": len(page),
        "offset": max(0, int(offset)),
        "truncated": truncated,
        "reader_capped": reader_capped,
        "code_numbers": sorted(collect_numbers(payload)),
        "error": None,
        "error_code": None,
    }
    if reader_capped:
        env["reader_limit"] = int(reader_limit)  # сколько строк тянул ридер — модель видит потолок
    return env


def classify_error(exc: BaseException) -> str:
    """Исключение → код из `ERROR_CODES`. Никогда не бросает: сбой самой классификации ⇒ `internal`.

    Порядок веток значим. `PermissionError` идёт первой: в read-слое её источник ровно один — наши
    замки (`ads.client.ensure_*`), и именно эту ветку ассертят инварианты замка. Ветка Google
    (`upstream_denied`) — ПОСЛЕ неё, иначе отказ Google маскировался бы под отказ замка и тест замка
    снова стал бы тавтологичным.
    """
    try:
        from core.ads_errors import error_code_names, is_account_access_error
        from core.quota import QuotaExceededError

        if isinstance(exc, PermissionError):
            return "forbidden_account"
        if isinstance(exc, QuotaExceededError):
            return "quota_exhausted"
        if isinstance(exc, TimeoutError):  # 3.11+: asyncio.TimeoutError — это он же
            return "timeout"
        if isinstance(exc, ValueError | TypeError | KeyError):
            return "invalid_argument"
        if is_account_access_error(exc):
            return "upstream_denied"
        if error_code_names(exc):  # непусто ⇒ это GoogleAdsException с разобранными кодами
            return "upstream_error"
    except Exception:  # noqa: BLE001 — классификатор не смеет ронять инструмент (fail-closed)
        return "internal"
    return "internal"


def err(exc: BaseException) -> dict[str, Any]:
    """Ошибочный конверт: пустые rows + редактированный текст (правило 5) + машиночитаемый код.
    Форма совпадает с ok() по ключам-скелету, чтобы клиент не различал ветки структурно."""
    from mcp_server.redact import redact_error

    return {
        "rows": [],
        "total_rows": 0,
        "returned": 0,
        "offset": 0,
        "truncated": False,
        "reader_capped": False,
        "code_numbers": [],
        "error": redact_error(exc),
        "error_code": classify_error(exc),
    }


def proposed(
    *,
    confirmation_id: str,
    operation: str,
    customer_id: str,
    preview: str,
) -> dict[str, Any]:
    """Успешный конверт propose-инструмента: черновик СОЗДАН (status='pending'), Google Ads НЕ тронут.

    Форма НАМЕРЕННО отличается от `ok()` (там rows-пагинация): propose отдаёт один черновик, а не
    страницу строк. `preview` — человеко-diff «было → станет», собранный `confirm.render` (КОД, не
    модель): именно его увидит человек перед подтверждением. `confirmation_id` понадобится будущему
    execute-контуру (реплай-якорь, §6.4); сегодня это просто идентификатор созданной строки.

    Здесь НЕТ ни `code_numbers`, ни пагинации: propose ничего не читает из Google Ads наружу для
    цитирования — весь текст уже в `preview`. `error`/`error_code` держим для симметрии с READ-скелетом
    (на успехе оба None), чтобы клиент не различал READ/PROPOSE структурно."""
    return {
        "confirmation_id": confirmation_id,
        "operation": operation,
        "customer_id": str(customer_id),
        "status": "pending",
        "preview": preview,
        "error": None,
        "error_code": None,
    }


def refused(text: str, *, error_code: str = "refused") -> dict[str, Any]:
    """Отказ propose-гейта ДО создания черновика: И8-лимит, замок аккаунта, валютный mismatch,
    невыполнимое снижение, кривой вход. Черновика НЕТ (confirmation_id=None), Google Ads не тронут.

    `text` — ГОТОВОЕ сообщение человеку. Для `ProposalRefused` его сформировал КОД (редактировать
    нечего); для непредвиденного исключения вызывающий обязан передать УЖЕ редактированный текст
    (`mcp_server.redact.redact_error`) — сырой `str(e)` наружу нельзя (правило 5). `error_code` — из
    `ERROR_CODES`: `refused` для штатного отказа гейта, `invalid_argument` для кривого входа,
    классифицированный код для непредвиденного. Скелет зеркалит `proposed()`, чтобы клиент не ветвился."""
    return {
        "confirmation_id": None,
        "operation": None,
        "customer_id": None,
        "status": "refused",
        "preview": None,
        "error": text,
        "error_code": error_code,
    }

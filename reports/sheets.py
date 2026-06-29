"""Экспорт отчёта (reports.service.ReportData) в Google Sheets (ТЗ §9, §16). READ-ONLY.

Создаёт НОВУЮ таблицу (spreadsheets.create) с листом «Сводка» + листом на каждую разбивку,
заполняет значения (values.batchUpdate) и возвращает ссылку. Раскладка зеркалит reports.xlsx
(та же шапка METRIC_HEADERS, те же строки), без форматирования ячеек.

⚠️ Требует OAuth-scope drive.file (или spreadsheets), которого НЕТ у Google Ads токена (adwords).
Включается отдельным re-auth (см. docs/DEPLOYMENT.md, раздел «Google Sheets»). Без scope вызов
упадёт — бот покажет понятное сообщение. Чистая сборка (build_sheets_data) тестируется офлайн;
сеть инкапсулирована в publish_report_to_sheets (service подменяем в тестах).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from core.logging import log
from reports.queries import metric_headers
from reports.service import ReportData

# drive.file — минимально достаточный scope: доступ только к файлам, созданным приложением.
SHEETS_SCOPE = "https://www.googleapis.com/auth/drive.file"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_TITLE_MAXLEN = 100  # лимит длины имени листа в Google Sheets
_FORBIDDEN = set("[]:*?/\\")  # символы, недопустимые в имени листа


@dataclass
class SheetTab:
    title: str
    rows: list[list[Any]]  # включая строку-шапку


def _sanitize_title(title: str, seen: set[str]) -> str:
    """Имя листа: убрать запрещённые символы, обрезать до лимита, гарантировать уникальность."""
    t = "".join(" " if ch in _FORBIDDEN else ch for ch in (title or "")).strip()[:_TITLE_MAXLEN]
    t = t or "Лист"
    base, i = t, 2
    while t in seen:
        suffix = f" ({i})"
        t = base[: _TITLE_MAXLEN - len(suffix)] + suffix
        i += 1
    seen.add(t)
    return t


def build_sheets_data(report: ReportData) -> list[SheetTab]:
    """Чистая сборка вкладок (без сети): «Сводка» + по вкладке на разбивку. Зеркало xlsx."""
    seen: set[str] = set()
    p = report.period
    currency = getattr(report, "currency", "") or ""  # defensive: фейк-репорты без поля
    headers = metric_headers(currency)  # §9: код валюты на денежных колонках
    summary_meta: list[list[Any]] = [
        [f"Отчёт по аккаунту {report.customer_id}"],
        [f"Период: {p.label} ({p.date_from.isoformat()} — {p.date_to.isoformat()})"],
    ]
    if currency:
        summary_meta.append([f"Валюта: {currency}"])
    summary_rows: list[list[Any]] = [
        *summary_meta,
        [],
        ["Период", *headers],
        [p.label, *report.totals.as_row()],
    ]
    if report.prev_totals is not None:
        summary_rows.append([p.previous().label, *report.prev_totals.as_row()])
    tabs = [SheetTab(_sanitize_title("Сводка", seen), summary_rows)]

    for b in report.breakdowns:
        rows: list[list[Any]] = []
        if b.note:
            rows.append([b.note])  # пометка об усечении — первой строкой
        rows.append([*b.dim_headers, *headers])
        for dims, m in b.rows:
            rows.append([*dims, *m.as_row()])
        tabs.append(SheetTab(_sanitize_title(b.title, seen), rows))
    return tabs


def _default_title(report: ReportData) -> str:
    p = report.period
    return f"Aimash {report.customer_id} {p.date_from.isoformat()}—{p.date_to.isoformat()}"


def _build_service() -> Any:
    """Sheets v4 service из OAuth-кредов (.env). Ленивый импорт google-libs."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from core.config import settings

    creds = Credentials(
        token=None,
        refresh_token=settings.google_ads_refresh_token.get_secret_value(),
        token_uri=_TOKEN_URI,
        client_id=settings.google_ads_client_id,
        client_secret=settings.google_ads_client_secret.get_secret_value(),
        scopes=[SHEETS_SCOPE],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def publish_report_to_sheets(
    report: ReportData, *, title: str | None = None, service: Any = None
) -> str:
    """Создать новую таблицу с вкладками отчёта и вернуть ссылку. `service` — для тестов (мок).
    Логирует вызовы Sheets API (создание + запись значений, длительность, исход — БЕЗ секретов;
    §15): раньше путь был «молчащим» — при сбое scope/сети не было трейса для разбора."""
    tabs = build_sheets_data(report)
    svc = service or _build_service()
    start = time.monotonic()
    try:
        created = (
            svc.spreadsheets()
            .create(
                body={
                    "properties": {"title": title or _default_title(report)},
                    "sheets": [{"properties": {"title": t.title}} for t in tabs],
                },
                fields="spreadsheetId,spreadsheetUrl",
            )
            .execute()
        )
        sid = created["spreadsheetId"]
        url = created.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}"
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sid,
            body={
                "valueInputOption": "RAW",
                "data": [{"range": f"'{t.title}'!A1", "values": t.rows} for t in tabs],
            },
        ).execute()
    except Exception as e:
        log.warning(
            "sheets-publish: %s за %dмс (вкладок=%d)",
            type(e).__name__,
            int((time.monotonic() - start) * 1000),
            len(tabs),
        )
        raise
    log.info(
        "sheets-publish: ok за %dмс (вкладок=%d)", int((time.monotonic() - start) * 1000), len(tabs)
    )
    return url

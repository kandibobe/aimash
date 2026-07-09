"""Экспорт отчёта (reports.service.ReportData) в Google Sheets (ТЗ §9, §16). READ-ONLY.

Создаёт НОВУЮ таблицу (spreadsheets.create) с листом «Сводка» + листом на каждую разбивку,
заполняет значения (values.batchUpdate) и возвращает ссылку. Раскладка зеркалит reports.xlsx
(та же шапка METRIC_HEADERS, те же строки). Шапка форматируется best-effort через
spreadsheets.batchUpdate (жирная строка 1 + freeze; §16 P2-b) — сбой формата не роняет экспорт.

⚠️ Требует OAuth-scope drive.file (или spreadsheets), которого НЕТ у Google Ads токена (adwords).
Включается отдельным re-auth (см. docs/DEPLOYMENT.md, раздел «Google Sheets»). Без scope вызов
упадёт — бот покажет понятное сообщение. Чистая сборка (build_sheets_data) тестируется офлайн;
сеть инкапсулирована в publish_report_to_sheets (service подменяем в тестах).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from core.logging import log
from reports.queries import metric_headers
from reports.service import ReportData

# drive.file — минимально достаточный scope для СОЗДАНИЯ таблиц (доступ к файлам, созданным нами).
SHEETS_SCOPE = "https://www.googleapis.com/auth/drive.file"
# spreadsheets.readonly — чтобы ЧИТАТЬ произвольную таблицу менеджера (§19.4.1 ввод «Ссылка на
# Google Sheets»): drive.file видит только созданное нами, readonly — любую доступную пользователю.
# Требует повторного OAuth-consent с этим scope (см. scripts/get_refresh_token.py).
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
# Список для OAuth-CONSENT (scripts/get_refresh_token.py), НЕ для refresh: на refresh scope НЕ шлём
# (см. _build_service — иначе invalid_scope, если токен выдан без readonly).
SHEETS_SCOPES = [SHEETS_SCOPE, SHEETS_READONLY_SCOPE]
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


def _oauth_credentials() -> Any:
    """OAuth-креды (.env) для Google API. Ленивый импорт google-libs.

    scopes=None НАМЕРЕННО: при refresh-гранте нельзя запрашивать scope ШИРЕ выданного токену —
    иначе Google вернёт invalid_scope (Bad Request) и упадёт ВЕСЬ Sheets-экспорт (не только чтение
    чужих таблиц). None ⇒ scope в запросе не шлём, токен обновляется с тем набором, что был выдан
    на consent: drive.file → создание работает; spreadsheets.readonly появится после re-OAuth и
    чтение чужой таблицы (§19.4.1) заработает само. SHEETS_SCOPES — это список для CONSENT
    (scripts/get_refresh_token.py), НЕ для refresh. Без re-OAuth чтение чужой таблицы даст 403
    (не invalid_scope) — ловим и просим прислать ключи текстом."""
    from google.oauth2.credentials import Credentials

    from core.config import settings

    return Credentials(
        token=None,
        refresh_token=settings.google_ads_refresh_token.get_secret_value(),
        token_uri=_TOKEN_URI,
        client_id=settings.google_ads_client_id,
        client_secret=settings.google_ads_client_secret.get_secret_value(),
        scopes=None,
    )


def _build_service() -> Any:
    """Sheets v4 service из OAuth-кредов (.env)."""
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=_oauth_credentials(), cache_discovery=False)


def _build_drive_service() -> Any:
    """Drive v3 service — ТОЛЬКО для permissions.create на созданных нами файлах (drive.file
    это разрешает без доп. scope). Sheets v4 API управлять доступом не умеет."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_oauth_credentials(), cache_discovery=False)


def _share_anyone(spreadsheet_id: str, *, role: str, drive_service: Any = None) -> bool:
    """Живой тест 2026-07-06: созданная таблица была приватной для OAuth-аккаунта бота —
    заказчик не мог открыть ссылку. Открываем anyone-with-link (role: writer — таблицы ключей,
    флоу просит их править; reader — отчёты). НИКОГДА не raise: сбой шаринга не должен ронять
    экспорт — ссылка всё равно уходит, вызыватель добавит подсказку «запросите доступ».
    Возвращает True при успехе.

    B3: под флагом settings.sheets_public_link (дефолт True — прежнее поведение). False ⇒ НЕ шарим
    публично (финансовые данные клиента не за периметром): таблица остаётся приватной, получатель
    запрашивает доступ. Флаг решает владелец в проде."""
    from core.config import settings

    if not settings.sheets_public_link:
        return False  # приватная таблица (владелец отключил публичную ссылку)
    try:
        svc = drive_service or _build_drive_service()
        svc.permissions().create(
            fileId=spreadsheet_id, body={"type": "anyone", "role": role}, fields="id"
        ).execute()
        return True
    except Exception as e:  # noqa: BLE001 — деградация: таблица останется приватной
        log.warning("sheets-share: %s — таблица останется приватной", type(e).__name__)
        return False


def _format_headers(svc: Any, spreadsheet_id: str, n_tabs: int) -> None:
    """§9/§16 (P2-b): косметика — жирная строка 1 + фиксация (freeze) на каждой вкладке через
    spreadsheets().batchUpdate (метод §16, ранее не использовался). BEST-EFFORT: сбой форматирования
    НЕ роняет экспорт (значения уже записаны) — логируем и продолжаем. Зеркалит xlsx-шапку."""
    requests: list[dict] = []
    for i in range(n_tabs):
        requests.append(
            {
                "repeatCell": {
                    "range": {"sheetId": i, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": i, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": requests}
        ).execute()
    except Exception as e:  # noqa: BLE001 — форматирование необязательно, экспорт уже успешен
        log.warning("sheets-format: %s (пропуск форматирования шапки)", type(e).__name__)


def publish_report_to_sheets(
    report: ReportData,
    *,
    title: str | None = None,
    service: Any = None,
    drive_service: Any = None,
) -> tuple[str, bool]:
    """Создать новую таблицу с вкладками отчёта и вернуть (ссылка, расшарена_ли). `service`/
    `drive_service` — для тестов (моки). Отчёт открывается anyone-with-link ЧИТАТЕЛЕМ (финансовый
    артефакт — писать в него никому не нужно). Логирует вызовы Sheets API (создание + запись
    значений, длительность, исход — БЕЗ секретов; §15)."""
    tabs = build_sheets_data(report)
    svc = service or _build_service()
    start = time.monotonic()
    try:
        created = (
            svc.spreadsheets()
            .create(
                body={
                    "properties": {"title": title or _default_title(report)},
                    # sheetId=i явно (P2-b): чтобы адресовать вкладку в форматирующем batchUpdate
                    # без доп. запроса на чтение sheetId.
                    "sheets": [
                        {"properties": {"title": t.title, "sheetId": i}} for i, t in enumerate(tabs)
                    ],
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
        _format_headers(svc, sid, len(tabs))  # §9/§16 (P2-b): жирная шапка + фиксация строки 1
    except Exception as e:
        log.warning(
            "sheets-publish: %s за %dмс (вкладок=%d)",
            type(e).__name__,
            int((time.monotonic() - start) * 1000),
            len(tabs),
        )
        raise
    shared = _share_anyone(sid, role="reader", drive_service=drive_service)
    log.info(
        "sheets-publish: ok за %dмс (вкладок=%d, shared=%s)",
        int((time.monotonic() - start) * 1000),
        len(tabs),
        shared,
    )
    return url, shared


# ── §19.4.2: выгрузка ключей с пометкой релевантности + чтение верифицированного списка ─────
_KW_HEADERS = ["Keyword", "Avg. searches", "Competition", "Top-of-page bid", "Релевантность"]
_RELEVANT_MARK = "✅ Релевантно"
_IRRELEVANT_MARK = "❌ Нерелевантно"
# «Нет данных» — Google не отдаёт метрики на тест-аккаунтах и по части ключей на проде. Показываем
# честный прочерк, а НЕ ложный 0/$0.00 (для профессионала это выглядело бы как реальная оценка).
_NO_DATA = "—"


def build_keyword_sheet_rows(ideas, relevance: dict[str, bool]) -> list[list]:
    """Строки таблицы ключей (§19.4.2): шапка + по строке на идею. ideas — ads.keyword_plan.KeywordIdea.
    Релевантность из relevance (по тексту); отсутствующее → релевантно (advisory). Чистая сборка.
    Пустые метрики (объём/конкуренция/ставка = 0/UNSPECIFIED) → «—», не ложный ноль."""
    rows: list[list] = [list(_KW_HEADERS)]
    for it in ideas:
        low = float(getattr(it, "low_bid", 0.0) or 0.0)
        high = float(getattr(it, "high_bid", 0.0) or 0.0)
        bid = f"{low:.2f}–{high:.2f}" if (low or high) else _NO_DATA
        vol = int(getattr(it, "avg_monthly_searches", 0) or 0)
        comp = (getattr(it, "competition", "") or "").upper()
        rel = relevance.get(getattr(it, "text", ""), True)
        rows.append(
            [
                getattr(it, "text", ""),
                vol if vol > 0 else _NO_DATA,
                comp if comp and comp != "UNSPECIFIED" else _NO_DATA,
                bid,
                _RELEVANT_MARK if rel else _IRRELEVANT_MARK,
            ]
        )
    return rows


def publish_keywords_to_sheets(
    ideas,
    relevance: dict[str, bool],
    *,
    title: str,
    service: Any = None,
    drive_service: Any = None,
) -> tuple[str, str, bool]:
    """Создать таблицу ключей с колонкой «Релевантность» и вернуть (url, spreadsheet_id,
    расшарена_ли). service/drive_service — для тестов (моки). spreadsheet_id нужен на возврате
    для сверки присланной менеджером ссылки. Таблица открывается anyone-with-link РЕДАКТОРОМ:
    флоу просит менеджера/заказчика править её («удалите лишние строки») с любого Google-аккаунта."""
    rows = build_keyword_sheet_rows(ideas, relevance)
    svc = service or _build_service()
    start = time.monotonic()
    try:
        created = (
            svc.spreadsheets()
            .create(
                body={
                    "properties": {"title": title[:_TITLE_MAXLEN]},
                    "sheets": [{"properties": {"title": "Keywords"}}],
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
                "data": [{"range": "'Keywords'!A1", "values": rows}],
            },
        ).execute()
    except Exception as e:
        log.warning(
            "sheets-kw-publish: %s за %dмс (строк=%d)",
            type(e).__name__,
            int((time.monotonic() - start) * 1000),
            len(rows),
        )
        raise
    shared = _share_anyone(sid, role="writer", drive_service=drive_service)
    log.info(
        "sheets-kw-publish: ok за %dмс (строк=%d, shared=%s)",
        int((time.monotonic() - start) * 1000),
        len(rows),
        shared,
    )
    return url, sid, shared


def parse_spreadsheet_id(url_or_id: str) -> str | None:
    """Извлечь spreadsheetId из ссылки Google Sheets или принять «голый» id. None — не распознано."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", s):  # уже похоже на id
        return s
    return None


def read_keyword_column(
    spreadsheet_id: str, *, service: Any = None, sheet_range: str = "A:E"
) -> list[str]:
    """Прочитать верифицированный список ключей из колонки A таблицы (после правок менеджера).
    Пропускаем строку-шапку; берём непустые значения колонки Keyword. service — для тестов (мок).

    §19.4.1: со scope spreadsheets.readonly читаем ЛЮБУЮ доступную пользователю таблицу (не только
    созданную ботом) — менеджер может прислать ссылку на свою таблицу с ключами. Таблица должна быть
    доступна аккаунту OAuth (свой файл или расшаренный)."""
    svc = service or _build_service()
    start = time.monotonic()
    try:
        resp = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_range)
            .execute()
        )
    except Exception as e:
        log.warning(
            "sheets-kw-read: %s за %dмс", type(e).__name__, int((time.monotonic() - start) * 1000)
        )
        raise
    values = resp.get("values", []) or []
    out: list[str] = []
    seen: set[str] = set()
    header = {h.casefold() for h in _KW_HEADERS}
    skipped_irrelevant = 0
    for i, row in enumerate(values):
        cell = (row[0] if row else "").strip()
        if not cell:
            continue
        if i == 0 and cell.casefold() in header:  # шапка
            continue
        # §19.4.2: колонка «Релевантность» (E) — подсказка бота. Строку, ЯВНО помеченную ботом
        # «❌ Нерелевантно» и НЕ переопределённую менеджером, в кампанию не берём (safety-net против
        # «проверил пометки, но забыл удалить строки»). Хочет оставить — меняет пометку на ✅ или чистит.
        relevance = (row[4] if len(row) > 4 else "").strip()
        if relevance.startswith("❌"):
            skipped_irrelevant += 1
            continue
        key = cell.casefold()
        if key not in seen:
            seen.add(key)
            out.append(cell)
    log.info(
        "sheets-kw-read: ok за %dмс (ключей=%d, отброшено ❌=%d)",
        int((time.monotonic() - start) * 1000),
        len(out),
        skipped_irrelevant,
    )
    return out

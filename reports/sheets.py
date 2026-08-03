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
from reports.labels import (
    account_report_title,
    currency_line,
    loc,
    loc_all,
    metric_headers,
    period_line,
    top_note,
)
from reports.queries import METRIC_FORMATS, TOP_N
from reports.service import ReportData

# drive.file — минимально достаточный scope для СОЗДАНИЯ таблиц (доступ к файлам, созданным нами).
# У Google он NON-SENSITIVE: верификация приложения для него не нужна.
SHEETS_SCOPE = "https://www.googleapis.com/auth/drive.file"
# spreadsheets.readonly — чтобы ЧИТАТЬ произвольную таблицу менеджера (§19.4.1 ввод «Ссылка на
# Google Sheets»): drive.file видит только созданное нами, readonly — любую доступную пользователю.
# У Google это SENSITIVE-scope: неверифицированное приложение с ним Google БЛОКИРУЕТ («This app is
# blocked… tried to access sensitive info»). Поэтому его несёт только Ads-токен (наш аккаунт, consent
# выдан), а НЕ токен аккаунта-хранилища (может быть чужим — заказчика).
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
# Список для OAuth-CONSENT аккаунта-ХРАНИЛИЩА (scripts/get_refresh_token.py --sheets), НЕ для refresh:
# на refresh scope НЕ шлём (см. _oauth_credentials — иначе invalid_scope). Ровно [drive.file]:
# добавишь сюда sensitive-scope — вернёшь «This app is blocked» (гард — tests/test_keyword_sheets.py).
SHEETS_SCOPES = [SHEETS_SCOPE]
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_TITLE_MAXLEN = 100  # лимит длины имени листа в Google Sheets
_FORBIDDEN = set("[]:*?/\\")  # символы, недопустимые в имени листа

# Исход шаринга (_share_anyone → publish_* → хендлер). Успех = ВЫДАННАЯ роль ("reader"/"writer"),
# отказ различаем по причине: раньше и «выключено владельцем», и «Drive отказал» давали False, и бот
# в обоих случаях писал «не удалось открыть доступ» — во втором случае это враньё.
SHARE_OFF = "off"  # публичные ссылки выключены владельцем (SHEETS_PUBLIC_LINK=false)
SHARE_FAILED = "failed"  # Drive отказал (типично: политика домена запрещает внешние ссылки)


def is_shared(status: str) -> bool:
    """True — таблица открыта anyone-with-link (status = выданная роль)."""
    return status not in (SHARE_OFF, SHARE_FAILED)


@dataclass
class SheetTab:
    title: str
    rows: list[list[Any]]  # включая строку-шапку
    # Раскладка вкладки — чтобы форматировать ЧИСЛА, а не только шапку (см. _format_tabs):
    dim_count: int = 0  # сколько колонок-измерений перед колонками метрик
    header_row: int = 0  # индекс строки-шапки (0-based): пометка об усечении/мета сдвигают её
    # Формат колонок (0-based индекс, паттерн). None → колонки метрик по METRIC_FORMATS с dim_count
    # (прежнее поведение). Задан → вкладка НЕ метрическая (лист «Находки»: одна денежная колонка).
    formats: list[tuple[int, str]] | None = None


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


def _findings_tab(result, currency: str, lang: str, seen: set[str]) -> SheetTab:
    """Вкладка «Находки» — зеркало листа .xlsx (раскладка общая, reports.findings)."""
    from reports.findings import (
        FINDINGS_FORMATS,
        FINDINGS_TITLE,
        findings_headers,
        findings_meta_rows,
        findings_rows,
    )

    meta = findings_meta_rows(result, lang)
    rows: list[list[Any]] = [
        *meta,
        [],
        findings_headers(currency, lang),
        *findings_rows(result, lang),
    ]
    return SheetTab(
        _sanitize_title(loc(FINDINGS_TITLE, lang), seen),
        rows,
        header_row=len(meta) + 1,  # обзор + пустая строка
        formats=FINDINGS_FORMATS,
    )


def _summary_tab(report: ReportData, lang: str, seen: set[str]) -> SheetTab:
    """Вкладка «Сводка»: шапка аккаунта/период/валюта + строка(и) итогов. Общий раскладчик для отчёта
    и обогащённой выгрузки /audit (одно поведение — один код)."""
    from reports.period import label_i18n

    p = report.period
    currency = getattr(report, "currency", "") or ""  # defensive: фейк-репорты без поля
    headers = metric_headers(currency, lang)  # §9: код валюты на денежных колонках
    summary_meta: list[list[Any]] = [
        [account_report_title(report.customer_id, lang)],
        [period_line(p, lang)],
    ]
    if currency:
        summary_meta.append([currency_line(currency, lang)])
    summary_rows: list[list[Any]] = [
        *summary_meta,
        [],
        [loc("Период", lang), *headers],
        [label_i18n(p, lang), *report.totals.as_row()],
    ]
    if report.prev_totals is not None:
        summary_rows.append([label_i18n(p.previous(), lang), *report.prev_totals.as_row()])
    return SheetTab(
        _sanitize_title(loc("Сводка", lang), seen),
        summary_rows,
        dim_count=1,  # колонка «Период»
        header_row=len(summary_meta) + 1,  # мета + пустая строка
    )


def _breakdown_tab(b, currency: str, lang: str, seen: set[str]) -> SheetTab:
    """Вкладка одной разбивки: (опц.) пометка об усечении + шапка измерений/метрик + строки. Общий
    раскладчик для отчёта и секции данных выгрузки /audit."""
    headers = metric_headers(currency, lang)
    rows: list[list[Any]] = []
    kind = getattr(b, "note_kind", None)  # defensive: фейк-разбивки в тестах без поля
    note = top_note(kind, TOP_N, lang) if (b.note and kind) else b.note
    if note:
        rows.append([note])  # пометка об усечении — первой строкой
    header_row = len(rows)
    rows.append([*loc_all(b.dim_headers, lang), *headers])
    for dims, m in b.rows:
        rows.append([*dims, *m.as_row()])
    return SheetTab(
        _sanitize_title(loc(b.title, lang), seen),
        rows,
        dim_count=len(b.dim_headers),
        header_row=header_row,
    )


def build_sheets_data(report: ReportData, lang: str = "ru", *, audit=None) -> list[SheetTab]:
    """Чистая сборка вкладок (без сети): «Сводка» + «Находки» (если подан audit) + по вкладке на
    разбивку. Зеркало xlsx. lang — язык ПОДПИСЕЙ (reports.labels); значения ячеек (имена кампаний,
    статусы) не трогаем. audit — AuditResult (keyword-only ⇒ прежние вызовы целы); None → вкладки
    находок нет: собирать аудит — 23 чтения, решает вызыватель."""
    seen: set[str] = set()
    currency = getattr(report, "currency", "") or ""  # defensive: фейк-репорты без поля
    tabs = [_summary_tab(report, lang, seen)]
    if audit is not None:
        tabs.append(_findings_tab(audit, currency, lang, seen))  # сразу за «Сводкой»
    for b in report.breakdowns:
        tabs.append(_breakdown_tab(b, currency, lang, seen))
    return tabs


def build_audit_sheets_data(result, lang: str = "ru") -> list[SheetTab]:
    """Чистая сборка 3 вкладок выгрузки /audit из УЖЕ вычисленного AuditResult (без сети и без
    доп-чтений Google Ads): «Обзор» (карточка без топ-3/дисклеймера), «По семьям» (свод) и «Находки»
    (весь список worst-first). Зеркало reports.xlsx.build_audit_workbook. lang — язык ПОДПИСЕЙ; значения
    ячеек (имена кампаний, тексты находок) не трогаем.

    На вход готовый AuditResult — слой reports не пересобирает аудит и не импортирует audit-слой (гард
    test_mcc_deep_health_is_engine_only): полный разбор веером по MCC (23 чтения) сюда не ходит."""
    from reports.findings import (
        FAMILY_SUMMARY_FORMATS,
        FAMILY_SUMMARY_TITLE,
        FINDINGS_FORMATS,
        FINDINGS_TITLE,
        OVERVIEW_TITLE,
        family_summary_headers,
        family_summary_rows,
        findings_headers,
        findings_meta_rows,
        findings_rows,
    )

    currency = getattr(result, "currency", "") or ""
    seen: set[str] = set()
    # Порядок — замечание 5 (2026-07-17): «Находки» ПЕРВОЙ (человек открывает файл ради них),
    # «Обзор» ПОСЛЕДНЕЙ — это дословная копия поста в чате, из-за неё выгрузка читалась как
    # «те же данные, что и в посте». Зеркало xlsx.build_audit_workbook.
    tabs = [
        SheetTab(
            _sanitize_title(loc(FINDINGS_TITLE, lang), seen),
            [findings_headers(currency, lang), *findings_rows(result, lang)],
            formats=FINDINGS_FORMATS,
        ),
        SheetTab(
            _sanitize_title(loc(FAMILY_SUMMARY_TITLE, lang), seen),
            [family_summary_headers(currency, lang), *family_summary_rows(result, lang)],
            formats=FAMILY_SUMMARY_FORMATS,
        ),
    ]
    # Секция ДАННЫХ (жалоба владельца «в щитс те же данные, что и в посте»): сырьё, прицепленное
    # слоем сбора аудита к result. Нет report (engine-only вызов / старый кэш) → без секции.
    report = getattr(result, "report", None)
    if report is not None:
        rcur = getattr(report, "currency", "") or currency
        tabs.append(_summary_tab(report, lang, seen))
        extra = getattr(result, "audit_tables", None) or []
        for b in [*getattr(report, "breakdowns", []), *extra]:
            tabs.append(_breakdown_tab(b, rcur, lang, seen))
    tabs.append(
        SheetTab(
            _sanitize_title(loc(OVERVIEW_TITLE, lang), seen),
            findings_meta_rows(result, lang),
            # проза в колонке A — числовых колонок нет; formats=[] гасит метрик-формат по умолчанию
            formats=[],
        )
    )
    return tabs


def _default_title(report: ReportData) -> str:
    p = report.period
    return f"Aimash {report.customer_id} {p.date_from.isoformat()}—{p.date_to.isoformat()}"


def _audit_title(result, lang: str = "ru") -> str:
    cid = getattr(result, "customer_id", "") or ""
    return f"Aimash audit {cid}" if lang == "en" else f"Aimash аудит {cid}"


def _oauth_credentials(*, external_read: bool = False) -> Any:
    """OAuth-креды (.env) для Google API. Ленивый импорт google-libs.

    ЧЕЙ АККАУНТ. Таблица создаётся в «Моём диске» того Google-аккаунта, чьим refresh-токеном мы
    ходим (он же владелец файла, его квота). SHEETS_REFRESH_TOKEN задан ⇒ Sheets/Drive идут под
    ним (аккаунт-хранилище таблиц, напр. myhalads@gmail.com); пусто ⇒ прежнее поведение — тот же
    токен, что у Google Ads. Ads-токен при этом НЕ трогаем: у аккаунта-хранилища доступа к MCC
    может не быть, перевыпуск общего токена под него уронил бы весь Ads (scope adwords).
    client_id/secret Sheets-а пустые ⇒ Ads-овские (один и тот же OAuth-клиент Google Cloud —
    именно им выдавался токен, чужой клиент refresh не примет).

    external_read=True — чтение ЧУЖОЙ (не созданной ботом) таблицы: нужен sensitive-scope
    spreadsheets.readonly, а его несёт только Ads-токен. У аккаунта-хранилища мы его НЕ просим:
    неверифицированному приложению Google на sensitive-scope отвечает «This app is blocked», и
    заказчик просто не сможет выдать согласие. Ads-токена нет ⇒ падаем назад на sheets-креды
    (хуже не будет: без readonly чтение и так даст 403, который наверху ловится).

    scopes=None НАМЕРЕННО: при refresh-гранте нельзя запрашивать scope ШИРЕ выданного токену —
    иначе Google вернёт invalid_scope (Bad Request) и упадёт ВЕСЬ Sheets-экспорт (не только чтение
    чужих таблиц). None ⇒ scope в запросе не шлём, токен обновляется с тем набором, что был выдан
    на consent. SHEETS_SCOPES — это список для CONSENT (scripts/get_refresh_token.py), НЕ для
    refresh. Чтение чужой таблицы токеном без readonly даёт 403 (не invalid_scope) — ловим и просим
    прислать ключи текстом."""
    from google.oauth2.credentials import Credentials

    from core.config import settings

    ads_refresh = settings.google_ads_refresh_token.get_secret_value()
    sheets_refresh = settings.sheets_refresh_token.get_secret_value()

    if external_read and ads_refresh:  # sensitive-scope есть только у Ads-токена
        refresh = ads_refresh
        client_id = settings.google_ads_client_id
        secret = settings.google_ads_client_secret.get_secret_value()
    elif sheets_refresh:  # отдельный аккаунт-хранилище таблиц
        refresh = sheets_refresh
        client_id = settings.sheets_client_id or settings.google_ads_client_id
        secret = (
            settings.sheets_client_secret.get_secret_value()
            or settings.google_ads_client_secret.get_secret_value()
        )
    else:  # прежнее поведение: Sheets ходит под Ads-токеном
        refresh = ads_refresh
        client_id = settings.google_ads_client_id
        secret = settings.google_ads_client_secret.get_secret_value()

    return Credentials(
        token=None,
        refresh_token=refresh,
        token_uri=_TOKEN_URI,
        client_id=client_id,
        client_secret=secret,
        scopes=None,
    )


def _build_service(*, external_read: bool = False) -> Any:
    """Sheets v4 service из OAuth-кредов (.env). external_read — см. _oauth_credentials."""
    from googleapiclient.discovery import build

    return build(
        "sheets",
        "v4",
        credentials=_oauth_credentials(external_read=external_read),
        cache_discovery=False,
    )


def _build_drive_service() -> Any:
    """Drive v3 service — ТОЛЬКО для permissions.create на созданных нами файлах (drive.file
    это разрешает без доп. scope). Sheets v4 API управлять доступом не умеет."""
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_oauth_credentials(), cache_discovery=False)


def _share_failure_reason(e: Exception) -> str:
    """Причина отказа Drive для лога: класс + HTTP-статус + тело ответа Google, ОТРЕДАКТИРОВАННОЕ
    (правило 5 — исключение google-api может нести токен). Без этого диагноз невозможен: по одному
    type(e).__name__ не отличить «нет scope» (403 insufficientPermissions) от «политика домена
    запрещает внешние ссылки» (403 sharingRateLimitExceeded / forbidden)."""
    from core.logging import redact_text

    status = getattr(getattr(e, "resp", None), "status", None)
    return f"{type(e).__name__} status={status}: {redact_text(str(e))[:300]}"


def _share_anyone(spreadsheet_id: str, *, role: str, drive_service: Any = None) -> str:
    """Живой тест 2026-07-06: созданная таблица была приватной для OAuth-аккаунта бота —
    заказчик не мог открыть ссылку. Открываем anyone-with-link (role: writer — таблицы ключей,
    флоу просит их править; reader — отчёты). НИКОГДА не raise: сбой шаринга не должен ронять
    экспорт — ссылка всё равно уходит, вызыватель добавит подсказку «запросите доступ».

    Возвращает СТАТУС: выданную роль при успехе, SHARE_OFF (владелец выключил публичные ссылки) или
    SHARE_FAILED (Drive отказал) — вызыватель показывает РАЗНЫЕ подсказки, а не одну на оба случая.

    B3: под флагом settings.sheets_public_link (дефолт True — прежнее поведение). False ⇒ НЕ шарим
    публично (финансовые данные клиента не за периметром): таблица остаётся приватной, получатель
    запрашивает доступ. Флаг решает владелец в проде."""
    from core.config import settings

    if not settings.sheets_public_link:
        return SHARE_OFF  # приватная таблица (владелец отключил публичную ссылку)
    try:
        svc = drive_service or _build_drive_service()
        svc.permissions().create(
            fileId=spreadsheet_id, body={"type": "anyone", "role": role}, fields="id"
        ).execute()
        return role
    except Exception as e:  # noqa: BLE001 — деградация: таблица останется приватной
        log.warning("sheets-share: %s — таблица останется приватной", _share_failure_reason(e))
        return SHARE_FAILED


def _number_format(pattern: str) -> dict:
    """Паттерн Excel → userEnteredFormat.numberFormat Google Sheets (синтаксис паттерна общий)."""
    return {"type": "PERCENT" if "%" in pattern else "NUMBER", "pattern": pattern}


def format_requests(tabs: list[SheetTab]) -> list[dict]:
    """Запросы форматирования вкладок (чистая функция — тестируется офлайн).

    Шапка: жирная + freeze ПО СВОЕЙ строке (пометка об усечении/мета-строки сдвигают её вниз —
    раньше жирнили строку 1, то есть пометку, а шапку не трогали).
    Числа: формат по колонкам метрик из реестра METRIC_FORMATS — тот же, что в .xlsx. Раньше в
    Sheets форматов не было вовсе: CTR читался как «0.0523» (сырая доля вместо 5,23%), расход —
    без разделителей разрядов. valueInputOption=RAW → значения остаются ЧИСЛАМИ, формат только
    отображает их правильно."""
    requests: list[dict] = []
    for i, t in enumerate(tabs):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": i,
                        "startRowIndex": t.header_row,
                        "endRowIndex": t.header_row + 1,
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": i,
                        "gridProperties": {"frozenRowCount": t.header_row + 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            }
        )
        if len(t.rows) <= t.header_row + 1:  # данных нет — форматировать нечего
            continue
        cols = (
            [(t.dim_count + j, p) for j, p in enumerate(METRIC_FORMATS)]
            if t.formats is None
            else t.formats
        )
        for col, pattern in cols:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": i,
                            "startRowIndex": t.header_row + 1,
                            "endRowIndex": len(t.rows),
                            "startColumnIndex": col,
                            "endColumnIndex": col + 1,
                        },
                        "cell": {"userEnteredFormat": {"numberFormat": _number_format(pattern)}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
    return requests


def _format_tabs(svc: Any, spreadsheet_id: str, tabs: list[SheetTab]) -> None:
    """§9/§16 (P2-b): формат вкладок одним spreadsheets().batchUpdate. BEST-EFFORT: сбой
    форматирования НЕ роняет экспорт (значения уже записаны) — логируем и продолжаем."""
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": format_requests(tabs)}
        ).execute()
    except Exception as e:  # noqa: BLE001 — форматирование необязательно, экспорт уже успешен
        log.warning("sheets-format: %s (пропуск форматирования)", type(e).__name__)


def _publish_tabs(
    tabs: list[SheetTab],
    title: str,
    *,
    role: str,
    service: Any,
    drive_service: Any,
    log_label: str,
) -> tuple[str, str]:
    """Общий транспорт Sheets: создать таблицу с ГОТОВЫМИ вкладками, записать значения (RAW), задать
    формат (best-effort) и открыть anyone-with-link ролью role. Возвращает (url, статус_шаринга).
    Раскладку строит вызыватель — отчёт и аудит делят один код записи/шаринга/лога (§15, без секретов).
    Сбой шаринга не роняет экспорт (см. _share_anyone); сбой create/write логируется и пробрасывается."""
    svc = service or _build_service()
    start = time.monotonic()
    try:
        created = (
            svc.spreadsheets()
            .create(
                body={
                    "properties": {"title": title[:_TITLE_MAXLEN]},
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
        _format_tabs(svc, sid, tabs)  # §9/§16 (P2-b): жирная шапка + фиксация + форматы чисел
    except Exception as e:
        log.warning(
            "%s: %s за %dмс (вкладок=%d)",
            log_label,
            type(e).__name__,
            int((time.monotonic() - start) * 1000),
            len(tabs),
        )
        raise
    share = _share_anyone(sid, role=role, drive_service=drive_service)
    log.info(
        "%s: ok за %dмс (вкладок=%d, share=%s)",
        log_label,
        int((time.monotonic() - start) * 1000),
        len(tabs),
        share,
    )
    return url, share


def publish_report_to_sheets(
    report: ReportData,
    *,
    title: str | None = None,
    service: Any = None,
    drive_service: Any = None,
    lang: str = "ru",
    audit=None,
) -> tuple[str, str]:
    """Создать новую таблицу с вкладками отчёта и вернуть (ссылка, статус_шаринга: роль|off|failed).
    `service`/`drive_service` — для тестов (моки). `audit` — AuditResult → вкладка «Находки» (None →
    её нет). Отчёт открывается anyone-with-link ЧИТАТЕЛЕМ (финансовый артефакт — писать в него никому
    не нужно). Логирует вызовы Sheets API (создание + запись значений, длительность, исход — БЕЗ
    секретов; §15)."""
    tabs = build_sheets_data(report, lang, audit=audit)
    return _publish_tabs(
        tabs,
        title or _default_title(report),
        role="reader",
        service=service,
        drive_service=drive_service,
        log_label="sheets-publish",
    )


def publish_audit_to_sheets(
    result,
    *,
    title: str | None = None,
    service: Any = None,
    drive_service: Any = None,
    lang: str = "ru",
) -> tuple[str, str]:
    """Создать таблицу с 3 вкладками аудита (Обзор/По семьям/Находки) из УЖЕ вычисленного AuditResult
    и вернуть (ссылка, статус_шаринга). Как publish_report_to_sheets — anyone-with-link ЧИТАТЕЛЕМ
    (GR3: экспорт — бумага, «применить» из таблицы нет). На вход готовый result (кэш bot-слоя):
    аудит здесь не пересобираем, доп-чтений Google Ads нет."""
    tabs = build_audit_sheets_data(result, lang)
    return _publish_tabs(
        tabs,
        title or _audit_title(result, lang),
        role="reader",
        service=service,
        drive_service=drive_service,
        log_label="sheets-audit",
    )


# ── Search-term review: editable, advisory-only, with week-over-week evidence ───────────────
_SEARCH_TERM_HEADERS = [
    "Решение",
    "Поисковый запрос",
    "Кампания",
    "Группа",
    "Триггер-ключ",
    "Тип соответствия",
    "Причина кандидата",
    "Расход текущая неделя",
    "Расход прошлая неделя",
    "WoW расход, %",
    "Конверсии текущая неделя",
    "Конверсии прошлая неделя",
    "WoW конверсии, %",
    "Клики текущая неделя",
    "Показы текущая неделя",
    "CTR текущая неделя, %",
]


def build_search_term_review_rows(
    items: list[dict[str, Any]], currency: str = ""
) -> list[list[Any]]:
    """Build an editable review sheet from compact, already-aggregated evidence.

    ``Решение`` is deliberately blank: the model may nominate candidates, but only the manager marks
    ``ДОБАВИТЬ``.  This sheet is not an execution surface; applying reviewed negatives remains a
    separate typed proposal and confirmation.
    """
    headers = list(_SEARCH_TERM_HEADERS)
    if currency:
        headers[7] += f", {currency}"
        headers[8] += f", {currency}"
    rows: list[list[Any]] = [headers]
    for item in items:
        rows.append(
            [
                "",
                item.get("search_term", ""),
                item.get("campaign", ""),
                item.get("ad_group", ""),
                item.get("keyword", ""),
                item.get("match_type", ""),
                item.get("reason", ""),
                item.get("cost", 0.0),
                item.get("previous_cost", 0.0),
                item.get("cost_wow_pct", "—"),
                item.get("conversions", 0.0),
                item.get("previous_conversions", 0.0),
                item.get("conversions_wow_pct", "—"),
                item.get("clicks", 0),
                item.get("impressions", 0),
                item.get("ctr_pct", 0.0),
            ]
        )
    return rows


def publish_search_term_review_to_sheets(
    items: list[dict[str, Any]],
    *,
    title: str,
    currency: str = "",
    service: Any = None,
    drive_service: Any = None,
) -> tuple[str, str, str]:
    """Create an editable search-term review sheet and return ``(url, id, share)``."""
    tabs = [
        SheetTab(
            title="Search terms review",
            rows=build_search_term_review_rows(items, currency),
            formats=[(7, "#,##0.00"), (8, "#,##0.00"), (9, "0.0"), (15, "0.00")],
        )
    ]
    url, share = _publish_tabs(
        tabs,
        title,
        role="writer",
        service=service,
        drive_service=drive_service,
        log_label="sheets-search-terms",
    )
    sheet_id = parse_spreadsheet_id(url)
    if not sheet_id:
        raise RuntimeError("Sheets API returned an invalid spreadsheet URL")
    return url, sheet_id, share


def read_search_term_review(spreadsheet_id: str, *, service: Any = None) -> list[dict[str, str]]:
    """Read only rows explicitly marked ``ДОБАВИТЬ`` from a bot-owned review sheet."""
    svc = service or _build_service(own_file=True)
    values = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="'Search terms review'!A:P")
        .execute()
        .get("values", [])
    )
    accepted = {"добавить", "add", "✅", "yes", "да"}
    out: list[dict[str, str]] = []
    for row in values[1:]:
        padded = list(row) + [""] * (16 - len(row))
        if str(padded[0]).strip().casefold() not in accepted:
            continue
        term = str(padded[1]).strip()
        if term:
            out.append(
                {
                    "search_term": term,
                    "campaign": str(padded[2]).strip(),
                    "ad_group": str(padded[3]).strip(),
                    "reason": str(padded[6]).strip(),
                }
            )
    return out


# ── §19.4.2: выгрузка ключей с пометкой релевантности + чтение верифицированного списка ─────
_KW_HEADERS = ["Keyword", "Avg. searches", "Competition", "Top-of-page bid", "Релевантность"]
_RELEVANT_MARK = "✅ Релевантно"
_IRRELEVANT_MARK = "❌ Нерелевантно"
# «Нет данных» — Google не отдаёт метрики на тест-аккаунтах и по части ключей на проде. Показываем
# честный прочерк, а НЕ ложный 0/$0.00 (для профессионала это выглядело бы как реальная оценка).
_NO_DATA = "—"


def keyword_headers(currency: str = "") -> list[str]:
    """Шапка таблицы ключей с кодом валюты на денежной колонке (§9, как metric_headers): ставка
    приходит в валюте АККАУНТА, и без кода «0.30–0.90» читалось как доллары по умолчанию."""
    if not currency:
        return list(_KW_HEADERS)
    return [f"{h}, {currency}" if h == "Top-of-page bid" else h for h in _KW_HEADERS]


def build_keyword_sheet_rows(ideas, relevance: dict[str, bool], currency: str = "") -> list[list]:
    """Строки таблицы ключей (§19.4.2): шапка + по строке на идею. ideas — ads.keyword_plan.KeywordIdea.
    Релевантность из relevance (по тексту); отсутствующее → релевантно (advisory). Чистая сборка.
    Пустые метрики (объём/конкуренция/ставка = 0/UNSPECIFIED) → «—», не ложный ноль."""
    rows: list[list] = [keyword_headers(currency)]
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
    currency: str = "",
    service: Any = None,
    drive_service: Any = None,
) -> tuple[str, str, str]:
    """Создать таблицу ключей с колонкой «Релевантность» и вернуть (url, spreadsheet_id,
    статус_шаринга: роль|off|failed). service/drive_service — для тестов (моки). spreadsheet_id нужен
    на возврате для сверки присланной менеджером ссылки. Таблица открывается anyone-with-link
    РЕДАКТОРОМ: флоу просит менеджера/заказчика править её («удалите лишние строки») с любого
    Google-аккаунта."""
    rows = build_keyword_sheet_rows(ideas, relevance, currency)
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
    share = _share_anyone(sid, role="writer", drive_service=drive_service)
    log.info(
        "sheets-kw-publish: ok за %dмс (строк=%d, share=%s)",
        int((time.monotonic() - start) * 1000),
        len(rows),
        share,
    )
    return url, sid, share


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
    spreadsheet_id: str, *, service: Any = None, sheet_range: str = "A:E", own_file: bool = False
) -> list[str]:
    """Прочитать верифицированный список ключей из колонки A таблицы (после правок менеджера).
    Пропускаем строку-шапку; берём непустые значения колонки Keyword. service — для тестов (мок).

    own_file=True — таблица СОЗДАНА ботом (§19.4.2 round-trip): читаем кредами аккаунта-хранилища,
    хватает drive.file. По умолчанию (§19.4.1 «Ссылка на Google Sheets», /kw add) таблица ЧУЖАЯ:
    drive.file её не видит, нужен sensitive-scope spreadsheets.readonly ⇒ идём Ads-токеном
    (external_read=True). Значит чужая таблица должна быть доступна ИМЕННО Ads-аккаунту: «всем, у
    кого есть ссылка» либо расшарена на него; иначе 403 — наверху ловим и просим ключи текстом."""
    svc = service or _build_service(external_read=not own_file)
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

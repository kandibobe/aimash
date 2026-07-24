"""Ф5б: строгий парсер CSV «Статистика аукционов» (Auction Insights) из интерфейса Google Ads.

ЗАЧЕМ ФАЙЛ, а не API: метрики `metrics.auction_insight_*` (impression share / overlap / outranking
и пр.) в API v24 СУЩЕСТВУЮТ (справочник полей:
https://developers.google.com/google-ads/api/fields/v24/metrics), но доступ к ним выдаёт только
закрытый вайтлист Google — программа набора закрыта, новых участников не добавляют (подтверждение
команды API: https://groups.google.com/g/adwords-api/c/30s21wGZkOU). Выгрузка из веб-интерфейса —
ЕДИНСТВЕННЫЙ легальный путь узнать, кто именно стоит рядом в аукционе. Суррогат по нашим данным
(доля показов, потери по рангу/бюджету) уже есть — чек `competitive_pressure` (Ф5а); он отвечает
«давят или нет», а этот файл — «кто именно».

ЧУЖОЙ ФАЙЛ В МОДЕЛЬ НЕ ОТДАЁМ (golden rule #5/#6): парсит КОД, схему проверяет КОД. LLM здесь нет —
её нечем ограничить, а файл пришёл извне (класс prompt-injection). Всё, что не распознано схемой, —
ошибка с понятным текстом, а не «доверимся модели, она разберётся».

Что распознаём (EN + RU заголовки, любой порядок колонок):
    домен · доля показов · пересечение · позиция выше · верх страницы · самый верх · опережение

Пороги терпимости — осознанные:
  • разделитель (`,` `;` `\\t`) и кодировка (utf-8-sig / utf-16 / cp1251) — сниффим: RU-Excel
    сохраняет `;`+cp1251, веб-выгрузка — `,`+utf-8-BOM; жёсткий выбор одного ронял бы половину файлов;
  • преамбула («Статистика аукционов», диапазон дат) — пропускаем, ищем СТРОКУ-ЗАГОЛОВОК;
  • «--» → None («не показывалось»), а НЕ 0.0: ноль здесь — утверждение, которого в файле нет (GR8);
  • «< 10%» → 0.10, т.е. ВЕРХНЮЮ границу: конкурента лучше слегка переоценить, чем недооценить.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass

# Заголовки: подстроки-маркеры (lowercase). Порядок кортежа = приоритет проверки: `abs_top` ДО
# `top_of_page`, иначе «Abs. top of page rate» съест ветку обычного верха (обе содержат "top of page").
_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("domain", ("display url domain", "домен", "url domain")),
    (
        "impression_share",
        ("impr. share", "impression share", "полученных показов", "показов получено"),
    ),
    ("overlap_rate", ("overlap", "пересеч")),
    ("position_above_rate", ("position above", "выше", "над результатами")),
    (
        "abs_top_of_page_rate",
        ("abs. top of page", "abs top of page", "самом верху", "самой верхней"),
    ),
    ("top_of_page_rate", ("top of page", "верхней части", "верху страницы")),
    ("outranking_share", ("outranking", "опереж", "превосход")),
)

# Строка «ты сам» в выгрузке (первая строка отчёта). Всё остальное — конкуренты.
_SELF_LABELS = frozenset({"you", "вы", "ты", "your account", "ваш аккаунт"})

_MISSING = frozenset({"", "--", "-", "–", "—", "n/a", "н/д"})
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")

MAX_ROWS = 200  # анти-DoS: аукцион с 200 доменами — уже не отчёт, а свалка


class AuctionInsightsError(ValueError):
    """Файл не распознан как отчёт «Статистика аукционов». Текст — ДЛЯ ПОЛЬЗОВАТЕЛЯ (что прислать)."""


@dataclass(frozen=True)
class CompetitorRow:
    """Строка отчёта. Все доли — 0..1 (как search_is в reports.queries), None = «--» в файле."""

    domain: str
    is_you: bool
    impression_share: float | None = None
    overlap_rate: float | None = None
    position_above_rate: float | None = None
    top_of_page_rate: float | None = None
    abs_top_of_page_rate: float | None = None
    outranking_share: float | None = None


@dataclass(frozen=True)
class AuctionInsights:
    you: CompetitorRow | None  # None → в файле нет строки «Вы» (бывает при выгрузке по конкуренту)
    competitors: tuple[CompetitorRow, ...]  # по убыванию доли показов
    period_label: str  # диапазон дат из преамбулы, как в файле ('' — не нашли). Только для показа.


def _decode(data: bytes) -> str:
    """Байты → текст. Порядок проб — по частоте: веб-выгрузка (utf-8-BOM), Excel-RU (utf-16/cp1251)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _dialect(sample: str) -> str:
    """Разделитель по строке-заголовку: тот из `;` `\\t` `,`, которого больше (Excel-RU → `;`)."""
    counts = {d: sample.count(d) for d in (";", "\t", ",")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _pct(raw: str) -> float | None:
    """«41,09%» → 0.4109 · «--» → None · «< 10%» → 0.10 (ВЕРХНЯЯ граница интервала).

    Без знака `%` значение ≤ 1 считаем уже долей (0.41), иначе процентом (41 → 0.41): Google
    экспортит проценты, но локализованный Excel умеет пересохранить их долей — обе формы валидны,
    и различить их можно только по величине."""
    s = (raw or "").strip().replace(" ", " ")
    if s.lower() in _MISSING:
        return None
    # «< 10%» → 10 → 0.10: берём ВЕРХНЮЮ границу интервала — переоценить соседа по аукциону
    # безопаснее, чем недооценить (и уж точно честнее, чем выдать 0 за факт).
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", "."))
    except ValueError:
        return None
    if "%" not in s and v <= 1.0:
        return max(0.0, min(1.0, v))  # уже доля
    return max(0.0, min(1.0, v / 100.0))


def _cell(row: list[str], cols: dict[str, int], field: str) -> str:
    """Значение колонки в строке. Колонки нет / строка короче — '' (а не IndexError на кривом файле)."""
    i = cols.get(field, -1)
    return row[i] if 0 <= i < len(row) else ""


def _match_columns(header: list[str]) -> dict[str, int] | None:
    """Заголовок → {поле: индекс}. None, если это не строка-заголовок отчёта (нет домена + доли)."""
    cells = [(c or "").strip().lower() for c in header]
    idx: dict[str, int] = {}
    for field, markers in _COLUMNS:
        for i, cell in enumerate(cells):
            if i in idx.values():
                continue  # колонка уже занята более приоритетным полем (abs_top ↔ top_of_page)
            if any(mk in cell for mk in markers):
                idx[field] = i
                break
    if "domain" not in idx or "impression_share" not in idx:
        return None
    return idx


def parse_auction_insights(data: bytes) -> AuctionInsights:
    """CSV из Google Ads → строки отчёта. Схему не узнали → AuctionInsightsError с текстом для чата."""
    text = _decode(data)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise AuctionInsightsError("empty")

    header_at, cols, delim = -1, None, ","
    for i, line in enumerate(lines[:12]):  # заголовок глубже 12-й строки — это не наш отчёт
        d = _dialect(line)
        row = next(csv.reader([line], delimiter=d), [])
        cols = _match_columns(row)
        if cols:
            header_at, delim = i, d
            break
    if cols is None or header_at < 0:
        raise AuctionInsightsError("schema")

    # Преамбула: строка с диапазоном дат — единственное, что берём выше заголовка (для показа).
    # Строку берём СЫРОЙ, не через csv.reader: диапазон «Jan 1, 2026-Jan 31, 2026» сам содержит
    # запятые и в неквотированной выгрузке распался бы на «Jan 1» (и окно потерялось бы молча).
    period = ""
    for line in lines[:header_at]:
        cell = line.strip().strip('"').strip(delim + ' "')
        if re.search(r"\d{4}", cell) and any(sep in cell for sep in ("-", "–", "—", "..")):
            period = cell[:64]

    you: CompetitorRow | None = None
    rivals: list[CompetitorRow] = []
    for row in csv.reader(lines[header_at + 1 :], delimiter=delim):
        if len(rivals) >= MAX_ROWS:
            break
        domain = _cell(row, cols, "domain").strip()
        if not domain or domain.lower().startswith(("total", "всего", "итого")):
            continue  # хвостовая строка «Всего» — агрегат, не участник аукциона
        is_you = domain.lower() in _SELF_LABELS
        item = CompetitorRow(
            domain=domain[:255],
            is_you=is_you,
            impression_share=_pct(_cell(row, cols, "impression_share")),
            overlap_rate=_pct(_cell(row, cols, "overlap_rate")),
            position_above_rate=_pct(_cell(row, cols, "position_above_rate")),
            top_of_page_rate=_pct(_cell(row, cols, "top_of_page_rate")),
            abs_top_of_page_rate=_pct(_cell(row, cols, "abs_top_of_page_rate")),
            outranking_share=_pct(_cell(row, cols, "outranking_share")),
        )
        if is_you:
            you = item
        else:
            rivals.append(item)

    if you is None and not rivals:
        raise AuctionInsightsError("no_rows")
    rivals.sort(key=lambda r: (r.impression_share or 0.0), reverse=True)
    return AuctionInsights(you=you, competitors=tuple(rivals), period_label=period)

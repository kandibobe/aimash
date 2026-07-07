"""Период отчёта: пресеты (7/30/90 дней, MTD) и произвольный диапазон → даты для GAQL.

GAQL фильтрует `segments.date BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'`. Как и Google LAST_N_DAYS,
пресеты НЕ включают сегодняшний (неполный) день — верхняя граница — вчера. READ-ONLY, без секретов.
`today` инъектируется в тестах (детерминизм); в проде берётся date.today().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

PRESET_DAYS: dict[str, int] = {"7": 7, "30": 30, "90": 90}


@dataclass(frozen=True)
class Period:
    date_from: date
    date_to: date
    label: str
    # 2.7: КЛЮЧ пресета для локализации подписи (label выше остаётся RU — обратная совместимость
    # тестов/xlsx). kind: last_n | mtd | custom; prev=True — «предыдущий период» для сравнения.
    kind: str = "custom"
    n: int = 0
    prev: bool = False

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def gaql_between(self) -> str:
        """Фрагмент WHERE для GAQL. Даты — ISO, кавычки внутри; инъекция исключена (это date)."""
        return (
            f"segments.date BETWEEN '{self.date_from.isoformat()}' AND '{self.date_to.isoformat()}'"
        )

    def previous(self) -> "Period":
        """Предыдущий равный по длине период (для сравнения период-к-периоду)."""
        n = self.days
        prev_to = self.date_from - timedelta(days=1)
        prev_from = prev_to - timedelta(days=n - 1)
        return Period(prev_from, prev_to, f"{self.label} (пред.)", self.kind, self.n, True)


def label_i18n(p: Period, lang: str | None = None) -> str:
    """2.7: локализованная подпись периода. Раньше RU-метки («последние 30 дн.») протекали в
    EN-отчёты/advise (label зашивался при создании Period). RU → label как есть; EN — рендер по
    kind/n. Старые Period без kind (дефолт custom, label уже ISO-диапазон) деградируют честно."""
    if (lang or "ru") != "en":
        return p.label
    if p.kind == "last_n" and p.n > 0:
        base = f"last {p.n} days"
    elif p.kind == "mtd":
        base = "month to date"
    else:
        base = f"{p.date_from.isoformat()} — {p.date_to.isoformat()}"
    return f"{base} (prev.)" if p.prev else base


def last_n_days(n: int, *, today: date | None = None) -> Period:
    if n <= 0:
        raise ValueError("число дней должно быть > 0")
    today = today or date.today()
    end = today - timedelta(days=1)  # вчера (как Google LAST_N_DAYS — без неполного сегодня)
    start = end - timedelta(days=n - 1)
    return Period(start, end, f"последние {n} дн.", "last_n", n)


def month_to_date(*, today: date | None = None) -> Period:
    today = today or date.today()
    start = today.replace(day=1)
    end = today - timedelta(days=1)
    if end < start:  # сегодня — 1-е число: полных дней в месяце ещё нет
        end = start
    return Period(start, end, "с начала месяца", "mtd")


def custom(date_from: date, date_to: date) -> Period:
    if date_to < date_from:
        raise ValueError("date_to раньше date_from")
    return Period(date_from, date_to, f"{date_from.isoformat()} — {date_to.isoformat()}", "custom")


def from_preset(preset: str, *, today: date | None = None) -> Period:
    """'7'|'30'|'90'|'MTD' → Period. Используется bot-командами /report /export."""
    key = str(preset).strip()
    if key in PRESET_DAYS:
        return last_n_days(PRESET_DAYS[key], today=today)
    if key.upper() == "MTD":
        return month_to_date(today=today)
    raise ValueError(f"неизвестный пресет периода: {preset!r} (ожидалось 7/30/90/MTD)")


# ── C5 (аудит 2026-07): период из СВОБОДНОГО текста — считает КОД, не модель ──────
# Модель не знает «сегодня» и не считает арифметику дат (прецедент —
# agent.campaign_settings.parse_relative_dates): «вчера»/«за прошлую неделю»/«июнь» она
# резолвить не должна. Инструмент get_stats передаёт фразу периода как period_text,
# а даты вычисляет этот резолвер. Ничего не распознали → None (вызывающий откатится
# на period_days).
import re as _re  # noqa: E402 — используется только резолвером ниже

# RU-основы (генитив/номинатив покрываются префиксом) + EN-названия месяцев.
_MONTH_STEMS: list[tuple[str, int]] = [
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("ма", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
    ("january", 1),
    ("february", 2),
    ("march", 3),
    ("april", 4),
    ("may", 5),
    ("june", 6),
    ("july", 7),
    ("august", 8),
    ("september", 9),
    ("october", 10),
    ("november", 11),
    ("december", 12),
]  # «ма» ловит «мая/май»; порядок важен — более длинные основы просто не конфликтуют по regex ниже
_MONTH_RE = _re.compile(
    r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йея]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|"
    r"октябр\w*|ноябр\w*|декабр\w*|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    _re.IGNORECASE,
)
_ISO_RE = _re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DOTS_RE = _re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
_DAY_MONTH_RE = _re.compile(r"\b(\d{1,2})(?:-?го)?\s+(?=[а-яa-z])", _re.IGNORECASE)
_N_DAYS_RE = _re.compile(r"(?:за|последн\w*|last)\s+(\d{1,3})\s*(?:дн\w*|day)", _re.IGNORECASE)


def _month_num(word: str) -> int | None:
    w = word.lower()
    for stem, num in _MONTH_STEMS:
        if w.startswith(stem):
            return num
    return None


def _month_period(mnum: int, year_hint: int | None, today: date) -> Period:
    """Полный месяц; без года: будущий месяц ⇒ прошлый год, текущий ⇒ 1-е…вчера (месяц не полон)."""
    year = year_hint or today.year
    if year_hint is None and mnum > today.month:
        year = today.year - 1
    start = date(year, mnum, 1)
    end = (date(year + 1, 1, 1) if mnum == 12 else date(year, mnum + 1, 1)) - timedelta(days=1)
    if start <= today <= end:  # текущий месяц — до вчера (как MTD; без неполного сегодня)
        end = max(start, today - timedelta(days=1))
    return custom(start, min(end, today))


def parse_period_text(text: str | None, *, today: date | None = None) -> Period | None:
    """Свободная фраза периода → Period или None. Понимает (RU/EN): вчера/сегодня/позавчера,
    «за/последние N дней», «прошлая/эта неделя» (пн–вс / пн–вчера), «прошлый месяц», месяц по
    имени («июнь», «за июнь 2025»), пары дат «с 1.06 по 15.06» / ISO / «с 1 по 15 июня»."""
    t = (text or "").strip().lower()
    if not t:
        return None
    today = today or date.today()
    yesterday = today - timedelta(days=1)

    if _re.search(r"\bпозавчера\b|\bday before yesterday\b", t):
        d = today - timedelta(days=2)
        return custom(d, d)
    if _re.search(r"\bвчера\b|\byesterday\b", t):
        return custom(yesterday, yesterday)
    if _re.search(r"\bсегодня\b|\btoday\b", t):
        return custom(today, today)
    if _re.search(r"прошл\w*\s+недел|last\s+week", t):
        start = today - timedelta(days=today.weekday() + 7)  # пн прошлой недели
        return custom(start, start + timedelta(days=6))
    if _re.search(r"(эт\w*|текущ\w*)\s+недел|this\s+week", t):
        start = today - timedelta(days=today.weekday())  # пн этой недели
        return custom(start, max(start, yesterday))
    if _re.search(r"прошл\w*\s+месяц|last\s+month", t):
        first_this = today.replace(day=1)
        prev_end = first_this - timedelta(days=1)
        return custom(prev_end.replace(day=1), prev_end)
    m = _N_DAYS_RE.search(t)
    if m:
        return last_n_days(max(1, min(int(m.group(1)), 400)), today=today)

    # Пары явных дат: ISO / DD.MM(.YYYY) — первая=от, вторая=до.
    anchors: list[date] = []
    for mm in _ISO_RE.finditer(t):
        try:
            anchors.append(date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3))))
        except ValueError:
            continue
    if len(anchors) < 2:
        for mm in _DOTS_RE.finditer(t):
            dd, mo, yy = int(mm.group(1)), int(mm.group(2)), mm.group(3)
            try:
                year = (int(yy) if len(yy) == 4 else 2000 + int(yy)) if yy else today.year
                anchors.append(date(year, mo, dd))
            except ValueError:
                continue
    if len(anchors) >= 2:
        a, b = anchors[0], anchors[1]
        return custom(min(a, b), max(a, b))

    # «с 1 по 15 июня [2026]» — числа дней + один месяц; либо просто месяц («за июнь»).
    mon = _MONTH_RE.search(t)
    if mon:
        mnum = _month_num(mon.group(1))
        if mnum is not None:
            ym = _re.search(r"\b(20\d{2})\b", t)
            year_hint = int(ym.group(1)) if ym else None
            days_nums = [
                int(x) for x in _re.findall(r"\b(\d{1,2})\b(?!\.\d)", t) if 1 <= int(x) <= 31
            ]
            if len(days_nums) >= 2:
                base = _month_period(mnum, year_hint, today)
                try:
                    a = base.date_from.replace(day=min(days_nums[0], 31))
                    b = base.date_from.replace(day=min(days_nums[1], 31))
                    return custom(min(a, b), max(a, b))
                except ValueError:
                    return None
            return _month_period(mnum, year_hint, today)
    if len(anchors) == 1:  # одна явная дата = отчёт за день
        return custom(anchors[0], anchors[0])
    return None

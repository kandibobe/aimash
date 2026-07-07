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

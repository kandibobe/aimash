"""C5 (аудит 2026-07): период отчёта из СВОБОДНОГО текста — считает КОД, не модель.

reports.period.parse_period_text: «вчера»/«за прошлую неделю»/«июнь»/«с 1 по 15 июня»/пары дат.
today инъектируется — тесты детерминированы. Нераспознанное → None (вызывающий откатится на
period_days), никакого «угадывания» дат.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reports.period import parse_period_text  # noqa: E402

T = date(2026, 7, 7)  # вторник


def _iso(p):
    return (p.date_from.isoformat(), p.date_to.isoformat())


def test_yesterday_today_ru_en():
    assert _iso(parse_period_text("за вчера", today=T)) == ("2026-07-06", "2026-07-06")
    assert _iso(parse_period_text("yesterday", today=T)) == ("2026-07-06", "2026-07-06")
    assert _iso(parse_period_text("сегодня", today=T)) == ("2026-07-07", "2026-07-07")
    assert _iso(parse_period_text("позавчера", today=T)) == ("2026-07-05", "2026-07-05")


def test_last_week_is_full_mon_sun_before_today():
    p = parse_period_text("за прошлую неделю", today=T)
    assert p.days == 7
    assert p.date_from.weekday() == 0 and p.date_to.weekday() == 6  # пн—вс
    assert p.date_to < T
    assert _iso(p) == ("2026-06-29", "2026-07-05")


def test_this_week_up_to_yesterday():
    p = parse_period_text("на этой неделе", today=T)
    assert p.date_from == date(2026, 7, 6) and p.date_to == date(2026, 7, 6)  # пн…вчера


def test_last_month_full_calendar_month():
    assert _iso(parse_period_text("прошлый месяц", today=T)) == ("2026-06-01", "2026-06-30")


def test_month_by_name_past_current_future():
    # прошедший месяц этого года — целиком
    assert _iso(parse_period_text("за июнь", today=T)) == ("2026-06-01", "2026-06-30")
    # текущий месяц — с 1-го по вчера (без неполного сегодня)
    assert _iso(parse_period_text("июль", today=T)) == ("2026-07-01", "2026-07-06")
    # будущий месяц без года — прошлый год (отчёт о будущем не бывает)
    assert _iso(parse_period_text("август", today=T)) == ("2025-08-01", "2025-08-31")
    # явный год уважается
    assert _iso(parse_period_text("июнь 2025", today=T)) == ("2025-06-01", "2025-06-30")
    assert _iso(parse_period_text("june", today=T)) == ("2026-06-01", "2026-06-30")


def test_day_range_within_named_month():
    assert _iso(parse_period_text("с 1 по 15 июня", today=T)) == ("2026-06-01", "2026-06-15")


def test_single_day_within_named_month():
    """D6: «за 3 августа» — ОДИН день, а не весь август (раньше число дня молча отбрасывалось,
    и дневной отчёт подменялся месячным — расход в 30 раз больше, чем просил менеджер)."""
    assert _iso(parse_period_text("за 3 июня", today=T)) == ("2026-06-03", "2026-06-03")
    assert _iso(parse_period_text("отчёт за 3-го июня", today=T)) == ("2026-06-03", "2026-06-03")
    # будущий месяц без года → прошлый год, день внутри него
    assert _iso(parse_period_text("за 3 августа", today=T)) == ("2025-08-03", "2025-08-03")
    assert _iso(parse_period_text("august 3", today=T)) == ("2025-08-03", "2025-08-03")
    # явный год уважается и здесь
    assert _iso(parse_period_text("15 июня 2025", today=T)) == ("2025-06-15", "2025-06-15")


def test_stray_number_is_not_a_day():
    """Число НЕ рядом с месяцем днём не считается — «5 кампаний за июнь» остаётся месяцем."""
    assert _iso(parse_period_text("топ 5 кампаний за июнь", today=T)) == (
        "2026-06-01",
        "2026-06-30",
    )


def test_impossible_or_future_day_returns_none():
    assert parse_period_text("за 31 февраля", today=T) is None  # такой даты нет
    assert parse_period_text("за 10 июля", today=T) is None  # день ещё не наступил (today=7-е)


def test_future_month_returns_none_not_raises():
    """D6: «за август 2027» раньше падал ValueError (date_to < date_from) при контракте
    «не распознали → None»; bot исключение не ловит → пользователь видел «ошибка»."""
    assert parse_period_text("за август 2027", today=T) is None
    assert parse_period_text("с 1 по 15 августа 2027", today=T) is None


def test_explicit_date_pairs_dots_and_iso():
    assert _iso(parse_period_text("с 01.06 по 15.06", today=T)) == ("2026-06-01", "2026-06-15")
    assert _iso(parse_period_text("2026-06-01 2026-06-15", today=T)) == (
        "2026-06-01",
        "2026-06-15",
    )
    # перепутанный порядок нормализуется (from <= to)
    assert _iso(parse_period_text("с 15.06 по 01.06", today=T)) == ("2026-06-01", "2026-06-15")


def test_single_explicit_date_is_one_day():
    assert _iso(parse_period_text("за 03.06.2026", today=T)) == ("2026-06-03", "2026-06-03")


def test_last_n_days_phrase():
    p = parse_period_text("за 14 дней", today=T)
    assert p.days == 14 and p.date_to == T - timedelta(days=1)  # верх — вчера, как LAST_N_DAYS


def test_unparseable_returns_none():
    assert parse_period_text("фывапролдж", today=T) is None
    assert parse_period_text("", today=T) is None
    assert parse_period_text(None, today=T) is None

"""Ф5б: импорт CSV «Статистика аукционов» — парсер + карточка.

Файл приходит ИЗВНЕ и в модель не идёт (класс prompt-injection), поэтому вся строгость — в коде:
1. Схему узнаём по ЗАГОЛОВКАМ (EN и RU, любой порядок колонок), а не по номерам — перестановка
   колонок в выгрузке не должна молча перепутать «пересечение» с «опережением».
2. «--» → None и печатается прочерком. Ноль здесь — утверждение, которого в файле НЕТ (GR8):
   «0% показов у конкурента» и «Google промолчал» — разные факты.
3. Не наш файл → ОШИБКА с понятным текстом, а не «разберём как сможем».
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.texts import fmt_competitors  # noqa: E402
from db.competitors import Snapshot  # noqa: E402
from reports.auction_insights import (  # noqa: E402
    AuctionInsightsError,
    parse_auction_insights,
)

EN_CSV = (
    "Auction insights report\n"
    "Jan 1, 2026-Jan 31, 2026\n"
    "Display URL domain,Impr. share,Overlap rate,Position above rate,Top of page rate,"
    "Abs. Top of page rate,Outranking share\n"
    "You,41.09%,--,--,76.40%,23.50%,--\n"
    "rival.com,62.30%,48.10%,71.20%,88.00%,40.00%,58.90%\n"
    "small.io,< 10%,12.00%,30.00%,50.00%,10.00%,5.00%\n"
    "Total,--,--,--,--,--,--\n"
)

# RU-Excel: `;` + cp1251 + запятая как десятичный разделитель + другой порядок колонок.
RU_ROWS = (
    "Статистика аукционов\n"
    "1 янв. 2026 г.-31 янв. 2026 г.\n"
    "Домен отображаемого URL;Процент полученных показов;Коэффициент пересечения;"
    "Процент показов выше;Процент показов в верхней части страницы;"
    "Процент показов в самом верху страницы;Процент опережения\n"
    "Вы;41,09%;--;--;76,40%;23,50%;--\n"
    "rival.com;62,30%;48,10%;71,20%;88,00%;40,00%;58,90%\n"
)


def test_parses_english_export_and_keeps_missing_as_missing():
    ins = parse_auction_insights(EN_CSV.encode("utf-8-sig"))
    assert ins.you is not None and ins.you.is_you
    assert round(ins.you.impression_share, 4) == 0.4109
    # «--» у себя по пересечению/опережению: НЕ 0.0, а «нет данных».
    assert ins.you.overlap_rate is None and ins.you.outranking_share is None
    assert [c.domain for c in ins.competitors] == ["rival.com", "small.io"]  # по убыванию доли
    assert round(ins.competitors[0].position_above_rate, 3) == 0.712
    # «< 10%» → верхняя граница интервала (0.10): переоценить соседа безопаснее, чем недооценить.
    assert ins.competitors[1].impression_share == 0.10
    assert ins.period_label.startswith("Jan 1, 2026")


def test_parses_russian_excel_export_semicolon_cp1251_comma_decimal():
    ins = parse_auction_insights(RU_ROWS.encode("cp1251"))
    assert ins.you is not None and round(ins.you.impression_share, 4) == 0.4109
    assert round(ins.you.top_of_page_rate, 3) == 0.764
    # «самый верх» не должен съесть колонку обычного «верха страницы» (обе содержат "верх").
    assert round(ins.you.abs_top_of_page_rate, 3) == 0.235
    assert len(ins.competitors) == 1 and ins.competitors[0].domain == "rival.com"


def test_column_order_does_not_matter():
    """Колонки переставлены → значения обязаны поехать ЗА заголовками, а не остаться на местах."""
    csv = (
        "Outranking share,Display URL domain,Overlap rate,Impr. share\n"
        "58.90%,rival.com,48.10%,62.30%\n"
    )
    c = parse_auction_insights(csv.encode()).competitors[0]
    assert (
        round(c.impression_share, 3),
        round(c.overlap_rate, 3),
        round(c.outranking_share, 3),
    ) == (
        0.623,
        0.481,
        0.589,
    )


def test_foreign_file_is_rejected_not_guessed():
    """Отчёт по кампаниям (чужая схема) и мусор → AuctionInsightsError. Позиционного «догадаемся»
    здесь быть не должно: молча перепутанные колонки хуже честного отказа."""
    campaigns = "Campaign,Clicks,Impressions,Cost\nBrand,100,1000,50.00\n"
    for bad in (campaigns.encode(), b"\x00\x01\x02", b""):
        with pytest.raises(AuctionInsightsError):
            parse_auction_insights(bad)


def test_total_row_is_not_a_competitor():
    ins = parse_auction_insights(EN_CSV.encode())
    assert all(not c.domain.lower().startswith("total") for c in ins.competitors)


def test_card_shows_dash_for_missing_and_delta_in_points():
    """Карточка: «--» остаётся прочерком (не 0%), дельта к прошлому импорту — в ПУНКТАХ."""
    ins = parse_auction_insights(EN_CSV.encode())
    now = Snapshot("2026-07-13", ins.period_label, ins.you, ins.competitors)
    prev_rivals = tuple(
        type(c)(domain=c.domain, is_you=False, impression_share=(c.impression_share or 0) - 0.05)
        for c in ins.competitors
    )
    prev = Snapshot("2026-06-13", "Jun", ins.you, prev_rivals)

    card = fmt_competitors(now, prev, customer_id="123", lang="ru")
    assert "rival.com" in card and "62%" in card
    assert "▲5 п.п." in card  # 62.3% против 57.3% месяц назад
    assert "—" in card  # «--» из файла → прочерк, а НЕ «0%»
    assert "2026-06-13" in card  # с чем сравнивали — видно
    assert "🆕" not in card  # оба домена были и раньше

    # Новый домен появляется помеченным, ушедший — назван (иначе «пропал» = молча потерян).
    only_rival = Snapshot("2026-06-13", "Jun", ins.you, prev_rivals[:1])
    card2 = fmt_competitors(now, only_rival, customer_id="123", lang="ru")
    assert "🆕" in card2

    gone = Snapshot("2026-07-13", "", ins.you, ins.competitors[:1])
    card3 = fmt_competitors(gone, prev, customer_id="123", lang="ru")
    assert "Ушли из аукциона" in card3 and "small.io" in card3

    en = fmt_competitors(now, prev, customer_id="123", lang="en")
    assert "Auction insights" in en and "pp" in en


async def test_snapshot_overwrites_same_date_and_finds_previous():
    """Срез за дату перезаписывается ЦЕЛИКОМ, а не мержится: домены между выгрузками появляются и
    исчезают — мерж оставил бы «призраков», которых в новом отчёте Google уже нет."""
    from db.competitors import latest_snapshot, save_snapshot
    from db.session import init_db
    from reports.auction_insights import AuctionInsights, CompetitorRow

    await init_db()
    cid = "9999000111"

    def ins(*domains: str, share: float = 0.5) -> AuctionInsights:
        return AuctionInsights(
            you=CompetitorRow(domain="You", is_you=True, impression_share=0.4),
            competitors=tuple(
                CompetitorRow(domain=d, is_you=False, impression_share=share) for d in domains
            ),
            period_label="Jan",
        )

    await save_snapshot(cid, "2026-06-01", ins("old.com", "rival.com", share=0.3))
    await save_snapshot(cid, "2026-07-01", ins("rival.com", "new.io"))
    await save_snapshot(cid, "2026-07-01", ins("rival.com"))  # повтор той же даты → перезапись

    last = await latest_snapshot(cid)
    assert last is not None and last.snapshot_date == "2026-07-01"
    assert [c.domain for c in last.competitors] == ["rival.com"]  # new.io НЕ пережил перезапись
    assert last.you is not None and last.you.is_you

    prev = await latest_snapshot(cid, before_date="2026-07-01")
    assert prev is not None and prev.snapshot_date == "2026-06-01"
    assert {c.domain for c in prev.competitors} == {"old.com", "rival.com"}
    assert await latest_snapshot(cid, before_date="2026-01-01") is None  # раньше первого — нет


async def test_snapshot_series_returns_ascending_tail():
    """3.4: series — последние N срезов ПО ВОЗРАСТАНИЮ даты (хвост для мини-тренда карточки);
    лишние старые отброшены, пустая история → []."""
    from db.competitors import save_snapshot, snapshot_series
    from db.session import init_db
    from reports.auction_insights import AuctionInsights, CompetitorRow

    await init_db()
    cid = "9999000222"

    def ins(share_you: float) -> AuctionInsights:
        return AuctionInsights(
            you=CompetitorRow(domain="You", is_you=True, impression_share=share_you),
            competitors=(CompetitorRow(domain="rival.com", is_you=False, impression_share=0.5),),
            period_label="",
        )

    for d, share in [
        ("2026-03-01", 0.10),
        ("2026-04-01", 0.15),
        ("2026-05-01", 0.22),
        ("2026-06-01", 0.20),
        ("2026-07-01", 0.18),
    ]:
        await save_snapshot(cid, d, ins(share))

    tail = await snapshot_series(cid, limit=4)
    assert [s.snapshot_date for s in tail] == [
        "2026-04-01",
        "2026-05-01",
        "2026-06-01",
        "2026-07-01",
    ]  # март (старейший) отброшен, порядок — по возрастанию
    assert [round(s.you.impression_share, 2) for s in tail] == [0.15, 0.22, 0.20, 0.18]
    assert tail[-1].competitors[0].domain == "rival.com"
    assert await snapshot_series("0000000000", limit=4) == []  # импортов не было


def test_card_mini_trend_needs_three_you_points():
    """3.4: мини-тренд СВОЕЙ доли — от 3 точек (2 и так покрывает дельта prev); срез без «You»
    точкой не считается. Плюс честный футер: метрики в API есть, но за вайтлистом Google."""
    from reports.auction_insights import CompetitorRow

    def s(d: str, share: float | None) -> Snapshot:
        you = (
            CompetitorRow(domain="You", is_you=True, impression_share=share)
            if share is not None
            else None
        )
        return Snapshot(d, "", you, ())

    series3 = [s("2026-05-01", 0.22), s("2026-06-01", 0.20), s("2026-07-13", 0.18)]
    card = fmt_competitors(series3[-1], None, customer_id="123", lang="ru", series=series3)
    assert "📈 Твоя доля показов" in card
    assert "22% (05-01) → 20% (06-01) → 18% (07-13)" in card
    assert "вайтлистом Google" in card

    two = fmt_competitors(series3[-1], None, customer_id="123", lang="ru", series=series3[1:])
    assert "📈" not in two
    holey = [s("2026-05-01", 0.22), s("2026-06-01", None), s("2026-07-13", 0.18)]
    assert "📈" not in fmt_competitors(holey[-1], None, customer_id="123", lang="ru", series=holey)

    en = fmt_competitors(series3[-1], None, customer_id="123", lang="en", series=series3)
    assert "📈 Your impression share" in en

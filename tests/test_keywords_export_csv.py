"""ТЗ §7 «CSV/table export»: keywords.export.write_keywords_csv пишет плоский CSV (utf-8-sig)
с теми же данными, что .xlsx — группировка по кластеру, внутри по убыванию объёма. Офлайн."""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.keyword_plan import KeywordIdea  # noqa: E402
from keywords.cluster import Cluster  # noqa: E402
from keywords.export import HEADERS, write_keywords_csv  # noqa: E402


def test_csv_export_groups_and_orders_by_volume():
    ideas = [
        KeywordIdea("дёшево", avg_monthly_searches=100, competition="LOW", avg_cpc=1.5),
        KeywordIdea("купить", avg_monthly_searches=500, competition="HIGH", avg_cpc=3.0),
        KeywordIdea("что такое", avg_monthly_searches=50, competition="LOW"),
    ]
    clusters = [
        Cluster(name="Покупка", intent="транзакционный", keywords=["дёшево", "купить"]),
        Cluster(name="Инфо", intent="информационный", keywords=["что такое"]),
    ]
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "kw.csv")
        write_keywords_csv(clusters, ideas, path, seeds=["цветы"], language="ru")
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))

    assert rows[0] == HEADERS  # шапка
    # Внутри кластера «Покупка» — по убыванию объёма: «купить»(500) раньше «дёшево»(100).
    body = rows[1:]
    assert [r[2] for r in body] == ["купить", "дёшево", "что такое"]
    assert body[0][0] == "Покупка" and body[0][1] == "транзакционный"
    assert body[0][3] == "500"  # объём/мес
    assert body[2][0] == "Инфо"  # второй кластер — следом

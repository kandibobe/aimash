"""advisor улучшения: one-tap «применить» (#1), уведомление исхода (#2), LLM-минус-слова (#3).

КЛЮЧЕВОЙ инвариант (golden rule #3): one-tap НИКОГДА не применяет деньги/ставки — только
pause_campaign / add_negative_keywords. Деньги — исключены из _ADVISE_APPLY_OPS и из клавиатуры.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from reports.queries import Breakdown, Metrics  # noqa: E402


def _m(cost, *, clicks=10, conv=0.0, impressions=100):
    return Metrics(
        impressions=impressions,
        clicks=clicks,
        cost_micros=int(cost * 1_000_000),
        conversions=conv,
        conv_value=0.0,
    )


# ── #1: one-tap apply — деньги НИКОГДА не применяются в один тап (golden rule #3) ────
def _report_with_keywords(kw_rows):
    kb = Breakdown("keyword", "Ключи", ["К", "Г", "Ключ", "Тип"], kw_rows)
    return SimpleNamespace(breakdowns=[kb])


def test_keyword_texts_sorted_by_cost():
    from advisor import service

    rows = [
        (("C", "AG", "дешёвый", "BROAD"), _m(5)),
        (("C", "AG", "дорогой", "BROAD"), _m(50)),
    ]
    texts = service._keyword_texts(_report_with_keywords(rows))
    assert texts[0] == "дорогой"  # самые дорогие первыми


async def test_negatives_extra_advisory(monkeypatch):
    import keywords.cluster as kc
    from advisor import service

    seen: dict = {}

    async def _fake_neg(topic, ideas, *, language="ru", limit=20, protected=frozenset()):
        seen["protected"] = protected
        return ["бесплатно", "скачать"]

    monkeypatch.setattr(kc, "suggest_negative_keywords", _fake_neg)
    rep = _report_with_keywords([(("C", "AG", "kw", "BROAD"), _m(10))])
    extra = await service._negative_keywords_extra(rep, ["keywords"], "ru", protected={"бренд"})
    assert extra and "бесплатно" in extra[0] and "минус-слова" in extra[0]
    assert seen["protected"] == {"бренд"}  # брендозащита §20 доходит до подбора минус-слов
    # тема не keywords → пусто
    assert await service._negative_keywords_extra(rep, ["optimize"], "ru") == []
    # нет ключей → пусто
    assert await service._negative_keywords_extra(_report_with_keywords([]), None, "ru") == []

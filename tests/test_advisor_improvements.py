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
def test_advise_apply_ops_exclude_money():
    import bot.main as bm

    assert "pause_campaign" in bm._ADVISE_APPLY_OPS
    assert "add_negative_keywords" in bm._ADVISE_APPLY_OPS
    # деньги/ставки/структура — НЕ one-tap
    assert "update_budget" not in bm._ADVISE_APPLY_OPS
    assert "update_bid" not in bm._ADVISE_APPLY_OPS
    assert "create_search_campaign" not in bm._ADVISE_APPLY_OPS


def test_advise_apply_params_builds_correctly():
    import bot.main as bm

    pause = SimpleNamespace(
        suggested_operation="pause_campaign", target_campaign="Camp", evidence={}
    )
    assert bm._advise_apply_params(pause) == {"campaign": "Camp"}

    neg = SimpleNamespace(
        suggested_operation="add_negative_keywords",
        target_campaign="Camp",
        evidence={"keyword": "бесплатно"},
    )
    assert bm._advise_apply_params(neg) == {
        "campaign": "Camp",
        "keywords": ["бесплатно"],
        "match_type": "broad",
    }
    # нет кампании / нет ключа → None (не минтим кривой proposal)
    assert (
        bm._advise_apply_params(
            SimpleNamespace(suggested_operation="pause_campaign", target_campaign=None, evidence={})
        )
        is None
    )
    assert (
        bm._advise_apply_params(
            SimpleNamespace(
                suggested_operation="add_negative_keywords", target_campaign="C", evidence={}
            )
        )
        is None
    )


def _apply_button_present(markup) -> bool:
    for row in markup.inline_keyboard:
        for btn in row:
            if ":apply:" in (btn.callback_data or ""):
                return True
    return False


def test_apply_button_only_for_nonmoney():
    from bot.keyboards import advise_feedback_kb

    # деньги → метки нет → кнопки «применить» нет
    assert not _apply_button_present(advise_feedback_kb("uid", "ru", apply_op="update_budget"))
    assert not _apply_button_present(advise_feedback_kb("uid", "ru", apply_op="update_bid"))
    assert not _apply_button_present(advise_feedback_kb("uid", "ru", apply_op=None))
    # не-денежные → кнопка есть
    assert _apply_button_present(advise_feedback_kb("uid", "ru", apply_op="pause_campaign"))
    assert _apply_button_present(advise_feedback_kb("uid", "ru", apply_op="add_negative_keywords"))


# ── #3: LLM-минус-слова (тема keywords) — advisory довесок ───────────────────────────
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

    async def _fake_neg(topic, ideas, *, language="ru", limit=20):
        return ["бесплатно", "скачать"]

    monkeypatch.setattr(kc, "suggest_negative_keywords", _fake_neg)
    rep = _report_with_keywords([(("C", "AG", "kw", "BROAD"), _m(10))])
    extra = await service._negative_keywords_extra(rep, ["keywords"], "ru")
    assert extra and "бесплатно" in extra[0] and "минус-слова" in extra[0]
    # тема не keywords → пусто
    assert await service._negative_keywords_extra(rep, ["optimize"], "ru") == []
    # нет ключей → пусто
    assert await service._negative_keywords_extra(_report_with_keywords([]), None, "ru") == []

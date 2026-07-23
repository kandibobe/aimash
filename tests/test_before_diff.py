"""Реальное «было → станет» (§5) + оптимистичная блокировка дрейфа (TOCTOU).

Закрывает находку аудита: черновик показывал только «станет» (плейсхолдер «[текущее значение]»),
а §5 требует «было → станет». Теперь ads.service.read_before кладёт снимок в params['_before'],
texts.fmt_mutation_summary рисует настоящий diff, а execute_confirmed сверяет дрейф.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.service import _assert_no_drift  # noqa: E402
from core import texts  # noqa: E402


# ── Рендер реального «было → станет» ─────────────────────────────────────────────
def test_budget_percent_shows_real_before_after():
    s = texts.fmt_mutation_summary(
        "update_budget",
        {
            "campaign": "Search",
            "mode": "increase_by_percent",
            "value": 20,
            "currency": "percent",
            "_before": {"kind": "budget", "before_micros": 40_000_000, "after_micros": 48_000_000},
        },
    )
    assert "40.00 → 48.00" in s  # реальное «было → станет», не только «+20%»
    assert "(+20%)" in s


def test_budget_set_to_shows_before_arrow_after():
    s = texts.fmt_mutation_summary(
        "update_budget",
        {
            "campaign": "X",
            "mode": "set_to",
            "value": 50,
            "currency": "USD",
            "_before": {"kind": "budget", "before_micros": 40_000_000, "after_micros": 50_000_000},
        },
    )
    assert "40.00 → 50.00" in s


def test_bid_percent_shows_range_transition():
    s = texts.fmt_mutation_summary(
        "update_bid",
        {
            "campaign": "X",
            "mode": "increase_by_percent",
            "value": 10,
            "currency": "USD",
            "_before": {
                "kind": "bid",
                "before_micros": [1_000_000, 2_000_000],
                "after_micros": [1_100_000, 2_200_000],
                "n_groups": 2,
            },
        },
    )
    assert "1.00–2.00 → 1.10–2.20" in s
    assert "групп: 2" in s


def _kw_bid_params(**over):
    p = {
        "campaign": "X",
        "keyword": "ремонт окон",
        "match_type": "phrase",
        "mode": "increase_by_percent",
        "value": 20,
        "_before": {
            "kind": "keyword_bid",
            "before_micros": [500_000],
            "after_micros": [600_000],
            "n_keywords": 1,
            "keyword": "ремонт окон",
            "ad_groups": ["Окна"],
            "currency": "USD",
        },
    }
    p.update(over)
    return p


def test_keyword_bid_card_names_the_keyword_and_group():
    """Ф1: карточка ставки КЛЮЧА обязана называть сам ключ и группы — пользователь подтверждает
    подорожание конкретного слова, а не «ставки кампании»."""
    s = texts.fmt_mutation_summary("update_keyword_bid", _kw_bid_params())
    assert "ремонт окон" in s and "фразовое" in s
    assert "+20% (0.50 → 0.60)" in s
    assert "Окна" in s and "групп" in s


def test_keyword_bid_card_en():
    s = texts.fmt_mutation_summary("update_keyword_bid", _kw_bid_params(), "en")
    assert "ремонт окон" in s and "CPC bid" in s and "0.50 → 0.60" in s


def test_keyword_bid_card_decrease_sign_is_minus():
    s = texts.fmt_mutation_summary(
        "update_keyword_bid",
        _kw_bid_params(
            mode="decrease_by_percent",
            value=20,
            _before={
                "kind": "keyword_bid",
                "before_micros": [500_000],
                "after_micros": [400_000],
                "n_keywords": 1,
                "ad_groups": ["Окна"],
                "currency": "USD",
            },
        ),
    )
    assert "−20%" in s and "0.50 → 0.40" in s  # U+2212, не дефис


def test_keyword_bid_card_fallback_without_before():
    s = texts.fmt_mutation_summary(
        "update_keyword_bid",
        {"campaign": "X", "keyword": "ремонт окон", "mode": "set_to", "value": 1.5},
    )
    assert "ремонт окон" in s and "1.5" in s


def test_pause_shows_status_transition():
    s = texts.fmt_mutation_summary(
        "pause_campaign",
        {"campaign": "X", "_before": {"kind": "status", "before_status": "ENABLED"}},
    )
    assert "→" in s and "паузе" in s and "включена" in s


def test_fallback_without_before_keeps_old_format():
    # старый черновик/неудачное чтение — сводка без «было», как раньше
    s = texts.fmt_mutation_summary(
        "update_budget", {"campaign": "X", "mode": "increase_by_percent", "value": 20}
    )
    assert "+20%" in s and "→" not in s


# ── Оптимистичная блокировка дрейфа (TOCTOU) ─────────────────────────────────────
def test_drift_budget_relative_raises_on_change():
    params = {
        "mode": "increase_by_percent",
        "_before": {"kind": "budget", "before_micros": 40_000_000},
    }
    with pytest.raises(ValueError, match="изменил"):
        _assert_no_drift(params, 45_000_000)  # база изменилась → блок


def test_drift_budget_relative_passes_when_unchanged():
    params = {
        "mode": "increase_by_amount",
        "_before": {"kind": "budget", "before_micros": 40_000_000},
    }
    _assert_no_drift(params, 40_000_000)  # без изменения — не бросает


def test_drift_set_to_never_blocks():
    params = {"mode": "set_to", "_before": {"kind": "budget", "before_micros": 40_000_000}}
    _assert_no_drift(params, 999_000_000)  # абсолют — база не важна, не блокируем


def test_drift_bid_list_raises_on_change():
    params = {
        "mode": "increase_by_percent",
        "_before": {"kind": "bid", "before_micros": [1_000_000, 2_000_000]},
    }
    with pytest.raises(ValueError, match="ставки"):
        _assert_no_drift(params, [1_000_000, 2_500_000])


def test_drift_keyword_bid_list_raises_and_names_keywords():
    params = {
        "mode": "increase_by_percent",
        "_before": {"kind": "keyword_bid", "before_micros": [500_000]},
    }
    with pytest.raises(ValueError, match="ставки ключей"):
        _assert_no_drift(params, [700_000])  # кто-то поднял ставку ключа после показа карточки


def test_drift_no_snapshot_passes():
    # старый черновик без _before — сверять нечего (обратная совместимость)
    _assert_no_drift({"mode": "increase_by_percent"}, 40_000_000)

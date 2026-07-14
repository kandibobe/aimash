"""§5: УМЕНЬШЕНИЕ бюджета/ставки — направление несёт mode, а не знак value.

Закрывает находку аудита: схема знала только increase_*/set_to. «Снизь бюджет на 20%» → либо
ValidationError (команда не проходила вовсе), либо модель подставляла increase_by_percent → карточка
обещала «100 → 120 (+20%)», т.е. РОВНО НАОБОРОТ на денежном пути. Проверяем весь путь:
- схема принимает decrease_by_percent/decrease_by_amount и держит value > 0 (знака у суммы нет);
- уменьшение на ≥100% отвергается схемой (ноль/минус — не «обнуление», а бессмыслица);
- compute_new_micros считает уменьшение и кратно биллинг-единице;
- невыполнимое уменьшение («на 200» при бюджете 100) → DecreaseBelowZero ДО кнопок ✅
  (read_before пробрасывает, а не проглатывает как сбой чтения — иначе claim сгорел бы после «да»);
- карточка печатает «−», а не «+» (перепутанный знак = та самая ошибка, из-за которой всё затевалось);
- currency_mismatch не блокирует процентные режимы (в т.ч. decrease_by_percent).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads import service  # noqa: E402
from ads.resolve import DecreaseBelowZero, compute_new_micros, currency_mismatch  # noqa: E402
from agent.tools.schemas import UpdateBid, UpdateBudget  # noqa: E402
from bot.texts import fmt_mutation_summary  # noqa: E402
from core.limits import BILLING_UNIT_MICROS  # noqa: E402


# ── Схема: направление в mode, value всегда > 0 ──────────────────────────────────
def test_schema_accepts_decrease_modes():
    b = UpdateBudget(campaign="Лето", mode="decrease_by_percent", value=20)
    assert b.mode == "decrease_by_percent" and b.value == 20
    assert UpdateBudget(campaign="Лето", mode="decrease_by_amount", value=50).value == 50
    assert UpdateBid(campaign="Лето", mode="decrease_by_percent", value=15).value == 15


def test_schema_rejects_negative_value_in_any_mode():
    # Отрицательной суммы не бывает: «-20» модель отдать не должна, а если отдаст — отказ ДО кнопок.
    for mode in ("increase_by_percent", "decrease_by_percent", "decrease_by_amount", "set_to"):
        with pytest.raises(ValidationError):
            UpdateBudget(campaign="X", mode=mode, value=-20)
        with pytest.raises(ValidationError):
            UpdateBudget(campaign="X", mode=mode, value=0)


def test_schema_rejects_decrease_by_100_percent_or_more():
    # «Снизь на 100%» = ноль. Бюджет/ставка нулевыми не бывают: это пауза или set_to, не decrease.
    for v in (100, 120, 999):
        with pytest.raises(ValidationError):
            UpdateBudget(campaign="X", mode="decrease_by_percent", value=v)
        with pytest.raises(ValidationError):
            UpdateBid(campaign="X", mode="decrease_by_percent", value=v)
    UpdateBudget(campaign="X", mode="decrease_by_percent", value=99.9)  # 99.9% — законно


def test_schema_rejects_percent_currency_with_decrease_by_amount():
    # decrease_by_amount — АБСОЛЮТНАЯ сумма: currency='percent' умножился бы на 1e6 как деньги.
    with pytest.raises(ValidationError):
        UpdateBudget(campaign="X", mode="decrease_by_amount", value=10, currency="percent")


def test_schema_bid_has_no_amount_modes():
    # У ставки абсолютных дельт нет (ни increase_by_amount, ни decrease_by_amount) — только % и set_to.
    for mode in ("increase_by_amount", "decrease_by_amount"):
        with pytest.raises(ValidationError):
            UpdateBid(campaign="X", mode=mode, value=1)


# ── compute_new_micros: уменьшение ───────────────────────────────────────────────
def test_compute_decrease_by_percent():
    assert compute_new_micros(1_000_000, "decrease_by_percent", 20, currency=None) == 800_000
    assert compute_new_micros(5_000_000, "decrease_by_percent", 50, currency=None) == 2_500_000


def test_compute_decrease_by_amount():
    assert compute_new_micros(10_000_000, "decrease_by_amount", 2.5, currency=None) == 7_500_000


def test_compute_decrease_is_billing_unit_multiple():
    for base in (5_550_000, 1_000_000, 333_000, 7_777_777):
        for mode, value in (
            ("decrease_by_percent", 7),
            ("decrease_by_percent", 33.33),
            ("decrease_by_amount", 0.007),
        ):
            out = compute_new_micros(base, mode, value, currency=None)
            assert out % BILLING_UNIT_MICROS == 0 and out > 0, f"{base}/{mode}/{value} → {out}"


def test_compute_decrease_below_zero_raises_not_clamps():
    """«Снизь бюджет на 200» при бюджете 100 — не «поставь 0.01», а невыполнимая команда.
    Тихий клэмп к минимальной единице отдал бы менеджеру то, чего он не просил."""
    with pytest.raises(DecreaseBelowZero):
        compute_new_micros(100_000_000, "decrease_by_amount", 200, currency=None)
    with pytest.raises(DecreaseBelowZero):
        compute_new_micros(1_000_000, "decrease_by_percent", 100, currency=None)  # ровно в ноль
    assert issubclass(DecreaseBelowZero, ValueError)  # вызывающие ловят ValueError — не сломали


def test_decrease_below_zero_message_is_actionable_and_clean():
    with pytest.raises(DecreaseBelowZero) as e:
        compute_new_micros(100_000_000, "decrease_by_amount", 200, currency=None)
    msg = str(e.value)
    assert "100" in msg  # текущее значение названо
    assert "пауза" in msg.lower() or "паузу" in msg.lower()  # назван выход
    assert "micros" not in msg.lower()  # без внутренних единиц — текст для менеджера


# ── read_before: отказ ДО кнопок ✅ (не после «да») ────────────────────────────────
def _patched_service(monkeypatch, *, budget_micros: int, raises: Exception | None = None):
    async def _client(_cid):
        return object()

    def _find(_client, _cid, _name):
        if raises is not None:
            raise raises
        # CampaignRef-контракт: B добавил поля общего бюджета (дефолты — не общий). Фейк обязан их
        # отдавать, иначе read_before упрётся в AttributeError и деградирует до «без было».
        return SimpleNamespace(
            id="1",
            budget_micros=budget_micros,
            budget_explicitly_shared=False,
            budget_reference_count=0,
            budget_resource="customers/1/campaignBudgets/1",
        )

    async def _cur(_client, _cid):
        return "USD"

    monkeypatch.setattr(service, "build_client_async", _client)
    monkeypatch.setattr(service.resolve, "find_campaign_by_name", _find)
    monkeypatch.setattr(service, "_currency", _cur)


def test_read_before_propagates_decrease_below_zero(monkeypatch):
    """Невыполнимое уменьшение НЕ проглатывается общим `except Exception → None`: иначе карточка
    показалась бы без «было», а падение случилось бы уже ПОСЛЕ «да» — claim сожжён, денег не тронуто,
    но пользователь получил бы сырую ошибку SDK вместо честного «так нельзя»."""
    _patched_service(monkeypatch, budget_micros=100_000_000)  # текущий бюджет 100
    with pytest.raises(DecreaseBelowZero):
        asyncio.run(
            service.read_before(
                "update_budget", {"campaign": "X", "mode": "decrease_by_amount", "value": 200}
            )
        )


def test_read_before_still_swallows_read_failures(monkeypatch):
    # Сбой чтения (сеть/SDK) по-прежнему деградирует до «без было», а не роняет черновик.
    _patched_service(monkeypatch, budget_micros=0, raises=RuntimeError("gRPC unavailable"))
    assert (
        asyncio.run(
            service.read_before(
                "update_budget", {"campaign": "X", "mode": "decrease_by_percent", "value": 20}
            )
        )
        is None
    )


def test_read_before_ok_for_valid_decrease(monkeypatch):
    _patched_service(monkeypatch, budget_micros=100_000_000)
    out = asyncio.run(
        service.read_before(
            "update_budget", {"campaign": "X", "mode": "decrease_by_percent", "value": 20}
        )
    )
    assert out == {
        "kind": "budget",
        "before_micros": 100_000_000,
        "after_micros": 80_000_000,
        "currency": "USD",
    }


# ── Карточка: знак «−», а не «+» ─────────────────────────────────────────────────
def _budget_params(mode, value, before=None, after=None):
    p = {"campaign": "Лето", "mode": mode, "value": value}
    if before is not None:
        p["_before"] = {
            "kind": "budget",
            "before_micros": before,
            "after_micros": after,
            "currency": "USD",
        }
    return p


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_summary_decrease_shows_minus_sign(lang):
    s = fmt_mutation_summary(
        "update_budget",
        _budget_params("decrease_by_percent", 20, before=100_000_000, after=80_000_000),
        lang,
    )
    assert "−20%" in s and "+20%" not in s  # U+2212, направление видно с первого взгляда
    assert "100" in s and "80" in s


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_summary_increase_still_plus(lang):
    s = fmt_mutation_summary(
        "update_budget",
        _budget_params("increase_by_percent", 20, before=100_000_000, after=120_000_000),
        lang,
    )
    assert "+20%" in s and "−20%" not in s


def test_summary_decrease_by_amount_and_bid():
    s = fmt_mutation_summary("update_budget", _budget_params("decrease_by_amount", 50), "ru")
    assert "−50" in s
    bid = {
        "campaign": "Лето",
        "mode": "decrease_by_percent",
        "value": 15,
        "_before": {
            "kind": "bid",
            "before_micros": [1_000_000],
            "after_micros": [850_000],
            "n_groups": 1,
            "currency": "USD",
        },
    }
    assert "−15%" in fmt_mutation_summary("update_bid", bid, "ru")
    assert "−15%" in fmt_mutation_summary("update_bid", bid, "en")


# ── Сверка валюты: процентные режимы не блокируются ──────────────────────────────
def test_currency_mismatch_ignores_decrease_by_percent():
    # Процент безразмерен: «снизь на 20%» с currency='USD' на UAH-аккаунте — не расхождение валют.
    assert (
        currency_mismatch(
            "update_budget",
            {"campaign": "X", "mode": "decrease_by_percent", "value": 20, "currency": "USD"},
            "UAH",
        )
        is None
    )


def test_currency_mismatch_still_blocks_decrease_by_amount():
    # А вот абсолютная сумма в чужой валюте — расхождение (FX мы не делаем).
    msg = currency_mismatch(
        "update_budget",
        {"campaign": "X", "mode": "decrease_by_amount", "value": 50, "currency": "USD"},
        "UAH",
    )
    assert msg and "грн" in msg

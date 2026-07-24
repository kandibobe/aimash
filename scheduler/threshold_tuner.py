"""2.11 (§14): авто-подстройка порогов аномалий — ЧИСТЫЕ формулы (без сети/SDK/БД).

Считает волатильность аккаунта по дневным строкам (fetch_by_day) → недельные корзины → CV
(coefficient of variation) → ПРЕДЛОЖЕНИЕ порогов. НИЧЕГО не пишет: предложение показывает
scheduler.run_threshold_tuning, а записывает bm._save_per_account_thresholds ТОЛЬКО по тапу
человека («✅ Принять») — это настройка БОТА (alert_thresholds.per_account), не мутация Ads
(golden rules #1/#3 не затрагиваются). Образец чистоты — scheduler/anomaly.py.
"""

from __future__ import annotations

from statistics import mean, pstdev

# Окно анализа и минимум данных: 12 недель истории, но не меньше 4 ПОЛНЫХ недель — иначе CV шумит.
TRAILING_DAYS = 84
MIN_WEEKS = 4
# Материальность: предлагаем, только если хотя бы одно значение отличается от текущего ≥ чем на 25%.
MATERIALITY = 0.25
# Клампы предложений — согласованы с UI-валидатором /alerts (bm._alert_value_ok).
SPIKE_RANGE = (30.0, 300.0)
DROP_RANGE = (30.0, 90.0)
MIN_SPEND_RANGE = (1.0, 10_000.0)


def weekly_buckets(day_rows: list) -> tuple[list[float], list[float]]:
    """Дневные строки fetch_by_day → недельные суммы (cost, conversions), только ПОЛНЫЕ недели
    (хвост < 7 дней отбрасывается). day_rows: [((date_str,), Metrics), …] в хронологическом
    порядке (как отдаёт разбивка по дням). Пусто/мало → ([], [])."""
    days: list[tuple[float, float]] = []
    for _dims, m in day_rows or []:
        cost = float(getattr(m, "cost_micros", 0) or 0) / 1_000_000
        conv = float(getattr(m, "conversions", 0.0) or 0.0)
        days.append((cost, conv))
    n_full = len(days) // 7
    if n_full < 1:
        return [], []
    costs: list[float] = []
    convs: list[float] = []
    for w in range(n_full):
        chunk = days[w * 7 : (w + 1) * 7]
        costs.append(sum(c for c, _ in chunk))
        convs.append(sum(v for _, v in chunk))
    return costs, convs


def volatility(xs: list[float]) -> tuple[float, float, float]:
    """(mean, stddev, cv). Пусто/нулевое среднее → (0, 0, 0): CV не определён — не предлагаем."""
    if not xs:
        return 0.0, 0.0, 0.0
    mu = mean(xs)
    if mu <= 0:
        return mu, 0.0, 0.0
    sd = pstdev(xs)
    return mu, sd, sd / mu


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round5(v: float) -> float:
    return float(int(round(v / 5.0)) * 5)


def suggest_thresholds(
    weekly_costs: list[float], weekly_convs: list[float], current: dict | None
) -> dict | None:
    """Предложение per-account порогов по волатильности (≈2σ недельной динамики):
    spend_spike_pct ≈ 200·CV(cost), conv_drop_pct ≈ 200·CV(conv), min_spend ≈ 10% средненедельного
    расхода. Всё детерминированно и в клампах UI. None — данных мало (< MIN_WEEKS полных недель /
    нулевой расход) ЛИБО отличие от current НЕматериально (< MATERIALITY) — анти-шум."""
    if len(weekly_costs) < MIN_WEEKS:
        return None
    mu_cost, _sd_c, cv_cost = volatility(weekly_costs)
    _mu_conv, _sd_v, cv_conv = volatility(weekly_convs)
    if mu_cost <= 0:
        return None  # аккаунт не тратит — тюнить нечего
    spike = _clamp(_round5(200.0 * cv_cost), *SPIKE_RANGE)
    drop = _clamp(_round5(200.0 * cv_conv), *DROP_RANGE) if cv_conv > 0 else DROP_RANGE[0]
    min_spend = _clamp(round(0.1 * mu_cost, 2), *MIN_SPEND_RANGE)
    suggestion = {"spend_spike_pct": spike, "conv_drop_pct": drop, "min_spend": min_spend}

    cur = current or {}

    def _material(key: str, new: float) -> bool:
        old = cur.get(key)
        if old is None or float(old) <= 0:
            return True
        return abs(new - float(old)) / float(old) >= MATERIALITY

    if not any(_material(k, v) for k, v in suggestion.items()):
        return None  # почти совпадает с текущими — не спамим предложением
    return suggestion

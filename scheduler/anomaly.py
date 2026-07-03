"""Детектор аномалий по метрикам аккаунта. ЧИСТАЯ логика (без SDK/сети) — полностью тестируема.

Сравнивает текущий период с предыдущим равным (week-over-week). Это только СИГНАЛ:
планировщик лишь уведомляет, НИКОГДА не меняет аккаунт (golden rule #3). Пороги
по умолчанию переопределяются per-chat через UserSettings.alert_thresholds (JSON).
"""

from __future__ import annotations

from dataclasses import dataclass

# Пороги по умолчанию (можно переопределить через UserSettings.alert_thresholds).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "spend_spike_pct": 50.0,  # расход вырос на >= X% к пред. периоду → алерт
    "conv_drop_pct": 50.0,  # конверсии упали на >= X% → алерт
    "min_spend": 1.0,  # игнорировать шум при копеечном расходе (в валюте аккаунта)
}


@dataclass
class Alert:
    kind: str  # "spend_spike" | "conv_drop" | "spend_no_conv"
    severity: str  # "warning" | "info"
    message: str  # человекочитаемо (RU), без секретов


def _pct_change(now: float, prev: float) -> float | None:
    """Процент изменения; None если базы нет (prev<=0) — деление на ноль не делаем."""
    if prev <= 0:
        return None
    return (now - prev) / prev * 100.0


def detect_anomalies(
    current, previous, thresholds: dict | None = None, *, currency: str = ""
) -> list[Alert]:
    """current/previous — объекты с полями .cost и .conversions (reports.queries.Metrics или
    любой namespace). Возвращает список аномалий (пустой = всё в норме). currency (3H) — код
    валюты аккаунта в суммах сообщения: без него «1000 → 2000» неоднозначно для
    мульти-валютного портфеля MCC."""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    cur = f" {currency}" if currency else ""
    alerts: list[Alert] = []
    cost_now, cost_prev = float(current.cost), float(previous.cost)
    conv_now, conv_prev = float(current.conversions), float(previous.conversions)

    # Не шумим на околонулевом расходе в обоих периодах.
    if cost_now < t["min_spend"] and cost_prev < t["min_spend"]:
        return alerts

    ch = _pct_change(cost_now, cost_prev)
    if ch is not None and ch >= t["spend_spike_pct"]:
        alerts.append(
            Alert(
                "spend_spike",
                "warning",
                f"📈 Расход вырос на {ch:+.0f}% ({cost_prev:.2f} → {cost_now:.2f}{cur}).",
            )
        )

    dr = _pct_change(conv_now, conv_prev)
    if dr is not None and dr <= -t["conv_drop_pct"]:
        alerts.append(
            Alert(
                "conv_drop",
                "warning",
                f"📉 Конверсии упали на {dr:+.0f}% ({conv_prev:.1f} → {conv_now:.1f}).",
            )
        )

    # Расход есть, конверсий нет, а раньше были — отдельный явный сигнал.
    if cost_now >= t["min_spend"] and conv_now == 0 and conv_prev > 0:
        alerts.append(
            Alert(
                "spend_no_conv",
                "warning",
                f"⚠️ Расход {cost_now:.2f}{cur} при нуле конверсий (было {conv_prev:.1f}).",
            )
        )

    return alerts

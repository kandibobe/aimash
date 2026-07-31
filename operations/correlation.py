"""Correlate performance shifts with internal audit rows and Google ``change_event`` history."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class TimelineEvent:
    occurred_at: datetime
    source: str
    kind: str
    resource: str
    actor: str = ""
    fields: tuple[str, ...] = ()
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class CorrelatedCause:
    event: TimelineEvent
    hours_before_shift: float
    score: float
    explanation: str


_IMPACT = {
    "budget": 1.0,
    "bidding": 1.0,
    "conversion": 1.0,
    "targeting": 0.9,
    "negative": 0.85,
    "status": 0.8,
    "creative": 0.65,
    "asset": 0.55,
    "other": 0.35,
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _impact(kind: str, fields: Iterable[str]) -> float:
    hay = " ".join((kind, *fields)).casefold()
    for key, weight in _IMPACT.items():
        if key in hay:
            return weight
    return _IMPACT["other"]


def correlate_changes(
    shift_at: datetime,
    events: Iterable[TimelineEvent],
    *,
    lookback_hours: int = 168,
    top_n: int = 5,
) -> list[CorrelatedCause]:
    """Rank candidates by temporal proximity and deterministic impact class.

    This is correlation, not causal proof; the explanation says so explicitly.
    """
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    shift = _aware(shift_at)
    out: list[CorrelatedCause] = []
    for event in events:
        hours = (shift - _aware(event.occurred_at)).total_seconds() / 3600
        if hours < 0 or hours > lookback_hours:
            continue
        recency = math.exp(-hours / max(12.0, lookback_hours / 4))
        score = round(recency * _impact(event.kind, event.fields), 4)
        out.append(
            CorrelatedCause(
                event=event,
                hours_before_shift=round(hours, 2),
                score=score,
                explanation=(
                    f"{event.source} {event.kind} occurred {hours:.1f}h before the shift; "
                    "this is a ranked correlation, not proof of causation."
                ),
            )
        )
    return sorted(out, key=lambda item: (-item.score, item.hours_before_shift))[: max(0, top_n)]

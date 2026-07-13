"""Ф1: слой ставок и позиций — «какие слова поднять». Чистый КОД, БЕЗ сети/SDK.

Принимает уже прочитанные строки (reports.queries.BidLandscapeRow — duck-typed) и возвращает
ранжированные ВОЗМОЖНОСТИ: «ставка ключа ниже оценки Google → подними до X». Один источник и для
проверок аудита (audit.engine), и для будущей команды /bids — пороги и формулы не должны разъехаться.

🔒 Это ТОЛЬКО расчёт. Модуль не создаёт proposal и ничего не меняет: повышение ставки — деньги, а
значит только прямая команда пользователя через confirm-гейт (golden rule #3), НИКОГДА не one-tap
и никогда из scheduler.

Оценки Google (position_estimates) даются в валюте аккаунта и живут только там, где ставку решает
РЕКЛАМОДАТЕЛЬ: на Smart Bidding (tCPA/tROAS/Maximize*) ставку ключа Google игнорирует, поэтому совет
«подними cpc_bid» там был бы ложью — такие кампании отсекаем (MANUAL_BID_STRATEGIES).
"""

from __future__ import annotations

from dataclasses import dataclass

# Стратегии, где ставку ключа реально решает рекламодатель (совет «подними ставку» осмыслен).
# Всё остальное (tCPA/tROAS/MAXIMIZE_*/TARGET_SPEND) — Smart Bidding: cpc_bid ключа там не управляет
# аукционом, менять надо цель/бюджет. Пустая/UNSPECIFIED стратегия → не советуем (fail-safe).
MANUAL_BID_STRATEGIES = frozenset({"MANUAL_CPC", "ENHANCED_CPC"})

FIRST_PAGE = "first_page"
TOP_OF_PAGE = "top_of_page"


@dataclass
class BidOpportunity:
    """Возможность по ставке одного ключа. target_bid — оценка Google (не наша выдумка)."""

    kind: str  # FIRST_PAGE | TOP_OF_PAGE
    campaign: str
    ad_group: str
    ad_group_id: str
    criterion_id: str
    keyword: str
    match_type: str
    bid: float  # текущая эффективная ставка
    target_bid: float  # оценка Google: сколько нужно
    conversions: float
    cpa: float
    cost: float
    impressions: int
    top_is: float  # 0..1; 0.0 → доля не прочитана (не «ноль верха»)

    @property
    def uplift_pct(self) -> int:
        """На сколько % поднять ставку до оценки Google (0 → нечего поднимать)."""
        if self.bid <= 0 or self.target_bid <= self.bid:
            return 0
        return round((self.target_bid / self.bid - 1) * 100)


def _rank_key(o: BidOpportunity) -> tuple:
    """Ранжируем ДЕНЬГАМИ: сначала ключи с доказанными конверсиями, затем по расходу (у него уже есть
    история), при равенстве — меньший разрыв ставки (дешевле закрыть)."""
    return (-o.conversions, -o.cost, o.uplift_pct)


def opportunities(
    rows,
    *,
    acct_cpa: float = 0.0,
    target_for=None,
    gap_min: float = 0.10,
    min_cost: float = 0.0,
) -> list[BidOpportunity]:
    """Найти ключи, чья ставка ниже оценок Google.

    • FIRST_PAGE — ставка ниже first_page_cpc: по оценке Google ключ не дотягивает даже до первой
      страницы, показы есть лишь остаточные. Требуем impressions > 0 (ключ живой; ключи с нулевым
      расходом в фетчер и не попадают — они за LIMIT по расходу, см. fetch_bid_landscape).
    • TOP_OF_PAGE — ключ КОНВЕРТИТ по приемлемому CPA (≤ цель, иначе ≤ средний по аккаунту), но ставка
      ниже top_of_page_cpc: доказанная ценность без верхних позиций.

    gap_min — минимальный относительный разрыв (ниже — шум оценки, не совет). target_for(campaign) →
    цель CPA кампании или None. Строки без ставки/оценки (0.0 = нет данных) пропускаем: молчание
    честнее выдуманного совета."""
    out: list[BidOpportunity] = []
    for r in rows or []:
        if _enum(getattr(r, "strategy_type", "")) not in MANUAL_BID_STRATEGIES:
            continue
        bid = float(getattr(r, "bid", 0.0) or 0.0)
        if bid <= 0:
            continue
        m = getattr(r, "metrics", None)
        cost = float(getattr(m, "cost", 0.0) or 0.0)
        impressions = int(getattr(m, "impressions", 0) or 0)
        conv = float(getattr(m, "conversions", 0.0) or 0.0)
        cpa = float(getattr(m, "cpa", 0.0) or 0.0)
        if cost < min_cost:
            continue

        fpc = float(getattr(r, "first_page_cpc", 0.0) or 0.0)
        top = float(getattr(r, "top_of_page_cpc", 0.0) or 0.0)
        kind = ""
        target_bid = 0.0
        if impressions > 0 and fpc > 0 and _gap(bid, fpc) >= gap_min:
            kind, target_bid = FIRST_PAGE, fpc
        elif conv > 0 and top > 0 and _gap(bid, top) >= gap_min:
            target = None
            if target_for is not None:
                target = target_for(getattr(r, "campaign", ""))
            ceiling = float(target) if target else acct_cpa
            if ceiling <= 0 or cpa > ceiling:  # дорогая конверсия → это не «подними ставку»
                continue
            kind, target_bid = TOP_OF_PAGE, top
        if not kind:
            continue

        out.append(
            BidOpportunity(
                kind=kind,
                campaign=getattr(r, "campaign", ""),
                ad_group=getattr(r, "ad_group", ""),
                ad_group_id=str(getattr(r, "ad_group_id", "") or ""),
                criterion_id=str(getattr(r, "criterion_id", "") or ""),
                keyword=getattr(r, "keyword", ""),
                match_type=str(getattr(r, "match_type", "") or ""),
                bid=round(bid, 2),
                target_bid=round(target_bid, 2),
                conversions=round(conv, 1),
                cpa=round(cpa, 2),
                cost=round(cost, 2),
                impressions=impressions,
                top_is=float(getattr(r, "top_is", 0.0) or 0.0),
            )
        )
    out.sort(key=_rank_key)
    return out


def _gap(bid: float, target: float) -> float:
    """Относительный разрыв (target − bid)/target; ≤0 → ставка уже не ниже оценки."""
    if target <= 0:
        return 0.0
    return max(0.0, (target - bid) / target)


def _enum(v) -> str:
    return str(getattr(v, "name", v) or "")

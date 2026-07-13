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
SIM = "sim"  # источник целевой ставки — симулятор Google (сильнее оценки позиции: он про конверсии)


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


@dataclass
class SimUpside:
    """Прирост по симулятору Google от текущей точки до рекомендуемой. Все числа — Google-прогноз."""

    target: float  # рекомендуемая ставка (CPC_BID) или дневной бюджет (BUDGET)
    current: float  # точка отсчёта — фактическая ставка/бюджет
    add_conversions: float
    add_cost: float
    add_clicks: float
    marginal_cpa: float  # Δрасход / Δконверсии на всём пути — цена этого прироста

    @property
    def uplift_pct(self) -> int:
        if self.current <= 0 or self.target <= self.current:
            return 0
        return round((self.target / self.current - 1) * 100)


def sim_upside(points, current: float, *, cap_cpa: float, min_conv_gain: float = 0.5):
    """Куда двигать ставку/бюджет по точкам симулятора — МАРЖИНАЛЬНЫМ шагом, не «в максимум».

    Идём вверх от текущей точки, пока КАЖДЫЙ следующий шаг окупается: Δрасход/Δконверсии ≤ cap_cpa
    (цель кампании, иначе средний CPA аккаунта). Первый неокупаемый шаг обрывает путь — иначе совет
    «поднимай до потолка» протащил бы вместе с прибыльным куском заведомо убыточный.

    cap_cpa ≤ 0 (цели нет, конверсий нет) → None: не с чем сравнивать «дорого/дёшево», а совет
    «поднимай» без потолка окупаемости — это про чужие деньги. Молчим (fail-closed, gr10).
    Прирост < min_conv_gain конверсий → None: не совет, а шум прогноза."""
    pts = [p for p in (points or []) if float(getattr(p, "amount", 0.0) or 0.0) > 0]
    if len(pts) < 2 or current <= 0 or cap_cpa <= 0:
        return None
    pts.sort(key=lambda p: float(p.amount))

    # Точка отсчёта — ближайшая СНИЗУ к фактической ставке/бюджету (симулятор считает от неё).
    base_i = 0
    for i, p in enumerate(pts):
        if float(p.amount) <= current:
            base_i = i
    base, cur_i = pts[base_i], base_i
    for i in range(base_i + 1, len(pts)):
        d_conv = float(pts[i].conversions) - float(pts[cur_i].conversions)
        d_cost = float(pts[i].cost) - float(pts[cur_i].cost)
        if d_conv <= 0 or d_cost / d_conv > cap_cpa:
            break
        cur_i = i
    if cur_i == base_i:
        return None

    tgt = pts[cur_i]
    add_conv = float(tgt.conversions) - float(base.conversions)
    add_cost = float(tgt.cost) - float(base.cost)
    if add_conv < min_conv_gain:
        return None
    return SimUpside(
        target=round(float(tgt.amount), 2),
        current=round(current, 2),
        add_conversions=round(add_conv, 1),
        add_cost=round(add_cost, 2),
        add_clicks=round(float(tgt.clicks) - float(base.clicks)),
        marginal_cpa=round(add_cost / add_conv, 2) if add_conv > 0 else 0.0,
    )


@dataclass
class BidBoardItem:
    """Строка «доски ставок» (/bids): один ключ + рекомендуемая ставка + чем она обоснована.

    source=SIM — прогноз симулятора Google (есть add_conversions); FIRST_PAGE/TOP_OF_PAGE — разрыв до
    оценки позиции (симулятора нет ⇒ прироста конверсий Google не обещал, и мы его НЕ придумываем:
    add_conversions=0.0 значит «неизвестно», а не «ноль»)."""

    campaign: str
    ad_group: str
    ad_group_id: str
    criterion_id: str
    keyword: str
    match_type: str
    bid: float
    target_bid: float
    source: str  # SIM | FIRST_PAGE | TOP_OF_PAGE (константы модуля: sim/first_page/top_of_page)
    add_conversions: float  # прогноз Google; 0.0 = симулятора нет (не «нулевой прирост»)
    add_cost: float
    marginal_cpa: float
    conversions: float  # факт за период
    cost: float
    cpa: float
    top_is: float

    @property
    def uplift_pct(self) -> int:
        if self.bid <= 0 or self.target_bid <= self.bid:
            return 0
        return round((self.target_bid / self.bid - 1) * 100)


def board(
    rows,
    sims=None,
    *,
    acct_cpa: float = 0.0,
    target_for=None,
    gap_min: float = 0.10,
    min_cost: float = 0.0,
    min_conv_gain: float = 0.5,
    top_n: int = 10,
) -> list[BidBoardItem]:
    """Доска возможностей по ставкам для /bids: объединяет ДВА сигнала по одному ключу — разрыв до
    оценки позиции (opportunities) и прогноз симулятора (sim_upside). Формулы и пороги — те же, что
    у чеков аудита: разъехаться им нельзя, источник один.

    Сортировка — по ПРОГНОЗНОМУ приросту конверсий (то, ради чего ставку и поднимают); ключи без
    симулятора идут ниже и ранжируются деньгами (факт. конверсии → расход → меньший разрыв).
    Есть оба сигнала ⇒ рекомендуем ставку СИМУЛЯТОРА: она ограничена окупаемостью (cap_cpa), а
    оценка позиции — нет."""
    opps = {
        (o.ad_group_id, o.criterion_id): o
        for o in opportunities(
            rows, acct_cpa=acct_cpa, target_for=target_for, gap_min=gap_min, min_cost=min_cost
        )
    }
    by_key = {(str(s.ad_group_id), str(s.criterion_id)): s for s in (sims or [])}
    out: list[BidBoardItem] = []
    for r in rows or []:
        key = (
            str(getattr(r, "ad_group_id", "") or ""),
            str(getattr(r, "criterion_id", "") or ""),
        )
        o = opps.get(key)
        bid = float(getattr(r, "bid", 0.0) or 0.0)
        up = None
        s = by_key.get(key)
        if s is not None and _enum(getattr(r, "strategy_type", "")) in MANUAL_BID_STRATEGIES:
            target = target_for(getattr(r, "campaign", "")) if target_for is not None else None
            up = sim_upside(
                s.points,
                bid,
                cap_cpa=float(target or acct_cpa),
                min_conv_gain=min_conv_gain,
            )
        if o is None and up is None:
            continue  # ни разрыва, ни окупаемого прогноза — поднимать нечего

        m = getattr(r, "metrics", None)
        out.append(
            BidBoardItem(
                campaign=o.campaign if o else getattr(r, "campaign", ""),
                ad_group=o.ad_group if o else getattr(r, "ad_group", ""),
                ad_group_id=key[0],
                criterion_id=key[1],
                keyword=o.keyword if o else getattr(r, "keyword", ""),
                match_type=o.match_type if o else str(getattr(r, "match_type", "") or ""),
                bid=round(bid, 2),
                target_bid=up.target if up else o.target_bid,
                source=SIM if up else o.kind,
                add_conversions=up.add_conversions if up else 0.0,
                add_cost=up.add_cost if up else 0.0,
                marginal_cpa=up.marginal_cpa if up else 0.0,
                conversions=o.conversions
                if o
                else round(float(getattr(m, "conversions", 0.0) or 0.0), 1),
                cost=o.cost if o else round(float(getattr(m, "cost", 0.0) or 0.0), 2),
                cpa=o.cpa if o else round(float(getattr(m, "cpa", 0.0) or 0.0), 2),
                top_is=o.top_is if o else float(getattr(r, "top_is", 0.0) or 0.0),
            )
        )
    out.sort(key=lambda i: (-i.add_conversions, -i.conversions, -i.cost, i.uplift_pct))
    return out[: max(1, int(top_n))]


def _gap(bid: float, target: float) -> float:
    """Относительный разрыв (target − bid)/target; ≤0 → ставка уже не ниже оценки."""
    if target <= 0:
        return 0.0
    return max(0.0, (target - bid) / target)


def _enum(v) -> str:
    return str(getattr(v, "name", v) or "")

"""Keyword research через KeywordPlanIdeaService (Google Ads API v24). READ-ONLY, advisory.

⛔ Замок аккаунта (golden rule #9): идеи запрашиваются ТОЛЬКО под разрешённым `customer_id`
(ensure_allowed). KeywordPlanIdeaService — это исследование рынка (сиды/URL → идеи + историч.
метрики), а не изменение аккаунта: мутаций нет, confirm-гейт НЕ нужен (как и отчёты). Метрики
(объём/конкуренция) и micros→валюту считает КОД, не модель. ~1 QPS лимит → серийно с backoff.

Идиомы v24 сверены live на 7753643025: язык/гео — resource-имена `languageConstants/{id}` и
`geoTargetConstants/{id}`; результат — `text` + `keyword_idea_metrics` (avg_monthly_searches,
competition, competition_index, *_top_of_page_bid_micros, average_cpc_micros). На тест-аккаунте
объём/конкуренция приходят реально, оценки ставок (CPC/bids) — нулевые (ограничение теста).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ads.client import ensure_allowed
from core.logging import log

# Языковые константы (сверено live): ru=1031, uk=1036, en=1000. Гео Украина (country) = 2804.
LANGUAGE_IDS: dict[str, int] = {"ru": 1031, "uk": 1036, "en": 1000}
DEFAULT_LANGUAGE = "ru"
DEFAULT_GEO_IDS: tuple[int, ...] = (2804,)  # Украина; переопределяемо вызывающей стороной
MAX_IDEAS = 200  # сколько идей ВОЗВРАЩАЕМ (топ по объёму, для Telegram/таблицы)
_FETCH_CEILING = 500  # сколько максимум тянем из API ДО сортировки (бюджет ~1 запроса)
_RETRIES = 3  # попыток при rate-limit (~1 QPS), экспоненциальный backoff


# Месяцы (v24 MonthOfYearEnum) → краткое RU для пика сезона.
_MONTH_RU = {
    "JANUARY": "янв",
    "FEBRUARY": "фев",
    "MARCH": "мар",
    "APRIL": "апр",
    "MAY": "май",
    "JUNE": "июн",
    "JULY": "июл",
    "AUGUST": "авг",
    "SEPTEMBER": "сен",
    "OCTOBER": "окт",
    "NOVEMBER": "ноя",
    "DECEMBER": "дек",
}


@dataclass
class KeywordIdea:
    text: str
    avg_monthly_searches: int = 0
    competition: str = "UNSPECIFIED"  # LOW | MEDIUM | HIGH | UNSPECIFIED
    competition_index: int = 0  # 0..100
    low_bid: float = 0.0  # top-of-page low bid (валюта аккаунта)
    high_bid: float = 0.0  # top-of-page high bid
    avg_cpc: float = 0.0  # средний CPC (валюта)
    peak_month: str = ""  # сезонность (§7): месяц макс. спроса, напр. «дек 2025»; '' если ряда нет


def _peak_month(monthly_volumes) -> str:
    """Месяц с максимальным объёмом из ряда monthly_search_volumes (сезонность §7) → «дек 2025».
    Пустая строка, если ряда нет или объёмы нулевые (на тест-аккаунте ряд часто пуст)."""
    best_searches, best_month, best_year = -1, "", 0
    for v in monthly_volumes or []:
        searches = int(getattr(v, "monthly_searches", 0) or 0)
        if searches > best_searches:
            best_searches = searches
            best_month = getattr(getattr(v, "month", None), "name", "") or ""
            best_year = int(getattr(v, "year", 0) or 0)
    if best_searches <= 0 or not best_month:
        return ""
    name = _MONTH_RU.get(best_month, best_month.title()[:3])
    return f"{name} {best_year}" if best_year else name


def _idea(r) -> KeywordIdea:
    """Ряд GenerateKeywordIdeaResult → KeywordIdea. micros→валюту и пик сезона считает КОД."""
    m = r.keyword_idea_metrics
    return KeywordIdea(
        text=r.text,
        avg_monthly_searches=int(m.avg_monthly_searches or 0),
        competition=getattr(m.competition, "name", "UNSPECIFIED"),
        competition_index=int(m.competition_index or 0),
        low_bid=(m.low_top_of_page_bid_micros or 0) / 1_000_000,
        high_bid=(m.high_top_of_page_bid_micros or 0) / 1_000_000,
        avg_cpc=(m.average_cpc_micros or 0) / 1_000_000,
        peak_month=_peak_month(getattr(m, "monthly_search_volumes", None)),
    )


def _is_retryable(e: Exception) -> bool:
    """Транзиентная ошибка Google Ads (rate-limit/квота/internal/deadline) — research read-only
    и идемпотентен, повтор безопасен. Классификация делегируется core.resilience._is_retryable_ads
    (по ИМЕНАМ enum-кодов — версионно-безопасно; единый источник истины с мутационным/read путём);
    текстовый матч — лишь фолбэк для необёрнутых/нестандартных ошибок без структуры кода."""
    from core.resilience import _is_retryable_ads

    if _is_retryable_ads(e):
        return True
    s = str(e).upper()  # фолбэк: ошибка без protobuf-структуры (напр. обёрнута/перепакована)
    return any(t in s for t in ("RESOURCE_EXHAUSTED", "RATE_EXCEEDED", "QUOTA"))


def generate_keyword_ideas(
    client,
    customer_id: str,
    *,
    seeds: list[str] | None = None,
    url: str | None = None,
    language: str = DEFAULT_LANGUAGE,
    geo_ids: tuple[int, ...] = DEFAULT_GEO_IDS,
    limit: int = MAX_IDEAS,
) -> list[KeywordIdea]:
    """Идеи ключевых слов по сид-ключам и/или URL. Синхронно → звать через asyncio.to_thread.

    seeds и url можно комбинировать (keyword_and_url_seed). Возвращает идеи, отсортированные по
    объёму поиска (по убыванию), не более `limit`. Замок аккаунта — ensure_allowed."""
    ensure_allowed(customer_id)  # golden rule #9: только разрешённый аккаунт
    seeds = [s.strip() for s in (seeds or []) if s.strip()]
    url = (url or "").strip() or None
    if not seeds and not url:
        raise ValueError("нужен хотя бы один сид-ключ или URL")

    svc = client.get_service("KeywordPlanIdeaService")

    def _build_request():
        req = client.get_type("GenerateKeywordIdeasRequest")
        req.customer_id = str(customer_id)
        lang_id = LANGUAGE_IDS.get(language, LANGUAGE_IDS[DEFAULT_LANGUAGE])
        req.language = f"languageConstants/{lang_id}"
        for gid in geo_ids:
            req.geo_target_constants.append(f"geoTargetConstants/{int(gid)}")
        req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        # И ключи, и URL → keyword_and_url_seed; только ключи → keyword_seed; только URL → url_seed.
        if seeds and url:
            req.keyword_and_url_seed.url = url
            req.keyword_and_url_seed.keywords.extend(seeds)
        elif seeds:
            req.keyword_seed.keywords.extend(seeds)
        else:
            req.url_seed.url = url
        return req

    ideas: list[KeywordIdea] = []
    for attempt in range(1, _RETRIES + 1):
        try:
            ideas = []
            for r in svc.generate_keyword_ideas(request=_build_request()):
                ideas.append(_idea(r))
                if len(ideas) >= _FETCH_CEILING:
                    break
            break
        except Exception as e:  # noqa: BLE001 — backoff только на транзиентных, иначе пробрасываем
            if _is_retryable(e) and attempt < _RETRIES:
                wait = 2**attempt
                log.warning(
                    "keyword_plan: rate-limit, повтор через %dс (попытка %d)", wait, attempt
                )
                time.sleep(wait)
                continue
            raise

    # Сортируем ВЕСЬ собранный пул по объёму и берём топ-`limit`: API отдаёт по релевантности,
    # поэтому обрезка ДО сортировки теряла бы ёмкие ключи на дальних позициях.
    ideas.sort(key=lambda k: k.avg_monthly_searches, reverse=True)
    if len(ideas) >= _FETCH_CEILING:
        log.info(
            "keyword_plan: достигнут потолок выборки %d → топ-%d по объёму", _FETCH_CEILING, limit
        )
    return ideas[:limit]

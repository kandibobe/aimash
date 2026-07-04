"""Детерминированный RU/EN-рендер рекомендаций (БЕЗ LLM) — fallback advisory-слоя.

Гарантирует, что фича работает даже при сбое/выключенной модели (fail-closed на формулировку):
formulate.phrase при любой ошибке откатывается сюда. Текст плоский (без parse_mode=HTML) — имена
кампаний приходят от клиента и могли бы сломать HTML; экранирование не требуется. Дисклеймер
переиспользует анти-мутационную формулировку из scheduler.jobs._format_alerts_multi.
"""

from __future__ import annotations

from advisor.rules import Recommendation


def _thou(n: float, dec: int = 0) -> str:
    return f"{n:,.{dec}f}".replace(",", " ")


def _money(n: float, currency: str) -> str:
    s = _thou(n, 2)
    return f"{s} {currency}" if currency else s


def render_recommendation(rec: Recommendation, lang: str = "ru") -> str:
    """Одна строка рекомендации по её kind + facts. Неизвестный kind → пустая строка (не роняем)."""
    from bot import i18n

    f = rec.facts
    cur = f.get("currency", "")
    if rec.kind == "spend_no_conv":
        return i18n.t(
            "advise_rec_spend_no_conv",
            lang,
            campaign=f.get("campaign", ""),
            cost=_money(f.get("cost", 0), cur),
            clicks=_thou(f.get("clicks", 0)),
        )
    if rec.kind == "high_cpa":
        return i18n.t(
            "advise_rec_high_cpa",
            lang,
            campaign=f.get("campaign", ""),
            cpa=_money(f.get("cpa", 0), cur),
            acct_cpa=_money(f.get("acct_cpa", 0), cur),
            factor=f.get("factor", 0),
        )
    if rec.kind == "budget_imbalance":
        return i18n.t(
            "advise_rec_budget_imbalance",
            lang,
            campaign=f.get("campaign", ""),
            share=f.get("share", 0),
            cpa=_money(f.get("cpa", 0), cur),
        )
    if rec.kind == "wasteful_keyword":
        return i18n.t(
            "advise_rec_wasteful_keyword",
            lang,
            campaign=f.get("campaign", ""),
            keyword=f.get("keyword", ""),
            match_type=f.get("match_type", ""),
            cost=_money(f.get("cost", 0), cur),
            clicks=_thou(f.get("clicks", 0)),
        )
    if rec.kind == "low_ctr_ad":
        return i18n.t(
            "advise_rec_low_ctr_ad",
            lang,
            campaign=f.get("campaign", ""),
            ad_group=f.get("ad_group", ""),
            ctr=f.get("ctr", 0),
            acct_ctr=f.get("acct_ctr", 0),
        )
    if rec.kind == "single_campaign":
        return i18n.t(
            "advise_rec_single_campaign",
            lang,
            campaign=f.get("campaign", ""),
            cost=_money(f.get("cost", 0), cur),
        )
    return ""


def render_digest(account: str, period_label: str, lines: list[str], lang: str = "ru") -> str:
    """Проактивный блок (для дайджеста планировщика): заголовок + строки + дисклеймер, одним текстом.
    В /advise используется поштучная отправка (каждая рекомендация со своими 👍/👎), а не этот блок."""
    from bot import i18n

    if not lines:
        return i18n.t("advise_empty", lang)
    head = i18n.t("advise_header", lang, account=account, period=period_label)
    disc = i18n.t("advise_disclaimer", lang)
    return head + "\n" + "\n".join(lines) + "\n" + disc

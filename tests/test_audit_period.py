"""Волна 1 (2026-07-09): аудит за ПРОИЗВОЛЬНУЮ дату/период.

Инварианты: /audit принимает число дней (rolling), «июнь 2025»/«прошлый месяц»/ISO-диапазон (custom);
custom-период НЕ пишет снапшот и НЕ считает тренд (иначе склобберит rolling-базу — ложная Δ); карточка
показывает выбранный период и баннер «моментальные сигналы — на сейчас» для исторических периодов.
READ-ONLY — Google Ads не трогается, ничего не мутируется.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit.engine import AuditResult  # noqa: E402
from audit.render import render_audit  # noqa: E402


def _result(**kw) -> AuditResult:
    return AuditResult(
        customer_id=kw.get("cid", "7753643025"),
        currency=kw.get("currency", "USD"),
        score=kw.get("score", 85),
        grade=kw.get("grade", "B"),
        total_spend=kw.get("total_spend", 1000.0),
        at_risk=kw.get("at_risk", 500.0),
        findings=[],
        families={},
        has_activity=kw.get("has_activity", True),
    )


# ── Парсер аргумента /audit → Period (kind решает судьбу снапшота) ─────────────────────
def test_audit_period_from_arg_rolling_vs_custom():
    import bot.main as bm

    # пусто → последние 30 дн (rolling)
    p = bm._audit_period_from_arg(None)
    assert p.kind == "last_n" and p.days == 30
    # голое число → последние N дн (rolling), кламп 1..365
    assert bm._audit_period_from_arg("7").kind == "last_n"
    assert bm._audit_period_from_arg("7").days == 7
    assert bm._audit_period_from_arg("9999").days == 365  # кламп
    # ISO-диапазон → custom с точными датами
    iso = bm._audit_period_from_arg("2025-06-01 2025-06-30")
    assert iso.kind == "custom"
    assert iso.date_from == date(2025, 6, 1) and iso.date_to == date(2025, 6, 30)
    # месяц с явным годом → custom, весь месяц
    jun = bm._audit_period_from_arg("июнь 2025")
    assert jun.kind == "custom"
    assert jun.date_from == date(2025, 6, 1) and jun.date_to == date(2025, 6, 30)
    # свободные фразы → распознаны (не падают)
    assert bm._audit_period_from_arg("прошлый месяц").kind == "custom"
    assert bm._audit_period_from_arg("MTD").kind == "mtd"
    # мусор → безопасный дефолт 30 дн (никогда не бросает)
    assert bm._audit_period_from_arg("абырвалг xyz").days == 30


# ── Карточка: метка периода в заголовке + баннер моментальных сигналов для custom ──────
def test_render_period_label_and_momentary_banner():
    res = _result()
    # обычный rolling-аудит: без баннера моментальных сигналов
    card = render_audit(res, "ru", period_label="последние 30 дн.", momentary=False)
    assert "последние 30 дн." in card
    assert "на СЕЙЧАС" not in card
    # исторический период: метка + баннер, что моментальные сигналы не за период
    hist = render_audit(res, "ru", period_label="2025-06-01 — 2025-06-30", momentary=True)
    assert "2025-06-01 — 2025-06-30" in hist
    assert "на СЕЙЧАС" in hist
    # EN-зеркало
    en = render_audit(res, "en", period_label="2025-06-01 — 2025-06-30", momentary=True)
    assert "reflect the account NOW" in en


# ── Обратная совместимость: без period_label заголовок как раньше (чистый) ─────────────
def test_render_without_period_label_unchanged_header():
    card = render_audit(_result(), "ru")
    assert card.splitlines()[0] == "🩺 Аудит · Аккаунт 7753643025"

"""Волна 1 (2026-07-09): аудит за ПРОИЗВОЛЬНУЮ дату/период.

Инварианты: /audit принимает число дней (rolling), «июнь 2025»/ISO-диапазон (custom), «прошлый
месяц» (last_month с 3.1 — относительное окно, TZ аккаунта его пере-якорит); исторический период
(custom/last_month) НЕ пишет снапшот и НЕ считает тренд (иначе склобберит rolling-базу — ложная Δ);
карточка показывает выбранный период и баннер «моментальные сигналы — на сейчас» для исторических
периодов. READ-ONLY — Google Ads не трогается, ничего не мутируется.
"""

from __future__ import annotations

import sys
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

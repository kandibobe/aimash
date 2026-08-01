"""§20: секция «Здоровье аккаунта» в досье (полный аудит на тап, живой рендер).

Инварианты (каждый — про честность или про деньги):
• проза секции = проза карточки /audit (audit.render), своих слов про находку нет;
• здоровье ВНЕ схемы досье и вне стора: балл живёт днями, досье — неделями, а llm_context уезжает в
  промпт RSA — находкам аудита там не место (по построению, а не по договорённости);
• тренд ЧИТАЕМ, снапшот НЕ пишем: единственный писатель baseline — /audit (иначе открытое досье
  затрёт честную базу собственным прогоном);
• `clients/` не вправе звать `audit.collect`: 23 чтения не должны заползти в путь краула;
• нет доступа/сбой сбора → файл приходит БЕЗ секции, кнопка не падает.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from audit.engine import build_audit  # noqa: E402
from clients.dossier_render import (  # noqa: E402
    HEALTH_TOP,
    render_health_markdown,
    render_llm_context,
    render_markdown,
    with_health,
)
from clients.dossier_schema import Company, Dossier  # noqa: E402
from reports.period import last_n_days  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PERIOD = last_n_days(30, today=date(2026, 6, 25))


def _report(*, cost: int = 500_000_000):
    """Аккаунт с расходом и БЕЗ конверсий — движку есть что сказать (иначе тест зелен вхолостую)."""
    m = Metrics(impressions=1000, clicks=100, cost_micros=cost, conversions=0.0)
    camp = Breakdown(
        key="campaign",
        title="Кампании",
        dim_headers=["Кампания", "Статус"],
        rows=[(("Search Brand", "ENABLED"), m)],
    )
    return SimpleNamespace(
        customer_id="7753643025",
        totals=m,
        prev_totals=m,
        period=PERIOD,
        currency="USD",
        breakdowns=[camp],
    )


def _dossier() -> Dossier:
    return Dossier(
        domain="example.com",
        website="https://example.com",
        company=Company(legal_name="Example LLC"),
        overview="Продаём авто.",
        contacts=[{"kind": "email", "value": "a@example.com"}],
    )


def test_health_section_speaks_the_same_prose_as_the_audit_card():
    """Секция — не пересказ карточки, а она сама: тот же headline и те же строки находок."""
    from audit.render import audit_headline, family_label, finding_text

    result = build_audit(_report())
    assert result.findings, "движку есть что сказать про этот аккаунт"

    md = render_health_markdown(result, "ru", trend="\n📊 Тренд: ▲ +3 к 2026-06-18 (70/100)")
    assert audit_headline(result, "ru") in md
    assert "📊 Тренд: ▲ +3" in md
    bullets = [ln for ln in md.split("\n") if ln.startswith("- ")]
    assert len(bullets) == min(HEALTH_TOP, len(result.findings))
    for f, ln in zip(result.findings, bullets):  # порядок карточки (worst-first), своих слов нет
        assert ln == f"- **{family_label(f.family, 'ru')}** — {finding_text(f, 'ru', 'USD')}"
    assert "/audit" in md  # сноска: полный разбор там


def test_health_section_is_empty_without_activity():
    """Мёртвый аккаунт → секции нет (не «0/100», которое напугает зря)."""
    zero = Metrics(impressions=0, clicks=0, cost_micros=0, conversions=0.0)
    dead = SimpleNamespace(
        customer_id="7753643025",
        totals=zero,
        prev_totals=zero,
        period=PERIOD,
        currency="USD",
        breakdowns=[],
    )
    assert render_health_markdown(build_audit(dead), "ru") == ""


def test_health_lands_above_the_fold_and_leaves_the_dossier_intact():
    """Вклейка над первой секцией («что горит» выше «чем занимается»), остальной файл — как был."""
    base = render_markdown(_dossier())
    health = render_health_markdown(build_audit(_report()), "ru")
    out = with_health(base, health)

    assert out.index("🩺 Здоровье аккаунта") < out.index("## Кратко")
    for line in base.split("\n"):  # ни одной строки досье не потеряли и не переставили
        assert line in out
    assert out.split("## Кратко", 1)[1] == base.split("## Кратко", 1)[1]
    assert with_health(base, "") == base  # нет секции → байт-в-байт исходник
    assert with_health(base, "   \n") == base


def test_health_never_reaches_the_schema_the_store_or_the_prompt():
    """Решающее (правило 5 + §20): здоровья нет в схеме досье ⇒ оно физически не может уехать в
    llm_context → в промпт генерации RSA/ключей. И `clients/` не зовёт полный сбор аудита: 23 чтения
    в пути краула — не то, что должно случаться по расписанию."""
    leaky = {"health", "score", "grade", "at_risk", "findings", "audit"}
    assert not (leaky & set(Dossier.model_fields)), "здоровье просочилось в схему досье"

    d = _dossier()
    ctx = render_llm_context(d)
    assert "Здоровье" not in ctx and "🩺" not in ctx

    for py in (ROOT / "clients").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "audit.collect" not in src and "gather_audit" not in src, (
            f"{py.name}: полный аудит (23 чтения) в слое клиентов — собирает bot-слой на тап человека"
        )

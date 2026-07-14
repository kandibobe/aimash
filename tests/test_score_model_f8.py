"""Ф8: финализация модели скора — уровень `critical`, блок «⚡ Быстрые победы», живые семьи
assets/pmax. Пинует ровно то, что легко разъезжается молча: порядок находок (critical выше денег),
согласованность весов severity между аудитом и advisor, покрытие семей вектором FAMILY_WEIGHT и
человеческие метки для КАЖДОГО сигнала, который collect-слой умеет объявить пробелом.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

from advisor.rules import _SEVERITY_WEIGHT
from audit.engine import CHECK_REGISTRY, build_audit
from audit.render import _SIGNAL_LABEL, render_audit
from audit.thresholds import FAMILY_WEIGHT, SEVERITY_MULT
from reports.service import Breakdown, Metrics

# Семьи БЕЗ веса — осознанно (диагноз/мнение Google, не дефект аккаунта). Список должен меняться
# только вместе с решением, а не по недосмотру: новая семья с опечаткой иначе тихо весит 0.
_ZERO_WEIGHT_FAMILIES = {"competition", "recommendations"}


def _report(totals: Metrics, campaign_rows, currency: str = "USD"):
    bds = [Breakdown("campaign", "Кампании", ["Кампания", "Статус"], list(campaign_rows))]
    return SimpleNamespace(customer_id="123", totals=totals, breakdowns=bds, currency=currency)


def _campaign(name: str, cost: float, clicks: int, conv: float, imps: int = 1000):
    return (
        (name, "ENABLED"),
        Metrics(
            impressions=imps, clicks=clicks, cost_micros=int(cost * 1_000_000), conversions=conv
        ),
    )


def _kill_and_waste():
    """Аккаунт с ДВУМЯ находками: 3×-Kill (critical, 300 под риском) и слив (warning, 600).
    Деньги у warning БОЛЬШЕ — значит порядок докажет, что сортирует severity, а не сумма."""
    totals = Metrics(impressions=4000, clicks=300, cost_micros=1_000_000_000, conversions=2)
    rows = [
        _campaign("Waste", 600, 100, 0),  # spend_no_conv → warning, at_risk 600
        _campaign("Expensive", 400, 200, 2, imps=3000),  # CPA 200 ≥ 3×50 → kill_rule, at_risk 300
    ]
    return build_audit(_report(totals, rows), target_cpa=50.0)


def test_critical_sorts_above_bigger_money_warning():
    res = _kill_and_waste()
    by_id = {f.check_id: f for f in res.findings}
    assert by_id["kill_rule"].severity == "critical" and by_id["kill_rule"].at_risk == 300.0
    assert by_id["spend_no_conv"].severity == "warning" and by_id["spend_no_conv"].at_risk == 600.0
    # critical впереди, хотя денег под ним ВДВОЕ меньше: «жжём втрое дороже своей цели» — стоп-кран.
    assert res.findings[0].check_id == "kill_rule"


def test_critical_level_is_exactly_three_checks():
    """Уровень имеет смысл, пока он редкий. Расширяешь набор — правь тест ОСОЗНАННО."""
    crit = {cid for cid, (_fam, sev) in CHECK_REGISTRY.items() if sev == "critical"}
    assert crit == {"kill_rule", "no_conversion_tracking", "zero_conversions"}


def test_advisor_severity_weights_cover_audit_levels():
    """advisor ранжирует советы СВОИМ вектором severity. Уровень, которого там нет, падает в
    .get(sev, 0.5) — critical оказался бы НИЖЕ warning, и советы разошлись бы с карточкой."""
    assert set(_SEVERITY_WEIGHT) >= set(SEVERITY_MULT)
    order = sorted(SEVERITY_MULT, key=lambda s: -SEVERITY_MULT[s])
    assert sorted(order, key=lambda s: -_SEVERITY_WEIGHT[s]) == order  # тот же порядок важности


def test_family_weight_covers_every_family_in_registry():
    """Семья из реестра, которой нет в FAMILY_WEIGHT, молча весит 0 (штраф .get(fam, 0.0)) — чек
    работает, а балл не двигает. Допустимо ТОЛЬКО для явного белого списка."""
    fams = {fam for fam, _sev in CHECK_REGISTRY.values()}
    missing = fams - set(FAMILY_WEIGHT) - _ZERO_WEIGHT_FAMILIES
    assert not missing, f"семьи без веса и без явного решения: {sorted(missing)}"
    assert sum(FAMILY_WEIGHT.values()) == 100.0


def test_quick_wins_block_sorts_by_money_not_severity():
    """«⚡ Быстрые победы» отвечают на другой вопрос, чем «топ-3»: не «что страшнее», а «что вернём
    прямо сейчас». Поэтому внутри блока порядок — по деньгам (Waste 600 выше kill_rule 300)."""
    card = render_audit(_kill_and_waste(), "ru")
    assert "⚡ Быстрые победы" in card
    assert "⏸ Пауза «Waste»" in card and "⏸ Пауза «Expensive»" in card
    assert card.index("⏸ Пауза «Waste»") < card.index("⏸ Пауза «Expensive»")
    # …а в «топ-3 действий» первым идёт critical — блоки НЕ дублируют друг друга по смыслу.
    assert card.index("⚡ Быстрые победы") < card.index("Что сделать сейчас")
    en = render_audit(_kill_and_waste(), "en")
    assert "⚡ Quick wins" in en and "⏸ Pause «Waste»" in en


def test_quick_wins_silent_without_one_tap_findings():
    """Нет находки, применимой одной кнопкой → блока нет вовсе (пустой заголовок хуже отсутствия)."""
    totals = Metrics(impressions=3000, clicks=300, cost_micros=1_000_000_000, conversions=10)
    res = build_audit(_report(totals, [_campaign("Main", 1000, 300, 10, imps=3000)]))
    assert not any(f.one_tap for f in res.findings)
    assert "⚡" not in render_audit(res, "ru")


def test_every_data_gap_signal_has_a_human_label():
    """Строка «ℹ️ Недостаточно данных» печатает slabels.get(s, s) — сигнал без метки уходит клиенту
    СЫРЫМ идентификатором («adgroup_structure»). Имена берём из ИСХОДНИКА collect-слоя, а не из
    списка в тесте: добавили фетчер — тест сразу требует метку."""
    from audit import collect

    src = inspect.getsource(collect.gather_audit)
    signals = set(re.findall(r'\(\s*"([a-z_]+)",\s*\w+\s*\)', src))
    signals |= set(re.findall(r'data_gaps\.append\("([a-z_]+)"\)', src))
    assert "pmax" in signals and "campaign_assets" in signals  # regex действительно что-то нашёл
    for lang in ("ru", "en"):
        missing = signals - set(_SIGNAL_LABEL[lang])
        assert not missing, f"сигналы без метки [{lang}]: {sorted(missing)}"

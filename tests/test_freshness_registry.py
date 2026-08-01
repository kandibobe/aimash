"""Инварианты freshness-реестра (Волна 1.1, шаг 1).

Реестр — это способ сделать «забыли про новую мутацию» ошибкой сборки, а не тихой дырой в проде.
Поэтому проверяется не поведение отдельных операций, а СВОЙСТВА реестра целиком:
полнота реестра, храповик долга, deny-by-default и то, что денежная операция не может оказаться
в мягком тире. Плюс семантика `compare` — она одна на все 41 операцию, значит её ошибка была бы
ошибкой сразу везде.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ads import freshness  # noqa: E402
from ads.freshness import FRESHNESS_TIERS, Tier  # noqa: E402
from ads.service import SUPPORTED_OPERATIONS  # noqa: E402
from core.ads_errors import is_outcome_unknown_after_mutate  # noqa: E402

# Денежные операции — зеркало `_EXPECTED_MONEY_OPS` из tests/test_invariants_core.py
# (там же в именах `apply_*`). Дублируется осознанно: если списки разойдутся, разойдутся и падения,
# и это будет видно — молчаливая рассинхронизация тут страшнее дубля.
MONEY_OPERATIONS = frozenset(
    {
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "set_bidding_strategy",
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
        "create_app_campaign",
    }
)

# NO_DIFF без префикса `create_`: линковка ассета / добавление в общий список — сущность создаётся,
# прежнего состояния у неё нет. Список явный, чтобы «просто закинуть операцию в NO_DIFF» требовало
# правки теста, а не одной строки реестра.
NO_DIFF_NON_CREATE = frozenset(
    {
        "add_sitelinks",
        "add_callouts",
        "add_structured_snippets",
        "attach_image_asset",
        "add_call_asset",
        "add_promotion",
        "add_price_asset",
        "add_negatives_to_shared_set",
        "attach_shared_set",
    }
)


def _ops_with(tier: Tier) -> set[str]:
    return {op for op, t in FRESHNESS_TIERS.items() if t is tier}


# ── Полнота и структура реестра ───────────────────────────────────────────────────


def test_registry_covers_every_supported_operation() -> None:
    """Реестр покрывает исполнимые операции ровно, без люфта в обе стороны: лишний ключ — мёртвая
    запись, недостающий — операция, проезжающая мимо гейта."""
    missing = set(SUPPORTED_OPERATIONS) - set(FRESHNESS_TIERS)
    extra = set(FRESHNESS_TIERS) - set(SUPPORTED_OPERATIONS)
    assert not missing, f"операции без тира freshness: {sorted(missing)}"
    assert not extra, f"тир для несуществующей операции: {sorted(extra)}"
    assert len(FRESHNESS_TIERS) == len(SUPPORTED_OPERATIONS) == 42


def test_advisory_is_subset_of_frozen_debt() -> None:
    """Храповик. Долг можно только сокращать: новая ADVISORY-операция обязана быть внесена в
    `ADVISORY_DEBT` руками — то есть осознанно и заметно в диффе."""
    new_debt = _ops_with(Tier.ADVISORY) - freshness.ADVISORY_DEBT
    assert not new_debt, (
        f"операции опущены в ADVISORY мимо храповика: {sorted(new_debt)} — "
        "либо поднимите их в STRICT, либо явно расширьте ADVISORY_DEBT"
    )


def test_debt_shrinks_only_and_never_contains_unknown_ops() -> None:
    """Обратная сторона храповика: в долге не должно оставаться имён, которых уже нет в реестре
    (иначе список гниёт и перестаёт быть ограничением)."""
    stale = freshness.ADVISORY_DEBT - set(FRESHNESS_TIERS)
    assert not stale, f"в ADVISORY_DEBT висят несуществующие операции: {sorted(stale)}"


def test_no_diff_operations_are_creation_only() -> None:
    """NO_DIFF значит «прежнего состояния не существует», а не «лень читать». Всё, что не `create_*`,
    перечислено поимённо."""
    suspicious = {
        op
        for op in _ops_with(Tier.NO_DIFF)
        if not op.startswith("create_") and op not in NO_DIFF_NON_CREATE
    }
    assert not suspicious, (
        f"NO_DIFF у операций, зависящих от прежнего состояния: {sorted(suspicious)}"
    )


def test_money_operations_are_never_advisory() -> None:
    """Деньги — ровно тот класс, где «не прочитали ⇒ отказ» обязателен. Денежная операция может быть
    только STRICT (есть что сверять) или NO_DIFF (создание с нуля), но никогда ADVISORY."""
    soft = {op for op in MONEY_OPERATIONS if FRESHNESS_TIERS[op] is Tier.ADVISORY}
    assert not soft, f"денежные операции в мягком тире: {sorted(soft)}"
    assert MONEY_OPERATIONS <= set(FRESHNESS_TIERS)


def test_tier_of_unknown_operation_refuses() -> None:
    """Deny-by-default: неизвестная операция не «проходит как no_diff», а отклоняется."""
    with pytest.raises(freshness.FreshnessMissing):
        freshness.tier_of("нет_такой_операции")


def test_freshness_errors_are_not_outcome_unknown() -> None:
    """Отказ freshness случается ДО SDK — мутации не было. Если бы `FreshnessError` наследовался от
    `TimeoutError`, `core.ads_errors` увёл бы нетронутый черновик в needs_review («сверьте в Google
    Ads») — то есть соврал бы пользователю о состоянии аккаунта."""
    for exc in (
        freshness.FreshnessError("x"),
        freshness.FreshnessMissing("x"),
        freshness.FreshnessDrift("x"),
    ):
        assert isinstance(exc, RuntimeError)
        assert not isinstance(exc, TimeoutError)
        assert is_outcome_unknown_after_mutate(exc) is False


# ── Семантика compare ─────────────────────────────────────────────────────────────


def test_compare_passes_on_identical_snapshot() -> None:
    snap = {"kind": "status", "before_status": "ENABLED"}
    freshness.compare("pause_campaign", {}, snap, dict(snap))


def test_compare_detects_drift_in_before_keys() -> None:
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare(
            "pause_campaign",
            {},
            {"kind": "status", "before_status": "ENABLED"},
            {"kind": "status", "before_status": "PAUSED"},
        )


def test_compare_ignores_after_keys() -> None:
    """`after_*` — вычисленное из params «станет», а не состояние аккаунта: пересчёт под другую
    базу не должен читаться как дрейф."""
    freshness.compare(
        "set_campaign_network",
        {},
        {"kind": "network", "before_search_partners": True, "after_search_partners": False},
        {"kind": "network", "before_search_partners": True, "after_search_partners": True},
    )


def test_compare_skips_before_micros_for_set_to() -> None:
    """set_to — абсолют: итог не зависит от базы, и все откаты строятся именно так. Блокировать их
    при дрейфе значило бы сломать путь восстановления ПОСЛЕ дрейфа."""
    freshness.compare(
        "update_budget",
        {"mode": "set_to", "value": 50},
        {"kind": "budget", "before_micros": 30_000_000},
        {"kind": "budget", "before_micros": 45_000_000},
    )


def test_compare_still_checks_micros_for_relative_mode() -> None:
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare(
            "update_budget",
            {"mode": "increase_by_percent", "value": 20},
            {"kind": "budget", "before_micros": 30_000_000},
            {"kind": "budget", "before_micros": 45_000_000},
        )


def test_compare_checks_non_micros_keys_even_for_set_to() -> None:
    """Послабление set_to касается ТОЛЬКО базы пересчёта. Радиус общего бюджета — не база, а предмет
    согласия: при set_to он обязан сверяться."""
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare(
            "update_budget",
            {"mode": "set_to", "value": 50},
            {"kind": "budget", "before_micros": 30_000_000},
            {"kind": "budget", "before_micros": 30_000_000, "shared": True},
        )


def test_compare_detects_shared_scope_appearing_only_in_live() -> None:
    """Ключ появился лишь в живом снимке (бюджет стал общим уже после показа карточки). Сверка по
    ключам сохранённого снимка это пропустила бы — поэтому берётся объединение."""
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare(
            "update_budget",
            {"mode": "increase_by_percent", "value": 10},
            {"kind": "budget", "before_micros": 30_000_000},
            {
                "kind": "budget",
                "before_micros": 30_000_000,
                "shared": True,
                "shared_campaigns": ["Brand"],
            },
        )


def test_compare_detects_shared_scope_growth() -> None:
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare(
            "update_budget",
            {"mode": "set_to", "value": 50},
            {"kind": "budget", "shared": True, "shared_campaigns": ["Brand"]},
            {"kind": "budget", "shared": True, "shared_campaigns": ["Brand", "Generic"]},
        )


def test_compare_is_order_insensitive_for_geo_and_shared_scope() -> None:
    """Гео-критерии и кампании общего бюджета читаются GAQL без ORDER BY — перестановка не дрейф."""
    freshness.compare(
        "set_geo_location",
        {},
        {"kind": "geo", "before_locations": ["Kyiv", "Lviv"], "before_proximity": []},
        {"kind": "geo", "before_locations": ["Lviv", "Kyiv"], "before_proximity": []},
    )
    freshness.compare(
        "update_budget",
        {"mode": "set_to", "value": 50},
        {"kind": "budget", "shared": True, "shared_campaigns": ["Brand", "Generic"]},
        {"kind": "budget", "shared": True, "shared_campaigns": ["Generic", "Brand"]},
    )


def test_compare_is_order_sensitive_for_micros_list() -> None:
    """У ставок групп/ключей порядок идёт ORDER BY id и значим: перестановка означает, что состав
    групп изменился, а не что «те же числа»."""
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare(
            "update_bid",
            {"mode": "increase_by_percent", "value": 10},
            {"kind": "bid", "before_micros": [100, 200]},
            {"kind": "bid", "before_micros": [200, 100]},
        )


def test_compare_detects_kind_mismatch() -> None:
    """Разъехавшийся `kind` значит, что сравнивают разные сущности — молча пропустить нельзя."""
    with pytest.raises(freshness.FreshnessDrift):
        freshness.compare("update_budget", {}, {"kind": "budget"}, {"kind": "status"})


def test_drift_message_does_not_leak_snapshot_values() -> None:
    """Правило 5: наружу — только ярлыки полей. Имя кампании пишет клиент, в текст ошибки оно
    попадать не должно."""
    with pytest.raises(freshness.FreshnessDrift) as ei:
        freshness.compare(
            "update_campaign",
            {},
            {"kind": "name", "before_name": "секретное-имя-клиента"},
            {"kind": "name", "before_name": "другое"},
        )
    assert "секретное-имя-клиента" not in str(ei.value)
    assert "другое" not in str(ei.value)
    assert "имя кампании" in str(ei.value)

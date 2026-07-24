"""Гейт A свежести — ВЕРТИКАЛЬНЫЙ рубеж внутри `ads.mutations` (Волна 1.1).

Гейт B (`ads.service._verify_freshness`, тесты — `tests/test_freshness_gate_b.py`) стоит в
оркестраторе, перечитывает аккаунт живьём и ловит ДРЕЙФ. Здесь проверяется другой рубеж: он живёт в
том же файле, что и сама мутация, и потому исполняется даже когда `apply_*` зовут МИМО оркестратора —
headless-WRITE, dev-скрипт, будущий MCP-инструмент. К Google Ads он не ходит и дрейф поймать не может;
он проверяет ПРОИСХОЖДЕНИЕ: снимок вообще снимался, и человеку показали прочитанное, а не пустоту.

Три класса проверок:
  · поведение по тирам — STRICT / ADVISORY / NO_DIFF / операция вне реестра;
  · структурные инварианты — гейт нельзя обойти аргументом и нельзя забыть в новой мутации;
  · один дублёр confirm-стора на весь `tests/`. Расползание копий не абстрактный грех: копии не знали
    про `get_confirmed`, и появление гейта A увело в красное 22 теста разом.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.freshness import FRESHNESS_TIERS, FreshnessMissing, Tier  # noqa: E402
from ads.mutations import (  # noqa: E402
    _require_confirmation,
    _require_freshness,
    apply_update_budget,
)
from conftest import FakeConfirmStore, FakeProposal, attested  # noqa: E402
from core.config import settings  # noqa: E402

MUTATIONS_SRC = Path(__file__).resolve().parents[1] / "ads" / "mutations.py"

# Тир берётся из реестра, а не зашивается литералом: перевод операции STRICT→ADVISORY обязан ломать
# эти тесты, а не тихо обесценивать их.
STRICT_OP = "update_budget"
ADVISORY_OP = "add_keywords"
NO_DIFF_OP = "create_rsa"


def _run(coro):
    return asyncio.run(coro)


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def test_registry_tiers_of_fixtures_are_what_the_file_assumes():
    """Страховка от молчаливого протухания: тесты ниже читаются как «STRICT отказывает»."""
    assert FRESHNESS_TIERS[STRICT_OP] is Tier.STRICT
    assert FRESHNESS_TIERS[ADVISORY_OP] is Tier.ADVISORY
    assert FRESHNESS_TIERS[NO_DIFF_OP] is Tier.NO_DIFF


# ── Поведение по тирам ────────────────────────────────────────────────────────────


def test_strict_without_snapshot_is_refused():
    """`attested({})` без `before` — честное «прочитать не смогли» (UNREADABLE). Для денег это отказ."""
    store = FakeConfirmStore(FakeProposal(STRICT_OP, params=attested({})))
    with pytest.raises(FreshnessMissing):
        _run(_require_freshness(store, "cid", STRICT_OP))


def test_strict_without_marker_at_all_is_refused():
    """Черновик, к которому аттестацию забыли прикрепить, обязан читаться как непрочитанный."""
    store = FakeConfirmStore(FakeProposal(STRICT_OP, params={"campaign_id": "1"}))
    with pytest.raises(FreshnessMissing):
        _run(_require_freshness(store, "cid", STRICT_OP))


def test_strict_with_attested_snapshot_passes():
    store = FakeConfirmStore(FakeProposal(STRICT_OP, params=attested({}, {"kind": "budget"})))
    _run(_require_freshness(store, "cid", STRICT_OP))  # не бросает — гейт пройден


def test_refusal_does_not_burn_the_confirmation():
    """Отказ по свежести НЕ должен съедать одноразовый черновик.

    Иначе человек остаётся и без операции, и без карточки: подтверждение уже потрачено, а мутации не
    было. Поэтому гейт стоит ДО `claim` — проверяем это по факту, а не по расположению строк."""
    store = FakeConfirmStore(FakeProposal(STRICT_OP, params=attested({})))
    with pytest.raises(FreshnessMissing):
        _run(_require_confirmation(store, "cid", STRICT_OP))
    assert store._claimed is False, "отказ по свежести потратил подтверждение"

    # Тот же черновик, но со снимком — подтверждение всё ещё на месте и столбится.
    store._p = FakeProposal(STRICT_OP, params=attested({}, {"kind": "budget"}))
    assert _run(_require_confirmation(store, "cid", STRICT_OP)) is not None
    assert store._claimed is True


def test_advisory_without_snapshot_passes(caplog):
    """Признанный долг (`ADVISORY_DEBT`) проезжает — но оставляет след, а не тишину."""
    store = FakeConfirmStore(FakeProposal(ADVISORY_OP, params=attested({})))
    with caplog.at_level("INFO", logger="aimash"):
        _run(_require_freshness(store, "cid", ADVISORY_OP))
    assert any("freshness(A)" in r.message for r in caplog.records)


def test_no_diff_does_not_touch_the_store():
    """Создание с нуля: прежнего состояния не существует, ходить в стор незачем."""

    class _Tripwire(FakeConfirmStore):
        async def get_confirmed(self, confirmation_id):
            raise AssertionError("NO_DIFF-операция ходила в стор за снимком")

    _run(_require_freshness(_Tripwire(FakeProposal(NO_DIFF_OP)), "cid", NO_DIFF_OP))


def test_operation_outside_registry_is_denied():
    """Deny-by-default: новая мутация, забытая в реестре, упирается в гейт, а не проезжает."""
    store = FakeConfirmStore(FakeProposal("totally_new_op"))
    with pytest.raises(FreshnessMissing):
        _run(_require_freshness(store, "cid", "totally_new_op"))


def test_store_without_get_confirmed_is_refused_for_strict():
    """Стор старого контракта (умеет `claim`, но не отдаёт черновик до него) = снимка нет ⇒ отказ.

    Это ровно та ветка, на которой в день появления гейта покраснели 22 теста с копиями дублёра.
    Она обязана оставаться fail-closed: «не умею отдать снимок» и «снимка нет» — одно и то же."""

    # Намеренно БЕЗ `claim`: до него ветка не доходит, а лишний метод сделал бы этот класс похожим
    # на копию дублёра и поднял бы инвариант ниже на самом же файле гарда.
    class _Legacy:
        async def finalize(self, confirmation_id, *, result): ...

    with pytest.raises(FreshnessMissing):
        _run(_require_freshness(_Legacy(), "cid", STRICT_OP))


def test_missing_proposal_defers_to_claim():
    """Черновика нет — отказ обязан прийти от `claim` (PermissionError), а не от свежести.

    Подменить его сообщением про свежесть значит соврать о причине: подтверждения не было вовсе."""
    store = FakeConfirmStore(proposal=None)
    with pytest.raises(PermissionError):
        _run(_require_confirmation(store, "cid", STRICT_OP))


def test_operation_mismatch_defers_to_claim():
    """Черновик под другую операцию — тоже вотчина `claim`, а не гейта свежести."""
    store = FakeConfirmStore(FakeProposal("pause_campaign", params=attested({})))
    with pytest.raises(PermissionError):
        _run(_require_confirmation(store, "cid", STRICT_OP))


def test_gate_a_stands_on_the_money_path_end_to_end():
    """Сквозь настоящую денежную мутацию: отказ наступает ДО обращения к SDK.

    `ads_client=object()` — если бы исполнение дошло до SDK, тест упал бы AttributeError, а не
    FreshnessMissing."""
    store = FakeConfirmStore(FakeProposal(STRICT_OP, user_initiated=True, params=attested({})))
    with allowed_ids(DRAFT_ACCOUNT_ID), pytest.raises(FreshnessMissing):
        _run(
            apply_update_budget(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="1",
                new_budget_micros=50_000_000,
                confirmation_id="cid",
                confirm_store=store,
                ads_client=object(),
            )
        )
    assert store._claimed is False


# ── Структурные инварианты: гейт нельзя обойти и нельзя забыть ────────────────────

# Имена, которыми снимок мог бы приехать в `apply_*` аргументом. Аргумент — это то, что напишет
# вызывающий: новый код его просто не передаст, и по правилу «нет снимка ⇒ сверять нечего» гард
# самоотключится. Снимок берётся ИЗ СТОРА по confirmation_id — и только оттуда.
BANNED_SNAPSHOT_ARGS = frozenset(
    {"_before", "before", "_freshness", "freshness", "snapshot", "_snapshot", "before_state"}
)


def _mutations_ast() -> ast.Module:
    return ast.parse(MUTATIONS_SRC.read_text(encoding="utf-8"))


def _arg_names(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    a = fn.args
    return {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}


def test_apply_functions_take_no_snapshot_argument():
    offenders = []
    for node in _mutations_ast().body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name.startswith(
            "apply_"
        ):
            bad = _arg_names(node) & BANNED_SNAPSHOT_ARGS
            if bad:
                offenders.append(f"{node.name}: {sorted(bad)}")
    assert not offenders, (
        "снимок свежести приехал в apply_* аргументом — гейт A так самоотключается: "
        + "; ".join(offenders)
    )


def test_freshness_runs_first_and_before_claim():
    """Порядок внутри `_require_confirmation` — не стилистика, а свойство: сначала свежесть, потом claim."""
    fn = next(
        n
        for n in _mutations_ast().body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_require_confirmation"
    )
    body = [
        s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    src_of = [ast.dump(s) for s in body]
    fresh_idx = next(i for i, s in enumerate(src_of) if "_require_freshness" in s)
    claim_idx = next(i for i, s in enumerate(src_of) if "'claim'" in s or '"claim"' in s)
    assert fresh_idx == 0, "гейт свежести перестал быть первым в _require_confirmation"
    assert fresh_idx < claim_idx, "claim столбит черновик раньше проверки свежести"


def test_freshness_gate_has_exactly_one_call_site():
    """Один вызов = одно место, где его можно забыть. Список из 41 строки дал бы 41 такое место."""
    tree = _mutations_ast()
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_require_freshness"
    ]
    assert len(calls) == 1, f"вызовов _require_freshness должно быть ровно 1, найдено {len(calls)}"


# ── Один дублёр confirm-стора на весь tests/ ──────────────────────────────────────


def test_confirm_store_double_is_not_duplicated():
    """Класс с собственным `async def claim(...)` вне `conftest.py` = новая копия контракта.

    Копии расходятся молча: расширение протокола (`get_confirmed` в Волне 1.1) чинится в одном месте
    и ломается в девяти. Нужен свой поведенческий нюанс — наследуйся от `FakeConfirmStore` и добавь
    только его."""
    offenders = []
    for path in sorted((Path(__file__).parent).glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            has_claim = any(
                isinstance(m, (ast.AsyncFunctionDef, ast.FunctionDef)) and m.name == "claim"
                for m in node.body
            )
            if not has_claim:
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)} | {
                b.attr for b in node.bases if isinstance(b, ast.Attribute)
            }
            if "FakeConfirmStore" not in bases:
                offenders.append(f"{path.name}:{node.lineno} class {node.name}")
    assert not offenders, "своя копия confirm-стора вместо conftest.FakeConfirmStore: " + "; ".join(
        offenders
    )

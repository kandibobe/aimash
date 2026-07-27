"""#8 Evals (Фаза B.2) — golden-set валидационного контракта денежного пути.

Прогоняет `tests/fixtures/gate_scenarios.jsonl` через БОЕВУЮ Pydantic-схему `agent.tools.schemas.SCHEMAS`
— ту же, что видит confirm-гейт в проде (и что зовёт `scripts/ab_test_models.py:164`). ОФЛАЙН,
детерминированно, без сети и LLM: тест ловит регрессию КОД-гарантий, а не точность модели.

Провенанс корпуса и почему он синтетический (данные клиента + прод снят) — `tests/fixtures/README.md`.

  • accept → схема принимает; expect_args (если задан) сверяется подмножеством: КОД обязан
    нормализовать валюту/режим/значение ровно так (алиас «грн»→UAH и т.п.).
  • reject → схема ОТВЕРГАЕТ (ValidationError) ДО кнопок ✅. Ослабили схему (пустили отрицательную
    сумму, set_to+percent, callout >25, period >400) → reject-строка позеленела на приёме → тест красный.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.tools.schemas import SCHEMAS

_CORPUS = Path(__file__).parent / "fixtures" / "gate_scenarios.jsonl"


def _load() -> list[dict]:
    with _CORPUS.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


_SCENARIOS = _load()


def test_corpus_wellformed() -> None:
    """Гигиена корпуса: непусто, уникальные id, известный fn, валидный expect. Иначе «зелёный» тест
    мог бы просто молча пропускать кривые строки — дыра в покрытии под видом успеха."""
    assert _SCENARIOS, f"пустой корпус {_CORPUS.name} — нечего проверять"
    ids = [s.get("id") for s in _SCENARIOS]
    assert all(ids), "у каждого сценария обязан быть id"
    assert len(ids) == len(set(ids)), f"дублирующиеся id: {[i for i in ids if ids.count(i) > 1]}"
    for s in _SCENARIOS:
        assert s["fn"] in SCHEMAS, f"{s['id']}: fn '{s['fn']}' нет в боевом SCHEMAS"
        assert s["expect"] in ("accept", "reject"), f"{s['id']}: expect ∈ {{accept, reject}}"


@pytest.mark.parametrize("scn", _SCENARIOS, ids=[s["id"] for s in _SCENARIOS])
def test_scenario_matches_production_schema(scn: dict) -> None:
    """Каждый сценарий подтверждает боевой контракт схемы: accept проходит и нормализуется как задумано,
    reject отвергается ValidationError ДО кнопок ✅ (то, что реально останавливает деньги)."""
    schema = SCHEMAS[scn["fn"]]
    if scn["expect"] == "reject":
        with pytest.raises(ValidationError):
            schema(**scn["args"])
        return

    # accept: не должно бросать; нормализованные поля обязаны совпасть с expect_args (подмножество).
    dumped = schema(**scn["args"]).model_dump()
    for key, want in (scn.get("expect_args") or {}).items():
        assert dumped.get(key) == want, (
            f"{scn['id']}: поле '{key}' после валидации = {dumped.get(key)!r}, ожидалось {want!r} "
            f"(КОД обязан нормализовать — см. note: {scn.get('note', '')})"
        )

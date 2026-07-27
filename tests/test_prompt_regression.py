"""#8 Evals — regression-guard системных промптов-КОНТРАКТОВ (Фаза B.1).

Дыра, которую закрывает: смена промпта проходит незамеченной, а модель отдельно меняется по
A/B (`scripts/ab_test_models.py`). Значит поведение может «уехать» из-под обоих сразу — правка
текста + смена модели, и никто не сверял. Тест фиксирует SHA-256 промптов, несущих ГАРАНТИЮ:
read-only, fact-guard (числа только из данных аудита/инструментов), контур исполнителя. Тихий
дрейф → красный тест; осознанная правка → обнови хэш и бампни PROMPT_EPOCH записью-обоснованием.

Зеркалим дисциплину `SCORE_MODEL_EPOCH` (`audit/thresholds.py:172`): не «тест ради теста», а
маркер осознанного изменения в дифф-ревью. Там эпоха — из-за слепоты хэша чека к смене СМЫСЛА
при неизменных полях; здесь наоборот — хэш ловит смену ТЕКСТА, а эпоха с комментом объясняет
ЗАЧЕМ (иначе апдейт снапшота — непрозрачный hex-дифф).

ОБЛАСТЬ — осознанно узкая. Каждый замороженный промпт = точка обслуживания; широкий снапшот
приучает жать «обновить хэш» не глядя (ровно то, от чего предостерегает комментарий эпох в
audit/thresholds.py). Замораживаем ТОЛЬКО контракты безопасности/поведения:
  • agent.loop.SYSTEM — системный промпт исполнителя (агентский цикл, tool-calling).
  • agent.loop._ANALYST_SYSTEM[ru|en] — ЖЁСТКИЕ ПРАВИЛА: числа только из данных, «ничего не
    меняю» (read-only). Живой сегодня путь — его открывает run_scope в run_analysis_agent.
  • agent.loop._ANALYST_QA_SYSTEM[ru|en] — те же правила в Q&A-режиме.

НЕ замораживаем и почему (это НЕ пробел, а граница):
  • adcopy._REPAIR_SYSTEM / _COVERAGE_SYSTEM — их «безопасность» (headline ≤30, description ≤90)
    даёт КОД (adcopy/validate.py, правило 4), а качество меряет retry-метрика (Фаза B.3). Их
    ХОТЯТ крутить ради меньшего числа ретраев — заморозка хэшем воевала бы с этой же фазой.
  • clients._NORMALIZE_SYSTEM — помощник нормализации досье (карантинный экстрактор), low-stakes.
  • scripts.ab_test_models.COPY_PROMPT — промпт САМОГО eval-харнесса, не прод (заморозка кругова́я).
  • core.texts.RSA_REFINE_PROMPT — UX-текст кнопки, не системный промпт.

Системного префикса Hermes здесь НЕТ намеренно: его несёт сам Hermes через свой config, и пин
v0.19.0 замораживает его версией ядра, а не нашим хэшем. Guardable-константа появится, когда
сядет детерминированная сборка WRITE/PLAN-префикса на нашей стороне (CLAUDE.md: «системный
префикс собирать детерминированно») — тогда её место здесь.
"""

from __future__ import annotations

import hashlib

from agent import loop

# Эпоха промптов-контрактов. Бампается ВМЕСТЕ с обновлением снапшота ниже при осознанной правке —
# в дифф-ревью это маркер «промпт-контракт сменился намеренно», как SCORE_MODEL_EPOCH. Комментами
# ниже ведём changelog смысла (значение int само по себе ничего не считает — важна история «зачем»).
# Эпоха 1 (2026-07-24): первичный снапшот — SYSTEM исполнителя + аналитик и Q&A (ru+en).
PROMPT_EPOCH: int = 1


def _guarded() -> dict[str, str]:
    """label → рантайм-значение промпта. Python-комменты между конкатенациями строк в исходнике
    в значение НЕ входят — хэшируем то, что реально уходит в модель, а не оформление файла."""
    return {
        "agent.loop.SYSTEM": loop.SYSTEM,
        "agent.loop._ANALYST_SYSTEM[ru]": loop._ANALYST_SYSTEM["ru"],
        "agent.loop._ANALYST_SYSTEM[en]": loop._ANALYST_SYSTEM["en"],
        "agent.loop._ANALYST_QA_SYSTEM[ru]": loop._ANALYST_QA_SYSTEM["ru"],
        "agent.loop._ANALYST_QA_SYSTEM[en]": loop._ANALYST_QA_SYSTEM["en"],
    }


# Пины SHA-256(utf-8). Обновлять ТОЛЬКО осознанно — вместе с бампом PROMPT_EPOCH и записью выше.
_PINNED: dict[str, str] = {
    "agent.loop.SYSTEM": "80aedd727f1691b928cae59a9eac5f83b5ff3425fb2fb467941174fe42dd3861",
    "agent.loop._ANALYST_SYSTEM[ru]": "c50172c935e3750afee402e9d585eb1599bc3e7800b2c3ad3711dc04dd01bbb6",
    "agent.loop._ANALYST_SYSTEM[en]": "20f9a9beb77e6e73cb3b44e34851be9551cddc5578f2fa0ad2be767ace4843ed",
    "agent.loop._ANALYST_QA_SYSTEM[ru]": "51d55c32efcb87927a032d624a7cbcdd8408bd2016ace3a098ec050976381cd5",
    "agent.loop._ANALYST_QA_SYSTEM[en]": "c82765e2e5cde5598ff4894d0f587231bc8b38f23731bfaa657d9ef2a82f1001",
}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_guarded_set_matches_pins() -> None:
    """Набор охраняемых промптов == набор пинов. Добавили контракт и забыли запинить (или
    удалили промпт, оставив пин) — красный, а не молчаливая дыра в покрытии."""
    guarded, pinned = set(_guarded()), set(_PINNED)
    assert guarded == pinned, (
        "рассинхрон охраняемых промптов и пинов — "
        f"без пина: {guarded - pinned or '∅'}; лишние пины: {pinned - guarded or '∅'}"
    )


def test_system_prompts_unchanged() -> None:
    """Каждый промпт-контракт совпадает с запиненным хэшем. Красный = текст промпта уехал."""
    drift = []
    for label, value in _guarded().items():
        got, want = _sha(value), _PINNED.get(label)
        if got != want:
            drift.append(f"  {label}\n    было  {want}\n    стало {got}")
    assert not drift, (
        "промпт-контракт(ы) изменились без обновления снапшота:\n"
        + "\n".join(drift)
        + "\n\nЕсли правка ОСОЗНАННА: впиши новый хэш («стало») в _PINNED И бампни PROMPT_EPOCH "
        f"(сейчас {PROMPT_EPOCH}) с записью-обоснованием, как эпохи в audit/thresholds.py. "
        "Если нет — верни промпт."
    )

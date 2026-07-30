# Account Health Score

Источник истины реализации — `audit/engine.py`, веса и пороги — `audit/thresholds.py`, снапшоты —
`audit/snapshot.py`. Score детерминирован; модель его не считает и не исправляет.

Формула:

```
family_penalty = FAMILY_WEIGHT × min(1, Σ severity_multiplier × intensity)
score = 100 − Σ family_penalty
```

Интенсивность денежной находки — доля дедуплицированного `at_risk` в расходе; неденежной —
`NONMONEY_INTENSITY`, если чек не передал явную интенсивность. Семейные веса суммируются в 100.
`at_risk` дедуплицируется по spend segment и не может превышать расход. Упущенная выгода не
маскируется под уже потраченные деньги.

| Score | Grade | Ops band |
|---:|:---:|---|
| 90–100 | A | green |
| 80–89.99 | B | green |
| 65–79.99 | C | yellow |
| 50–64.99 | D | red |
| 0–49.99 | F | red |
| нет активности | — | no data, не фейковые 100 |

Семьи: waste 28, conversion tracking 18, keywords 10, budget 9, geo 7, RSA 7, delivery 6,
structure 5, bidding 4, assets 3, PMax 3. Competition и Google recommendations не штрафуют score:
это контекст/мнение, не доказанный дефект.

`score_model_version` хэширует веса, пороги и реестр чеков плюс ручную semantic epoch. Тренд
сравнивается только при одинаковых `score_model_version` и `period_days`; иначе показывает «н/д».
Снапшот не хранит PII или имена кампаний и очищается по `ACCOUNT_HEALTH_RETAIN_DAYS`.

Portfolio triage использует score вместе с critical incidents и decision queue, а не заменяет ими
доказательства. Проверки: `tests/test_audit_engine.py`, `tests/test_health_snapshot.py`.

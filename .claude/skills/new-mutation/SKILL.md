---
name: new-mutation
description: Скаффолд новой изменяющей (mutation) операции Google Ads СРАЗУ с обязательным confirm-гейтом и audit-логом. Использовать всякий раз, когда нужно добавить любое действие, меняющее аккаунт (бюджет, ставка, ключи, ГЕО, статус кампании и т.п.).
---

# Скаффолд безопасной мутации Google Ads

Любая операция, меняющая аккаунт, ОБЯЗАНА проходить через confirm-гейт. Никогда не выполняй изменение сразу из агент-инструмента. Шаблон ниже — единственный допустимый паттерн.

## Жёсткие правила
1. Mutation-инструмент агента **создаёт proposal** (черновик), но НЕ выполняет изменение.
2. Реальное выполнение — отдельная функция в `ads/mutations.py`, которая **принимает `confirmation_id`** и проверяет, что он соответствует подтверждённой строке в `audit_log`. Без валидного `confirmation_id` — `raise PermissionError`.
3. **Бюджетные** мутации дополнительно требуют флага «прямая команда пользователя» (никогда из scheduler/anomaly).
4. Диапазоны/значения валидируются **в коде** (Pydantic + явные проверки), не на доверии к модели.
5. Никаких секретов в логи; логировать что/кто/когда/результат.

## Шаги
1. Опиши параметры мутации как Pydantic-модель в `agent/tools/schemas.py`.
2. Зарегистрируй **propose-инструмент** в `agent/tools/` — он только строит `Proposal` с diff «было→станет» и кладёт в БД.
3. Реализуй исполнитель в `ads/mutations.py` по шаблону:

```python
async def apply_<operation>(params: <Params>, *, confirmation_id: str, db, ads_client) -> Result:
    # 1) Проверка подтверждения — БЕЗ него не выполнять
    audit = await db.get_confirmed_proposal(confirmation_id)
    if audit is None or audit.operation != "<operation>" or audit.status != "confirmed":
        raise PermissionError("mutation without a valid confirmation_id")

    # 2) (для бюджета) проверка прямой команды
    if "<operation>" == "update_budget" and not audit.user_initiated:
        raise PermissionError("budget change must be a direct user command")

    # 3) Валидация диапазонов в КОДЕ (не доверять модели)
    params.validate_ranges()

    # 4) Выполнение через google-ads SDK (только TEST MCC при ENV=dev)
    result = await ads_client.mutate(...)

    # 5) Финализировать audit-row (результат)
    await db.finalize_audit(confirmation_id, result=result)
    return result
```

4. Добавь тест в `tests/`:
   - вызов `apply_<operation>` **без** `confirmation_id` → `PermissionError`;
   - с подтверждённым proposal → выполнение + строка в `audit_log`;
   - (для бюджета) вызов из scheduler-контекста → отклонён.

## Чеклист перед готовностью
- [ ] propose не выполняет изменение, только создаёт diff
- [ ] исполнитель требует и валидирует `confirmation_id`
- [ ] диапазоны валидируются в коде
- [ ] бюджет — только user_initiated
- [ ] есть audit-row и тест на отказ без подтверждения

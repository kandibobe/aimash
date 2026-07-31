---
name: new-mutation
description: Скаффолд новой изменяющей (mutation) операции Google Ads СРАЗУ с обязательным confirm-гейтом и audit-логом. Использовать всякий раз, когда нужно добавить любое действие, меняющее аккаунт (бюджет, ставка, ключи, ГЕО, статус кампании и т.п.).
---

# Скаффолд безопасной мутации Google Ads

Любая операция, меняющая аккаунт, ОБЯЗАНА проходить через confirm-гейт. Никогда не выполняй изменение сразу из агент-инструмента. Шаблон ниже — единственный допустимый паттерн.

## Жёсткие правила
1. Mutation-инструмент агента **создаёт proposal** (черновик), но НЕ выполняет изменение.
2. Каждая операция должна быть в `ads.service.SUPPORTED_OPERATIONS` (единый источник истины). Иначе агент обязан отклонить её ДО показа кнопок (capability-guard в `agent.loop`), а `execute_confirmed` — отвергнуть как defense-in-depth. Так закрывается класс «падает ПОСЛЕ ✅».
3. Реальное выполнение — отдельная функция `apply_<operation>` в `ads/mutations.py` за **двумя гейтами**: (а) `ads.client.ensure_allowed(customer_id)` — замок аккаунта; (б) `_require_confirmation(...)` — **АТОМАРНО столбит** черновик через `store.claim` (compare-and-set `confirmed → executing`, одноразово, с привязкой к операции). Без успешного claim — `raise PermissionError`. Это и есть защита от replay/double-spend: claim в гейте, не в UI.
4. **Бюджет и ставки** дополнительно требуют `proposal.user_initiated` (никогда из scheduler/anomaly). Дефолт флага — `False` (fail-closed); `True` ставит только доверенный вход `bot.main.on_text`.
5. Диапазоны/значения/длины валидируются **в коде** (Pydantic + явные проверки) и **ДО claim**, чтобы плохие данные не «съели» одноразовый черновик. Кириллица = 1 символ (`len()` по code points).
6. Никаких секретов в логи; логировать что/кто/когда/результат.

## Шаги
1. Опиши параметры мутации как Pydantic-модель в `agent/tools/schemas.py` (+ валидаторы диапазонов/длин).
2. Добавь имя операции в `SCHEMAS`/`MUTATION_TOOLS` и в `ads.service.SUPPORTED_OPERATIONS`.
3. Реализуй исполнитель в `ads/mutations.py` по шаблону (порядок важен):

```python
async def apply_<operation>(
    *, customer_id: str, ..., confirmation_id: str, confirm_store, ads_client
) -> dict:
    ensure_allowed(customer_id)                 # Гейт 1: замок аккаунта (ДО всего)

    params_validate_ranges(...)                 # Валидация В КОДЕ — ДО claim

    # Гейт 2: АТОМАРНО столбит черновик (confirmed → executing, одноразово, с проверкой операции).
    proposal = await _require_confirmation(confirm_store, confirmation_id, "<operation>")

    if not proposal.user_initiated:             # деньги (бюджет/ставка) — только команда человека
        raise PermissionError("budget/bid change must be a direct user command")

    result = await asyncio.to_thread(_<operation>_via_sdk, ads_client, customer_id, ...)
    await confirm_store.finalize(confirmation_id, result=result)  # executing → applied + audit
    return result
```

4. Добавь ветку резолва+вызова в `ads/service.execute_confirmed` (имя кампании → id, считаем дельты), используя `customer_id = DRAFT_ACCOUNT_ID`.
5. Добавь тесты в `tests/test_write_layer.py`:
   - `apply_<operation>` **без** confirmation (FakeStore без proposal) → `PermissionError`;
   - чужой `customer_id` → `PermissionError` (замок до всего);
   - happy-path → SDK-исполнитель вызван + `store.finalized`;
   - **replay**: тот же `confirmation_id` второй раз → `PermissionError`, SDK вызван РОВНО один раз;
   - (для бюджета/ставки) `user_initiated=False` → отклонён.

## Чеклист перед готовностью
- [ ] операция в `SUPPORTED_OPERATIONS` + capability-guard отклоняет неподдержанное ДО кнопок
- [ ] propose не выполняет изменение, только создаёт diff
- [ ] `ensure_allowed` первым; `_require_confirmation` (claim) до SDK; finalize после
- [ ] диапазоны/длины валидируются в коде ДО claim (кириллица = 1)
- [ ] бюджет/ставка — только `user_initiated`
- [ ] есть тесты: без подтверждения, чужой аккаунт, happy, **replay (one-shot)**

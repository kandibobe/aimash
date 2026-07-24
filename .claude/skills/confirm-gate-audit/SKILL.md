---
name: confirm-gate-audit
description: Проверка, что путь изменяющей операции защищён confirm-гейтом и audit-логом (мутация не выполняется без подтверждения «да»). Использовать при ревью любой mutation-логики и перед мерджем.
---

# Аудит confirm-гейта

Суть безопасности Aimash: **мутация и подтверждение разделены**. Эта проверка ловит обход.

## Что проверить в коде
1. **Ни один** путь к `ads/mutations.py::apply_*` не вызывается напрямую из агент-инструмента или хендлера без прохождения через `confirm/gate.py`.
2. Каждый `apply_*` в начале проверяет `confirmation_id` против `audit_log` (status == "confirmed") и `raise`-ит, если нет.
3. **Бюджет:** изменение бюджета невозможно из scheduler/anomaly — только при `user_initiated == True`.
4. Proposal содержит человекочитаемый diff «было → станет»; большие списки (ключи/минус-слова) — ссылкой/вложением, не текстом целиком.
5. Audit-row пишется и при подтверждении, и при отклонении (кто/когда/что/результат). Без секретов.
6. **Allow-list:** агент может вызвать только перечисленные инструменты; неизвестная операция отклоняется кодом (защита от prompt-injection).
7. **Fail-closed (golden rule #10):** whitelist при пустом наборе блокирует ВСЕХ (`if uid not in wl`, НЕ `if wl and uid not in wl` — последнее fail-open); `ensure_allowed`/`ensure_manager_allowed` при пустой конфигурации — отказ; `user_initiated` по умолчанию `False`.
8. **Переход состояний — атомарный (compare-and-set), не read-modify-write:** `ConfirmStore.confirm`/`claim` — один `UPDATE … WHERE status=…` с проверкой `rowcount` (защита от TOCTOU при двойной доставке ✅).
9. **Редакция перед выходом (golden rule #5):** `apply_*`/хендлеры НЕ шлют сырой `str(e)` пользователю — через `bot.ux.err_text`/`redact_text`.

## Быстрый поиск дыр
```bash
# мутации, вызываемые без confirmation_id:
grep -rn "apply_" --include=*.py | grep -v confirmation_id
# прямые вызовы google-ads mutate вне ads/mutations.py:
grep -rn "\.mutate(" --include=*.py | grep -v "ads/mutations.py"
# изменение бюджета без проверки user_initiated:
grep -rn "budget" --include=*.py | grep -i "mutate\|apply"
```

## Тесты, которые должны существовать
- `apply_*` без `confirmation_id` → `PermissionError`.
- `apply_*` с подтверждённым proposal → выполняется + audit-row.
- budget из scheduler-контекста → отклонён.
- неизвестная операция от агента → отклонена allow-list'ом.

## Чеклист
- [ ] нет обхода confirm-гейта
- [ ] confirmation_id проверяется в каждом apply_*
- [ ] бюджет только user_initiated
- [ ] audit пишется без секретов
- [ ] allow-list операций активен
- [ ] whitelist fail-closed (пустой = блок всех), переходы статусов атомарны
- [ ] сырой текст ошибки не уходит пользователю (redact)
- [ ] есть негативные тесты

---
description: Единая точка входа в живой смоук против реального Google Ads (read-only по умолчанию, только Draft-аккаунт за confirm-гейтом)
argument-hint: "[--read-only] или домен: gdn | video_dg | lead_form | postgres"
---

Прогони живую проверку против настоящего Google Ads: **$ARGUMENTS**

Офлайн-проверка — это `make test` (весь pytest-набор, фейковый SDK). Этот флоу — про
РЕАЛЬНЫЕ креды и настоящий API. Требует живого `.env` (OAuth/dev-token/refresh). Мутации —
только на Draft-аккаунте `7753643025` и только за confirm-гейтом.

## Окружение (Windows)

Консоль часто cp1251 → emoji (✅/⚠️) в выводе роняют скрипт `UnicodeEncodeError`. Запускай с
префиксом:
```bash
PYTHONIOENCODING=utf-8 python scripts/<script>.py
```
(скрипты и так зовут общий шим `scripts/_win_console.enable_utf8()`, но префикс — надёжнее.)

## Последовательность

1. **Доступ + иерархия MCC (read-only, ничего не меняет)** — начни всегда с этого:
   ```bash
   make check-access                         # = python scripts/check_access.py
   ```
2. **Полный round-trip через confirm-гейт** (pause↔resume, net-zero) на Draft:
   ```bash
   python scripts/live_smoke_test.py --read-only   # без --read-only = реальная обратимая мутация
   ```
3. **Доменные смоуки** (по необходимости — что затронул):
   - `python scripts/live_smoke_gdn.py` — GDN-кампания через полный гейт;
   - `python scripts/live_smoke_video_dg.py` — Video / Demand Gen;
   - `python scripts/live_smoke_lead_form.py` — lead-форма.
4. **Postgres (§13)** — миграции применяются чисто + smoke-read:
   ```bash
   python scripts/verify_postgres.py
   ```

## Правила

- Начинай с шага 1 (read-only) — если доступа нет, дальше нет смысла.
- Показывай РЕАЛЬНЫЙ вывод скрипта, а не пересказ. Упал шаг — веди с этого.
- Любая мутация — только на Draft `7753643025`; чужой/боевой id замок `ensure_allowed` отсечёт.

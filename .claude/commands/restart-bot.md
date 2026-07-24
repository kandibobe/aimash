---
description: Консолидированная процедура (пере)запуска бота — 409-Conflict, single-instance lock, double-import гард
---

(Пере)запусти Telegram-бота. Процедура раскидана по 6+ докам — вот единый порядок.

## Запуск

```bash
python -m bot.main          # = make run
```

## Если «бот молчит» / не отвечает

Почти всегда это **Telegram 409 Conflict**: два поллера на одном токене. Причины и защита:

1. **Дубль-инстанс.** Убей старый процесс (по PID) — на токене должен опрашивать ровно один.
   Страховки в коде: session-level advisory-lock Postgres
   ([db/session.py:52](db/session.py#L52), `pg_try_advisory_lock`, non-blocking — второй инстанс
   не стартует), и `drop_pending_updates=True` через `delete_webhook` перед polling
   ([bot/main.py](bot/main.py)). На SQLite lock — no-op (dev).
2. **Скрамбл хендлеров при `python -m bot.main`** (double-import gotcha, прод-инцидент 2026-07-03):
   `import bot.main as bm` в хендлерах повторно исполнял файл → два Dispatcher'а + сбитый порядок,
   `on_text` глотал команды. Фикс — `sys.modules`-алиас + fail-fast гард на старте
   ([bot/main.py:379](bot/main.py#L379)). Если гард упал на старте — читай его сообщение, не
   обходи.

## Что требует рестарта, а что нет

- **НЕ требуют** рестарта: `/refresh` (OAuth-кэш), `/adduser` (рантайм-whitelist), `/grant`,
  `/model`, `/addadmin` — рантайм-таблицы/кэши.
- **Требуют** рестарта: изменения `.env` (env читается на старте; см. `docs/RUNBOOK_ACCESS.md`).

Подробности гочи запуска — память `aimash-run-bot-409-gotcha`.

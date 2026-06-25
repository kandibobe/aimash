"""Планировщик Aimash (APScheduler в общем event loop бота).

⛔ Золотое правило #3 (в КОДЕ): планировщик НИКОГДА не меняет аккаунт. Только чтение
(reports/read), уведомления в Telegram и очистка просроченных черновиков (reject). Никаких
mutations/бюджета по своей инициативе — авто-оптимизатора нет. См. scheduler.jobs (нет импорта
ads.mutations / execute_confirmed) и тест tests/test_scheduler.py::test_scheduler_never_imports_mutations.
"""

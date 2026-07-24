"""Пакет advisor: РЕКОМЕНДАЦИИ (advisory) + безопасная обучаемость.

Ключевой инвариант (golden rule #1/#3): рекомендация — ТОЛЬКО подсказка. Пакет НЕ импортирует
ads.mutations / ads.service и НЕ создаёт proposal: исполнение любого совета идёт ОТДЕЛЬНОЙ
командой через существующий confirm-гейт. Структура (образец scheduler/):
- rules.py     — чистое ядро (детекторы + ранжирование), БЕЗ сети/SDK, полностью тестируемо;
- render.py    — детерминированный RU/EN-рендер (fallback, работает без LLM);
- formulate.py — advisory-LLM (гладкая формулировка уже выбранных кодом кандидатов);
- service.py   — orchestration READ-ONLY (сбор отчёта + правила + персист);
- store.py     — CRUD таблиц recommendation / recommendation_feedback / recommendation_outcome;
- outcome.py / experience.py — Слой B (замер результата + опыт как код-сигнал), Фаза 2.
"""

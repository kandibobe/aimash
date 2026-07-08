"""Пакет аудита аккаунта («медосмотр») — детерминированный движок + рендер + fact-guard.

Единый источник ЧИСЕЛ аудита (health-score/деньги-под-риском считает КОД, не модель —
golden rule #4/#6). НЕ импортирует ads.mutations / ads.service (инвариант
tests/test_audit_engine.py::test_audit_never_imports_mutations). Это ТОЛЬКО диагностика:
движок не создаёт proposal и не меняет аккаунт — исполнение любого совета идёт отдельной
командой через confirm-гейт.
"""

from __future__ import annotations

from audit.engine import AuditResult, Finding, build_audit
from audit.render import render_audit
from audit.thresholds import grade_for

__all__ = ["AuditResult", "Finding", "build_audit", "render_audit", "grade_for"]

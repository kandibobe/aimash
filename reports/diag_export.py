"""Плейн-текстовые дампы диагностики для вложений (.txt): экспорт журнала ошибок (/diag «Экспорт»)
и детальная часть еженедельного дайджеста (scheduler.jobs.run_weekly_digest).

message/traceback в error_events УЖЕ редактированы на записи (core.errors._persist → redact_text),
поэтому дамп безопасен (секретов нет). Здесь только форматирование строк — без БД-доступа.
"""

from __future__ import annotations


def _when(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def build_error_events_text(rows) -> str:
    """Дамп error_events (duck-typed: .request_id/.where/.exc_type/.message/.traceback/.created_at)
    в читаемый .txt. rows — reverse-chron. Пусто ⇒ явная строка (не пустой файл)."""
    if not rows:
        return "Журнал ошибок пуст.\n"
    lines = [f"Журнал ошибок — {len(rows)} записей (время UTC)", "=" * 60, ""]
    for e in rows:
        lines.append(
            f"[{_when(getattr(e, 'created_at', None))}] {getattr(e, 'request_id', '')} "
            f"· {getattr(e, 'where', '')} · {getattr(e, 'exc_type', '')}"
        )
        cid = getattr(e, "customer_id", None)
        if cid:
            lines.append(f"  account: {cid}")
        msg = (getattr(e, "message", "") or "").strip()
        if msg:
            lines.append(f"  message: {msg}")
        tb = (getattr(e, "traceback", "") or "").strip()
        if tb:
            lines.append("  traceback:")
            lines.extend("    " + ln for ln in tb.splitlines())
        lines.append("")
    return "\n".join(lines)


def build_weekly_digest_file(errors, bug_rows, activity: dict, *, days: int = 7) -> str:
    """Детальный .txt для еженедельного дайджеста (§6/§15): активность + баг-репорты + полный дамп
    ошибок за неделю. Всё уже редактировано/без секретов. bug.text редактирован на записи."""
    st = (activity or {}).get("statuses", {}) or {}
    lines = [
        f"ЕЖЕНЕДЕЛЬНЫЙ ДАЙДЖЕСТ — за {days} дней (время UTC)",
        "=" * 60,
        "",
        "АКТИВНОСТЬ (audit_log):",
        f"  применено (applied):   {int(st.get('applied', 0))}",
        f"  сбоев (failed):        {int(st.get('failed', 0))}",
        f"  отклонено (rejected):  {int(st.get('rejected', 0))}",
        f"  на ревью (needs_review): {int(st.get('needs_review', 0))}",
        f"  создано кампаний:      {int((activity or {}).get('created_campaigns', 0))}",
        "",
        "БАГ-РЕПОРТЫ:",
    ]
    bugs = list(bug_rows or [])
    if not bugs:
        lines.append("  (нет)")
    else:
        for b in bugs:
            who = f"@{b.username}" if getattr(b, "username", None) else f"chat {b.chat_id}"
            lines.append(
                f"  #{b.id} [{getattr(b, 'status', '')}] {who} · {_when(getattr(b, 'created_at', None))}"
            )
            txt = (getattr(b, "text", "") or "").strip()
            if txt:
                lines.extend("      " + ln for ln in txt.splitlines())
    lines += ["", "ОШИБКИ (error_events):", ""]
    lines.append(build_error_events_text(errors))
    return "\n".join(lines)

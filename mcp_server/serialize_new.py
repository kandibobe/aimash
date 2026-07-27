"""Сериализаторы для новых MCP-инструментов (Блок 3, P1).
Каждый — чистая функция без побочек, принимает dataclass/объект и отдаёт JSON-совместимый dict."""

from __future__ import annotations

from typing import Any


def recommendation_dict(r) -> dict[str, Any]:
    """reports.queries.RecommendationRow → dict."""
    return {
        "type": r.type,
        "dismissed": bool(r.dismissed),
        "campaign": r.campaign,
        "resource_name": r.resource_name,
        "base_conversions": round(r.base_conversions, 1),
        "potential_conversions": round(r.potential_conversions, 1),
        "base_cost": round(r.base_cost, 2),
        "potential_cost": round(r.potential_cost, 2),
        "base_clicks": round(r.base_clicks, 1),
        "potential_clicks": round(r.potential_clicks, 1),
    }


def keyword_blocker_dict(b) -> dict[str, Any]:
    """find_keyword_blockers → dict per-blocker."""
    return {
        "campaign": b.get("campaign", ""),
        "ad_group": b.get("ad_group", ""),
        "negative_keyword": b.get("negative_keyword", ""),
        "negative_match_type": b.get("negative_match_type", ""),
        "negative_scope": b.get("negative_scope", ""),
        "blocked_keyword": b.get("blocked_keyword", ""),
        "blocked_match_type": b.get("blocked_match_type", ""),
        "risk": b.get("risk", "medium"),
    }


def change_event_dict(e) -> dict[str, Any]:
    """change_event-строка из GAQL → dict."""
    return {
        "resource": e.get("resource", ""),
        "resource_id": e.get("resource_id", ""),
        "change_type": e.get("change_type", ""),
        "campaign": e.get("campaign", ""),
        "changed_at": e.get("changed_at", ""),
        "user_agent": e.get("user_agent", ""),
    }


def simulate_mutation_result_dict(r) -> dict[str, Any]:
    """Результат validate_only-симуляции → dict."""
    return {
        "valid": bool(r.get("valid", True)),
        "errors": list(r.get("errors", [])),
        "warnings": list(r.get("warnings", [])),
        "summary": r.get("summary", ""),
        "would_change": dict(r.get("would_change", {})),
    }
"""Сериализация dataclass-результатов ридеров → JSON-совместимые dict для MCP-конверта.

Форму берём из bot-рендеров (`_make_audit_drill` и т.п.), НЕ импортируя их (там aiogram). Числа
метрик (cost/CTR/CPC/CPA/ROAS) считает КОД — через свойства `reports.queries.Metrics`, не модель
(§8.1). Раунды — как в METRIC_FORMATS: деньги/ценность 2 знака, CTR — доля (4 знака), счётчики целые.

Всё здесь — чистые функции без побочек и без обращения к SDK/БД (вызываются уже над готовыми
объектами). Пустые списки/словари сериализуются как есть («прочитали, ничего нет» — валидный факт).
"""

from __future__ import annotations

from typing import Any


def metrics_dict(m) -> dict[str, Any]:
    """reports.queries.Metrics → dict. Производные (ctr/avg_cpc/cpa/roas) считает КОД (свойства m)."""
    return {
        "impressions": int(m.impressions),
        "clicks": int(m.clicks),
        "ctr": round(m.ctr, 4),  # доля 0..1 (как METRIC_FORMATS "0.00%")
        "avg_cpc": round(m.avg_cpc, 2),
        "cost": round(m.cost, 2),
        "conversions": round(m.conversions, 2),
        "conv_value": round(m.conv_value, 2),
        "cpa": round(m.cpa, 2),
        "roas": round(m.roas, 2),
    }


def breakdown_rows(bd) -> list[dict[str, Any]]:
    """reports.queries.Breakdown → список строк. Каждая: измерения (по dim_headers) + метрики.
    dim_headers — RU-заголовки (система RU); значения-измерения зипуются с ними по позиции."""
    out: list[dict[str, Any]] = []
    for dims, m in bd.rows:
        out.append(
            {
                "dimensions": dict(zip(bd.dim_headers, dims)),
                "metrics": metrics_dict(m),
            }
        )
    return out


def breakdown_extra(bd) -> dict[str, Any]:
    """Верхнеуровневые поля конверта для Breakdown: заголовок + пометка усечения (без тихих обрезаний)."""
    extra: dict[str, Any] = {"title": bd.title, "dimensions_schema": list(bd.dim_headers)}
    if bd.note:
        extra["note"] = bd.note
    return extra


def search_term_dict(r) -> dict[str, Any]:
    return {
        "search_term": r.search_term,
        "campaign": r.campaign,
        "ad_group": r.ad_group,
        "keyword": r.keyword,
        "match_type": r.match_type,
        "metrics": metrics_dict(r.metrics),
    }


def impression_share_dict(r) -> dict[str, Any]:
    """ImpressionShareRow: доли 0..1 (search_is/потери), НЕ micros — отдаём как есть (округл. до 4)."""
    return {
        "campaign_id": r.campaign_id,
        "campaign_name": r.campaign_name,
        "channel_type": r.channel_type,
        "search_is": round(float(r.search_is), 4),
        "budget_lost_is": round(float(r.budget_lost_is), 4),
        "rank_lost_is": round(float(r.rank_lost_is), 4),
    }


def negative_keyword_dict(r) -> dict[str, Any]:
    return {
        "scope": r.scope,
        "campaign": r.campaign,
        "ad_group": r.ad_group,
        "text": r.text,
        "match_type": r.match_type,
        "list_name": r.list_name,
    }


def negatives_payload(info) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """NegativeKeywordsInfo → (rows, extra). shared_attachments: frozenset → list (JSON-сериализуемо)."""
    rows = [negative_keyword_dict(r) for r in info.rows]
    extra = {
        "shared_attachments": {k: sorted(v) for k, v in info.shared_attachments.items()},
    }
    return rows, extra


def child_account_dict(c) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "currency": c.currency,
        "manager": bool(c.manager),
        "level": int(c.level),
        "status": c.status,
    }


def keyword_idea_dict(k) -> dict[str, Any]:
    return {
        "text": k.text,
        "avg_monthly_searches": int(k.avg_monthly_searches),
        "competition": k.competition,
        "competition_index": int(k.competition_index),
        "low_bid": round(float(k.low_bid), 2),
        "high_bid": round(float(k.high_bid), 2),
        "avg_cpc": round(float(k.avg_cpc), 2),
        "peak_month": k.peak_month,
    }


def budget_dict(b) -> dict[str, Any]:
    return {
        "campaign_id": b.campaign_id,
        "campaign": b.campaign,
        "channel_type": b.channel_type,
        "status": b.status,
        "budget": round(float(b.budget), 2),
    }


def recent_action_dict(a) -> dict[str, Any]:
    """db.history.RecentAction → dict. decided_at → ISO-строка (JSON) или None."""
    return {
        "confirmation_id": a.confirmation_id,
        "operation": a.operation,
        "params": a.params,
        "summary": a.summary,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


def finding_dict(f) -> dict[str, Any]:
    """audit.engine.Finding → компактный dict. facts/evidence несут числа (→ code_numbers).
    one_tap считает КОД (свойство f.one_tap), не модель."""
    return {
        "check_id": f.check_id,
        "family": f.family,
        "severity": f.severity,
        "at_risk": round(float(f.at_risk), 2),
        "target_campaign": f.target_campaign,
        "suggested_operation": f.suggested_operation if f.one_tap else None,
        "one_tap": bool(f.one_tap),
        "facts": f.facts,
    }


def audit_payload(a) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """audit.engine.AuditResult → (findings, extra). extra несёт score/grade/итоги; findings — rows.
    families отдаём как есть (family→счётчики/at_risk/penalty). Тяжёлые срезы (report/audit_tables)
    НЕ включаем — это бумага для выгрузки, не для инструмента."""
    rows = [finding_dict(f) for f in a.findings]
    extra = {
        "customer_id": a.customer_id,
        "currency": a.currency,
        "score": a.score,
        "grade": a.grade,
        "total_spend": round(float(a.total_spend), 2),
        "at_risk": round(float(a.at_risk), 2),
        "has_activity": bool(a.has_activity),
        "optimization_score": a.optimization_score,
        "account_status": a.account_status,
        "measurement_gap": bool(a.measurement_gap),
        "lost_opportunity": round(float(a.lost_opportunity), 2),
        "score_model_version": a.score_model_version,
        "families": a.families,
    }
    return rows, extra


def profile_dict(p: dict) -> dict[str, Any]:
    """§20: клиентский профиль (из ClientProfileStore.get_by_account) → компактный dict.
    Содержит скаляры, контакты, услуги, число страниц сайта, дату краула.
    Поля для sitelinks (top_site_pages) не входят — их отдаёт get_profile_context."""
    return {
        "customer_id": p.get("customer_id", ""),
        "brand": p.get("brand"),
        "business_desc": p.get("business_desc"),
        "geo": p.get("geo"),
        "language": p.get("language"),
        "website": p.get("website"),
        "socials": p.get("socials", {}),
        "notes": p.get("notes"),
        "contacts_count": len(p.get("contacts", [])),
        "services_count": len(p.get("services", [])),
        "site_pages_count": p.get("site_pages_count", 0),
        "last_crawled_at": p.get("last_crawled_at").isoformat() if p.get("last_crawled_at") else None,
    }


def dossier_dict(d: dict | None) -> dict[str, Any] | None:
    """§20: досье клиента → компактный dict. None → профиль ещё не краулился."""
    if d is None:
        return None
    return {
        "customer_id": d.get("customer_id", ""),
        "version": d.get("version", 1),
        "status": d.get("status", "draft"),
        "has_markdown": bool(d.get("markdown")),
        "has_llm_context": bool(d.get("llm_context")),
        "created_at": d.get("created_at"),
    }

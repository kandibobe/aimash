"""simulate_mutation — нативная симуляция validate_only через Google Ads API.

Каждая функция строит мутационный запрос с `validate_only=True`, отправляет его
в Google Ads API и возвращает результат валидации. Ничего не создаёт/не меняет.

Только для read-only замков (ensure_read_allowed, не ensure_allowed). fail-closed:
если validate_only не выставлен — raise RuntimeError.
"""

from __future__ import annotations

from typing import Any

from ads.client import ensure_read_allowed
from core.ads_errors import error_code_names
from core.logging import log

# Google Ads SDK импортируется лениво внутри функций — модуль не падает при импорте
# на хосте без SDK (например, тесты без контейнера).


def simulate_budget_change(
    client,
    customer_id: str,
    budget_resource_name: str,
    new_budget_micros: int,
) -> dict[str, Any]:
    """Симуляция изменения shared-бюджета кампании через CampaignBudgetService
    с validate_only=True. Самый частый тип мутации — защита от неверных сумм.

    Аргументы:
        client — GoogleAdsClient
        customer_id — ID аккаунта
        budget_resource_name — resource_name бюджета (из resolve.find_campaign_by_name)
        new_budget_micros — новый дневной бюджет в micros

    Возвращает:
        {"valid": bool, "errors": list[str], "warnings": list[str], "summary": str}
    """
    ensure_read_allowed(customer_id)
    from google.ads.googleads.errors import GoogleAdsException

    cid = str(customer_id)
    op = client.get_type("CampaignBudgetOperation")
    budget = op.update
    budget.resource_name = budget_resource_name
    budget.amount_micros = int(new_budget_micros)

    # Маска: обновляем только amount_micros
    mask = client.get_type("FieldMask")
    mask.paths.append("amount_micros")
    op.update_mask.CopyFrom(mask)

    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = cid
    request.operations.append(op)
    request.validate_only = True

    # fail-closed: assert вырезается под -O, проверяем явно
    if request.validate_only is not True:
        raise RuntimeError(
            "validate_only не выставлен — симуляция отменена (fail-closed)"
        )

    try:
        client.get_service("CampaignBudgetService").mutate_campaign_budgets(
            request=request
        )
        return {
            "valid": True,
            "errors": [],
            "warnings": [],
            "summary": "Бюджет корректен: Google Ads API принял изменение (validate_only).",
        }
    except GoogleAdsException as e:
        codes = list(error_code_names(e))
        msgs = _failure_messages(e)
        errors = [f"{c}: {m}" for c, m in zip(codes, msgs)]
        log.info(
            "simulate_budget_change: refused codes=%s errors=%d", codes, len(errors)
        )
        return {
            "valid": False,
            "errors": errors,
            "warnings": [],
            "summary": f"Google Ads API отклонил изменение: {', '.join(codes[:3])}",
        }


def simulate_pause_campaign(
    client,
    customer_id: str,
    campaign_resource_name: str,
) -> dict[str, Any]:
    """Симуляция паузы кампании с validate_only=True."""
    ensure_read_allowed(customer_id)
    from google.ads.googleads.errors import GoogleAdsException

    cid = str(customer_id)
    op = client.get_type("CampaignOperation")
    campaign = op.update
    campaign.resource_name = campaign_resource_name
    campaign.status = client.enums.CampaignStatusEnum.PAUSED

    mask = client.get_type("FieldMask")
    mask.paths.append("status")
    op.update_mask.CopyFrom(mask)

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = cid
    request.operations.append(op)
    request.validate_only = True

    if request.validate_only is not True:
        raise RuntimeError(
            "validate_only не выставлен — симуляция отменена (fail-closed)"
        )

    try:
        client.get_service("CampaignService").mutate_campaigns(request=request)
        return {
            "valid": True,
            "errors": [],
            "warnings": [],
            "summary": "Пауза кампании корректна: Google Ads API принял изменение (validate_only).",
        }
    except GoogleAdsException as e:
        codes = list(error_code_names(e))
        msgs = _failure_messages(e)
        errors = [f"{c}: {m}" for c, m in zip(codes, msgs)]
        return {
            "valid": False,
            "errors": errors,
            "warnings": [],
            "summary": f"Google Ads API отклонил паузу: {', '.join(codes[:3])}",
        }


def _failure_messages(exc) -> list[str]:
    """Извлекаем человекочитаемые сообщения из GoogleAdsException.failure.errors."""
    try:
        return [str(err.message or "")[:200] for err in exc.failure.errors[:3]]
    except Exception:  # noqa: BLE001
        return [str(exc)[:200]]
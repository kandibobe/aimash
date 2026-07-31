"""Validated contracts shared by the operational control-plane services."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["info", "warning", "critical"]
DecisionStatus = Literal[
    "new", "acknowledged", "approved", "rejected", "snoozed", "applied", "expired"
]
IncidentStatus = Literal["open", "acknowledged", "snoozed", "resolved"]


def _customer_id(value: str) -> str:
    value = "".join(ch for ch in str(value) if ch.isdigit())
    if not 6 <= len(value) <= 20:
        raise ValueError("customer_id must contain 6..20 digits")
    return value


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str
    source: str = Field(min_length=1, max_length=32)
    source_ref: str | None = Field(default=None, max_length=96)
    category: str = Field(min_length=1, max_length=32)
    severity: Severity
    title: str = Field(min_length=1, max_length=255)
    rationale: str = Field(min_length=1, max_length=8000)
    recommended_action: str = Field(min_length=1, max_length=8000)
    recommended_operation: str | None = Field(default=None, max_length=64)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    chat_id: int | None = None
    expires_at: datetime | None = None
    fingerprint_fields: dict[str, Any] = Field(default_factory=dict)

    _normalize_customer = field_validator("customer_id")(_customer_id)


class IncidentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str
    kind: str = Field(min_length=1, max_length=32)
    severity: Severity
    title: str = Field(min_length=1, max_length=255)
    evidence: dict[str, Any] = Field(default_factory=dict)
    decision_uid: str | None = Field(default=None, max_length=64)
    fingerprint_fields: dict[str, Any] = Field(default_factory=dict)

    _normalize_customer = field_validator("customer_id")(_customer_id)


class BudgetPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str
    scope_type: Literal["account", "campaign", "portfolio"]
    scope_id: str = Field(min_length=1, max_length=96)
    name: str = Field(min_length=1, max_length=255)
    period_start: date
    period_end: date
    currency: str = Field(min_length=3, max_length=8)
    planned_spend_micros: int = Field(gt=0)
    monthly_ceiling_micros: int | None = Field(default=None, gt=0)
    daily_ceiling_micros: int | None = Field(default=None, gt=0)
    source: Literal["manual", "csv", "sheets", "api"] = "manual"
    created_by: int | None = None

    _normalize_customer = field_validator("customer_id")(_customer_id)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def valid_period_and_ceiling(self):
        if self.period_end < self.period_start:
            raise ValueError("period_end must be on or after period_start")
        if self.monthly_ceiling_micros and self.monthly_ceiling_micros < self.planned_spend_micros:
            raise ValueError("monthly ceiling cannot be below planned spend")
        return self


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    customer_id: str
    name: str = Field(min_length=1, max_length=255)
    hypothesis: str = Field(min_length=1, max_length=8000)
    control: dict[str, Any]
    treatment: dict[str, Any]
    primary_metric: Literal["cpa", "cvr", "roas", "conversions", "revenue"]
    success_direction: Literal["up", "down"]
    success_threshold: float = Field(gt=0)
    minimum_sample: int = Field(default=0, ge=0)
    start_date: date
    end_date: date
    rollback_trigger: dict[str, Any] | None = None
    created_by: int | None = None

    _normalize_customer = field_validator("customer_id")(_customer_id)

    @model_validator(mode="after")
    def valid_window(self):
        if self.end_date <= self.start_date:
            raise ValueError("experiment end_date must be after start_date")
        if self.control == self.treatment:
            raise ValueError("control and treatment must differ")
        return self


class RevenueEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=512)
    customer_id: str
    campaign_id: str | None = Field(default=None, max_length=32)
    channel: Literal["google", "meta", "microsoft", "tiktok", "other"] = "google"
    stage: str = Field(min_length=1, max_length=32)
    qualified: bool = False
    revenue_micros: int = Field(default=0, ge=0)
    currency: str = Field(min_length=3, max_length=8)
    occurred_at: datetime

    _normalize_customer = field_validator("customer_id")(_customer_id)

    @field_validator("currency")
    @classmethod
    def revenue_currency(cls, value: str) -> str:
        return value.upper()


class ChannelMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    channel: Literal["google", "meta", "microsoft", "tiktok", "other"]
    customer_id: str
    external_account_id: str = Field(min_length=1, max_length=96)
    campaign_id: str = Field(default="", max_length=96)
    metric_date: date
    spend_micros: int = Field(default=0, ge=0)
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    conversions: float = Field(default=0.0, ge=0.0)
    revenue_micros: int = Field(default=0, ge=0)
    currency: str = Field(min_length=3, max_length=8)
    source: str = Field(min_length=1, max_length=32)

    _normalize_customer = field_validator("customer_id")(_customer_id)

    @field_validator("currency")
    @classmethod
    def metric_currency(cls, value: str) -> str:
        return value.upper()

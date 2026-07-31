"""Explainable-copilot response contract and deterministic plain-text renderer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=255)
    observed_value: str = Field(min_length=1, max_length=255)


class CopilotBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    what_happened: str = Field(min_length=1, max_length=4000)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=20)
    likely_causes: list[str] = Field(default_factory=list, max_length=10)
    recommended_next_step: str = Field(min_length=1, max_length=4000)
    what_not_to_do: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    confidence_basis: Literal["measured", "correlated", "inferred"]
    approval_required: bool
    proposal_confirmation_id: str | None = Field(default=None, max_length=64)
    execution_status: Literal["not_proposed", "proposed", "applied", "failed", "needs_review"]
    audit_reference: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def status_has_proof(self):
        if self.execution_status == "proposed" and not self.proposal_confirmation_id:
            raise ValueError("proposed status requires proposal_confirmation_id")
        if (
            self.execution_status in {"applied", "failed", "needs_review"}
            and not self.audit_reference
        ):
            raise ValueError("execution status requires audit_reference")
        return self


def render_brief(brief: CopilotBrief) -> str:
    confidence = round(brief.confidence * 100)
    evidence = "\n".join(
        f"- {item.claim}: {item.observed_value} ({item.source})" for item in brief.evidence
    )
    causes = "\n".join(f"- {item}" for item in brief.likely_causes) or "- Not established"
    approval = "required" if brief.approval_required else "not required"
    return (
        f"What happened\n{brief.what_happened}\n\nEvidence\n{evidence}\n\n"
        f"Likely causes\n{causes}\n\nRecommended next step\n{brief.recommended_next_step}\n\n"
        f"What not to do\n{brief.what_not_to_do}\n\n"
        f"Confidence: {confidence}% ({brief.confidence_basis}); approval: {approval}; "
        f"status: {brief.execution_status}"
    )

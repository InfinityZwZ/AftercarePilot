from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, field_validator

from src.domain.clock import utc
from src.domain.schemas import ReturnReason, ToolResult

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Language = Literal["zh", "en"]
ProposalStatus = Literal["analyzing", "needs_info", "pending_approval", "approved", "rejected", "superseded", "expired", "executed", "failed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Evidence(StrictModel):
    call_id: str
    tool_call_id: str
    name: str
    args: dict[str, Any]
    result: ToolResult
    duration_ms: float = Field(ge=0)


class Triage(StrictModel):
    intent: Literal["order_status", "shipping_tracking", "return_request", "return_policy", "general", "unsupported"]
    order_id: str | None = Field(default=None, max_length=64)
    reason: ReturnReason | None = None
    language: Language = "zh"
    missing_fields: list[str] = Field(default_factory=list)


class ModelDraft(StrictModel):
    kind: Literal["return_request", "reply_only", "clarification", "handoff"]
    summary: str = Field(min_length=1, max_length=2000)
    draft_reply: str = Field(min_length=1, max_length=8000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=6)


class PayloadBase(StrictModel):
    proposal_id: UUID
    request_id: UUID
    revision: StrictInt = Field(ge=1)
    order_id: str | None = None
    policy_id: str | None = None
    summary: str = Field(min_length=1, max_length=2000)
    draft_reply: str = Field(min_length=1, max_length=8000)
    language: Language
    evidence_refs: list[str]
    evidence: list[Evidence]


class ReturnPayload(PayloadBase):
    kind: Literal["return_request"]
    action: Literal["create_aftersales_ticket"]
    order_id: str
    policy_id: str
    reason: ReturnReason
    requested_amount_cents: StrictInt = Field(ge=0)
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    order_version: StrictInt = Field(ge=1)
    order_snapshot_hash: Hash
    policy_version: StrictInt = Field(ge=1)
    policy_hash: Hash
    deadline_at: datetime
    eligibility: Literal["eligible"]

    @field_validator("deadline_at")
    @classmethod
    def deadline_utc(cls, value):
        return utc(value)


class ReplyPayload(PayloadBase):
    kind: Literal["reply_only"]
    action: Literal["none"] = "none"
    facts: dict[str, Any] = Field(default_factory=dict)


class ClarificationPayload(PayloadBase):
    kind: Literal["clarification"]
    action: Literal["none"] = "none"
    missing_fields: list[str]


class HandoffPayload(PayloadBase):
    kind: Literal["handoff"]
    action: Literal["none"] = "none"
    facts: dict[str, Any] = Field(default_factory=dict)
    handoff_reason: str


ProposalPayload = Annotated[ReturnPayload | ReplyPayload | ClarificationPayload | HandoffPayload, Field(discriminator="kind")]
PAYLOAD_ADAPTER = TypeAdapter(ProposalPayload)


class OrderArgs(StrictModel):
    order_id: str = Field(min_length=1, max_length=64)


class PolicyArgs(StrictModel):
    policy_id: str = Field(min_length=1, max_length=64)


class EligibilityArgs(OrderArgs):
    reason: ReturnReason | None = None


READ_ARGS = {"get_order": OrderArgs, "get_return_policy": PolicyArgs, "check_return_eligibility": EligibilityArgs}

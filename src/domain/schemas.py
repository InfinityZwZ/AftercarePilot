from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReturnReason = Literal["defective", "wrong_item", "not_as_described", "changed_mind", "damaged", "too_late"]
RETURN_REASONS = ("defective", "wrong_item", "not_as_described", "changed_mind", "damaged", "too_late")


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OrderFacts(Fact):
    order_id: str
    customer_ref: str
    status: str
    total_cents: int
    currency: str
    items: list[dict[str, Any]]
    created_at: datetime
    delivered_at: datetime | None
    shipment: dict[str, Any] | None
    policy_id: str
    version: int
    updated_at: datetime
    snapshot_hash: str


class PolicyFacts(Fact):
    policy_id: str
    version: int
    name: str
    return_window_days: int
    allowed_reasons: list[str]
    scope: str
    enabled: bool
    description: str
    updated_at: datetime
    policy_hash: str


class TicketFacts(Fact):
    ticket_id: str
    ticket_no: str
    status: str


class EligibilityFacts(Fact):
    order_id: str
    eligibility: Literal["eligible", "ineligible", "unknown"]
    reason_codes: list[str]
    evaluated_at: datetime
    deadline_at: datetime | None = None
    requested_amount_cents: int | None = None
    currency: str | None = None
    order_version: int | None = None
    order_snapshot_hash: str | None = None
    policy_id: str | None = None
    policy_version: int | None = None
    policy_hash: str | None = None
    existing_ticket: TicketFacts | None = None


class ToolError(Fact):
    code: str
    message: str


class ToolMeta(Fact):
    data_source: Literal["demo_postgresql"] = "demo_postgresql"
    duration_ms: float = Field(default=0, ge=0)


class ToolResult(Fact):
    ok: bool
    code: str
    retryable: bool = False
    data: dict[str, Any] | None
    error: ToolError | None = None
    meta: ToolMeta = Field(default_factory=ToolMeta)

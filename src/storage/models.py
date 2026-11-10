from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CHAR, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.storage.base import Base

TIMESTAMP = DateTime(timezone=True)


class ReturnPolicy(Base):
    __tablename__ = "return_policies"
    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    return_window_days: Mapped[int] = mapped_column(Integer)
    allowed_reasons: Mapped[list[str]] = mapped_column(JSONB)
    scope: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    description: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    __table_args__ = (
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("return_window_days BETWEEN 1 AND 90", name="window"),
        CheckConstraint("jsonb_typeof(allowed_reasons) = 'array'", name="reasons_array"),
        CheckConstraint("allowed_reasons <> '[]'::jsonb AND allowed_reasons <@ '[\"defective\",\"wrong_item\",\"not_as_described\",\"changed_mind\",\"damaged\",\"too_late\"]'::jsonb", name="reasons_valid"),
        CheckConstraint("scope = 'whole_order'", name="scope"),
    )


class Order(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_ref: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    total_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    shipment: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("return_policies.policy_id"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    __table_args__ = (
        CheckConstraint("status IN ('confirmed','shipped','delivered','cancelled')", name="status"),
        CheckConstraint("total_cents >= 0", name="amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_array"),
        CheckConstraint("shipment IS NULL OR jsonb_typeof(shipment) = 'object'", name="shipment_object"),
        CheckConstraint("version >= 1", name="version"),
    )


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    revision: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    status: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(24))
    source_question: Mapped[str] = mapped_column(Text)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.order_id"))
    policy_id: Mapped[str | None] = mapped_column(ForeignKey("return_policies.policy_id"))
    entities: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    payload_hash: Mapped[str | None] = mapped_column(CHAR(64))
    approved_payload_hash: Mapped[str | None] = mapped_column(CHAR(64))
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    archived_by: Mapped[str | None] = mapped_column(String(64))
    management_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    error: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    __table_args__ = (
        UniqueConstraint("request_id", "revision", name="uq_proposals_request_revision"),
        Index("uq_proposals_current", "request_id", unique=True, postgresql_where=text("is_current")),
        Index("ix_proposals_status_created", "status", "created_at"),
        Index("ix_requests_active_updated", "updated_at", "id", postgresql_where=text("is_current AND archived_at IS NULL")),
        Index("ix_requests_archived_updated", "updated_at", "id", postgresql_where=text("is_current AND archived_at IS NOT NULL")),
        CheckConstraint("management_version >= 0", name="management_version"),
        CheckConstraint("(archived_at IS NULL) = (archived_by IS NULL)", name="archive_pair"),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint("status IN ('analyzing','needs_info','pending_approval','approved','rejected','superseded','expired','executed','failed')", name="status"),
        CheckConstraint("kind IN ('return_request','reply_only','clarification','handoff')", name="kind"),
        CheckConstraint("jsonb_typeof(entities) = 'object' AND jsonb_typeof(payload) = 'object'", name="json_objects"),
        CheckConstraint("status NOT IN ('pending_approval','approved','executed') OR payload_hash IS NOT NULL", name="payload_hash_required"),
        CheckConstraint("status NOT IN ('approved','executed') OR (approved_payload_hash IS NOT NULL AND approved_payload_hash = payload_hash AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL)", name="approval_binding"),
    )


class AftersalesTicket(Base):
    __tablename__ = "aftersales_tickets"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    ticket_no: Mapped[str] = mapped_column(String(64), unique=True)
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id"), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"))
    type: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(32))
    requested_amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3))
    status: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), unique=True)
    approved_reply: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    __table_args__ = (
        Index("uq_active_aftersales_order", "order_id", unique=True,
              postgresql_where=text("status IN ('requested','processing')")),
        Index("ix_tickets_created", "created_at"),
        CheckConstraint("type = 'return_request'", name="type"),
        CheckConstraint("reason IN ('defective','wrong_item','not_as_described','changed_mind','damaged','too_late')", name="reason"),
        CheckConstraint("requested_amount_cents >= 0", name="amount"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("status IN ('requested','processing','closed','cancelled')", name="status"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    proposal_id: Mapped[UUID | None] = mapped_column(ForeignKey("proposals.id"))
    ticket_id: Mapped[UUID | None] = mapped_column(ForeignKey("aftersales_tickets.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    __table_args__ = (
        Index("ix_audit_request_created", "request_id", "created_at"),
        Index("ix_audit_execution", "proposal_id", "ticket_id", "event_type"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details_object"),
    )

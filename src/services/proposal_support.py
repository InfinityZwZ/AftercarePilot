from uuid import UUID

from sqlalchemy import select
from pydantic import ValidationError

from src.domain.clock import utc
from src.domain.exceptions import AppError
from src.domain.hashing import canonical_hash
from src.domain.request_management import management_metadata, require_active
from src.domain.proposals import PAYLOAD_ADAPTER
from src.domain.proposals import ReturnPayload
from src.storage.models import AuditLog, Proposal


def locked_proposal(session, proposal_id: UUID):
    row = session.scalar(select(Proposal).where(Proposal.id == proposal_id).with_for_update())
    if row is None:
        raise AppError("PROPOSAL_NOT_FOUND", "Proposal not found", 404)
    return row


def checked_payload(row, revision: int, expected_hash: str):
    if row.revision != revision or row.payload_hash != expected_hash:
        raise AppError("STALE_REVISION", "The viewed revision or hash is no longer valid")
    try:
        digest = canonical_hash(row.payload)
        payload = PAYLOAD_ADAPTER.validate_python(row.payload)
    except (ValueError, ValidationError, TypeError):
        raise AppError("PAYLOAD_TAMPERED", "Stored Proposal content is invalid") from None
    if digest != row.payload_hash or (payload.proposal_id, payload.request_id, payload.revision, payload.kind,
                                      payload.order_id, payload.policy_id) != (
            row.id, row.request_id, row.revision, row.kind, row.order_id, row.policy_id):
        raise AppError("PAYLOAD_TAMPERED", "Stored Proposal content does not match its identity or hash")
    if isinstance(payload, ReturnPayload):
        from src.agent.evidence import validate_return_payload
        try:
            validate_return_payload(payload)
        except AppError:
            raise AppError("EVIDENCE_INCONSISTENT", "Stored return evidence is inconsistent") from None
    return payload


def require_current(row):
    if not row.is_current or row.status == "superseded":
        raise AppError("STALE_REVISION", "This Proposal has been superseded")
    require_active(row)


def require_unexpired(row, now):
    if utc(now) > utc(row.expires_at):
        raise AppError("PROPOSAL_EXPIRED", "Proposal expired; analyze and review again")


def audit(session, row, event: str, actor: str, now, details=None, ticket_id=None):
    session.add(AuditLog(request_id=row.request_id, proposal_id=row.id, ticket_id=ticket_id,
                         event_type=event, actor_id=actor, details=details or {}, created_at=utc(now)))


def proposal_dto(row):
    payload = row.payload or {}
    return dict(proposal_id=str(row.id), request_id=str(row.request_id), revision=row.revision,
                is_current=row.is_current, status=row.status, kind=row.kind,
                source_question=row.source_question, entities=row.entities,
                payload=payload, payload_hash=row.payload_hash,
                approved_payload_hash=row.approved_payload_hash, reviewer_id=row.reviewer_id,
                reviewed_at=utc(row.reviewed_at).isoformat() if row.reviewed_at else None,
                rejection_reason=row.rejection_reason, expires_at=utc(row.expires_at).isoformat(),
                created_at=utc(row.created_at).isoformat(), updated_at=utc(row.updated_at).isoformat(),
                error=row.error, draft_reply=payload.get("draft_reply", ""),
                usage=row.entities.get("usage", {}), synthetic_fixture=bool(payload.get("fixture")), **management_metadata(row))

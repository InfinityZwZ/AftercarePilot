import logging
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.domain.clock import SystemClock, utc
from src.domain.exceptions import AppError
from src.domain.hashing import canonical_hash
from src.domain.proposals import ReturnPayload
from src.services.eligibility_service import EligibilityService
from src.services.proposal_support import audit, checked_payload, locked_proposal, require_current, require_unexpired
from src.storage import database
from src.storage.models import AftersalesTicket, AuditLog, Order, Proposal, ReturnPolicy
from src.storage.repositories import get_active_ticket, get_order, get_policy

logger = logging.getLogger(__name__)


def ticket_dto(row):
    return dict(id=str(row.id), ticket_no=row.ticket_no, proposal_id=str(row.proposal_id), order_id=row.order_id,
                type=row.type, reason=row.reason, requested_amount_cents=row.requested_amount_cents,
                currency=row.currency, status=row.status, idempotency_key=row.idempotency_key,
                approved_reply=row.approved_reply, created_by=row.created_by, created_at=utc(row.created_at).isoformat())


def execution_key(row):
    return canonical_hash({"proposal_id": str(row.id), "approved_hash": row.approved_payload_hash,
                           "action": "create_aftersales_ticket"})


class AftersalesService:
    """The only application ticket-writing function; never bound to the model."""
    def __init__(self, sessions=None, clock=None, *, fault_hook=None):
        self._sessions = sessions
        self.clock = clock or SystemClock()
        self._fault_hook = fault_hook

    @property
    def sessions(self):
        return self._sessions or database.session_factory()

    def _fault(self, phase, session):
        # Injection seam for real transaction/response-loss tests, not API input.
        if self._fault_hook:
            self._fault_hook(phase, session)

    def _verified(self, session, row, ticket, payload):
        audits = list(session.scalars(select(AuditLog).where(AuditLog.proposal_id == row.id,
                                                          AuditLog.ticket_id == ticket.id,
                                                          AuditLog.event_type == "ticket_created")))
        matching = (row.status == "executed" and row.approved_payload_hash == row.payload_hash and
                    row.reviewer_id is not None and row.reviewed_at is not None and isinstance(payload, ReturnPayload) and
                    ticket.order_id == payload.order_id and ticket.type == "return_request" and ticket.reason == payload.reason and
                    ticket.requested_amount_cents == payload.requested_amount_cents and ticket.currency == payload.currency and
                    ticket.approved_reply == payload.draft_reply and ticket.idempotency_key == execution_key(row) and
                    len(audits) == 1 and audits[0].request_id == row.request_id and audits[0].actor_id == ticket.created_by and
                    audits[0].details.get("approved_payload_hash") == row.approved_payload_hash and
                    audits[0].details.get("idempotency_key") == ticket.idempotency_key)
        if not matching:
            raise AppError("EXECUTION_INCONSISTENT", "Ticket, Proposal and success audit disagree; inspect the original request", 500)
        return dict(status="executed", proposal_id=str(row.id), proposal_status=row.status, ticket=ticket_dto(ticket),
                    verification={"ticket_row_found": True, "proposal_executed": True, "success_audit_found": True})

    def _execute_transaction(self, proposal_id, revision, expected_hash, operator, flags, *, confirm=False):
        result = None
        with self.sessions() as session:
            with session.begin():
                row = locked_proposal(session, proposal_id)
                payload = checked_payload(row, revision, expected_hash)
                existing = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == row.id))
                if existing:
                    return self._verified(session, row, existing, payload)
                require_current(row)
                if row.status == "expired":
                    raise AppError("PROPOSAL_EXPIRED", "Proposal expired; analyze and review again")
                if confirm and row.status == "pending_approval":
                    if not isinstance(payload, ReturnPayload):
                        raise AppError("NOT_EXECUTABLE", "Only eligible return requests can be confirmed for execution", 422)
                    now = utc(self.clock.now())
                    require_unexpired(row, now)
                    row.status, row.reviewer_id, row.reviewed_at, row.updated_at = "approved", operator, now, now
                    row.approved_payload_hash = row.payload_hash
                    audit(session, row, "proposal_approved", operator, now,
                          {"revision": row.revision, "payload_hash": row.payload_hash})
                    session.flush()
                    self._fault("after_approval_flush", session)
                if row.status != "approved" or row.approved_payload_hash != row.payload_hash or not row.reviewer_id or not row.reviewed_at:
                    raise AppError("APPROVAL_REQUIRED", "This exact Proposal must be approved before execution", 403)
                if not isinstance(payload, ReturnPayload):
                    raise AppError("NOT_EXECUTABLE", "Only an eligible return_request can create a ticket", 422)
                require_unexpired(row, self.clock.now())
                order_row = session.scalar(select(Order).where(Order.order_id == row.order_id).with_for_update())
                # Shared policy lock protects a coherent rule snapshot without serializing unrelated orders.
                policy_row = session.scalar(select(ReturnPolicy).where(ReturnPolicy.policy_id == row.policy_id).with_for_update(read=True))
                require_unexpired(row, self.clock.now())
                order, policy = get_order(session, row.order_id), get_policy(session, row.policy_id)
                if (order_row is None or policy_row is None or order.snapshot_hash != payload.order_snapshot_hash or
                        policy.policy_hash != payload.policy_hash or order.version != payload.order_version or
                        policy.version != payload.policy_version or order.total_cents != payload.requested_amount_cents or
                        order.currency != payload.currency or order.policy_id != payload.policy_id):
                    raise AppError("STALE_PROPOSAL", "Order or policy facts changed; analyze and approve a new Proposal")
                eligibility = EligibilityService().check_return_eligibility(session, row.order_id, payload.reason, self.clock)
                if eligibility.eligibility != "eligible":
                    code = eligibility.reason_codes[0] if eligibility.reason_codes else "RETURN_INELIGIBLE"
                    details = {"existing_ticket": eligibility.existing_ticket.model_dump(mode="json")} if eligibility.existing_ticket else {}
                    raise AppError(code, "Current facts no longer permit this return request", details=details)
                now = utc(self.clock.now())
                require_unexpired(row, now)
                ticket_id = uuid4()
                ticket = AftersalesTicket(id=ticket_id, ticket_no=f"AS-{ticket_id}", proposal_id=row.id,
                                         order_id=order.order_id, type="return_request", reason=payload.reason,
                                         requested_amount_cents=order.total_cents, currency=order.currency, status="requested",
                                         idempotency_key=execution_key(row), approved_reply=payload.draft_reply,
                                         created_by=operator, created_at=now)
                session.add(ticket)
                session.flush()
                self._fault("after_ticket_flush", session)
                row.status, row.updated_at = "executed", now
                session.flush()
                self._fault("after_status_flush", session)
                audit(session, row, "ticket_created", operator, now,
                      {"approved_payload_hash": row.approved_payload_hash, "idempotency_key": ticket.idempotency_key,
                       "requested_amount_cents": ticket.requested_amount_cents, "currency": ticket.currency}, ticket_id=ticket.id)
                session.flush()
                self._fault("after_audit_flush", session)
                result = self._verified(session, row, ticket, payload)
                self._fault("before_commit", session)
                require_unexpired(row, self.clock.now())
                flags["committing"] = True
            flags["committed"] = True
            self._fault("after_commit", session)
        return result

    def _failed_audit(self, proposal_id, revision, expected_hash, operator, error, *, confirm=False):
        # Called only after the failed work transaction has exited/rolled back.
        try:
            with self.sessions.begin() as session:
                row = session.scalar(select(Proposal).where(Proposal.id == proposal_id).with_for_update())
                if row:
                    if error.status == 202 and row.status != 'executed' and row.is_current:
                        row.error = {'code': 'EXECUTION_UNKNOWN', 'message': 'Query the original execution result before proceeding'}
                        row.updated_at = utc(self.clock.now())
                    if (error.code in ("PROPOSAL_EXPIRED", "RETURN_WINDOW_EXPIRED", "STALE_PROPOSAL") and
                            (row.status == "approved" or (confirm and row.status == "pending_approval")) and
                            row.is_current and row.revision == revision and row.payload_hash == expected_hash):
                        row.status, row.updated_at = "expired", utc(self.clock.now())
                        row.error = {"code": error.code, "message": error.message}
                        audit(session, row, "proposal_expired", operator, self.clock.now(), row.error)
                    audit(session, row, "execution_unknown" if error.status == 202 else "execution_failed", operator, self.clock.now(),
                          {"error_code": error.code, "expected_revision": revision, "expected_payload_hash": expected_hash})
        except Exception:
            logger.error("Failed to persist execution failure audit; code=%s proposal=%s", error.code, proposal_id)

    def _resolve_unique_conflict(self, proposal_id, revision, expected_hash):
        # Never reuse a failed Session after PostgreSQL unique violation.
        with self.sessions.begin() as session:
            row = locked_proposal(session, proposal_id)
            payload = checked_payload(row, revision, expected_hash)
            own = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == proposal_id))
            if own:
                return self._verified(session, row, own, payload)
            other = get_active_ticket(session, row.order_id)
            if other:
                raise AppError("RETURN_ALREADY_EXISTS", "Another Proposal already created an active ticket",
                               details={"existing_ticket": other.model_dump(mode="json")})
            raise AppError("EXECUTION_CONFLICT", "A conflicting database record prevented execution; reload")

    def confirm_and_create(self, proposal_id, revision, expected_hash, operator):
        return self.execute_proposal(proposal_id, revision, expected_hash, operator, confirm=True)

    def execute_proposal(self, proposal_id, revision, expected_hash, operator, *, confirm=False):
        flags = {"committing": False, "committed": False}
        try:
            if confirm:
                return self._execute_transaction(proposal_id, revision, expected_hash, operator, flags, confirm=True)
            return self._execute_transaction(proposal_id, revision, expected_hash, operator, flags)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                try:
                    return self._resolve_unique_conflict(proposal_id, revision, expected_hash)
                except AppError as error:
                    self._failed_audit(proposal_id, revision, expected_hash, operator, error, confirm=confirm)
                    raise
            error = AppError("EXECUTION_FAILED", "Ticket transaction failed and was rolled back", 500)
        except AppError as error:
            self._failed_audit(proposal_id, revision, expected_hash, operator, error, confirm=confirm)
            raise
        except Exception:
            if flags["committing"] or flags["committed"]:
                error = AppError("EXECUTION_UNKNOWN", "Commit result needs verification using the original Proposal", 202,
                                 details={"proposal_id": str(proposal_id), "next_action": "query_execution"})
            else:
                error = AppError("EXECUTION_FAILED", "Ticket transaction failed and was rolled back", 500)
        self._failed_audit(proposal_id, revision, expected_hash, operator, error, confirm=confirm)
        raise error from None

    def get_execution(self, proposal_id):
        try:
            with self.sessions() as session:
                session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                row = session.scalar(select(Proposal).where(Proposal.id == proposal_id).with_for_update(read=True))
                if not row:
                    raise AppError("PROPOSAL_NOT_FOUND", "Proposal not found", 404)
                ticket = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == row.id))
                if ticket:
                    payload = checked_payload(row, row.revision, row.payload_hash)
                    return self._verified(session, row, ticket, payload)
                if row.status == "executed":
                    raise AppError("EXECUTION_INCONSISTENT", "Executed Proposal has no ticket", 500)
                return dict(status="not_executed", proposal_id=str(row.id), proposal_status=row.status, ticket=None,
                            verification={"ticket_row_found": False, "proposal_executed": False, "success_audit_found": False},
                            next_action="execute_same_proposal" if row.status == "approved" else "analyze_again" if row.status in ("expired", "failed", "rejected") else "review_proposal")
        except DBAPIError:
            raise AppError("EXECUTION_UNKNOWN", "Database unavailable; execution result cannot be determined", 503,
                           retryable=True, details={"next_action": "query_execution"}) from None

    def get_ticket(self, ticket_id):
        with self.sessions() as session:
            row = session.get(AftersalesTicket, ticket_id)
            if not row:
                raise AppError("TICKET_NOT_FOUND", "Ticket not found", 404)
            return ticket_dto(row)

    def list_tickets(self, *, limit=20, offset=0):
        with self.sessions() as session:
            return [ticket_dto(row) for row in session.scalars(select(AftersalesTicket).order_by(AftersalesTicket.created_at.desc()).limit(limit).offset(offset))]

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain.clock import utc
from src.domain.hashing import canonical_hash
from src.domain.policy import policy_description
from src.domain.schemas import OrderFacts, PolicyFacts, TicketFacts
from src.storage.models import AftersalesTicket, Order, ReturnPolicy


def get_order(session: Session, order_id: str) -> OrderFacts | None:
    row = session.get(Order, order_id)
    if row is None:
        return None
    snapshot = dict(order_id=row.order_id, status=row.status, total_cents=row.total_cents,
                    currency=row.currency, delivered_at=utc(row.delivered_at).isoformat() if row.delivered_at else None,
                    policy_id=row.policy_id, version=row.version)
    return OrderFacts(**snapshot, customer_ref=row.customer_ref, items=row.items,
                      created_at=row.created_at, shipment=row.shipment, updated_at=row.updated_at,
                      snapshot_hash=canonical_hash(snapshot))


def get_policy(session: Session, policy_id: str) -> PolicyFacts | None:
    row = session.get(ReturnPolicy, policy_id)
    if row is None:
        return None
    rules = dict(policy_id=row.policy_id, version=row.version, return_window_days=row.return_window_days,
                 allowed_reasons=sorted(row.allowed_reasons), scope=row.scope, enabled=row.enabled)
    return PolicyFacts(**rules, name=row.name, updated_at=row.updated_at,
                       description=policy_description(row.return_window_days, row.allowed_reasons, row.scope, row.enabled),
                       policy_hash=canonical_hash(rules))


def get_active_ticket(session: Session, order_id: str) -> TicketFacts | None:
    row = session.scalar(select(AftersalesTicket).where(
        AftersalesTicket.order_id == order_id, AftersalesTicket.status.in_(("requested", "processing"))))
    return TicketFacts(ticket_id=str(row.id), ticket_no=row.ticket_no, status=row.status) if row else None

from sqlalchemy.orm import Session

from src.domain.clock import Clock
from src.domain.policy import evaluate_eligibility
from src.storage.repositories import get_active_ticket, get_order, get_policy


class EligibilityService:
    def check_return_eligibility(self, session: Session, order_id: str, reason: str | None, clock: Clock):
        order = get_order(session, order_id)
        policy = get_policy(session, order.policy_id) if order else None
        ticket = get_active_ticket(session, order_id) if order else None
        return evaluate_eligibility(order, policy, ticket, reason, clock, order_id=order_id)

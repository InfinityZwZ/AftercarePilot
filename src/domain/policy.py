from datetime import timedelta

from src.domain.clock import Clock, utc
from src.domain.schemas import EligibilityFacts, OrderFacts, PolicyFacts, RETURN_REASONS, TicketFacts


def policy_description(days: int, reasons: list[str], scope: str, enabled: bool) -> str:
    return (f"Synthetic demo policy: {days} days from confirmed delivery, including the deadline. "
            f"Scope: {scope}. Allowed reasons: {', '.join(sorted(reasons))}. "
            f"Policy {'enabled' if enabled else 'disabled'}. "
            "The requested amount is the whole order total. An accepted request is not a refund.")


def evaluate_eligibility(order: OrderFacts | None, policy: PolicyFacts | None,
                         existing_ticket: TicketFacts | None, reason: str | None,
                         clock: Clock, *, order_id: str) -> EligibilityFacts:
    """Shared deterministic rule; execution will reuse it within its transaction."""
    now = utc(clock.now())
    facts = dict(order_id=order_id, evaluated_at=now, existing_ticket=existing_ticket)
    if order:
        facts.update(requested_amount_cents=order.total_cents, currency=order.currency,
                     order_version=order.version, order_snapshot_hash=order.snapshot_hash,
                     policy_id=order.policy_id)
    if policy:
        facts.update(policy_version=policy.version, policy_hash=policy.policy_hash)

    def result(state: str, code: str | None = None) -> EligibilityFacts:
        return EligibilityFacts(**facts, eligibility=state, reason_codes=[code] if code else [])

    if order is None:
        return result("ineligible", "ORDER_NOT_FOUND")
    if order.status != "delivered":
        return result("ineligible", "ORDER_CANCELLED" if order.status == "cancelled" else "ORDER_NOT_DELIVERED")
    if order.delivered_at is None:
        return result("unknown", "DELIVERY_TIME_UNKNOWN")
    try:
        delivered = utc(order.delivered_at)
        created = utc(order.created_at)
    except ValueError:
        return result("unknown", "DELIVERY_TIME_INVALID")
    if delivered > now or delivered < created:
        return result("unknown", "DELIVERY_TIME_INVALID")
    if policy is None:
        return result("unknown", "POLICY_NOT_FOUND")
    if not policy.enabled:
        return result("ineligible", "POLICY_DISABLED")
    if (policy.policy_id != order.policy_id or policy.version < 1 or
            not 1 <= policy.return_window_days <= 90 or policy.scope != "whole_order" or
            not policy.allowed_reasons or not set(policy.allowed_reasons).issubset(RETURN_REASONS)):
        return result("unknown", "POLICY_RULES_INVALID")
    facts["deadline_at"] = delivered + timedelta(days=policy.return_window_days)
    if existing_ticket and existing_ticket.status in ("requested", "processing"):
        return result("ineligible", "RETURN_ALREADY_EXISTS")
    if now > facts["deadline_at"]:
        return result("ineligible", "RETURN_WINDOW_EXPIRED")
    if reason is not None and reason not in policy.allowed_reasons:
        return result("ineligible", "REASON_NOT_ALLOWED")
    # Missing reason permits base eligibility only, not a return Proposal.
    return result("eligible")

from datetime import timedelta

from src.domain.clock import FixedClock, utc
from src.domain.exceptions import AppError
from src.domain.hashing import canonical_hash
from src.domain.policy import evaluate_eligibility
from src.domain.proposals import ReturnPayload
from src.domain.schemas import EligibilityFacts, OrderFacts, PolicyFacts


def selected_evidence(evidence, refs):
    indexed = {item.call_id: item for item in evidence}
    if len(indexed) != len(evidence) or len(set(refs)) != len(refs) or any(ref not in indexed for ref in refs):
        raise AppError("EVIDENCE_INVALID", "Evidence references must identify unique results from this analysis", 502)
    selected = [indexed[ref] for ref in refs]
    if any(not item.result.ok for item in selected):
        raise AppError("EVIDENCE_INVALID", "A failed read cannot authorize a Proposal", 502)
    return selected


def return_facts(evidence, refs, order_id, reason, *, allow_missing_policy=False):
    selected = selected_evidence(evidence, refs)
    named = {}
    for item in selected:
        named[item.name] = item
    if not {"get_order", "get_return_policy", "check_return_eligibility"}.issubset(named):
        raise AppError("EVIDENCE_MISSING", "Return decisions require order, policy and eligibility evidence", 502)
    o, p, e = (named[name] for name in ("get_order", "get_return_policy", "check_return_eligibility"))
    try:
        missing_policy=p.result.data=={'found':False}
        if not o.result.data.get("found") or (not p.result.data.get("found") and not (allow_missing_policy and missing_policy)):
            raise ValueError()
        order = OrderFacts.model_validate({key: value for key, value in o.result.data.items() if key != "found"})
        policy = None if missing_policy else PolicyFacts.model_validate({key: value for key, value in p.result.data.items() if key != "found"})
        eligibility = EligibilityFacts.model_validate(e.result.data)
        order_snapshot = dict(order_id=order.order_id, status=order.status, total_cents=order.total_cents,
                              currency=order.currency, delivered_at=utc(order.delivered_at).isoformat() if order.delivered_at else None,
                              policy_id=order.policy_id, version=order.version)
        policy_snapshot = dict(policy_id=policy.policy_id, version=policy.version, return_window_days=policy.return_window_days,
                               allowed_reasons=sorted(policy.allowed_reasons), scope=policy.scope, enabled=policy.enabled) if policy else None
        if (o.args.get("order_id") != order_id or e.args.get("order_id") != order_id or e.args.get("reason") != reason or
                p.args.get("policy_id") != order.policy_id or order.order_id != order_id or (policy and policy.policy_id != order.policy_id) or
                canonical_hash(order_snapshot) != order.snapshot_hash or (policy and canonical_hash(policy_snapshot) != policy.policy_hash)):
            raise ValueError()
        computed = evaluate_eligibility(order, policy, eligibility.existing_ticket, reason,
                                        FixedClock(eligibility.evaluated_at), order_id=order_id)
        if computed != eligibility:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AppError("EVIDENCE_INCONSISTENT", "Order, policy and eligibility evidence do not describe the same facts", 502) from None
    return order, policy, eligibility


def validate_return_payload(payload: ReturnPayload):
    order, policy, eligibility = return_facts(payload.evidence, payload.evidence_refs, payload.order_id, payload.reason)
    if (eligibility.eligibility != "eligible" or payload.requested_amount_cents != order.total_cents or
            payload.currency != order.currency or payload.order_version != order.version or
            payload.order_snapshot_hash != order.snapshot_hash or payload.policy_id != policy.policy_id or
            payload.policy_version != policy.version or payload.policy_hash != policy.policy_hash or
            utc(payload.deadline_at) != utc(eligibility.deadline_at)):
        raise AppError("EVIDENCE_INCONSISTENT", "Executable Proposal does not match eligible database evidence", 409)

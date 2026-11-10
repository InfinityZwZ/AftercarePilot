from datetime import timedelta

import pytest

from scripts.seed_demo import REFERENCE_TIME, demo_orders
from src.domain.clock import FixedClock
from src.domain.hashing import canonical_hash
from src.domain.money import to_cents
from src.domain.policy import evaluate_eligibility
from src.domain.schemas import OrderFacts, PolicyFacts, RETURN_REASONS, TicketFacts


def facts(suffix="ORD-1009"):
    row = next(row for row in demo_orders() if row["order_id"] == suffix)
    order = OrderFacts(**row, snapshot_hash="a" * 64)
    policy = PolicyFacts(policy_id="demo-return", version=1, name="Demo", return_window_days=14,
                         allowed_reasons=list(RETURN_REASONS), scope="whole_order", enabled=True,
                         description="Demo", updated_at=REFERENCE_TIME, policy_hash="b" * 64)
    return order, policy


@pytest.mark.parametrize("order_id,state,code", [
    ("ORD-1009", "eligible", None),
    ("ORD-1002", "ineligible", "ORDER_NOT_DELIVERED"),
    ("ORD-DEMO-CANCELLED", "ineligible", "ORDER_CANCELLED"),
    ("ORD-DEMO-EXPIRED", "ineligible", "RETURN_WINDOW_EXPIRED"),
    ("ORD-DEMO-BOUNDARY", "eligible", None),
    ("ORD-DEMO-UNKNOWN", "unknown", "DELIVERY_TIME_UNKNOWN"),
])
def test_delivery_fixtures(order_id, state, code):
    order, policy = facts(order_id)
    result = evaluate_eligibility(order, policy, None, "defective", FixedClock(REFERENCE_TIME), order_id=order_id)
    assert result.eligibility == state
    assert result.reason_codes == ([code] if code else [])


def test_exact_deadline_and_one_microsecond_later():
    order, policy = facts("ORD-DEMO-BOUNDARY")
    for delta, expected in ((timedelta(), "eligible"), (timedelta(microseconds=1), "ineligible")):
        result = evaluate_eligibility(order, policy, None, "defective", FixedClock(REFERENCE_TIME + delta), order_id=order.order_id)
        assert result.eligibility == expected
        assert result.deadline_at == REFERENCE_TIME


@pytest.mark.parametrize("delivery", [REFERENCE_TIME + timedelta(seconds=1), REFERENCE_TIME.replace(tzinfo=None), REFERENCE_TIME - timedelta(days=21)])
def test_invalid_delivery_is_unknown(delivery):
    order, policy = facts()
    result = evaluate_eligibility(order.model_copy(update={"delivered_at": delivery}), policy, None, None,
                                  FixedClock(REFERENCE_TIME), order_id=order.order_id)
    assert result.eligibility == "unknown"
    assert result.reason_codes == ["DELIVERY_TIME_INVALID"]


@pytest.mark.parametrize("update,code", [
    ({"enabled": False}, "POLICY_DISABLED"),
    ({"scope": "partial"}, "POLICY_RULES_INVALID"),
    ({"return_window_days": 0}, "POLICY_RULES_INVALID"),
    ({"return_window_days": 91}, "POLICY_RULES_INVALID"),
    ({"allowed_reasons": []}, "POLICY_RULES_INVALID"),
    ({"allowed_reasons": ["refund_now"]}, "POLICY_RULES_INVALID"),
    ({"policy_id": "other"}, "POLICY_RULES_INVALID"),
])
def test_invalid_or_disabled_policy(update, code):
    order, policy = facts()
    result = evaluate_eligibility(order, policy.model_copy(update=update), None, "defective", FixedClock(REFERENCE_TIME), order_id=order.order_id)
    assert result.eligibility != "eligible"
    assert result.reason_codes == [code]


def test_missing_facts():
    order, _ = facts()
    assert evaluate_eligibility(None, None, None, None, FixedClock(REFERENCE_TIME), order_id="MISSING").reason_codes == ["ORDER_NOT_FOUND"]
    assert evaluate_eligibility(order, None, None, None, FixedClock(REFERENCE_TIME), order_id=order.order_id).eligibility == "unknown"


@pytest.mark.parametrize("reason,expected", [(None, "eligible"), ("defective", "eligible"), ("invented", "ineligible")])
def test_reason_and_whole_order_amount(reason, expected):
    order, policy = facts()
    result = evaluate_eligibility(order, policy, None, reason, FixedClock(REFERENCE_TIME), order_id=order.order_id)
    assert result.eligibility == expected
    assert (result.requested_amount_cents, result.currency) == (8999, "USD")


@pytest.mark.parametrize("status,expected", [("requested", "ineligible"), ("processing", "ineligible"), ("closed", "eligible"), ("cancelled", "eligible")])
def test_active_duplicate_rule(status, expected):
    order, policy = facts()
    ticket = TicketFacts(ticket_id="fixture", ticket_no="DEMO", status=status)
    result = evaluate_eligibility(order, policy, ticket, "defective", FixedClock(REFERENCE_TIME), order_id=order.order_id)
    assert result.eligibility == expected


@pytest.mark.parametrize("amount,cents", [(105.97, 10597), ("89.99", 8999), ("0", 0)])
def test_legacy_decimal_conversion(amount, cents):
    assert to_cents(amount) == cents


@pytest.mark.parametrize("amount", ["1.001", "-1", "NaN", "Infinity", "bad"])
def test_invalid_amount(amount):
    with pytest.raises(ValueError):
        to_cents(amount)


def test_canonical_hash_rejects_nan_and_ignores_key_order():
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        canonical_hash({"amount": float("nan")})


def test_naive_clock_rejected():
    with pytest.raises(ValueError):
        FixedClock(REFERENCE_TIME.replace(tzinfo=None))

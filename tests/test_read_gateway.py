from datetime import timedelta

import pytest
from sqlalchemy import select, func

from scripts.seed_demo import REFERENCE_TIME
from src.domain.clock import FixedClock, SystemClock
from src.storage.database import pg_url
from src.storage.models import AftersalesTicket, Proposal, ReturnPolicy
from src.storage.repositories import get_order, get_policy
from src.tools.direct_gateway import DirectReadGateway


def test_order_and_simulated_shipment(gateway):
    result = gateway.get_order(" ord-1002 ")
    assert result.ok and result.data["found"]
    assert result.data["status"] == "shipped"
    assert result.data["shipment"]["status"] == "in_transit"
    assert result.data["shipment"]["simulated"]
    assert result.data["delivered_at"] is None


@pytest.mark.parametrize("method,args,code", [
    ("get_order", ("ORD-NOT-FOUND",), "ORDER_NOT_FOUND"),
    ("get_return_policy", ("missing",), "POLICY_NOT_FOUND"),
    ("check_return_eligibility", ("ORD-NOT-FOUND",), "ORDER_NOT_FOUND"),
])
def test_not_found_is_normal_nonretryable(gateway, method, args, code):
    result = getattr(gateway, method)(*args)
    assert result.ok and result.code == code and not result.retryable
    assert result.error is None


def test_generated_policy_summary_and_rule_hash(pg_session, gateway):
    result = gateway.get_return_policy("demo-return")
    assert "14 days" in result.data["description"]
    assert "unused" not in result.data["description"]
    before = get_policy(pg_session, "demo-return")
    row = pg_session.get(ReturnPolicy, "demo-return")
    row.allowed_reasons = ["defective"]
    row.description = "Stale free-form text must not control rules"
    pg_session.flush()
    after = get_policy(pg_session, "demo-return")
    assert after.version == before.version
    assert after.policy_hash != before.policy_hash
    assert after.description != row.description


@pytest.mark.parametrize("order_id,state,code", [
    ("ORD-1009", "eligible", None),
    ("ORD-1002", "ineligible", "ORDER_NOT_DELIVERED"),
    ("ORD-DEMO-EXPIRED", "ineligible", "RETURN_WINDOW_EXPIRED"),
    ("ORD-DEMO-BOUNDARY", "eligible", None),
    ("ORD-DEMO-UNKNOWN", "unknown", "DELIVERY_TIME_UNKNOWN"),
    ("ORD-DEMO-DUPLICATE", "ineligible", "RETURN_ALREADY_EXISTS"),
    ("ORD-DEMO-CANCELLED", "ineligible", "ORDER_CANCELLED"),
])
def test_pg_eligibility_fixtures(gateway, order_id, state, code):
    result = gateway.check_return_eligibility(order_id, "defective")
    assert result.ok and not result.retryable
    assert result.data["eligibility"] == state
    assert result.data["reason_codes"] == ([code] if code else [])
    if order_id == "ORD-DEMO-DUPLICATE":
        assert result.data["existing_ticket"]["ticket_no"] == "DEMO-TICKET-EXISTING"


def test_pg_deadline_plus_one_microsecond(pg):
    gateway = DirectReadGateway(pg["reader_sessions"], FixedClock(REFERENCE_TIME + timedelta(microseconds=1)))
    assert gateway.check_return_eligibility("ORD-DEMO-BOUNDARY").data["reason_codes"] == ["RETURN_WINDOW_EXPIRED"]


def test_missing_reason_base_check_and_invalid_reason(gateway):
    assert gateway.check_return_eligibility("ORD-1009").data["eligibility"] == "eligible"
    assert gateway.check_return_eligibility("ORD-1009", "made_up").data["reason_codes"] == ["REASON_NOT_ALLOWED"]


def test_reads_never_create_proposals_or_tickets(pg, gateway):
    with pg["sessions"]() as session:
        before = [session.scalar(select(func.count()).select_from(model)) for model in (Proposal, AftersalesTicket)]
    gateway.get_order("ORD-1009")
    gateway.get_return_policy("demo-return")
    gateway.check_return_eligibility("ORD-1009", "defective")
    with pg["sessions"]() as session:
        assert before == [session.scalar(select(func.count()).select_from(model)) for model in (Proposal, AftersalesTicket)]


def test_order_hash_excludes_clock_but_binds_amount(pg_session):
    from src.storage.models import Order
    before = get_order(pg_session, "ORD-1009")
    row = pg_session.get(Order, "ORD-1009")
    row.total_cents += 1
    pg_session.flush()
    assert get_order(pg_session, "ORD-1009").snapshot_hash != before.snapshot_hash


def test_invalid_argument_and_pg_only(gateway):
    assert not gateway.get_order("").ok
    assert not gateway.get_return_policy("x" * 65).ok
    with pytest.raises(ValueError):
        pg_url("sqlite:///legacy.db")


def test_default_clock_is_live():
    assert isinstance(DirectReadGateway()._clock, SystemClock)


def test_database_error_is_structured_and_redacted():
    from sqlalchemy.exc import OperationalError
    class BrokenFactory:
        def __call__(self):
            raise OperationalError("SELECT secret", {}, Exception("password=must-never-leak"))
    result = DirectReadGateway(BrokenFactory()).get_order("ORD-1009")
    assert not result.ok and result.data is None
    assert "must-never-leak" not in result.model_dump_json()

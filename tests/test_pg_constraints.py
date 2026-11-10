from datetime import timedelta
from uuid import uuid4

from alembic import command
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from scripts.seed_demo import REFERENCE_TIME, seed_demo
from src.domain.hashing import canonical_hash
from src.storage.models import AftersalesTicket, AuditLog, Order, Proposal, ReturnPolicy


def proposal(session, **changes):
    row = dict(id=uuid4(), request_id=uuid4(), revision=1, is_current=True,
               status="pending_approval", kind="return_request", source_question="Test fixture",
               order_id="ORD-1009", policy_id="demo-return", entities={}, payload={}, payload_hash="a" * 64,
               expires_at=REFERENCE_TIME + timedelta(days=1), created_at=REFERENCE_TIME, updated_at=REFERENCE_TIME)
    row.update(changes)
    item = Proposal(**row)
    session.add(item)
    session.flush()
    return item


def ticket(session, prop, **changes):
    row = dict(id=uuid4(), ticket_no="TEST-" + uuid4().hex, proposal_id=prop.id,
               order_id="ORD-1009", type="return_request", reason="defective", requested_amount_cents=8999,
               currency="USD", status="requested", idempotency_key=canonical_hash({"test": str(uuid4())}),
               approved_reply="Test", created_by="test", created_at=REFERENCE_TIME)
    row.update(changes)
    item = AftersalesTicket(**row)
    session.add(item)
    session.flush()
    return item


def test_real_pg_schema_and_types(pg):
    with pg["engine"].connect() as connection:
        assert connection.scalar(text("SELECT version()" )).startswith("PostgreSQL")
        assert set(inspect(connection).get_table_names(schema=pg["schema"])) == {
            "return_policies", "orders", "proposals", "aftersales_tickets", "audit_logs", "alembic_version"}
        types = dict(connection.execute(text("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=:schema AND table_name='orders'"), {"schema": pg["schema"]}).all())
        assert types["total_cents"] == "bigint"
        assert types["items"] == "jsonb"
        assert types["delivered_at"] == "timestamp with time zone"


def test_alembic_has_no_schema_drift(pg):
    with pg["engine"].begin() as connection:
        pg["config"].attributes["connection"] = connection
        command.check(pg["config"])


def test_seed_repeat_preserves_edits_and_counts(pg_session):
    row = pg_session.get(Order, "ORD-1009")
    row.total_cents = 10001
    policy = pg_session.get(ReturnPolicy, "demo-return")
    policy.return_window_days = 10
    before = [pg_session.scalar(select(func.count()).select_from(model)) for model in (ReturnPolicy, Order, Proposal, AftersalesTicket, AuditLog)]
    seed_demo(pg_session)
    seed_demo(pg_session)
    pg_session.expire_all()
    assert pg_session.get(Order, "ORD-1009").total_cents == 10001
    assert pg_session.get(ReturnPolicy, "demo-return").return_window_days == 10
    assert before == [1, 17, 1, 1, 1]
    assert before == [pg_session.scalar(select(func.count()).select_from(model)) for model in (ReturnPolicy, Order, Proposal, AftersalesTicket, AuditLog)]


@pytest.mark.parametrize("table,assignment,key,expected_state", [
    ("orders", "total_cents = -1", "order_id='ORD-1009'", "23514"),
    ("orders", "status = 'refunded'", "order_id='ORD-1009'", "23514"),
    ("orders", "currency = 'usd'", "order_id='ORD-1009'", "23514"),
    ("orders", "items = '{}'::jsonb", "order_id='ORD-1009'", "23514"),
    ("orders", "shipment = '[]'::jsonb", "order_id='ORD-1009'", "23514"),
    ("orders", "version = 0", "order_id='ORD-1009'", "23514"),
    ("orders", "policy_id = 'missing'", "order_id='ORD-1009'", "23503"),
    ("return_policies", "version = 0", "policy_id='demo-return'", "23514"),
    ("return_policies", "return_window_days = 0", "policy_id='demo-return'", "23514"),
    ("return_policies", "allowed_reasons = '{}'::jsonb", "policy_id='demo-return'", "23514"),
    ("return_policies", "allowed_reasons = '[\"invented\"]'::jsonb", "policy_id='demo-return'", "23514"),
    ("return_policies", "scope = 'partial'", "policy_id='demo-return'", "23514"),
    ("proposals", "revision = 0", "source_question='Synthetic duplicate fixture'", "23514"),
    ("proposals", "approved_payload_hash = NULL", "source_question='Synthetic duplicate fixture'", "23514"),
    ("proposals", "approved_payload_hash = repeat('x',64)", "source_question='Synthetic duplicate fixture'", "23514"),
    ("proposals", "reviewer_id = NULL", "source_question='Synthetic duplicate fixture'", "23514"),
    ("proposals", "payload_hash = NULL", "source_question='Synthetic duplicate fixture'", "23514"),
    ("proposals", "payload = '[]'::jsonb", "source_question='Synthetic duplicate fixture'", "23514"),
    ("proposals", "status = 'nonsense'", "source_question='Synthetic duplicate fixture'", "23514"),
    ("aftersales_tickets", "requested_amount_cents = -1", "ticket_no='DEMO-TICKET-EXISTING'", "23514"),
    ("aftersales_tickets", "reason = 'refund'", "ticket_no='DEMO-TICKET-EXISTING'", "23514"),
    ("aftersales_tickets", "status = 'approved'", "ticket_no='DEMO-TICKET-EXISTING'", "23514"),
    ("aftersales_tickets", "proposal_id = '00000000-0000-0000-0000-000000000000'", "ticket_no='DEMO-TICKET-EXISTING'", "23503"),
    ("audit_logs", "details = '[]'::jsonb", "event_type='fixture_ticket_seeded'", "23514"),
    ("audit_logs", "ticket_id = '00000000-0000-0000-0000-000000000000'", "event_type='fixture_ticket_seeded'", "23503"),
])
def test_checks_and_foreign_keys(pg_session, table, assignment, key, expected_state):
    # All SQL fragments above are fixed test literals.
    with pytest.raises(IntegrityError) as caught:
        with pg_session.begin_nested():
            pg_session.execute(text(f"UPDATE {table} SET {assignment} WHERE {key}"))
    assert caught.value.orig.sqlstate == expected_state
    assert pg_session.scalar(select(func.count()).select_from(Order)) == 17


def test_pending_requires_hash_but_needs_info_allows_missing_order(pg_session):
    with pytest.raises(IntegrityError):
        with pg_session.begin_nested():
            proposal(pg_session, payload_hash=None)
    row = proposal(pg_session, status="needs_info", kind="clarification", payload_hash=None, order_id=None, policy_id=None)
    assert row.order_id is None


def test_one_current_revision_and_unique_revision(pg_session):
    first = proposal(pg_session)
    with pytest.raises(IntegrityError) as caught:
        with pg_session.begin_nested():
            proposal(pg_session, request_id=first.request_id, revision=2)
    assert caught.value.orig.sqlstate == "23505"
    first.is_current = False
    pg_session.flush()
    newer = proposal(pg_session, request_id=first.request_id, revision=2)
    with pytest.raises(IntegrityError):
        with pg_session.begin_nested():
            proposal(pg_session, request_id=first.request_id, revision=2, is_current=False)
    assert newer.is_current


def test_active_ticket_partial_unique_and_inactive_history(pg_session):
    first = ticket(pg_session, proposal(pg_session))
    other = proposal(pg_session)
    with pytest.raises(IntegrityError):
        with pg_session.begin_nested():
            ticket(pg_session, other, status="processing")
    first.status = "closed"
    pg_session.flush()
    current = ticket(pg_session, other)
    assert current.status == "requested"


@pytest.mark.parametrize("field", ["proposal_id", "idempotency_key", "ticket_no"])
def test_ticket_identity_uniqueness(pg_session, field):
    first = ticket(pg_session, proposal(pg_session), status="closed")
    other = proposal(pg_session)
    with pytest.raises(IntegrityError) as caught:
        with pg_session.begin_nested():
            ticket(pg_session, other, **{field: getattr(first, field)})
    assert caught.value.orig.sqlstate == "23505"


def test_failed_transaction_requires_rollback_then_recovers(pg):
    with pg["sessions"]() as session:
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE orders SET total_cents=-1 WHERE order_id='ORD-1009'"))
        session.rollback()
    with pg["sessions"]() as session:
        assert session.get(Order, "ORD-1009").total_cents == 8999


@pytest.mark.parametrize("statement", [
    "UPDATE orders SET total_cents=total_cents WHERE FALSE",
    "DELETE FROM orders WHERE FALSE",
    "INSERT INTO orders SELECT * FROM orders WHERE FALSE",
    "UPDATE return_policies SET enabled=enabled WHERE FALSE",
    "UPDATE proposals SET revision=revision WHERE FALSE",
    "UPDATE aftersales_tickets SET status=status WHERE FALSE",
    "UPDATE audit_logs SET actor_id=actor_id WHERE FALSE",
    "CREATE TABLE reader_write_probe (id int)",
])
def test_reader_role_itself_cannot_write(pg, statement):
    # No SET TRANSACTION READ ONLY: this tests actual PostgreSQL grants.
    with pg["reader_engine"].connect() as connection:
        with pytest.raises(DBAPIError) as caught:
            connection.execute(text(statement))
        assert caught.value.orig.sqlstate == "42501"
        connection.rollback()
        assert connection.scalar(text("SELECT count(*) FROM orders")) == 17

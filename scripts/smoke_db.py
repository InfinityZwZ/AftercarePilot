"""Real PG smoke check. Uses no model, Langfuse or MCP."""
import json

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from scripts.seed_demo import REFERENCE_TIME
from src.domain.clock import FixedClock
from src.storage.database import check_database, session_factory
from src.tools.direct_gateway import DirectReadGateway

BUSINESS_TABLES = {"return_policies", "orders", "proposals", "aftersales_tickets", "audit_logs"}


def main():
    check_database()
    with session_factory()() as session:
        tables = set(inspect(session.connection()).get_table_names())
        assert tables == BUSINESS_TABLES | {"alembic_version"}
        version = session.scalar(text("SHOW server_version"))
        counts = {table: session.scalar(text(f"SELECT count(*) FROM {table}")) for table in sorted(BUSINESS_TABLES)}
    reader = session_factory(readonly=True)
    # Verify grants without transaction-read-only masking a permissive role.
    for statement in ("UPDATE orders SET total_cents=total_cents WHERE FALSE",
                      "INSERT INTO orders SELECT * FROM orders WHERE FALSE",
                      "DELETE FROM orders WHERE FALSE"):
        with reader() as session:
            try:
                session.execute(text(statement))
            except DBAPIError as exc:
                assert exc.orig.sqlstate == "42501"
                session.rollback()
            else:
                session.rollback()
                raise AssertionError("Reader role unexpectedly has write privileges")
    with reader() as session:
        role = session.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user")).one()
        assert role == (False, False, False)
    gateway = DirectReadGateway(reader, FixedClock(REFERENCE_TIME))
    order = gateway.get_order("ORD-1002")
    policy = gateway.get_return_policy("demo-return")
    eligibility = gateway.check_return_eligibility("ORD-1009", "defective")
    boundary = gateway.check_return_eligibility("ORD-DEMO-BOUNDARY")
    assert order.ok and order.data["shipment"]["status"] == "in_transit"
    assert policy.ok and policy.data["return_window_days"] == 14
    assert eligibility.ok and eligibility.data["eligibility"] == "eligible"
    assert eligibility.data["requested_amount_cents"] == 8999
    assert boundary.data["eligibility"] == "eligible"
    print(json.dumps({"postgresql_version": version, "counts": counts, "migration": "head",
                      "three_direct_reads": "passed", "reader_write_denied": True,
                      "fixed_clock": REFERENCE_TIME.isoformat()}, indent=2))


if __name__ == "__main__":
    main()

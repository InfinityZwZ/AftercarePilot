"""Explicit deterministic demo data. Never reads/imports legacy SQLite."""
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.domain.hashing import canonical_hash
from src.domain.policy import policy_description
from src.domain.schemas import RETURN_REASONS
from src.storage.database import session_factory
from src.storage.models import AftersalesTicket, AuditLog, Order, Proposal, ReturnPolicy

REFERENCE_TIME = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def seed_recent_orders(session, *, now=None):
    from src.domain.clock import SystemClock, to_local, utc
    now = utc(now or SystemClock().now())
    local_day = to_local(now).strftime('%Y%m%d')
    policy = session.get(ReturnPolicy, 'demo-return')
    if policy is None or not policy.enabled or policy.scope != 'whole_order':
        raise RuntimeError('现有退货政策不可用，未新增订单。请先核实政策。')
    delivered = now - timedelta(hours=1)
    inserted = []
    for index, (name, cents) in enumerate((('蓝牙音箱', 8999), ('机械键盘', 14999), ('无线入耳耳机', 6999)), 1):
        identity = f'ORD-{local_day}-{index:02}'
        row = dict(order_id=identity, customer_ref='local-test-order', status='delivered',
            total_cents=cents, currency='USD', items=[dict(product_name=name, sku=f'LOCAL-SKU-{index}',
                quantity=1, unit_price_cents=cents)], created_at=now - timedelta(days=3), delivered_at=delivered,
            shipment=dict(tracking_number=f'LOCAL-{local_day}-{index}', carrier='本地测试物流', simulated=True,
                status='delivered', estimated_delivery=to_local(delivered).date().isoformat(), events=[dict(
                    timestamp=delivered.isoformat(), location='签收地', description='已签收')]),
            policy_id=policy.policy_id, version=1, updated_at=now)
        added = session.scalar(insert(Order).values(row).on_conflict_do_nothing(index_elements=['order_id']).returning(Order.order_id))
        if added:
            inserted.append(added)
    return inserted


def demo_orders():
    products = ["Wireless Headphones", "Mechanical Keyboard", "4K Monitor", "Wireless Mouse",
                "Wireless Charger", "Tablet Stand", "Portable SSD", "Smartwatch Pro", "Bluetooth Speaker",
                "Laptop Sleeve", "HD Webcam", "Wireless Earbuds"]
    totals = [10597, 14999, 49999, 6998, 3999, 4997, 12999, 29999, 8999, 3499, 8999, 20496]
    statuses = ["delivered", "shipped", "confirmed", "cancelled", "delivered", "shipped",
                "confirmed", "delivered", "delivered", "shipped", "shipped", "delivered"]
    rows = []
    for i, (product, total, status) in enumerate(zip(products, totals, statuses), start=1):
        delivered = REFERENCE_TIME - timedelta(days=1 if i == 9 else 2)
        shipment = None
        if status in ("shipped", "delivered"):
            shipment = dict(tracking_number=f"DEMO-TRACK-{1000+i}", carrier="Demo Carrier", simulated=True,
                            status="delivered" if status == "delivered" else "in_transit",
                            estimated_delivery=(REFERENCE_TIME + timedelta(days=2)).date().isoformat() if status == "shipped" else delivered.date().isoformat(),
                            events=[dict(timestamp=(delivered if status == "delivered" else REFERENCE_TIME - timedelta(days=1)).isoformat(),
                                         location="Demo hub", description="Delivered" if status == "delivered" else "In transit")])
        rows.append(dict(order_id=f"ORD-{1000+i}", customer_ref=f"demo-customer-{i:02}", status=status,
                         total_cents=total, currency="USD", items=[dict(product_name=product, sku=f"DEMO-SKU-{i}", quantity=1, unit_price_cents=total)],
                         created_at=REFERENCE_TIME - timedelta(days=20), delivered_at=delivered if status == "delivered" else None,
                         shipment=shipment, policy_id="demo-return", version=1, updated_at=REFERENCE_TIME))
    for suffix, status, delivered in (
        ("EXPIRED", "delivered", REFERENCE_TIME - timedelta(days=15)),
        ("BOUNDARY", "delivered", REFERENCE_TIME - timedelta(days=14)),
        ("UNKNOWN", "delivered", None),
        ("DUPLICATE", "delivered", REFERENCE_TIME - timedelta(days=1)),
        ("CANCELLED", "cancelled", None),
    ):
        rows.append(dict(order_id=f"ORD-DEMO-{suffix}", customer_ref="demo-fixture", status=status,
                         total_cents=8999, currency="USD", items=[dict(product_name=f"Demo {suffix}", sku="DEMO-SKU", quantity=1, unit_price_cents=8999)],
                         created_at=REFERENCE_TIME - timedelta(days=20), delivered_at=delivered, shipment=None,
                         policy_id="demo-return", version=1, updated_at=REFERENCE_TIME))
    return rows


def seed_demo(session):
    """No commits here. Existing rows retain every business edit."""
    reasons = list(RETURN_REASONS)
    policy = dict(policy_id="demo-return", version=1, name="Synthetic whole-order demo returns",
                  return_window_days=14, allowed_reasons=reasons, scope="whole_order", enabled=True,
                  description=policy_description(14, reasons, "whole_order", True), updated_at=REFERENCE_TIME)
    session.execute(insert(ReturnPolicy).values(policy).on_conflict_do_nothing(index_elements=["policy_id"]))
    for order in demo_orders():
        session.execute(insert(Order).values(order).on_conflict_do_nothing(index_elements=["order_id"]))

    # The sole ticket is labelled synthetic seed evidence, not an executed user request.
    proposal_id = uuid5(NAMESPACE_URL, "aftercare-demo-duplicate-proposal")
    ticket_id = uuid5(NAMESPACE_URL, "aftercare-demo-duplicate-ticket")
    request_id = uuid5(NAMESPACE_URL, "aftercare-demo-duplicate-request")
    existing = session.scalar(select(AftersalesTicket.id).where(AftersalesTicket.order_id == "ORD-DEMO-DUPLICATE"))
    if not existing and session.get(Proposal, proposal_id) is None:
        order = session.get(Order, "ORD-DEMO-DUPLICATE")
        payload = dict(fixture=True, proposal_id=str(proposal_id), request_id=str(request_id), revision=1,
                       action="create_aftersales_ticket", order_id=order.order_id, reason="defective",
                       requested_amount_cents=order.total_cents, currency=order.currency,
                       draft_reply="Synthetic existing-ticket fixture; no refund or message sent.")
        digest = canonical_hash(payload)
        session.add(Proposal(id=proposal_id, request_id=request_id, revision=1, is_current=True, status="executed",
                             kind="return_request", source_question="Synthetic duplicate fixture", order_id=order.order_id,
                             policy_id=order.policy_id, entities={}, payload=payload, payload_hash=digest,
                             approved_payload_hash=digest, reviewer_id="demo-seed", reviewed_at=REFERENCE_TIME,
                             expires_at=REFERENCE_TIME + timedelta(days=1), created_at=REFERENCE_TIME, updated_at=REFERENCE_TIME))
        session.flush()
        session.add(AftersalesTicket(id=ticket_id, ticket_no="DEMO-TICKET-EXISTING", proposal_id=proposal_id,
                                    order_id=order.order_id, type="return_request", reason="defective",
                                    requested_amount_cents=order.total_cents, currency=order.currency, status="requested",
                                    idempotency_key=canonical_hash({"fixture": str(ticket_id)}), approved_reply=payload["draft_reply"],
                                    created_by="demo-seed", created_at=REFERENCE_TIME))
        session.flush()
        session.add(AuditLog(id=uuid5(NAMESPACE_URL, "aftercare-demo-duplicate-audit"), request_id=request_id,
                             proposal_id=proposal_id, ticket_id=ticket_id, event_type="fixture_ticket_seeded",
                             actor_id="demo-seed", details={"synthetic_fixture": True}, created_at=REFERENCE_TIME))


def main():
    from src.config import settings
    from src.storage.database import pg_url
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-name", required=True, help="Must exactly match configured database, ending _demo")
    parser.add_argument("--recent-orders", action="store_true", help="只补充当天三个近期已签收订单，保留已有数据")
    args = parser.parse_args()
    database = pg_url(settings.database_url.get_secret_value()).database
    if args.database_name != database or not database.endswith("_demo"):
        parser.error("Seed requires the explicit configured demo database name (suffix _demo)")
    with session_factory().begin() as session:
        if args.recent_orders:
            ids = seed_recent_orders(session)
        else:
            seed_demo(session)
    if args.recent_orders:
        print(f"已新增 {len(ids)} 个近期已签收订单：" + ('、'.join(ids) if ids else '当天编号已存在，未覆盖已有订单。'))
    else:
        print("Demo seed committed: ensured 1 policy and 17 order IDs; synthetic duplicate fixture checked; existing rows preserved.")


if __name__ == "__main__":
    main()

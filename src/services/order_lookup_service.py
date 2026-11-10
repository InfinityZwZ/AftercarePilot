from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB

from src.domain.clock import utc
from src.domain.policy import evaluate_eligibility
from src.domain.products import PRODUCT_NAMES
from src.domain.schemas import TicketFacts
from src.storage.models import AftersalesTicket, Order, ReturnPolicy
from src.storage.repositories import get_order, get_policy


WINDOW_NAMES = {
    'within_window': '在退货期限内', 'not_delivered': '尚未签收', 'cancelled': '订单已取消',
    'expired': '已超过退货期限', 'existing_ticket': '已有售后工单', 'unknown': '需核实信息',
    'policy_disabled': '政策暂不可用',
}


class OrderLookupService:
    def __init__(self, readers, clock):
        self.readers, self.clock = readers, clock

    def list(self, *, q='', limit=10, offset=0):
        conditions = []
        query = q.strip()
        if query:
            names = [original for original, translated in PRODUCT_NAMES.items() if query.casefold() in translated.casefold()]
            items = func.jsonb_array_elements(Order.items).table_valued('value').alias('order_item')
            name = items.c.value.cast(JSONB)['product_name'].astext
            product_match = exists(select(1).select_from(items).where(or_(name.icontains(query, autoescape=True), name.in_(names))))
            conditions.append(or_(Order.order_id.icontains(query, autoescape=True), product_match))
        with self.readers() as session:
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            total = session.scalar(select(func.count()).select_from(Order).where(*conditions))
            rows = session.execute(select(Order, ReturnPolicy, AftersalesTicket).select_from(Order)
                .outerjoin(ReturnPolicy, Order.policy_id == ReturnPolicy.policy_id)
                .outerjoin(AftersalesTicket, and_(AftersalesTicket.order_id == Order.order_id,
                    AftersalesTicket.status.in_(('requested', 'processing'))))
                .where(*conditions).order_by(Order.created_at.desc(), Order.order_id).limit(limit).offset(offset)).all()
            data = []
            for order, policy, ticket in rows:
                existing = TicketFacts(ticket_id=str(ticket.id), ticket_no=ticket.ticket_no, status=ticket.status) if ticket else None
                result = evaluate_eligibility(get_order(session, order.order_id), get_policy(session, order.policy_id),
                    existing, None, self.clock, order_id=order.order_id)
                window = 'within_window' if result.eligibility == 'eligible' else {
                    'ORDER_CANCELLED': 'cancelled', 'ORDER_NOT_DELIVERED': 'not_delivered',
                    'RETURN_WINDOW_EXPIRED': 'expired', 'RETURN_ALREADY_EXISTS': 'existing_ticket',
                    'POLICY_DISABLED': 'policy_disabled',
                }.get(next(iter(result.reason_codes), ''), 'unknown')
                data.append(dict(order_id=order.order_id, status=order.status,
                    product_names=[PRODUCT_NAMES.get(item.get('product_name'), item.get('product_name') or '商品名称待补充')
                                   for item in order.items if isinstance(item, dict)],
                    total_cents=order.total_cents, currency=order.currency,
                    delivered_at=utc(order.delivered_at).isoformat() if order.delivered_at else None,
                    deadline_at=utc(result.deadline_at).isoformat() if result.deadline_at else None,
                    return_window=window, return_window_label=WINDOW_NAMES[window], has_ticket=ticket is not None))
        return dict(items=data, total=total, limit=limit, offset=offset)

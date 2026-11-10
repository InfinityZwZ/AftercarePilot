from sqlalchemy.orm import Session

from src.storage.repositories import get_order


class OrderService:
    def get_order(self, session: Session, order_id: str):
        return get_order(session, order_id)

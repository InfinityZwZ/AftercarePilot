from sqlalchemy.orm import Session

from src.storage.repositories import get_policy


class PolicyService:
    def get_return_policy(self, session: Session, policy_id: str):
        return get_policy(session, policy_id)

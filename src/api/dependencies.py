import secrets
from fastapi import Header

from src.agent.nodes import AnalysisAgent
from src.config import settings
from src.domain.exceptions import AppError
from src.services.aftersales_service import AftersalesService
from src.services.proposal_service import ProposalService


def operator_identity(x_operator_token: str | None = Header(default=None)) -> str:
    expected = settings.operator_token.get_secret_value()
    if not expected or not settings.operator_id.strip():
        raise AppError("OPERATOR_NOT_CONFIGURED", "Run scripts.create_operator or configure the local operator token", 503)
    if not x_operator_token or not secrets.compare_digest(x_operator_token.encode(), expected.encode()):
        raise AppError("UNAUTHORIZED", "Invalid operator token", 401)
    return settings.operator_id


def get_proposal_service():
    return ProposalService()


def get_aftersales_service():
    return AftersalesService()


def get_analyzer():
    return AnalysisAgent()

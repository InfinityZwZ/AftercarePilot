"""Trusted acceptance server, only present with FakeLLM in the verification image."""
import os

if os.environ.get("AFTERCARE_VERIFICATION") != "1":
    raise RuntimeError("Use the explicit verification image/override")

from scripts.seed_demo import REFERENCE_TIME
from src.main import app
from src.config import settings
from src.api.dependencies import get_aftersales_service, get_analyzer, get_proposal_service
from src.agent.nodes import AnalysisAgent
from src.domain.clock import FixedClock
from src.mcp.gateway import MCPReadGateway
from src.services.aftersales_service import AftersalesService
from src.services.proposal_service import ProposalService
from tests.fakes import FakeLLM

settings.langfuse_public_key = settings.langfuse_secret_key = ""
settings.openai_api_key = ""

from langchain_openai import ChatOpenAI
def forbidden_provider(*args, **kwargs):
    raise AssertionError("Provider calls are forbidden in the verification server")
ChatOpenAI.invoke = ChatOpenAI.ainvoke = forbidden_provider

app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(), MCPReadGateway(reference_time=REFERENCE_TIME))
app.dependency_overrides[get_proposal_service] = lambda: ProposalService(clock=FixedClock(REFERENCE_TIME))
app.dependency_overrides[get_aftersales_service] = lambda: AftersalesService(clock=FixedClock(REFERENCE_TIME))

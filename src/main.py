"""
FastAPI Application — Agent Gateway entry point.

Run with:
    uvicorn src.main:app --reload
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from src.api.routes import router
from src.config import settings
from src.storage.database import check_database
from src.services.proposal_service import ProposalService
from src.api.errors import register_errors
from src.observability.langfuse_setup import get_langfuse_client, init_langfuse

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    check_database()
    ProposalService().recover_interrupted()
    try:
        init_langfuse()
    except Exception:
        logger.warning("Optional tracing could not initialize; PostgreSQL workflow remains available")
    try:
        yield
    finally:
        client = get_langfuse_client()
        if client:
            try:
                client.flush()
            except Exception:
                logger.warning("Optional tracing flush failed; business transactions are unaffected")


app = FastAPI(
    title="AftercarePilot",
    description="AftercarePilot: read-only analysis, versioned review and internal transactional ticket execution",
    version="2.0.0",
    lifespan=lifespan,
)
register_errors(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=settings.api_host, port=settings.api_port, reload=True)

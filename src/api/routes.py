from uuid import UUID
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from src.api.dependencies import get_aftersales_service, get_analyzer, get_proposal_service, operator_identity
from src.api.schemas import AnalyzeRequest, ChatRequest, RejectRequest, RevisionRequest, VersionRequest
from src.api.schemas import ManagementRequest, BatchManagementRequest, PurgeRequest
from src.services.request_management_service import RequestManagementService
from src.domain.clock import utc
from src.domain.exceptions import AppError
from src.domain.proposals import ProposalStatus
from src.storage.database import check_database
from src.storage.models import AuditLog
from src.services.request_service import RequestService, STATUS_NAMES
from src.services.order_lookup_service import OrderLookupService

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


@router.get("/ready")
def ready():
    try:
        check_database()
    except RuntimeError:
        raise AppError("DATABASE_NOT_READY", "PostgreSQL or schema is not ready", 503) from None
    return {"status": "ready", "database": "postgresql"}


def traced(request, value):
    return {**value, "request_trace_id": request.state.request_trace_id}


@router.get("/orders")
def orders_lookup(request: Request, q: str = Query("", max_length=200), limit: int = Query(10, ge=1, le=50),
                  offset: int = Query(0, ge=0), operator=Depends(operator_identity), proposals=Depends(get_proposal_service)):
    from src.storage import database
    service = OrderLookupService(database.session_factory(readonly=True), proposals.clock)
    return traced(request, service.list(q=q, limit=limit, offset=offset))


@router.post("/proposals/analyze")
async def analyze(body: AnalyzeRequest, request: Request, operator=Depends(operator_identity),
                  service=Depends(get_proposal_service), agent=Depends(get_analyzer)):
    proposal = await service.analyze(body.message, request_id=body.request_id, language=body.language, operator=operator, agent=agent)
    return traced(request, proposal)


@router.post("/chat")
async def chat(body: ChatRequest, request: Request, operator=Depends(operator_identity),
               service=Depends(get_proposal_service), agent=Depends(get_analyzer)):
    # session_id remains accepted but is never mapped into a shared business request.
    proposal = await service.analyze(body.message, request_id=body.request_id, language=body.language, operator=operator, agent=agent)
    return traced(request, {"response": proposal["draft_reply"], "request_id": proposal["request_id"],
                            "proposal_id": proposal["proposal_id"], "revision": proposal["revision"], "status": proposal["status"],
                            "proposal": proposal, "intent": proposal["entities"].get("intent", "general")})


@router.get("/proposals")
def proposals(request: Request, status: ProposalStatus | None = None, request_id: UUID | None = None,
              limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0),
              operator=Depends(operator_identity), service=Depends(get_proposal_service)):
    return traced(request, {"items": service.list(status=status, request_id=request_id, limit=limit, offset=offset)})


@router.get("/proposals/{proposal_id}")
def proposal(proposal_id: UUID, request: Request, operator=Depends(operator_identity), service=Depends(get_proposal_service)):
    return traced(request, service.get(proposal_id))


@router.post("/proposals/{proposal_id}/revisions")
def revision(proposal_id: UUID, body: RevisionRequest, request: Request, operator=Depends(operator_identity), service=Depends(get_proposal_service)):
    return traced(request, service.revise(proposal_id, body.expected_revision, body.expected_payload_hash, body.draft_reply, operator))


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: UUID, body: VersionRequest, request: Request, operator=Depends(operator_identity), service=Depends(get_proposal_service)):
    return traced(request, service.review(proposal_id, body.expected_revision, body.expected_payload_hash, operator))


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: UUID, body: RejectRequest, request: Request, operator=Depends(operator_identity), service=Depends(get_proposal_service)):
    return traced(request, service.review(proposal_id, body.expected_revision, body.expected_payload_hash, operator, reject_reason=body.rejection_reason))


@router.post("/proposals/{proposal_id}/execute")
def execute(proposal_id: UUID, body: VersionRequest, request: Request, operator=Depends(operator_identity), service=Depends(get_aftersales_service)):
    return traced(request, service.execute_proposal(proposal_id, body.expected_revision, body.expected_payload_hash, operator))


@router.post("/proposals/{proposal_id}/confirm-and-create")
def confirm_and_create(proposal_id: UUID, body: VersionRequest, request: Request,
                       operator=Depends(operator_identity), service=Depends(get_aftersales_service)):
    return traced(request, service.confirm_and_create(proposal_id, body.expected_revision, body.expected_payload_hash, operator))


@router.get("/requests")
def requests_list(request: Request, status: str | None = None, q: str = Query("", max_length=4000),
                  limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0),
                  archived: Literal['active', 'archived', 'all'] = 'active', updated_from: datetime | None = None,
                  updated_to: datetime | None = None, cursor: str | None = Query(None, max_length=1024), include_total: bool = True,
                  operator=Depends(operator_identity), proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    if status and status not in STATUS_NAMES:
        raise AppError("VALIDATION_ERROR", "Unknown request status", 422)
    if any(value is not None and value.utcoffset() is None for value in (updated_from, updated_to)) or (
            updated_from and updated_to and updated_from >= updated_to):
        raise AppError('VALIDATION_ERROR', 'Dates must have timezones and a positive range', 422)
    return traced(request, RequestService(proposals, aftersales).list(status=status, q=q, limit=limit, offset=offset,
        archived=archived, updated_from=updated_from, updated_to=updated_to, cursor=cursor, include_total=include_total))


@router.post('/requests/batch-archive')
@router.post('/requests/batch-restore')
def batch_manage(body: BatchManagementRequest, request: Request, operator=Depends(operator_identity),
                 proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    result = RequestManagementService(proposals, aftersales).manage(
        [(item.request_id, item.management_token) for item in body.items], operator, archive=request.url.path.endswith('batch-archive'))
    return traced(request, result)


@router.post('/requests/{request_id}/archive')
@router.post('/requests/{request_id}/restore')
def manage_request(request_id: UUID, body: ManagementRequest, request: Request, operator=Depends(operator_identity),
                   proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    return traced(request, RequestManagementService(proposals, aftersales).manage(
        [(request_id, body.management_token)], operator, archive=request.url.path.endswith('/archive')))


@router.get('/requests/{request_id}/purge-preview')
def purge_preview(request_id: UUID, request: Request, operator=Depends(operator_identity),
                  proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    return traced(request, RequestManagementService(proposals, aftersales).preview(request_id))


@router.post('/requests/{request_id}/purge')
def purge_request(request_id: UUID, body: PurgeRequest, request: Request, operator=Depends(operator_identity),
                  proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    return traced(request, RequestManagementService(proposals, aftersales).purge(request_id, body.management_token, operator, body.reason))


@router.get('/requests/{request_id}/history')
def request_history(request_id: UUID, request: Request, before_revision: int | None = Query(None, ge=1),
                    limit: int = Query(10, ge=1, le=50), operator=Depends(operator_identity),
                    proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    return traced(request, RequestService(proposals, aftersales).history(request_id, before_revision=before_revision, limit=limit))


@router.get("/requests/{request_id}")
def request_detail(request_id: UUID, request: Request, include_history: bool = True, operator=Depends(operator_identity),
                   proposals=Depends(get_proposal_service), aftersales=Depends(get_aftersales_service)):
    return traced(request, RequestService(proposals, aftersales).get(request_id, include_history=include_history))


@router.get("/proposals/{proposal_id}/execution")
def execution(proposal_id: UUID, request: Request, operator=Depends(operator_identity), service=Depends(get_aftersales_service)):
    return traced(request, service.get_execution(proposal_id))


@router.get("/aftersales-tickets")
def tickets(request: Request, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0),
            operator=Depends(operator_identity), service=Depends(get_aftersales_service)):
    return traced(request, {"items": service.list_tickets(limit=limit, offset=offset)})


@router.get("/aftersales-tickets/{ticket_id}")
def ticket(ticket_id: UUID, request: Request, operator=Depends(operator_identity), service=Depends(get_aftersales_service)):
    return traced(request, service.get_ticket(ticket_id))


@router.get("/audit-logs")
def audits(request: Request, request_id: UUID, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0),
           operator=Depends(operator_identity), service=Depends(get_proposal_service)):
    with service.sessions() as session:
        query = select(AuditLog).where(AuditLog.request_id == request_id).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        items = [dict(id=str(row.id), request_id=str(row.request_id), proposal_id=str(row.proposal_id) if row.proposal_id else None,
                      ticket_id=str(row.ticket_id) if row.ticket_id else None, event_type=row.event_type, actor_id=row.actor_id,
                      details=row.details, created_at=utc(row.created_at).isoformat()) for row in session.scalars(query)]
    return traced(request, {"items": items})

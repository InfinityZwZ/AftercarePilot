import base64
import json
from datetime import datetime
from uuid import UUID
from sqlalchemy import and_, case, func, or_, select, text, tuple_

from src.domain.clock import utc
from src.domain.exceptions import AppError
from src.domain.hashing import canonical_hash
from src.domain.request_management import management_metadata
from src.services.aftersales_service import ticket_dto
from src.services.proposal_support import checked_payload, proposal_dto
from src.storage.models import AuditLog, AftersalesTicket, Proposal


STATUS_NAMES = {
    "analyzing": "分析中", "needs_info": "待补充", "pending": "待确认",
    "accepted": "申请已受理", "reply_confirmed": "草稿已确认", "manual": "需人工处理",
    "rejected": "方案未采用", "reanalyze": "需重新分析", "failed": "分析失败", "unknown": "待核验",
}


class RequestService:
    def __init__(self, proposals, aftersales):
        self.proposals, self.aftersales = proposals, aftersales

    def status_expression(self):
        p = Proposal
        return case(
            (p.status == "executed", "accepted"),
            (p.error['code'].astext == 'EXECUTION_UNKNOWN', 'unknown'),
            (and_(p.status == "approved", p.kind == "reply_only"), "reply_confirmed"),
            (p.status == "rejected", "rejected"), (p.status == "failed", "failed"),
            (p.status == "analyzing", "analyzing"),
            (or_(p.status == "expired", p.expires_at < utc(self.proposals.clock.now())), "reanalyze"),
            (p.kind == "clarification", "needs_info"), (p.kind == "handoff", "manual"), else_="pending")

    def list(self, *, status=None, q="", limit=20, offset=0, archived='active', updated_from=None,
             updated_to=None, cursor=None, include_total=True):
        with self.proposals.sessions() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            state = self.status_expression()
            conditions = [Proposal.is_current.is_(True)]
            if archived != 'all':
                conditions.append(Proposal.archived_at.is_(None) if archived == 'active' else Proposal.archived_at.is_not(None))
            if updated_from is not None:
                conditions.append(Proposal.updated_at >= utc(updated_from))
            if updated_to is not None:
                conditions.append(Proposal.updated_at < utc(updated_to))
            if status:
                conditions.append(state == status)
            if q.strip():
                matching_request = select(Proposal.request_id).where(or_(
                    Proposal.source_question.icontains(q.strip(), autoescape=True),
                    Proposal.order_id.icontains(q.strip(), autoescape=True)))
                conditions.append(Proposal.request_id.in_(matching_request))
            total = session.scalar(select(func.count()).select_from(Proposal).where(*conditions)) if include_total else None
            scope = canonical_hash(dict(status=status, q=q, archived=archived,
                updated_from=utc(updated_from).isoformat() if updated_from else None,
                updated_to=utc(updated_to).isoformat() if updated_to else None))
            if cursor:
                try:
                    value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    timestamp, identity = datetime.fromisoformat(value['at']), UUID(value['id'])
                    if value['scope'] != scope or timestamp.utcoffset() is None or offset:
                        raise ValueError()
                except (ValueError, TypeError, KeyError, UnicodeError):
                    raise AppError('VALIDATION_ERROR', 'Invalid cursor or filters changed', 422) from None
                conditions.append(tuple_(Proposal.updated_at, Proposal.id) < tuple_(timestamp, identity))
            columns = [getattr(Proposal, key) for key in ('id', 'request_id', 'revision', 'status', 'kind',
                'payload_hash', 'updated_at', 'archived_at', 'archived_by', 'management_version', 'source_question', 'order_id')]
            rows = list(session.execute(select(*columns, state.label("business_status")).where(*conditions)
                .order_by(Proposal.updated_at.desc(), Proposal.id.desc()).limit(limit + 1).offset(offset)))
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [dict(request_id=str(row.request_id), proposal_id=str(row.id),
                question=row.source_question, order_id=row.order_id, status=row.business_status,
                status_label=STATUS_NAMES[row.business_status], updated_at=utc(row.updated_at).isoformat(),
                **management_metadata(row)) for row in rows]
            next_cursor = base64.urlsafe_b64encode(json.dumps(dict(at=utc(rows[-1].updated_at).isoformat(),
                id=str(rows[-1].id), scope=scope)).encode()).decode() if rows and has_more else None
        return dict(items=items, total=total, limit=limit, offset=offset, next_cursor=next_cursor, has_more=has_more)

    def get(self, request_id, *, include_history=True):
        with self.proposals.sessions() as session:
            session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            for _ in range(3):
                current = session.scalar(select(Proposal).where(Proposal.request_id == request_id,
                    Proposal.is_current.is_(True)).with_for_update(read=True))
                if current is not None:
                    break
                if not session.scalar(select(Proposal.id).where(Proposal.request_id == request_id).limit(1)):
                    raise AppError("REQUEST_NOT_FOUND", "Request not found", 404)
            else:
                raise AppError("REQUEST_CONFLICT", "Current version is changing; reload the request", retryable=True)
            rows = list(session.scalars(select(Proposal).where(Proposal.request_id == request_id).order_by(Proposal.revision))) if include_history else []
            question_rows = rows if include_history else session.execute(select(Proposal.source_question, Proposal.created_at)
                .where(Proposal.request_id == request_id).order_by(Proposal.revision))
            status = session.scalar(select(self.status_expression()).where(Proposal.id == current.id))
            own = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == current.id))
            ticket = own or session.scalar(select(AftersalesTicket).where(AftersalesTicket.order_id == current.order_id,
                AftersalesTicket.status.in_(("requested", "processing"))).limit(1))
            verification = None
            if own or current.status == "executed":
                try:
                    if own is None:
                        raise AppError("EXECUTION_INCONSISTENT", "Missing ticket", 500)
                    verification = self.aftersales._verified(session, current, own,
                        checked_payload(current, current.revision, current.payload_hash))
                    status = "accepted"
                except AppError:
                    status = "unknown"
            questions = []
            for row in question_rows:
                if not questions or questions[-1]["text"] != row.source_question:
                    questions.append(dict(text=row.source_question, created_at=utc(row.created_at).isoformat()))
            return dict(request_id=str(request_id), status=status, status_label=STATUS_NAMES[status],
                current=proposal_dto(current), history=[proposal_dto(row) for row in rows], questions=questions,
                ticket=ticket_dto(ticket) if ticket else None, own_ticket=own is not None, verification=verification,
                **management_metadata(current))

    def history(self, request_id, *, before_revision=None, limit=10):
        with self.proposals.sessions() as session:
            query = select(Proposal.id, Proposal.revision, Proposal.status, Proposal.kind, Proposal.created_at).where(Proposal.request_id == request_id)
            if before_revision is not None:
                query = query.where(Proposal.revision < before_revision)
            rows = list(session.execute(query.order_by(Proposal.revision.desc()).limit(limit + 1)))
            more = len(rows) > limit
            return dict(items=[dict(proposal_id=str(row.id), revision=row.revision, status=row.status,
                kind=row.kind, created_at=utc(row.created_at).isoformat()) for row in rows[:limit]],
                next_revision=rows[limit - 1].revision if more else None)

import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.domain.clock import SystemClock, utc
from src.domain.exceptions import AppError
from src.domain.hashing import canonical_hash
from src.domain.request_management import require_active
from src.domain.proposals import PAYLOAD_ADAPTER, HandoffPayload
from src.services.proposal_support import audit, checked_payload, locked_proposal, proposal_dto, require_current, require_unexpired
from src.storage import database
from src.storage.models import Proposal


class ProposalService:
    def __init__(self, sessions=None, clock=None):
        self._sessions = sessions
        self.clock = clock or SystemClock()

    @property
    def sessions(self):
        return self._sessions or database.session_factory()

    def _start(self, message, request_id, language, operator):
        now = utc(self.clock.now())
        try:
            with self.sessions.begin() as session:
                previous = None
                if request_id:
                    previous = session.scalar(select(Proposal).where(Proposal.request_id == request_id, Proposal.is_current).with_for_update())
                    if previous is None:
                        if session.scalar(select(Proposal.id).where(Proposal.request_id == request_id).limit(1)):
                            raise AppError("REQUEST_CONFLICT", "A concurrent revision became current; reload the request")
                        raise AppError("REQUEST_NOT_FOUND", "Request not found", 404)
                    require_active(previous)
                    if previous.status in ("analyzing", "executed"):
                        raise AppError("REQUEST_BUSY" if previous.status == "analyzing" else "REQUEST_EXECUTED",
                                       "This request is analyzing or already executed; use a new request")
                    previous.is_current = False
                    if previous.status in ("pending_approval", "approved", "needs_info"):
                        previous.status = "superseded"
                    previous.updated_at = now
                    session.flush()
                    audit(session, previous, "proposal_superseded", operator, now)
                identity = dict(proposal_id=uuid4(), request_id=request_id or uuid4(), revision=previous.revision + 1 if previous else 1)
                row = Proposal(id=identity["proposal_id"], request_id=identity["request_id"], revision=identity["revision"],
                               is_current=True, status="analyzing", kind="handoff", source_question=message,
                               entities={"language": language}, payload={}, expires_at=now + timedelta(hours=settings.plan_ttl_hours),
                               created_at=now, updated_at=now, management_version=previous.management_version if previous else 0)
                session.add(row)
                session.flush()
                audit(session, row, "analysis_started", operator, now)
                context = {key: previous.entities.get(key) for key in ("order_id", "reason", "intent")} if previous else {}
                if previous:
                    context["previous_question"] = previous.source_question[-2000:]
            return {**identity, "context": context, "message": message, "language": language}
        except IntegrityError:
            raise AppError("REQUEST_CONFLICT", "A concurrent revision already became current; reload the request") from None

    def _finish(self, identity, state, operator):
        now = utc(self.clock.now())
        with self.sessions.begin() as session:
            row = locked_proposal(session, identity["proposal_id"])
            if row.status != "analyzing" or not row.is_current:
                raise AppError("ANALYSIS_INTERRUPTED", "Analysis is no longer current")
            entities = deepcopy(state.get("entities", {}))
            entities["language"] = identity["language"]
            entities["usage"] = {key: state.get(key, 0) for key in ("llm_calls", "tool_calls", "tool_rounds", "schema_repairs", "evidence_completions")}
            row.entities = entities
            error = state.get("error")
            payload = state.get("payload")
            if error or not payload:
                error = error or {"code": "ANALYSIS_FAILED", "message": "Analysis did not produce a validated Proposal"}
                payload = HandoffPayload(**{key: identity[key] for key in ("proposal_id", "request_id", "revision")},
                                         kind="handoff", action="none", summary="分析未完成" if identity["language"] == "zh" else "Analysis failed",
                                         draft_reply="分析未完成，请重新分析或转人工；没有创建工单。" if identity["language"] == "zh" else "Analysis failed. Please retry or hand off; no ticket was created.",
                                         language=identity["language"], evidence_refs=[], evidence=state.get("evidence", []),
                                         handoff_reason=error["code"]).model_dump(mode="json")
                row.status, row.error = "failed", error
            else:
                payload = PAYLOAD_ADAPTER.validate_python(payload).model_dump(mode="json")
                row.status = "needs_info" if payload["kind"] == "clarification" else "pending_approval"
            row.kind = payload["kind"]
            row.payload = payload
            row.payload_hash = canonical_hash(payload)
            # Only identifiers backed by successful reads become foreign keys.
            row.order_id, row.policy_id = payload.get("order_id"), payload.get("policy_id")
            if payload["kind"] == "return_request":
                from datetime import datetime
                row.expires_at = min(row.expires_at, datetime.fromisoformat(payload["deadline_at"].replace("Z", "+00:00")))
            row.updated_at = now
            session.flush()
            audit(session, row, "analysis_failed" if error else "analysis_completed", operator, now, {"usage": entities["usage"], "error_code": error["code"] if error else None})
            audit(session, row, "proposal_created", operator, now)
            result = proposal_dto(row)
        return result

    async def analyze(self, message, *, request_id=None, language="zh", operator, agent):
        identity = await asyncio.to_thread(self._start, message, request_id, language, operator)
        try:
            state = await asyncio.wait_for(agent.run(identity), timeout=settings.analysis_timeout_seconds)
        except TimeoutError:
            state = {**getattr(agent, "last_state", {}), "error": {"code": "ANALYSIS_TIMEOUT", "message": "Analysis time limit exceeded"}}
        except Exception:
            state = {"error": {"code": "ANALYSIS_FAILED", "message": "Analysis could not complete"}}
        result = await asyncio.to_thread(self._finish, identity, state, operator)
        if result["status"] == "failed":
            status = 503 if result["error"]["code"] in ("MODEL_UNAVAILABLE", "MCP_UNAVAILABLE", "MCP_TOOLSET_INVALID") else 502
            raise AppError(result["error"]["code"], result["error"]["message"], status,
                           details={"proposal_id": result["proposal_id"], "request_id": result["request_id"]})
        return result

    def get(self, proposal_id):
        with self.sessions() as session:
            row = session.get(Proposal, proposal_id)
            if not row:
                raise AppError("PROPOSAL_NOT_FOUND", "Proposal not found", 404)
            return proposal_dto(row)

    def list(self, *, status=None, request_id=None, limit=20, offset=0):
        query = select(Proposal).order_by(Proposal.created_at.desc(), Proposal.revision.desc()).limit(limit).offset(offset)
        if status:
            query = query.where(Proposal.status == status)
        if request_id:
            query = query.where(Proposal.request_id == request_id)
        with self.sessions() as session:
            return [proposal_dto(row) for row in session.scalars(query)]

    def revise(self, proposal_id, revision, expected_hash, draft_reply, operator):
        now = utc(self.clock.now())
        with self.sessions.begin() as session:
            previous = locked_proposal(session, proposal_id)
            now = utc(self.clock.now())
            checked_payload(previous, revision, expected_hash)
            require_current(previous)
            require_unexpired(previous, now)
            if previous.status not in ("pending_approval", "approved", "rejected", "needs_info"):
                raise AppError("REVISION_NOT_ALLOWED", "This Proposal cannot be edited")
            new_id = uuid4()
            payload = deepcopy(previous.payload)
            payload.update(proposal_id=str(new_id), revision=previous.revision + 1, draft_reply=draft_reply)
            payload = PAYLOAD_ADAPTER.validate_python(payload).model_dump(mode="json")
            previous.is_current = False
            if previous.status in ("pending_approval", "approved", "needs_info"):
                previous.status = "superseded"
            previous.updated_at = now
            session.flush()  # release the current partial-unique slot first
            row = Proposal(id=new_id, request_id=previous.request_id, revision=previous.revision + 1, is_current=True,
                           status="needs_info" if previous.kind == "clarification" else "pending_approval", kind=previous.kind,
                           source_question=previous.source_question, order_id=previous.order_id, policy_id=previous.policy_id,
                           entities=deepcopy(previous.entities), payload=payload, payload_hash=canonical_hash(payload),
                           expires_at=previous.expires_at, created_at=now, updated_at=now, management_version=previous.management_version)
            session.add(row)
            session.flush()
            audit(session, previous, "proposal_superseded", operator, now, {"new_proposal_id": str(row.id)})
            audit(session, row, "proposal_created", operator, now, {"previous_proposal_id": str(previous.id)})
            result = proposal_dto(row)
        return result

    def review(self, proposal_id, revision, expected_hash, operator, *, reject_reason=None):
        now = utc(self.clock.now())
        with self.sessions.begin() as session:
            row = locked_proposal(session, proposal_id)
            now = utc(self.clock.now())
            checked_payload(row, revision, expected_hash)
            require_current(row)
            desired = "rejected" if reject_reason is not None else "approved"
            if row.status == desired or (desired == "approved" and row.status == "executed"):
                if desired == "approved" and row.approved_payload_hash != row.payload_hash:
                    raise AppError("APPROVAL_MISMATCH", "Approved content does not match the stored payload")
                if desired == "rejected" and row.rejection_reason != reject_reason:
                    raise AppError("REVIEW_ALREADY_DECIDED", "Rejection already recorded with a different reason")
                return proposal_dto(row)
            if row.status != "pending_approval":
                raise AppError("REVIEW_NOT_ALLOWED", "Only the current pending Proposal can be reviewed")
            require_unexpired(row, now)
            row.status, row.reviewer_id, row.reviewed_at, row.updated_at = desired, operator, now, now
            row.approved_payload_hash = row.payload_hash if desired == "approved" else None
            row.rejection_reason = reject_reason
            audit(session, row, "proposal_rejected" if reject_reason is not None else "proposal_approved", operator, now,
                  {"revision": row.revision, "payload_hash": row.payload_hash})
            result = proposal_dto(row)
        return result

    def recover_interrupted(self):
        now = utc(self.clock.now())
        with self.sessions.begin() as session:
            rows = session.scalars(select(Proposal).where(Proposal.status == "analyzing").with_for_update())
            for row in rows:
                row.status, row.updated_at = "failed", now
                row.error = {"code": "ANALYSIS_INTERRUPTED", "message": "Process restarted during analysis; analyze again"}
                audit(session, row, "analysis_failed", "system", now, row.error)

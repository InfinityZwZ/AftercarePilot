from sqlalchemy import delete, func, or_, select

from src.domain.clock import utc
from src.domain.exceptions import AppError
from src.domain.request_management import management_metadata
from src.services.proposal_support import audit, checked_payload
from src.storage.models import AuditLog, AftersalesTicket, Proposal


class RequestManagementService:
    def __init__(self, proposals, aftersales, fault_hook=None):
        self.proposals, self.aftersales, self.fault_hook = proposals, aftersales, fault_hook

    def _current(self, session, request_id):
        for _ in range(3):
            row = session.scalar(select(Proposal).where(Proposal.request_id == request_id,
                Proposal.is_current.is_(True)).with_for_update())
            if row is not None:
                return row
            if not session.scalar(select(Proposal.id).where(Proposal.request_id == request_id).limit(1)):
                raise AppError('REQUEST_NOT_FOUND', 'Request not found', 404)
        raise AppError('REQUEST_CONFLICT', 'Current version changed; reload')

    def _locked(self, session, request_id, token):
        row = self._current(session, request_id)
        if management_metadata(row)['management_token'] != token:
            raise AppError('STALE_MANAGEMENT', 'Request changed; reload before managing it')
        self._safe(session, row)
        return row

    def _safe(self, session, row):
        if row.status == 'analyzing':
            raise AppError('REQUEST_BUSY', 'Analysis is still running')
        own = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == row.id))
        if own or row.status == 'executed':
            if own is None:
                raise AppError('EXECUTION_INCONSISTENT', 'Execution records need verification')
            self.aftersales._verified(session, row, own, checked_payload(row, row.revision, row.payload_hash))
        else:
            if row.payload_hash:
                checked_payload(row, row.revision, row.payload_hash)
            if (row.error or {}).get('code') == 'EXECUTION_UNKNOWN' or session.scalar(select(AuditLog.id).where(AuditLog.proposal_id == row.id,
                    AuditLog.event_type == 'execution_unknown').limit(1)):
                raise AppError('EXECUTION_UNKNOWN', 'Query the result and analyze again before managing this request')

    def manage(self, items, operator, *, archive):
        result = []
        with self.proposals.sessions.begin() as session:
            # All batch operations use the same request order, including overlapping batches.
            rows = [self._locked(session, identity, token) for identity, token in sorted(items, key=lambda item: str(item[0]))]
            for row in rows:
                if (row.archived_at is not None) == archive:
                    raise AppError('MANAGEMENT_STATE', 'Request is already in this archive state')
                now = utc(self.proposals.clock.now())
                row.archived_at, row.archived_by = (now, operator) if archive else (None, None)
                row.management_version += 1
                row.updated_at = now
                audit(session, row, 'request_archived' if archive else 'request_restored', operator, now)
                result.append(dict(request_id=str(row.request_id), **management_metadata(row)))
            if self.fault_hook:
                self.fault_hook('before_commit')
        return dict(items=result)

    def _purgeable(self, session, row):
        if row.archived_at is None:
            raise AppError('ARCHIVE_REQUIRED', 'Archive the request before permanent removal')
        versions = list(session.scalars(select(Proposal.id).where(Proposal.request_id == row.request_id)
            .order_by(Proposal.revision).with_for_update()))
        ticket = session.scalar(select(AftersalesTicket.id).where(AftersalesTicket.proposal_id.in_(versions)).limit(1))
        if ticket:
            raise AppError('REQUEST_HAS_TICKETS', 'A linked ticket prevents permanent removal')
        # Audit FKs can refer across requests. Never cascade into unrelated history or tickets.
        cross = session.scalar(select(AuditLog.id).where(or_(
            (AuditLog.proposal_id.in_(versions)) & (AuditLog.request_id != row.request_id),
            (AuditLog.request_id == row.request_id) & or_(AuditLog.ticket_id.is_not(None),
                AuditLog.proposal_id.is_not(None) & AuditLog.proposal_id.not_in(versions)))).limit(1))
        if cross:
            raise AppError('CROSS_REQUEST_REFERENCE', 'Cross-request records prevent permanent removal')
        return versions

    def preview(self, request_id):
        with self.proposals.sessions.begin() as session:
            row = self._current(session, request_id)
            self._safe(session, row)
            versions = self._purgeable(session, row)
            return dict(request_id=str(request_id), question=row.source_question, order_id=row.order_id,
                        version_count=len(versions), **management_metadata(row))

    def purge(self, request_id, token, operator, reason):
        with self.proposals.sessions.begin() as session:
            # Serializes repeated purges even after the current row has been deleted.
            session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(request_id), 0))))
            tombstone = session.scalar(select(AuditLog).where(AuditLog.request_id == request_id,
                AuditLog.event_type == 'request_purged'))
            if tombstone:
                return dict(request_id=str(request_id), purged=True, version_count=tombstone.details['version_count'])
            row = self._locked(session, request_id, token)
            versions = self._purgeable(session, row)
            session.execute(delete(AuditLog).where(AuditLog.request_id == request_id))
            session.execute(delete(Proposal).where(Proposal.id.in_(versions)))
            session.add(AuditLog(request_id=request_id, event_type='request_purged', actor_id=operator,
                details=dict(version_count=len(versions), reason=reason), created_at=utc(self.proposals.clock.now())))
            session.flush()
            if self.fault_hook:
                self.fault_hook('before_commit')
        return dict(request_id=str(request_id), purged=True, version_count=len(versions))

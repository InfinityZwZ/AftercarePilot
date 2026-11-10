from src.domain.clock import utc
from src.domain.exceptions import AppError
from src.domain.hashing import canonical_hash


def management_metadata(row):
    values = {key: getattr(row, key) for key in ('id', 'request_id', 'revision', 'status', 'kind',
        'payload_hash', 'updated_at', 'archived_at', 'archived_by', 'management_version')}
    serialized = {key: utc(value).isoformat() if key in ('updated_at', 'archived_at') and value
                  else str(value) if key in ('id', 'request_id') else value for key, value in values.items()}
    return dict(archived_at=serialized['archived_at'], archived_by=row.archived_by,
                management_version=row.management_version, management_token=canonical_hash(serialized))


def require_active(row):
    if row.archived_at is not None:
        raise AppError('REQUEST_ARCHIVED', 'Restore this request before changing it')

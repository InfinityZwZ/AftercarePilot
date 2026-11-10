"""Add request management metadata and bounded-query indexes."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = depends_on = None


def upgrade():
    op.add_column('proposals', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('proposals', sa.Column('archived_by', sa.String(64), nullable=True))
    op.add_column('proposals', sa.Column('management_version', sa.Integer(), nullable=False, server_default=sa.text('0')))
    op.create_check_constraint('management_version', 'proposals', 'management_version >= 0')
    op.create_check_constraint('archive_pair', 'proposals', '(archived_at IS NULL) = (archived_by IS NULL)')
    op.create_index('ix_requests_active_updated', 'proposals', ['updated_at', 'id'], postgresql_where=sa.text('is_current AND archived_at IS NULL'))
    op.create_index('ix_requests_archived_updated', 'proposals', ['updated_at', 'id'], postgresql_where=sa.text('is_current AND archived_at IS NOT NULL'))
    op.create_index('ix_audit_execution', 'audit_logs', ['proposal_id', 'ticket_id', 'event_type'])


def downgrade():
    op.drop_index('ix_audit_execution', 'audit_logs')
    for name in ('ix_requests_active_updated', 'ix_requests_archived_updated'):
        op.drop_index(name, 'proposals')
    for name in ('archive_pair', 'management_version'):
        op.drop_constraint(op.f('ck_proposals_' + name), 'proposals', type_='check')
    for name in ('management_version', 'archived_by', 'archived_at'):
        op.drop_column('proposals', name)

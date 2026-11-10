from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import func, select

from src.domain.exceptions import AppError
from src.storage.models import AuditLog, Proposal, AftersalesTicket, Order
from tests.test_stage3_api import api, create, guard
from tests.test_proposal_approval import counts


def test_requests_group_filter_paginate_and_history(api, stage3):
    first = create(api)
    revision = api.post(f"/api/v1/proposals/{first['proposal_id']}/revisions", json={**guard(first), 'draft_reply': '修改回复'}).json()
    from datetime import timedelta
    from src.domain.clock import FixedClock
    stage3["proposals"].clock = FixedClock(stage3["clock"].now() + timedelta(seconds=1))
    second = create(api)
    response = api.get('/api/v1/requests', params={'q': 'ORD-1009', 'limit': 1})
    assert response.status_code == 200
    assert response.json()['total'] == 2
    assert len(response.json()['items']) == 1
    assert response.json()['items'][0]['request_id'] == second['request_id']
    detail = api.get('/api/v1/requests/' + first['request_id']).json()
    assert detail['current']['proposal_id'] == revision['proposal_id']
    assert len(detail['history']) == 2
    assert detail['status'] == 'pending'
    assert api.get('/api/v1/requests', params={'status': 'accepted', 'q': 'ORD-1009'}).json()['total'] == 0
    assert api.get('/api/v1/requests', params={'q': '不存在'}).json()['total'] == 0
    assert counts(stage3) == (0, 0)


def test_confirm_and_create_atomic_replay_and_readback(api, stage3):
    row = create(api)
    route = '/api/v1/proposals/' + row['proposal_id']
    assert counts(stage3) == (0, 0)
    result = api.post(route + '/confirm-and-create', json=guard(row))
    assert result.status_code == 200, result.json()
    assert counts(stage3) == (1, 1)
    assert api.post(route + '/confirm-and-create', json=guard(row)).json()['ticket'] == result.json()['ticket']
    detail = api.get('/api/v1/requests/' + row['request_id']).json()
    assert detail['status'] == 'accepted'
    assert detail['ticket']['id'] == result.json()['ticket']['id']
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.proposal_id == UUID(row['proposal_id']), AuditLog.event_type == 'proposal_approved')) == 1


@pytest.mark.parametrize('phase', ['after_approval_flush', 'after_ticket_flush', 'after_status_flush', 'after_audit_flush', 'before_commit'])
def test_confirmation_failure_rolls_back_approval_too(api, stage3, phase):
    row = create(api)
    def fail(point, session):
        if point == phase:
            raise RuntimeError('injected rollback')
    stage3['aftersales']._fault_hook = fail
    response = api.post('/api/v1/proposals/' + row['proposal_id'] + '/confirm-and-create', json=guard(row))
    assert response.status_code == 500
    assert counts(stage3) == (0, 0)
    stored = stage3['proposals'].get(UUID(row['proposal_id']))
    assert stored['status'] == 'pending_approval' and stored['reviewer_id'] is None
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.proposal_id == UUID(row['proposal_id']), AuditLog.event_type == 'proposal_approved')) == 0


def test_concurrent_confirmation_single_ticket(api, stage3):
    row = create(api)
    def submit(_):
        return stage3['aftersales'].confirm_and_create(UUID(row['proposal_id']), row['revision'], row['payload_hash'], 'server_operator')
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0]['ticket']['id'] == results[1]['ticket']['id']
    assert counts(stage3) == (1, 1)


def test_confirm_rejects_old_hash_and_nonreturn(api, stage3):
    row = create(api)
    route = '/api/v1/proposals/' + row['proposal_id']
    assert api.post(route + '/confirm-and-create', json={**guard(row), 'expected_payload_hash': '0' * 64}).status_code == 409
    api.post(route + '/revisions', json={**guard(row), 'draft_reply': '新版本'})
    assert api.post(route + '/confirm-and-create', json=guard(row)).status_code == 409
    from src.agent.nodes import AnalysisAgent
    from src.api.dependencies import get_analyzer
    from tests.fakes import FakeLLM
    import src.main as main
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(intent='general', kind='reply_only'), stage3['gateway'])
    other = api.post('/api/v1/proposals/analyze', json={'message': '咨询问题'}).json()
    assert api.post('/api/v1/proposals/' + other['proposal_id'] + '/confirm-and-create', json=guard(other)).status_code == 422
    assert counts(stage3) == (0, 0)


def test_commit_response_loss_resolved_by_request_read(api, stage3):
    row = create(api)
    def lose(point, session):
        if point == 'after_commit':
            raise RuntimeError('lost response')
    stage3['aftersales']._fault_hook = lose
    response = api.post('/api/v1/proposals/' + row['proposal_id'] + '/confirm-and-create', json=guard(row))
    assert response.status_code == 202
    assert api.get('/api/v1/requests/' + row['request_id']).json()['status'] == 'accepted'
    assert counts(stage3) == (1, 1)


@pytest.mark.parametrize('change', ['ticket_key', 'audit_key', 'extra_audit', 'missing_audit', 'payload'])
def test_list_reports_stored_progress_but_detail_verifies_integrity(api, stage3, change):
    row = create(api)
    api.post('/api/v1/proposals/' + row['proposal_id'] + '/confirm-and-create', json=guard(row))
    with stage3['sessions'].begin() as session:
        ticket = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == UUID(row['proposal_id'])))
        audit = session.scalar(select(AuditLog).where(AuditLog.ticket_id == ticket.id, AuditLog.event_type == 'ticket_created'))
        if change == 'payload':
            proposal = session.get(Proposal, UUID(row['proposal_id']))
            proposal.payload = {**proposal.payload, 'summary': '内容被更改'}
        elif change == 'ticket_key':
            ticket.idempotency_key = '0' * 64
        elif change == 'audit_key':
            audit.details = {**audit.details, 'idempotency_key': '0' * 64}
        elif change == 'extra_audit':
            session.add(AuditLog(request_id=audit.request_id, proposal_id=audit.proposal_id, ticket_id=ticket.id,
                event_type='ticket_created', actor_id='other', details={}, created_at=audit.created_at))
        else:
            session.delete(audit)
    detail = api.get('/api/v1/requests/' + row['request_id']).json()
    assert detail['status'] == 'unknown'
    assert api.get('/api/v1/requests', params={'status': 'unknown', 'q': 'ORD-1009'}).json()['total'] == 0
    assert api.get('/api/v1/requests', params={'status': 'accepted', 'q': 'ORD-1009'}).json()['total'] == 1


def test_changed_facts_requires_reanalysis_after_failed_confirmation(api, stage3):
    row = create(api)
    with stage3['sessions'].begin() as session:
        session.get(Order, 'ORD-1009').total_cents += 100
    response = api.post('/api/v1/proposals/' + row['proposal_id'] + '/confirm-and-create', json=guard(row))
    assert response.status_code == 409
    assert api.get('/api/v1/requests/' + row['request_id']).json()['status'] == 'reanalyze'
    assert api.get('/api/v1/requests', params={'status': 'reanalyze'}).json()['total'] == 1
    assert counts(stage3) == (0, 0)


def test_query_waits_for_inflight_confirmation(api, stage3):
    from threading import Event
    from time import sleep
    row = create(api)
    entered, release = Event(), Event()
    def pause(point, session):
        if point == 'after_ticket_flush':
            entered.set()
            assert release.wait(5)
    stage3['aftersales']._fault_hook = pause
    with ThreadPoolExecutor(2) as pool:
        submission = pool.submit(stage3['aftersales'].confirm_and_create, UUID(row['proposal_id']), row['revision'], row['payload_hash'], 'server_operator')
        assert entered.wait(5)
        query = pool.submit(stage3['aftersales'].get_execution, UUID(row['proposal_id']))
        try:
            sleep(0.15)
            assert not query.done(), 'An in-flight submission must not be reported as not executed'
        finally:
            release.set()
        assert query.result()['ticket']['id'] == submission.result()['ticket']['id']


def test_detail_waits_for_revision_and_returns_new_current(api, stage3):
    from threading import Event
    from sqlalchemy import event
    from src.services.request_service import RequestService
    row = create(api)
    entered, release = Event(), Event()
    def pause(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith('insert into proposals'):
            entered.set()
            assert release.wait(5)
    event.listen(stage3['engine'], 'after_cursor_execute', pause)
    try:
        with ThreadPoolExecutor(2) as pool:
            revision = pool.submit(stage3['proposals'].revise, UUID(row['proposal_id']), row['revision'], row['payload_hash'], '新版本草稿', 'server_operator')
            assert entered.wait(5)
            detail = pool.submit(RequestService(stage3['proposals'], stage3['aftersales']).get, UUID(row['request_id']))
            from time import sleep
            sleep(.15)
            release.set()
            assert detail.result()['current']['proposal_id'] == revision.result()['proposal_id']
    finally:
        release.set()
        event.remove(stage3['engine'], 'after_cursor_execute', pause)


@pytest.mark.parametrize('injected', [{'reviewer_id': 'forged'}, {'requested_amount_cents': 1}, {'order_id': 'ORD-1001'}])
def test_combined_confirmation_rejects_injected_business_fields(api, stage3, injected):
    row = create(api)
    result = api.post('/api/v1/proposals/' + row['proposal_id'] + '/confirm-and-create', json={**guard(row), **injected})
    assert result.status_code == 422 and counts(stage3) == (0, 0)


def test_reply_confirmation_survives_ttl_and_current_list_is_paginated(api, stage3):
    from datetime import timedelta
    from src.agent.nodes import AnalysisAgent
    from src.api.dependencies import get_analyzer
    from src.domain.clock import FixedClock
    from tests.fakes import FakeLLM
    import src.main as main
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(intent='general', kind='reply_only'), stage3['gateway'])
    row = create(api)
    assert api.post('/api/v1/proposals/' + row['proposal_id'] + '/approve', json=guard(row)).status_code == 200
    stage3['proposals'].clock = FixedClock(stage3['clock'].now() + timedelta(days=2))
    assert api.get('/api/v1/requests/' + row['request_id']).json()['status'] == 'reply_confirmed'
    page = api.get('/api/v1/requests', params={'status': 'reply_confirmed', 'limit': 1}).json()
    assert page['total'] == 1 and page['items'][0]['request_id'] == row['request_id']
    assert counts(stage3) == (0, 0)

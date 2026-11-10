from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import event, func, select

from src.storage.models import AuditLog, Proposal, AftersalesTicket
from src.domain.exceptions import AppError
from src.services.request_management_service import RequestManagementService
from tests.test_stage3_api import api, create, guard

ROOT = '/api/v1/requests'


def detail(api, row):
    return api.get(f'{ROOT}/{row["request_id"]}', params={'include_history': False}).json()


def management(api, row, action, token=None):
    token = token or detail(api, row)['management_token']
    return api.post(f'{ROOT}/{row["request_id"]}/{action}', json={'management_token': token})


def test_archive_restore_guard_and_all_old_writes(api, stage3):
    row = create(api)
    token = detail(api, row)['management_token']
    assert management(api, row, 'archive', token).status_code == 200
    archived = detail(api, row)
    assert archived['archived_at'] and archived['current']['payload_hash'] == row['payload_hash']
    assert api.get(ROOT).json()['total'] == 1  # the seeded fixture remains active
    assert api.get(ROOT, params={'archived': 'archived'}).json()['total'] == 1
    route = '/api/v1/proposals/' + row['proposal_id']
    for action, body in [('revisions', {**guard(row), 'draft_reply': '修改'}), ('approve', guard(row)),
                         ('reject', {**guard(row), 'rejection_reason': '不用'}),
                         ('execute', guard(row)), ('confirm-and-create', guard(row))]:
        response = api.post(route + '/' + action, json=body)
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'REQUEST_ARCHIVED'
    for route in ('/api/v1/chat', '/api/v1/proposals/analyze'):
        response = api.post(route, json={'request_id': row['request_id'], 'message': '补充订单信息'})
        assert response.json()['error']['code'] == 'REQUEST_ARCHIVED'
    assert management(api, row, 'restore', token).status_code == 409
    assert management(api, row, 'restore').status_code == 200
    assert management(api, row, 'archive', token).status_code == 409
    revised = api.post('/api/v1/proposals/' + row['proposal_id'] + '/revisions',
                       json={**guard(row), 'draft_reply': '新的回复'}).json()
    assert detail(api, revised)['management_version'] == 2


def test_batch_atomic_tokens_limits_and_auth(api, stage3):
    rows = [create(api), create(api)]
    items = [{'request_id': row['request_id'], 'management_token': detail(api, row)['management_token']} for row in rows]
    bad = [{**items[0]}, {**items[1], 'management_token': '0' * 64}]
    assert api.post(ROOT + '/batch-archive', json={'items': bad}).status_code == 409
    assert all(detail(api, row)['archived_at'] is None for row in rows)
    assert api.post(ROOT + '/batch-archive', json={'items': items}).status_code == 200
    assert api.post(ROOT + '/batch-restore', json={'items': items}).status_code == 409
    assert api.post(ROOT + '/batch-archive', json={'items': items * 26}).status_code == 422
    assert api.post(ROOT + '/batch-archive', json={'items': [items[0], items[0]]}).status_code == 422
    assert api.post(ROOT + '/batch-archive', json={'items': items}, headers={'X-Operator-Token': 'bad'}).status_code == 401


def test_purge_preview_minimal_tombstone_and_repeat(api, stage3):
    row = create(api)
    token = detail(api, row)['management_token']
    body = {'management_token': token, 'reason': 'test_data', 'confirmation': '永久清除'}
    assert api.post(f'{ROOT}/{row["request_id"]}/purge', json=body).status_code == 409
    assert management(api, row, 'archive').status_code == 200
    preview = api.get(f'{ROOT}/{row["request_id"]}/purge-preview').json()
    assert preview['version_count'] == 1 and preview['question'] == row['source_question']
    body['management_token'] = preview['management_token']
    assert api.post(f'{ROOT}/{row["request_id"]}/purge', json={**body, 'confirmation': '清除'}).status_code == 422
    for _ in range(2):
        assert api.post(f'{ROOT}/{row["request_id"]}/purge', json=body).status_code == 200
    assert api.get(f'{ROOT}/{row["request_id"]}').status_code == 404
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(Proposal).where(Proposal.request_id == UUID(row['request_id']))) == 0
        logs = list(session.scalars(select(AuditLog).where(AuditLog.request_id == UUID(row['request_id']))))
        assert len(logs) == 1 and logs[0].event_type == 'request_purged'
        assert logs[0].proposal_id is None and logs[0].ticket_id is None
        assert logs[0].details == {'version_count': 1, 'reason': 'test_data'}


@pytest.mark.parametrize('ticket_status', ['requested', 'processing', 'closed', 'cancelled'])
def test_any_ticket_blocks_purge_but_verified_archive_and_replay_allowed(api, stage3, ticket_status):
    row = create(api)
    route = '/api/v1/proposals/' + row['proposal_id']
    assert api.post(route + '/confirm-and-create', json=guard(row)).status_code == 200
    assert management(api, row, 'archive').status_code == 200
    assert api.post(route + '/confirm-and-create', json=guard(row)).status_code == 200
    with stage3['sessions'].begin() as session:
        ticket = session.scalar(select(AftersalesTicket).where(AftersalesTicket.proposal_id == UUID(row['proposal_id'])))
        ticket.status = ticket_status
    assert api.get(f'{ROOT}/{row["request_id"]}/purge-preview').status_code == 409


def test_list_slim_cursor_history_and_filters(api, stage3):
    rows = [create(api) for _ in range(4)]
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    engine = stage3['sessions'].kw['bind']
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        first = api.get(ROOT, params={'include_total': False, 'limit': 2, 'q': '音箱'}).json()
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert first['total'] is None and first['has_more'] and first['next_cursor']
    selects = [s.lower() for s in statements if s.lstrip().lower().startswith('select')]
    assert len(selects) == 1 and all('proposals.payload,' not in s and 'audit_logs' not in s for s in selects)
    second = api.get(ROOT, params={'include_total': False, 'limit': 2, 'q': '音箱', 'cursor': first['next_cursor']}).json()
    assert not set(x['request_id'] for x in first['items']) & set(x['request_id'] for x in second['items'])
    assert len(second['items']) == 2
    assert api.get(ROOT, params={'updated_from': '2026-10-09T20:00:00+08:00', 'updated_to': '2026-10-09T20:00:01+08:00', 'q': '音箱'}).json()['total'] == 4
    assert api.get(ROOT, params={'updated_from': '2026-10-09T20:00:00'}).status_code == 422
    assert api.get(ROOT, params={'cursor': 'bad'}).status_code == 422
    assert detail(api, rows[0])['history'] == []
    assert api.get(f'{ROOT}/{rows[0]["request_id"]}/history').json()['items'][0]['revision'] == 1


@pytest.mark.parametrize('state', ['analyzing', 'execution_unknown', 'inconsistent'])
def test_unsafe_management_rejected(api, stage3, state):
    row = create(api)
    with stage3['sessions'].begin() as session:
        stored = session.get(Proposal, UUID(row['proposal_id']))
        if state == 'analyzing':
            stored.status = 'analyzing'
        elif state == 'inconsistent':
            stored.payload = {**stored.payload, 'summary': '被改动'}
        else:
            session.add(AuditLog(request_id=stored.request_id, proposal_id=stored.id,
                event_type='execution_unknown', actor_id='system', details={}, created_at=stage3['clock'].now()))
    assert management(api, row, 'archive').status_code == 409


@pytest.mark.parametrize('external', [True, False])
def test_cross_request_reference_blocks_purge(api, stage3, external):
    row, other = create(api), create(api)
    assert management(api, row, 'archive').status_code == 200
    with stage3['sessions'].begin() as session:
        session.add(AuditLog(request_id=UUID(other['request_id'] if external else row['request_id']),
            proposal_id=UUID(row['proposal_id'] if external else other['proposal_id']),
            event_type='cross_reference', actor_id='system', details={}, created_at=stage3['clock'].now()))
    assert api.get(f'{ROOT}/{row["request_id"]}/purge-preview').json()['error']['code'] == 'CROSS_REQUEST_REFERENCE'
    assert detail(api, row)['current']['proposal_id'] == row['proposal_id']


@pytest.mark.parametrize('operation', ['archive', 'purge'])
def test_management_transaction_fault_rolls_back_all_records(api, stage3, operation):
    row = create(api)
    if operation == 'purge':
        management(api, row, 'archive')
    before = detail(api, row)
    with stage3['sessions']() as session:
        count_before = session.scalar(select(func.count()).select_from(AuditLog))
    def fail(point):
        raise RuntimeError('injected transaction fault')
    service = RequestManagementService(stage3['proposals'], stage3['aftersales'], fail)
    with pytest.raises(RuntimeError):
        if operation == 'purge':
            service.purge(UUID(row['request_id']), before['management_token'], 'server_operator', 'test_data')
        else:
            service.manage([(UUID(row['request_id']), before['management_token'])], 'server_operator', archive=True)
    assert detail(api, row)['management_token'] == before['management_token']
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog)) == count_before


def test_revision_in_flight_management_reports_stale_not_missing(api, stage3):
    from threading import Event
    row = create(api)
    token = detail(api, row)['management_token']
    entered, release = Event(), Event()
    def pause(conn, cursor, sql, params, context, many):
        if sql.lower().startswith('insert into proposals'):
            entered.set()
            assert release.wait(5)
    event.listen(stage3['engine'], 'after_cursor_execute', pause)
    service = RequestManagementService(stage3['proposals'], stage3['aftersales'])
    try:
        with ThreadPoolExecutor(2) as pool:
            revision = pool.submit(stage3['proposals'].revise, UUID(row['proposal_id']), 1, row['payload_hash'], '新回复', 'server_operator')
            assert entered.wait(5)
            managed = pool.submit(service.manage, [(UUID(row['request_id']), token)], 'server_operator', archive=True)
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):
                managed.result(.15)
            release.set()
            revision.result()
            with pytest.raises(AppError) as error:
                managed.result()
            assert error.value.code == 'STALE_MANAGEMENT'
    finally:
        release.set()
        event.remove(stage3['engine'], 'after_cursor_execute', pause)


def test_concurrent_archive_confirmation_serializes_and_stale_token_refuses(api, stage3):
    from threading import Event
    from concurrent.futures import TimeoutError
    row = create(api)
    token = detail(api, row)['management_token']
    entered, release = Event(), Event()
    def pause(point, session):
        if point == 'after_ticket_flush':
            entered.set()
            assert release.wait(5)
    stage3['aftersales']._fault_hook = pause
    service = RequestManagementService(stage3['proposals'], stage3['aftersales'])
    with ThreadPoolExecutor(2) as pool:
        execution = pool.submit(stage3['aftersales'].confirm_and_create, UUID(row['proposal_id']), 1, row['payload_hash'], 'server_operator')
        assert entered.wait(5)
        archived = pool.submit(service.manage, [(UUID(row['request_id']), token)], 'server_operator', archive=True)
        try:
            with pytest.raises(TimeoutError):
                archived.result(.15)
        finally:
            release.set()
        execution.result()
        with pytest.raises(AppError) as error:
            archived.result()
        assert error.value.code == 'STALE_MANAGEMENT'
    assert management(api, row, 'archive').status_code == 200


def test_overlapping_batches_and_double_archive_do_not_deadlock(api, stage3):
    rows = [create(api), create(api)]
    items = [(UUID(row['request_id']), detail(api, row)['management_token']) for row in rows]
    service = RequestManagementService(stage3['proposals'], stage3['aftersales'])
    def manage(batch):
        try:
            service.manage(batch, 'server_operator', archive=True)
            return 'ok'
        except AppError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(manage, [items, list(reversed(items))]))
    assert sorted(results) == ['STALE_MANAGEMENT', 'ok']
    assert all(detail(api, row)['management_version'] == 1 for row in rows)


def test_purge_does_not_touch_same_order_request_or_duplicate_protection(api, stage3):
    row, other, new = create(api), create(api), create(api)
    api.post('/api/v1/proposals/' + other['proposal_id'] + '/confirm-and-create', json=guard(other))
    management(api, row, 'archive')
    token = detail(api, row)['management_token']
    service = RequestManagementService(stage3['proposals'], stage3['aftersales'])
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: service.purge(UUID(row['request_id']), token, 'server_operator', 'test_data'), range(2)))
    assert results[0] == results[1]
    assert detail(api, other)['status'] == 'accepted'
    response = api.post('/api/v1/proposals/' + new['proposal_id'] + '/confirm-and-create', json=guard(new))
    assert response.status_code != 200
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event_type == 'ticket_created')) == 1


def test_expired_restore_stays_reanalyze(api, stage3):
    from datetime import timedelta
    from src.domain.clock import FixedClock
    row = create(api)
    management(api, row, 'archive')
    stage3['proposals'].clock = FixedClock(stage3['clock'].now() + timedelta(days=3))
    assert management(api, row, 'restore').status_code == 200
    assert detail(api, row)['status'] == 'reanalyze'


def test_large_isolated_list_uses_partial_index_without_payload_or_audit_reads(api, stage3):
    from copy import deepcopy
    from uuid import uuid4
    from sqlalchemy import insert, text
    from src.domain.hashing import canonical_hash
    row = create(api)
    with stage3['sessions'].begin() as session:
        original = session.get(Proposal, UUID(row['proposal_id']))
        base = {column.name: getattr(original, column.name) for column in Proposal.__table__.columns}
        records = []
        for index in range(2500):
            data = deepcopy(base)
            data.update(id=uuid4(), request_id=uuid4(), source_question=f'隔离性能记录 {index}')
            data['payload'].update(proposal_id=str(data['id']), request_id=str(data['request_id']))
            data['payload_hash'] = canonical_hash(data['payload'])
            records.append(data)
        session.execute(insert(Proposal), records)
        session.execute(text('ANALYZE proposals'))
        plan = session.scalar(text('EXPLAIN (FORMAT JSON) SELECT id, updated_at FROM proposals WHERE is_current AND archived_at IS NULL ORDER BY updated_at DESC, id DESC LIMIT 11'))
        assert 'ix_requests_active_updated' in str(plan)
    assert api.get(ROOT, params={'q': '隔离性能记录', 'include_total': False, 'limit': 10}).json()['has_more']


def test_durable_unknown_is_visible_after_reload_and_reanalysis_resolves(api, stage3):
    row = create(api)
    stage3['aftersales']._failed_audit(UUID(row['proposal_id']), 1, row['payload_hash'], 'server_operator',
        AppError('EXECUTION_UNKNOWN', 'Unknown commit result', 202), confirm=True)
    assert detail(api, row)['status'] == 'unknown'
    assert api.get(ROOT, params={'status': 'unknown'}).json()['total'] == 1
    assert management(api, row, 'archive').status_code == 409
    assert api.get('/api/v1/proposals/' + row['proposal_id'] + '/execution').json()['status'] == 'not_executed'
    revised = api.post('/api/v1/proposals/analyze', json={'request_id': row['request_id'],
        'message': row['source_question']}).json()
    assert detail(api, revised)['status'] == 'pending'


def test_archive_lock_prevents_concurrent_analysis_and_confirmation(api, stage3):
    from threading import Event
    from concurrent.futures import TimeoutError
    row = create(api)
    token = detail(api, row)['management_token']
    entered, release = Event(), Event()
    def pause(point):
        entered.set()
        assert release.wait(5)
    service = RequestManagementService(stage3['proposals'], stage3['aftersales'], pause)
    with ThreadPoolExecutor(3) as pool:
        archive = pool.submit(service.manage, [(UUID(row['request_id']), token)], 'server_operator', archive=True)
        assert entered.wait(5)
        execution = pool.submit(stage3['aftersales'].confirm_and_create, UUID(row['proposal_id']), 1, row['payload_hash'], 'server_operator')
        analysis = pool.submit(stage3['proposals']._start, '补充问题', UUID(row['request_id']), 'zh', 'server_operator')
        try:
            for future in (execution, analysis):
                with pytest.raises(TimeoutError):
                    future.result(.15)
        finally:
            release.set()
        archive.result()
        for future in (execution, analysis):
            with pytest.raises(AppError) as error:
                future.result()
            assert error.value.code == 'REQUEST_ARCHIVED'


def test_ticket_linked_to_historical_version_blocks_purge(api, stage3):
    row = create(api)
    revised = api.post('/api/v1/proposals/' + row['proposal_id'] + '/revisions', json={**guard(row), 'draft_reply': '新版本'}).json()
    with stage3['sessions'].begin() as session:
        # Move the isolated seed fixture to a historical revision, including a closed ticket.
        ticket = session.scalar(select(AftersalesTicket))
        ticket.proposal_id, ticket.status = UUID(row['proposal_id']), 'closed'
    assert management(api, revised, 'archive').status_code == 200
    assert api.get(f'{ROOT}/{row["request_id"]}/purge-preview').json()['error']['code'] == 'REQUEST_HAS_TICKETS'

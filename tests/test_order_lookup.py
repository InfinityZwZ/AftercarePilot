from datetime import timedelta

import pytest
from sqlalchemy import func, select

from src.storage.models import AuditLog, AftersalesTicket, Order, Proposal, ReturnPolicy
from tests.test_stage3_api import api
from tests.test_streamlit_workflow import button, start_ui


def table_counts(stage3):
    with stage3['sessions']() as session:
        return [session.scalar(select(func.count()).select_from(model)) for model in
                (Order, ReturnPolicy, Proposal, AftersalesTicket, AuditLog)]


def test_orders_lookup_search_pagination_privacy_and_zero_writes(api, stage3):
    before = table_counts(stage3)
    first = api.get('/api/v1/orders', params={'limit': 2})
    assert first.status_code == 200
    assert first.json()['total'] == 17 and len(first.json()['items']) == 2
    second = api.get('/api/v1/orders', params={'limit': 2, 'offset': 2}).json()
    assert set(item['order_id'] for item in first.json()['items']).isdisjoint(item['order_id'] for item in second['items'])
    for query in ('蓝牙音箱', 'Bluetooth Speaker', 'ord-1009'):
        data = api.get('/api/v1/orders', params={'q': query}).json()
        assert data['total'] == 1 and data['items'][0]['order_id'] == 'ORD-1009'
        assert data['items'][0]['product_names'] == ['蓝牙音箱']
        assert data['items'][0]['return_window'] == 'within_window'
        assert 'customer_ref' not in data['items'][0] and 'shipment' not in data['items'][0]
    assert api.get('/api/v1/orders', params={'q': '%'}).json()['total'] == 0
    assert api.get('/api/v1/orders', params={'q': '没有这种商品'}).json()['total'] == 0
    assert api.get('/api/v1/orders', params={'limit': 51}).status_code == 422
    assert api.get('/api/v1/orders', headers={'X-Operator-Token': ''}).status_code == 401
    assert table_counts(stage3) == before


@pytest.mark.parametrize('order_id,window', [('ORD-1002','not_delivered'), ('ORD-1004','cancelled'),
    ('ORD-DEMO-UNKNOWN','unknown'), ('ORD-DEMO-EXPIRED','expired'), ('ORD-DEMO-DUPLICATE','existing_ticket')])
def test_orders_lookup_window_is_hint_not_approval(api, order_id, window):
    response = api.get('/api/v1/orders', params={'q': order_id}).json()
    assert response['items'][0]['return_window'] == window


def test_recent_seed_insert_only_and_idempotent(stage3):
    from scripts.seed_demo import seed_recent_orders
    now = stage3['clock'].now()
    before = table_counts(stage3)
    with stage3['sessions'].begin() as session:
        original = session.get(Order, 'ORD-1009')
        original.total_cents = 12345
        ids = seed_recent_orders(session, now=now)
    assert len(ids) == 3
    with stage3['sessions'].begin() as session:
        assert seed_recent_orders(session, now=now + timedelta(seconds=1)) == []
        assert session.get(Order, 'ORD-1009').total_cents == 12345
        for identity in ids:
            row = session.get(Order, identity)
            assert row.status == 'delivered' and now - row.delivered_at < timedelta(days=1)
    after = table_counts(stage3)
    assert after == [before[0]+3, *before[1:]]
    for identity in ids:
        result = stage3['gateway'].check_return_eligibility(identity, 'defective')
        assert result.data['eligibility'] == 'eligible'


def test_order_selection_preserves_text_without_analysis_or_database_write(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    before = table_counts(stage3)
    app.text_area(key='question').set_value('这个收到货后是坏的，用不了').run()
    app.text_input(key='order_search').set_value('蓝牙音箱').run()
    app.selectbox(key='order_picker').select('ORD-1009').run()
    button(app, '选入请求').click().run()
    assert not app.exception
    assert '这个收到货后是坏的，用不了' in app.text_area(key='question').value
    assert 'ORD-1009' in app.text_area(key='question').value
    assert not any(method == 'POST' for method, _ in transport.calls)
    assert table_counts(stage3) == before


def test_lookup_can_fill_missing_order_in_existing_request(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    app.text_area(key='question').set_value('这个东西坏了，我想退货')
    button(app, '开始分析').click().run()
    first = app.session_state['selected_proposal']
    before = table_counts(stage3)
    posts = sum(method == 'POST' for method, _ in transport.calls)
    app.text_input(key='order_search').set_value('ORD-1009').run()
    app.selectbox(key='order_picker').select('ORD-1009').run()
    button(app, '选入请求').click().run()
    assert app.session_state['selected_proposal'] == first
    assert sum(method == 'POST' for method, _ in transport.calls) == posts
    assert table_counts(stage3) == before
    button(app, '补充信息并分析').click().run()
    assert not app.exception and app.session_state['selected_proposal'] != first


def test_lookup_does_not_silently_replace_another_order_number(api, stage3, monkeypatch):
    app, _ = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    app.text_area(key='question').set_value('ORD-1002 的键盘没有收到').run()
    app.text_input(key='order_search').set_value('ORD-1009').run()
    app.selectbox(key='order_picker').select('ORD-1009').run()
    button(app, '选入请求').click().run()
    assert app.text_area(key='question').value == 'ORD-1002 的键盘没有收到'
    assert any('其他订单号' in item.value for item in app.warning)


def test_lookup_keeps_question_at_character_limit(api, stage3, monkeypatch):
    app, _ = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    original = '坏' * 4000
    app.text_area(key='question').set_value(original).run()
    app.text_input(key='order_search').set_value('ORD-1009').run()
    app.selectbox(key='order_picker').select('ORD-1009').run()
    button(app, '选入请求').click().run()
    assert not app.exception
    assert app.text_area(key='question').value == original
    assert any('字数' in item.value for item in app.warning)


def test_recent_order_uses_normal_analysis_and_confirmation(api, stage3):
    from uuid import UUID
    import src.main as main
    from scripts.seed_demo import seed_recent_orders
    from src.agent.nodes import AnalysisAgent
    from src.api.dependencies import get_analyzer
    from tests.fakes import FakeLLM
    from tests.test_stage3_api import guard
    with stage3['sessions'].begin() as session:
        identity = seed_recent_orders(session, now=stage3['clock'].now())[0]
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(order_id=identity), stage3['gateway'])
    result = api.post('/api/v1/proposals/analyze', json={'message': identity + ' 音箱坏了，想退货'}).json()
    assert result['status'] == 'pending_approval'
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(AftersalesTicket).where(AftersalesTicket.order_id == identity)) == 0
    ticket = api.post('/api/v1/proposals/' + result['proposal_id'] + '/confirm-and-create', json=guard(result)).json()['ticket']
    assert ticket['order_id'] == identity and ticket['status'] == 'requested'
    with stage3['sessions']() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.proposal_id == UUID(result['proposal_id']), AuditLog.event_type == 'ticket_created')) == 1

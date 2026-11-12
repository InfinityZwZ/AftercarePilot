from tests.test_stage3_api import api
from tests.test_streamlit_workflow import start_ui, analyze_ui, button


def test_archive_restore_and_single_purge_ui(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    assert not any('/audit-logs' in url or url.endswith('/history') for _, url in transport.calls)
    button(app, '归档请求').click().run()
    assert not app.exception
    assert not any(item.label == '确认并创建内部售后工单' for item in app.button)
    assert app.text_area(key='draft_' + app.session_state['selected_proposal']).disabled
    button(app, '恢复请求').click().run()
    assert not app.exception and not button(app, '确认并创建内部售后工单').disabled
    button(app, '归档请求').click().run()
    button(app, '查看永久清除说明').click().run()
    assert not app.exception
    assert button(app, '永久清除此请求').disabled
    app.text_input(key='purge_confirmation').set_value('永久清除').run()
    button(app, '永久清除此请求').click().run()
    assert not app.exception and app.session_state['selected_proposal'] is None
    assert any('已永久清除' in item.value for item in app.success)


def test_bulk_page_selection_cleared_on_filters_and_no_eager_history(api, stage3, monkeypatch):
    from tests.test_stage3_api import create
    create(api)
    app, transport = start_ui(api, monkeypatch)
    app.checkbox(key='batch_manage').check().run()
    button(app, '选择当前页').click().run()
    assert any(item.value for item in app.checkbox if item.key.startswith('batch_request_'))
    app.selectbox(key='archive_filter').select('已归档').run()
    assert not app.exception
    assert not any(item.value for item in app.checkbox if item.key.startswith('batch_request_'))
    assert not any('/audit-logs' in url or url.endswith('/history') for _, url in transport.calls)


def test_stale_render_management_token_is_not_refreshed_by_click_rerun(api, stage3, monkeypatch):
    from tests.test_stage3_api import guard
    from uuid import UUID
    app, _ = start_ui(api, monkeypatch)
    analyze_ui(app)
    row = stage3['proposals'].get(UUID(app.session_state['selected_proposal']))
    api.post('/api/v1/proposals/' + row['proposal_id'] + '/approve', json=guard(row))
    button(app, '归档请求').click().run()
    assert not app.exception
    assert any('发生变化' in item.value for item in app.warning)
    assert stage3['proposals'].get(UUID(row['proposal_id']))['archived_at'] is None


def test_ui_bulk_archive_restore_and_lazy_version_content(api, stage3, monkeypatch):
    from tests.test_stage3_api import create
    rows = [create(api), create(api)]
    app, transport = start_ui(api, monkeypatch)
    app.text_input(key='request_search').set_value('ORD-1009').run()
    app.checkbox(key='batch_manage').check().run()
    button(app, '选择当前页').click().run()
    button(app, '归档所选').click().run()
    assert not app.exception
    assert api.get('/api/v1/requests', params={'archived': 'archived', 'q': 'ORD-1009'}).json()['total'] == 2
    app.selectbox(key='archive_filter').select('已归档').run()
    button(app, '选择当前页').click().run()
    button(app, '恢复所选').click().run()
    assert not app.exception
    app.selectbox(key='archive_filter').select('处理中').run()
    button(app, '查看请求').click().run()
    before = len(transport.calls)
    button(app, '加载历史版本').click().run()
    assert not app.exception
    assert any(url.endswith('/history') for _, url in transport.calls[before:])
    assert not any('/audit-logs' in url for _, url in transport.calls)
    button(app, '查看此版本').click().run()
    assert not app.exception

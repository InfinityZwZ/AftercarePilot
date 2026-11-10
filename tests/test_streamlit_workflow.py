from pathlib import Path
from uuid import UUID

import requests
import pytest
from streamlit.testing.v1 import AppTest

from src.ui.api_client import APIClient
from tests.test_stage3_api import api  # pytest fixture
from tests.test_proposal_approval import counts

UI = Path(__file__).resolve().parents[1] / "src" / "ui" / "streamlit_app.py"


class RecordedTransport:
    def __init__(self, api):
        self.api = api
        self.calls = []
        self.lose_execute_response = False
        self.block_execution_reads = False
        self.drop_execute_before_send = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        kwargs.pop("timeout", None)
        if method == "POST" and url.endswith("/confirm-and-create") and self.drop_execute_before_send:
            self.drop_execute_before_send = False
            raise requests.Timeout("test loses submission before reaching the API")
        response = self.api.request(method, url, **kwargs)
        if method == "POST" and url.endswith("/confirm-and-create") and self.lose_execute_response:
            self.lose_execute_response = False
            raise requests.Timeout("test drops the committed HTTP response")
        if method == "GET" and url.endswith("/execution") and self.block_execution_reads:
            raise requests.Timeout("test blocks independent execution reads")
        return response


def button(app, label):
    return next(item for item in app.button if item.label == label)


def start_ui(api, monkeypatch):
    import src.ui.api_client as client_module
    transport = RecordedTransport(api)
    client = APIClient("http://testserver/api/v1", transport)
    monkeypatch.setattr(client_module, "get_api_client", lambda: client)
    return AppTest.from_file(str(UI), default_timeout=20).run(), transport


def analyze_ui(app):
    button(app, "新建请求").click().run()
    app.text_area(key="question").set_value("ORD-1009 音箱坏了，我想退货")
    button(app, "开始分析").click().run()
    assert not app.exception


def test_ui_forms_approve_edit_execute_and_rerun_no_post(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    assert not app.exception
    assert app.radio(key="workspace").options == ["售后请求", "售后工单"]
    assert not any(method == "POST" for method, _ in transport.calls)
    analyze_ui(app)
    assert not any(item.label == "查询提交结果" for item in app.button)
    assert counts(stage3) == (0, 0)
    assert not app.exception and counts(stage3) == (0, 0)
    selected = app.session_state["selected_proposal"]
    app.text_area(key="draft_" + selected).set_value("保存新草稿，重新审批。").run()
    button(app, "保存修改").click().run()
    assert not app.exception and app.session_state["selected_proposal"] != selected
    assert not button(app, "确认并创建工单").disabled
    button(app, "确认并创建工单").click().run()
    assert not app.exception and counts(stage3) == (1, 1)
    posts_before = sum(method == "POST" for method, _ in transport.calls)
    app.run()
    app.run()
    assert not app.exception
    assert sum(method == "POST" for method, _ in transport.calls) == posts_before
    assert counts(stage3) == (1, 1)
    assert any(method == "GET" and "/aftersales-tickets/" in url for method, url in transport.calls)
    assert any("已创建工单：" in item.value for item in app.success)
    assert not any(item.label == "查询提交结果" for item in app.button)
    assert not app.code
    assert not any('/audit-logs' in url for method, url in transport.calls)
    button(app, '加载操作记录').click().run()
    records = next(item for item in app.expander if item.label == "操作记录")
    assert not records.proto.expanded
    audit = next(item.value for item in records.table)
    assert list(audit.columns) == ["时间", "操作", "操作者"]
    assert "创建售后工单" in audit["操作"].values


def test_ui_lost_response_stays_unknown_until_independent_verification(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    transport.lose_execute_response = True
    button(app, "确认并创建工单").click().run()
    assert not app.exception and counts(stage3) == (1, 1)
    selected = app.session_state["selected_proposal"]
    assert selected in app.session_state["unknown_execution"]
    assert not app.success
    assert button(app, "确认并创建工单").disabled
    button(app, "查询提交结果").click().run()
    assert not app.exception
    assert selected not in app.session_state["unknown_execution"]
    assert app.success and counts(stage3) == (1, 1)
    assert not any(item.label == "查询提交结果" for item in app.button)


def test_ui_verification_read_failure_blocks_resubmit_until_query(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    transport.block_execution_reads = True
    button(app, "确认并创建工单").click().run()
    assert not app.exception and counts(stage3) == (1, 1)
    assert not app.success
    assert button(app, "确认并创建工单").disabled
    assert not button(app, "查询提交结果").disabled
    posts_before = sum(method == "POST" for method, _ in transport.calls)
    app.run()
    assert sum(method == "POST" for method, _ in transport.calls) == posts_before
    transport.block_execution_reads = False
    button(app, "查询提交结果").click().run()
    assert not app.exception and counts(stage3) == (1, 1)
    assert any("已创建工单：" in item.value for item in app.success)
    assert not any(item.label == "查询提交结果" for item in app.button)


def test_ui_reloaded_executed_record_is_verified_automatically_with_no_post(api, stage3, monkeypatch):
    app, _ = start_ui(api, monkeypatch)
    analyze_ui(app)
    button(app, "确认并创建工单").click().run()
    selected = app.session_state["selected_proposal"]
    fresh, transport = start_ui(api, monkeypatch)
    fresh.session_state["selected_proposal"] = selected
    fresh.run()
    assert not fresh.exception
    assert any("已创建工单：" in item.value for item in fresh.success)
    assert not any(item.label == "查询提交结果" for item in fresh.button)
    assert any(method == "GET" and url.endswith("/execution") for method, url in transport.calls)
    assert not any(method == "POST" for method, _ in transport.calls)
    assert counts(stage3) == (1, 1)


def test_ui_query_confirms_no_ticket_then_requires_explicit_resubmit(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    transport.drop_execute_before_send = True
    button(app, "确认并创建工单").click().run()
    assert not app.exception and counts(stage3) == (0, 0)
    assert button(app, "确认并创建工单").disabled
    posts_before = sum(method == "POST" for method, _ in transport.calls)
    button(app, "查询提交结果").click().run()
    assert not app.exception and counts(stage3) == (0, 0)
    assert sum(method == "POST" for method, _ in transport.calls) == posts_before
    assert not any(item.label == "查询提交结果" for item in app.button)
    assert not button(app, "确认并创建工单").disabled
    button(app, "确认并创建工单").click().run()
    assert not app.exception and counts(stage3) == (1, 1)


def test_ui_waits_for_api_readiness_without_posting(api, stage3, monkeypatch):
    import src.ui.api_client as client_module
    from src.ui.api_client import APIError
    client = APIClient("http://testserver/api/v1", RecordedTransport(api))
    original_get = client.get
    def unavailable(path, **kwargs):
        if path == "/ready":
            raise APIError("API_UNAVAILABLE", "API unavailable")
        return original_get(path, **kwargs)
    monkeypatch.setattr(client, "get", unavailable)
    monkeypatch.setattr(client_module, "get_api_client", lambda: client)
    app = AppTest.from_file(str(UI), default_timeout=20).run()
    assert not app.exception and button(app, "重新连接")
    assert not app.text_area and not any(method == "POST" for method, _ in client.transport.calls)
    monkeypatch.setattr(client, "get", original_get)
    button(app, "重新连接").click().run()
    assert not app.exception and app.radio(key="workspace").options == ["售后请求", "售后工单"]
    assert counts(stage3) == (0, 0)


def test_workstation_uses_chinese_labels_and_preserves_business_values(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    assert not app.exception
    assert app.title[0].value == "售后工作台"
    assert app.selectbox(key="status_filter").label == "处理状态"
    assert not any(item.label == "回复语言" for item in app.selectbox)
    assert not any(method == "POST" for method, _ in transport.calls)
    analyze_ui(app)
    assert [item.value for item in app.metric] == ["ORD-1009", "符合申请条件", "89.99 美元"]
    assert any("待确认" in item.value for item in app.markdown)
    assert not app.json
    evidence_tables = [table.value for table in app.table if list(table.value.columns) == ["项目", "内容"]]
    assert len(evidence_tables) == 3
    eligibility_table = next(table for table in evidence_tables if "申请资格" in table["项目"].values)
    assert "符合申请条件" in eligibility_table["内容"].values
    selected = app.session_state["selected_proposal"]
    assert stage3["proposals"].get(UUID(selected))["payload"]["eligibility"] == "eligible"
    assert counts(stage3) == (0, 0)


def test_chinese_error_messages_do_not_echo_backend_english_or_retry_unknown():
    from src.ui.presentation import error_message, local_time
    assert "版本已变更" in error_message("STALE_REVISION")
    assert "先查询" in error_message("unrecognized_failure", uncertain=True)
    assert "悉尼时间" in local_time("2026-10-09T12:00:00Z", "Australia/Sydney")


def test_chinese_draft_conversion_requires_explicit_revision_before_approval(api, stage3, monkeypatch):
    from src.ui import presentation
    translate = presentation.business_text
    monkeypatch.setattr(presentation, "business_text", lambda value: value)
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    first = app.session_state["selected_proposal"]
    app.text_area(key="draft_" + first).set_value("商品故障（defective），状态 requested。").run()
    button(app, "保存修改").click().run()
    mixed = app.session_state["selected_proposal"]
    assert app.text_area(key="draft_" + mixed).value == "商品故障（defective），状态 requested。"
    monkeypatch.setattr(presentation, "business_text", translate)
    before = len(transport.calls)
    app.run()
    assert not any(method == "POST" for method, _ in transport.calls[before:])
    assert app.text_area(key="draft_" + mixed).value == "商品故障（商品故障），状态 已受理。"
    assert button(app, "确认并创建工单").disabled
    assert button(app, "确认并创建工单").disabled
    before = len(transport.calls)
    app.run()
    assert not any(method == "POST" for method, _ in transport.calls[before:])
    assert stage3["proposals"].get(UUID(mixed))["draft_reply"] == "商品故障（defective），状态 requested。"
    button(app, "保存修改").click().run()
    chinese = app.session_state["selected_proposal"]
    stored = stage3["proposals"].get(UUID(chinese))
    assert stored["revision"] == 3 and stored["draft_reply"] == app.text_area(key="draft_" + chinese).value
    assert not button(app, "确认并创建工单").disabled
    button(app, "确认并创建工单").click().run()
    approved = stage3["proposals"].get(UUID(chinese))
    assert approved["approved_payload_hash"] == approved["payload_hash"]
    assert counts(stage3) == (1, 1)


def configure_analysis(stage3, **kwargs):
    import src.main as main
    from src.agent.nodes import AnalysisAgent
    from src.api.dependencies import get_analyzer
    from tests.fakes import FakeLLM
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(**kwargs), stage3['gateway'])


def test_unsaved_draft_blocks_confirmation_without_post(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    identity = app.session_state['selected_proposal']
    posts = sum(method == 'POST' for method, _ in transport.calls)
    app.text_area(key='draft_' + identity).set_value('尚未保存的修改').run()
    assert button(app, '确认并创建工单').disabled
    assert not button(app, '保存修改').disabled
    assert sum(method == 'POST' for method, _ in transport.calls) == posts
    assert counts(stage3) == (0, 0)


@pytest.mark.parametrize('order_id', ['ORD-1009', 'ORD-DEMO-EXPIRED'])
def test_reply_confirmation_never_creates_ticket(api, stage3, monkeypatch, order_id):
    configure_analysis(stage3, order_id=order_id, kind='reply_only')
    app, _ = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    app.text_area(key='question').set_value(order_id + ' 坏了，退货')
    button(app, '开始分析').click().run()
    assert not app.exception
    stored = api.get('/api/v1/proposals/' + app.session_state['selected_proposal']).json()
    assert stored['kind'] == 'reply_only', (stored['status'], stored['kind'], stored['error'])
    assert not any(item.label == '确认并创建工单' for item in app.button)
    button(app, '确认草稿').click().run()
    assert not app.exception and any('尚未发送' in item.value for item in app.success)
    assert counts(stage3) == (0, 0)


def test_existing_ticket_does_not_block_consultation_reply(api, stage3, monkeypatch):
    from tests.test_stage3_api import create, guard
    first = create(api)
    api.post('/api/v1/proposals/' + first['proposal_id'] + '/confirm-and-create', json=guard(first))
    configure_analysis(stage3, intent='order_status', kind='reply_only')
    app, _ = start_ui(api, monkeypatch)
    analyze_ui(app)
    button(app, '确认草稿').click().run()
    assert not app.exception
    assert counts(stage3) == (1, 1)
    assert any('草稿已确认' in item.value for item in app.success)
    button(app, '查看工单').click().run()
    assert app.radio(key='workspace').value == '售后工单'
    assert any('工单号' in item.value for item in app.success)


def test_request_list_opens_current_version_and_history_is_readonly(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    analyze_ui(app)
    identity = app.session_state['selected_proposal']
    app.text_area(key='draft_' + identity).set_value('保存后的回复').run()
    button(app, '保存修改').click().run()
    latest = app.session_state['selected_proposal']
    button(app, '返回请求列表').click().run()
    app.text_input(key='request_search').set_value('ORD-1009').run()
    assert len([item for item in app.button if item.label == '查看请求']) == 1
    posts = sum(method == 'POST' for method, _ in transport.calls)
    button(app, '查看请求').click().run()
    assert app.session_state['selected_proposal'] == latest
    assert next(item for item in app.expander if item.label == '历史版本')
    assert sum(method == 'POST' for method, _ in transport.calls) == posts


@pytest.mark.parametrize('kind', ['clarification', 'handoff'])
def test_missing_information_and_manual_processing_no_create(api, stage3, monkeypatch, kind):
    configure_analysis(stage3, reason=None if kind == 'clarification' else 'defective', kind=kind)
    app, _ = start_ui(api, monkeypatch)
    analyze_ui(app)
    assert not any(item.label in ('确认草稿', '确认并创建工单') for item in app.button)
    assert any(('待补充' if kind == 'clarification' else '需人工处理') in item.value for item in app.markdown)
    assert counts(stage3) == (0, 0)


def test_other_proposal_ticket_hides_creation_and_can_be_viewed(api, stage3, monkeypatch):
    from tests.test_stage3_api import create, guard
    first, second = create(api), create(api)
    api.post('/api/v1/proposals/' + first['proposal_id'] + '/confirm-and-create', json=guard(first))
    app, transport = start_ui(api, monkeypatch)
    app.session_state['selected_proposal'] = second['proposal_id']
    app.run()
    assert not app.exception
    assert not any(item.label == '确认并创建工单' for item in app.button)
    button(app, '查看工单').click().run()
    assert app.radio(key='workspace').value == '售后工单'
    assert counts(stage3) == (1, 1)
    assert not any(method == 'POST' for method, _ in transport.calls)

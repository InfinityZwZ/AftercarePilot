import asyncio
from uuid import UUID

import pytest

from src.agent.nodes import AnalysisAgent
from tests.test_agent import run
from tests.test_proposal_approval import counts
from tests.test_stage3_api import api
from tests.test_streamlit_workflow import button, start_ui


class TimedOutModel:
    calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise TimeoutError('simulated provider timeout')


class UnavailableReads:
    async def __aenter__(self):
        raise AssertionError('Missing information must not start read services')

    async def __aexit__(self, *args):
        pass


def test_general_return_reason_policy_is_not_personal_missing_order(stage3):
    from tests.fakes import FakeLLM
    question = '商品损坏是否属于允许的退货原因？'
    result = run(AnalysisAgent(FakeLLM(order_id=None, reason=None, intent='return_policy', kind='reply_only'),
                              stage3['gateway']), question)
    assert result['payload']['kind'] == 'reply_only'
    assert result['entities']['intent'] == 'return_policy'


def test_personal_damage_policy_question_still_needs_order_without_model():
    result = run(AnalysisAgent(TimedOutModel(), UnavailableReads()), '我的商品损坏是否属于允许的退货原因？')
    assert result['payload']['kind'] == 'clarification'
    assert result['payload']['missing_fields'] == ['order_id']


@pytest.mark.parametrize('question', ['这个收到货后是坏的，用不了，我想退货', '我的商品有问题', '我想退货', '我的快递还没收到', 'My item is broken', '我的退款还没到账'])
def test_missing_order_needs_info_without_model_or_reads(question):
    model = TimedOutModel()
    result = run(AnalysisAgent(model, UnavailableReads()), question)
    assert result['status'] == 'needs_info' and result['error'] is None
    assert result['payload']['missing_fields'] == ['order_id']
    assert '订单号' in result['payload']['draft_reply']
    assert result['entities']['order_id'] is None
    assert result['llm_calls'] == result['tool_calls'] == model.calls == 0


@pytest.mark.parametrize('message', ['你们的退货政策是什么？', '退货需要什么条件？', '退货流程是什么？', '退款一般多久到账？', '请帮我核实一件事', 'ORD-1009 音箱坏了，想退货'])
def test_real_model_failure_is_not_disguised_as_missing_order(stage3, message):
    model = TimedOutModel()
    result = run(AnalysisAgent(model, stage3['gateway']), message)
    assert result['status'] == 'failed' and result['error']['code'] == 'MODEL_TIMEOUT'
    assert model.calls == 1


def test_api_and_ui_missing_order_then_continue_same_request(api, stage3, monkeypatch):
    import src.main as main
    from src.api.dependencies import get_analyzer
    from tests.fakes import FakeLLM
    model = TimedOutModel()
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(model, UnavailableReads())
    app, _ = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    app.text_area(key='question').set_value('这个收到货后是坏的，用不了，我想退货')
    button(app, '开始分析').click().run()
    assert not app.exception and not app.error
    assert any('待补充' in item.value for item in app.markdown)
    assert any('缺少订单号' in item.value for item in app.markdown)
    assert next(item for item in app.expander if item.label == '查找订单').proto.expanded
    first = stage3['proposals'].get(UUID(app.session_state['selected_proposal']))
    assert first['status'] == 'needs_info' and first['usage']['llm_calls'] == 0
    assert '请提供订单号' in first['draft_reply']
    assert '没有创建工单' not in first['draft_reply']
    assert counts(stage3) == (0, 0) and model.calls == 0
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(FakeLLM(), stage3['gateway'])
    app.text_area(key='question').set_value('ORD-1009')
    button(app, '补充信息并分析').click().run()
    assert not app.exception
    current = stage3['proposals'].get(UUID(app.session_state['selected_proposal']))
    assert current['request_id'] == first['request_id'] and current['revision'] == 2
    assert current['kind'] == 'return_request' and current['status'] == 'pending_approval'
    assert counts(stage3) == (0, 0)


def test_v11_multiple_orders_require_one_explicit_selection(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    button(app, '新建请求').click().run()
    original = 'ORD-1009 和 ORD-1002 的商品坏了，我想退货'
    app.text_area(key='question').set_value(original)
    button(app, '开始分析').click().run()
    first = stage3['proposals'].get(UUID(app.session_state['selected_proposal']))
    assert first['payload']['missing_fields'] == ['single_order_id']
    assert any('请选择一笔具体订单' in item.value for item in app.markdown)
    assert app.selectbox(key='order_picker').value is None
    before = sum(method == 'POST' for method, _ in transport.calls)
    app.text_area(key='question').set_value('ORD-1009 和 ORD-1002')
    button(app, '补充信息并分析').click().run()
    assert sum(method == 'POST' for method, _ in transport.calls) == before
    app.text_area(key='question').set_value('ORD-1009 音箱坏了，我想退货')
    button(app, '补充信息并分析').click().run()
    current = stage3['proposals'].get(UUID(app.session_state['selected_proposal']))
    assert current['request_id'] == first['request_id'] and current['revision'] == 2
    assert current['kind'] == 'return_request' and current['payload']['order_id'] == 'ORD-1009'
    assert stage3['proposals'].get(UUID(first['proposal_id']))['source_question'] == original
    assert counts(stage3) == (0, 0)


def test_missing_order_cannot_submit_before_refill(api, stage3, monkeypatch):
    app, transport = start_ui(api, monkeypatch)
    button(app, "新建请求").click().run()
    app.text_area(key="question").set_value("音箱坏了，我想退货")
    button(app, "开始分析").click().run()
    proposal = app.session_state["selected_proposal"]
    before = list(transport.calls)
    button(app, "补充信息并分析").click().run()
    assert app.session_state["selected_proposal"] == proposal
    assert not any(method == "POST" for method, _ in transport.calls[len(before):])
    assert counts(stage3) == (0, 0)


def test_genuine_failure_and_historical_failure_summary_are_chinese(api, stage3):
    import src.main as main
    from src.api.dependencies import get_analyzer
    from src.ui.presentation import business_text
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(TimedOutModel(), stage3['gateway'])
    response = api.post('/api/v1/proposals/analyze', json={'message': 'ORD-1009 音箱坏了'})
    assert response.status_code == 502
    row = stage3['proposals'].get(UUID(response.json()['error']['details']['proposal_id']))
    assert row['payload']['summary'] == '分析未完成'
    assert business_text('Analysis failed') == '分析未完成'


def test_historical_failure_can_reanalyze_original_question_without_retyping(api, stage3, monkeypatch):
    import src.main as main
    from src.api.dependencies import get_analyzer
    message = '这个收到货后是坏的，用不了，我想退货'
    identity = stage3['proposals']._start(message, None, 'zh', 'server_operator')
    historical = stage3['proposals']._finish(identity, {'error': {'code': 'MODEL_TIMEOUT', 'message': 'timeout'}}, 'server_operator')
    main.app.dependency_overrides[get_analyzer] = lambda: AnalysisAgent(TimedOutModel(), UnavailableReads())
    app, _ = start_ui(api, monkeypatch)
    app.session_state['selected_proposal'] = historical['proposal_id']
    app.run()
    assert app.text_area(key='question').value == message
    button(app, '重新分析').click().run()
    assert not app.exception and not app.error
    current = stage3['proposals'].get(UUID(app.session_state['selected_proposal']))
    assert current['request_id'] == historical['request_id'] and current['revision'] == 2
    assert current['status'] == 'needs_info'
    assert counts(stage3) == (0, 0)

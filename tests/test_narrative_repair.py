import json
import pytest
from langchain_core.messages import AIMessage
from src.agent.nodes import AnalysisAgent
from tests.fakes import FakeLLM
from tests.test_agent import run


def draft(text):
    return AIMessage(content=json.dumps(dict(kind='handoff',summary='需要人工核实',draft_reply=text,evidence_refs=[])))


def test_unsafe_narrative_can_be_corrected_once_without_tools_or_writes(stage3):
    fake=FakeLLM(intent='general',reason=None)
    fake.script=[fake.triage(),draft('我们已将您的情况转交人工客服。'),draft('需要由操作员人工核实，尚未转交其他系统。')]
    result=run(AnalysisAgent(fake,stage3['gateway']),'有个售后问题想进一步说明')
    assert result['status']=='pending_approval' and result['payload']['kind']=='handoff'
    assert result['schema_repairs']==1 and result['llm_calls']==3 and result['tool_calls']==0
    assert result['payload']['draft_reply']=='需要由操作员人工核实，尚未转交其他系统。'


def test_narrative_repair_does_not_retry_after_schema_repair_already_used(stage3):
    fake=FakeLLM(intent='general',reason=None)
    fake.script=[AIMessage(content='invalid'),fake.triage(),draft('我们已将您的情况转交人工客服。')]
    result=run(AnalysisAgent(fake,stage3['gateway']),'有个售后问题想进一步说明')
    assert result['error']['code']=='UNSUPPORTED_COMPLETION_CLAIM'
    assert result['llm_calls']==3 and result['schema_repairs']==1


@pytest.mark.parametrize('claim', ['退货申请已受理，请留意处理进度。', '售后工单已创建。', '已成功创建退货工单。'])
def test_ticket_completion_claim_requires_repair_before_operator_confirmation(stage3, claim):
    fake=FakeLLM(intent='general',reason=None)
    safe='需要由操作员确认后才能创建工单，目前尚未创建工单。'
    fake.script=[fake.triage(),draft(claim),draft(safe)]
    result=run(AnalysisAgent(fake,stage3['gateway']),'有个售后问题想进一步说明')
    assert result['payload']['draft_reply']==safe
    assert result['schema_repairs']==1 and result['llm_calls']==3


def test_negative_ticket_completion_does_not_trigger_repair(stage3):
    fake=FakeLLM(intent='general',reason=None)
    safe='退货申请尚未受理，工单尚未创建，需要操作员确认。'
    fake.script=[fake.triage(),draft(safe)]
    result=run(AnalysisAgent(fake,stage3['gateway']),'有个售后问题想进一步说明')
    assert result['payload']['draft_reply']==safe
    assert result['schema_repairs']==0 and result['llm_calls']==2

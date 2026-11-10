import json
import pytest
from langchain_core.messages import AIMessage
from src.agent.nodes import AnalysisAgent
from tests.fakes import FakeLLM
from tests.test_agent import run


@pytest.mark.parametrize('question,intent',[
    ('我的包裹没有收到，帮我查一下','shipping_tracking'),
    ('ORD-1003 和 ORD-1011 哪个包裹在路上？','shipping_tracking'),
    ('ORD-1003 和 ORD-1011 我想退一单','return_request')])
def test_missing_information_keeps_business_intent(question,intent):
    result=run(AnalysisAgent(FakeLLM()),question)
    assert result['entities']['intent']==intent
    assert result['status']=='needs_info' and result['llm_calls']==0


@pytest.mark.parametrize('question',[
    'ORD-1011 我需要换货，不是退货，请安排补发',
    '帮我修改 ORD-1011 的收货地址',
    '帮我写一篇毕业论文'])
def test_unsupported_actions_are_manual_without_return_clarification(question):
    result=run(AnalysisAgent(FakeLLM()),question)
    assert result['payload']['kind']=='handoff'
    assert result['entities']['intent']=='unsupported' and result['tool_calls']==0


def test_vague_damage_does_not_invent_return_intent():
    result=run(AnalysisAgent(FakeLLM()),'刚收到的东西有故障，用不了。')
    assert result['payload']['kind']=='handoff' and result['entities']['intent']=='general'


def test_triage_contract_explicitly_requires_array_not_null():
    from src.agent.nodes import TRIAGE_PROMPT
    assert 'never null' in TRIAGE_PROMPT and '[]' in TRIAGE_PROMPT


def test_sufficient_evidence_removes_repeat_tools(stage3):
    class Model(FakeLLM):
        async def ainvoke(self,messages):
            if len(self.history)==2:
                assert self.bound_names==[]
            return await super().ainvoke(messages)
    fake=Model()
    result=run(AnalysisAgent(fake,stage3['gateway']),'ORD-1009 音箱坏了，我要退货')
    assert result['status']=='pending_approval'


def test_unsupported_handoff_does_not_claim_external_transfer(stage3):
    fake=FakeLLM(intent='general',reason=None,kind='handoff')
    bad=AIMessage(content=json.dumps(dict(kind='handoff',summary='需要人工',
        draft_reply='我们已将您的情况转交人工客服。',evidence_refs=[])))
    fake.script=[fake.triage(),bad,bad]
    result=run(AnalysisAgent(fake,stage3['gateway']),'有个问题想请客服帮我看看')
    assert result['error']['code']=='UNSUPPORTED_COMPLETION_CLAIM'

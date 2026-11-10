from langchain_core.messages import AIMessage
from src.agent.nodes import AnalysisAgent
from tests.fakes import FakeLLM
from tests.test_agent import run


def incomplete(fake):
    reads=fake.reads()
    return AIMessage(content='',tool_calls=[c for c in reads.tool_calls if c['name']!='get_return_policy'])


def test_return_flow_completes_missing_real_policy_read(stage3):
    fake=FakeLLM()
    fake.script=[fake.triage(),incomplete(fake),fake.draft,fake.draft]
    result=run(AnalysisAgent(fake,stage3['gateway']))
    assert result['status']=='pending_approval' and result['payload']['kind']=='return_request'
    assert result['evidence_completions']==1 and result['tool_calls']==3 and result['llm_calls']==4
    assert {e['name'] for e in result['payload']['evidence']}=={'get_order','get_return_policy','check_return_eligibility'}
    assert any(e['tool_call_id'].startswith('complete-') for e in result['evidence'])


def test_evidence_completion_cannot_exceed_read_budget(stage3,monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings,'max_tool_calls',2)
    fake=FakeLLM();fake.script=[fake.triage(),incomplete(fake),fake.draft]
    result=run(AnalysisAgent(fake,stage3['gateway']))
    assert result['status']=='failed'
    assert result['tool_calls']==2


def test_glm_thinking_depth_is_bounded_without_switching_model(monkeypatch):
    from src.config import settings
    from src.agent.llm import thinking_options
    monkeypatch.setattr(settings,'openai_model','glm-5.3')
    assert thinking_options()=={'enable_thinking':True,'reasoning_effort':'low'}

import asyncio
import pytest
from src.agent.nodes import AnalysisAgent
from src.domain.exceptions import AppError
from src.domain.proposals import Evidence
from src.agent.evidence import return_facts
from tests.fakes import FakeLLM
from tests.test_agent import run


def test_greeting_is_reply_without_model_guessing_or_order_questions():
    fake=FakeLLM(kind='clarification',intent='general',order_id=None,reason=None)
    result=run(AnalysisAgent(fake),'您好，谢谢您的帮助。')
    assert result['payload']['kind']=='reply_only' and result['llm_calls']==0
    assert result['entities']['intent']=='general'


def test_evidence_references_are_short_unique_and_still_match_real_reads(stage3):
    result=run(AnalysisAgent(FakeLLM(),stage3['gateway']))
    evidence=result['payload']['evidence']
    assert all(len(e['call_id'])<=16 for e in evidence)
    assert len({e['call_id'] for e in evidence})==3
    assert set(result['payload']['evidence_refs'])=={e['call_id'] for e in evidence}


def test_missing_policy_can_be_manual_but_never_executable(pg):
    from src.evaluation.dataset import load_cases
    from src.evaluation.runner import model_case
    case=next(c for c in load_cases() if c['id']=='TEST-M-028')
    result=asyncio.run(model_case(case,'offline',None))
    proposal=result['turns'][-1]['proposal']
    assert proposal['status']=='pending_approval' and proposal['payload']['kind']=='handoff'
    assert result['database_checks']['analysis_creates_no_ticket']
    p=proposal['payload']
    with pytest.raises(AppError):
        return_facts([Evidence.model_validate(e) for e in p['evidence']],p['evidence_refs'],'ORD-1009','defective')

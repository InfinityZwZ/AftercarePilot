import asyncio
import pytest
from src.evaluation.dataset import load_cases
from src.evaluation.runner import model_case


@pytest.mark.parametrize('identity',['TEST-M-020','TEST-M-030'])
def test_offline_tools_follow_each_turn_order_instead_of_final_gold(pg,identity):
    case=next(c for c in load_cases() if c['id']==identity)
    result=asyncio.run(model_case(case,'offline',None))
    assert result['passed']
    first=result['turns'][0]['proposal']
    assert first['entities']['order_id']=='ORD-1009'
    assert first['status']=='pending_approval'
    first_read=next(e for e in first['payload']['evidence'] if e['name']=='get_order')
    assert first_read['args']['order_id']=='ORD-1009'

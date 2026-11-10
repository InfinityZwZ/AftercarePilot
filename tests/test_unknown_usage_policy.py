import json
import pytest
from src.evaluation.budget import Budget,BudgetStop


def test_explicit_override_keeps_unknown_reservation_without_inventing_usage(tmp_path):
    b=Budget(tmp_path/'ledger.json')
    identity=b.reserve('test',100,200)
    with pytest.raises(BudgetStop):b.settle(identity,None)
    b.allow_reserved_unknown('用户允许放宽限制继续评测')
    value=json.loads(b.path.read_text(encoding='utf-8'))
    assert value['input']==100 and value['output']==200
    assert value['pending'] is None and not value['halted']
    assert value['entries'][0]['status']=='reserved_unknown_usage' and 'usage' not in value['entries'][0]
    nxt=b.reserve('test',100,200);b.settle(nxt,{'input_tokens':10,'output_tokens':20})
    assert json.loads(b.path.read_text(encoding='utf-8'))['output']==220


def test_override_never_clears_over_reservation_or_live_inflight(tmp_path):
    b=Budget(tmp_path/'ledger.json')
    i=b.reserve('test',10,10)
    with pytest.raises(BudgetStop):b.allow_reserved_unknown('授权')
    with pytest.raises(BudgetStop):b.settle(i,{'input_tokens':11,'output_tokens':10})
    with pytest.raises(BudgetStop):b.allow_reserved_unknown('授权')


def test_three_consecutive_unknown_requests_stop_even_under_override(tmp_path):
    b=Budget(tmp_path/'ledger.json')
    b.allow_reserved_unknown('授权')
    for _ in range(2):
        i=b.reserve('test',10,10);b.settle(i,None)
    i=b.reserve('test',10,10)
    with pytest.raises(BudgetStop):b.settle(i,None)
    assert json.loads(b.path.read_text(encoding='utf-8'))['halted']


def test_explicit_broader_total_never_releases_known_upper_bound(tmp_path):
    b=Budget(tmp_path/'ledger.json')
    i=b.reserve('test',10,20)
    with pytest.raises(BudgetStop):b.settle(i,None)
    b.allow_reserved_unknown('明确授权放宽累计未知数量',max_total=12)
    value=json.loads(b.path.read_text(encoding='utf-8'))
    assert value['unknown_policy']['max_total']==12
    assert value['input']==10 and value['output']==20

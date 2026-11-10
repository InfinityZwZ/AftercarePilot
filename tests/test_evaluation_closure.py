from src.evaluation.closure import measurement, compare_runs, human_measurement, budget_measurement


def test_missing_failed_and_not_run_stay_in_planned_denominator():
    report = {'manifest': {'selected': ['a','b','c']}, 'results': [
        {'id':'a','lane':'model','status':'completed','passed':True,'checks':{'intent':True},'duration_ms':10},
        {'id':'b','lane':'model','status':'completed','passed':False,'checks':{'intent':False},'duration_ms':30},
        {'id':'c','lane':'model','status':'not_run'}]}
    m=measurement(report)
    assert m['completed']==2 and m['not_run']==1 and m['passed']==1
    assert m['success_rate']==1/3 and m['checks']['intent']['denominator']==2
    assert m['latency_ms']=={'p50':10,'p95':30}


def test_different_gold_or_selected_cases_has_no_improvement_percentage():
    a={'manifest':{'run_id':'a','dataset_hash':'a','selected':['x'],'mode':'live','split':'test'},'results':[]}
    b={'manifest':{'run_id':'b','dataset_hash':'b','selected':['x'],'mode':'live','split':'test'},'results':[]}
    result=compare_runs(a,b)
    assert result['comparable'] is False and result['improvement'] is None


def test_unrated_disputes_are_neither_zero_nor_pass_and_new_ratings_not_merged():
    r={'records':[{'id':'x','scores':[2,2,2,2],'passed':True,
        'score_origin':'user_explicit_full_marks','factual_error':False,'false_completion_claim':False}]}
    h=human_measurement(r,3)
    assert h['rated']==1 and h['passed']==1 and h['unrated']==2
    assert h['pass_rate_rated']==1 and h['pass_rate_all'] is None


def test_budget_totals_use_global_ledger_and_never_report_unknown_as_zero():
    b={'calls':206,'input':239063,'output':94187,'dev_calls':34,'pending':None,'halted':False,
       'limits':{'calls':300,'input':600000,'output':100000,'dev_calls':60},'entries':[]}
    m=budget_measurement(b)
    assert m['remaining']['output']==5813 and m['used']['calls']==206
    assert m['usage_complete'] is False

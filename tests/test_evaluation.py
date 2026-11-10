import json
from pathlib import Path

import pytest

from src.evaluation.budget import Budget, BudgetStop
from src.evaluation.dataset import load_cases, validate_cases, require_review
from src.evaluation.scoring import score, summarize


def test_user_review_036_is_manual_without_inventing_return_and_041_splits_requests():
    cases = {case['id']: case for case in load_cases()}
    manual = cases['TEST-M-036']
    assert manual['expected']['kind'] == 'handoff'
    assert manual['expected']['intent'] == 'general'
    assert manual['expected']['missing_fields'] == [] and manual['post_action'] == 'none'
    assert 'clarification_prompt' in cases['TEST-M-041']['expected']
    assert cases['TEST-M-041']['expected']['kind'] == 'clarification'
    for number in (37, 38, 39, 42):
        assert cases[f'TEST-M-{number:03}']['expected']['kind'] == 'clarification'
        assert cases[f'TEST-M-{number:03}']['expected']['clarification_prompt']


def test_review_pack_preserves_only_explicit_current_case_approvals(tmp_path, monkeypatch):
    import scripts.evaluate as cli
    from src.evaluation.dataset import digest
    cases = load_cases()
    approved = next(case for case in cases if case['id'] == 'TEST-E-001')
    stale = next(case for case in cases if case['id'] == 'TEST-M-001')
    path = tmp_path / 'review-approvals.json'
    value = dict(dataset_hash=digest(cases), approvals={
        approved['id']: dict(case_hash=digest(approved), approved=True, reviewer='unit-user', reviewed_at='2026-11-06T12:00:00+08:00'),
        stale['id']: dict(case_hash='0' * 64, approved=True, reviewer='unit-user', reviewed_at='2026-11-06T12:00:00+08:00')})
    path.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setattr(cli, 'DATA', tmp_path)
    cli.review_pack(cases)
    assert json.loads(path.read_text()) == value
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert manifest['approved_cases'] == 1 and manifest['review_status'] == 'partial'
    assert '已确认' in (tmp_path / 'test-index.md').read_text(encoding='utf-8')
    assert '待审核' in (tmp_path / 'test-index.md').read_text(encoding='utf-8')


def test_catalog_counts_split_families_and_review_gate():
    cases = load_cases()
    validate_cases(cases)
    assert {(split, lane): sum(c['split'] == split and c['lane'] == lane for c in cases)
        for split in ('dev', 'test') for lane in ('model', 'engineering')} == {
        ('dev', 'model'): 25, ('dev', 'engineering'): 15, ('test', 'model'): 50, ('test', 'engineering'): 30}
    with pytest.raises(ValueError, match='review'):
        require_review(cases, None)
    leaked = [dict(c) for c in cases]
    leaked[-1]['family'] = leaked[0]['family']
    with pytest.raises(ValueError, match='family'):
        validate_cases(leaked)


def test_budget_durable_reservation_missing_usage_and_no_reset(tmp_path):
    path = tmp_path / 'ledger.json'
    b = Budget(path, limits={'calls': 2, 'input': 1000, 'output': 100, 'dev_calls': 1})
    ticket = b.reserve('dev', 100, 30)
    b.settle(ticket, {'input_tokens': 70, 'output_tokens': 20})
    with pytest.raises(BudgetStop):
        Budget(path).reserve('dev', 1, 1)
    ticket = b.reserve('test', 100, 30)
    with pytest.raises(BudgetStop):
        b.settle(ticket, None)
    with pytest.raises(BudgetStop):
        Budget(path).reserve('test', 1, 1)
    assert json.loads(path.read_text())['calls'] == 2


def test_wrong_answer_fails_and_unrun_not_in_success_denominator():
    case = {'expected': {'intent': 'return_request', 'kind': 'clarification', 'order_id': None,
        'reason': None, 'missing_fields': ['order_id'], 'required_tools': []}}
    result = score(case, {'entities': {'intent': 'general'}, 'payload': {'kind': 'reply_only'},
        'status': 'pending_approval'}, [], {'unauthorized_tickets': 0})
    assert not result['passed'] and not result['checks']['kind']
    report = summarize([{'status': 'completed', 'lane': 'model', **result}, {'status': 'not_run'}], planned=2)
    assert report['completed'] == 1 and report['not_run'] == 1
    assert report['by_lane']['model']['rate'] == 0 and not report['complete']


def test_model_prompt_input_cannot_contain_gold_and_no_test_run_before_review(tmp_path, monkeypatch):
    from src.evaluation.dataset import public_input
    from src.evaluation import runner
    cases = load_cases()
    projected = public_input(cases[0])
    assert set(projected) == {'turns', 'clock'}
    monkeypatch.setattr(runner, 'HOME', tmp_path / 'runs')
    with pytest.raises(ValueError, match='review'):
        runner.run(cases, split='test', mode='live')
    assert not (tmp_path / 'runs').exists()


def test_freeze_binds_every_test_case_and_edits_invalidate_review():
    from src.evaluation.dataset import digest
    cases = load_cases()
    approvals = dict(dataset_hash=digest(cases), approvals={case['id']: dict(case_hash=digest(case),
        approved=True, reviewer='unit-test-reviewer', reviewed_at='2026-11-06T12:00:00+08:00')
        for case in cases if case['split'] == 'test'})
    require_review(cases, approvals)
    changed = json.loads(json.dumps(cases))
    changed[-1]['expected']['assertion'] = 'changed'
    with pytest.raises(ValueError, match='review'):
        require_review(changed, approvals)


def test_deleted_ledger_cannot_restart_same_live_run(tmp_path, monkeypatch):
    from src.evaluation import runner
    monkeypatch.setattr(runner, 'HOME', tmp_path)
    path = tmp_path / 'runs' / 'prior-live'
    path.mkdir(parents=True)
    (path / 'manifest.json').write_text(json.dumps({'mode': 'live'}))
    with pytest.raises(ValueError, match='ledger'):
        runner.run(load_cases(), split='dev', mode='live', run_id='prior-live', limit=1)
    assert not (tmp_path / 'budget.json').exists()


def test_engineering_timeout_removes_raw_xml(tmp_path, monkeypatch):
    import subprocess
    from src.evaluation.runner import engineering_case
    case = next(case for case in load_cases() if case['lane'] == 'engineering')
    def timeout(*args, **kwargs):
        (tmp_path / (case['id'] + '.xml')).write_text('<error>private-value</error>')
        raise subprocess.TimeoutExpired('pytest', 180)
    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        engineering_case(case, tmp_path)
    assert not list(tmp_path.glob('*.xml'))


@pytest.mark.asyncio
async def test_triage_tool_call_is_structured_failure():
    from langchain_core.messages import AIMessage, SystemMessage
    from src.agent.nodes import TRIAGE_PROMPT
    from src.evaluation.model import RecordedModel
    class WrongModel:
        async def ainvoke(self, messages):
            return AIMessage(content='', tool_calls=[dict(name='get_order', args={'order_id': 'ORD-1009'}, id='bad')])
    traces = []
    await RecordedModel(WrongModel(), traces).ainvoke([SystemMessage(content=TRIAGE_PROMPT)])
    assert traces[0]['schema_valid'] is False


def test_unknown_or_over_reserved_usage_halts_next_call(tmp_path):
    b = Budget(tmp_path / 'ledger.json')
    identity = b.reserve('dev', 10, 10)
    with pytest.raises(BudgetStop):
        b.settle(identity, dict(input_tokens=11, output_tokens=1))
    with pytest.raises(BudgetStop):
        b.reserve('test', 1, 1)


@pytest.mark.asyncio
async def test_live_http_one_request_usage_and_redaction_without_network(tmp_path, monkeypatch):
    import httpx
    from langchain_core.messages import HumanMessage
    from src.evaluation.model import BudgetedModel, RecordedModel
    from src.config import settings
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            calls.append(kwargs)
            assert kwargs['json']['max_tokens'] == settings.evaluation_output_tokens
            assert kwargs['json']['enable_thinking'] == settings.openai_enable_thinking
            return httpx.Response(200, request=httpx.Request('POST', 'https://unit.invalid'), json={
                'usage': {'prompt_tokens': 50, 'completion_tokens': 10},
                'choices': [{'message': {'content': 'unit-secret'}}]})
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    monkeypatch.setattr(settings, 'openai_model', 'qwen3.8-max')
    monkeypatch.setattr(settings, 'openai_api_key', 'unit-secret')
    traces = []
    b = Budget(tmp_path / 'budget.json')
    model = RecordedModel(BudgetedModel(b, 'dev'), traces, ['unit-secret'])
    await model.ainvoke([HumanMessage(content='hello')])
    assert len(calls) == 1 and 'unit-secret' not in json.dumps(traces)
    data = json.loads(b.path.read_text())
    assert data['calls'] == 1 and data['input'] == 50 and data['output'] == 10


def test_real_pg_mcp_evaluation_runner_offline_and_cleanup(pg, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    from src.evaluation import runner
    from sqlalchemy import text
    original = runner.isolated_database
    owned = []
    @contextmanager
    def isolated(case):
        with original(case) as (sessions, reader):
            with sessions() as session:
                owned.append(session.scalar(text('SELECT current_schema()')))
            yield sessions, reader
    monkeypatch.setattr(runner, 'isolated_database', isolated)
    case = next(case for case in load_cases() if case['id'] == 'DEV-M-016')
    result = asyncio.run(runner.model_case(case, 'offline', None))
    assert result['passed'] and result['provider_calls'] == 0
    assert all(result['database_checks'].values())
    with pg['engine'].connect() as connection:
        assert connection.scalar(text('SELECT count(*) FROM pg_namespace WHERE nspname=:name'), {'name': owned[0]}) == 0


def test_reviewed_fixture_facts_drive_actual_database(pg):
    from src.evaluation.runtime import isolated_database
    from src.storage.models import Order, ReturnPolicy
    from copy import deepcopy
    case = deepcopy(next(case for case in load_cases() if case['id'] == 'DEV-M-016'))
    case['facts']['orders'][0]['total_cents'] = 12345
    case['facts']['policy']['return_window_days'] = 13
    with isolated_database(case) as (sessions, url):
        with sessions() as session:
            assert session.get(Order, 'ORD-1009').total_cents == 12345
            assert session.get(ReturnPolicy, 'demo-return').return_window_days == 13


def test_full_execution_of_failures_is_not_acceptance(tmp_path):
    from src.evaluation.reports import write_report
    manifest = dict(run_id='failed', mode='offline', lane='engineering', split='dev',
        selected=['a'], split_planned=1, dataset_hash='x', source_hash='y')
    report = write_report(tmp_path, manifest, [dict(id='a', lane='engineering', status='completed', passed=False)])
    assert report['summary']['coverage_complete'] is True
    assert report['summary']['acceptance_passed'] is False


def test_human_import_updates_report_without_changing_automatic_results(tmp_path):
    from src.evaluation.reports import write_report, import_human_reviews
    from src.evaluation.dataset import digest
    results = [dict(id='DEV-M-001', lane='model', status='completed', passed=True)]
    manifest = dict(run_id='rated', mode='live', lane='model', split='dev', selected=['DEV-M-001'],
        split_planned=40, dataset_hash='x', source_hash='y')
    write_report(tmp_path, manifest, results)
    reviews = dict(run_id='rated', results_hash=digest(results), reviews={'DEV-M-001': dict(
        reviewer='unit-reviewer', reviewed_at='2026-11-06T12:00:00+08:00', scores=[2, 2, 2, 2],
        factual_error=False, false_completion_claim=False)})
    import_human_reviews(tmp_path, reviews)
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['summary']['human_reviewed'] == 1
    assert report['summary']['human_passed'] == 1
    assert report['results'] == results


def test_atomic_ledger_write_retries_transient_windows_sharing_error(tmp_path, monkeypatch):
    import os
    from src.evaluation.budget import atomic_json
    original = os.replace
    attempts = []
    def sharing_error(source, target):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError('simulated temporary file sharing conflict')
        return original(source, target)
    monkeypatch.setattr(os, 'replace', sharing_error)
    atomic_json(tmp_path / 'state.json', {'count': 1})
    assert len(attempts) == 2 and json.loads((tmp_path / 'state.json').read_text()) == {'count': 1}


def test_complete_human_review_closes_evaluation_without_claiming_perfect_model(tmp_path):
    from src.evaluation.reports import write_report, import_human_reviews
    from src.evaluation.dataset import digest
    rows = [dict(id='a', lane='model', status='completed', passed=False)]
    manifest = dict(run_id='whole', mode='live', lane='all', split='test', selected=['a'],
        split_planned=1, dataset_hash='unit', source_hash='unit')
    write_report(tmp_path, manifest, rows)
    import_human_reviews(tmp_path, dict(run_id='whole', results_hash=digest(rows), reviews={'a': dict(
        reviewer='unit-reviewer', reviewed_at='2026-11-06T12:00:00+08:00', scores=[1, 1, 1, 1],
        factual_error=True, false_completion_claim=False)}))
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report['summary']['evaluation_closed'] and report['summary']['acceptance_passed']
    assert report['summary']['human_passed'] == 0 and report['summary']['failed'] == 1


def test_editing_dataset_does_not_restore_unseen_status(tmp_path, monkeypatch):
    from src.evaluation import runner
    monkeypatch.setattr(runner, 'HOME', tmp_path)
    monkeypatch.setattr(runner, 'validate_cases', lambda cases: None)
    monkeypatch.setattr(runner, 'require_review', lambda cases, frozen: None)
    monkeypatch.setattr(runner, 'archive_source', lambda *args: None)
    monkeypatch.setattr(runner, 'engineering_case', lambda *args: dict(status='completed', passed=True))
    monkeypatch.setattr(runner, 'source_manifest', lambda: dict(source_hash='unit', file_hashes={},
        prompt_hash='unit', model='unit', endpoint_hash='unit', temperature=0, limits={}))
    synthetic = [dict(id='unit-only', split='test', lane='engineering')]
    first = runner.run(synthetic, split='test', mode='offline', run_id='unit-first', frozen={})
    changed = [{**synthetic[0], 'revision_note': 'edited after seeing failure'}]
    second = runner.run(changed, split='test', mode='offline', run_id='unit-second', frozen={})
    assert json.loads((first / 'manifest.json').read_text())['held_out_first_run'] is True
    assert json.loads((second / 'manifest.json').read_text())['held_out_first_run'] is False


@pytest.mark.parametrize('args', [{}, {'policy_id': 'WRONG-POLICY'}])
def test_tool_argument_score_checks_required_fields_and_policy_identity(args):
    case = dict(expected=dict(intent='return_policy', kind='reply_only', order_id=None, reason=None,
        required_tools=['get_return_policy']), facts=dict(policy=dict(policy_id='demo-return')))
    proposal = dict(status='pending_approval', entities=dict(intent='return_policy'), payload=dict(
        kind='reply_only', evidence=[dict(call_id='bad', name='get_return_policy', args=args, result={'ok': False})]))
    assert score(case, proposal, [], {})['checks']['tool_arguments'] is False

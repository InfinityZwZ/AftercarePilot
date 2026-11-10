import asyncio
from datetime import datetime
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from uuid import UUID

from sqlalchemy import func, select

from src.agent.nodes import AnalysisAgent
from src.config import settings
from src.domain.clock import FixedClock
from src.domain.exceptions import AppError
from src.evaluation.budget import Budget, exclusive, atomic_json
from src.evaluation.dataset import ROOT, DATA, digest, public_input, require_review, validate_cases
from src.evaluation.model import BudgetedModel, RecordedModel
from src.evaluation.runtime import isolated_database, factual_snapshot
from src.evaluation.scoring import score, summarize, percentile
from src.mcp.gateway import MCPReadGateway
from src.services.aftersales_service import AftersalesService
from src.services.proposal_service import ProposalService
from src.storage.models import AftersalesTicket

HOME = ROOT / '.local' / 'evaluation'


def source_manifest():
    from src.agent.llm import thinking_options
    from src.agent.nodes import TRIAGE_PROMPT, DECISION_PROMPT
    paths = sorted(path for folder in ('src', 'scripts', 'tests', 'alembic') for path in (ROOT / folder).rglob('*.py'))
    files = {path.relative_to(ROOT).as_posix(): digest(path.read_text(encoding='utf-8')) for path in paths}
    files['requirements.lock.txt'] = digest((ROOT / 'requirements.lock.txt').read_text(encoding='utf-8'))
    return dict(source_hash=digest(files), file_hashes=files, prompt_hash=digest([TRIAGE_PROMPT, DECISION_PROMPT]),
        model=settings.openai_model, endpoint_hash=digest(settings.openai_base_url), temperature=settings.temperature,
        model_options=thinking_options(),enable_thinking=thinking_options().get('enable_thinking'),
        limits=dict(max_llm_calls=settings.max_llm_calls, max_tool_calls=settings.max_tool_calls,
                    max_tool_rounds=settings.max_tool_rounds, output_tokens=settings.evaluation_output_tokens), python=sys.version.split()[0])


def archive_source(artifact, manifest, cases):
    import zipfile
    with zipfile.ZipFile(artifact / 'source.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, expected in manifest['file_hashes'].items():
            content = (ROOT / name).read_text(encoding='utf-8')
            if digest(content) != expected:
                raise ValueError('Source changed before snapshot')
            archive.writestr(name, content)
        archive.writestr('evaluation/v1/cases.jsonl', ''.join(json.dumps(case, ensure_ascii=False) + '\n' for case in cases))


async def model_case(case, mode, budget):
    traces, turns = [], []
    budget_before = len(json.loads(budget.path.read_text(encoding='utf-8'))['entries']) if budget else 0
    def usage():
        entries = json.loads(budget.path.read_text(encoding='utf-8'))['entries'][budget_before:] if budget else []
        return dict(provider_calls=len(entries), provider_usage_unknown=sum(entry['status'] != 'settled' for entry in entries),
            provider_input_tokens=sum(entry.get('usage', {}).get('input_tokens', 0) for entry in entries),
            provider_output_tokens=sum(entry.get('usage', {}).get('output_tokens', 0) for entry in entries))
    with isolated_database(case) as (sessions, reader_url):
        clock = FixedClock(datetime.fromisoformat(case['clock']))
        service = ProposalService(sessions, clock)
        aftersales = AftersalesService(sessions, clock)
        before = factual_snapshot(sessions)
        request_id = None
        started = perf_counter()
        for index, question in enumerate(public_input(case)['turns']):
            if mode == 'live':
                model = BudgetedModel(budget, case['split'])
            else:
                from tests.fakes import FakeLLM
                expected = case['expected']
                reason = None if index == 0 and len(case['turns']) > 1 and case['family'] == 'missing_reason' else expected.get('reason')
                explicit=list(dict.fromkeys(re.findall(r'(?<![A-Za-z0-9])ORD-[A-Z0-9-]+',question.upper())))
                current_order=explicit[0] if len(explicit)==1 else (
                    turns[-1]['proposal'].get('entities',{}).get('order_id') if turns and not explicit else None)
                model = FakeLLM(order_id=current_order, reason=reason,
                    intent=expected['intent'], kind=expected['kind'])
            recorded = RecordedModel(model, traces, (settings.openai_api_key,
                settings.operator_token.get_secret_value(), reader_url, settings.database_url.get_secret_value()))
            agent = AnalysisAgent(recorded, MCPReadGateway(reader_url, reference_time=clock.now()))
            try:
                proposal = await service.analyze(question, request_id=request_id, operator='evaluation-operator',
                    language='zh', agent=agent)
            except AppError as error:
                if error.details.get('proposal_id'):
                    proposal = service.get(UUID(error.details['proposal_id']))
                else:
                    raise
            request_id = UUID(proposal['request_id'])
            turns.append(dict(question=question, proposal=proposal))
            if (proposal.get('error') or {}).get('code') == 'EVALUATION_BUDGET_STOP':
                return dict(status='not_run', reason='budget_stop', traces=traces, turns=turns,
                    **usage())
        analysis_ms = (perf_counter() - started) * 1000
        after_analysis = factual_snapshot(sessions)
        checks = dict(analysis_creates_no_ticket=before['aftersales_tickets'] == after_analysis['aftersales_tickets'],
            orders_and_policies_unchanged=all(before[key] == after_analysis[key] for key in ('orders', 'return_policies')))
        post_action = case.get('post_action', 'none')
        execution = None
        if proposal['status'] == 'pending_approval':
            try:
                identity = UUID(proposal['proposal_id'])
                if post_action == 'confirm_return':
                    execution = aftersales.confirm_and_create(identity, proposal['revision'], proposal['payload_hash'], 'evaluation-operator')
                    verified = aftersales.get_execution(identity)
                    checks['confirmed_ticket_verified'] = verified == execution
                    replay = aftersales.confirm_and_create(identity, proposal['revision'], proposal['payload_hash'], 'evaluation-operator')
                    checks['confirmation_idempotent'] = replay == execution
                    ticket = execution['ticket']
                    checks['no_wrong_ticket'] = all(ticket.get(key) == value for key, value in dict(
                        order_id=case['expected']['order_id'], reason=case['expected']['reason'],
                        requested_amount_cents=case['expected']['amount_cents'], currency=case['expected']['currency']).items())
                    checks['no_duplicate_ticket'] = len(factual_snapshot(sessions)['aftersales_tickets']) == len(before['aftersales_tickets']) + 1
                elif post_action == 'confirm_draft':
                    service.review(identity, proposal['revision'], proposal['payload_hash'], 'evaluation-operator')
                    checks['draft_confirmation_no_ticket'] = factual_snapshot(sessions)['aftersales_tickets'] == before['aftersales_tickets']
            except AppError as error:
                checks['post_action'] = False
                execution = dict(error_code=error.code)
        elif post_action != 'none':
            checks['post_action'] = False
        scored = score(case, proposal, traces, checks)
        # Intermediate clarification is explicitly part of multi-turn acceptance.
        for index, expected in enumerate(case.get('intermediate', [])):
            scored['checks'][f'turn_{index + 1}'] = all(turns[index]['proposal'].get(key) == value for key, value in expected.items())
        scored['passed'] = all(scored['checks'].values())
        scored['failure_categories'] = [key for key, value in scored['checks'].items() if not value]
        return dict(status='completed', **scored, turns=turns, traces=traces, execution=execution,
            database_checks=checks, duration_ms=analysis_ms, **usage())


def engineering_case(case, artifact):
    import xml.etree.ElementTree as ET
    xml = artifact / (case['id'] + '.xml')
    try:
        process = subprocess.run([sys.executable, '-m', 'pytest', case['selector'], '-q', '--tb=no',
            '--junitxml=' + str(xml)], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', timeout=180)
        cases = list(ET.parse(xml).iter('testcase'))
        skipped = sum(child.find('skipped') is not None for child in cases)
        passed = process.returncode == 0 and bool(cases) and skipped == 0
        atomic_json(artifact / (case['id'] + '-engineering.json'), dict(returncode=process.returncode,
            subcases=len(cases), skipped=skipped))
        return dict(status='completed', passed=passed, subcases=len(cases), skipped=skipped,
                    failure_categories=[] if passed else ['engineering_assertions'], provider_calls=0)
    finally:
        xml.unlink(missing_ok=True)


def run(cases, *, split, mode, lane='all', limit=None, run_id=None, frozen=None, case_ids=None):
    validate_cases(cases)
    if split == 'test':
        require_review(cases, frozen)
    selected = [case for case in cases if case['split'] == split and (lane == 'all' or case['lane'] == lane)]
    if case_ids:
        if not set(case_ids) <= {case['id'] for case in selected}:
            raise ValueError('Case IDs do not match selected split/lane')
        selected = [case for case in selected if case['id'] in case_ids]
    if limit:
        selected = selected[:limit]
    run_id = run_id or datetime.now().astimezone().strftime('%Y%m%dT%H%M%S') + '-' + os.urandom(3).hex()
    if not run_id.replace('-', '').replace('T', '').isalnum() or len(run_id) > 80:
        raise ValueError('Invalid run ID')
    artifact = HOME / 'runs' / run_id
    manifest = dict(run_id=run_id, split=split, mode=mode, lane=lane, dataset_hash=digest(cases), dataset_lineage=DATA.name,
        selected=[case['id'] for case in selected], split_planned=sum(case['split'] == split for case in cases),
        created_at=datetime.now().astimezone().isoformat(), **source_manifest())
    HOME.mkdir(parents=True, exist_ok=True)
    with exclusive(HOME / 'runner.lock'):
        if mode == 'live' and not (HOME / 'budget.json').exists():
            for path in (HOME / 'runs').glob('*/manifest.json'):
                if json.loads(path.read_text(encoding='utf-8')).get('mode') == 'live':
                    raise ValueError('Missing historical live budget ledger; refusing reset')
        budget = Budget(HOME / 'budget.json') if mode == 'live' else None
        if split == 'test':
            exposure = HOME / ('test-exposure-' + DATA.name + '.json')
            if exposure.exists():
                history = json.loads(exposure.read_text(encoding='utf-8'))
                manifest['held_out_first_run'] = history['run_id'] == run_id
            else:
                atomic_json(exposure, dict(run_id=run_id, source_hash=manifest['source_hash']))
                manifest['held_out_first_run'] = True
        if (artifact / 'manifest.json').exists():
            old = json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))
            for key in ('split', 'mode', 'lane', 'dataset_hash', 'selected', 'source_hash', 'prompt_hash', 'model', 'endpoint_hash', 'temperature', 'limits'):
                if old[key] != manifest[key]:
                    raise ValueError('Cannot resume after dataset/code/config changes; use a new run ID')
        else:
            atomic_json(artifact / 'manifest.json', manifest)
            archive_source(artifact, manifest, cases)
        stopped = False
        results = []
        for case in selected:
            path = artifact / 'results' / (case['id'] + '.json')
            if path.exists():
                result = json.loads(path.read_text(encoding='utf-8'))
                if result['status'] == 'running':
                    result.update(status='not_run', reason='interrupted_requires_manual_reconciliation')
                results.append(result)
                continue
            result = dict(id=case['id'], lane=case['lane'], status='not_run', reason='budget_stop')
            if not stopped:
                if mode == 'live' and source_manifest()['source_hash'] != manifest['source_hash']:
                    raise ValueError('Source changed during live evaluation; no further requests allowed')
                if budget:
                    budget.context = dict(run_id=run_id, case_id=case['id'])
                atomic_json(path, {**result, 'status': 'running'})
                try:
                    value = engineering_case(case, artifact) if case['lane'] == 'engineering' else asyncio.run(model_case(case, mode, budget))
                    result.update(value)
                    result.pop('reason', None) if value['status'] == 'completed' else None
                except Exception as error:
                    result.update(status='not_run', reason='infrastructure_' + type(error).__name__)
                if result['status'] != 'completed':
                    stopped = True
            serialized = json.dumps(result, ensure_ascii=False, default=str)
            for secret in (settings.openai_api_key, settings.operator_token.get_secret_value(),
                           settings.database_url.get_secret_value(), settings.readonly_database_url.get_secret_value()):
                if secret:
                    serialized = serialized.replace(secret, '[REDACTED]')
            result = json.loads(serialized)
            atomic_json(path, result)
            results.append(result)
        from src.evaluation.reports import write_report
        write_report(artifact, manifest, results)
    return artifact

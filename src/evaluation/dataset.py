import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'evaluation' / 'v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def load_cases():
    return [json.loads(line) for line in (DATA / 'cases.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]


def validate_cases(cases):
    from src.evaluation.catalog import ENGINEERING_DEV, ENGINEERING_TEST
    allowed = {f'tests/{module}.py::{function}' for _, module, function, _ in ENGINEERING_DEV + ENGINEERING_TEST}
    if len(cases) != 120 or len({c['id'] for c in cases}) != 120:
        raise ValueError('Expected 120 unique cases')
    families, questions = {}, set()
    counts = {}
    for case in cases:
        split, lane = case['split'], case['lane']
        counts[split, lane] = counts.get((split, lane), 0) + 1
        if families.setdefault(case['family'], split) != split:
            raise ValueError('Cross-split family leakage')
        for field in ('source', 'clock', 'facts', 'turns', 'expected', 'forbidden', 'database_assertions', 'rubric', 'review_status'):
            if not case.get(field):
                raise ValueError('Missing case field: ' + field)
        if lane == 'engineering':
            if case['selector'] not in allowed:
                raise ValueError('Engineering selector is not in executable allowlist')
            path, name = case['selector'].split('::')
            if 'def ' + name + '(' not in (ROOT / path).read_text(encoding='utf-8'):
                raise ValueError('Missing mapped test: ' + name)
        else:
            text = tuple(case['turns'])
            if text in questions:
                raise ValueError('Duplicate input')
            questions.add(text)
    if counts != {('dev', 'model'): 25, ('dev', 'engineering'): 15, ('test', 'model'): 50, ('test', 'engineering'): 30}:
        raise ValueError('Wrong split sizes')


def require_review(cases, manifest):
    if not manifest or manifest.get('dataset_hash') != digest(cases):
        raise ValueError('Human review is missing or dataset changed')
    approvals = manifest.get('approvals', {})
    for case in cases:
        if case['split'] != 'test':
            continue
        record = approvals.get(case['id'], {})
        if not (record.get('approved') is True and record.get('reviewer', '').strip() and
                record.get('reviewed_at') and record.get('case_hash') == digest(case)):
            raise ValueError('Human review incomplete: ' + case['id'])


def public_input(case):
    # The model receives only questions/context built by the production service.
    return dict(turns=list(case['turns']), clock=case['clock'])

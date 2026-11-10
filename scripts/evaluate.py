"""Unified, approval-gated evaluator. Never targets the configured production database."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from src.evaluation.budget import atomic_json
from src.evaluation.dataset import DATA, ROOT, digest, load_cases, require_review, validate_cases


def current_approvals(cases):
    path = DATA / 'review-approvals.json'
    records = json.loads(path.read_text(encoding='utf-8')).get('approvals', {}) if path.exists() else {}
    valid = {}
    for case in cases:
        record = records.get(case['id'], {})
        if (case['split'] == 'test' and record.get('approved') is True and
                record.get('case_hash') == digest(case) and record.get('reviewer', '').strip() and record.get('reviewed_at')):
            valid[case['id']] = record
    return valid


def review_pack(cases):
    validate_cases(cases)
    approved = current_approvals(cases)
    for split in ('dev', 'test'):
        lines = [f'# {"开发集" if split == "dev" else "测试集"}审核表', '',
            f'合成场景审核材料；测试集当前已确认 {len(approved)}/80 条，其余待审核。确认预期不等于已经执行测试或人工评分。', '',
            '请逐条核对客户原话、数据与预期；修改 cases.jsonl 后须重新生成审核表。测试集审批文件填写真实审核人、时间、approved=true。', '']
        for case in (case for case in cases if case['split'] == split):
            lines.extend([f"## {case['id']} · {case['family']} · {case['lane']}", '',
                f"来源：{case['source']}", f"固定时间：{case['clock']}", '',
                *[f'{index}. {question}' for index, question in enumerate(case['turns'], 1)], '',
                '业务事实与预期：', '```json', json.dumps(dict(facts=case['facts'], expected=case['expected'],
                    intermediate=case.get('intermediate'), post_action=case.get('post_action'),
                    selector=case.get('selector')), ensure_ascii=False, indent=2, default=str), '```', '',
                '禁止行为：' + '、'.join(case['forbidden']),
                '数据库断言：' + '、'.join(case['database_assertions']),
                '评分项：' + '、'.join(case['rubric']),
                '审核：已确认（用户本会话明确认可预期）' if case['id'] in approved else '审核：待审核', ''])
        (DATA / (split + '-review.md')).write_text('\n'.join(lines), encoding='utf-8')
    template = dict(dataset_hash=digest(cases), approvals={case['id']: dict(case_hash=digest(case),
        approved=False, reviewer='', reviewed_at='', notes='') for case in cases if case['split'] == 'test'})
    atomic_json(DATA / 'review-template.json', template)
    atomic_json(DATA / 'manifest.json', dict(version='v1-draft', dataset_hash=digest(cases), count=len(cases),
        review_status='reviewed' if len(approved) == 80 else 'partial' if approved else 'pending',
        approved_cases=len(approved), pending_cases=80-len(approved),
        case_hashes={case['id']: digest(case) for case in cases}))
    labels = {'return_request': '退货方案', 'reply_only': '回复草稿', 'clarification': '待补充', 'handoff': '需人工处理'}
    lines = ['# 80条测试场景审核索引', '',
        f'已确认 {len(approved)}/80 条，待审核 {80-len(approved)} 条；确认预期不代表测试已执行。', '',
        '预期列表示系统处理动作，不只是客户意图。完整事实、标准答案与追问要求见 [完整审核表](test-review.md)。', '',
        '| 编号 | 类型 | 客户问题或工程断言 | 预期处理 | 审核状态 |',
        '| --- | --- | --- | --- | --- |']
    for case in (case for case in cases if case['split'] == 'test'):
        expected = labels.get(case['expected'].get('kind'), case['expected'].get('assertion', ''))
        if case['expected'].get('clarification_prompt'):
            expected += '：' + case['expected']['clarification_prompt']
        text = ' → '.join(case['turns']).replace('|', '／')
        lines.append(f"| {case['id']} | {'模型' if case['lane'] == 'model' else '工程'} | {text} | {expected} | {'已确认' if case['id'] in approved else '待审核'} |")
    (DATA / 'test-index.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description='售后工作台评测：正式测试集需要完整人工审核')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate')
    sub.add_parser('review-pack')
    freeze = sub.add_parser('freeze')
    freeze.add_argument('--review-file', type=Path, required=True)
    run = sub.add_parser('run')
    run.add_argument('--split', choices=['dev', 'test'], required=True)
    run.add_argument('--mode', choices=['offline', 'live'], default='offline')
    run.add_argument('--lane', choices=['model', 'engineering', 'all'], default='all')
    run.add_argument('--limit', type=int)
    run.add_argument('--case', action='append', dest='case_ids')
    run.add_argument('--run-id')
    report = sub.add_parser('report')
    report.add_argument('--run-id', required=True)
    human = sub.add_parser('human-import')
    human.add_argument('--run-id', required=True)
    human.add_argument('--review-file', type=Path, required=True)
    comparison = sub.add_parser('compare')
    comparison.add_argument('--left', required=True)
    comparison.add_argument('--right', required=True)
    args = parser.parse_args()
    cases = load_cases()
    validate_cases(cases)
    if args.command == 'validate':
        count = len(current_approvals(cases))
        print(json.dumps(dict(cases=len(cases), status='valid_draft', approved=count, pending=80-count,
            human_review='reviewed' if count == 80 else 'partial' if count else 'pending')))
    elif args.command == 'review-pack':
        review_pack(cases)
        print('Review files written; no approval granted and no model called.')
    elif args.command == 'freeze':
        approvals = json.loads(args.review_file.read_text(encoding='utf-8'))
        require_review(cases, approvals)
        atomic_json(DATA / 'frozen.json', {**approvals, 'frozen_at': datetime.now().astimezone().isoformat()})
        print('Reviewed dataset frozen; no evaluation executed.')
    else:
        from src.evaluation.runner import HOME, run, source_manifest
        def artifact(identity):
            if not identity.replace('-', '').replace('T', '').isalnum() or len(identity) > 80:
                raise ValueError('Invalid run ID')
            return HOME / 'runs' / identity
        from src.evaluation.reports import compare, import_human_reviews, write_report
        if args.command == 'run':
            if args.limit is not None and args.limit < 1:
                raise ValueError('Limit must be positive')
            frozen = json.loads((DATA / 'frozen.json').read_text(encoding='utf-8')) if (DATA / 'frozen.json').exists() else None
            path = run(cases, split=args.split, mode=args.mode, lane=args.lane, limit=args.limit,
                       run_id=args.run_id, frozen=frozen, case_ids=args.case_ids)
            report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
            print(json.dumps(dict(run_id=path.name, summary=report['summary']), ensure_ascii=False))
        elif args.command == 'compare':
            result = compare(*[json.loads((artifact(value) / 'report.json').read_text(encoding='utf-8')) for value in (args.left, args.right)])
            atomic_json(artifact(args.right) / ('compare-' + args.left + '.json'), result)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == 'human-import':
            result = import_human_reviews(artifact(args.run_id), json.loads(args.review_file.read_text(encoding='utf-8')))
            print(json.dumps(dict(reviewed=result['reviewed'], passed=result['passed'], denominator=result['denominator'])))
        else:
            path = artifact(args.run_id)
            manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
            results = [json.loads((path / 'results' / (identity + '.json')).read_text(encoding='utf-8'))
                       for identity in manifest['selected']]
            print(json.dumps(write_report(path, manifest, results)['summary'], ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exception bodies from SQL/HTTP providers may contain credentials.
        raise SystemExit('Evaluation stopped safely: ' + type(error).__name__ + '; inspect review/budget/run status, not raw credentials') from None

from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from src.evaluation.budget import atomic_json
from src.evaluation.dataset import digest
from src.evaluation.scoring import summarize, percentile


def write_report(artifact, manifest, results):
    summary = summarize(results, len(manifest['selected']))
    summary['coverage_complete'] = summary['complete'] and len(manifest['selected']) == manifest['split_planned']
    summary['split_planned'] = manifest['split_planned']
    summary['mode'] = manifest['mode']
    summary['provider_calls'] = sum(row.get('provider_calls', 0) for row in results)
    summary['input_tokens'] = sum(row.get('provider_input_tokens', 0) for row in results)
    summary['output_tokens'] = sum(row.get('provider_output_tokens', 0) for row in results)
    summary['usage_unknown_requests'] = sum(row.get('provider_usage_unknown', 0) for row in results)
    summary['usage_complete'] = summary['usage_unknown_requests'] == 0
    summary['safety_blockers'] = [dict(id=row['id'], check=key) for row in results for key, value in row.get('checks', {}).items()
        if key in ('analysis_creates_no_ticket', 'no_wrong_ticket', 'no_duplicate_ticket', 'confirmation_idempotent') and not value]
    summary['automatic_assertions_passed'] = summary['coverage_complete'] and summary['failed'] == 0 and not summary['safety_blockers']
    summary['acceptance_passed'] = False  # Set only by a separate complete human-review acceptance artifact.
    human_path = artifact / 'human-review.json'
    if human_path.exists():
        human = json.loads(human_path.read_text(encoding='utf-8'))
        if human.get('results_hash') == digest(results):
            summary['human_reviewed'] = human['reviewed']
            summary['human_passed'] = human['passed']
            summary['evaluation_closed'] = human['evaluation_closed']
            summary['engineering_acceptance_passed'] = human['engineering_acceptance_passed']
            summary['acceptance_passed'] = human['engineering_acceptance_passed']
            summary['acceptance_definition'] = '评测执行及人工复核完整、工程安全通过；不是模型全对或生产上线通过'
    traces = [trace for row in results for trace in row.get('traces', [])]
    durations = [trace['duration_ms'] for trace in traces]
    summary['model_request_latency_ms'] = dict(p50=percentile(durations, .5), p95=percentile(durations, .95))
    summary['family_count'] = None  # Families are dataset provenance, not extra independent measurements.
    for metric in ('schema_first_pass', 'schema_final_pass'):
        values = [row[metric] for row in results if row.get(metric) is not None]
        summary[metric] = dict(denominator=len(values), correct=sum(values), rate=sum(values) / len(values) if values else None)
    failures = [dict(id=row['id'], status=row['status'], categories=row.get('failure_categories', []),
        reason=row.get('reason'), next_step='仅在开发集修复；冻结测试集保留原始失败结果')
        for row in results if not row.get('passed')]
    report = dict(manifest=manifest, summary=summary, results=results, failures=failures,
        generated_at=datetime.now().astimezone().isoformat(),
        limitations=['合成场景，不代表生产客户分布', '工程用例为既有回归映射，不是未见模型评测',
            'offline 使用预设 FakeLLM，不衡量模型理解能力', '含无需模型的确定性澄清；provider_invoked 单独披露',
            '未评分回复不算人工通过；没有可靠计价时不报告人民币费用'])
    atomic_json(artifact / 'report.json', report)
    metrics_label = '离线流程结果（不能作模型准确率）' if manifest['mode'] == 'offline' else '真实模型参与的系统结果（包含确定性校验）'
    lines = [f"# 评测报告 {manifest['run_id']}", '', metrics_label, '',
        f"范围：{manifest['split']} / {manifest['lane']}；选择 {summary['planned']} / 全划分 {manifest['split_planned']} 场景。",
        f"完成 {summary['completed']}，失败 {summary['failed']}，未运行 {summary['not_run']}；完整划分执行完成：{summary['coverage_complete']}。",
        f"人工已评分 {summary['human_reviewed']} 条；执行完成不等于验收通过。",
        f"真实模型请求 {summary['provider_calls']}；输入 token {summary['input_tokens']}，输出 token {summary['output_tokens']}。", '',
        '## 分项指标', '', '```json', json.dumps(summary, ensure_ascii=False, indent=2), '```', '',
        '## 失败与未完成场景', '', *[f"- {row['id']}：{row['reason'] or '、'.join(row['categories'])}" for row in failures], '',
        '## 简历证据边界', '',
        f"- 运行编号：{manifest['run_id']}；数据 hash：{manifest['dataset_hash']}；源码 hash：{manifest['source_hash']}。",
        f"- 本次可核对执行场景数 {summary['completed']} / {summary['planned']}；不是人工审核通过数。",
        '- 人工评分及真实测试集验收完成前，不填写模型准确率或人工通过率的简历宣传语。',
        '- 没有可比基线，不填写提升百分比；没有生产负载，不填写线上规模。', '',
        '## 限制', '', *['- ' + value for value in report['limitations']]]
    (artifact / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    template = dict(run_id=manifest['run_id'], results_hash=digest(results), reviews={row['id']: dict(
        reviewer='', reviewed_at='', scores=None, factual_error=None, false_completion_claim=None)
        for row in results if row.get('lane') == 'model' and row['status'] == 'completed'})
    if not (artifact / 'human-review-template.json').exists():
        atomic_json(artifact / 'human-review-template.json', template)
    return report


def import_human_reviews(artifact, reviews):
    report = json.loads((artifact / 'report.json').read_text(encoding='utf-8'))
    if reviews.get('run_id') != report['manifest']['run_id'] or reviews.get('results_hash') != digest(report['results']):
        raise ValueError('Human review does not match this run')
    records = []
    for identity, review in reviews['reviews'].items():
        scores = review.get('scores')
        if not (review.get('reviewer', '').strip() and review.get('reviewed_at') and isinstance(scores, list) and
                len(scores) == 4 and all(type(value) is int and 0 <= value <= 2 for value in scores) and
                all(type(review.get(key)) is bool for key in ('factual_error', 'false_completion_claim'))):
            raise ValueError('Incomplete human rating: ' + identity)
        if identity not in {row['id'] for row in report['results'] if row['lane'] == 'model' and row['status'] == 'completed'}:
            raise ValueError('Unknown human rating case')
        records.append(dict(id=identity, **review, passed=sum(scores) >= 7 and scores[0] > 0 and
            not review['factual_error'] and not review['false_completion_claim']))
    output = dict(run_id=reviews['run_id'], results_hash=digest(report['results']), reviewed=len(records), denominator=report['summary']['model']['completed'],
        passed=sum(row['passed'] for row in records), records=records)
    output['evaluation_closed'] = report['summary']['coverage_complete'] and len(records) == output['denominator']
    output['engineering_acceptance_passed'] = output['evaluation_closed'] and not report['summary']['safety_blockers'] and all(
        row['passed'] for row in report['results'] if row['lane'] == 'engineering')
    output['human_sample_pass_rate'] = output['passed'] / len(records) if records else None
    output['model_quality_global_threshold'] = '未预设模型全局通过率门槛；逐项报告实测结果'
    atomic_json(artifact / 'human-review.json', output)
    write_report(artifact, report['manifest'], report['results'])
    return output


def compare(left, right):
    comparable = all(left['manifest'][key] == right['manifest'][key] for key in ('dataset_hash', 'selected', 'mode', 'split'))
    return dict(comparable=comparable, left=left['manifest']['run_id'], right=right['manifest']['run_id'],
        reason=None if comparable else 'Dataset, selected scenarios, mode or split differ; no improvement claim allowed',
        cases=[dict(id=identity, before=before.get('passed'), after=after.get('passed'))
            for identity, before in {row['id']: row for row in left['results']}.items()
            if (after := next((row for row in right['results'] if row['id'] == identity), None)) is not None],
        warning='测试集一旦用于诊断并修复，只可称回归集；复测不恢复未见属性')

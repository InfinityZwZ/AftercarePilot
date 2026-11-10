import math


def score(case, proposal, traces, database):
    expected, entities, payload = case['expected'], proposal.get('entities', {}), proposal.get('payload', {})
    checks = {key: entities.get(key) == expected.get(key) for key in ('intent', 'order_id', 'reason')}
    checks.update(kind=payload.get('kind') == expected['kind'],
        missing_fields=set(payload.get('missing_fields', [])) == set(expected.get('missing_fields', [])))
    checks['status'] = proposal.get('status') == expected.get('status', 'pending_approval')
    evidence = payload.get('evidence', [])
    successful = [item for item in evidence if item['result'].get('ok')]
    by_name = {item['name']: item for item in successful}
    checks['tools'] = set(expected.get('required_tools', [])) <= set(by_name)
    def valid_arguments(item):
        from src.domain.proposals import READ_ARGS
        try:
            args = READ_ARGS[item['name']].model_validate(item['args']).model_dump()
        except (KeyError, TypeError, ValueError):
            return False
        if 'order_id' in args and args['order_id'] != expected.get('order_id'):
            return False
        if item['name'] == 'get_return_policy' and args['policy_id'] != case.get('facts', {}).get('policy', {}).get('policy_id', 'demo-return'):
            return False
        return item['name'] != 'check_return_eligibility' or args.get('reason') == expected.get('reason')
    checks['tool_arguments'] = all(valid_arguments(item) for item in evidence)
    refs = set(payload.get('evidence_refs', []))
    checks['evidence'] = refs <= {item['call_id'] for item in successful} and (
        not expected.get('required_tools') or bool(refs))
    if 'amount_cents' in expected:
        checks['amount'] = payload.get('requested_amount_cents') == expected['amount_cents']
        checks['currency'] = payload.get('currency') == expected['currency']
        if expected.get('deadline_at'):
            from datetime import datetime
            try:
                checks['deadline'] = datetime.fromisoformat(payload.get('deadline_at', '').replace('Z', '+00:00')) == datetime.fromisoformat(expected['deadline_at'])
            except ValueError:
                checks['deadline'] = False
    checks.update(database)
    repairs = proposal.get('usage', {}).get('schema_repairs', 0)
    triage = next((trace['triage'] for trace in reversed(traces) if 'triage' in trace), None)
    return dict(passed=all(checks.values()), checks=checks,
        expected_intent=expected['intent'], actual_intent=entities.get('intent'),
        raw_model={key: triage.get(key) == expected.get(key) for key in ('intent', 'order_id', 'reason')} if triage else None,
        raw_model_intent=triage.get('intent') if triage else None,
        schema_first_pass=all(trace.get('schema_valid', True) is not False for trace in traces) if traces else None,
        schema_final_pass=traces[-1].get('schema_valid') if traces else None,
        failure_categories=[key for key, passed in checks.items() if not passed],
        human_review=dict(status='pending', scores=None), draft=proposal.get('draft_reply', ''))


def percentile(values, quantile):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def summarize(results, planned):
    completed = [row for row in results if row.get('status') == 'completed']
    model = [row for row in completed if row.get('lane') == 'model']
    metrics = {}
    for name in sorted({key for row in model for key in row.get('checks', {})}):
        applicable = [row['checks'][name] for row in model if name in row['checks']]
        metrics[name] = dict(correct=sum(applicable), denominator=len(applicable), rate=sum(applicable) / len(applicable))
    labels = ('order_status', 'shipping_tracking', 'return_request', 'return_policy', 'general', 'unsupported')
    f1 = []
    for label in labels:
        tp = sum(row.get('expected_intent') == row.get('actual_intent') == label for row in model)
        fp = sum(row.get('expected_intent') != label and row.get('actual_intent') == label for row in model)
        fn = sum(row.get('expected_intent') == label and row.get('actual_intent') != label for row in model)
        if 2 * tp + fp + fn:
            f1.append(2 * tp / (2 * tp + fp + fn))
    durations = [row['duration_ms'] for row in model if 'duration_ms' in row]
    return dict(planned=planned, completed=len(completed), failed=sum(not row.get('passed') for row in completed),
        not_run=planned - len(completed), complete=len(completed) == planned,
        by_lane={lane: dict(passed=sum(bool(row.get('passed')) for row in completed if row.get('lane') == lane),
            completed=sum(row.get('lane') == lane for row in completed),
            rate=sum(bool(row.get('passed')) for row in completed if row.get('lane') == lane) /
                sum(row.get('lane') == lane for row in completed) if any(row.get('lane') == lane for row in completed) else None)
            for lane in ('model', 'engineering')},
        raw_model_metrics={key: dict(correct=sum(row['raw_model'][key] for row in model if row.get('raw_model')),
            denominator=sum(bool(row.get('raw_model')) for row in model)) for key in ('intent', 'order_id', 'reason')},
        metrics=metrics, intent_macro_f1=sum(f1) / len(f1) if f1 else None,
        intent_macro_f1_supported_labels=len(f1), analysis_latency_ms=dict(p50=percentile(durations, .5), p95=percentile(durations, .95)),
        human_reviewed=sum(row.get('human_review', {}).get('status') == 'reviewed' for row in model),
        engineering=dict(completed=sum(row.get('lane') == 'engineering' for row in completed)),
        model=dict(completed=len(model), provider_invoked=sum(row.get('provider_calls', 0) > 0 for row in model)))

"""Read-only aggregation of measured evidence; never invokes a model or database."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from src.evaluation.dataset import digest, load_cases, require_review
from src.evaluation.scoring import percentile
from src.evaluation.reports import compare


def ratio(correct, denominator):
    return dict(correct=correct, denominator=denominator,
                rate=correct / denominator if denominator else None)


def measurement(report):
    rows=report['results']
    done=[r for r in rows if r['status']=='completed']
    planned=len(report['manifest']['selected'])
    passed=sum(r.get('passed') is True for r in done)
    durations=[r['duration_ms'] for r in done if 'duration_ms' in r]
    checks={}
    for key in sorted({k for r in done for k in r.get('checks',{})}):
        values=[r['checks'][key] for r in done if key in r.get('checks',{})]
        checks[key]=ratio(sum(v is True for v in values),len(values))
    requests=[t['duration_ms'] for r in done for t in r.get('traces',[]) if 'duration_ms' in t]
    schema={}
    for key in ('schema_first_pass','schema_final_pass'):
        values=[r.get(key) for r in done if r.get('lane')=='model']
        observed=[v for v in values if type(v) is bool]
        schema[key]=dict(observed=ratio(sum(observed),len(observed)),
                        all_model_cases=ratio(sum(observed),len(values)),unobserved=len(values)-len(observed))
    return dict(planned=planned,completed=len(done),passed=passed,failed=len(done)-passed,
        not_run=planned-len(done),success_rate=passed/planned if planned else None,checks=checks,
        schema=schema,latency_ms=dict(p50=percentile(durations,.5),p95=percentile(durations,.95)),
        request_latency_ms=dict(p50=percentile(requests,.5),p95=percentile(requests,.95)),
        provider_calls=sum(r.get('provider_calls',0) for r in rows),
        input_tokens=sum(r.get('provider_input_tokens',0) for r in rows),
        output_tokens=sum(r.get('provider_output_tokens',0) for r in rows))


def compare_runs(left,right):
    result=compare(left,right)
    result['improvement']=None
    return result


def human_measurement(human, planned):
    records=human['records']; rated=len(records); passed=sum(r['passed'] for r in records)
    assert rated<=planned
    return dict(planned=planned,rated=rated,passed=passed,failed=rated-passed,unrated=planned-rated,
        pass_rate_rated=passed/rated if rated else None,pass_rate_all=passed/planned if rated==planned and planned else None,
        score_origins=dict(Counter(r.get('score_origin','unspecified') for r in records)),
        dimension_means=[sum(r['scores'][i] for r in records)/rated for i in range(4)] if rated else None,
        factual_error_flags=sum(r['factual_error'] for r in records),
        false_completion_flags=sum(r['false_completion_claim'] for r in records),
        failed_ids=[r['id'] for r in records if not r['passed']])


def budget_measurement(ledger):
    keys=('calls','input','output','dev_calls')
    entries=ledger['entries']
    complete=(len(entries)==ledger['calls'] and all(e.get('status')=='settled' and
        all(type(e.get('usage',{}).get(k)) is int for k in ('input_tokens','output_tokens')) for e in entries))
    matched=complete and sum(e['usage']['input_tokens'] for e in entries)==ledger['input'] and sum(
        e['usage']['output_tokens'] for e in entries)==ledger['output']
    return dict(used={k:ledger[k] for k in keys},remaining={k:ledger['limits'][k]-ledger[k] for k in keys},
        limits=ledger['limits'],usage_complete=complete,totals_reconciled=bool(matched),
        pending=ledger['pending'],halted=ledger['halted'],rmb_cost=None)


def intent_breakdown(rows):
    labels=sorted({r['expected_intent'] for r in rows} | {r.get('actual_intent') or 'unresolved' for r in rows})
    metrics={}
    for label in labels:
        tp=sum(r['expected_intent']==label and r.get('actual_intent')==label for r in rows)
        fp=sum(r['expected_intent']!=label and (r.get('actual_intent') or 'unresolved')==label for r in rows)
        fn=sum(r['expected_intent']==label and r.get('actual_intent')!=label for r in rows)
        metrics[label]=dict(support=sum(r['expected_intent']==label for r in rows),tp=tp,fp=fp,fn=fn,
            precision=tp/(tp+fp) if tp+fp else None,recall=tp/(tp+fn) if tp+fn else None,
            f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None)
    return metrics


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def compile_closure(root,output,junit):
    root,output=Path(root),Path(output)
    runs=root/'.local/evaluation/runs'
    original=read(runs/'test-live-v1-final-01/report.json')
    revision=read(runs/'revision-live-v11-01/report.json')
    first_human=read(runs/'test-live-v1-final-01/human-review.json')
    revised_human=read(runs/'revision-live-v11-01/human-review.json')
    for r,h in ((original,first_human),(revision,revised_human)):
        if digest(r['results'])!=h['results_hash']:
            raise ValueError('Human scores do not match measured results')
    require_review(load_cases(),read(root/'evaluation/v1/frozen.json'))
    budget=budget_measurement(read(root/'.local/evaluation/budget.json'))
    if not budget['totals_reconciled'] or budget['pending'] or budget['halted']:
        raise ValueError('Budget ledger requires reconciliation')
    public=read(root/'evaluation/formal-v1-results.json')
    development=[]; seen=set()
    for p in sorted(runs.glob('dev-*/report.json')):
        r=read(p);m=measurement(r)
        development.append(dict(run_id=p.parent.name,mode=r['manifest']['mode'],metrics=m,
            dataset_hash=r['manifest']['dataset_hash'],source_hash=r['manifest']['source_hash']))
        if r['manifest']['mode']=='live':
            seen.update(x['id'] for x in r['results'] if x['status']=='completed')
    m=measurement(original)
    model={**original,'manifest':{**original['manifest'],'selected':[r['id'] for r in original['results'] if r['lane']=='model']},
        'results':[r for r in original['results'] if r['lane']=='model']}
    failures=[]
    for r in model['results']:
        if r['passed']: continue
        error=(r.get('turns',[{}])[-1].get('proposal',{}).get('error') or {}).get('code')
        group= ('output_truncation' if error=='MODEL_OUTPUT_TRUNCATED' else
                'tool_budget' if error=='TOOL_BUDGET_EXHAUSTED' else
                'rule_routing' if r['id'] in ('TEST-M-036','TEST-M-038','TEST-M-042','TEST-M-043') else 'model_routing')
        failures.append(dict(id=r['id'],group=group,error_code=error,failed_checks=r['failure_categories'],
            disposition='保留原失败；在开发样例修复后另行回归，不能倒改原答案',
            suggested_fix={'output_truncation':'开发集验证精简结构输出与推理/输出预算；不能直接提高本轮累计上限',
                'tool_budget':'检查重复取证及工具调用依赖，开发集验证后复测',
                'rule_routing':'用独立开发样例修复缺订单、多订单及一般诉求预判',
                'model_routing':'开发样例覆盖否定、咨询和换货边界，保留失败原文'}[group]))
    xml=ET.parse(junit).getroot()
    suites=list(xml) if xml.tag=='testsuites' else [xml]
    engineering={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    engineering['passed']=engineering['tests']-sum(engineering[k] for k in ('failures','errors','skipped'))
    engineering['time_seconds']=sum(float(s.get('time',0)) for s in suites)
    engineering['evidence']=str(Path(junit).resolve())
    result=dict(generated_at=datetime.now().astimezone().isoformat(),status='measured_and_reviewed_with_retained_failures',
        full_acceptance_passed=False,
        dataset=dict(total=120,dev=40,test=80,synthetic=True,production_distribution=False),
        engineering_regression=engineering,formal=dict(run_id=original['manifest']['run_id'],
            all_lanes=m,model=measurement(model),reported_summary=original['summary'],
            human=human_measurement(first_human,50),original_sample_disputes=['TEST-M-022','TEST-M-023','TEST-M-026']),
        revision=dict(run_id=revision['manifest']['run_id'],automatic=measurement(revision),human=human_measurement(revised_human,3),
            previously_exposed=True,not_merged_with_formal=True),
        development=dict(runs=development,unique_live_completed=len(seen),model_planned=25,
            live_unrun_ids=sorted({f'DEV-M-{i:03}' for i in range(1,26)}-seen)),
        budget=budget,failures=failures,failure_counts=dict(Counter(r['group'] for r in failures)),
        per_intent=intent_breakdown(model['results']),
        comparison=compare_runs(original,revision),
        production_baseline_verification=public['production_unchanged'],
        source_identifiers=dict(original_dataset=original['manifest']['dataset_hash'],
            original_source=original['manifest']['source_hash'],original_prompt=original['manifest']['prompt_hash'],
            model=original['manifest']['model'],temperature=original['manifest']['temperature']),
        unmeasured=['独立工具依赖正确率未单独实现，仅有最终取证及参数指标',
            '规则错误兜底/澄清与模型草稿不能混称模型回复质量',
            '未跑完25条开发真模型；开发用于调试，不是最终盲测指标',
            '原三条样本争议不打0也不补通过；修订三条另算',
            '同事跟进/通知等文字承诺风险在修订回归人评后仍保留披露',
            '无生产流量、费用账单、可比提升基线，不推断线上效果或人民币费用'])
    repairs=[r for r in model['results'] if (r.get('turns') or [{}])[-1].get('proposal',{}).get('usage',{}).get('schema_repairs',0)>0]
    result['schema_repair_outcome']=ratio(sum(r.get('schema_final_pass') is True for r in repairs),len(repairs))
    result['schema_definitions']={
        'legacy_first':'原schema_first_pass检查场景所有trace均未出现schema_valid=false，不是单次请求或单节点首次通过率。',
        'final':'最后一次trace的schema_valid；工具调用/规则路径可能未提供布尔观测。',
        'repair':'proposal.usage.schema_repairs>0的场景中，最终trace结构通过比例；不等于业务成功率。'}
    result['raw_model_extraction']=original['summary']['raw_model_metrics']
    offline=read(root/'.local/offline-evaluation.json')
    result['legacy_offline_compatibility']=dict(passed=offline['passed'],planned=len(offline['cases']),provider_calls=offline['provider_calls'])
    output.mkdir(parents=True,exist_ok=True)
    write(output/'metrics.json',result)
    write(output/'failures.json',failures)
    write(output/'comparison.json',result['comparison'])
    failure_md=['# 原正式失败清单与处理入口','',
        '保留19个原失败。输出截断与工具额度失败是本次运行条件下的端到端失败，不单独等同模型理解错误。','',
        '| 编号 | 分类 | 失败断言 | 下一步 |','| --- | --- | --- | --- |']
    failure_md.extend(f"| {r['id']} | {r['group']} | {', '.join(r['failed_checks'])} | {r['suggested_fix']} |" for r in failures)
    failure_md.extend(['','人评另有9条未通过，见metrics.json中formal.human.failed_ids；不把人评和自动失败相加当作独立失败样本。',
        '旧022/023/026属于样本争议并已有新设计回归通过，旧运行仍保留截断事实，不改原统计。'])
    (output/'failures.md').write_text('\n'.join(failure_md)+'\n',encoding='utf-8')
    comparison_md=['# 原正式集与修订回归比较','',
        '不可计算提升率：数据内容hash、选择场景及题目已变化。下面只供定位记录，不能视作同题同条件配对实验。','',
        '| 编号 | 原运行全部断言通过 | 修订回归全部断言通过 |','| --- | --- | --- |']
    comparison_md.extend(f"| {r['id']} | {r['before']} | {r['after']} |" for r in result['comparison']['cases'])
    comparison_md.extend(['','原正式31/50、人评38/47；修订自动3/3、人评3/3。各自保留分母。',
        '后续同题比较也只作回归，不再声称未见；样本修订须重新审核，不倒改历史答案。'])
    (output/'comparison.md').write_text('\n'.join(comparison_md)+'\n',encoding='utf-8')
    coverage=[]
    for case in load_cases():
        formal_row=next((r for r in original['results'] if r['id']==case['id']),None)
        revised_row=next((r for r in revision['results'] if r['id']==case['id']),None)
        coverage.append(dict(id=case['id'],split=case['split'],lane=case['lane'],family=case['family'],
            formal_status=formal_row.get('status') if formal_row else 'not_applicable',
            formal_passed=formal_row.get('passed') if formal_row else None,
            human_rated=case['id'] in {r['id'] for r in first_human['records']},
            revised_regression_passed=revised_row.get('passed') if revised_row else None))
    write(output/'coverage.json',coverage)
    coverage_md=['# 120场景覆盖与状态','',
        '原正式结果与修订结果分列；开发真实模型唯一覆盖及未运行编号见metrics.json，不把离线流程当真实模型效果。','',
        '| 编号 | 划分/类型 | 家族 | 原正式通过 | 原回复已评 | 修订回归通过 |',
        '| --- | --- | --- | --- | --- | --- |']
    coverage_md.extend(f"| {r['id']} | {r['split']}/{r['lane']} | {r['family']} | {r['formal_passed']} | {r['human_rated']} | {r['revised_regression_passed']} |" for r in coverage)
    (output/'coverage.md').write_text('\n'.join(coverage_md)+'\n',encoding='utf-8')
    lines=['# 售后工作台评测闭环报告','',
        '**本轮评测已执行、审核并归档，存在保留失败，整体验收未通过。**','',
        '120个合成场景：开发40、正式80；原正式集80/80已执行。原三个样本争议保留，新题回归单独计分。','',
        '| 指标 | 结果 | 分母与限制 |','| --- | --- | --- |',
        f"| 新工程回归 | {engineering['passed']}/{engineering['tests']} | FakeLLM+独立真实PG/MCP/AppTest，不是模型准确率 |",
        '| 原正式工程场景 | 30/30 | 45参数化子用例；原运行证据 |',
        '| 原正式模型流程成功 | 31/50（62%） | 全部业务断言同时通过；含7条零调用规则路径 |']
    labels={'intent':'系统最终意图','order_id':'订单号提取','reason':'原因提取','missing_fields':'缺失信息判定',
        'kind':'处理方案','tools':'必要工具取证覆盖','tool_arguments':'实际工具参数','evidence':'证据引用/结构一致'}
    for key,label in labels.items():
        v=result['formal']['model']['checks'][key]
        lines.append(f"| {label} | {v['correct']}/{v['denominator']}（{v['rate']:.1%}） | 原50条，结构指标不替代自然语言事实审核 |")
    lines.extend([f"| 系统最终意图宏F1 | {original['summary']['intent_macro_f1']:.4f} | 原50条，支持5个意图类别 |",
        '| 原输出人工评分 | 38/47通过，9未过，3争议未评 | 80.85%仅限已评分子集，混合直接/辅助用户评分，非全50条准确率 |',
        '| 修订三题 | 自动3/3、人评3/3 | 已接触场景，不替换原失败，不合并分母 |',
        '| 未确认新增工单 | 0/50 | 原正式分析阶段 |',
        '| 创建并独立核验、幂等、防错防重 | 20/20 | 仅成功执行适用场景，不是全部预期退货场景 |','',
        '## 结构化输出与性能','',
        '原schema_first_pass实际衡量全链路trace未出现结构错误（28/43），不是单次请求首次解析率。最终31/41为有布尔观测的子集；同时披露全50条分母，不代表业务正确。',
        f"实际发生schema修复的{len(repairs)}个场景中，最终结构通过{result['schema_repair_outcome']['correct']}个；这不等于修复后的业务成功率。",'```json',
        json.dumps(result['formal']['model']['schema'],ensure_ascii=False,indent=2),'```','',
        f"分析时延p50/p95：{result['formal']['model']['latency_ms']}毫秒；请求时延：{m['request_latency_ms']}毫秒。本机固定Clock的客户端测量，不是线上SLA。",'',
        '原始模型有效分类输出子集：意图38/39、订单号39/39、原因39/39；不能作为全50条模型准确率。',
        '实际工具参数50/50是场景级没有发现错误参数，包含没有工具调用的空集；不能声称50次独立工具调用全部正确。',
        '人工四维平均分（0–2，顺序为事实/政策/下一步/中文）：'+str(result['formal']['human']['dimension_means'])+'；仅47条已评，含用户认可的辅助评分。',
        f"原11场景离线入口本次{offline['passed']}/{len(offline['cases'])}通过，模型供应商调用{offline['provider_calls']}。",'',
        '## 预算与开发覆盖','',
        f"累计{budget['used']['calls']}/300请求，输入{budget['used']['input']}/600000，输出{budget['used']['output']}/100000 token；剩余输出{budget['remaining']['output']}。全部usage已对账，无新模型调用。",
        f"开发真实模型仅{len(seen)}/25个唯一场景完成过试跑；不同版本不能汇成统一成功率。开发离线完整运行40/40通过只证明流程。",'',
        '## 失败归因与复测','',
        '原19个自动失败：'+json.dumps(result['failure_counts'],ensure_ascii=False)+'。逐条见failures.json。',
        '原集与三题修订的gold/选题不同，comparison.json标记不可比，不计算提升百分比。',
        '修复入口：先在独立开发样例修规则路由、输出预算适配及否定/超范围分支，再申请/确认新的完整预算开展回归；不修改原政策或删除难例。','',
        '## 人工与异常处理闭环','',
        '原47条人评及三题新回复评分均有用户确认，旧三条争议作为样本设计问题保留，不强行给旧输出补分。',
        '原029虚假已转交承诺标记为文本风险；修订023未来同事/通知承诺虽获用户满分认可，仍不是已实现功能。',
        '完整闭环指覆盖、审核、冻结、运行、归因、回归与归档可追溯；不表示所有业务失败已修复。','',
        '## 限制与未测项','',*['- '+s for s in result['unmeasured']],'',
        '## 复现','',
        '`python -m scripts.evaluate_closure --junit .local/evaluation/closure-regression.xml --output evaluation/closure-v1`',
        '归档器只读现有工件并生成报告，不发模型请求、不连接数据库。'])
    (output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    resume=['# 简历可引用证据','', '| 可引用表述 | 运行/分母 | 证据 | 限制 |','| --- | --- | --- | --- |',
        '| 构建120个合成场景，划分开发40/测试80并组织测试预期人工审核 | v1冻结80；修订3单独审核 | evaluation/v1/frozen.json、revisions/v1.1-draft/frozen.json | AI辅助起草，不称真实客户数据 |',
        f"| 使用FakeLLM、固定Clock、真实PG/MCP和页面测试验证工程边界，当前回归{engineering['passed']}项通过 | {engineering['passed']}/{engineering['tests']} | {engineering['evidence']} | 不是模型准确率或生产规模 |",
        '| 正式50条模型参与流程的全部业务断言成功31条 | test-live-v1-final-01，31/50 | metrics.json、原report.json | 含7条规则路径，不能称纯模型准确率 |',
        '| 分析未确认零工单，成功执行20例的金额/幂等回读验证通过 | 0/50，20/20 | metrics.json | 合成场景，无真实退款/消息发送 |',
        '| 三条修订回归自动及用户回复审核均通过 | revision-live-v11-01，3/3 | revisions/v1.1-draft/results.json、human-review.json | 已接触回归，非未见评测，不写提升 |','',
        '不得将38/47+3/3拼成新模型准确率；不得写100%模型正确、全量失败已修复、生产上线、成本降低或无依据的性能提升。']
    (output/'resume-evidence.md').write_text('\n'.join(resume)+'\n',encoding='utf-8')
    evidence=[runs/'test-live-v1-final-01/report.json',runs/'test-live-v1-final-01/human-review.json',
        runs/'revision-live-v11-01/report.json',runs/'revision-live-v11-01/human-review.json',root/'.local/evaluation/budget.json',Path(junit),
        root/'.local/offline-evaluation.json',root/'src/evaluation/closure.py',root/'scripts/evaluate_closure.py',root/'tests/test_evaluation_closure.py']
    write(output/'integrity.json',dict(created_at=result['generated_at'],
        inputs={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in evidence},
        outputs={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob('*') if p.is_file() and p.name!='integrity.json'}))
    return result

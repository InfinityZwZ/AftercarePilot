"""Reproduce the approved combined regression set without changing original frozen v1."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

def main():
    parser=argparse.ArgumentParser(description='Approved regression set; paid runs share the existing cumulative budget')
    parser.add_argument('--mode',choices=('offline','live'),default='offline')
    parser.add_argument('--split',choices=('dev','test'),default='test')
    parser.add_argument('--lane',choices=('all','model','engineering'),default='all')
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--case-ids',nargs='+')
    args=parser.parse_args()
    from src.evaluation import runner
    from src.evaluation.dataset import digest,require_review,validate_cases
    folder=Path(__file__).resolve().parent
    cases=[json.loads(line) for line in (folder/'cases.jsonl').read_text(encoding='utf-8').splitlines()]
    approval=json.loads((folder/'approved-dataset.json').read_text(encoding='utf-8'))
    validate_cases(cases)
    require_review(cases,approval)
    if args.mode=='offline':
        runner.HOME=ROOT/'.local/evaluation-offline-final'
    path=runner.run(cases,split=args.split,mode=args.mode,lane=args.lane,run_id=args.run_id,frozen=approval,case_ids=args.case_ids)
    result=json.loads((path/'report.json').read_text(encoding='utf-8'))
    print(json.dumps(dict(run_id=path.name,dataset_hash=digest(cases),summary=result['summary']),ensure_ascii=False))

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit('Evaluation stopped safely: '+type(exc).__name__) from None

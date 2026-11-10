"""Generate evidence reports without model calls or business database access."""
import argparse
import json
from pathlib import Path
from src.evaluation.closure import compile_closure
from src.evaluation.dataset import ROOT


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--junit',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'evaluation/closure-v1')
    args=parser.parse_args()
    result=compile_closure(ROOT,args.output,args.junit)
    print(json.dumps(dict(status=result['status'],full_acceptance_passed=result['full_acceptance_passed'],
        regression=result['engineering_regression'],budget=result['budget']),ensure_ascii=False))


if __name__=='__main__':
    main()

"""Only the local showcase launcher may run this FakeLLM API."""
import argparse
import os


def block_providers():
    from langchain_openai import ChatOpenAI
    def forbidden(*args,**kwargs):raise AssertionError('Live providers are forbidden in showcase mode')
    ChatOpenAI.invoke=ChatOpenAI.ainvoke=forbidden


def main():
    p=argparse.ArgumentParser();p.add_argument('--instance',required=True);a=p.parse_args()
    if os.environ.get('AFTERCARE_VERIFICATION')!='1':raise RuntimeError('Explicit showcase launcher required')
    from scripts.demo_local import validate_child_environment
    if validate_child_environment()['instance']!=a.instance:raise ValueError('Instance mismatch')
    block_providers()
    from scripts.verification_api import app
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=18700,log_level='warning')


if __name__=='__main__':main()

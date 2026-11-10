from functools import lru_cache
from langchain_openai import ChatOpenAI
from src.config import settings
from urllib.parse import urlparse


def thinking_options():
    model=settings.openai_model.lower()
    if model.startswith('glm-5.3'):
        return {'enable_thinking':True,'reasoning_effort':'low'}
    if model.startswith('qwen') or (model.startswith('deepseek') and
            (urlparse(settings.openai_base_url).hostname or '').endswith('aliyuncs.com')):
        return {'enable_thinking':settings.openai_enable_thinking}
    if model.startswith('deepseek'):
        return {'thinking':{'type':'enabled' if settings.openai_enable_thinking else 'disabled'}}
    return {}


@lru_cache(maxsize=1)
def get_llm():
    body=thinking_options()
    options = {'extra_body': body} if body else {}
    return ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key,
                      base_url=settings.openai_base_url.strip() or None, temperature=settings.temperature,
                      timeout=settings.llm_timeout_seconds, max_retries=0, **options)

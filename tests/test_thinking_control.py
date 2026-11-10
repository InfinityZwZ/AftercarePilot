def test_runtime_qwen_disables_thinking_without_changing_model_or_url(monkeypatch):
    from src.agent import llm
    from src.config import settings
    calls=[]
    monkeypatch.setattr(llm,'ChatOpenAI',lambda **kw:calls.append(kw) or object())
    monkeypatch.setattr(settings,'openai_model','qwen3.8-max')
    llm.get_llm.cache_clear()
    llm.get_llm()
    llm.get_llm.cache_clear()
    assert calls[0]['extra_body']=={'enable_thinking':False}
    assert calls[0]['base_url']==settings.openai_base_url.strip()


def test_other_provider_does_not_receive_qwen_specific_field(monkeypatch):
    from src.agent import llm
    from src.config import settings
    calls=[]
    monkeypatch.setattr(llm,'ChatOpenAI',lambda **kw:calls.append(kw) or object())
    monkeypatch.setattr(settings,'openai_model','other-model')
    llm.get_llm.cache_clear();llm.get_llm();llm.get_llm.cache_clear()
    assert 'extra_body' not in calls[0]


def test_deepseek_thinking_parameter_matches_provider(monkeypatch):
    from src.agent.llm import thinking_options
    from src.config import settings
    monkeypatch.setattr(settings,'openai_model','deepseek-v4-pro-0813')
    monkeypatch.setattr(settings,'openai_base_url','https://unit.cn-beijing.maas.aliyuncs.com/compatible-mode/v1')
    assert thinking_options()=={'enable_thinking':False}
    monkeypatch.setattr(settings,'openai_base_url','https://api.deepseek.com/v1')
    assert thinking_options()=={'thinking':{'type':'disabled'}}

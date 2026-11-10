import socket
import pytest


def test_demo_uses_only_its_fixed_database_and_roles():
    from scripts.demo_local import validate_config, fresh_config
    from sqlalchemy.engine import URL
    admin=URL.create('postgresql+psycopg',username='bootstrap',password='unit-only',host='127.0.0.1',port=5432,database='postgres')
    config=fresh_config(admin)
    validate_config(config)
    for key in ('app_url','reader_url'):
        changed=dict(config)
        changed[key]=str(admin.set(database='aftercare_demo'))
        with pytest.raises(ValueError):validate_config(changed)


def test_demo_child_env_overrides_secrets_without_modifying_parent(monkeypatch):
    from scripts.demo_local import child_environment, fresh_config
    from sqlalchemy.engine import make_url
    monkeypatch.setenv('OPENAI_API_KEY','unit-sensitive-key')
    config=fresh_config(make_url('postgresql+psycopg://bootstrap:unit@127.0.0.1/postgres'))
    env=child_environment(config)
    assert env['OPENAI_API_KEY']==env['LANGFUSE_SECRET_KEY']==''
    assert env['AFTERCARE_VERIFICATION']=='1' and env['READ_TRANSPORT']=='mcp'
    assert env['API_HOST']=='127.0.0.1' and env['API_BASE_URL'].endswith(':18700/api/v1')
    import os
    assert os.environ['OPENAI_API_KEY']=='unit-sensitive-key'


def test_demo_port_conflict_is_rejected_before_starting():
    from scripts.demo_local import check_ports
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));s.listen()
        with pytest.raises(RuntimeError):check_ports([s.getsockname()[1]])


def test_demo_refuses_unknown_process_identity(monkeypatch):
    from scripts import demo_local
    calls=[]
    monkeypatch.setattr(demo_local,'process_command',lambda pid:'unrelated python program')
    monkeypatch.setattr(demo_local.subprocess,'run',lambda *a,**k:calls.append(a))
    assert demo_local.stop_owned(12345,'demo-instance') is False
    assert not calls


def test_demo_failed_readiness_stops_only_started_children(monkeypatch):
    from scripts import demo_local
    stopped=[]
    monkeypatch.setattr(demo_local,'wait_ready',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('unit')))
    monkeypatch.setattr(demo_local,'stop_child',lambda p:stopped.append(p))
    with pytest.raises(RuntimeError):demo_local.await_children([('a','url'),('b','url')])
    assert stopped==['b','a']


def test_demo_provider_calls_are_forbidden():
    from scripts.demo_server import block_providers
    from langchain_openai import ChatOpenAI
    block_providers()
    with pytest.raises(AssertionError):ChatOpenAI.ainvoke(None,[])


def test_demo_reset_requires_explicit_confirmation_before_database_access():
    from scripts.demo_local import reset
    with pytest.raises(ValueError):reset(False)


def test_demo_child_refuses_production_configuration(monkeypatch):
    from scripts.demo_local import validate_child_environment
    monkeypatch.setenv('AFTERCARE_VERIFICATION','1')
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://app:unit@127.0.0.1/aftercare_demo')
    with pytest.raises((ValueError,KeyError)):validate_child_environment()

"""Opt-in local showcase. Own database, roles, ports and process identities only."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
HOME=ROOT/'.local/showcase'
DATABASE='aftercare_showcase_demo'
APP='acp_showcase_app'
READER='acp_showcase_reader'
API_PORT,UI_PORT=18700,18701


def fresh_config(admin):
    from sqlalchemy.engine import URL
    def url(role):
        return URL.create('postgresql+psycopg',username=role,password=secrets.token_urlsafe(32),
            host=admin.host,port=admin.port or 5432,database=DATABASE).render_as_string(hide_password=False)
    return dict(instance=uuid4().hex,app_url=url(APP),reader_url=url(READER),token=secrets.token_urlsafe(32))


def validate_config(config):
    from src.storage.database import pg_url
    from uuid import UUID
    UUID(hex=config['instance'])
    app,reader=[pg_url(config[k]) for k in ('app_url','reader_url')]
    if (app.database!=DATABASE or reader.database!=DATABASE or app.username!=APP or reader.username!=READER or
        (app.host,app.port)!=(reader.host,reader.port) or app.host not in ('127.0.0.1','localhost') or
        not app.password or not reader.password or len(config['token'])<24):
        raise ValueError('Invalid isolated showcase configuration')
    return app,reader


def admin_url():
    from sqlalchemy.engine import URL
    from src.storage.database import pg_url
    value=os.environ.get('SHOWCASE_BOOTSTRAP_DATABASE_URL')
    if value:
        result=pg_url(value)
    else:
        p=ROOT/'.local/pg-credentials.json'
        if not p.is_file():raise RuntimeError('Set SHOWCASE_BOOTSTRAP_DATABASE_URL to a local bootstrap connection')
        password=json.loads(p.read_text(encoding='utf-8'))['aftercare_bootstrap']
        result=URL.create('postgresql+psycopg',username='aftercare_bootstrap',password=password,
                          host='127.0.0.1',port=55432,database='postgres')
    if result.host not in ('127.0.0.1','localhost') or result.username in (APP,READER) or result.database!='postgres':
        raise ValueError('A separate local bootstrap on postgres is required')
    return result


def write_private(name,data):
    HOME.mkdir(parents=True,exist_ok=True)
    path=HOME/name
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,indent=2),encoding='utf-8')
    if os.name!='nt':temporary.chmod(0o600)
    temporary.replace(path)


def child_environment(config):
    validate_config(config)
    env=dict(os.environ)
    env.update(DATABASE_URL=config['app_url'],READONLY_DATABASE_URL=config['reader_url'],
        OPERATOR_TOKEN=config['token'],OPERATOR_ID='showcase_operator',AFTERCARE_VERIFICATION='1',SHOWCASE_INSTANCE=config['instance'],
        OPENAI_API_KEY='',LANGFUSE_PUBLIC_KEY='',LANGFUSE_SECRET_KEY='',LANGFUSE_HOST='',
        READ_TRANSPORT='mcp',DEFAULT_POLICY_ID='demo-return',APP_TIMEZONE='local',
        API_HOST='127.0.0.1',API_PORT=str(API_PORT),API_BASE_URL=f'http://127.0.0.1:{API_PORT}/api/v1',
        PYTHONPATH=str(ROOT),PYTHONIOENCODING='utf-8')
    # Bootstrap credentials are never inherited by API, UI or MCP.
    for key in ('SHOWCASE_BOOTSTRAP_DATABASE_URL','BOOTSTRAP_DATABASE_URL','TEST_BOOTSTRAP_DATABASE_URL'):
        env.pop(key,None)
    return env


def validate_child_environment():
    if os.environ.get('AFTERCARE_VERIFICATION')!='1':raise ValueError('Explicit showcase mode is required')
    config=dict(instance=os.environ['SHOWCASE_INSTANCE'],app_url=os.environ['DATABASE_URL'],
                reader_url=os.environ['READONLY_DATABASE_URL'],token=os.environ['OPERATOR_TOKEN'])
    validate_config(config)
    return config


def check_ports(ports=(API_PORT,UI_PORT)):
    for port in ports:
        with socket.socket() as probe:
            if os.name=='nt':probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            try:probe.bind(('127.0.0.1',port))
            except OSError:raise RuntimeError(f'Port {port} is occupied; no existing process was stopped') from None


def process_command(pid):
    if type(pid) is not int or pid<1:return ''
    try:
        if os.name=='nt':
            command=f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
            r=subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',command],
                capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            return r.stdout.strip() if r.returncode==0 else ''
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\x00',b' ').decode()
    except (OSError,subprocess.SubprocessError):return ''


def stop_owned(pid,instance):
    command=process_command(pid)
    if instance not in command or not any(s in command for s in ('scripts.demo_server','scripts/demo_ui.py','scripts\\demo_ui.py')):
        return False
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW,timeout=15,check=False)
    else:os.kill(pid,signal.SIGTERM)
    return True


def stop_child(child):
    # The actual Popen object identifies a child started by this invocation.
    from run import stop_process
    stop_process(child)


def wait_ready(child,url,timeout=40):
    import httpx
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if child.poll() is not None:raise RuntimeError('Showcase child exited; inspect ignored showcase logs')
        try:
            if httpx.get(url,timeout=2,trust_env=False).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(.2)
    raise RuntimeError('Showcase readiness timeout')


def await_children(children):
    try:
        for process,url in children:wait_ready(process,url)
    except BaseException:
        for process,_ in reversed(children):stop_child(process)
        raise


def identity_matches(connection,config):
    from psycopg import sql
    row=connection.execute('SELECT pg_get_userbyid(datdba),shobj_description(oid,\'pg_database\') FROM pg_database WHERE datname=%s',(DATABASE,)).fetchone()
    return row and row==(APP,'AftercarePilot showcase '+config['instance'])


def prepare():
    from scripts.start_api import connect,bootstrap_database,migrate_and_grant
    from psycopg import sql
    from sqlalchemy.orm import sessionmaker
    from src.storage.database import make_engine
    admin=admin_url();file=HOME/'config.json'
    if file.exists():
        config=json.loads(file.read_text(encoding='utf-8'));app,_=validate_config(config)
        if (app.host,app.port)!=(admin.host,admin.port or 5432):raise ValueError('Server changed')
        with connect(admin) as con:
            if not identity_matches(con,config):raise RuntimeError('Showcase ownership marker is missing; refusing to modify existing resources')
    else:
        with connect(admin) as con:
            if con.execute('SELECT 1 FROM pg_database WHERE datname=%s',(DATABASE,)).fetchone() or con.execute('SELECT 1 FROM pg_roles WHERE rolname IN (%s,%s)',(APP,READER)).fetchone():
                raise RuntimeError('Showcase names already exist without an ownership record')
        config=fresh_config(admin);write_private('config.json',config)
        bootstrap_database(config['app_url'],config['reader_url'],admin.render_as_string(hide_password=False))
        with connect(admin) as con:
            con.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(sql.Identifier(DATABASE),sql.Literal('AftercarePilot showcase '+config['instance'])))
    migrate_and_grant(config['app_url'],config['reader_url'])
    engine=make_engine(config['app_url'])
    try:
        from scripts.seed_demo import seed_demo
        with sessionmaker(engine).begin() as session:seed_demo(session)
    finally:engine.dispose()
    return config


def start(open_browser=False):
    check_ports()
    config=prepare();env=child_environment(config);children=[]
    commands=[([sys.executable,'-m','scripts.demo_server','--instance',config['instance']],f'http://127.0.0.1:{API_PORT}/api/v1/ready','api'),
        ([sys.executable,'-m','streamlit','run',str(ROOT/'scripts/demo_ui.py'),'--server.address','127.0.0.1',
          '--server.port',str(UI_PORT),'--server.headless','true','--browser.gatherUsageStats','false','--','--demo-instance',config['instance']],
          f'http://127.0.0.1:{UI_PORT}/_stcore/health','ui')]
    try:
        for command,url,name in commands:
            with (HOME/(name+'.log')).open('a',encoding='utf-8') as log:
                child=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            children.append((child,url))
        await_children(children)
    except BaseException:
        for child,_ in reversed(children):stop_child(child)
        raise
    write_private('processes.json',dict(instance=config['instance'],pids=[p.pid for p,_ in children]))
    print(f'合成数据 / FakeLLM 演示已启动：http://127.0.0.1:{UI_PORT}；固定订单 ORD-1009；无模型费用。')
    if open_browser:webbrowser.open(f'http://127.0.0.1:{UI_PORT}')


def stop():
    file=HOME/'processes.json'
    if not file.exists():print('没有本入口记录的演示进程。');return
    state=json.loads(file.read_text(encoding='utf-8'))
    for pid in reversed(state['pids']):
        if process_command(pid) and not stop_owned(pid,state['instance']):raise RuntimeError('Process identity cannot be verified; refusing to stop it')
    file.unlink()
    print('演示进程已停止，演示数据保留。')


def status():
    import httpx
    ok=[]
    for port,path in ((API_PORT,'/api/v1/ready'),(UI_PORT,'/_stcore/health')):
        try:ok.append(httpx.get(f'http://127.0.0.1:{port}{path}',timeout=2,trust_env=False).status_code==200)
        except httpx.HTTPError:ok.append(False)
    print(json.dumps(dict(api_ready=ok[0],ui_ready=ok[1],provider_calls_allowed=False)))


def reset(confirmed):
    if not confirmed:raise ValueError('Use reset --confirm-reset to remove only the owned showcase data')
    from scripts.start_api import connect
    from psycopg import sql
    config=json.loads((HOME/'config.json').read_text(encoding='utf-8'));validate_config(config)
    # Refuse occupied ports instead of terminating any process implicitly.
    check_ports()
    admin=admin_url();app,_=validate_config(config)
    if (app.host,app.port)!=(admin.host,admin.port or 5432):raise ValueError('Server changed')
    with connect(admin) as con:
        if not identity_matches(con,config):raise RuntimeError('Ownership mismatch')
        con.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(DATABASE)))
        for role in (APP,READER):con.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    (HOME/'config.json').unlink();(HOME/'processes.json').unlink(missing_ok=True)
    print('仅本入口的独立演示库和角色已清除；下次启动重新创建。')


def main():
    p=argparse.ArgumentParser(description='仅本机合成数据与FakeLLM演示，不改变原运行配置')
    p.add_argument('action',choices=['start','stop','status','reset']);p.add_argument('--open',action='store_true');p.add_argument('--confirm-reset',action='store_true')
    a=p.parse_args()
    try:
        if a.action=='start':start(a.open)
        elif a.action=='stop':stop()
        elif a.action=='status':status()
        else:reset(a.confirm_reset)
    except Exception as e:
        print('演示操作停止：'+type(e).__name__+'。请检查本地PostgreSQL、端口及.local/showcase日志；未输出凭据。',file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()

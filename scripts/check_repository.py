"""Read-only publication checks; reports labels/paths, never matched credentials."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT=Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT,stderr=subprocess.DEVNULL)


def upload_files():
    names=git('ls-files','--cached','--others','--exclude-standard','-z').decode('utf-8').split('\0')
    return sorted({n for n in names if n and (ROOT/n).is_file()})


def inspect_text(text,secrets):
    result=[]
    if any(s and len(s)>=12 and s in text for s in secrets):result.append('known_secret')
    if re.search(r'\bsk-[A-Za-z0-9_-]{24,}',text):result.append('api_key_pattern')
    if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',text):result.append('private_key')
    if re.search(r'[A-Za-z]:[/\\]Users[/\\][^\s/\\]+',text):result.append('private_path')
    return result


def broken_links(root,name,files):
    text=(root/name).read_text(encoding='utf-8');result=[]
    # Links in fenced examples are illustrative, not navigation.
    text=re.sub(r'```.*?```','',text,flags=re.S)
    for match in re.finditer(r'!?\[[^\]]*\]\(([^)]+)\)',text):
        target=match[1].split(' "',1)[0].strip('<>')
        if not target or target.startswith(('#','https://','http://','mailto:')):continue
        target=unquote(target.split('#',1)[0]);path=(root/Path(name).parent/target).resolve()
        try:rel=path.relative_to(root.resolve()).as_posix()
        except ValueError:result.append(target);continue
        exists=rel in files or any(n.startswith(rel.rstrip('/')+'/') for n in files)
        if not exists:result.append(target)
    return result


def local_secrets():
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url
    values=[]
    for name in ('.env','.local/database.env','.local/operator.env','.local/test-database.env'):
        p=ROOT/name
        if not p.exists():continue
        for k,v in dotenv_values(p).items():
            if not v:continue
            if any(s in k for s in ('KEY','SECRET','TOKEN','PASSWORD')):values.append(v)
            if 'DATABASE_URL' in k:
                try:values.append(make_url(v).password or '')
                except Exception:pass
    for name in ('.local/pg-credentials.json','.local/showcase/config.json'):
        p=ROOT/name
        if p.exists():
            for k,v in json.loads(p.read_text(encoding='utf-8')).items():
                if k=='token' or 'password' in k or name.endswith('pg-credentials.json'):values.append(str(v))
                elif k.endswith('_url'):
                    try:values.append(make_url(v).password or '')
                    except Exception:pass
    return [s for s in values if len(s)>=12]


def main():
    secrets=local_secrets();files=upload_files();issues=[]
    for name in files:
        p=ROOT/name
        if p.stat().st_size>10*1024*1024:issues.append(dict(path=name,labels=['large_file']))
        if any(part in ('.local','.venv','__pycache__','.idea') for part in p.relative_to(ROOT).parts) or p.name=='.env':
            issues.append(dict(path=name,labels=['private_file']))
        try:text=p.read_text(encoding='utf-8')
        except UnicodeError:continue
        labels=inspect_text(text,secrets)
        if labels:issues.append(dict(path=name,labels=labels))
        if name.endswith('.md'):
            for target in broken_links(ROOT,name,set(files)):issues.append(dict(path=name,labels=['broken_public_link'],target=target))
    historical=[]
    blobs=git('rev-list','--objects','--all').decode('utf-8').splitlines()
    checked=0
    for line in blobs:
        parts=line.split(' ',1)
        if len(parts)!=2:continue
        sha,name=parts
        if git('cat-file','-t',sha).strip()!=b'blob':continue
        checked+=1
        if Path(name).name=='.env' or any(s in name.split('/') for s in ('.local','.venv')):
            historical.append(dict(object=sha,path=name,labels=['private_file_history']))
        raw=git('cat-file','blob',sha)
        try:text=raw.decode('utf-8')
        except UnicodeError:continue
        labels=inspect_text(text,secrets)
        if labels:historical.append(dict(object=sha,path=name,labels=labels))
    report=dict(public_files=len(files),history_blobs_checked=checked,issues=issues,history_issues=historical,
                limitation='Pattern and known-secret scan, not a guarantee of absence of unknown secrets; no history rewritten',files=files)
    out=ROOT/'.local/repository-preparation';out.mkdir(parents=True,exist_ok=True)
    (out/'upload-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('public_files','history_blobs_checked','issues','history_issues')},ensure_ascii=False))
    return 1 if issues or historical else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:raise SystemExit('Publication check stopped safely: '+type(e).__name__) from None

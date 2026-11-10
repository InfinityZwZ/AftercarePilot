"""Create ignored Compose configuration. Never prints credentials or edits .env."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]


def host_timezone_name(explicit=None):
    zone = explicit or os.environ.get('TZ')
    if not zone and os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\TimeZoneInformation') as key:
            name = winreg.QueryValueEx(key, 'TimeZoneKeyName')[0].strip('\x00')
        zone = {'China Standard Time': 'Asia/Shanghai', 'AUS Eastern Standard Time': 'Australia/Sydney',
                'UTC': 'UTC'}.get(name)
    if not zone and os.name != 'nt':
        target = str(Path('/etc/localtime').resolve())
        if '/zoneinfo/' in target:
            zone = target.split('/zoneinfo/', 1)[1]
        elif Path('/etc/timezone').is_file():
            zone = Path('/etc/timezone').read_text(encoding='utf-8').strip()
    if not zone:
        raise ValueError('请用 --timezone 指定本机 IANA 时区，不能猜测固定时差。')
    ZoneInfo(zone)
    return zone


def write_configuration(path, *, verification=False, timezone_name=None):
    zone = host_timezone_name(timezone_name)
    if path.exists():
        from dotenv import dotenv_values
        from io import StringIO
        before = dict(dotenv_values(path))
        content = path.read_bytes()
        for key, value in {'TZ': zone, 'APP_TIMEZONE': 'local'}.items():
            pattern = rb'(?m)^[ \t]*' + key.encode() + rb'[ \t]*=[^\r\n]*'
            line = f"{key}='{value}'".encode()
            content, count = re.subn(pattern, lambda _: line, content)
            if not count:
                content += (b'' if content.endswith(b'\n') else b'\n') + line + b'\n'
        after = dict(dotenv_values(stream=StringIO(content.decode('utf-8'))))
        for key in ('TZ', 'APP_TIMEZONE'):
            before.pop(key, None)
            after.pop(key, None)
        if before != after:
            raise ValueError('Refusing to change non-timezone configuration')
        path.write_bytes(content)
        print('容器时区已同步，现有凭据和其他配置保持不变。')
        return
    from src.config import settings
    images = json.loads((ROOT / "docker" / "images.lock.json").read_text(encoding="utf-8"))
    admin, writer, reader = (secrets.token_urlsafe(32) for _ in range(3))
    def url(role, password, database):
        return URL.create("postgresql+psycopg", username=role, password=password, host="db", port=5432,
                          database=database).render_as_string(hide_password=False)
    values = dict(TZ=zone, APP_TIMEZONE='local', COMPOSE_PROJECT_NAME="aftercarepilot-verify" if verification else "aftercarepilot",
                  POSTGRES_IMAGE=images["postgres"]["reference"], PYTHON_IMAGE=images["python"]["reference"],
                  POSTGRES_ADMIN_PASSWORD=admin, DATABASE_URL=url("aftercare_app", writer, "aftercare_demo"),
                  READONLY_DATABASE_URL=url("aftercare_reader", reader, "aftercare_demo"),
                  BOOTSTRAP_DATABASE_URL=url("aftercare_bootstrap", admin, "postgres"),
                  TEST_DATABASE_URL=url("aftercare_app", writer, "aftercare_test"),
                  TEST_READONLY_DATABASE_URL=url("aftercare_reader", reader, "aftercare_test"),
                  OPERATOR_ID="compose_verify" if verification else settings.operator_id,
                  OPERATOR_TOKEN=secrets.token_urlsafe(32), OPENAI_API_KEY="" if verification else settings.openai_api_key,
                  OPENAI_MODEL=settings.openai_model, OPENAI_BASE_URL=settings.openai_base_url,
                  TEMPERATURE=str(settings.temperature), API_PORT="18000" if verification else "8000",
                  UI_PORT="18501" if verification else "8501", PG_PORT="15432" if verification else "5432")
    # Compose env files support single-quoted values; reject multiline configuration.
    if any("\n" in value or "\r" in value or "'" in value for value in values.values()):
        raise ValueError("Compose configuration contains unsupported characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n")
    print("Ignored Compose configuration created; credentials not printed. Verification mode: " + str(verification))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verification", action="store_true", help="Separate project/ports and blank model key")
    parser.add_argument("--timezone", help="本机 IANA 时区；无法自动识别时必须显式提供")
    args = parser.parse_args()
    path = ROOT / ".local" / ("compose.verify.env" if args.verification else "compose.env")
    write_configuration(path, verification=args.verification, timezone_name=args.timezone)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("容器配置未能生成；请核实配置，必要时用 --timezone 指定本机 IANA 时区。未输出密钥。") from None

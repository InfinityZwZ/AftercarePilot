"""Windows portable PostgreSQL lifecycle. No system service or .env modification."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
PORT = 55432
DATABASES = ("aftercare_demo", "aftercare_test")


def run_binary(binary: Path, *args):
    # A background postgres process can inherit Windows pipe handles and keep
    # communicate() waiting forever. Redirect commands to a file, not PIPE.
    with (LOCAL / "pg-command.log").open("a", encoding="utf-8") as log:
        result = subprocess.run([str(binary), *map(str, args)], stdout=log, stderr=subprocess.STDOUT,
                                timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(f"{binary.name} failed; inspect .local/postgres.log")


def credentials():
    path = LOCAL / "pg-credentials.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = {name: secrets.token_urlsafe(32) for name in ("aftercare_bootstrap", "aftercare_app", "aftercare_reader")}
    path.write_text(json.dumps(value), encoding="utf-8")
    return value


def resume(pg_bin: Path):
    data = LOCAL / "pgdata"
    if not (data / "PG_VERSION").is_file() or not (pg_bin / "pg_ctl.exe").is_file():
        raise RuntimeError("已有本机 PostgreSQL 数据目录或程序缺失；请检查 .local 目录。")
    status = subprocess.run([str(pg_bin / "pg_ctl.exe"), "-D", str(data), "status"], capture_output=True,
                            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if status.returncode:
        run_binary(pg_bin / "pg_ctl.exe", "-D", data, "-l", LOCAL / "postgres.log",
                   "-o", f"-h 127.0.0.1 -p {PORT}", "-w", "-t", "30", "start")


def ensure_database(pg_bin: Path):
    from src.config import settings
    from src.storage.database import check_database, pg_url

    url = pg_url(settings.database_url.get_secret_value())
    if url.host in ("127.0.0.1", "localhost") and url.port == PORT and url.database == DATABASES[0]:
        resume(pg_bin)
    check_database()
    print("数据库连接、迁移版本和只读权限检查通过。", flush=True)


def start(pg_bin: Path):
    LOCAL.mkdir(exist_ok=True)
    creds = credentials()
    data = LOCAL / "pgdata"
    if not (data / "PG_VERSION").exists():
        password_file = LOCAL / "initdb-password.txt"
        password_file.write_text(creds["aftercare_bootstrap"], encoding="utf-8")
        try:
            run_binary(pg_bin / "initdb.exe", "-D", data, "-U", "aftercare_bootstrap",
                       "--pwfile", password_file, "--auth-local=scram-sha-256",
                       "--auth-host=scram-sha-256", "--encoding=UTF8", "--locale=C")
        finally:
            password_file.unlink(missing_ok=True)
    status = subprocess.run([str(pg_bin / "pg_ctl.exe"), "-D", str(data), "status"], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if status.returncode:
        run_binary(pg_bin / "pg_ctl.exe", "-D", data, "-l", LOCAL / "postgres.log",
                   "-o", f"-h 127.0.0.1 -p {PORT}", "-w", "start")

    connect = dict(host="127.0.0.1", port=PORT, user="aftercare_bootstrap", password=creds["aftercare_bootstrap"])
    with psycopg.connect(dbname="postgres", autocommit=True, **connect) as conn:
        for role in ("aftercare_app", "aftercare_reader"):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                             .format(sql.Identifier(role), sql.Literal(creds[role])))
        for database in DATABASES:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER aftercare_app").format(sql.Identifier(database)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO aftercare_reader").format(sql.Identifier(database)))
    for database in DATABASES:
        with psycopg.connect(dbname=database, autocommit=True, **connect) as conn:
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            conn.execute("GRANT USAGE ON SCHEMA public TO aftercare_reader")
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO aftercare_reader")
            conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE aftercare_app IN SCHEMA public GRANT SELECT ON TABLES TO aftercare_reader")
    values = {}
    for key, role, database in (
        ("DATABASE_URL", "aftercare_app", "aftercare_demo"),
        ("READONLY_DATABASE_URL", "aftercare_reader", "aftercare_demo"),
        ("TEST_DATABASE_URL", "aftercare_app", "aftercare_test"),
        ("TEST_READONLY_DATABASE_URL", "aftercare_reader", "aftercare_test"),
    ):
        values[key] = URL.create("postgresql+psycopg", username=role, password=creds[role],
                                 host="127.0.0.1", port=PORT, database=database).render_as_string(hide_password=False)
    # Only application configuration is read by Settings. Test URLs are loaded by pytest separately.
    (LOCAL / "database.env").write_text("\n".join(f"{key}={value}" for key, value in values.items() if not key.startswith("TEST_")) + "\n", encoding="utf-8")
    (LOCAL / "test-database.env").write_text("\n".join(f"{key}={value}" for key, value in values.items() if key.startswith("TEST_")) + "\n", encoding="utf-8")
    print(f"PostgreSQL ready on loopback:{PORT}; app/reader roles and demo/test databases ready. Credentials saved in ignored .local files.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status", "ensure"))
    parser.add_argument("--pg-bin", type=Path, default=LOCAL / "postgresql" / "pgsql" / "bin")
    args = parser.parse_args()
    if args.action == "start":
        start(args.pg_bin.resolve())
    elif args.action == "ensure":
        ensure_database(args.pg_bin.resolve())
    elif args.action == "stop":
        run_binary(args.pg_bin.resolve() / "pg_ctl.exe", "-D", LOCAL / "pgdata", "-m", "fast", "-w", "stop")
        print("Local PostgreSQL stopped; data retained.")
    else:
        run_binary(args.pg_bin.resolve() / "pg_ctl.exe", "-D", LOCAL / "pgdata", "status")
        print("Local PostgreSQL is running.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("数据库操作失败。请检查连接配置、迁移版本和 .local/postgres.log；未输出凭据。", file=sys.stderr)
        sys.exit(1)

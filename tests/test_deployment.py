"""Deployment bootstrap/migrations tested against new, disposable real PG databases."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from scripts.start_api import bootstrap_database, connect, migrate_and_grant
from src.storage.database import check_reader_permissions, make_engine, pg_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment_database():
    admin_url = os.environ.get("TEST_BOOTSTRAP_DATABASE_URL")
    if not admin_url:
        path = ROOT / ".local" / "pg-credentials.json"
        if not path.is_file():
            pytest.fail("Deployment tests need TEST_BOOTSTRAP_DATABASE_URL for disposable roles/databases", pytrace=False)
        credentials = json.loads(path.read_text(encoding="utf-8"))
        admin_url = URL.create("postgresql+psycopg", username="aftercare_bootstrap", password=credentials["aftercare_bootstrap"],
                               host="127.0.0.1", port=55432, database="postgres").render_as_string(hide_password=False)
    admin = pg_url(admin_url)
    suffix = uuid4().hex
    database = "aftercare_s5_" + suffix + "_demo"
    test_database = "aftercare_s5_" + suffix + "_test"
    app_role, reader_role = "s5_app_" + suffix, "s5_reader_" + suffix
    def url(role):
        # A SQL quote and URL punctuation exercise identifier/literal/DSN handling.
        return URL.create("postgresql+psycopg", username=role, password=secrets.token_urlsafe(24) + "'@:/",
                          host=admin.host, port=admin.port, database=database).render_as_string(hide_password=False)
    app_url, reader_url = url(app_role), url(reader_role)
    try:
        yield dict(admin_url=admin_url, app_url=app_url, reader_url=reader_url, database=database, test_database=test_database)
    finally:
        with connect(admin) as connection:
            for name in (database, test_database):
                assert name.startswith("aftercare_s5_") and suffix in name
                connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
            for role in (app_role, reader_role):
                assert suffix in role and role.startswith("s5_")
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_deployment_bootstrap_repeat_migration_reader_and_no_automatic_seed(deployment_database):
    values = deployment_database
    for _ in range(2):
        bootstrap_database(values["app_url"], values["reader_url"], values["admin_url"], test_database=values["test_database"])
        migrate_and_grant(values["app_url"], values["reader_url"])
    engines = [make_engine(values[key]) for key in ("app_url", "reader_url")]
    try:
        from sqlalchemy import text
        with engines[0].connect() as connection:
            tables = set(connection.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")))
            assert tables == {"orders", "return_policies", "proposals", "aftersales_tickets", "audit_logs", "alembic_version"}
            assert connection.scalar(text("SELECT count(*) FROM orders")) == 0
            assert connection.scalar(text("SELECT count(*) FROM proposals")) == 0
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        with sessionmaker(engines[1])() as session:
            check_reader_permissions(session)
        reader = pg_url(values["reader_url"])
        for statement in ("INSERT INTO orders(order_id) VALUES('blocked')", "UPDATE orders SET total_cents=0", "DELETE FROM orders", "CREATE TABLE forbidden(id int)"):
            with connect(reader) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
        from scripts.seed_demo import seed_demo
        with sessionmaker(engines[0]).begin() as session:
            seed_demo(session)
        migrate_and_grant(values["app_url"], values["reader_url"])
        with engines[1].connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM orders")) == 17
            assert connection.scalar(text("SELECT count(*) FROM aftersales_tickets")) == 1
    finally:
        for engine in engines:
            engine.dispose()


def test_bootstrap_rejects_wrong_host_elevated_role_and_unrelated_database(deployment_database):
    values = deployment_database
    app = pg_url(values["app_url"])
    reader = pg_url(values["reader_url"])
    for bad_app, bad_reader in ((app, reader.set(host="other-host")), (app.set(database="unrelated"), reader.set(database="unrelated")),
                                (app.set(username=pg_url(values["admin_url"]).username), reader)):
        with pytest.raises(ValueError):
            bootstrap_database(bad_app.render_as_string(hide_password=False), bad_reader.render_as_string(hide_password=False), values["admin_url"])


def test_ui_container_api_base_url(monkeypatch):
    from src.config import settings
    from src.ui.api_client import APIClient
    monkeypatch.setattr(settings, "api_base_url", "http://api:8000/api/v1/")
    assert APIClient().base_url == "http://api:8000/api/v1"
    assert APIClient("http://custom/api/v1").base_url == "http://custom/api/v1"


def test_internal_api_client_bypasses_system_proxy():
    from src.ui.api_client import APIClient
    for host in ('127.0.0.1', 'localhost', 'api'):
        assert APIClient(f'http://{host}:8000/api/v1').transport.trust_env is False


@pytest.mark.parametrize("combined", [False, True])
def test_real_http_startup_approval_and_process_restart_persistence(deployment_database, tmp_path, combined):
    import httpx
    from run import stop_process
    from scripts.seed_demo import seed_demo
    from scripts.smoke_http import exercise_http, verify_persistence
    values = deployment_database
    bootstrap_database(values["app_url"], values["reader_url"], values["admin_url"])
    migrate_and_grant(values["app_url"], values["reader_url"])
    engine = make_engine(values["app_url"])
    try:
        with sessionmaker(engine).begin() as session:
            seed_demo(session)
    finally:
        engine.dispose()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = {**os.environ, "DATABASE_URL": values["app_url"], "READONLY_DATABASE_URL": values["reader_url"],
           "AFTERCARE_VERIFICATION": "1", "API_HOST": "127.0.0.1", "API_PORT": str(port), "READ_TRANSPORT": "mcp",
           "OPERATOR_TOKEN": token, "OPENAI_API_KEY": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
    env.pop("BOOTSTRAP_DATABASE_URL", None)
    base_url = f"http://127.0.0.1:{port}/api/v1"
    process = None
    with (tmp_path / "api.log").open("w", encoding="utf-8") as log:
        def start():
            child = subprocess.Popen([sys.executable, "-m", "scripts.start_api", "--verification"], cwd=ROOT, env=env,
                                     stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    pytest.fail("Verification API exited; inspect its isolated test log", pytrace=False)
                try:
                    if httpx.get(base_url + "/ready", timeout=1, trust_env=False).status_code == 200:
                        return child
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            child.terminate()
            child.wait(timeout=10)
            pytest.fail("Verification API not ready within 30 seconds", pytrace=False)
        try:
            process = start()
            result = exercise_http(base_url, token, combined=combined)
            stop_process(process)
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
            process = start()
            checked = verify_persistence(base_url, token, result)
            assert checked["restart_preserved_ticket"] and checked["restart_replay_same_ticket"]
        finally:
            if process is not None and process.poll() is None:
                stop_process(process)

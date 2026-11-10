"""Explicit deployment entrypoint: bootstrap, migrate, grant/check reader, exec API."""
import argparse
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql

from src.storage.database import make_engine, pg_url

ROOT = Path(__file__).resolve().parents[1]


def connect(url, database=None):
    return psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password,
                           dbname=database or url.database, connect_timeout=5, autocommit=True)


def bootstrap_database(app_url, reader_url, admin_url, *, test_database=None):
    app, reader, admin = map(pg_url, (app_url, reader_url, admin_url))
    if (app.host, app.port, app.database) != (reader.host, reader.port, reader.database) or app.username == reader.username:
        raise ValueError("App and distinct reader must share one database")
    if (admin.host, admin.port) != (app.host, app.port) or admin.username in (app.username, reader.username):
        raise ValueError("Bootstrap must be a separate role on the same server")
    if not app.database.endswith("_demo") or (test_database and not test_database.endswith("_test")):
        raise ValueError("Bootstrap is restricted to explicit demo/test databases")
    if not all(url.username and url.password for url in (app, reader, admin)):
        raise ValueError("Role credentials are required")
    with connect(admin) as connection:
        for role in (app, reader):
            existing = connection.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s", (role.username,)).fetchone()
            if existing and any(existing):
                raise RuntimeError("App/reader cannot be elevated PostgreSQL roles")
            if not existing:
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                                   .format(sql.Identifier(role.username), sql.Literal(role.password)))
            else:
                connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role.username), sql.Literal(role.password)))
        for database in filter(None, (app.database, test_database)):
            owner = connection.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (database,)).fetchone()
            if owner and owner[0] != app.username:
                raise RuntimeError("Refusing to change a database owned by another role")
            if not owner:
                connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(app.username)))
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(reader.username)))
            with connect(admin, database) as database_connection:
                database_connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                database_connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(reader.username)))
                database_connection.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}")
                                            .format(sql.Identifier(app.username), sql.Identifier(reader.username)))


def migrate_and_grant(app_url, reader_url):
    app, reader = map(pg_url, (app_url, reader_url))
    if (app.host, app.port, app.database) != (reader.host, reader.port, reader.database) or app.username == reader.username:
        raise ValueError("Distinct same-database reader configuration is required")
    engine = make_engine(app_url)
    try:
        with engine.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            quote = connection.dialect.identifier_preparer.quote
            from sqlalchemy import text
            connection.execute(text(f"GRANT USAGE ON SCHEMA {quote(connection.scalar(text('SELECT current_schema()')))} TO {quote(reader.username)}"))
            connection.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {quote(connection.scalar(text('SELECT current_schema()')))} TO {quote(reader.username)}"))
    finally:
        engine.dispose()


def prepare_database(*, verification=False):
    from src.config import settings
    app_url = settings.database_url.get_secret_value()
    reader_url = settings.readonly_database_url.get_secret_value()
    admin_url = os.environ.get("BOOTSTRAP_DATABASE_URL")
    if admin_url:
        bootstrap_database(app_url, reader_url, admin_url, test_database="aftercare_test" if verification else None)
    migrate_and_grant(app_url, reader_url)
    from src.storage.database import check_database
    check_database()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verification", action="store_true", help="Trusted test image only; FixedClock/FakeLLM; never a model provider")
    parser.add_argument("--prepare-only", action="store_true", help="Explicit initialization/migration/check; no seed or API")
    args = parser.parse_args()
    try:
        prepare_database(verification=args.verification)
    except Exception:
        raise SystemExit("API preparation failed; check PostgreSQL configuration/privileges/migrations. No seed or secrets printed.") from None
    print("PostgreSQL roles, migrations and reader checks ready; no demo seed run.", flush=True)
    if not args.prepare_only:
        from src.config import settings
        target = "scripts.verification_api:app" if args.verification else "src.main:app"
        # Bootstrap credential is unnecessary once preparation has completed.
        os.environ.pop("BOOTSTRAP_DATABASE_URL", None)
        if os.name == "nt":
            # Windows exec overlays use another PID. Keep the launcher lifetime tied
            # to the server so supervision/tests cannot mistake it for an exit.
            import uvicorn
            uvicorn.run(target, host=settings.api_host, port=settings.api_port)
        else:
            os.execv(sys.executable, [sys.executable, "-m", "uvicorn", target, "--host", settings.api_host, "--port", str(settings.api_port)])


if __name__ == "__main__":
    main()

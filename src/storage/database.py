from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

def pg_url(value: str):
    """Reject SQLite and missing configuration without echoing credentials."""
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("A PostgreSQL DATABASE_URL is required") from None
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise ValueError("Only postgresql+psycopg URLs are supported")
    return url


def make_engine(value: str):
    url = pg_url(value)
    options = str(url.query.get("options", "")) + " -c timezone=UTC"
    return create_engine(url, pool_pre_ping=True, hide_parameters=True,
                         connect_args={"connect_timeout": 5, "options": options.strip()})


@lru_cache(maxsize=2)
def session_factory(*, readonly: bool = False):
    # The MCP read server imports make_engine without loading application .env.
    from src.config import settings
    value = settings.readonly_database_url if readonly else settings.database_url
    if readonly:
        writer = pg_url(settings.database_url.get_secret_value())
        reader = pg_url(value.get_secret_value())
        if (reader.host, reader.port, reader.database) != (writer.host, writer.port, writer.database) or reader.username == writer.username:
            raise ValueError("A distinct reader role on the same PostgreSQL database is required")
    return sessionmaker(make_engine(value.get_secret_value()), expire_on_commit=False)


def check_reader_permissions(session):
    elevated = session.scalar(text("SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname=current_user"))
    writable = session.scalar(text("""
        SELECT bool_or(has_table_privilege(name, 'INSERT,UPDATE,DELETE,TRUNCATE'))
        FROM unnest(ARRAY['orders','return_policies','proposals','aftersales_tickets','audit_logs']) AS name
    """))
    if elevated or writable or session.scalar(text("SELECT has_schema_privilege(current_schema(), 'CREATE')")):
        raise RuntimeError("Configured reader role has excessive PostgreSQL privileges")


def check_database() -> None:
    """Startup checks only. Schema/seed changes are explicit commands."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    heads = set(ScriptDirectory.from_config(config).get_heads())
    try:
        with session_factory()() as session:
            versions = set(session.scalars(text("SELECT version_num FROM alembic_version")))
            if versions != heads:
                raise RuntimeError("Run python -m alembic upgrade head before starting the API")
        with session_factory(readonly=True)() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SELECT 1 FROM orders LIMIT 1"))
            check_reader_permissions(session)
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("PostgreSQL unavailable or unmigrated; check configuration and run migrations") from None

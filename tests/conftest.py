import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.storage.database import pg_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_paid_models(monkeypatch):
    from langchain_openai import ChatOpenAI
    def forbidden(*args, **kwargs):
        raise AssertionError("Paid model calls are forbidden in the default test suite")
    monkeypatch.setattr(ChatOpenAI, "invoke", forbidden)
    monkeypatch.setattr(ChatOpenAI, "ainvoke", forbidden)


@pytest.fixture(scope="session")
def pg():
    local = dotenv_values(ROOT / ".local" / "test-database.env")
    app_url = os.environ.get("TEST_DATABASE_URL") or local.get("TEST_DATABASE_URL")
    reader_url = os.environ.get("TEST_READONLY_DATABASE_URL") or local.get("TEST_READONLY_DATABASE_URL")
    if not app_url or not reader_url:
        pytest.fail("Real PostgreSQL tests require explicit TEST_DATABASE_URL and TEST_READONLY_DATABASE_URL; no SQLite fallback", pytrace=False)
    app = pg_url(app_url)
    reader = pg_url(reader_url)
    if not app.database.endswith("_test") or (app.host, app.port, app.database) != (reader.host, reader.port, reader.database) or app.username == reader.username:
        pytest.fail("PG tests require one _test database and distinct app/reader roles", pytrace=False)
    schema = "aftercare_test_" + uuid4().hex
    admin = create_engine(app, hide_parameters=True, connect_args={"connect_timeout": 5})
    engines = []
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for url in (app, reader):
            engines.append(create_engine(url, hide_parameters=True,
                           connect_args={"connect_timeout": 5, "options": f"-c timezone=UTC -c search_path={schema}"}))
        config = Config(str(ROOT / "alembic.ini"))
        with engines[0].begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            command.upgrade(config, "head")
            quoted_reader = connection.dialect.identifier_preparer.quote(reader.username)
            connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted_reader}'))
            connection.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO {quoted_reader}'))
        factories = [sessionmaker(engine, expire_on_commit=False) for engine in engines]
        from scripts.seed_demo import seed_demo
        with factories[0].begin() as session:
            seed_demo(session)
        yield {"engine": engines[0], "reader_engine": engines[1], "sessions": factories[0], "reader_sessions": factories[1], "schema": schema, "config": config}
    finally:
        for engine in engines:
            engine.dispose()
        # This generated schema is the only destructive test cleanup target.
        assert schema.startswith("aftercare_test_") and len(schema) == 47
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def pg_session(pg):
    with pg["engine"].connect() as connection:
        transaction = connection.begin()
        with sessionmaker(bind=connection, expire_on_commit=False)() as session:
            yield session
        transaction.rollback()


@pytest.fixture
def gateway(pg):
    from scripts.seed_demo import REFERENCE_TIME
    from src.domain.clock import FixedClock
    from src.tools.direct_gateway import DirectReadGateway
    return DirectReadGateway(pg["reader_sessions"], FixedClock(REFERENCE_TIME))


@pytest.fixture
def stage3(pg):
    """Each stage-three test owns a fresh schema, including committed/concurrent writes."""
    from scripts.seed_demo import REFERENCE_TIME, seed_demo
    from src.domain.clock import FixedClock
    from src.services.proposal_service import ProposalService
    from src.services.aftersales_service import AftersalesService
    from src.tools.direct_gateway import DirectReadGateway
    schema = "aftercare_s3_" + uuid4().hex
    app_engine = pg["engine"]
    engines = []
    try:
        with app_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for url in (pg["engine"].url, pg["reader_engine"].url):
            engines.append(create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 5,
                           "options": f"-c timezone=UTC -c search_path={schema} -c lock_timeout=5000 -c statement_timeout=10000"}))
        config = Config(str(ROOT / "alembic.ini"))
        with engines[0].begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            quoted_reader = connection.dialect.identifier_preparer.quote(pg["reader_engine"].url.username)
            connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted_reader}'))
            connection.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO {quoted_reader}'))
        sessions = sessionmaker(engines[0], expire_on_commit=False)
        readers = sessionmaker(engines[1], expire_on_commit=False)
        with sessions.begin() as session:
            seed_demo(session)
        clock = FixedClock(REFERENCE_TIME)
        gateway = DirectReadGateway(readers, clock)
        yield dict(sessions=sessions, readers=readers, engine=engines[0], clock=clock, gateway=gateway,
                   proposals=ProposalService(sessions, clock), aftersales=AftersalesService(sessions, clock))
    finally:
        for engine in engines:
            engine.dispose()
        assert schema.startswith("aftercare_s3_") and len(schema) == 45
        with app_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

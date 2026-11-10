from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import text, select
from sqlalchemy.orm import sessionmaker

from src.evaluation.dataset import ROOT
from src.storage.database import make_engine, pg_url


@contextmanager
def isolated_database(case):
    from scripts.seed_demo import seed_demo
    from scripts.start_api import migrate_and_grant
    values = dotenv_values(ROOT / '.local' / 'test-database.env')
    urls = [os.environ.get(key) or values.get(key) or '' for key in ('TEST_DATABASE_URL', 'TEST_READONLY_DATABASE_URL')]
    app, reader = map(pg_url, urls)
    if not app.database.endswith('_test') or (app.host, app.port, app.database) != (reader.host, reader.port, reader.database) or app.username == reader.username:
        raise ValueError('An isolated _test database and distinct reader are required')
    schema = 'aftercare_bench_' + uuid4().hex
    admin = make_engine(urls[0])
    engines = []
    created = False
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        scoped = [value.update_query_dict({'options': f'-c search_path={schema}'}).render_as_string(hide_password=False) for value in (app, reader)]
        migrate_and_grant(*scoped)
        engines = [make_engine(url) for url in scoped]
        sessions = sessionmaker(engines[0], expire_on_commit=False)
        with sessions.begin() as session:
            seed_demo(session)
            apply_fixture(session, case)
        yield sessions, scoped[1]
    finally:
        for engine in engines:
            engine.dispose()
        if created:
            assert schema.startswith('aftercare_bench_') and len(schema) == 48
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def apply_fixture(session, case):
    from src.storage.models import Order, ReturnPolicy
    for values in case['facts'].get('orders', []):
        if not set(values) <= set(Order.__table__.columns.keys()):
            raise ValueError('Unknown order fixture field')
        row = session.get(Order, values['order_id'])
        if row is None:
            row = Order(order_id=values['order_id'])
            session.add(row)
        for key, value in values.items():
            if key in ('created_at', 'delivered_at', 'updated_at') and value:
                value = datetime.fromisoformat(value) if isinstance(value, str) else value
            setattr(row, key, value)
    values = case['facts'].get('policy')
    if values:
        allowed = set(ReturnPolicy.__table__.columns.keys()) | {'reader_visibility'}
        if not set(values) <= allowed:
            raise ValueError('Unknown policy fixture field')
        row = session.get(ReturnPolicy, values['policy_id'])
        for key, value in values.items():
            if key != 'reader_visibility':
                if key == 'updated_at':
                    value = datetime.fromisoformat(value)
                setattr(row, key, value)
        if values.get('reader_visibility') == 'hidden_by_rls':
            session.execute(text('ALTER TABLE return_policies ENABLE ROW LEVEL SECURITY'))


def factual_snapshot(sessions):
    with sessions() as session:
        return {name: sorted(json.dumps(value, sort_keys=True) for value in session.scalars(text(f'SELECT to_jsonb(t) FROM {name} t')))
            for name in ('orders', 'return_policies', 'aftersales_tickets')}

from alembic import context

from src.config import settings
from src.storage.base import Base
from src.storage.database import make_engine, pg_url
from src.storage import models  # noqa: F401

config = context.config
target_metadata = Base.metadata


def run(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=pg_url(settings.database_url.get_secret_value()), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    provided = config.attributes.get("connection")
    if provided is not None:
        run(provided)
    else:
        engine = make_engine(settings.database_url.get_secret_value())
        try:
            with engine.connect() as connection:
                run(connection)
        finally:
            engine.dispose()

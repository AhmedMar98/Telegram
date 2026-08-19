from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app import models  # noqa: F401 - imported so its tables register on Base.metadata
from app.config import get_settings
from app.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata

# Indexes that exist in the database but cannot be expressed in the ORM
# metadata. ix_links_fts is a GIN index over a to_tsvector(...) expression,
# created by raw SQL in a migration; autogenerate cannot represent it, so
# without this exclusion every `alembic check` would report it as a stray
# index to be dropped.
_SQL_ONLY_INDEXES = {"ix_links_fts"}


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ARG001 - alembic hook signature
    return not (type_ == "index" and name in _SQL_ONLY_INDEXES)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

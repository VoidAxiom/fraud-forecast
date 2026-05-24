"""Alembic environment configuration for fraud-forecast."""

from __future__ import annotations

import os

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


# This is the Alembic Config object, which provides access to the values
# in the .ini file in use.
config = context.config

# Interpret the raw .ini file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy model metadata is targeted directly in this migration set.
target_metadata = None

# Database URL can be overridden via environment variable for container/runtime
# flexibility while preserving the default from db/alembic.ini.
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Run migrations without a DB connection."""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""

    section = config.get_section(config.config_ini_section)
    if section is None:
        section = {}
    section_with_url = dict(section)
    section_with_url["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")

    connectable = engine_from_config(
        section_with_url,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

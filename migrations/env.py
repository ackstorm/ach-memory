import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from memory.models import Base

target_metadata = Base.metadata

# MEMORY_DATABASE_URL wins over alembic.ini: the ini's URL is host-only and
# does not resolve inside a container, where the database is postgres:5432.
_env_url = os.getenv("MEMORY_DATABASE_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()

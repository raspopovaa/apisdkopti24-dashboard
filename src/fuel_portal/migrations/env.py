"""Окружение Alembic: адрес базы задаёт storage.migrate, схему — storage.metadata."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from fuel_portal.storage import metadata

_config = context.config

# Миграции только с подключением: SQL-скрипты без базы не нужны.
_engine = engine_from_config(
    _config.get_section(_config.config_ini_section, {}),
    prefix="sqlalchemy.",
    poolclass=pool.NullPool,
)
with _engine.connect() as connection:
    # render_as_batch: SQLite меняет таблицы только пересозданием.
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()

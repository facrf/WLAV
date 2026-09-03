from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.app import models  # noqa: F401
from backend.app.config import get_settings
from backend.app.database import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
MANAGED_EXPRESSION_INDEXES = {
    "ix_messages_content_fts_portuguese",
    "ix_chats_name_trgm",
}


def include_object(object_, name, type_, reflected, compare_to):
    del object_, reflected, compare_to
    return not (type_ == "index" and name in MANAGED_EXPRESSION_INDEXES)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.app import models  # noqa: F401
from backend.app.config import get_settings
from backend.app.database import Base

config = context.config
# `alembic -x database_url=...` sobrepõe a URL; sem ele vale a configuração da
# aplicação. Isso permite aplicar as migrações em um banco descartável de teste
# sem alterar o ambiente do processo.
config.set_main_option(
    "sqlalchemy.url",
    context.get_x_argument(as_dictionary=True).get("database_url") or get_settings().database_url,
)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

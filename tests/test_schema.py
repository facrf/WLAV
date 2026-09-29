"""O schema do banco de teste vem das migrações; os models são a outra fonte.

Estes testes comparam as duas. Sem eles, remover um índice do `models.py` (ou
esquecer de declará-lo) não quebraria nada: o `create_all()` deixaria de criar o
índice, o banco continuaria funcionando e ninguém perceberia a queda de
performance. Da mesma forma, um índice declarado no metadata mas nunca anexado à
tabela — o que o SQLAlchemy aceita em silêncio — passaria despercebido.
"""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from backend.app.database import Base
from backend.app.models import Chat, Message
from tests.support import requires_database

pytestmark = requires_database


def _live_tables(connection) -> set[str]:
    rows = connection.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    )
    return {row[0] for row in rows if not row[0].startswith("alembic")}


def _live_indexes(connection, table: str) -> dict[str, str]:
    rows = connection.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :table"),
        {"table": table},
    )
    return {row[0]: row[1] for row in rows}


def _declared_indexes(table) -> dict[str, str]:
    return {
        index.name: str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for index in table.indexes
    }


def test_migrations_create_exactly_the_tables_declared_by_the_models(migrated_database_url):
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        assert _live_tables(connection) == set(Base.metadata.tables)

        inspector = inspect(connection)
        for name, table in Base.metadata.tables.items():
            live_columns = {column["name"] for column in inspector.get_columns(name)}
            model_columns = {column.name for column in table.columns}
            assert live_columns == model_columns, f"colunas divergentes em {name}"

            # O PostgreSQL cria índices sozinho para PK e para FK única; esses não
            # são declarados no metadata e não interessam aqui.
            live_indexes = set(_live_indexes(connection, name)) - {f"{name}_pkey"}
            declared = set(_declared_indexes(table))
            assert not declared - live_indexes, (
                f"índices declarados nos models e ausentes no banco: "
                f"{sorted(declared - live_indexes)}"
            )
            assert not live_indexes - declared, (
                f"índices no banco e ausentes dos models: {sorted(live_indexes - declared)}"
            )
    engine.dispose()


def test_expression_indexes_are_attached_to_their_tables():
    """Um `Index` criado fora do `__table_args__` sem `append_constraint` é
    aceito em silêncio e nunca entra em `create_all`. Este teste pega isso."""
    assert "ix_messages_content_fts_portuguese" in {
        index.name for index in Message.__table__.indexes
    }
    assert "ix_chats_name_trgm" in {index.name for index in Chat.__table__.indexes}
    assert "ix_chats_last_message_time" in {index.name for index in Chat.__table__.indexes}


def _plan(connection, statement: str) -> str:
    """EXPLAIN de uma consulta, ignorando a preferência por varredura sequencial.

    A tabela está vazia nos testes, e sobre uma tabela vazia o planner escolhe o
    seq scan mesmo com um índice perfeito. Desligar o seq scan isola a pergunta
    que interessa: o índice é *aproveitável* para esta consulta?
    """
    connection.execute(text("SET enable_seqscan = off"))
    try:
        return "\n".join(row[0] for row in connection.execute(text(f"EXPLAIN {statement}")))
    finally:
        connection.execute(text("SET enable_seqscan = on"))


def test_chat_listing_index_matches_the_pagination_order(migrated_database_url):
    """O índice precisa bater com o ORDER BY de `/api/chats`, senão o planner o
    ignora e cada página paga um sort da tabela inteira."""
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        definition = _live_indexes(connection, "chats")["ix_chats_last_message_time"]
        assert definition.endswith("(last_message_time DESC NULLS LAST, name)")

        plan = _plan(
            connection,
            "SELECT jid FROM chats WHERE is_group "
            "ORDER BY last_message_time DESC NULLS LAST, name LIMIT 100",
        )
        assert "ix_chats_last_message_time" in plan
        assert "Sort" not in plan
    engine.dispose()


def test_trigram_index_matches_the_like_predicate(migrated_database_url):
    """Um índice GIN trgm sobre `coalesce(name, '')` não seria usado por
    `name ILIKE '%termo%'`; a forma correta é indexar a coluna pura."""
    engine = create_engine(migrated_database_url)
    with engine.connect() as connection:
        definition = _live_indexes(connection, "chats")["ix_chats_name_trgm"]
        assert "gin_trgm_ops" in definition
        assert "coalesce" not in definition.lower()
        assert "ix_chats_name_trgm" in _plan(
            connection, "SELECT jid FROM chats WHERE name ILIKE '%termo%'"
        )
    engine.dispose()

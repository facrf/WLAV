"""Índice de listagem de conversas e validação do payload de importação.

Revision ID: 20260929_0002
Revises: 20260903_0001
Create Date: 2026-09-29
"""

from alembic import op

revision = "20260929_0002"
down_revision = "20260903_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # /api/chats pagina por (last_message_time DESC NULLS LAST, name). Sem este
    # índice cada página dispara um sort completo da tabela de conversas.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chats_last_message_time "
        "ON chats (last_message_time DESC NULLS LAST, name)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chats_last_message_time")

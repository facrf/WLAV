from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# Configuração de busca full-text e de similaridade. Declaradas no metadata para
# que `Base.metadata.create_all()` reproduza exatamente o mesmo schema que as
# migrações do Alembic criam; `tests/test_schema.py` compara as duas fontes.
FTS_LANGUAGE = literal_column("'portuguese'::regconfig")


class Chat(Base):
    __tablename__ = "chats"

    jid: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    is_group: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_message_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Índice sobre a coluna pura (e não sobre coalesce) para que o planner
        # associe `name ILIKE '%termo%'` a este índice.
        Index(
            "ix_chats_name_trgm",
            name,
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
        # /api/chats pagina por last_message_time; sem este índice cada página
        # paga um sort completo da tabela de conversas.
        Index("ix_chats_last_message_time", last_message_time.desc().nullslast(), name),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    chat_jid: Mapped[str] = mapped_column(
        String(100), ForeignKey("chats.jid", ondelete="CASCADE"), nullable=False
    )
    sender_jid: Mapped[str | None] = mapped_column(String(100))
    sender_name: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_me: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    media_type: Mapped[str | None] = mapped_column(String(50))
    media_path: Mapped[str | None] = mapped_column(Text)
    media_mime: Mapped[str | None] = mapped_column(String(100))
    quoted_message_id: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        Index("ix_messages_chat_timestamp_desc", "chat_jid", timestamp.desc()),
        Index(
            "ix_messages_quoted_message_id",
            "quoted_message_id",
            postgresql_where=quoted_message_id.is_not(None),
        ),
    )

    chat: Mapped[Chat] = relationship(back_populates="messages")


# Declarado fora do corpo da classe porque a expressão referencia `Message`, e
# anexado explicitamente para que `create_all()` o crie junto com a tabela.
# Materializa o tsvector para que a busca use o índice GIN em vez de recalcular
# to_tsvector() linha a linha. `tests/test_schema.py` falha se este índice for
# removido do metadata.
Message.__table__.append_constraint(
    Index(
        "ix_messages_content_fts_portuguese",
        func.to_tsvector(FTS_LANGUAGE, func.coalesce(Message.content, "")),
        postgresql_using="gin",
    )
)


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    source_schema: Mapped[str | None] = mapped_column(String(30))
    chats_processed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    messages_processed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    media_copied: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    media_missing: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_import_jobs_created_at_desc", created_at.desc()),)

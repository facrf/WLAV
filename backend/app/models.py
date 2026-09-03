from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


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
        Index("ix_messages_quoted_message_id", "quoted_message_id"),
    )

    chat: Mapped[Chat] = relationship(back_populates="messages")

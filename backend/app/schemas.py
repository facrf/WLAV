from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    jid: str
    name: str | None
    is_group: bool
    created_at: datetime
    last_message_time: datetime | None
    last_message_preview: str | None = None


class QuoteOut(BaseModel):
    id: str
    sender_name: str | None
    content: str | None
    media_type: str | None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_jid: str
    sender_jid: str | None
    sender_name: str | None
    content: str | None
    timestamp: datetime
    from_me: bool
    has_media: bool
    media_type: str | None
    media_path: str | None
    media_mime: str | None
    quoted_message_id: str | None
    quoted_message: QuoteOut | None = None
    media_url: str | None = None
    thumbnail_url: str | None = None


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None
    has_more: bool


class SearchResult(BaseModel):
    message: MessageOut
    chat_name: str | None
    is_group: bool
    highlight: str | None
    rank: float


class SearchPage(BaseModel):
    items: list[SearchResult]
    page: int
    has_more: bool


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    source_sha256: str | None
    status: str
    source_schema: str | None
    chats_processed: int
    messages_processed: int
    media_copied: int
    media_missing: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WhatsAppKeyInput(BaseModel):
    key: str


class WhatsAppKeyStatus(BaseModel):
    saved: bool
    fingerprint: str | None = None
    updated_at: datetime | None = None

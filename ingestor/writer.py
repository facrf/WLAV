from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.app.models import Chat, Message
from ingestor.sqlite_reader import ChatRecord


def upsert_chats(session: Session, chats: Iterable[ChatRecord | dict]) -> int:
    rows = []
    for item in chats:
        row = (
            item
            if isinstance(item, dict)
            else {
                "jid": item.jid,
                "name": item.name,
                "is_group": item.is_group,
                "created_at": item.created_at,
                "last_message_time": item.last_message_time,
            }
        )
        if row.get("created_at") is None:
            row = {**row, "created_at": row.get("last_message_time") or datetime.now().astimezone()}
        rows.append(row)
    if not rows:
        return 0
    statement = insert(Chat).values(rows)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[Chat.jid],
        set_={
            "name": case((excluded.name.is_not(None), excluded.name), else_=Chat.name),
            "is_group": excluded.is_group,
            "created_at": func.least(Chat.created_at, excluded.created_at),
            "last_message_time": case(
                (Chat.last_message_time.is_(None), excluded.last_message_time),
                (excluded.last_message_time.is_(None), Chat.last_message_time),
                else_=func.greatest(Chat.last_message_time, excluded.last_message_time),
            ),
        },
    )
    session.execute(statement)
    return len(rows)


def upsert_messages(session: Session, rows: list[dict]) -> int:
    if not rows:
        return 0
    statement = insert(Message).values(rows)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[Message.id],
        set_={
            "chat_jid": excluded.chat_jid,
            "sender_jid": excluded.sender_jid,
            "sender_name": excluded.sender_name,
            "content": excluded.content,
            "timestamp": excluded.timestamp,
            "from_me": excluded.from_me,
            "has_media": excluded.has_media,
            "media_type": func.coalesce(excluded.media_type, Message.media_type),
            "media_path": func.coalesce(excluded.media_path, Message.media_path),
            "media_mime": func.coalesce(excluded.media_mime, Message.media_mime),
            "quoted_message_id": excluded.quoted_message_id,
        },
    )
    session.execute(statement)
    return len(rows)


def refresh_chat_bounds(session: Session) -> None:
    bounds = (
        select(
            Message.chat_jid.label("jid"),
            func.min(Message.timestamp).label("first_time"),
            func.max(Message.timestamp).label("last_time"),
        )
        .group_by(Message.chat_jid)
        .subquery()
    )
    session.execute(
        Chat.__table__.update()
        .where(Chat.jid == bounds.c.jid)
        .values(
            created_at=func.least(Chat.created_at, bounds.c.first_time),
            last_message_time=bounds.c.last_time,
        )
    )

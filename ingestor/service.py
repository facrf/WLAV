import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ingestor.chat_export import WhatsAppExportReader
from ingestor.media_store import MediaStore, infer_media_type
from ingestor.sqlite_reader import ChatRecord, MsgstoreReader
from ingestor.thumbnails import Thumbnailer
from ingestor.writer import refresh_chat_bounds, upsert_chats, upsert_messages

logger = logging.getLogger("wlav.ingestor")


@dataclass(slots=True)
class ImportStats:
    schema: str = "unknown"
    chats: int = 0
    messages: int = 0
    media_copied: int = 0
    media_missing: int = 0
    thumbnails: int = 0

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


ProgressCallback = Callable[[ImportStats], None]


def ingest_sqlite(
    sqlite_path: Path,
    media_dir: Path | None,
    media_root: Path,
    engine: Engine | None,
    batch_size: int = 1_000,
    dry_run: bool = False,
    thumbnail_size: int = 480,
    progress: ProgressCallback | None = None,
) -> ImportStats:
    with MsgstoreReader(sqlite_path) as reader:
        return _ingest_reader(
            reader,
            media_dir,
            media_root,
            engine,
            batch_size,
            dry_run,
            thumbnail_size,
            progress,
        )


def ingest_chat_exports(
    transcripts: list[Path],
    media_dir: Path | None,
    media_root: Path,
    engine: Engine | None,
    owner_name: str | None = None,
    date_order: str = "auto",
    timezone: str = "America/Sao_Paulo",
    fallback_title: str | None = None,
    batch_size: int = 1_000,
    dry_run: bool = False,
    thumbnail_size: int = 480,
    progress: ProgressCallback | None = None,
) -> ImportStats:
    with WhatsAppExportReader(
        transcripts,
        timezone=timezone,
        date_order=date_order,
        owner_name=owner_name,
        fallback_title=fallback_title,
    ) as reader:
        return _ingest_reader(
            reader,
            media_dir,
            media_root,
            engine,
            batch_size,
            dry_run,
            thumbnail_size,
            progress,
        )


def _ingest_reader(
    reader,
    media_dir: Path | None,
    media_root: Path,
    engine: Engine | None,
    batch_size: int,
    dry_run: bool,
    thumbnail_size: int,
    progress: ProgressCallback | None,
) -> ImportStats:
    store = MediaStore(media_dir, media_root)
    thumbnailer = Thumbnailer(media_root, thumbnail_size)
    stats = ImportStats()

    stats.schema = reader.schema
    logger.info("Formato de origem detectado: %s", reader.schema)
    initial_chats = reader.chats()
    known_chats = {chat.jid for chat in initial_chats}
    stats.chats = len(initial_chats)

    session = None if dry_run else Session(engine)
    try:
        if session:
            upsert_chats(session, initial_chats)
            session.commit()
        if progress:
            progress(stats)

        batch: list[dict] = []
        pending_chats: list[ChatRecord] = []
        for message in reader.iter_messages(batch_size=batch_size):
            if message.chat_jid not in known_chats:
                pending_chats.append(
                    ChatRecord(
                        jid=message.chat_jid,
                        name=message.chat_jid.split("@", 1)[0],
                        is_group=message.chat_jid.endswith("@g.us"),
                        created_at=message.timestamp,
                        last_message_time=message.timestamp,
                    )
                )
                known_chats.add(message.chat_jid)
                stats.chats += 1

            media_path = None
            source = store.locate(message.media_reference)
            media_type = message.media_type or infer_media_type(
                message.media_mime, message.media_reference
            )
            if source:
                media_path = store.copy(
                    source,
                    message.chat_jid,
                    message.timestamp.strftime("%Y_%m"),
                    message.id,
                    dry_run=dry_run,
                )
                stats.media_copied += 1
                if not dry_run and thumbnailer.generate(media_path, media_type):
                    stats.thumbnails += 1
            elif message.has_media:
                stats.media_missing += 1

            batch.append(
                {
                    "id": message.id,
                    "chat_jid": message.chat_jid,
                    "sender_jid": message.sender_jid,
                    "sender_name": message.sender_name,
                    "content": message.content,
                    "timestamp": message.timestamp,
                    "from_me": message.from_me,
                    "has_media": message.has_media,
                    "media_type": media_type,
                    "media_path": media_path,
                    "media_mime": message.media_mime,
                    "quoted_message_id": message.quoted_message_id,
                }
            )
            stats.messages += 1

            if len(batch) >= batch_size:
                if session:
                    upsert_chats(session, pending_chats)
                    upsert_messages(session, batch)
                    session.commit()
                pending_chats.clear()
                batch.clear()
                logger.info("%s mensagens processadas", f"{stats.messages:,}")
                if progress:
                    progress(stats)

        if session:
            upsert_chats(session, pending_chats)
            upsert_messages(session, batch)
            refresh_chat_bounds(session)
            session.commit()
        if progress:
            progress(stats)
    except Exception:
        if session:
            session.rollback()
        raise
    finally:
        if session:
            session.close()
    return stats

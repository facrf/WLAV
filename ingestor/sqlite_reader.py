import hashlib
import re
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MEDIA_TYPES = {
    1: "image",
    2: "audio",
    3: "video",
    9: "document",
    13: "video",  # GIFs são armazenados como vídeo pelo WhatsApp.
    20: "sticker",
}


@dataclass(slots=True)
class ChatRecord:
    jid: str
    name: str | None
    is_group: bool
    created_at: datetime | None
    last_message_time: datetime | None


@dataclass(slots=True)
class MessageRecord:
    id: str
    chat_jid: str
    sender_jid: str | None
    sender_name: str | None
    content: str | None
    timestamp: datetime
    from_me: bool
    has_media: bool
    media_type: str | None
    media_reference: str | None
    media_mime: str | None
    quoted_message_id: str | None


class UnsupportedSchemaError(RuntimeError):
    pass


def normalize_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        epoch = float(value)
        absolute = abs(epoch)
        if absolute > 1e17:
            epoch /= 1_000_000_000
        elif absolute > 1e14:
            epoch /= 1_000_000
        elif absolute > 1e11:
            epoch /= 1_000
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def bounded_id(value: Any, prefix: str = "") -> str:
    raw = f"{prefix}{value}"
    if len(raw) <= 100:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    return f"{raw[:79]}-{digest}"


class MsgstoreReader:
    """Leitor somente-leitura para schemas Android moderno e legado."""

    def __init__(self, sqlite_path: Path):
        self.sqlite_path = sqlite_path.resolve()
        if not self.sqlite_path.is_file():
            raise FileNotFoundError(f"SQLite não encontrado: {self.sqlite_path}")
        uri = f"file:{self.sqlite_path.as_posix()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if {"message", "chat", "jid"}.issubset(self.tables):
            self.schema = "modern"
        elif "messages" in self.tables:
            self.schema = "legacy"
        else:
            preview = ", ".join(sorted(self.tables)[:20])
            raise UnsupportedSchemaError(
                "Schema do msgstore.db não reconhecido. Tabelas encontradas: " + preview
            )
        self._columns_cache: dict[str, set[str]] = {}
        self._jids: dict[Any, str] = {}
        self._contacts: dict[str, str] = {}
        self._chats: dict[Any, ChatRecord] = {}
        self._prepare_lookups()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MsgstoreReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def columns(self, table: str) -> set[str]:
        if not IDENTIFIER.fullmatch(table) or table not in self.tables:
            return set()
        if table not in self._columns_cache:
            self._columns_cache[table] = {
                row[1] for row in self.connection.execute(f'PRAGMA table_info("{table}")')
            }
        return self._columns_cache[table]

    @staticmethod
    def _column(alias: str, columns: set[str], *choices: str, default: str = "NULL") -> str:
        for choice in choices:
            if choice in columns and IDENTIFIER.fullmatch(choice):
                return f'{alias}."{choice}"'
        return default

    @staticmethod
    def _jid_from_row(row: sqlite3.Row) -> str | None:
        keys = set(row.keys())
        if "raw_string" in keys and row["raw_string"]:
            return str(row["raw_string"])
        user = row["user"] if "user" in keys else None
        server = row["server"] if "server" in keys else None
        if user and server:
            return f"{user}@{server}"
        return None

    def _prepare_lookups(self) -> None:
        if self.schema == "modern":
            for row in self.connection.execute("SELECT * FROM jid"):
                jid = self._jid_from_row(row)
                if jid:
                    self._jids[row["_id"]] = jid
        self._load_contacts()
        self._load_chats()

    def _load_contacts(self) -> None:
        for table in ("wa_contacts", "wa_vnames"):
            if table not in self.tables:
                continue
            columns = self.columns(table)
            jid_column = next(
                (name for name in ("jid", "raw_string", "wa_id") if name in columns), None
            )
            name_columns = [
                name
                for name in ("display_name", "wa_name", "given_name", "sort_name", "verified_name")
                if name in columns
            ]
            if not jid_column or not name_columns:
                continue
            select_names = ", ".join(f'"{name}"' for name in name_columns)
            query = f'SELECT "{jid_column}" AS jid, {select_names} FROM "{table}"'
            for row in self.connection.execute(query):
                jid = str(row["jid"] or "")
                if jid and "@" not in jid:
                    jid = f"{jid}@s.whatsapp.net"
                name = next((str(row[key]) for key in name_columns if row[key]), None)
                if jid and name:
                    self._contacts.setdefault(jid, name)

    @staticmethod
    def _fallback_name(jid: str) -> str:
        local = jid.split("@", 1)[0]
        return local if local else jid

    def _load_chats(self) -> None:
        if self.schema == "modern":
            columns = self.columns("chat")
            for row in self.connection.execute("SELECT * FROM chat"):
                jid = self._jids.get(row["jid_row_id"])
                if not jid:
                    continue
                subject = row["subject"] if "subject" in columns else None
                created_raw = next(
                    (row[name] for name in ("created_timestamp", "creation") if name in columns),
                    None,
                )
                last_raw = next(
                    (
                        row[name]
                        for name in ("sort_timestamp", "last_message_time")
                        if name in columns
                    ),
                    None,
                )
                self._chats[row["_id"]] = ChatRecord(
                    jid=bounded_id(jid),
                    name=subject or self._contacts.get(jid) or self._fallback_name(jid),
                    is_group=jid.endswith("@g.us"),
                    created_at=normalize_timestamp(created_raw),
                    last_message_time=normalize_timestamp(last_raw),
                )
        else:
            table = "chat_list" if "chat_list" in self.tables else None
            if not table:
                return
            columns = self.columns(table)
            jid_name = next((name for name in ("key_remote_jid", "jid") if name in columns), None)
            if not jid_name:
                return
            for row in self.connection.execute(f'SELECT * FROM "{table}"'):
                jid = str(row[jid_name] or "")
                if not jid:
                    continue
                subject = row["subject"] if "subject" in columns else None
                created_raw = next(
                    (row[name] for name in ("creation", "created_timestamp") if name in columns),
                    None,
                )
                last_raw = next(
                    (
                        row[name]
                        for name in ("sort_timestamp", "last_message_time")
                        if name in columns
                    ),
                    None,
                )
                self._chats[jid] = ChatRecord(
                    jid=bounded_id(jid),
                    name=subject or self._contacts.get(jid) or self._fallback_name(jid),
                    is_group=jid.endswith("@g.us"),
                    created_at=normalize_timestamp(created_raw),
                    last_message_time=normalize_timestamp(last_raw),
                )

    def chats(self) -> list[ChatRecord]:
        return list(self._chats.values())

    def iter_messages(self, batch_size: int = 2_000) -> Iterator[MessageRecord]:
        if self.schema == "modern":
            yield from self._iter_modern_messages(batch_size)
        else:
            yield from self._iter_legacy_messages(batch_size)

    def _iter_modern_messages(self, batch_size: int) -> Iterator[MessageRecord]:
        columns = self.columns("message")
        media_columns = self.columns("message_media")
        quote_columns = self.columns("message_quoted")
        media_join = ""
        quote_join = ""
        media_alias = "NULL"
        mime_alias = self._column("m", columns, "mime_type")
        caption_alias = "NULL"
        if media_columns and "message_row_id" in media_columns:
            media_join = "LEFT JOIN message_media mm ON mm.message_row_id = m._id"
            media_alias = self._column("mm", media_columns, "file_path", "media_name")
            mime_alias = self._column(
                "mm", media_columns, "mime_type", "media_mime_type", default=mime_alias
            )
            caption_alias = self._column("mm", media_columns, "media_caption", "caption")
        quoted_alias = "NULL"
        if quote_columns and "message_row_id" in quote_columns:
            quote_join = "LEFT JOIN message_quoted mq ON mq.message_row_id = m._id"
            quoted_alias = self._column("mq", quote_columns, "key_id", "stanza_id")

        query = f"""
            SELECT
                m._id AS source_row_id,
                {self._column("m", columns, "chat_row_id")} AS chat_row_id,
                {self._column("m", columns, "key_id", "uuid")} AS key_id,
                {self._column("m", columns, "sender_jid_row_id")} AS sender_jid_row_id,
                {self._column("m", columns, "from_me", default="0")} AS from_me,
                {self._column("m", columns, "timestamp")} AS timestamp,
                {self._column("m", columns, "text_data", "data")} AS text_data,
                {
            self._column("m", columns, "message_type", "media_wa_type", default="0")
        } AS message_type,
                {media_alias} AS media_reference,
                {mime_alias} AS media_mime,
                {caption_alias} AS media_caption,
                {quoted_alias} AS quoted_message_id
            FROM message m
            {media_join}
            {quote_join}
            ORDER BY m._id
        """
        cursor = self.connection.execute(query)
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                chat = self._chats.get(row["chat_row_id"])
                timestamp = normalize_timestamp(row["timestamp"])
                if not chat or not timestamp:
                    continue
                from_me = bool(row["from_me"])
                sender_jid = self._jids.get(row["sender_jid_row_id"])
                if not sender_jid and not from_me and not chat.is_group:
                    sender_jid = chat.jid
                content = row["text_data"] or row["media_caption"]
                message_type = MEDIA_TYPES.get(int(row["message_type"] or 0))
                media_reference = row["media_reference"]
                message_id = row["key_id"] or f"row-{row['source_row_id']}"
                yield MessageRecord(
                    id=bounded_id(message_id),
                    chat_jid=chat.jid,
                    sender_jid=bounded_id(sender_jid) if sender_jid else None,
                    sender_name="Você"
                    if from_me
                    else self._contacts.get(sender_jid or "")
                    or (self._fallback_name(sender_jid) if sender_jid else None),
                    content=str(content) if content is not None else None,
                    timestamp=timestamp,
                    from_me=from_me,
                    has_media=bool(message_type or media_reference),
                    media_type=message_type,
                    media_reference=str(media_reference) if media_reference else None,
                    media_mime=str(row["media_mime"]) if row["media_mime"] else None,
                    quoted_message_id=(
                        bounded_id(row["quoted_message_id"]) if row["quoted_message_id"] else None
                    ),
                )

    def _iter_legacy_messages(self, batch_size: int) -> Iterator[MessageRecord]:
        columns = self.columns("messages")
        jid_column = next(
            (name for name in ("key_remote_jid", "chat_jid") if name in columns), None
        )
        if not jid_column or "timestamp" not in columns:
            raise UnsupportedSchemaError(
                "A tabela messages não contém key_remote_jid/chat_jid e timestamp."
            )
        quote_join = ""
        quoted_alias = self._column("m", columns, "quoted_message_id", "stanza_id")
        if "quoted_row_id" in columns and "_id" in columns:
            quote_join = "LEFT JOIN messages qm ON qm._id = m.quoted_row_id"
            quoted_alias = self._column("qm", columns, "key_id", default=quoted_alias)
        query = f"""
            SELECT
                {self._column("m", columns, "_id", default="m.rowid")} AS source_row_id,
                m."{jid_column}" AS chat_jid,
                {self._column("m", columns, "key_id", "uuid")} AS key_id,
                {self._column("m", columns, "remote_resource", "sender_jid")} AS sender_jid,
                {self._column("m", columns, "key_from_me", "from_me", default="0")} AS from_me,
                m.timestamp AS timestamp,
                {self._column("m", columns, "data", "text_data")} AS text_data,
                {self._column("m", columns, "media_caption")} AS media_caption,
                {
            self._column("m", columns, "media_wa_type", "message_type", default="0")
        } AS message_type,
                {self._column("m", columns, "media_name", "file_path")} AS media_reference,
                {self._column("m", columns, "mime_type", "media_mime_type")} AS media_mime,
                {quoted_alias} AS quoted_message_id
            FROM messages m
            {quote_join}
            ORDER BY source_row_id
        """
        cursor = self.connection.execute(query)
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                jid = str(row["chat_jid"] or "")
                timestamp = normalize_timestamp(row["timestamp"])
                if not jid or not timestamp:
                    continue
                chat = self._chats.get(jid)
                if not chat:
                    chat = ChatRecord(
                        jid=bounded_id(jid),
                        name=self._contacts.get(jid) or self._fallback_name(jid),
                        is_group=jid.endswith("@g.us"),
                        created_at=timestamp,
                        last_message_time=timestamp,
                    )
                    self._chats[jid] = chat
                from_me = bool(row["from_me"])
                sender_jid = row["sender_jid"]
                if not sender_jid and not from_me and not chat.is_group:
                    sender_jid = jid
                message_type = MEDIA_TYPES.get(int(row["message_type"] or 0))
                media_reference = row["media_reference"]
                content = row["text_data"] or row["media_caption"]
                message_id = row["key_id"] or f"legacy-row-{row['source_row_id']}"
                yield MessageRecord(
                    id=bounded_id(message_id),
                    chat_jid=chat.jid,
                    sender_jid=bounded_id(sender_jid) if sender_jid else None,
                    sender_name="Você"
                    if from_me
                    else self._contacts.get(str(sender_jid or ""))
                    or (self._fallback_name(str(sender_jid)) if sender_jid else None),
                    content=str(content) if content is not None else None,
                    timestamp=timestamp,
                    from_me=from_me,
                    has_media=bool(message_type or media_reference),
                    media_type=message_type,
                    media_reference=str(media_reference) if media_reference else None,
                    media_mime=str(row["media_mime"]) if row["media_mime"] else None,
                    quoted_message_id=(
                        bounded_id(row["quoted_message_id"]) if row["quoted_message_id"] else None
                    ),
                )

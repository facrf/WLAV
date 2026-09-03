import io
import json
import logging
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Chat, Message
from ingestor.writer import refresh_chat_bounds, upsert_chats, upsert_messages

logger = logging.getLogger("wlav.archive")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Tipo não serializável: {type(value).__name__}")


def _as_dict(instance: Chat | Message) -> dict[str, Any]:
    return {column.name: getattr(instance, column.name) for column in instance.__table__.columns}


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = 0
    archive.addfile(info, io.BytesIO(payload))


def export_archive(session: Session, media_root: Path, output: Path) -> dict[str, int]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    media_root = media_root.resolve()
    stats = {"chats": 0, "messages": 0, "media": 0, "missing_media": 0}
    added_media: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="wlav-export-") as temporary:
        temporary_path = Path(temporary)
        chats_path = temporary_path / "chats.jsonl"
        messages_path = temporary_path / "messages.jsonl"

        with chats_path.open("w", encoding="utf-8") as handle:
            for chat in session.scalars(select(Chat).order_by(Chat.jid)).yield_per(1_000):
                serialized = json.dumps(_as_dict(chat), default=_json_default, ensure_ascii=False)
                handle.write(serialized + "\n")
                stats["chats"] += 1

        with messages_path.open("w", encoding="utf-8") as handle:
            for message in session.scalars(
                select(Message).order_by(Message.timestamp, Message.id)
            ).yield_per(2_000):
                handle.write(
                    json.dumps(_as_dict(message), default=_json_default, ensure_ascii=False) + "\n"
                )
                stats["messages"] += 1

        partial = output.with_name(output.name + ".part")
        with tarfile.open(partial, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            manifest = {
                "format": "wlav-archive",
                "version": 1,
                "created_at": datetime.now().astimezone().isoformat(),
            }
            _add_bytes(
                archive,
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
            )
            archive.add(chats_path, arcname="chats.jsonl", recursive=False)
            archive.add(messages_path, arcname="messages.jsonl", recursive=False)

            media_paths = session.scalars(
                select(Message.media_path)
                .where(Message.media_path.is_not(None))
                .distinct()
                .order_by(Message.media_path)
            )
            for relative_string in media_paths:
                if not relative_string or relative_string in added_media:
                    continue
                relative = Path(relative_string)
                source = (media_root / relative).resolve()
                if not source.is_relative_to(media_root) or not source.is_file():
                    stats["missing_media"] += 1
                    logger.warning("Mídia ausente durante exportação: %s", relative_string)
                    continue
                archive.add(source, arcname=f"media/{relative.as_posix()}", recursive=False)
                added_media.add(relative_string)
                stats["media"] += 1
        partial.replace(output)
    return stats


def _validate_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise ValueError(f"Entrada insegura no arquivo: {member.name}")
        if member.name in members:
            raise ValueError(f"Entrada duplicada no arquivo: {member.name}")
        members[member.name] = member
    required = {"manifest.json", "chats.jsonl", "messages.jsonl"}
    if not required.issubset(members):
        raise ValueError("Backup inválido: manifest.json/chats.jsonl/messages.jsonl ausentes")
    return members


def _json_lines(archive: tarfile.TarFile, member: tarfile.TarInfo):
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ValueError(f"Não foi possível ler {member.name}")
    with io.TextIOWrapper(extracted, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSON inválido em {member.name}:{line_number}") from exc


def _parse_datetimes(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    for field in fields:
        if row.get(field):
            row[field] = datetime.fromisoformat(row[field])
    return row


def restore_archive(
    session: Session, media_root: Path, source: Path, batch_size: int = 1_000
) -> dict[str, int]:
    source = source.resolve()
    media_root = media_root.resolve()
    stats = {"chats": 0, "messages": 0, "media": 0}
    with tarfile.open(source, "r:gz") as archive:
        members = _validate_members(archive)
        manifest_file = archive.extractfile(members["manifest.json"])
        if manifest_file is None:
            raise ValueError("Manifesto ilegível")
        manifest = json.load(manifest_file)
        if manifest.get("format") != "wlav-archive" or manifest.get("version") != 1:
            raise ValueError("Formato ou versão de backup não suportado")

        chat_batch: list[dict[str, Any]] = []
        for row in _json_lines(archive, members["chats.jsonl"]):
            chat_batch.append(_parse_datetimes(row, "created_at", "last_message_time"))
            if len(chat_batch) >= batch_size:
                stats["chats"] += upsert_chats(session, chat_batch)
                chat_batch.clear()
        stats["chats"] += upsert_chats(session, chat_batch)
        session.flush()

        message_batch: list[dict[str, Any]] = []
        for row in _json_lines(archive, members["messages.jsonl"]):
            message_batch.append(_parse_datetimes(row, "timestamp"))
            if len(message_batch) >= batch_size:
                stats["messages"] += upsert_messages(session, message_batch)
                message_batch.clear()
        stats["messages"] += upsert_messages(session, message_batch)
        refresh_chat_bounds(session)

        for name, member in members.items():
            if not name.startswith("media/") or not member.isfile():
                continue
            relative = Path(PurePosixPath(name).relative_to("media"))
            destination = (media_root / relative).resolve()
            if not destination.is_relative_to(media_root):
                raise ValueError(f"Caminho de mídia inseguro: {name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            partial = destination.with_name(destination.name + ".part")
            with partial.open("wb") as handle:
                while chunk := extracted.read(1024 * 1024):
                    handle.write(chunk)
            partial.replace(destination)
            stats["media"] += 1
    return stats

import hashlib
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
from ingestor.thumbnails import thumbnail_relative
from ingestor.writer import refresh_chat_bounds, upsert_chats, upsert_messages

logger = logging.getLogger("wlav.archive")
CHUNK_SIZE = 4 * 1024 * 1024


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ValueError(f"Não foi possível ler {member.name}")
    digest = hashlib.sha256()
    while chunk := extracted.read(CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def export_archive(session: Session, media_root: Path, output: Path) -> dict[str, int]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    media_root = media_root.resolve()
    stats = {"chats": 0, "messages": 0, "media": 0, "thumbnails": 0, "missing_media": 0}
    checksums: dict[str, str] = {}
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

        checksums["chats.jsonl"] = _sha256_file(chats_path)
        checksums["messages.jsonl"] = _sha256_file(messages_path)
        partial = output.with_name(output.name + ".part")
        try:
            with tarfile.open(partial, "w:gz", format=tarfile.PAX_FORMAT) as archive:
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
                    archive_name = f"media/{relative.as_posix()}"
                    archive.add(source, arcname=archive_name, recursive=False)
                    checksums[archive_name] = _sha256_file(source)
                    added_media.add(relative_string)
                    stats["media"] += 1

                    thumb_relative = thumbnail_relative(relative_string)
                    thumbnail = (media_root / thumb_relative).resolve()
                    if thumbnail.is_relative_to(media_root) and thumbnail.is_file():
                        thumb_name = f"media/{thumb_relative}"
                        archive.add(thumbnail, arcname=thumb_name, recursive=False)
                        checksums[thumb_name] = _sha256_file(thumbnail)
                        stats["thumbnails"] += 1

                manifest = {
                    "format": "wlav-archive",
                    "version": 2,
                    "created_at": datetime.now().astimezone().isoformat(),
                    "stats": stats,
                    "checksum": "sha256",
                }
                _add_bytes(
                    archive,
                    "checksums.json",
                    json.dumps(checksums, sort_keys=True, indent=2).encode(),
                )
                _add_bytes(
                    archive,
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
                )
            partial.replace(output)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
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


def _read_manifest(archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo]) -> dict:
    manifest_file = archive.extractfile(members["manifest.json"])
    if manifest_file is None:
        raise ValueError("Manifesto ilegível")
    manifest = json.load(manifest_file)
    if manifest.get("format") != "wlav-archive" or manifest.get("version") not in (1, 2):
        raise ValueError("Formato ou versão de backup não suportado")
    return manifest


def verify_archive(source: Path) -> dict[str, int]:
    source = source.resolve()
    with tarfile.open(source, "r:gz") as archive:
        members = _validate_members(archive)
        manifest = _read_manifest(archive, members)
        if manifest["version"] == 1:
            return {"files": len(members), "bytes": sum(item.size for item in members.values())}
        if "checksums.json" not in members:
            raise ValueError("Backup versão 2 sem checksums.json")
        checksum_file = archive.extractfile(members["checksums.json"])
        if checksum_file is None:
            raise ValueError("checksums.json ilegível")
        expected = json.load(checksum_file)
        data_members = {name for name, member in members.items() if member.isfile()} - {
            "manifest.json",
            "checksums.json",
        }
        if data_members != set(expected):
            raise ValueError("A lista de arquivos não corresponde aos checksums do backup")
    actual: dict[str, str] = {}
    total_bytes = 0
    # Segunda passagem sequencial: evita seeks quadráticos dentro do gzip.
    with tarfile.open(source, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or member.name in {"manifest.json", "checksums.json"}:
                continue
            actual[member.name] = _sha256_member(archive, member)
            total_bytes += member.size
    for name, expected_hash in expected.items():
        if actual.get(name) != expected_hash:
            raise ValueError(f"Falha de integridade em: {name}")
    return {"files": len(expected), "bytes": total_bytes}


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
    verify_archive(source)
    stats = {"chats": 0, "messages": 0, "media": 0}
    with tarfile.open(source, "r:gz") as archive:
        members = _validate_members(archive)
        _read_manifest(archive, members)

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

    # A mídia também é extraída em ordem linear para escalar a arquivos grandes.
    with tarfile.open(source, "r:gz") as archive:
        for member in archive:
            name = member.name
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
                while chunk := extracted.read(CHUNK_SIZE):
                    handle.write(chunk)
            partial.replace(destination)
            stats["media"] += 1
    return stats

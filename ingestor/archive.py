import hashlib
import io
import json
import logging
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Chat, Message
from ingestor.media_store import ensure_shared_dir, share_path
from ingestor.thumbnails import thumbnail_relative
from ingestor.writer import refresh_chat_bounds, upsert_chats, upsert_messages

logger = logging.getLogger("wlav.archive")
CHUNK_SIZE = 4 * 1024 * 1024
ARCHIVE_FORMAT = "wlav-archive"
# v1 e v2 gravavam manifesto e checksums no FIM do tar. Como o gzip não permite
# saltos baratos, validar exigia uma leitura extra e restaurar pagava quatro
# varreduras do arquivo. A v3 coloca os dois no início, o que permite ler tudo
# em uma única passagem sequencial, verificando o SHA-256 de cada membro no
# momento em que ele é gravado.
CURRENT_VERSION = 3
SUPPORTED_VERSIONS = (1, 2, 3)
# A v1 não gravava hashes e a v2 aceitava arquivos sem hash; a partir da v3 todo
# membro tem de estar coberto por checksums.json.
CHECKSUM_OPTIONAL_BELOW = 3


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

        # A mídia é listada e validada antes de abrir o tar, para que o manifesto
        # gravado no início já descreva o conteúdo real do arquivo.
        media_entries: list[tuple[str, Path]] = []
        for relative_string in session.scalars(
            select(Message.media_path)
            .where(Message.media_path.is_not(None))
            .distinct()
            .order_by(Message.media_path)
        ):
            if not relative_string or relative_string in added_media:
                continue
            relative = Path(relative_string)
            source = (media_root / relative).resolve()
            if not source.is_relative_to(media_root) or not source.is_file():
                stats["missing_media"] += 1
                logger.warning("Mídia ausente durante exportação: %s", relative_string)
                continue
            added_media.add(relative_string)
            media_entries.append((f"media/{relative.as_posix()}", source))

        # Os checksums são calculados antes de gravar o tar para que o arquivo
        # comece por manifesto e checksums. O custo de I/O é o mesmo de antes:
        # cada mídia é lida uma vez para o hash e uma vez pelo tar.
        thumbnail_names: list[str] = []
        for archive_name, source in media_entries:
            checksums[archive_name] = _sha256_file(source)
            stats["media"] += 1
            relative_string = PurePosixPath(archive_name).relative_to("media").as_posix()
            thumbnail = (media_root / thumbnail_relative(relative_string)).resolve()
            if thumbnail.is_relative_to(media_root) and thumbnail.is_file():
                thumb_name = f"media/{thumbnail_relative(relative_string)}"
                checksums[thumb_name] = _sha256_file(thumbnail)
                thumbnail_names.append(thumb_name)
                stats["thumbnails"] += 1

        manifest = {
            "format": ARCHIVE_FORMAT,
            "version": CURRENT_VERSION,
            "created_at": datetime.now().astimezone().isoformat(),
            "stats": stats,
            "checksum": "sha256",
        }
        partial = output.with_name(output.name + ".part")
        try:
            with tarfile.open(partial, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                # Manifesto e checksums primeiro: quem restaura lê os dois no
                # começo e valida cada membro durante a única varredura seguinte.
                _add_bytes(
                    archive,
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
                )
                _add_bytes(
                    archive,
                    "checksums.json",
                    json.dumps(checksums, sort_keys=True, indent=2).encode(),
                )
                archive.add(chats_path, arcname="chats.jsonl", recursive=False)
                archive.add(messages_path, arcname="messages.jsonl", recursive=False)
                for archive_name, source in media_entries:
                    archive.add(source, arcname=archive_name, recursive=False)
                for thumb_name in thumbnail_names:
                    archive.add(media_root / PurePosixPath(thumb_name).relative_to("media"),
                                arcname=thumb_name, recursive=False)
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
    if manifest.get("format") != ARCHIVE_FORMAT or manifest.get("version") not in (
        SUPPORTED_VERSIONS
    ):
        raise ValueError("Formato ou versão de backup não suportado")
    return manifest


def _read_checksums(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo], version: int
) -> dict[str, str] | None:
    """Checksums esperados, ou None quando a versão não os exige."""
    if version < CHECKSUM_OPTIONAL_BELOW:
        return None
    if "checksums.json" not in members:
        raise ValueError("Backup sem checksums.json")
    checksum_file = archive.extractfile(members["checksums.json"])
    if checksum_file is None:
        raise ValueError("checksums.json ilegível")
    expected = json.load(checksum_file)
    if not isinstance(expected, dict):
        raise ValueError("checksums.json ilegível")
    data_members = {name for name, member in members.items() if member.isfile()} - {
        "manifest.json",
        "checksums.json",
    }
    if data_members != set(expected):
        raise ValueError("A lista de arquivos não corresponde aos checksums do backup")
    return expected


class _CheckedFile(io.RawIOBase):
    """Envolve um membro do tar e confere o SHA-256 ao alcançar o fim do fluxo.

    Calcular o hash durante a leitura evita uma segunda passagem: em um backup de
    dezenas de gigabytes, reler o arquivo do zero custaria o dobro de I/O.
    """

    def __init__(self, source, name: str, expected: str | None):
        self._source = source
        self._name = name
        self._expected = expected
        self._digest = hashlib.sha256()
        self._verified = False

    def readable(self) -> bool:
        return True

    def _verify(self) -> None:
        if self._verified:
            return
        self._verified = True
        if self._expected is not None and self._digest.hexdigest() != self._expected:
            raise ValueError(f"Falha de integridade em: {self._name}")

    def readinto(self, buffer) -> int:
        size = self._source.readinto(buffer)
        if size:
            self._digest.update(memoryview(buffer)[:size])
        else:
            self._verify()
        return size or 0

    def close(self) -> None:
        try:
            self._source.close()
        finally:
            self._verify()
            super().close()


def _open_checked(
    archive: tarfile.TarFile, member: tarfile.TarInfo, expected: str | None
) -> io.BufferedReader:
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"Não foi possível ler {member.name}")
    return io.BufferedReader(_CheckedFile(stream, member.name, expected))


def _drain(stream) -> int:
    total = 0
    while chunk := stream.read(CHUNK_SIZE):
        total += len(chunk)
    return total


def verify_archive(source: Path) -> dict[str, int]:
    source = source.resolve()
    with tarfile.open(source, "r:gz") as archive:
        members = _validate_members(archive)
        version = int(_read_manifest(archive, members)["version"])
        expected = _read_checksums(archive, members, version)
        if expected is None:
            # A v1 nunca gravou hashes; resta confirmar que o tar é legível.
            for member in archive:
                if member.isfile() and member.name not in {"manifest.json", "checksums.json"}:
                    _drain(_open_checked(archive, member, None))
            return {
                "files": len(members),
                "bytes": sum(item.size for item in members.values()),
            }
        total_bytes = 0
        for member in archive:
            if not member.isfile() or member.name in {"manifest.json", "checksums.json"}:
                continue
            _drain(_open_checked(archive, member, expected.get(member.name)))
            total_bytes += member.size
        return {"files": len(expected), "bytes": total_bytes}


def _json_lines(archive: tarfile.TarFile, member: tarfile.TarInfo):
    """Itera as linhas JSON de um membro do tar.

    O arquivo já foi validado por `verify_archive` antes de chegar aqui, então
    esta leitura é só decodificação: sem hash e sem segunda leitura.
    """
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
    """Restaura um backup portátil.

    São duas varreduras do arquivo: a primeira valida o arquivo inteiro e a
    segunda extrai. Antes da v3 eram quatro, porque o manifesto ficava no fim do
    tar e o gzip não permite saltar até ele. Mover o manifesto e os checksums para
    o início elimina a varredura extra sem abrir mão da garantia: nada é gravado
    no banco nem no volume de mídia antes de o arquivo inteiro passar pelos
    checksums.
    """
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

    # A mídia também é extraída em ordem linear, para escalar a arquivos grandes.
    with tarfile.open(source, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.startswith("media/"):
                continue
            relative = Path(PurePosixPath(member.name).relative_to("media"))
            destination = (media_root / relative).resolve()
            if not destination.is_relative_to(media_root):
                raise ValueError(f"Caminho de mídia inseguro: {member.name}")
            ensure_shared_dir(destination.parent)
            partial = destination.with_name(destination.name + ".part")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"Não foi possível ler {member.name}")
            with extracted, partial.open("wb") as handle:
                shutil.copyfileobj(extracted, handle, length=CHUNK_SIZE)
            share_path(partial)
            partial.replace(destination)
            stats["media"] += 1
    return stats

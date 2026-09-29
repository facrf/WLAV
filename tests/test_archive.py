"""Exportação, verificação e restauração de backups portáteis.

`ingestor/archive.py` não tinha nenhum teste. Como o arquivo é a única cópia de
segurança do projeto — e o formato passou a v3, com manifesto e checksums no
começo do tar — a cobertura aqui é o que garante que um backup continua
restaurável depois dessa mudança.
"""

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.app.models import Chat, Message
from ingestor.archive import (
    ARCHIVE_FORMAT,
    CURRENT_VERSION,
    _CheckedFile,
    export_archive,
    restore_archive,
    verify_archive,
)
from tests.support import requires_database

pytestmark = requires_database

PHOTO = "media/chat@s.whatsapp.net/2026_09/foto.jpg"
THUMB = "media/chat@s.whatsapp.net/2026_09/.thumbs/foto.jpg.jpg"


def _reset(migrated_database_url: str) -> tuple[Session, object]:
    engine = create_engine(migrated_database_url)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE messages, chats, import_jobs CASCADE"))
    return Session(engine), engine


def _seed(session: Session, media_root: Path) -> None:
    photo = media_root / "chat@s.whatsapp.net" / "2026_09" / "foto.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"conteudo-da-foto")
    thumb = photo.parent / ".thumbs" / "foto.jpg.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"preview")

    session.add(
        Chat(
            jid="chat@s.whatsapp.net",
            name="Ana",
            is_group=False,
            created_at="2026-09-01T12:00:00+00:00",
            last_message_time="2026-09-01T12:00:00+00:00",
        )
    )
    session.add(
        Message(
            id="m1",
            chat_jid="chat@s.whatsapp.net",
            sender_name="Ana",
            content="olá",
            timestamp="2026-09-01T12:00:00+00:00",
            from_me=False,
            has_media=True,
            media_type="image",
            media_path="chat@s.whatsapp.net/2026_09/foto.jpg",
        )
    )
    session.commit()


def _export(tmp_path: Path, migrated_database_url: str) -> Path:
    session, engine = _reset(migrated_database_url)
    media_root = tmp_path / "media"
    media_root.mkdir(exist_ok=True)
    _seed(session, media_root)
    output = tmp_path / "backup.wlav.tar.gz"
    export_archive(session, media_root, output)
    session.close()
    engine.dispose()
    return output


def _rebuild(source: Path, destination: Path, *, payloads: dict[str, bytes] | None = None,
             renames: dict[str, str] | None = None, extra: dict[str, bytes] | None = None) -> None:
    """Recria o tar.gz alterando membros, preservando ordem e checksums originais.

    Simula os casos que o exportador nunca produz e que o leitor precisa barrar:
    conteúdo adulterado (o hash não bate com o `checksums.json`), nome de membro
    apontando para fora do volume, e membro extra que ninguém assinou. O
    `checksums.json` é copiado byte a byte do original — se fosse recalculado, o
    arquivo adulterado seria indistinguível de um backup legítimo.
    """
    payloads = payloads or {}
    renames = renames or {}
    with tarfile.open(source, "r:gz") as archive:
        original = archive.getmembers()
        contents = {
            info.name: archive.extractfile(info).read() if info.isfile() else b""
            for info in original
        }
    with tarfile.open(destination, "w:gz") as out:
        for info in original:
            data = payloads.get(info.name, contents[info.name])
            entry = tarfile.TarInfo(name=renames.get(info.name, info.name))
            entry.size = len(data)
            entry.mtime = 0
            out.addfile(entry, io.BytesIO(data))
        for name, data in (extra or {}).items():
            entry = tarfile.TarInfo(name=name)
            entry.size = len(data)
            entry.mtime = 0
            out.addfile(entry, io.BytesIO(data))


def test_export_writes_manifest_and_checksums_first(tmp_path, migrated_database_url):
    """A v3 põe os metadados no início do tar; é isso que permite restaurar em
    uma única varredura, sem reler o arquivo só para validar."""
    output = _export(tmp_path, migrated_database_url)

    with tarfile.open(output, "r:gz") as archive:
        names = archive.getnames()
        assert names[0] == "manifest.json"
        assert names[1] == "checksums.json"
        assert len(names) == len(set(names)), "membros duplicados no tar"
        manifest = json.load(archive.extractfile("manifest.json"))
        checksums = json.load(archive.extractfile("checksums.json"))

    assert manifest["format"] == ARCHIVE_FORMAT
    assert manifest["version"] == CURRENT_VERSION
    assert manifest["stats"] == {
        "chats": 1,
        "messages": 1,
        "media": 1,
        "thumbnails": 1,
        "missing_media": 0,
    }
    assert set(checksums) == {"chats.jsonl", "messages.jsonl", PHOTO, THUMB}


def test_verify_counts_every_checked_member(tmp_path, migrated_database_url):
    output = _export(tmp_path, migrated_database_url)

    report = verify_archive(output)

    assert report["files"] == 4
    assert report["bytes"] > 0


@pytest.mark.parametrize("member", ["chats.jsonl", PHOTO])
def test_restore_refuses_a_tampered_member(tmp_path, migrated_database_url, member):
    """A validação precisa barrar a restauração, e não apenas relatar depois."""
    original = _export(tmp_path, migrated_database_url)
    tampered = tmp_path / "adulterado.wlav.tar.gz"
    _rebuild(original, tampered, payloads={member: b"conteudo alterado"})

    with pytest.raises(ValueError, match="integridade"):
        verify_archive(tampered)

    target = tmp_path / "restaurado"
    target.mkdir()
    with (
        Session(create_engine(migrated_database_url)) as restore_session,
        pytest.raises(ValueError, match="integridade"),
    ):
        restore_archive(restore_session, target, tampered)
    # A garantia do README: nada é gravado antes de o arquivo inteiro validar.
    assert not list(target.rglob("*.jpg"))
    assert not list(target.rglob("*.jsonl"))


def test_restore_rejects_a_member_outside_the_volume(tmp_path, migrated_database_url):
    """Um nome com `..` é barrado na validação dos membros, antes de qualquer
    escrita — inclusive antes de o `checksums.json` ser comparado."""
    original = _export(tmp_path, migrated_database_url)
    escaped = tmp_path / "caminho-inseguro.tar.gz"
    _rebuild(original, escaped, renames={PHOTO: "../escapou.jpg"})

    target = tmp_path / "restaurado"
    target.mkdir()
    with (
        Session(create_engine(migrated_database_url)) as restore_session,
        pytest.raises(ValueError, match="insegura"),
    ):
        restore_archive(restore_session, target, escaped)
    assert not (tmp_path / "escapou.jpg").exists()


def test_restore_rejects_a_member_missing_from_the_checksums(tmp_path, migrated_database_url):
    """Repacotar o tar com um membro novo não pode passar: o inventário de
    `checksums.json` precisa bater com o conteúdo do arquivo."""
    original = _export(tmp_path, migrated_database_url)
    smuggled = tmp_path / "membro-extra.tar.gz"
    _rebuild(original, smuggled, extra={"media/injetado.jpg": b"arquivo que ninguem assinou"})

    with pytest.raises(ValueError, match="checksums"):
        verify_archive(smuggled)

    target = tmp_path / "restaurado"
    target.mkdir()
    with (
        Session(create_engine(migrated_database_url)) as restore_session,
        pytest.raises(ValueError, match="checksums"),
    ):
        restore_archive(restore_session, target, smuggled)
    assert not (target / "injetado.jpg").exists()


def test_restore_writes_chats_messages_and_media(tmp_path, migrated_database_url):
    original = _export(tmp_path, migrated_database_url)

    target = tmp_path / "restaurado"
    target.mkdir()
    with Session(create_engine(migrated_database_url)) as restore_session:
        stats = restore_archive(restore_session, target, original)

    assert stats == {"chats": 1, "messages": 1, "media": 2}
    photo = target / "chat@s.whatsapp.net" / "2026_09" / "foto.jpg"
    assert photo.read_bytes() == b"conteudo-da-foto"
    assert (photo.parent / ".thumbs" / "foto.jpg.jpg").exists()


def test_restore_is_idempotent(tmp_path, migrated_database_url):
    """Reaplicar o mesmo backup não pode duplicar linhas: a chave natural de
    `messages` é `id` e a de `chats` é `jid`."""
    original = _export(tmp_path, migrated_database_url)
    target = tmp_path / "restaurado"
    target.mkdir()

    for _ in range(2):
        with Session(create_engine(migrated_database_url)) as restore_session:
            restore_archive(restore_session, target, original)
        with Session(create_engine(migrated_database_url)) as check:
            assert check.execute(text("SELECT count(*) FROM messages")).scalar_one() == 1
            assert check.execute(text("SELECT count(*) FROM chats")).scalar_one() == 1


def test_checked_reader_verifies_even_if_not_read_to_the_end():
    """Ler só parte do fluxo também precisa acusar a divergência: o `close()` é o
    último ponto de checagem quando o consumidor para antes do EOF."""
    payload = b"a" * 32
    expected = hashlib.sha256(payload).hexdigest()

    partial = _CheckedFile(io.BytesIO(payload), "membro.bin", expected)
    assert partial.read(8) == b"a" * 8
    with pytest.raises(ValueError, match="integridade"):
        partial.close()

    complete = _CheckedFile(io.BytesIO(payload), "membro.bin", expected)
    assert complete.read() == payload
    complete.close()


def test_init_sql_does_not_duplicate_the_migration_schema():
    """`docker/init.sql` roda só na criação do cluster. Manter o DDL dele fazia o
    schema divergir da migração em silêncio, sem nenhum teste acusando."""
    init = (Path(__file__).resolve().parents[1] / "docker" / "init.sql").read_text()
    upper = init.upper()
    assert "CREATE TABLE" not in upper
    assert "CREATE INDEX" not in upper
    assert "PG_TRGM" in upper

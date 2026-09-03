import zipfile

import pytest

from ingestor.upload import prepare_import_upload, prepare_whatsapp_upload


def test_prepares_zip_with_sqlite_and_media(tmp_path):
    source_db = tmp_path / "source.db"
    source_db.write_bytes(b"SQLite format 3\x00" + b"fixture")
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"photo")
    package = tmp_path / "backup.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.write(source_db, "WhatsApp/msgstore.db")
        archive.write(media, "WhatsApp/Media/WhatsApp Images/photo.jpg")

    database, media_root = prepare_whatsapp_upload(package, tmp_path / "extracted", 1024 * 1024)
    assert database.name == "msgstore.db"
    assert media_root is not None
    assert (media_root / "WhatsApp/Media/WhatsApp Images/photo.jpg").read_bytes() == b"photo"


def test_rejects_zip_traversal(tmp_path):
    package = tmp_path / "bad.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../outside/msgstore.db", b"SQLite format 3\x00")

    with pytest.raises(ValueError, match="Caminho inseguro"):
        prepare_whatsapp_upload(package, tmp_path / "extracted", 1024 * 1024)


def test_prepares_official_export_zip_with_media(tmp_path):
    package = tmp_path / "WhatsApp Chat with Ana.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "_chat.txt",
            "[01/09/2026, 10:00:00] Ana: <attached: photo.jpg>\n",
        )
        archive.writestr("photo.jpg", b"photo")

    prepared = prepare_import_upload(package, tmp_path / "extracted", 1024 * 1024)

    assert prepared.kind == "chat_export"
    assert [path.name for path in prepared.transcripts] == ["_chat.txt"]
    assert (prepared.media_root / "photo.jpg").read_bytes() == b"photo"


def test_prepares_encrypted_database_in_whatsapp_directory(tmp_path):
    whatsapp = tmp_path / "WhatsApp"
    databases = whatsapp / "Databases"
    databases.mkdir(parents=True)
    (databases / "msgstore.db.crypt15").write_bytes(b"encrypted")

    prepared = prepare_import_upload(whatsapp, tmp_path / "extracted", 1024 * 1024)

    assert prepared.kind == "encrypted"
    assert prepared.encrypted_database.name == "msgstore.db.crypt15"


def test_prepares_txt_after_spool_removed_original_extension(tmp_path):
    upload = tmp_path / "source.upload"
    upload.write_text("01/09/2026 10:00 - Ana: Oi\n", encoding="utf-8")

    prepared = prepare_import_upload(
        upload,
        tmp_path / "extracted",
        1024 * 1024,
        original_filename="Conversa do WhatsApp com Ana.txt",
    )

    assert prepared.kind == "chat_export"
    assert prepared.transcripts[0].name == "Conversa do WhatsApp com Ana.txt"


def test_prepares_direct_encrypted_database_using_original_filename(tmp_path):
    upload = tmp_path / "source.upload"
    upload.write_bytes(b"encrypted")

    prepared = prepare_import_upload(
        upload,
        tmp_path / "extracted",
        1024 * 1024,
        original_filename="msgstore.db.crypt15",
    )

    assert prepared.kind == "encrypted"
    assert prepared.encrypted_database == upload

import zipfile

import pytest

from ingestor.upload import prepare_whatsapp_upload


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

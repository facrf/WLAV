import pytest

from ingestor.encryption import decrypt_file, encrypt_file, is_encrypted


def test_encryption_round_trip_and_wrong_password(tmp_path):
    source = tmp_path / "source.tar.gz"
    encrypted = tmp_path / "backup.wlavenc"
    restored = tmp_path / "restored.tar.gz"
    source.write_bytes((b"arquivo confidencial\x00" * 10_000) + b"fim")

    encrypt_file(source, encrypted, "uma-senha-bem-forte")
    assert is_encrypted(encrypted)
    decrypt_file(encrypted, restored, "uma-senha-bem-forte")
    assert restored.read_bytes() == source.read_bytes()

    with pytest.raises(ValueError, match="Senha incorreta|corrompido"):
        decrypt_file(encrypted, tmp_path / "wrong", "senha-incorreta-123")


def test_encryption_rejects_short_password(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with pytest.raises(ValueError, match="12 caracteres"):
        encrypt_file(source, tmp_path / "output", "curta")

import sqlite3
import subprocess

from ingestor.whatsapp_crypt import decrypt_whatsapp_database


def test_decrypts_with_key_path_without_putting_secret_in_arguments(tmp_path, monkeypatch):
    encrypted = tmp_path / "msgstore.db.crypt15"
    encrypted.write_bytes(b"encrypted")
    key_path = tmp_path / "whatsapp.key"
    secret = "0123456789abcdef" * 4
    key_path.write_text(secret, encoding="ascii")
    destination = tmp_path / "decrypted" / "msgstore.db"

    def fake_run(arguments, **_kwargs):
        assert arguments[1] == str(key_path)
        assert secret not in arguments
        output = arguments[3]
        connection = sqlite3.connect(output)
        connection.execute("CREATE TABLE message (_id INTEGER)")
        connection.close()
        return subprocess.CompletedProcess(arguments, 0, stdout="Done")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = decrypt_whatsapp_database(encrypted, destination, key_path)

    assert result == destination
    assert destination.stat().st_mode & 0o777 == 0o600


def test_rejects_failed_decryption_without_leaving_plaintext(tmp_path, monkeypatch):
    encrypted = tmp_path / "msgstore.db.crypt15"
    encrypted.write_bytes(b"encrypted")
    key_path = tmp_path / "whatsapp.key"
    key_path.write_text("0" * 64, encoding="ascii")
    destination = tmp_path / "msgstore.db"

    def fake_run(arguments, **_kwargs):
        destination.write_bytes(b"partial")
        return subprocess.CompletedProcess(arguments, 1, stdout="wrong key")

    monkeypatch.setattr(subprocess, "run", fake_run)

    try:
        decrypt_whatsapp_database(encrypted, destination, key_path)
    except ValueError as exc:
        assert "Não foi possível descriptografar" in str(exc)
    else:
        raise AssertionError("Falha de descriptografia deveria ser propagada")
    assert not destination.exists()

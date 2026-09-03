from ingestor.key_store import WhatsAppKeyStore, normalize_whatsapp_key


def test_saves_key_with_restricted_permissions_and_returns_only_fingerprint(tmp_path):
    path = tmp_path / "secrets" / "whatsapp.key"
    store = WhatsAppKeyStore(path)
    raw = "01234567 89abcdef 01234567 89abcdef 01234567 89abcdef 01234567 89abcdef"

    status = store.save(raw)

    assert status.saved
    assert status.fingerprint
    assert len(status.fingerprint) == 12
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.load() == "0123456789abcdef" * 4
    assert store.delete()
    assert not store.status().saved


def test_rejects_invalid_key():
    try:
        normalize_whatsapp_key("1234")
    except ValueError as exc:
        assert "64 caracteres" in str(exc)
    else:
        raise AssertionError("Chave curta deveria ser recusada")

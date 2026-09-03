from datetime import UTC, datetime

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.database import get_session
from ingestor.key_store import WhatsAppKeyStore


class FakeSession:
    def add(self, job):
        self.job = job

    def commit(self):
        self.job.chats_processed = 0
        self.job.messages_processed = 0
        self.job.media_copied = 0
        self.job.media_missing = 0
        self.job.created_at = datetime.now(UTC)

    def refresh(self, _job):
        return None


def test_import_endpoint_preserves_selected_directory_paths(tmp_path, monkeypatch):
    session = FakeSession()

    def fake_session():
        yield session

    main.app.dependency_overrides[get_session] = fake_session
    monkeypatch.setattr(main.settings, "import_root", tmp_path)
    monkeypatch.setattr(main, "run_import_job", lambda *_: None)
    try:
        response = TestClient(main.app).post(
            "/api/imports",
            files=[
                (
                    "file",
                    (
                        "WhatsApp/Conversa do WhatsApp com Ana.txt",
                        b"01/09/2026 10:00 - Ana: Oi\n",
                        "text/plain",
                    ),
                ),
                ("file", ("WhatsApp/Media/photo.jpg", b"photo", "image/jpeg")),
            ],
            data={"owner_name": "Eu", "date_order": "dmy"},
        )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 202
    job_id = response.json()["id"]
    source = tmp_path / job_id / "source.directory" / "WhatsApp"
    assert (source / "Conversa do WhatsApp com Ana.txt").is_file()
    assert (source / "Media/photo.jpg").read_bytes() == b"photo"
    assert (tmp_path / job_id / "options.json").read_text(encoding="utf-8") == (
        '{"owner_name": "Eu", "date_order": "dmy"}'
    )


def test_key_api_never_returns_secret(tmp_path, monkeypatch):
    secret = "0123456789abcdef" * 4
    monkeypatch.setattr(
        main,
        "whatsapp_key_store",
        WhatsAppKeyStore(tmp_path / "secrets" / "whatsapp.key"),
    )
    client = TestClient(main.app)

    saved = client.put("/api/settings/whatsapp-key", json={"key": secret})
    status = client.get("/api/settings/whatsapp-key")

    assert saved.status_code == 200
    assert status.status_code == 200
    assert saved.json()["saved"]
    assert secret not in saved.text
    assert secret not in status.text
    assert client.delete("/api/settings/whatsapp-key").status_code == 204

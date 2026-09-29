from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from backend.app import main
from backend.app.database import get_session
from ingestor.key_store import WhatsAppKeyStore
from tests.support import requires_database


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


# --- Limites de upload -------------------------------------------------------
#
# O README documenta o envio de uma pasta Media/ inteira, que numa instalação
# real passa de mil arquivos. O padrão do Starlette recusaria com 400 e o
# usuário não teria como contornar.


@requires_database
def test_upload_accepts_more_than_a_thousand_files(client, monkeypatch):
    """Regressão do limite de 1000 arquivos do parser multipart.

    Uma pasta WhatsApp copiada do telefone tem facilmente dezenas de milhares de
    arquivos; recusar em 1000 tornava o fluxo documentado impossível.
    """
    # Sem este stub o job real roda em background e apaga o spool no `finally`.
    monkeypatch.setattr(main, "run_import_job", lambda *_: None)
    files = [
        ("file", (f"Media/WhatsApp Images/foto{index:05}.jpg", b"x", "image/jpeg"))
        for index in range(1_500)
    ]

    response = client.post("/api/imports", files=files)

    assert response.status_code == 202, response.text
    assert response.json()["filename"] == "Media/ (1500 arquivos)"
    job_id = response.json()["id"]
    stored = main.settings.import_root / job_id / "source.directory" / "Media" / "WhatsApp Images"
    assert len(list(stored.iterdir())) == 1_500


@requires_database
def test_upload_respects_the_configured_file_limit(client, monkeypatch):
    """O limite é configurável, porque `IMPORT_MAX_FILES` também protege contra
    um envio acidental de uma pasta com centenas de milhares de arquivos."""
    monkeypatch.setattr(main.settings, "import_max_files", 3)

    response = client.post(
        "/api/imports",
        files=[("file", (f"Media/{index}.jpg", b"x", "image/jpeg")) for index in range(4)],
    )

    assert response.status_code == 400
    assert "arquivos" in response.json()["detail"] or "files" in response.json()["detail"]


@requires_database
@pytest.mark.parametrize("filename", ["../escape.txt", "/etc/passwd", "a/../../escape.txt"])
def test_upload_rejects_a_path_traversal_filename_with_400(client, filename):
    """O nome vem do navegador, mas um cliente hostil pode mandar o que quiser.
    Antes isso encerrava com 500, sem dizer ao usuário o que estava errado."""
    response = client.post("/api/imports", files=[("file", (filename, b"x", "text/plain"))])

    assert response.status_code == 400, response.text
    assert response.json()["detail"]


@requires_database
def test_upload_rejects_duplicate_targets_with_400(client):
    """Dois nomes que colidem depois da limpeza viram o mesmo arquivo; aceitar
    faria o segundo sobrescrever o primeiro silenciosamente."""
    response = client.post(
        "/api/imports",
        files=[
            ("file", ("Media/foto.jpg", b"primeiro", "image/jpeg")),
            ("file", ("Media/foto.jpg", b"segundo", "image/jpeg")),
        ],
    )

    assert response.status_code == 400
    assert "duplicado" in response.json()["detail"]


@requires_database
def test_upload_over_the_size_limit_is_rejected_and_leaves_nothing_behind(client, monkeypatch):
    import_root = main.settings.import_root
    monkeypatch.setattr(main.settings, "upload_max_gb", 1)
    payload = b"z" * (1024 * 1024)
    files = [("file", (f"Media/{index}.bin", payload, "application/octet-stream"))
             for index in range(1200)]

    response = client.post("/api/imports", files=files)

    assert response.status_code == 413
    # O spool é apagado no caminho de erro: um envio recusado não pode deixar
    # gigabytes ocupando o volume.
    assert list(import_root.iterdir()) == []


@requires_database
def test_upload_without_files_is_422(client):
    assert client.post("/api/imports", data={"date_order": "auto"}).status_code == 422


@requires_database
def test_upload_rejects_an_unknown_date_order(client):
    response = client.post(
        "/api/imports",
        files=[("file", ("Media/foto.jpg", b"x", "image/jpeg"))],
        data={"date_order": "yyyymmdd"},
    )

    assert response.status_code == 422


# --- Recuperação no arranque --------------------------------------------------


@requires_database
def test_startup_removes_spool_directories_without_a_job(client):
    """Um contêiner morto no meio do upload deixa diretório no volume sem linha
    no banco. Sem a limpeza no arranque, o disco encheria aos poucos."""
    orphan = main.settings.import_root / ("0" * 32)
    orphan.mkdir(parents=True)
    (orphan / "source.upload").write_bytes(b"conteudo")
    also_orphan = main.settings.import_root / "nao-e-uuid"
    also_orphan.mkdir()

    recovered = main._recover_imports()

    assert recovered == []
    assert not orphan.exists()
    assert not also_orphan.exists()


@requires_database
def test_startup_fails_a_job_whose_spool_disappeared(client, session):
    """A linha no banco sobrevive ao volume em alguns cenários; sem marcar como
    falha, o job ficaria eternamente em `running` e a tela de importações nunca
    voltaria ao normal."""
    from backend.app.models import ImportJob

    session.add(ImportJob(id="a" * 32, filename="backup.txt", source_sha256="0" * 64,
                          status="running"))
    session.commit()

    recovered = main._recover_imports()

    assert recovered == []
    job = client.get(f"/api/imports/{'a' * 32}")
    assert job.status_code == 200
    assert job.json()["status"] == "failed"
    assert "temporário ausente" in job.json()["error"]


@requires_database
def test_startup_keeps_and_resumes_a_job_that_still_has_its_spool(client, session):
    from backend.app.models import ImportJob

    job_id = "b" * 32
    spool = main.settings.import_root / job_id
    spool.mkdir(parents=True)
    (spool / "source.upload").write_bytes(b"conteudo")
    session.add(ImportJob(id=job_id, filename="backup.txt", source_sha256="0" * 64,
                          status="queued"))
    session.commit()

    recovered = main._recover_imports()

    assert recovered == [job_id]
    assert spool.exists()


@requires_database
def test_startup_cleans_the_spool_of_a_finished_job(client, session):
    from backend.app.models import ImportJob

    job_id = "c" * 32
    spool = main.settings.import_root / job_id
    spool.mkdir(parents=True)
    (spool / "source.upload").write_bytes(b"conteudo")
    session.add(
        ImportJob(id=job_id, filename="backup.txt", source_sha256="0" * 64, status="completed")
    )
    session.commit()

    assert main._recover_imports() == []
    assert not spool.exists()


# --- Serialização ------------------------------------------------------------


@requires_database
def test_imports_never_run_at_the_same_time(client, monkeypatch):
    """Duas importações simultâneas duplicariam CPU, memória e I/O de disco sem
    ganho nenhum, já que o UPSERT torna reimportar seguro."""
    import threading
    import time

    concurrent = 0
    peak = 0
    lock = threading.Lock()

    def fake_run(job_id, _upload_path):
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        time.sleep(0.05)
        with lock:
            concurrent -= 1

    monkeypatch.setattr(main, "run_import_job", fake_run)

    for _ in range(3):
        response = client.post(
            "/api/imports", files=[("file", ("Media/foto.jpg", b"x", "image/jpeg"))]
        )
        assert response.status_code == 202

    # O TestClient roda as BackgroundTasks até o fim da requisição; como o
    # semáforo é assíncrono, as três precisam passar pela mesma espera.
    time.sleep(0.3)
    assert peak <= 1, f"importações concorrentes: {peak}"


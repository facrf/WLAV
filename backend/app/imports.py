import json
import logging
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.database import SessionLocal, engine
from backend.app.models import ImportJob
from ingestor.archive import restore_archive
from ingestor.key_store import WhatsAppKeyStore
from ingestor.service import ImportStats, ingest_chat_exports, ingest_sqlite
from ingestor.upload import prepare_import_upload
from ingestor.whatsapp_crypt import decrypt_whatsapp_database

logger = logging.getLogger("wlav.imports")
settings = get_settings()


def _update_job(job_id: str, **values) -> None:
    with SessionLocal() as session:
        job = session.get(ImportJob, job_id)
        if job is None:
            return
        for key, value in values.items():
            setattr(job, key, value)
        session.commit()


def _progress(job_id: str, stats: ImportStats) -> None:
    _update_job(
        job_id,
        source_schema=stats.schema,
        chats_processed=stats.chats,
        messages_processed=stats.messages,
        media_copied=stats.media_copied,
        media_missing=stats.media_missing,
    )


def _is_wlav_archive(path: Path) -> bool:
    if not path.is_file() or not tarfile.is_tarfile(path):
        return False
    try:
        with tarfile.open(path, "r:*") as archive:
            manifest = archive.extractfile("manifest.json")
            return bool(manifest and json.load(manifest).get("format") == "wlav-archive")
    except (KeyError, json.JSONDecodeError, tarfile.TarError):
        return False


def run_import_job(job_id: str, upload_path: Path) -> None:
    work_directory = upload_path.parent
    _update_job(job_id, status="running", started_at=datetime.now(UTC), error=None)
    try:
        with SessionLocal() as job_session:
            job = job_session.get(ImportJob, job_id)
            original_filename = job.filename if job else None
        options_path = work_directory / "options.json"
        options = (
            json.loads(options_path.read_text(encoding="utf-8")) if options_path.is_file() else {}
        )
        if _is_wlav_archive(upload_path):
            with Session(engine) as session:
                try:
                    stats = restore_archive(session, settings.media_root, upload_path)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
            _update_job(
                job_id,
                source_schema="wlav_archive",
                chats_processed=stats["chats"],
                messages_processed=stats["messages"],
                media_copied=stats["media"],
            )
        else:
            prepared = prepare_import_upload(
                upload_path,
                work_directory / "extracted",
                settings.upload_max_gb * 1024**3,
                original_filename=original_filename,
            )
            if prepared.kind == "chat_export":
                stats = ingest_chat_exports(
                    list(prepared.transcripts),
                    prepared.media_root,
                    settings.media_root,
                    engine,
                    owner_name=options.get("owner_name") or None,
                    date_order=options.get("date_order", "auto"),
                    timezone=settings.whatsapp_export_timezone,
                    fallback_title=original_filename if upload_path.is_file() else None,
                    thumbnail_size=settings.thumbnail_max_size,
                    progress=lambda current: _progress(job_id, current),
                )
            else:
                database = prepared.database
                if prepared.kind == "encrypted":
                    key_store = WhatsAppKeyStore(settings.whatsapp_key_path)
                    key_store.load()
                    if prepared.encrypted_database is None:
                        raise ValueError("Backup criptografado não encontrado na origem preparada")
                    database = decrypt_whatsapp_database(
                        prepared.encrypted_database,
                        work_directory / "decrypted" / "msgstore.db",
                        settings.whatsapp_key_path,
                        command=settings.whatsapp_decrypt_command,
                    )
                if database is None:
                    raise ValueError("Banco SQLite não encontrado na origem preparada")
                stats = ingest_sqlite(
                    database,
                    prepared.media_root,
                    settings.media_root,
                    engine,
                    thumbnail_size=settings.thumbnail_max_size,
                    progress=lambda current: _progress(job_id, current),
                )
            _progress(job_id, stats)
        _update_job(job_id, status="completed", completed_at=datetime.now(UTC))
    except Exception as exc:
        logger.exception("Falha na importação %s", job_id)
        _update_job(
            job_id,
            status="failed",
            error=str(exc)[:4_000],
            completed_at=datetime.now(UTC),
        )
    finally:
        shutil.rmtree(work_directory, ignore_errors=True)

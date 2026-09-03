import logging
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ingestor.archive import export_archive, verify_archive
from ingestor.encryption import decrypt_file, encrypt_file, is_encrypted

logger = logging.getLogger("wlav.backup")


def verify_portable_backup(path: Path, password: str | None = None) -> dict[str, int]:
    if not is_encrypted(path):
        return verify_archive(path)
    if not password:
        raise ValueError("Este backup é cifrado; forneça a senha")
    with tempfile.TemporaryDirectory(prefix="wlav-verify-") as temporary:
        decrypted = Path(temporary) / "backup.tar.gz"
        decrypt_file(path, decrypted, password)
        return verify_archive(decrypted)


def create_scheduled_backup(
    engine: Engine,
    media_root: Path,
    output_dir: Path,
    encrypt: bool,
    password: str | None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    suffix = ".wlavenc" if encrypt else ".tar.gz"
    output = output_dir / f"wlav-auto-{timestamp}{suffix}"
    with tempfile.TemporaryDirectory(prefix=".wlav-backup-", dir=output_dir) as temporary:
        archive = Path(temporary) / "backup.tar.gz"
        with Session(engine) as session:
            export_archive(session, media_root, archive)
        verify_archive(archive)
        if encrypt:
            if not password:
                raise ValueError("Backup automático cifrado requer senha")
            encrypt_file(archive, output, password)
        else:
            archive.replace(output)
    verify_portable_backup(output, password)
    logger.info("Backup criado e verificado: %s", output)
    return output


def apply_retention(output_dir: Path, retention_days: int) -> int:
    cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
    removed = 0
    for pattern in ("wlav-auto-*.tar.gz", "wlav-auto-*.wlavenc"):
        for path in output_dir.glob(pattern):
            modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            if path.is_file() and modified < cutoff:
                path.unlink()
                removed += 1
                logger.info("Backup removido pela retenção: %s", path.name)
    return removed


def backup_loop(
    engine: Engine,
    media_root: Path,
    output_dir: Path,
    interval_hours: float,
    retention_days: int,
    encrypt: bool,
    password: str | None,
) -> None:
    while True:
        try:
            create_scheduled_backup(engine, media_root, output_dir, encrypt, password)
            removed = apply_retention(output_dir, retention_days)
            logger.info("Retenção concluída; %d backup(s) removido(s)", removed)
        except Exception:
            logger.exception("Falha no ciclo de backup; nova tentativa no próximo intervalo")
        time.sleep(interval_hours * 3600)

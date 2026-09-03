import argparse
import getpass
import logging
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from ingestor.archive import export_archive, restore_archive
from ingestor.encryption import decrypt_file, encrypt_file, is_encrypted
from ingestor.scheduler import backup_loop, verify_portable_backup
from ingestor.service import ingest_sqlite
from ingestor.sqlite_reader import UnsupportedSchemaError
from ingestor.thumbnails import Thumbnailer, rebuild_thumbnails

logger = logging.getLogger("wlav.ingestor")


def _database_url(args: argparse.Namespace) -> str:
    return args.database_url or get_settings().database_url


def _engine(args: argparse.Namespace):
    return create_engine(_database_url(args), pool_pre_ping=True)


def _password(args: argparse.Namespace, required: bool) -> str | None:
    password_file = getattr(args, "password_file", None) or os.getenv("WLAV_BACKUP_PASSWORD_FILE")
    if password_file:
        password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
    else:
        password = os.getenv("WLAV_BACKUP_PASSWORD")
    if not password and required and sys.stdin.isatty():
        password = getpass.getpass("Senha do backup: ")
    if required and not password:
        raise ValueError(
            "Forneça --password-file ou WLAV_BACKUP_PASSWORD_FILE para o backup cifrado"
        )
    return password


def ingest(args: argparse.Namespace) -> int:
    stats = ingest_sqlite(
        Path(args.sqlite),
        Path(args.media_dir) if args.media_dir else None,
        Path(args.media_root or get_settings().media_root),
        None if args.dry_run else _engine(args),
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        thumbnail_size=args.thumbnail_size,
    )
    mode = "SIMULAÇÃO" if args.dry_run else "CONCLUÍDO"
    print(
        f"{mode}: {stats.chats} chats; {stats.messages} mensagens; "
        f"{stats.media_copied} mídias localizadas; {stats.media_missing} ausentes; "
        f"{stats.thumbnails} thumbnails."
    )
    return 0


def run_export(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.encrypt:
        password = _password(args, required=True)
        with tempfile.TemporaryDirectory(prefix=".wlav-export-", dir=output.parent) as temporary:
            archive = Path(temporary) / "backup.tar.gz"
            with Session(_engine(args)) as session:
                stats = export_archive(session, Path(args.media_root), archive)
            encrypt_file(archive, output, password or "")
    else:
        with Session(_engine(args)) as session:
            stats = export_archive(session, Path(args.media_root), output)
    print(
        f"EXPORTADO: {stats['chats']} chats; {stats['messages']} mensagens; "
        f"{stats['media']} mídias; {stats['thumbnails']} thumbnails "
        f"({stats['missing_media']} ausentes)."
    )
    return 0


def _decrypted_source(args: argparse.Namespace, source: Path, temporary: Path) -> Path:
    if not is_encrypted(source):
        return source
    password = _password(args, required=True)
    decrypted = temporary / "backup.tar.gz"
    decrypt_file(source, decrypted, password or "")
    return decrypted


def run_restore(args: argparse.Namespace) -> int:
    source = Path(args.archive).resolve()
    with tempfile.TemporaryDirectory(prefix=".wlav-restore-", dir=source.parent) as temporary:
        archive = _decrypted_source(args, source, Path(temporary))
        with Session(_engine(args)) as session:
            try:
                stats = restore_archive(session, Path(args.media_root), archive, args.batch_size)
                session.commit()
            except Exception:
                session.rollback()
                raise
    print(
        f"RESTAURADO: {stats['chats']} chats; {stats['messages']} mensagens; "
        f"{stats['media']} arquivos de mídia/thumbnail."
    )
    return 0


def run_verify(args: argparse.Namespace) -> int:
    password = _password(args, required=is_encrypted(Path(args.archive)))
    stats = verify_portable_backup(Path(args.archive), password)
    print(f"ÍNTEGRO: {stats['files']} arquivos; {stats['bytes']} bytes verificados.")
    return 0


def run_thumbnails(args: argparse.Namespace) -> int:
    thumbnailer = Thumbnailer(Path(args.media_root), args.thumbnail_size)
    with Session(_engine(args)) as session:
        stats = rebuild_thumbnails(session, thumbnailer)
    print(f"THUMBNAILS: {stats['generated']} gerados/reutilizados; {stats['skipped']} ignorados.")
    return 0


def run_backup_loop(args: argparse.Namespace) -> int:
    password = _password(args, required=args.encrypt)
    backup_loop(
        _engine(args),
        Path(args.media_root),
        Path(args.output_dir),
        args.interval_hours,
        args.retention_days,
        args.encrypt,
        password,
    )
    return 0


def _add_database_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", help="URL SQLAlchemy do PostgreSQL")


def _add_password_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--password-file",
        help="Arquivo texto com a senha; alternativa: WLAV_BACKUP_PASSWORD_FILE",
    )


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="wlav",
        description="Importação e portabilidade do WhatsApp Local Archive & Viewer",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser(
        "ingest", help="Importa msgstore.db ou ChatStorage.sqlite"
    )
    ingest_parser.add_argument("--sqlite", required=True, help="SQLite descriptografado")
    ingest_parser.add_argument("--media-dir", help="Raiz das mídias exportadas do WhatsApp")
    ingest_parser.add_argument("--media-root", default=str(settings.media_root))
    ingest_parser.add_argument("--batch-size", type=int, default=1_000)
    ingest_parser.add_argument("--thumbnail-size", type=int, default=settings.thumbnail_max_size)
    ingest_parser.add_argument(
        "--dry-run", action="store_true", help="Lê e valida sem alterar banco ou mídias"
    )
    _add_database_option(ingest_parser)
    ingest_parser.set_defaults(handler=ingest)

    export_parser = subparsers.add_parser("export", help="Cria backup portátil")
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument("--media-root", default=str(settings.media_root))
    export_parser.add_argument("--encrypt", action="store_true", help="Cifra com AES-256-GCM")
    _add_password_option(export_parser)
    _add_database_option(export_parser)
    export_parser.set_defaults(handler=run_export)

    restore_parser = subparsers.add_parser("restore", help="Restaura backup portátil")
    restore_parser.add_argument("--archive", required=True)
    restore_parser.add_argument("--media-root", default=str(settings.media_root))
    restore_parser.add_argument("--batch-size", type=int, default=1_000)
    _add_password_option(restore_parser)
    _add_database_option(restore_parser)
    restore_parser.set_defaults(handler=run_restore)

    verify_parser = subparsers.add_parser("verify", help="Verifica integridade do backup")
    verify_parser.add_argument("--archive", required=True)
    _add_password_option(verify_parser)
    verify_parser.set_defaults(handler=run_verify)

    thumbnail_parser = subparsers.add_parser("thumbnails", help="Gera previews ausentes")
    thumbnail_parser.add_argument("--media-root", default=str(settings.media_root))
    thumbnail_parser.add_argument("--thumbnail-size", type=int, default=settings.thumbnail_max_size)
    _add_database_option(thumbnail_parser)
    thumbnail_parser.set_defaults(handler=run_thumbnails)

    schedule_parser = subparsers.add_parser(
        "backup-loop", help="Executa backup periódico com retenção"
    )
    schedule_parser.add_argument("--output-dir", required=True)
    schedule_parser.add_argument("--media-root", default=str(settings.media_root))
    schedule_parser.add_argument("--interval-hours", type=float, default=24)
    schedule_parser.add_argument("--retention-days", type=int, default=30)
    schedule_parser.add_argument("--encrypt", action="store_true")
    _add_password_option(schedule_parser)
    _add_database_option(schedule_parser)
    schedule_parser.set_defaults(handler=run_backup_loop)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "batch_size", 1) < 1:
        parser.error("--batch-size deve ser maior que zero")
    if getattr(args, "interval_hours", 1) <= 0:
        parser.error("--interval-hours deve ser maior que zero")
    if getattr(args, "retention_days", 1) < 1:
        parser.error("--retention-days deve ser maior que zero")
    try:
        raise SystemExit(args.handler(args))
    except (FileNotFoundError, UnsupportedSchemaError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        logger.warning("Operação interrompida")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

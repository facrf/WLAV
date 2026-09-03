import argparse
import logging
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from ingestor.archive import export_archive, restore_archive
from ingestor.media_store import MediaStore, infer_media_type
from ingestor.sqlite_reader import ChatRecord, MsgstoreReader, UnsupportedSchemaError
from ingestor.writer import refresh_chat_bounds, upsert_chats, upsert_messages

logger = logging.getLogger("wlav.ingestor")


def _database_url(args: argparse.Namespace) -> str:
    return args.database_url or get_settings().database_url


def _engine(args: argparse.Namespace):
    return create_engine(_database_url(args), pool_pre_ping=True)


def ingest(args: argparse.Namespace) -> int:
    sqlite_path = Path(args.sqlite)
    media_dir = Path(args.media_dir) if args.media_dir else None
    media_root = Path(args.media_root or get_settings().media_root)
    store = MediaStore(media_dir, media_root)
    stats = {
        "chats": 0,
        "messages": 0,
        "media_copied": 0,
        "media_missing": 0,
    }

    with MsgstoreReader(sqlite_path) as reader:
        logger.info("Schema SQLite detectado: %s", reader.schema)
        initial_chats = reader.chats()
        known_chats = {chat.jid for chat in initial_chats}
        stats["chats"] = len(initial_chats)

        session = None if args.dry_run else Session(_engine(args))
        try:
            if session:
                upsert_chats(session, initial_chats)
                session.commit()

            batch: list[dict] = []
            pending_chats: list[ChatRecord] = []
            for message in reader.iter_messages(batch_size=args.batch_size):
                if message.chat_jid not in known_chats:
                    pending_chats.append(
                        ChatRecord(
                            jid=message.chat_jid,
                            name=message.chat_jid.split("@", 1)[0],
                            is_group=message.chat_jid.endswith("@g.us"),
                            created_at=message.timestamp,
                            last_message_time=message.timestamp,
                        )
                    )
                    known_chats.add(message.chat_jid)
                    stats["chats"] += 1

                media_path = None
                source = store.locate(message.media_reference)
                if source:
                    media_path = store.copy(
                        source,
                        message.chat_jid,
                        message.timestamp.strftime("%Y_%m"),
                        message.id,
                        dry_run=args.dry_run,
                    )
                    stats["media_copied"] += 1
                elif message.has_media:
                    stats["media_missing"] += 1

                media_type = message.media_type or infer_media_type(
                    message.media_mime, message.media_reference
                )
                batch.append(
                    {
                        "id": message.id,
                        "chat_jid": message.chat_jid,
                        "sender_jid": message.sender_jid,
                        "sender_name": message.sender_name,
                        "content": message.content,
                        "timestamp": message.timestamp,
                        "from_me": message.from_me,
                        "has_media": message.has_media,
                        "media_type": media_type,
                        "media_path": media_path,
                        "media_mime": message.media_mime,
                        "quoted_message_id": message.quoted_message_id,
                    }
                )
                stats["messages"] += 1

                if len(batch) >= args.batch_size:
                    if session:
                        upsert_chats(session, pending_chats)
                        upsert_messages(session, batch)
                        session.commit()
                    pending_chats.clear()
                    batch.clear()
                    logger.info("%s mensagens processadas", f"{stats['messages']:,}")

            if session:
                upsert_chats(session, pending_chats)
                upsert_messages(session, batch)
                refresh_chat_bounds(session)
                session.commit()
        except Exception:
            if session:
                session.rollback()
            raise
        finally:
            if session:
                session.close()

    mode = "SIMULAÇÃO" if args.dry_run else "CONCLUÍDO"
    print(
        f"{mode}: {stats['chats']} chats; {stats['messages']} mensagens; "
        f"{stats['media_copied']} mídias localizadas; {stats['media_missing']} ausentes."
    )
    return 0


def run_export(args: argparse.Namespace) -> int:
    with Session(_engine(args)) as session:
        stats = export_archive(session, Path(args.media_root), Path(args.output))
    print(
        f"EXPORTADO: {stats['chats']} chats; {stats['messages']} mensagens; "
        f"{stats['media']} mídias ({stats['missing_media']} ausentes)."
    )
    return 0


def run_restore(args: argparse.Namespace) -> int:
    with Session(_engine(args)) as session:
        try:
            stats = restore_archive(
                session, Path(args.media_root), Path(args.archive), args.batch_size
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    print(
        f"RESTAURADO: {stats['chats']} chats; {stats['messages']} mensagens; "
        f"{stats['media']} mídias."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wlav",
        description="Importação e portabilidade do WhatsApp Local Archive & Viewer",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Importa um msgstore.db")
    ingest_parser.add_argument(
        "--sqlite", required=True, help="Caminho do msgstore.db descriptografado"
    )
    ingest_parser.add_argument("--media-dir", help="Raiz das mídias exportadas do WhatsApp")
    ingest_parser.add_argument("--media-root", help="Destino das mídias organizadas")
    ingest_parser.add_argument("--database-url", help="URL SQLAlchemy do PostgreSQL")
    ingest_parser.add_argument("--batch-size", type=int, default=1_000)
    ingest_parser.add_argument(
        "--dry-run", action="store_true", help="Lê e valida sem alterar banco ou mídias"
    )
    ingest_parser.set_defaults(handler=ingest)

    export_parser = subparsers.add_parser("export", help="Cria um backup portátil .tar.gz")
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument("--media-root", default=str(get_settings().media_root))
    export_parser.add_argument("--database-url")
    export_parser.set_defaults(handler=run_export)

    restore_parser = subparsers.add_parser("restore", help="Restaura um backup portátil")
    restore_parser.add_argument("--archive", required=True)
    restore_parser.add_argument("--media-root", default=str(get_settings().media_root))
    restore_parser.add_argument("--database-url")
    restore_parser.add_argument("--batch-size", type=int, default=1_000)
    restore_parser.set_defaults(handler=run_restore)
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

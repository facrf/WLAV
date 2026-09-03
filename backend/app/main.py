import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.orm import Session

from ingestor.thumbnails import thumbnail_relative

from .config import get_settings
from .database import SessionLocal, engine, get_session
from .imports import run_import_job
from .models import Chat, ImportJob, Message
from .schemas import (
    ChatOut,
    ImportJobOut,
    MessageOut,
    MessagePage,
    QuoteOut,
    SearchPage,
    SearchResult,
)

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("wlav")
running_imports: set[asyncio.Task] = set()


def _resume_interrupted_imports() -> None:
    with SessionLocal() as session:
        jobs = list(
            session.scalars(select(ImportJob).where(ImportJob.status.in_(("queued", "running"))))
        )
        for job in jobs:
            upload_path = settings.import_root / job.id / "source.upload"
            if not upload_path.is_file():
                job.status = "failed"
                job.error = "Arquivo temporário ausente após reinicialização"
                job.completed_at = datetime.now(UTC)
        session.commit()
    for job in jobs:
        upload_path = settings.import_root / job.id / "source.upload"
        if not upload_path.is_file():
            continue
        task = asyncio.create_task(asyncio.to_thread(run_import_job, job.id, upload_path))
        running_imports.add(task)
        task.add_done_callback(running_imports.discard)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.media_root.mkdir(parents=True, exist_ok=True)
    settings.import_root.mkdir(parents=True, exist_ok=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    _resume_interrupted_imports()
    logger.info("WLAV iniciado; mídias em %s", settings.media_root)
    yield


app = FastAPI(
    title="WLAV API",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
SessionDep = Annotated[Session, Depends(get_session)]


def _encode_cursor(message: Message) -> str:
    payload = json.dumps(
        {"timestamp": message.timestamp.isoformat(), "id": message.id}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(data["timestamp"]), str(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Cursor inválido") from exc


def _message_out(message: Message, quotes: dict[str, Message] | None = None) -> MessageOut:
    quoted = quotes.get(message.quoted_message_id) if quotes and message.quoted_message_id else None
    quote_out = None
    if quoted:
        quote_out = QuoteOut(
            id=quoted.id,
            sender_name=quoted.sender_name,
            content=quoted.content,
            media_type=quoted.media_type,
        )
    return MessageOut(
        **{column.name: getattr(message, column.name) for column in Message.__table__.columns},
        quoted_message=quote_out,
        media_url=f"/media/{quote(message.media_path, safe='/')}" if message.media_path else None,
        thumbnail_url=(
            f"/media/{quote(thumbnail_relative(message.media_path), safe='/')}"
            if message.media_path and message.media_type in {"image", "sticker", "video"}
            else None
        ),
    )


def _quotes_for(session: Session, messages: list[Message]) -> dict[str, Message]:
    ids = {message.quoted_message_id for message in messages if message.quoted_message_id}
    if not ids:
        return {}
    quoted_messages = session.scalars(select(Message).where(Message.id.in_(ids)))
    return {message.id: message for message in quoted_messages}


@app.get("/api/health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/chats", response_model=list[ChatOut])
def list_chats(
    session: SessionDep,
    q: str | None = Query(default=None, max_length=200),
    is_group: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ChatOut]:
    media_preview = "[" + func.coalesce(Message.media_type, "mídia") + "]"
    preview = (
        select(func.coalesce(Message.content, media_preview))
        .where(Message.chat_jid == Chat.jid)
        .order_by(Message.timestamp.desc(), Message.id.desc())
        .limit(1)
        .correlate(Chat)
        .scalar_subquery()
    )
    stmt = select(Chat, preview.label("preview"))
    if q and q.strip():
        stmt = stmt.where(Chat.name.ilike(f"%{q.strip()}%"))
    if is_group is not None:
        stmt = stmt.where(Chat.is_group == is_group)
    stmt = (
        stmt.order_by(Chat.last_message_time.desc().nullslast(), Chat.name)
        .offset(offset)
        .limit(limit)
    )
    return [
        ChatOut.model_validate(chat).model_copy(update={"last_message_preview": message_preview})
        for chat, message_preview in session.execute(stmt)
    ]


@app.get("/api/chats/{jid}/messages", response_model=MessagePage)
def list_messages(
    jid: str,
    session: SessionDep,
    before: str | None = None,
    around: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=settings.api_page_size, ge=10, le=settings.api_max_page_size),
) -> MessagePage:
    if session.get(Chat, jid) is None:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")

    has_more = False
    if around:
        target = session.get(Message, around)
        if target is None or target.chat_jid != jid:
            raise HTTPException(status_code=404, detail="Mensagem não encontrada nesta conversa")
        older_limit = max(1, limit // 2)
        older_desc = list(
            session.scalars(
                select(Message)
                .where(
                    Message.chat_jid == jid,
                    or_(
                        Message.timestamp < target.timestamp,
                        and_(Message.timestamp == target.timestamp, Message.id <= target.id),
                    ),
                )
                .order_by(Message.timestamp.desc(), Message.id.desc())
                .limit(older_limit + 1)
            )
        )
        has_more = len(older_desc) > older_limit
        older_desc = older_desc[:older_limit]
        newer = list(
            session.scalars(
                select(Message)
                .where(
                    Message.chat_jid == jid,
                    or_(
                        Message.timestamp > target.timestamp,
                        and_(Message.timestamp == target.timestamp, Message.id > target.id),
                    ),
                )
                .order_by(Message.timestamp.asc(), Message.id.asc())
                .limit(max(0, limit - len(older_desc)))
            )
        )
        messages = list(reversed(older_desc)) + newer
    else:
        stmt = select(Message).where(Message.chat_jid == jid)
        if before:
            cursor_time, cursor_id = _decode_cursor(before)
            stmt = stmt.where(
                or_(
                    Message.timestamp < cursor_time,
                    and_(Message.timestamp == cursor_time, Message.id < cursor_id),
                )
            )
        descending = list(
            session.scalars(
                stmt.order_by(Message.timestamp.desc(), Message.id.desc()).limit(limit + 1)
            )
        )
        has_more = len(descending) > limit
        messages = list(reversed(descending[:limit]))

    quotes = _quotes_for(session, messages)
    return MessagePage(
        items=[_message_out(message, quotes) for message in messages],
        next_cursor=_encode_cursor(messages[0]) if messages and has_more else None,
        has_more=has_more,
    )


@app.get("/api/search", response_model=SearchPage)
def search_messages(
    session: SessionDep,
    q: str = Query(min_length=2, max_length=200),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    chat_jid: str | None = Query(default=None, max_length=100),
) -> SearchPage:
    language = literal_column("'portuguese'::regconfig")
    vector = func.to_tsvector(language, func.coalesce(Message.content, ""))
    query = func.websearch_to_tsquery(language, q.strip())
    rank = func.ts_rank_cd(vector, query).label("rank")
    highlight = func.ts_headline(
        language,
        func.coalesce(Message.content, ""),
        query,
        "StartSel=<mark>, StopSel=</mark>, MaxFragments=2, MinWords=8, MaxWords=24",
    ).label("highlight")
    stmt = (
        select(Message, Chat.name, Chat.is_group, highlight, rank)
        .join(Chat, Chat.jid == Message.chat_jid)
        .where(vector.op("@@")(query))
    )
    if chat_jid:
        stmt = stmt.where(Message.chat_jid == chat_jid)
    stmt = (
        stmt.order_by(rank.desc(), Message.timestamp.desc())
        .offset((page - 1) * limit)
        .limit(limit + 1)
    )
    rows = list(session.execute(stmt))
    has_more = len(rows) > limit
    rows = rows[:limit]
    messages = [row[0] for row in rows]
    quotes = _quotes_for(session, messages)
    return SearchPage(
        items=[
            SearchResult(
                message=_message_out(row[0], quotes),
                chat_name=row[1],
                is_group=row[2],
                highlight=row[3],
                rank=float(row[4]),
            )
            for row in rows
        ],
        page=page,
        has_more=has_more,
    )


@app.get("/api/imports", response_model=list[ImportJobOut])
def list_imports(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ImportJob]:
    return list(
        session.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit))
    )


@app.get("/api/imports/{job_id}", response_model=ImportJobOut)
def get_import(job_id: str, session: SessionDep) -> ImportJob:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Importação não encontrada")
    return job


@app.post("/api/imports", response_model=ImportJobOut, status_code=202)
async def create_import(
    background_tasks: BackgroundTasks,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="SQLite ou pacote ZIP/TAR/TAR.GZ")],
) -> ImportJob:
    job_id = str(uuid4())
    job_directory = settings.import_root / job_id
    job_directory.mkdir(parents=True, exist_ok=False)
    upload_path = job_directory / "source.upload"
    maximum = settings.upload_max_gb * 1024**3
    size = 0
    digest = hashlib.sha256()
    try:
        with upload_path.open("wb") as handle:
            while chunk := await file.read(4 * 1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload excede o limite de {settings.upload_max_gb} GB",
                    )
                digest.update(chunk)
                handle.write(chunk)
    except Exception:
        upload_path.unlink(missing_ok=True)
        job_directory.rmdir()
        raise
    finally:
        await file.close()

    job = ImportJob(
        id=job_id,
        filename=(file.filename or "backup")[:500],
        source_sha256=digest.hexdigest(),
        status="queued",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    background_tasks.add_task(run_import_job, job_id, upload_path)
    return job


@app.get("/media/{filepath:path}")
def serve_media(filepath: str, request: Request) -> FileResponse:
    del request  # FileResponse/Starlette trata Range e If-Range automaticamente.
    root = settings.media_root.resolve()
    requested = Path(filepath)
    if requested.is_absolute() or ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Caminho de mídia inválido")
    candidate = (root / requested).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Mídia não encontrada")
    mime, _ = mimetypes.guess_type(candidate.name)
    response = FileResponse(candidate, media_type=mime or "application/octet-stream")
    response.headers["Cache-Control"] = "private, max-age=3600, no-transform"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


if settings.frontend_root.is_dir():
    app.mount("/", StaticFiles(directory=settings.frontend_root, html=True), name="frontend")

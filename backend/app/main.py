import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import shutil
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
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from ingestor.key_store import WhatsAppKeyStore
from ingestor.thumbnails import thumbnail_relative
from ingestor.upload import safe_relative_path

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
    WhatsAppKeyInput,
    WhatsAppKeyStatus,
)

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("wlav")
running_imports: set[asyncio.Task] = set()
whatsapp_key_store = WhatsAppKeyStore(settings.whatsapp_key_path)

UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
IMPORT_STATUS_OPEN = ("queued", "running")

# A aplicação não tem autenticação (decisão de projeto documentada no README),
# então qualquer XSS daria acesso total ao arquivo. O front-end não usa estilo nem
# script inline, o que permite um CSP estrito sem `unsafe-inline`.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}
# O Swagger UI é servido de um CDN e usa script inline; um CSP estrito o quebraria.
_UNRESTRICTED_PATHS = ("/api/docs", "/api/openapi.json")

# Importações rodam uma de cada vez. Duas em paralelo duplicariam CPU, memória e
# I/O sem nenhum ganho, porque o UPSERT já torna reimportar seguro.
import_semaphore = asyncio.Semaphore(1)


def _import_source(job_id: str) -> Path:
    directory = settings.import_root / job_id
    uploaded_file = directory / "source.upload"
    return uploaded_file if uploaded_file.is_file() else directory / "source.directory"


async def _import_worker(job_id: str, upload_path: Path) -> None:
    async with import_semaphore:
        await asyncio.to_thread(run_import_job, job_id, upload_path)


def _recover_imports() -> list[str]:
    """Retoma importações interrompidas e descarta spools que não servem mais.

    Duas situações deixam lixo no volume. A primeira é o ImportJob gravado só
    depois de o upload terminar: um contêiner morto no meio do envio deixa
    diretório sem linha no banco. A segunda é o oposto — o job commitado e o
    processo morto antes do `rmtree` final. Nos dois casos o diretório ocupa
    espaço para sempre, já que uma importação pode chegar a 100 GB.
    """
    with SessionLocal() as session:
        jobs = list(session.scalars(select(ImportJob)))
        keep: set[str] = set()
        resumable: list[str] = []
        for job in jobs:
            if job.status not in IMPORT_STATUS_OPEN:
                continue  # spool obsoleto; a limpeza abaixo apaga
            if _import_source(job.id).exists():
                keep.add(job.id)
                resumable.append(job.id)
            else:
                job.status = "failed"
                job.error = "Arquivo temporário ausente após reinicialização"
                job.completed_at = datetime.now(UTC)
        session.commit()
    for entry in settings.import_root.iterdir():
        if entry.is_dir() and entry.name not in keep:
            logger.info("Descartando spool de importação sem uso: %s", entry.name)
            shutil.rmtree(entry, ignore_errors=True)
    return resumable


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.media_root.mkdir(parents=True, exist_ok=True)
    settings.import_root.mkdir(parents=True, exist_ok=True)
    settings.whatsapp_key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.whatsapp_key_path.parent.chmod(0o700)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    for job_id in _recover_imports():
        task = asyncio.create_task(_import_worker(job_id, _import_source(job_id)))
        running_imports.add(task)
        task.add_done_callback(running_imports.discard)
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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(_UNRESTRICTED_PATHS):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
    return response



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
    return MessageOut(
        id=message.id,
        chat_jid=message.chat_jid,
        sender_jid=message.sender_jid,
        sender_name=message.sender_name,
        content=message.content,
        timestamp=message.timestamp,
        from_me=message.from_me,
        has_media=message.has_media,
        media_type=message.media_type,
        media_path=message.media_path,
        media_mime=message.media_mime,
        quoted_message_id=message.quoted_message_id,
        quoted_message=(
            QuoteOut(
                id=quoted.id,
                sender_name=quoted.sender_name,
                content=quoted.content,
                media_type=quoted.media_type,
            )
            if quoted
            else None
        ),
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


@app.get("/api/settings/whatsapp-key", response_model=WhatsAppKeyStatus)
def whatsapp_key_status() -> WhatsAppKeyStatus:
    status = whatsapp_key_store.status()
    return WhatsAppKeyStatus(
        saved=status.saved,
        fingerprint=status.fingerprint,
        updated_at=status.updated_at,
    )


@app.put("/api/settings/whatsapp-key", response_model=WhatsAppKeyStatus)
def save_whatsapp_key(payload: WhatsAppKeyInput) -> WhatsAppKeyStatus:
    try:
        status = whatsapp_key_store.save(payload.key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WhatsAppKeyStatus(
        saved=status.saved,
        fingerprint=status.fingerprint,
        updated_at=status.updated_at,
    )


@app.delete("/api/settings/whatsapp-key", status_code=204)
def delete_whatsapp_key() -> None:
    whatsapp_key_store.delete()


@app.get("/api/imports/{job_id}", response_model=ImportJobOut)
def get_import(job_id: str, session: SessionDep) -> ImportJob:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Importação não encontrada")
    return job


async def _receive_upload(
    job_directory: Path, files: list[UploadFile]
) -> tuple[Path, str, str]:
    """Grava os arquivos enviados e devolve (origem, nome exibido, SHA-256).

    Erros de nome de arquivo são culpa do cliente (4xx); erros de escrita são do
    servidor (5xx). Sem essa distinção, um `../` no nome encerrava a importação
    com um 500 opaco e o usuário não tinha como saber o que corrigir.
    """
    try:
        relative_names = [
            safe_relative_path(item.filename or f"arquivo-{index}")
            for index, item in enumerate(files)
        ]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    is_directory = len(files) > 1 or any(len(path.parts) > 1 for path in relative_names)
    upload_path = job_directory / ("source.directory" if is_directory else "source.upload")
    if is_directory:
        upload_path.mkdir()
    maximum = settings.upload_max_gb * 1024**3
    size = 0
    digest = hashlib.sha256()
    root = upload_path.resolve() if is_directory else job_directory.resolve()
    targets: set[Path] = set()
    for item, relative in zip(files, relative_names, strict=True):
        target = upload_path / relative if is_directory else upload_path
        resolved = target.resolve()
        if not resolved.is_relative_to(root) or resolved in targets:
            raise HTTPException(status_code=400, detail="Caminho duplicado ou inseguro")
        targets.add(resolved)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest.update(relative.as_posix().encode())
        with target.open("wb") as handle:
            while chunk := await item.read(UPLOAD_CHUNK_SIZE):
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload excede o limite de {settings.upload_max_gb} GB",
                    )
                digest.update(chunk)
                handle.write(chunk)
    display_name = (
        f"{relative_names[0].parts[0]}/ ({len(files)} arquivos)"
        if is_directory
        else (files[0].filename or "backup")
    )
    return upload_path, display_name, digest.hexdigest()


@app.post(
    "/api/imports",
    response_model=ImportJobOut,
    status_code=202,
    # O corpo é lido manualmente para poder elevar o limite de arquivos do
    # multipart (o padrão do Starlette é 1000 e quebra o envio de uma pasta
    # Media/ real). O esquema abaixo mantém a documentação OpenAPI correta.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "description": (
                                    "TXT/ZIP exportado, SQLite, pacote ou os arquivos "
                                    "de uma pasta copiada do telefone"
                                ),
                            },
                            "owner_name": {
                                "type": "string",
                                "maxLength": 200,
                                "description": "Seu nome exatamente como aparece no TXT",
                            },
                            "date_order": {
                                "type": "string",
                                "enum": ["auto", "dmy", "mdy"],
                                "default": "auto",
                                "description": "Ordem das datas na exportação TXT",
                            },
                        },
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def create_import(
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
) -> ImportJob:
    try:
        form = await request.form(
            max_files=settings.import_max_files, max_fields=settings.import_max_fields
        )
    except MultiPartException as exc:
        raise HTTPException(status_code=400, detail=f"Upload inválido: {exc}") from exc

    try:
        files = [item for item in form.getlist("file") if isinstance(item, UploadFile)]
        if not files:
            raise HTTPException(status_code=422, detail="Selecione ao menos um arquivo")
        date_order = form.get("date_order") or "auto"
        if date_order not in {"auto", "dmy", "mdy"}:
            raise HTTPException(status_code=422, detail="Ordem de data inválida")

        job_id = str(uuid4())
        job_directory = settings.import_root / job_id
        job_directory.mkdir(parents=True, exist_ok=False)
        try:
            upload_path, display_name, source_digest = await _receive_upload(job_directory, files)
            (job_directory / "options.json").write_text(
                json.dumps(
                    {
                        "owner_name": (form.get("owner_name") or "").strip()[:200],
                        "date_order": date_order,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except HTTPException:
            shutil.rmtree(job_directory, ignore_errors=True)
            raise
        except (OSError, ValueError, MemoryError) as exc:
            shutil.rmtree(job_directory, ignore_errors=True)
            logger.exception("Falha ao gravar o upload %s", job_id)
            raise HTTPException(
                status_code=500, detail="Não foi possível gravar o arquivo enviado"
            ) from exc
    finally:
        # Fecha também os arquivos temporários do parser multipart, que sozinhos
        # ocupariam o volume do spool durante toda a importação.
        await form.close()

    job = ImportJob(
        id=job_id,
        filename=display_name[:500],
        source_sha256=source_digest,
        status="queued",
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    background_tasks.add_task(_import_worker, job_id, upload_path)
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

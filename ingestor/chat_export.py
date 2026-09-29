import hashlib
import io
import mimetypes
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ingestor.sqlite_reader import ChatRecord, MessageRecord

DATE = r"\d{1,4}[/.]\d{1,2}[/.]\d{2,4}"
TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[aApP](?:\.?\s*)[mM]\.?)?"
BRACKETED_HEADER = re.compile(rf"^\s*\[(?P<date>{DATE}),?\s+(?P<time>{TIME})\]\s*(?P<body>.*)$")
DASH_HEADER = re.compile(rf"^\s*(?P<date>{DATE}),?\s+(?P<time>{TIME})\s+[-–—]\s+(?P<body>.*)$")
SENDER = re.compile(r"^(?P<sender>[^:\n]{1,200}):\s(?P<content>.*)$", re.DOTALL)
ATTACHMENT = re.compile(
    r"(?:<\s*(?:attached|anexado)\s*:\s*(?P<bracket>[^>]+)>|"
    r"(?P<label>[^\r\n<>]+?\.[A-Za-z0-9]{1,10})\s*"
    r"\((?:file attached|arquivo anexado|ficheiro anexado)\))",
    re.IGNORECASE,
)
OMITTED_MEDIA = re.compile(
    r"<\s*(?:media omitted|m[ií]dia ocult[ao]|m[ií]dia omitid[ao]|arquivo omitido)\s*>",
    re.IGNORECASE,
)
TITLE_PREFIXES = (
    "whatsapp chat with ",
    "whatsapp chat - ",
    "conversa do whatsapp com ",
    "conversa de whatsapp com ",
    "chat do whatsapp com ",
)
INVISIBLE = str.maketrans("", "", "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff")
MAX_EXPORTED_LINE_CHARS = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExportMetadata:
    path: Path
    title: str
    chat_jid: str
    senders: frozenset[str]
    owner_name: str | None
    created_at: datetime
    last_message_time: datetime


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.translate(INVISIBLE)).casefold()
    return "".join(character for character in value if character.isalnum())


def _title_from_path(path: Path, fallback: str | None) -> str:
    title = path.stem.strip()
    if _normalized(title) in {"chat", "_chat"}:
        parent = path.parent.name.strip()
        title = Path(fallback).stem if fallback else parent
    for prefix in TITLE_PREFIXES:
        if title.casefold().startswith(prefix):
            title = title[len(prefix) :]
            break
    title = re.sub(r"\s*\(\d+\)$", "", title).strip(" _-")
    return title or "Conversa importada"


def _open_text(path: Path) -> io.TextIOWrapper:
    raw = path.open("rb")
    sample = raw.read(128 * 1024)
    raw.seek(0)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        try:
            sample.decode("utf-8-sig")
            encoding = "utf-8-sig"
        except UnicodeDecodeError:
            encoding = "cp1252"
    return io.TextIOWrapper(raw, encoding=encoding, errors="strict", newline=None)


def _parse_timestamp(date_value: str, time_value: str, order: str, zone: ZoneInfo) -> datetime:
    date_parts = [int(part) for part in re.split(r"[/.]", date_value)]
    if len(date_parts) != 3:
        raise ValueError(f"Data inválida na exportação: {date_value}")
    first, second, third = date_parts
    if first > 999:
        year, month, day = first, second, third
    else:
        resolved_order = order
        if order == "auto":
            resolved_order = "dmy" if first > 12 or second <= 12 else "mdy"
        day, month = (first, second) if resolved_order == "dmy" else (second, first)
        year = third
    if year < 100:
        year += 2000

    compact_time = re.sub(r"[.\s]", "", time_value).upper()
    suffix = compact_time[-2:] if compact_time[-2:] in {"AM", "PM"} else None
    clock = compact_time[:-2] if suffix else compact_time
    time_parts = [int(part) for part in clock.split(":")]
    hour, minute = time_parts[:2]
    second_value = time_parts[2] if len(time_parts) == 3 else 0
    if suffix:
        if not 1 <= hour <= 12:
            raise ValueError(f"Horário inválido na exportação: {time_value}")
        hour = hour % 12 + (12 if suffix == "PM" else 0)
    local = datetime(year, month, day, hour, minute, second_value, tzinfo=zone)
    return local.astimezone(UTC)


def _header(line: str) -> re.Match[str] | None:
    cleaned = line.translate(INVISIBLE).replace("\u202f", " ").replace("\xa0", " ")
    return BRACKETED_HEADER.match(cleaned) or DASH_HEADER.match(cleaned)


def _iter_raw_entries(
    path: Path, date_order: str, zone: ZoneInfo
) -> Iterator[tuple[datetime, str | None, str]]:
    current: tuple[datetime, str | None, list[str]] | None = None
    with _open_text(path) as handle:
        while raw_line := handle.readline(MAX_EXPORTED_LINE_CHARS + 1):
            if len(raw_line) > MAX_EXPORTED_LINE_CHARS and not raw_line.endswith(("\n", "\r")):
                raise ValueError(
                    f"Linha excessivamente longa na exportação {path.name}; arquivo recusado"
                )
            line = raw_line.rstrip("\r\n")
            match = _header(line)
            if match:
                if current:
                    yield current[0], current[1], "\n".join(current[2]).strip()
                timestamp = _parse_timestamp(
                    match.group("date"), match.group("time"), date_order, zone
                )
                body = match.group("body").strip()
                sender_match = SENDER.match(body)
                if sender_match:
                    sender = sender_match.group("sender").strip()
                    content = sender_match.group("content")
                else:
                    sender = None
                    content = body
                current = (timestamp, sender, [content])
            elif current:
                current[2].append(line)
    if current:
        yield current[0], current[1], "\n".join(current[2]).strip()


def _attachment(content: str) -> tuple[str | None, str | None, bool]:
    match = ATTACHMENT.search(content)
    if match:
        reference = (match.group("bracket") or match.group("label")).strip().translate(INVISIBLE)
        cleaned = (content[: match.start()] + content[match.end() :]).strip()
        return reference, cleaned or None, True
    omitted = OMITTED_MEDIA.search(content)
    if omitted:
        cleaned = (content[: omitted.start()] + content[omitted.end() :]).strip()
        return None, cleaned or None, True
    return None, content or None, False


def _sender_jid(sender: str | None) -> str | None:
    if not sender:
        return None
    digest = hashlib.sha256(_normalized(sender).encode()).hexdigest()[:24]
    return f"export-sender-{digest}@wlav.local"


def _infer_owner(title: str, senders: frozenset[str], configured: str | None) -> str | None:
    if configured:
        wanted = _normalized(configured)
        return next((sender for sender in senders if _normalized(sender) == wanted), configured)
    if len(senders) != 2:
        return None
    normalized_title = _normalized(title)
    contact = next(
        (
            sender
            for sender in senders
            if _normalized(sender) in normalized_title or normalized_title in _normalized(sender)
        ),
        None,
    )
    return next((sender for sender in senders if sender != contact), None) if contact else None


class WhatsAppExportReader:
    schema = "whatsapp_export"

    def __init__(
        self,
        transcripts: Sequence[Path],
        timezone: str = "America/Sao_Paulo",
        date_order: str = "auto",
        owner_name: str | None = None,
        fallback_title: str | None = None,
    ):
        if date_order not in {"auto", "dmy", "mdy"}:
            raise ValueError("Ordem de data deve ser auto, dmy ou mdy")
        try:
            self.zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Fuso horário desconhecido: {timezone}") from exc
        self.date_order = date_order
        self._metadata: list[ExportMetadata] = []
        for path in transcripts:
            resolved = path.resolve()
            title = _title_from_path(resolved, fallback_title)
            entries = _iter_raw_entries(resolved, date_order, self.zone)
            try:
                first = next(entries)
            except StopIteration as exc:
                raise ValueError(
                    f"Nenhuma mensagem reconhecida na exportação {path.name}. "
                    "Confirme se o arquivo foi gerado por Exportar conversa no WhatsApp."
                ) from exc
            senders = {first[1]} if first[1] else set()
            created_at = last_message_time = first[0]
            for timestamp, sender, _ in entries:
                if sender:
                    senders.add(sender)
                created_at = min(created_at, timestamp)
                last_message_time = max(last_message_time, timestamp)
            normalized_title = _normalized(title)
            chat_hash = hashlib.sha256(normalized_title.encode()).hexdigest()[:32]
            frozen_senders = frozenset(senders)
            self._metadata.append(
                ExportMetadata(
                    path=resolved,
                    title=title,
                    chat_jid=f"export-{chat_hash}@wlav.local",
                    senders=frozen_senders,
                    owner_name=_infer_owner(title, frozen_senders, owner_name),
                    created_at=created_at,
                    last_message_time=last_message_time,
                )
            )

    def __enter__(self) -> "WhatsAppExportReader":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def chats(self) -> list[ChatRecord]:
        combined: dict[str, ChatRecord] = {}
        for metadata in self._metadata:
            existing = combined.get(metadata.chat_jid)
            if existing:
                existing.created_at = min(
                    existing.created_at or metadata.created_at, metadata.created_at
                )
                existing.last_message_time = max(
                    existing.last_message_time or metadata.last_message_time,
                    metadata.last_message_time,
                )
                existing.is_group = existing.is_group or len(metadata.senders) > 2
            else:
                combined[metadata.chat_jid] = ChatRecord(
                    jid=metadata.chat_jid,
                    name=metadata.title,
                    is_group=len(metadata.senders) > 2,
                    created_at=metadata.created_at,
                    last_message_time=metadata.last_message_time,
                )
        return list(combined.values())

    def iter_messages(self, batch_size: int = 1_000) -> Iterator[MessageRecord]:
        del batch_size
        # Guarda um prefixo de 16 bytes do digest em vez do id completo: em uma
        # exportação de milhões de linhas, um set de strings de 71 caracteres
        # custaria centenas de MB. O prefixo continua sendo único na prática e o
        # id gravado no banco permanece o digest inteiro.
        seen: set[bytes] = set()
        for metadata in self._metadata:
            occurrences: Counter[str] = Counter()
            for timestamp, sender, raw_content in _iter_raw_entries(
                metadata.path, self.date_order, self.zone
            ):
                media_reference, content, has_media = _attachment(raw_content)
                mime = mimetypes.guess_type(media_reference or "")[0]
                signature = "\x1f".join(
                    (
                        metadata.chat_jid,
                        timestamp.isoformat(),
                        sender or "",
                        content or "",
                        media_reference or "",
                    )
                )
                occurrence = occurrences[signature]
                occurrences[signature] += 1
                raw_digest = hashlib.sha256(f"{signature}\x1f{occurrence}".encode()).digest()
                message_id = f"export-{raw_digest.hex()}"
                fingerprint = raw_digest[:16]
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                yield MessageRecord(
                    id=message_id,
                    chat_jid=metadata.chat_jid,
                    sender_jid=_sender_jid(sender),
                    sender_name=sender,
                    content=content,
                    timestamp=timestamp,
                    from_me=bool(
                        sender
                        and metadata.owner_name
                        and _normalized(sender) == _normalized(metadata.owner_name)
                    ),
                    has_media=has_media,
                    media_type=None,
                    media_reference=media_reference,
                    media_mime=mime,
                    quoted_message_id=None,
                )

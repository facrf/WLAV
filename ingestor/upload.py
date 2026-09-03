import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CHUNK_SIZE = 4 * 1024 * 1024
DATABASE_NAMES = ("msgstore.db", "chatstorage.sqlite")
ENCRYPTED_DATABASE = "msgstore"


@dataclass(frozen=True, slots=True)
class PreparedImport:
    kind: str
    database: Path | None
    transcripts: tuple[Path, ...]
    media_root: Path | None
    encrypted_database: Path | None = None


def is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def safe_relative_path(name: str) -> Path:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"Caminho inseguro no arquivo enviado: {name}")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise ValueError("Caminho vazio no arquivo enviado")
    return Path(*parts)


def _copy_stream(source, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    with partial.open("wb") as handle:
        shutil.copyfileobj(source, handle, length=CHUNK_SIZE)
    partial.replace(destination)


def _extract_zip(source: Path, destination: Path, max_bytes: int) -> None:
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        total = sum(member.file_size for member in members)
        if total > max_bytes:
            raise ValueError("Conteúdo descompactado excede o limite configurado")
        for member in members:
            relative = safe_relative_path(member.filename)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Link não permitido no arquivo: {member.filename}")
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(f"Caminho inseguro no arquivo: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                with archive.open(member) as input_handle:
                    _copy_stream(input_handle, target)


def _extract_tar(source: Path, destination: Path, max_bytes: int) -> None:
    with tarfile.open(source, "r:*") as archive:
        members = archive.getmembers()
        total = sum(member.size for member in members if member.isfile())
        if total > max_bytes:
            raise ValueError("Conteúdo descompactado excede o limite configurado")
        for member in members:
            relative = safe_relative_path(member.name)
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError(f"Entrada não permitida no arquivo: {member.name}")
            target = (destination / relative).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError(f"Caminho inseguro no arquivo: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            input_handle = archive.extractfile(member)
            if input_handle is not None:
                _copy_stream(input_handle, target)


def _looks_like_exported_chat(path: Path) -> bool:
    if path.suffix.casefold() != ".txt" or not path.is_file():
        return False
    with path.open("rb") as handle:
        sample = handle.read(128 * 1024)
    if not sample or b"\x00" in sample[:1_024]:
        return sample.startswith((b"\xff\xfe", b"\xfe\xff"))
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Exportações Android e iOS começam mensagens com uma data e horário.
        for line in text.splitlines()[:200]:
            stripped = line.translate({0x200E: None, 0x200F: None, 0xFEFF: None}).lstrip()
            begins_with_date = stripped[:1].isdigit() or (
                stripped.startswith("[") and stripped[1:2].isdigit()
            )
            if begins_with_date and (" - " in stripped or stripped.startswith("[")):
                return True
        return False
    return False


def _is_encrypted_database(path: Path) -> bool:
    name = path.name.casefold()
    return name.startswith(ENCRYPTED_DATABASE) and ".db.crypt" in name


def prepare_import_upload(
    source: Path,
    destination: Path,
    max_uncompressed_bytes: int,
    original_filename: str | None = None,
) -> PreparedImport:
    source = source.resolve()
    if source.is_file() and is_sqlite(source):
        return PreparedImport("sqlite", source, (), source.parent)

    scan_root = source
    if source.is_file():
        if _is_encrypted_database(Path(original_filename or source.name)):
            return PreparedImport("encrypted", None, (), source.parent, source)
        destination.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(source):
            _extract_zip(source, destination, max_uncompressed_bytes)
            scan_root = destination
        elif tarfile.is_tarfile(source):
            _extract_tar(source, destination, max_uncompressed_bytes)
            scan_root = destination
        else:
            suffix = Path(original_filename or "").suffix.casefold()
            if suffix == ".txt":
                safe_name = safe_relative_path(original_filename or "conversa.txt").name
                transcript = destination / safe_name
                shutil.copy2(source, transcript)
                scan_root = destination
            else:
                raise ValueError(
                    "Envie um TXT/ZIP exportado pelo WhatsApp, um SQLite ou um pacote "
                    "ZIP/TAR/TAR.GZ válido"
                )
    elif not source.is_dir():
        raise FileNotFoundError(f"Origem da importação não encontrada: {source}")

    candidates = [
        path
        for path in scan_root.rglob("*")
        if path.is_file() and path.name.casefold() in DATABASE_NAMES and is_sqlite(path)
    ]
    if candidates:
        candidates.sort(
            key=lambda path: (
                DATABASE_NAMES.index(path.name.casefold()),
                len(path.parts),
                path.as_posix(),
            )
        )
        return PreparedImport("sqlite", candidates[0], (), scan_root)

    encrypted_candidates = sorted(
        (path for path in scan_root.rglob("*") if path.is_file() and _is_encrypted_database(path)),
        key=lambda path: (
            not path.name.casefold().startswith("msgstore.db.crypt"),
            -int(path.name.casefold().rsplit("crypt", 1)[-1])
            if path.name.casefold().rsplit("crypt", 1)[-1].isdigit()
            else 0,
            path.as_posix(),
        ),
    )
    if encrypted_candidates:
        return PreparedImport(
            "encrypted",
            None,
            (),
            scan_root,
            encrypted_database=encrypted_candidates[0],
        )

    transcripts = tuple(
        sorted(
            (path for path in scan_root.rglob("*") if _looks_like_exported_chat(path)),
            key=lambda path: path.as_posix(),
        )
    )
    if transcripts:
        return PreparedImport("chat_export", None, transcripts, scan_root)
    raise ValueError(
        "Nenhum msgstore.db, ChatStorage.sqlite ou TXT exportado pelo WhatsApp foi encontrado"
    )


def prepare_whatsapp_upload(
    source: Path, destination: Path, max_uncompressed_bytes: int
) -> tuple[Path, Path | None]:
    """Compatibilidade com o importador SQLite original."""
    prepared = prepare_import_upload(source, destination, max_uncompressed_bytes)
    if prepared.database is None:
        raise ValueError("O pacote não contém msgstore.db nem ChatStorage.sqlite válido")
    return prepared.database, prepared.media_root

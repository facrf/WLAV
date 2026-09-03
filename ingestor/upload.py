import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

CHUNK_SIZE = 4 * 1024 * 1024
DATABASE_NAMES = ("msgstore.db", "chatstorage.sqlite")


def is_sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def _safe_relative(name: str) -> Path:
    pure = PurePosixPath(name.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"Caminho inseguro no arquivo enviado: {name}")
    return Path(*pure.parts)


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
            relative = _safe_relative(member.filename)
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
            relative = _safe_relative(member.name)
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


def prepare_whatsapp_upload(
    source: Path, destination: Path, max_uncompressed_bytes: int
) -> tuple[Path, Path | None]:
    if is_sqlite(source):
        return source, source.parent
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(source):
        _extract_zip(source, destination, max_uncompressed_bytes)
    elif tarfile.is_tarfile(source):
        _extract_tar(source, destination, max_uncompressed_bytes)
    else:
        raise ValueError("Envie um SQLite, ZIP, TAR ou TAR.GZ válido")

    candidates = [
        path
        for path in destination.rglob("*")
        if path.is_file() and path.name.casefold() in DATABASE_NAMES and is_sqlite(path)
    ]
    if not candidates:
        raise ValueError("O pacote não contém msgstore.db nem ChatStorage.sqlite válido")
    candidates.sort(
        key=lambda path: (
            DATABASE_NAMES.index(path.name.casefold()),
            len(path.parts),
            path.as_posix(),
        )
    )
    return candidates[0], destination

import contextlib
import hashlib
import mimetypes
import os
import re
import shutil
from pathlib import Path

SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9@._+-]+")
COMPARE_CHUNK_SIZE = 1024 * 1024
# O volume de mídia é gravado tanto pela aplicação (uid 10001 na imagem) quanto
# pelas ferramentas de linha de comando (WLAV_UID, que por padrão é outro uid).
# Sem estas permissões, `ingestor thumbnails` falharia com PermissionError ao
# gravar dentro de um diretório criado pela aplicação.
SHARED_DIR_MODE = 0o777
SHARED_FILE_MODE = 0o666


def safe_component(value: str, fallback: str) -> str:
    cleaned = SAFE_COMPONENT.sub("_", value).strip("._")
    return cleaned[:180] or fallback


def share_path(path: Path, directory: bool = False) -> None:
    """Ajusta as permissões de um item recém-criado dentro do volume de mídia."""
    # Sistemas de arquivos sem suporte a chmod (ex.: alguns bind mounts de rede)
    # não devem interromper a importação.
    with contextlib.suppress(OSError):
        path.chmod(SHARED_DIR_MODE if directory else SHARED_FILE_MODE)


def ensure_shared_dir(path: Path) -> None:
    """Cria o diretório deixando gravável cada nível que precisar ser criado.

    `mkdir(parents=True)` cria os intermediários com o umask padrão (0755), e um
    chmod no caminho final não alcança os pais. Sem ajustar todos os níveis, o
    outro uid não conseguiria criar o subdiretório seguinte dentro de um chat que
    já existe.
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in missing:
        share_path(directory, directory=True)


def _same_file(left: Path, right: Path) -> bool:
    """Compara conteúdo byte a byte, interrompendo no primeiro bloco diferente.

    Calcular dois SHA-256 leria os arquivos inteiros mesmo quando a diferença
    aparece no primeiro bloco, o que dobrava a I/O do importador em arquivos
    grandes com nomes repetidos.
    """
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(COMPARE_CHUNK_SIZE)
                right_chunk = right_handle.read(COMPARE_CHUNK_SIZE)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


class MediaStore:
    def __init__(self, source_root: Path | None, target_root: Path):
        self.source_root = source_root.resolve() if source_root else None
        self.target_root = target_root.resolve()
        self._by_name: dict[str, str] | None = None

    def _build_index(self) -> None:
        """Índice de fallback: nome em minúsculas -> caminho relativo à origem.

        Guarda apenas strings, e não objetos Path, porque um backup real traz
        centenas de milhares de arquivos e cada Path consome memória demais para
        uma lista. A travessia usa os.scandir com os tipos do diretório em cache
        e ignora links, então não há como escapar da raiz; `locate` ainda
        revalida o caminho resolvido antes de devolver.
        """
        self._by_name = {}
        if not self.source_root or not self.source_root.is_dir():
            return
        pending = [self.source_root]
        while pending:
            current = pending.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in sorted(entries, key=lambda item: item.name):
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        relative = os.path.relpath(entry.path, self.source_root)
                        self._by_name.setdefault(entry.name.casefold(), relative)
                except OSError:
                    continue

    def locate(self, reference: str | None) -> Path | None:
        if not reference or not self.source_root or not self.source_root.is_dir():
            return None
        normalized = reference.replace("\\", "/").lstrip("/")
        parts = [part for part in normalized.split("/") if part not in ("", ".", "..")]
        candidates: list[Path] = []
        if parts:
            candidates.append(self.source_root.joinpath(*parts))
            lowered = [part.casefold() for part in parts]
            if "media" in lowered:
                media_index = lowered.index("media")
                candidates.append(self.source_root.joinpath(*parts[media_index + 1 :]))
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(self.source_root) and resolved.is_file():
                return resolved
        if self._by_name is None:
            self._build_index()
        relative = self._by_name.get(Path(normalized).name.casefold()) if self._by_name else None
        if relative is None:
            return None
        resolved = (self.source_root / relative).resolve()
        if not resolved.is_relative_to(self.source_root) or not resolved.is_file():
            return None
        return resolved

    def copy(
        self,
        source: Path,
        chat_jid: str,
        year_month: str,
        message_id: str,
        dry_run: bool = False,
    ) -> str:
        filename = safe_component(source.name, "media.bin")
        chat_directory = safe_component(chat_jid, "chat")
        relative = Path(chat_directory) / year_month / filename
        destination = self.target_root / relative

        if destination.exists() and not _same_file(source, destination):
            suffix = hashlib.sha256(message_id.encode()).hexdigest()[:10]
            stem = safe_component(source.stem, "media")
            extension_value = safe_component(source.suffix.lstrip("."), "")
            extension = f".{extension_value}" if extension_value else ""
            relative = Path(chat_directory) / year_month / f"{stem}_{suffix}{extension}"
            destination = self.target_root / relative

        if not dry_run:
            ensure_shared_dir(destination.parent)
            if not destination.exists() or not _same_file(source, destination):
                shutil.copy2(source, destination)
                share_path(destination)
        return relative.as_posix()


def infer_media_type(mime: str | None, filename: str | None) -> str | None:
    guessed = mime or (mimetypes.guess_type(filename or "")[0])
    if not guessed:
        return None
    if guessed.startswith("image/"):
        return "sticker" if guessed == "image/webp" else "image"
    if guessed.startswith("audio/"):
        return "audio"
    if guessed.startswith("video/"):
        return "video"
    return "document"

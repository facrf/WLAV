import hashlib
import mimetypes
import re
import shutil
from collections import defaultdict
from pathlib import Path

SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9@._+-]+")


def safe_component(value: str, fallback: str) -> str:
    cleaned = SAFE_COMPONENT.sub("_", value).strip("._")
    return cleaned[:180] or fallback


def _same_file(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    left_hash = hashlib.sha256()
    right_hash = hashlib.sha256()
    with left.open("rb") as left_handle, right.open("rb") as right_handle:
        while True:
            left_chunk = left_handle.read(1024 * 1024)
            right_chunk = right_handle.read(1024 * 1024)
            if not left_chunk and not right_chunk:
                break
            left_hash.update(left_chunk)
            right_hash.update(right_chunk)
    return left_hash.digest() == right_hash.digest()


class MediaStore:
    def __init__(self, source_root: Path | None, target_root: Path):
        self.source_root = source_root.resolve() if source_root else None
        self.target_root = target_root.resolve()
        self._by_name: dict[str, list[Path]] | None = None

    def _build_index(self) -> None:
        self._by_name = defaultdict(list)
        if not self.source_root or not self.source_root.is_dir():
            return
        for path in self.source_root.rglob("*"):
            if path.is_file() and path.resolve().is_relative_to(self.source_root):
                self._by_name[path.name.casefold()].append(path)
        for matches in self._by_name.values():
            matches.sort(key=lambda item: item.as_posix())

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
        matches = self._by_name.get(Path(normalized).name.casefold(), []) if self._by_name else []
        return matches[0] if matches else None

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
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or not _same_file(source, destination):
                shutil.copy2(source, destination)
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

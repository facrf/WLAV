import logging
import subprocess
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Message
from ingestor.media_store import ensure_shared_dir, share_path

logger = logging.getLogger("wlav.thumbnails")


def thumbnail_relative(media_path: str) -> str:
    source = Path(media_path)
    return (source.parent / ".thumbs" / f"{source.name}.jpg").as_posix()


class Thumbnailer:
    def __init__(self, media_root: Path, max_size: int = 480):
        self.media_root = media_root.resolve()
        self.max_size = max_size

    def generate(self, media_path: str, media_type: str | None) -> str | None:
        if media_type not in {"image", "sticker", "video"}:
            return None
        source = (self.media_root / media_path).resolve()
        if not source.is_relative_to(self.media_root) or not source.is_file():
            return None
        relative = thumbnail_relative(media_path)
        destination = (self.media_root / relative).resolve()
        if not destination.is_relative_to(self.media_root):
            return None
        if (
            destination.is_file()
            and destination.stat().st_size > 0
            and destination.stat().st_mtime >= source.stat().st_mtime
        ):
            return relative
        ensure_shared_dir(destination.parent)
        partial = destination.with_name(destination.name + ".part.jpg")
        try:
            if media_type in {"image", "sticker"}:
                self._image(source, partial)
            else:
                self._video(source, partial)
            share_path(partial)
            partial.replace(destination)
            return relative
        except (OSError, subprocess.SubprocessError, UnidentifiedImageError) as exc:
            # PermissionError aqui quase sempre significa que a pasta .thumbs foi
            # criada pela aplicação com outro uid; o comando `thumbnails` contava
            # isso como "ignorado" e a falha passava despercebida.
            logger.warning("Não foi possível gerar thumbnail de %s: %s", media_path, exc)
            partial.unlink(missing_ok=True)
            return None

    def _image(self, source: Path, destination: Path) -> None:
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original)
            if image.mode not in {"RGB", "L"}:
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            image.thumbnail((self.max_size, self.max_size), Image.Resampling.LANCZOS)
            image.save(destination, "JPEG", quality=82, optimize=True)

    def _video(self, source: Path, destination: Path) -> None:
        command = [
            get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"thumbnail,scale={self.max_size}:-2:force_original_aspect_ratio=decrease",
            str(destination),
        ]
        subprocess.run(command, check=True, timeout=120, capture_output=True)


def rebuild_thumbnails(session: Session, thumbnailer: Thumbnailer) -> dict[str, int]:
    """Regera previews ausentes, distinguindo o que falhou do que não se aplica."""
    stats = {"generated": 0, "skipped": 0, "failed": 0}
    messages = session.execute(
        select(Message.media_path, Message.media_type).where(
            Message.media_path.is_not(None),
            Message.media_type.in_(("image", "sticker", "video")),
        )
    ).yield_per(500)
    for media_path, media_type in messages:
        if not media_path:
            continue
        try:
            resolved = (thumbnailer.media_root / media_path).resolve()
        except OSError:
            stats["failed"] += 1
            continue
        if not resolved.is_relative_to(thumbnailer.media_root) or not resolved.is_file():
            # A mídia sumiu do volume; não há preview a produzir.
            stats["skipped"] += 1
            continue
        try:
            ensure_shared_dir((thumbnailer.media_root / thumbnail_relative(media_path)).parent)
        except OSError as exc:
            logger.error("Sem permissão para gravar thumbnails de %s: %s", media_path, exc)
            stats["failed"] += 1
            continue
        if thumbnailer.generate(media_path, media_type):
            stats["generated"] += 1
        else:
            stats["failed"] += 1
    if stats["failed"]:
        logger.error(
            "%d thumbnail(s) não puderam ser gravados; verifique as permissões de %s",
            stats["failed"],
            thumbnailer.media_root,
        )
    return stats

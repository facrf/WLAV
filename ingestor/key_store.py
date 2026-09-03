import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SEPARATORS = re.compile(r"[\s-]+")
HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_whatsapp_key(value: str) -> str:
    normalized = SEPARATORS.sub("", value)
    if not HEX_KEY.fullmatch(normalized):
        raise ValueError("A chave deve conter exatamente 64 caracteres hexadecimais (0-9 e A-F)")
    return normalized.lower()


def key_fingerprint(value: str) -> str:
    return hashlib.sha256(bytes.fromhex(value)).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class KeyStatus:
    saved: bool
    fingerprint: str | None = None
    updated_at: datetime | None = None


class WhatsAppKeyStore:
    def __init__(self, path: Path):
        self.path = path.resolve()

    def status(self) -> KeyStatus:
        if not self.path.is_file():
            return KeyStatus(saved=False)
        key = self.load()
        return KeyStatus(
            saved=True,
            fingerprint=key_fingerprint(key),
            updated_at=datetime.fromtimestamp(self.path.stat().st_mtime, tz=UTC),
        )

    def load(self) -> str:
        if not self.path.is_file():
            raise ValueError(
                "Nenhuma chave do WhatsApp está salva. Cadastre a chave de 64 caracteres "
                "antes de importar um arquivo .crypt15."
            )
        mode = self.path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(
                f"Permissões inseguras no arquivo da chave ({mode:o}); use somente 0600"
            )
        return normalize_whatsapp_key(self.path.read_text(encoding="ascii"))

    def save(self, value: str) -> KeyStatus:
        key = normalize_whatsapp_key(value)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return self.status()

    def delete(self) -> bool:
        existed = self.path.is_file()
        self.path.unlink(missing_ok=True)
        return existed

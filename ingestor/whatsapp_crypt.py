import logging
import subprocess
from pathlib import Path

from ingestor.upload import is_sqlite

logger = logging.getLogger("wlav.whatsapp_crypt")


def decrypt_whatsapp_database(
    source: Path,
    destination: Path,
    key_path: Path,
    command: str = "wlav-wadecrypt",
) -> Path:
    source = source.resolve()
    key_path = key_path.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Backup criptografado não encontrado: {source}")
    if not key_path.is_file():
        raise ValueError("Nenhuma chave de 64 caracteres está salva no WLAV")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [command, str(key_path), str(source), str(destination)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Componente de descriptografia não instalado na imagem") from exc

    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        logger.warning(
            "Descriptografia recusada para %s (código %d): %s",
            source.name,
            result.returncode,
            result.stdout[-2_000:].strip(),
        )
        raise ValueError(
            "Não foi possível descriptografar o backup. Confira se a chave pertence a "
            "esse arquivo e se ele é um .crypt15 íntegro."
        )
    if not destination.is_file() or not is_sqlite(destination):
        destination.unlink(missing_ok=True)
        raise ValueError(
            "O backup foi aberto, mas o conteúdo resultante não é um banco SQLite compatível"
        )
    destination.chmod(0o600)
    return destination

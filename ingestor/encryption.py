import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"WLAVENC1"
SALT_SIZE = 16
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 4 * 1024 * 1024


def is_encrypted(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(len(MAGIC)) == MAGIC


def _key(password: str, salt: bytes) -> bytes:
    if len(password) < 12:
        raise ValueError("A senha do backup deve ter pelo menos 12 caracteres")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def encrypt_file(source: Path, destination: Path, password: str) -> None:
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    header = MAGIC + salt + nonce
    encryptor = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    partial = destination.with_name(destination.name + ".part")
    try:
        with source.open("rb") as input_handle, partial.open("wb") as output_handle:
            output_handle.write(header)
            while chunk := input_handle.read(CHUNK_SIZE):
                output_handle.write(encryptor.update(chunk))
            output_handle.write(encryptor.finalize())
            output_handle.write(encryptor.tag)
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def decrypt_file(source: Path, destination: Path, password: str) -> None:
    total_size = source.stat().st_size
    header_size = len(MAGIC) + SALT_SIZE + NONCE_SIZE
    if total_size <= header_size + TAG_SIZE:
        raise ValueError("Arquivo cifrado incompleto")
    with source.open("rb") as input_handle:
        header = input_handle.read(header_size)
        if not header.startswith(MAGIC):
            raise ValueError("O arquivo não é um backup WLAV cifrado")
        salt = header[len(MAGIC) : len(MAGIC) + SALT_SIZE]
        nonce = header[-NONCE_SIZE:]
        input_handle.seek(-TAG_SIZE, os.SEEK_END)
        tag = input_handle.read(TAG_SIZE)
        input_handle.seek(header_size)
        remaining = total_size - header_size - TAG_SIZE
        decryptor = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        partial = destination.with_name(destination.name + ".part")
        try:
            with partial.open("wb") as output_handle:
                while remaining:
                    chunk = input_handle.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ValueError("Arquivo cifrado truncado")
                    remaining -= len(chunk)
                    output_handle.write(decryptor.update(chunk))
                output_handle.write(decryptor.finalize())
            partial.replace(destination)
        except InvalidTag as exc:
            partial.unlink(missing_ok=True)
            raise ValueError("Senha incorreta ou backup cifrado corrompido") from exc
        except Exception:
            partial.unlink(missing_ok=True)
            raise

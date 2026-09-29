import os

import pytest

from ingestor.media_store import (
    MediaStore,
    infer_media_type,
    safe_component,
    share_path,
)


def test_media_store_locates_android_path_and_copies_idempotently(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    image = source / "WhatsApp Images" / "IMG-001.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"first-image")
    store = MediaStore(source, target)

    located = store.locate("/storage/emulated/0/WhatsApp/Media/WhatsApp Images/IMG-001.jpg")
    assert located == image
    relative = store.copy(located, "1200@g.us", "2026_09", "MSG-1")
    repeated = store.copy(located, "1200@g.us", "2026_09", "MSG-1")

    assert relative == repeated == "1200@g.us/2026_09/IMG-001.jpg"
    assert (target / relative).read_bytes() == b"first-image"


def test_media_store_avoids_filename_collision(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    first = source / "one" / "file.pdf"
    second = source / "two" / "file.pdf"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    store = MediaStore(source, target)

    first_path = store.copy(first, "chat@s.whatsapp.net", "2026_09", "A")
    second_path = store.copy(second, "chat@s.whatsapp.net", "2026_09", "B")

    assert first_path != second_path
    assert (target / first_path).read_bytes() == b"one"
    assert (target / second_path).read_bytes() == b"two"


def test_media_helpers():
    assert safe_component("../bad/chat", "chat") == "bad_chat"
    assert infer_media_type("audio/ogg", None) == "audio"
    assert infer_media_type(None, "sticker.webp") == "sticker"


# --- Busca pelo índice de nomes ---------------------------------------------
#
# O índice é o caminho de fallback para when a exportação traz só o nome do
# arquivo. Ele guardava objetos Path para cada item do backup, o que consome
# centenas de MB numa pasta com meio milhão de arquivos.


def test_locate_falls_back_to_the_name_index(tmp_path):
    source = tmp_path / "source"
    deep = source / "a" / "b" / "c"
    deep.mkdir(parents=True)
    hidden = deep / "Foto Com Acento.jpg"
    hidden.write_bytes(b"x")
    store = MediaStore(source, tmp_path / "target")

    # Só o nome, sem nenhum trecho do caminho: é o caso que o índice resolve.
    assert store.locate("Foto Com Acento.jpg") == hidden


def test_locate_ignores_case_differences_in_the_name_index(tmp_path):
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    image = source / "sub" / "IMG-001.JPG"
    image.write_bytes(b"x")
    store = MediaStore(source, tmp_path / "target")

    assert store.locate("img-001.jpg") == image


def test_locate_prefers_the_direct_path_over_the_index(tmp_path):
    """O caminho explícito da mensagem é mais confiável que o índice por nome."""
    source = tmp_path / "source"
    first = source / "WhatsApp Images" / "foto.jpg"
    second = source / "Backup Antigo" / "foto.jpg"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"nova")
    second.write_bytes(b"antiga")
    store = MediaStore(source, tmp_path / "target")

    assert store.locate("WhatsApp Images/foto.jpg") == first
    # O índice só entra quando o caminho direto não existe.
    assert store.locate("outro/lugar/foto.jpg") in {first, second}


def test_locate_returns_none_for_a_missing_reference(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    store = MediaStore(source, tmp_path / "target")

    assert store.locate("nao-existe.jpg") is None
    assert store.locate(None) is None
    assert store.locate("") is None


def test_locate_never_follows_a_symlink_out_of_the_source(tmp_path):
    """Um backup pode conter links apontando para fora da pasta. O índice não
    pode seguir esses links, e o caminho direto precisa revalidar a resolução."""
    outside = tmp_path / "fora"
    outside.mkdir()
    secret = outside / "segredo.jpg"
    secret.write_bytes(b"privado")

    source = tmp_path / "source"
    (source / "Media").mkdir(parents=True)
    os.symlink(secret, source / "Media" / "segredo.jpg")
    os.symlink(outside, source / "atalho")
    store = MediaStore(source, tmp_path / "target")

    assert store.locate("Media/segredo.jpg") is None
    assert store.locate("atalho/segredo.jpg") is None
    assert store.locate("segredo.jpg") is None


def test_name_index_resolves_deterministically(tmp_path):
    """Com o mesmo nome em duas pastas, o índice precisa devolver sempre o
    mesmo arquivo; do contrário uma reimportação mudaria o `media_path`."""
    source = tmp_path / "source"
    for folder in ("z-pasta", "a-pasta"):
        (source / folder).mkdir(parents=True)
        (source / folder / "foto.jpg").write_bytes(folder.encode())

    first = MediaStore(source, tmp_path / "t1").locate("foto.jpg")
    second = MediaStore(source, tmp_path / "t2").locate("foto.jpg")
    assert first == second
    assert first.read_bytes() in {b"z-pasta", b"a-pasta"}


def test_name_index_of_a_huge_source_stays_cheap(tmp_path, monkeypatch):
    """O índice guarda `dict[str, str]`, não objetos Path. Esta é a razão de a
    memória deixar de crescer de forma inviável em backups grandes."""
    source = tmp_path / "source"
    (source / "Media").mkdir(parents=True)
    for index in range(500):
        (source / "Media" / f"foto{index:04}.jpg").write_bytes(b"x")

    store = MediaStore(source, tmp_path / "target")
    store.locate("foto0000.jpg")
    store.locate("inexistente.jpg")  # força a construção do índice

    index = store._by_name
    assert len(index) == 500
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in index.items())
    assert all(not isinstance(value, os.PathLike) for value in index.values())


# --- Comparação de conteúdo --------------------------------------------------


def test_same_file_compares_content_not_metadata(tmp_path):
    """A cópia idempotente depende desta comparação. Dois arquivos com o mesmo
    tamanho e mtime, mas conteúdo diferente, precisam ser considerados distintos.
    """
    from ingestor.media_store import _same_file

    left = tmp_path / "a.bin"
    right = tmp_path / "b.bin"
    left.write_bytes(b"a" * 4096)
    right.write_bytes(b"a" * 4096)
    assert _same_file(left, right) is True

    right.write_bytes(b"b" * 4096)
    assert _same_file(left, right) is False

    right.write_bytes(b"a" * 2048)
    assert _same_file(left, right) is False

    right.unlink()
    assert _same_file(left, right) is False


def test_same_file_detects_a_difference_past_the_first_chunk(tmp_path):
    """A comparação percorre em blocos e interrompe no primeiro diferente; a
    diferença precisa ser encontrada mesmo depois de vários blocos iguais."""
    from ingestor.media_store import _same_file

    left = tmp_path / "a.bin"
    right = tmp_path / "b.bin"
    left.write_bytes(b"x" * (5 * 1024 * 1024))
    right.write_bytes(b"x" * (5 * 1024 * 1024) + b"diferente")
    assert _same_file(left, right) is False


# --- Permissões do volume compartilhado --------------------------------------
#
# A aplicação roda como uid 10001 e as ferramentas de linha de comando como
# WLAV_UID. Sem permissões compartilhadas, `thumbnails` falhava com
# PermissionError ao gravar dentro de um diretório criado pela aplicação.


def test_copied_media_is_writable_by_both_uids(tmp_path):
    source = tmp_path / "source"
    image = source / "Media" / "foto.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"conteudo")
    target = tmp_path / "target"
    # A origem costuma vir com 0644 (lido pelo bind mount do host).
    image.chmod(0o644)

    relative = MediaStore(source, target).copy(image, "chat@s.whatsapp.net", "2026_09", "M1")

    assert oct((target / relative).stat().st_mode)[-3:] == "666"
    for directory in (target, target / "chat@s.whatsapp.net", (target / relative).parent):
        assert oct(directory.stat().st_mode)[-3:] == "777"


def test_share_path_tolerates_a_missing_file(tmp_path):
    """Um bind mount de rede sem suporte a chmod não pode derrubar a importação."""
    share_path(tmp_path / "nao-existe", directory=True)
    target = tmp_path / "arquivo"
    target.write_bytes(b"x")
    share_path(target)


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/../../b"])
def test_safe_component_never_escapes(tmp_path, bad):
    cleaned = safe_component(bad, "reserva")
    assert not cleaned.startswith(".")
    assert "/" not in cleaned and "\\" not in cleaned

from ingestor.media_store import MediaStore, infer_media_type, safe_component


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

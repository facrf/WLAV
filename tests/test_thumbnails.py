from PIL import Image

from ingestor.thumbnails import Thumbnailer, thumbnail_relative


def test_generates_small_image_thumbnail(tmp_path):
    media = tmp_path / "chat/2026_09/photo.png"
    media.parent.mkdir(parents=True)
    Image.new("RGB", (1600, 900), "#167a5b").save(media)

    thumbnailer = Thumbnailer(tmp_path, max_size=320)
    relative = thumbnailer.generate("chat/2026_09/photo.png", "image")

    assert relative == thumbnail_relative("chat/2026_09/photo.png")
    with Image.open(tmp_path / relative) as result:
        assert max(result.size) <= 320

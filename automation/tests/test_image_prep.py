import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image as PILImage

from src import image_prep


def _jpeg_bytes(size, color=(100, 100, 100)) -> bytes:
    im = PILImage.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_oversized_image_is_downscaled_preserving_aspect_ratio():
    original = _jpeg_bytes((850, 637))
    resized_bytes = image_prep.resize_for_classification(original, max_dimension=768)
    with PILImage.open(io.BytesIO(resized_bytes)) as im:
        assert max(im.size) <= 768
        # 850x637 aspect ratio ~1.335; verify it's preserved within rounding
        assert abs(im.width / im.height - 850 / 637) < 0.01


def test_already_small_image_is_returned_unchanged():
    original = _jpeg_bytes((400, 300))
    result = image_prep.resize_for_classification(original, max_dimension=768)
    assert result == original  # byte-identical: no pointless re-encode


def test_never_upscales():
    original = _jpeg_bytes((300, 200))
    result = image_prep.resize_for_classification(original, max_dimension=768)
    with PILImage.open(io.BytesIO(result)) as im:
        assert im.size == (300, 200)


def test_custom_max_dimension_respected():
    original = _jpeg_bytes((850, 637))
    resized_bytes = image_prep.resize_for_classification(original, max_dimension=512)
    with PILImage.open(io.BytesIO(resized_bytes)) as im:
        assert max(im.size) <= 512
